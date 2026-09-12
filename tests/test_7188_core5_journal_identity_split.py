"""Regression test for #7188 CORE blocker #5: run-journal identity split
across compression + cancel.

When a turn is auto-compressed mid-run and then cancelled, the run's events
must NOT be split across two session journals. The immutable journal session
ID (captured at run admission) must be used for every fence operation AND
every terminal-event publish. The continuation ID (rotated by compression into
agent.session_id) must be used ONLY for session/scene persistence.

Before the fix:
  - ACTIVE_RUNS and the run writer kept the ORIGINAL session ID.
  - Compression rotated agent.session_id to the CONTINUATION ID.
  - The acceptance fence closed against the ORIGINAL ID.
  - But the cancel terminal was journaled under the CONTINUATION ID
    (cancel_stream resolved _cancel_session_id = agent.session_id first).
  - Result: original journal had only steer_delivered, continuation journal
    had only cancel. find_run_summary() returned only the first (incomplete)
    journal. The original acceptance fence stayed closed-but-retained (leaked).

After the fix:
  - cancel_stream captures _journal_session_id from ACTIVE_RUNS (the original
    admission ID) and uses it for BOTH the fence close AND the cancel terminal
    journal. The run's events stay in ONE journal.
"""
import queue
import threading
import time
from unittest.mock import MagicMock, Mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api import run_journal
from api.models import Session


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    # Isolate run journal to tmp_path.
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: session_dir)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    run_journal._ACCEPTANCE_FENCE.clear()
    yield
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    run_journal._ACCEPTANCE_FENCE.clear()


