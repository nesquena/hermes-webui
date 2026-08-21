"""Regression tests for the #7188 gate-certifier rework.

Three release-blocking regressions the bot reproduced:

1. Steer accepted after runtime finished/cancelled, leaks into next turn.
2. Steer with uploaded archive always fails HTTP 400.
3. Journaled Steer disappears after settlement if tab closes before scene POST.

Each test exercises the production code path that the bot probed, not a
hand-copied harness.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import run_journal


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def isolated_steer_state():
    from api.config import (
        AGENT_INSTANCES,
        SESSION_AGENT_CACHE,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS,
        STREAMS_LOCK,
    )

    with SESSION_AGENT_CACHE_LOCK:
        cache_snapshot = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snapshot = dict(STREAMS)
        STREAMS.clear()
        agents_snapshot = dict(AGENT_INSTANCES)
        AGENT_INSTANCES.clear()
    # Reset acceptance fence between tests so each run starts fresh.
    run_journal._ACCEPTANCE_FENCE.clear()
    try:
        yield SESSION_AGENT_CACHE, STREAMS, AGENT_INSTANCES
    finally:
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()
            SESSION_AGENT_CACHE.update(cache_snapshot)
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(streams_snapshot)
            AGENT_INSTANCES.clear()
            AGENT_INSTANCES.update(agents_snapshot)
        run_journal._ACCEPTANCE_FENCE.clear()


def _handler():
    handler = MagicMock()
    handler.wfile = MagicMock()
    handler.headers = MagicMock()
    handler.headers.get = MagicMock(return_value="")
    return handler


def _response(handler):
    raw = handler.wfile.write.call_args_list[-1][0][0]
    return json.loads(raw.decode("utf-8"))


def _make_writer_factory(tmp_path):
    """Return a factory that constructs RunJournalWriter with a tmp session_dir."""
    def _factory(session_id, run_id):
        return run_journal.RunJournalWriter(
            session_id,
            run_id,
            session_dir=tmp_path,
        )
    return _factory


# ── Must-fix #1: acceptance fence rejects late Steer after runtime completion ──


def test_fence_closed_before_done_append_rejects_late_steer(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    """The gate-certifier reproduced: completion drains pending Steer at
    streaming.py:~12198 but doesn't append 'done' until :~12281. A late Steer
    in that window is accepted and leaks into the next turn.

    Fix: close the acceptance fence BEFORE the drain. A late Steer must be
    rejected with fence_closed (surfaced as stream_dead).
    """
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams, agents = isolated_steer_state
    sid = "fence_completion_sid"
    stream_id = "fence_completion_run"
    stream = create_stream_channel()

    agent = MagicMock()
    agent.steer.return_value = True
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
        agents[stream_id] = agent

    monkeypatch.setattr(streaming, "RunJournalWriter", _make_writer_factory(tmp_path))

    # Simulate the completion path: close the fence (as the drain does), then
    # attempt a late Steer.
    writer = run_journal.RunJournalWriter(sid, stream_id, session_dir=tmp_path)
    writer.close_acceptance_fence()

    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "too late"})

    # The Steer must be rejected — the runtime is effectively over.
    agent.steer.assert_not_called()
    body = _response(handler)
    assert body["accepted"] is False
    assert body["fallback"] == "stream_dead"
    assert body["stream_id"] == stream_id

    # No steer_delivered event in the journal.
    journal = run_journal.read_run_events(sid, stream_id, session_dir=tmp_path)
    assert not any(e["event"] == "steer_delivered" for e in journal["events"])


def test_fence_closed_on_cancel_rejects_late_steer(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    """Eager cancellation closes the fence before the eager pop. A late Steer
    arriving after cancel must be rejected, not accepted as delivered.
    """
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams, agents = isolated_steer_state
    sid = "fence_cancel_sid"
    stream_id = "fence_cancel_run"
    stream = create_stream_channel()

    agent = MagicMock()
    agent.steer.return_value = True
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
        agents[stream_id] = agent

    monkeypatch.setattr(streaming, "RunJournalWriter", _make_writer_factory(tmp_path))

    # Simulate cancel_stream closing the fence.
    writer = run_journal.RunJournalWriter(sid, stream_id, session_dir=tmp_path)
    writer.close_acceptance_fence()

    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "post-cancel"})

    agent.steer.assert_not_called()
    body = _response(handler)
    assert body["accepted"] is False
    assert body["fallback"] == "stream_dead"


def test_terminal_event_via_put_closes_fence(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    """Terminal events (done/cancel/apperror) routed through put() must close
    the acceptance fence atomically with their journal append.
    """
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams, agents = isolated_steer_state
    sid = "fence_put_terminal_sid"
    stream_id = "fence_put_terminal_run"
    stream = create_stream_channel()

    agent = MagicMock()
    agent.steer.return_value = True
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
        agents[stream_id] = agent

    monkeypatch.setattr(streaming, "RunJournalWriter", _make_writer_factory(tmp_path))

    # Build a real RunJournalWriter and simulate the put() path for 'done'.
    writer = run_journal.RunJournalWriter(sid, stream_id, session_dir=tmp_path)
    published_events = []

    def _publish(journaled):
        published_events.append(journaled)

    writer.close_acceptance_fence_and_publish_terminal(
        "done", {"session": {}}, _publish
    )

    # The fence must be closed.
    assert run_journal.is_acceptance_fence_closed(sid, stream_id, session_dir=tmp_path)

    # The done event must be journaled and published.
    journal = run_journal.read_run_events(sid, stream_id, session_dir=tmp_path)
    assert any(e["event"] == "done" and e.get("terminal") for e in journal["events"])
    assert len(published_events) == 1
    assert published_events[0]["event"] == "done"

    # A late Steer after the terminal event must be rejected.
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "late"})

    agent.steer.assert_not_called()
    assert _response(handler)["accepted"] is False


def test_fence_does_not_block_steer_before_terminal(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    """Before the fence is closed, Steer must be accepted normally. The fence
    only blocks Steer after the run's lifecycle owner has closed it.
    """
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams, agents = isolated_steer_state
    sid = "fence_open_sid"
    stream_id = "fence_open_run"
    stream = create_stream_channel()

    agent = MagicMock()
    agent.steer.return_value = True
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
        agents[stream_id] = agent

    monkeypatch.setattr(streaming, "RunJournalWriter", _make_writer_factory(tmp_path))

    # Fence is NOT closed — Steer should be accepted.
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "go ahead"})

    agent.steer.assert_called_once_with("go ahead")
    body = _response(handler)
    assert body["accepted"] is True


# ── Must-fix #2: archive-backed Steer no longer 400s ─────────────────────────


def test_archive_backed_ster_directory_is_accepted(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    """The gate-certifier reproduced: /api/upload/extract returns the extracted
    directory, but _verified_steer_attachment_paths required is_file(), so
    archive-backed Steer always 400'd.

    Fix: permit a contained extracted directory by expanding to member files.
    """
    from api import streaming
    from api.upload import _session_attachment_dir

    sid = "archive_steer_sid"
    stream_id = "archive_steer_run"

    # Create a fake session upload inbox with an extracted archive directory.
    session_root = _session_attachment_dir(sid).resolve()
    session_root.mkdir(parents=True, exist_ok=True)
    archive_dir = session_root / "my_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "doc1.txt").write_text("content 1")
    (archive_dir / "doc2.txt").write_text("content 2")
    subdir = archive_dir / "sub"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("nested")

    # The directory path (as returned by /api/upload/extract) must be accepted.
    verified = streaming._verified_steer_attachment_paths(sid, [str(archive_dir)])
    assert len(verified) == 3
    assert all(Path(p).is_file() for p in verified)
    assert all(Path(p).is_relative_to(session_root) for p in verified)


def test_archive_backed_ster_rejects_inbox_root(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    """The inbox root itself must not be accepted as a Steer attachment."""
    from api import streaming
    from api.upload import _session_attachment_dir

    sid = "archive_steer_root_sid"
    session_root = _session_attachment_dir(sid).resolve()
    session_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="not a session upload"):
        streaming._verified_steer_attachment_paths(sid, [str(session_root)])


def test_archive_backed_ster_rejects_outside_session(
    isolated_steer_state,
    tmp_path,
):
    """Paths outside the session root must still be rejected."""
    from api import streaming

    sid = "archive_steer_outside_sid"
    with pytest.raises(ValueError, match="not a session upload"):
        streaming._verified_steer_attachment_paths(sid, [str(tmp_path / "evil.txt")])


def test_regular_file_ster_still_accepted(
    isolated_steer_state,
    tmp_path,
):
    """A regular uploaded file (non-archive) must still pass validation."""
    from api import streaming
    from api.upload import _session_attachment_dir

    sid = "archive_steer_file_sid"
    session_root = _session_attachment_dir(sid).resolve()
    session_root.mkdir(parents=True, exist_ok=True)
    doc = session_root / "report.pdf"
    doc.write_text("pdf content")

    verified = streaming._verified_steer_attachment_paths(sid, [str(doc)])
    assert verified == [str(doc.resolve())]


# ── Must-fix #3: server-side terminal settlement durability ──────────────────


def test_terminal_settlement_persists_anchor_scene_without_browser(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    """The gate-certifier reproduced: a terminal journal containing
    steer_delivered had the row in journal projection, but settled hydration
    returned no Anchor scene — because durability depended on the browser's
    async scene POST, and the tab closed before it landed.

    Fix: _persist_terminal_anchor_scene_from_journal materializes the canonical
    journal scene into anchor_activity_scenes server-side, with no browser.
    """
    from api import routes
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel
    from api.models import Session

    cache, streams, agents = isolated_steer_state
    sid = "settlement_sid"
    stream_id = "settlement_run"

    # Build a real journal with a steer_delivered event and a done event.
    run_journal.append_run_event(
        sid, stream_id, "steer_delivered",
        {"text": "steer text", "status": "delivered", "created_at": time.time()},
        session_dir=tmp_path,
    )
    run_journal.append_run_event(
        sid, stream_id, "done", {"session": {}}, session_dir=tmp_path,
    )

    # Build a real session with an assistant message that the scene can attach to.
    session_path = tmp_path / "sessions" / f"{sid}.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)

    # We need a mock session with the right interface for both
    # _persist_terminal_anchor_scene_from_journal and _run_journal_live_snapshot.
    # _run_journal_live_snapshot calls find_run_summary, which needs the journal
    # summary cache. Let's mock at the right level.
    mock_session = MagicMock()
    mock_session.session_id = sid
    mock_session.anchor_activity_scenes = {}
    assistant_msg = {
        "role": "assistant",
        "content": "final answer",
        "timestamp": int(time.time()),
    }
    mock_session.messages = [assistant_msg]
    mock_session.save = MagicMock(return_value=True)

    # Mock get_session to return our session.
    with patch.object(routes, "get_session", return_value=mock_session), \
         patch.object(routes, "find_run_summary", return_value={
             "session_id": sid,
             "run_id": stream_id,
             "last_seq": 2,
             "last_event_id": f"{stream_id}:2",
         }), \
         patch.object(routes, "read_run_events", return_value={
             "session_id": sid,
             "run_id": stream_id,
             "events": run_journal.read_run_events(sid, stream_id, session_dir=tmp_path)["events"],
             "malformed": False,
         }), \
         patch.object(routes, "_get_session_agent_lock") as _lock_ctx:
        _lock_ctx.return_value.__enter__ = MagicMock()
        _lock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = routes._persist_terminal_anchor_scene_from_journal(
            sid, stream_id, terminal_state="completed",
        )

    assert result is True
    mock_session.save.assert_called_once()
    # The scene must be in anchor_activity_scenes.
    records = mock_session.anchor_activity_scenes
    assert len(records) == 1
    record = list(records.values())[0]
    assert record["stream_id"] == stream_id
    scene = record["scene"]
    assert scene["terminal_state"] == "completed"
    # The scene must have activity_rows (including the steer_delivered row).
    assert isinstance(scene.get("activity_rows"), list)
    assert len(scene["activity_rows"]) > 0
    # The steer_delivered row must be present.
    steer_rows = [
        r for r in scene["activity_rows"]
        if r.get("source_event_type") == "steer_delivered"
    ]
    assert len(steer_rows) == 1
    assert steer_rows[0]["text"] == "steer text"


def test_terminal_settlement_no_scene_for_empty_journal(
    isolated_steer_state,
    tmp_path,
):
    """If the journal has no events (or no activity rows), settlement must
    return False without persisting an empty scene."""
    from api import routes

    sid = "settlement_empty_sid"
    stream_id = "settlement_empty_run"

    # No journal events written.
    with patch.object(routes, "find_run_summary", return_value=None):
        result = routes._persist_terminal_anchor_scene_from_journal(
            sid, stream_id, terminal_state="completed",
        )
    assert result is False