def test_cancel_after_compression_journals_under_original_session_id(tmp_path, monkeypatch):
    """CORE #5: mid-run compression rotates agent.session_id to a continuation
    ID, but cancel_stream must journal the cancel terminal under the ORIGINAL
    session ID (the immutable journal identity captured at run admission).

    Asserts:
    1. The ORIGINAL journal contains BOTH steer_delivered AND cancel.
    2. NO continuation journal is created for this run.
    3. find_run_summary() returns the complete journal (both events).
    """
    original_sid = "orig_sid_7188_core5"
    continuation_sid = "continuation_sid_7188_core5"
    stream_id = "stream_7188_core5"

    # 1. Set up the session with the ORIGINAL session ID.
    s = Session(
        session_id=original_sid,
        title="Compression + cancel test",
        messages=[{"role": "user", "content": "long task"}],
    )
    s.active_stream_id = stream_id
    s.pending_user_message = "long task"
    s.pending_started_at = time.time()
    s.pending_user_source = "webui"
    s.save()
    models.SESSIONS[original_sid] = s

    # 2. Register the run with the ORIGINAL session ID (as register_active_run
    #    does at admission, BEFORE any compression rotation).
    config.register_stream_owner(stream_id, original_sid)
    config.register_session_writeback_owner(original_sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()

    # 3. Create a RunJournalWriter with the ORIGINAL session ID (as the
    #    admission code does at streaming.py:8708) and journal a steer_delivered
    #    event — this is the event that was already in the journal before cancel.
    admission_writer = run_journal.RunJournalWriter(
        original_sid, stream_id, session_dir=tmp_path / "sessions"
    )
    admission_writer.append_and_publish_sse_event(
        "steer_delivered",
        {"session_id": original_sid, "stream_id": stream_id, "text": "steer text", "status": "delivered"},
        lambda event: None,  # no live SSE in test
    )

    # Verify the steer_delivered event landed in the ORIGINAL journal.
    original_journal = run_journal.read_run_events(
        original_sid, stream_id, session_dir=tmp_path / "sessions"
    )
    assert any(e["event"] == "steer_delivered" for e in original_journal["events"]), (
        "steer_delivered must be in the ORIGINAL journal before compression"
    )

    # 4. Simulate compression: the agent's session_id has rotated to the
    #    CONTINUATION ID. This is the core of the bug — agent.session_id is
    #    now the continuation, but ACTIVE_RUNS still has the original.
    agent = MagicMock()
    agent.session_id = continuation_sid  # compression rotated this
    agent.interrupt = Mock(return_value=True)
    agent._drain_pending_steer = Mock(return_value=None)
    config.AGENT_INSTANCES[stream_id] = agent

    # ACTIVE_RUNS still has the ORIGINAL session_id (set at admission).
    config.register_active_run(stream_id, session_id=original_sid, phase="running")

    # 5. Call cancel_stream() — this is where the bug manifested.
    #    Before the fix, cancel_stream resolved _cancel_session_id =
    #    agent.session_id (continuation) FIRST, then journaled the cancel
    #    terminal under the continuation ID, splitting the run's events.
    result = streaming.cancel_stream(stream_id)
    assert result is True, "cancel_stream must find the active run"

    # 6. Assert: the ORIGINAL journal now contains BOTH steer_delivered AND cancel.
    original_journal_after = run_journal.read_run_events(
        original_sid, stream_id, session_dir=tmp_path / "sessions"
    )
    original_events = [e["event"] for e in original_journal_after["events"]]
    assert "steer_delivered" in original_events, (
        "ORIGINAL journal must contain steer_delivered"
    )
    assert "cancel" in original_events, (
        "ORIGINAL journal must contain the cancel terminal — "
        "before the fix it was journaled under the continuation ID"
    )

    # 7. Assert: NO continuation journal exists for this run.
    continuation_journal_path = (
        tmp_path / "sessions" / "run_journals" / continuation_sid / f"{stream_id}.jsonl"
    )
    assert not continuation_journal_path.exists(), (
        f"NO continuation journal must be created for run {stream_id} — "
        f"before the fix the cancel terminal was split into {continuation_journal_path}"
    )

    # 8. Assert: find_run_summary() returns the COMPLETE journal — the one
    #    with the ORIGINAL session ID, containing BOTH events. Before the fix,
    #    if two journals existed, find_run_summary returned only the first one
    #    glob found (which might be the incomplete continuation journal).
    summary = run_journal.find_run_summary(stream_id, session_dir=tmp_path / "sessions")
    assert summary is not None, "find_run_summary must find the run"
    assert summary["session_id"] == original_sid, (
        f"find_run_summary must return the ORIGINAL journal ({original_sid}), "
        f"got {summary['session_id']}"
    )
    assert summary["event_count"] >= 2, (
        f"find_run_summary must return a journal with BOTH events (count>=2), "
        f"got count={summary['event_count']}"
    )
    assert summary["terminal"] is True, (
        "find_run_summary must show the run is terminal (cancel landed)"
    )
    # Cross-check by reading the full events from the summary's session_id.
    summary_events = run_journal.read_run_events(
        summary["session_id"], stream_id, session_dir=tmp_path / "sessions"
    )
    event_names = [e["event"] for e in summary_events["events"]]
    assert "steer_delivered" in event_names, (
        "journal from find_run_summary must contain steer_delivered"
    )
    assert "cancel" in event_names, (
        "journal from find_run_summary must contain cancel"
    )

    # 9. Assert: the acceptance fence was evicted (not leaked).
    fence_key = str(
        run_journal._run_path(original_sid, stream_id, session_dir=tmp_path / "sessions")
    )
    assert fence_key not in run_journal._ACCEPTANCE_FENCE, (
        "acceptance fence must be evicted after terminal — "
        "before the fix the original fence stayed closed-but-retained (leaked)"
    )


def test_cancel_after_compression_uses_continuation_id_for_session_persistence(tmp_path, monkeypatch):
    """CORE #5 companion: the continuation ID must STILL be used for
    session/scene persistence (get_session, save, anchor-scene settlement).
    The fix only redirects JOURNAL identity to the original ID — it must not
    break session cleanup targeting the continuation session.

    This test verifies that cancel_stream still resolves _cancel_session_id
    (which may be the continuation ID) and uses it for session-level cleanup,
    even though journal identity uses the original ID.
    """
    original_sid = "orig_sid_persist"
    continuation_sid = "continuation_sid_persist"
    stream_id = "stream_persist"

    # Set up the continuation session (post-compression, the session object
    # is stored under the continuation ID).
    s = Session(
        session_id=continuation_sid,
        title="Continuation session",
        messages=[{"role": "user", "content": "task"}],
    )
    s.active_stream_id = stream_id
    s.save()
    models.SESSIONS[continuation_sid] = s

    config.register_stream_owner(stream_id, original_sid)
    config.register_session_writeback_owner(original_sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()

    agent = MagicMock()
    agent.session_id = continuation_sid
    agent.interrupt = Mock(return_value=True)
    agent._drain_pending_steer = Mock(return_value=None)
    config.AGENT_INSTANCES[stream_id] = agent

    config.register_active_run(stream_id, session_id=original_sid, phase="running")

    # Patch the settlement function to capture which session_id it receives.
    captured_session_ids = []
    try:
        import api.routes as routes

        def _capturing_settlement(session_id, stream_id, **kwargs):
            captured_session_ids.append(session_id)
            return False  # no scene to persist in this test

        monkeypatch.setattr(
            routes, "_persist_terminal_anchor_scene_from_journal", _capturing_settlement
        )
    except (ImportError, AttributeError):
        pass  # settlement function may not exist in all configs

    result = streaming.cancel_stream(stream_id)
    assert result is True

    # The cancel terminal must be journaled under the ORIGINAL ID.
    original_journal = run_journal.read_run_events(
        original_sid, stream_id, session_dir=tmp_path / "sessions"
    )
    original_events = [e["event"] for e in original_journal["events"]]
    assert "cancel" in original_events, (
        "cancel terminal must be in the ORIGINAL journal"
    )

    # Session cleanup should target the continuation session (where the
    # session object lives post-compression). The cancel marker should be
    # on the continuation session, not the original.
    # Note: cancel_stream clears active_stream_id on the session it resolves
    # via _cancel_session_id (agent.session_id = continuation).
    assert s.active_stream_id is None, (
        "continuation session's active_stream_id must be cleared by cancel"
    )
