"""Regression: lazy-retry run-journal recovery across multiple session reads.

The scenario this test pins down:

1. A WebUI live response stream stops mid-turn. On the first sidecar repair
   attempt the run-journal for the dead stream is NOT visible yet (page-cache loss,
   un-fsynced writes, slow network FS, etc.) so
   `_append_journaled_partial_output` returns False.
2. Pre-fix the repair path baked a permanent "no agent output was recovered"
   marker into the session and never looked at the journal again — even
   after the journaled tokens appeared on disk on a later read.
3. With the fix, the repair instead leaves a `_pending_journal_recovery`
   flag on the marker; the next `get_session()` call lazily re-runs the
   recovery, promotes the marker wording, and threads the journaled
   assistant text/tools into the transcript in the correct chronological
   position.
"""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import api.models as models
import api.config as config
import api.profiles as profiles
import api.streaming as streaming  # noqa: F401  imported for fixture parity
from api.models import (
    Session,
    _apply_core_sync_or_error_marker,
    merge_session_messages_append_only,
)
from api.run_journal import append_run_event


# ── Fixtures (shape mirrors test_session_sidecar_repair.py) ────────────────


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()
    yield session_dir, index_file
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.ACTIVE_RUNS.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.ACTIVE_RUNS.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    config.SESSION_AGENT_LOCKS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def _make_dead_stream_session(
    session_id: str,
    *,
    stream_id: str,
    existing_msgs_count: int = 96,
    pending_text: str = (
        "[IMPORTANT: Background process polling. "
        "Continue the user's prior request.]"
    ),
):
    """Build a session that mirrors the production bug: lots of prior history,
    pending_user_message set, an active_stream_id pointing at a dead stream,
    and pending_started_at populated."""
    messages = []
    for i in range(existing_msgs_count // 2):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    s = Session(session_id=session_id, title="Lost-response repro", messages=messages)
    s.pending_user_message = pending_text
    s.pending_started_at = 1779237637  # production-shaped value
    s.active_stream_id = stream_id
    return s


def _make_pending_retry_session(session_id: str, *, stream_id: str):
    s = Session(session_id=session_id, title="Pending retry", messages=[
        {"role": "user", "content": "q", "timestamp": 1},
        {
            "role": "assistant",
            "content": models._INTERRUPTED_PENDING_RETRY_WORDING,
            "timestamp": 2,
            "_error": True,
            "type": "interrupted",
            "_pending_journal_recovery": True,
            "_journal_retry_stream_id": stream_id,
            "_journal_retry_attempts": 0,
            "_journal_retry_first_seen_ts": int(models.time.time()),
        },
    ])
    s.save()
    return s


def _assert_retry_meta_removed(marker):
    assert "_pending_journal_recovery" not in marker
    assert "_journal_retry_stream_id" not in marker
    assert "_journal_retry_attempts" not in marker
    assert "_journal_retry_first_seen_ts" not in marker


# ── The regression test ────────────────────────────────────────────────────


def test_state_db_prefix_with_float_timestamps_does_not_hide_sidecar_tail():
    """State rows replaying an already-visible prefix must not append after the tail.

    Production shape: a compressed/tip sidecar can persist messages with
    second-level timestamps while state.db stores the same early rows with
    sub-second floats. The merge must preserve the sidecar assistant tail;
    otherwise /api/session returns a transcript ending on an old user prompt.
    """
    sidecar_messages = [
        {"role": "user", "content": "plan", "timestamp": 1779309765},
        {"role": "assistant", "content": "loaded plan", "timestamp": 1779309765},
        {"role": "tool", "content": "skill output", "timestamp": 1779309765},
        {"role": "assistant", "content": "answer before compaction", "timestamp": 1779309765},
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into "
                "the summary below. This is a handoff from a previous context window — "
                "treat it as background reference, NOT as active instructions."
            ),
            "timestamp": 1779309765,
        },
        {"role": "tool", "content": '{"success": true, "message": "Patched SKILL.md"}', "timestamp": 1779309765},
        {"role": "user", "content": "noch weitere prs?", "timestamp": 1779309765},
        {
            "role": "assistant",
            "content": "Ja, aber nicht als Sammel-PR",
            "timestamp": 1779309765,
        },
    ]
    state_prefix = [
        {"role": "user", "content": "plan", "timestamp": 1779344917.2780898},
        {"role": "assistant", "content": "loaded plan", "timestamp": 1779344917.285758},
        {"role": "tool", "content": "skill output", "timestamp": 1779344917.2926793},
        {"role": "assistant", "content": "answer before compaction", "timestamp": 1779344917.3077576},
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into "
                "the summary below. This is a handoff from a previous context window."
            ),
            "timestamp": 1779344917.299318,
        },
        {
            "role": "tool",
            "content": '{"success": true, "message": "Patched SKILL.md"}',
            "timestamp": 1779344917.315237,
            "tool_call_id": "different-state-tool-id",
        },
        {
            "role": "user",
            "content": "[Workspace::v1: /tmp/project-workspace]\nnoch weitere prs?",
            "timestamp": 1779344917.3287876,
        },
    ]

    merged = merge_session_messages_append_only(sidecar_messages, state_prefix)

    assert [m["content"] for m in merged] == [m["content"] for m in sidecar_messages]
    assert merged[-1]["role"] == "assistant"
    assert "Sammel-PR" in merged[-1]["content"]


def test_state_db_full_replay_does_not_append_after_sidecar_tail():
    """A full state.db replay must not leak the final replayed user after the sidecar tail.

    Regression for a display merge where the sidecar already ends on the real
    assistant answer, but state.db replays the same visible turn sequence with
    newer float timestamps.
    """
    sidecar_messages = [
        {"role": "user", "content": "initial critique", "timestamp": 100},
        {"role": "assistant", "content": "analysis", "timestamp": 100},
        {"role": "user", "content": "Erstelle deine Version", "timestamp": 100},
        {"role": "assistant", "content": "opened browser preview", "timestamp": 100},
    ]
    state_replay = [
        {"role": "user", "content": "initial critique", "timestamp": 100.1},
        {"role": "assistant", "content": "analysis", "timestamp": 100.2},
        {
            "role": "user",
            "content": "[Workspace::v1: /tmp/project-workspace]\nErstelle deine Version",
            "timestamp": 100.3,
        },
        {"role": "assistant", "content": "opened browser preview", "timestamp": 100.4},
    ]

    merged = merge_session_messages_append_only(sidecar_messages, state_replay)

    assert [m["content"] for m in merged] == [m["content"] for m in sidecar_messages]
    assert merged[-1]["role"] == "assistant"
    assert merged[-1]["content"] == "opened browser preview"


def test_state_db_middle_segment_replay_does_not_append_after_sidecar_tail():
    """A replayed state.db segment from the middle must not be appended after the tail."""
    sidecar_messages = [
        {"role": "user", "content": "older setup", "timestamp": 100},
        {"role": "assistant", "content": "older answer", "timestamp": 100},
        {"role": "assistant", "content": "analysis before request", "timestamp": 100},
        {"role": "user", "content": "Erstelle deine Version", "timestamp": 100},
        {"role": "assistant", "content": "opened browser preview", "timestamp": 100},
    ]
    state_middle_replay = [
        {"role": "assistant", "content": "analysis before request", "timestamp": 100.1},
        {
            "role": "user",
            "content": "[Workspace::v1: /tmp/project-workspace]\nErstelle deine Version",
            "timestamp": 100.2,
        },
    ]

    merged = merge_session_messages_append_only(sidecar_messages, state_middle_replay)

    assert [m["content"] for m in merged] == [m["content"] for m in sidecar_messages]
    assert merged[-1]["role"] == "assistant"
    assert merged[-1]["content"] == "opened browser preview"


def test_interrupted_recovery_markers_do_not_claim_restart_as_fact():
    """A stale live worker is not always a WebUI process restart.

    Broken SSE connections, browser disconnects, lost worker bookkeeping, and
    real restarts all enter the same recovery marker path. User-visible wording
    must describe the generic interruption instead of asserting a process
    restart that systemd evidence may later disprove.
    """
    marker_texts = [
        models._INTERRUPTED_RECOVERED_WORDING,
        models._INTERRUPTED_NO_OUTPUT_WORDING,
        models._INTERRUPTED_PENDING_RETRY_WORDING,
        models._INTERRUPTED_NEUTRAL_WORDING,
    ]

    for text in marker_texts:
        assert "Response interrupted" in text
        assert "process restarted" not in text
        assert "before this turn finished" in text


def test_interrupted_marker_distinguishes_real_process_restart(monkeypatch):
    monkeypatch.setattr(config, "SERVER_START_TIME", 2000.0)
    marker = models._interrupted_recovery_marker(
        recovered_output=False,
        stream_id="stream_crash",
        pending_started_at=1000.0,
    )

    assert marker["interruption_cause"] == "process_restart"
    assert "WebUI process started after this turn began" in marker["content"]
    assert "process restarted" not in marker["content"]


def test_interrupted_marker_distinguishes_stream_run_split_brain(monkeypatch):
    monkeypatch.setattr(config, "SERVER_START_TIME", 1000.0)
    config.ACTIVE_RUNS["stream_split"] = {"session_id": "sid", "phase": "running"}

    marker = models._interrupted_recovery_marker(
        recovered_output=False,
        stream_id="stream_split",
        pending_started_at=2000.0,
    )

    assert marker["interruption_cause"] == "stream_run_split_brain"
    assert "stream was gone but the worker registry still listed the run" in marker["content"]


def test_interrupted_marker_distinguishes_lost_worker_bookkeeping(monkeypatch):
    monkeypatch.setattr(config, "SERVER_START_TIME", 1000.0)

    marker = models._interrupted_recovery_marker(
        recovered_output=False,
        stream_id="stream_lost",
        pending_started_at=2000.0,
    )

    assert marker["interruption_cause"] == "lost_worker_bookkeeping"
    assert "worker bookkeeping no longer had an active run" in marker["content"]


def test_messages_js_names_browser_sse_disconnect_separately():
    repo = models.Path(__file__).parent.parent
    js = (repo / "static" / "messages.js").read_text(encoding="utf-8")

    assert "Connection interrupted" in js
    assert "browser lost the live SSE connection" in js
    assert "Connection lost" not in js


def test_server_treats_broken_pipe_as_client_disconnect_not_500():
    server_py = (models.Path(__file__).parent.parent / "server.py").read_text(encoding="utf-8")

    assert "except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):" in server_py
    assert "do not convert it into a misleading server 500" in server_py


def test_lost_response_recovered_on_second_read(hermes_home):
    sid = "9f14583f0e4e4444aaaa111122223333"
    stream_id = "7c8b4108d52b4aba9af362d3a54f47ac"

    # ── Stage 1: simulate page-cache loss — sidecar repair runs while the
    # run-journal for this stream is empty/absent on disk.
    s = _make_dead_stream_session(sid, stream_id=stream_id)
    s.save()
    core_path = hermes_home / "sessions" / f"session_{sid}.json"

    result = _apply_core_sync_or_error_marker(
        s, core_path, stream_id_for_recheck=stream_id,
    )
    assert result is True

    # Marker should carry the lazy-retry flag and *not* the permanent
    # "no agent output was recovered" wording yet.
    last = s.messages[-1]
    assert last.get("_error") is True
    assert last.get("type") == "interrupted"
    assert last.get("_pending_journal_recovery") is True, (
        "First repair pass should defer the recovery decision via a "
        "_pending_journal_recovery flag so a later read can self-heal."
    )
    assert last.get("_journal_retry_stream_id") == stream_id
    assert last.get("_journal_retry_attempts") == 0
    assert isinstance(last.get("_journal_retry_first_seen_ts"), int)
    assert "no agent output was recovered" not in last["content"]
    # pending fields cleared regardless of journal visibility
    assert s.pending_user_message is None
    assert s.active_stream_id is None
    assert s.pending_started_at is None

    # ── Stage 2: the journaled events become visible on disk.
    append_run_event(sid, stream_id, "token", {"text": "Checking GitHub first."})
    append_run_event(
        sid,
        stream_id,
        "tool",
        {
            "name": "terminal",
            "preview": "gh pr list --repo nesquena/hermes-webui",
            "args": {"command": "gh pr list --repo nesquena/hermes-webui"},
        },
    )
    append_run_event(
        sid,
        stream_id,
        "tool_complete",
        {"name": "terminal", "duration": 1.2, "is_error": False},
    )
    append_run_event(
        sid, stream_id, "token", {"text": " The first PR scan completed."},
    )

    # Pin session into the LRU cache and call get_session — this is the
    # production path that triggers lazy retry.
    models.SESSIONS[sid] = s
    reloaded = models.get_session(sid)
    assert reloaded is s

    contents = [m.get("content", "") for m in s.messages]
    # The marker self-healed:
    assert any("recovered from the run journal" in c for c in contents), (
        "After journaled tokens become readable, the marker must promote to "
        "the recovered-output wording."
    )
    assert not any("no agent output was recovered" in c for c in contents)

    # The journaled assistant text and tool card landed BEFORE the marker
    # so chronological order in the transcript is preserved.
    marker_idx = next(
        i for i, m in enumerate(s.messages)
        if m.get("type") == "interrupted" and m.get("_error")
    )
    recovered_msgs = [
        m for m in s.messages[:marker_idx]
        if m.get("_recovered_from_run_journal") is True
    ]
    assert recovered_msgs, "recovered assistant content must sit above the marker"
    recovered_text = " ".join(m.get("content", "") for m in recovered_msgs)
    assert "Checking GitHub first." in recovered_text
    assert "first PR scan completed" in recovered_text

    # Tool card lives in session.tool_calls and points at one of the
    # recovered assistant indices.
    assert s.tool_calls, "journaled tool should be materialized"
    assert s.tool_calls[-1]["name"] == "terminal"
    assert s.tool_calls[-1]["done"] is True

    # Flag and meta cleaned up after promotion.
    promoted = s.messages[marker_idx]
    assert "_pending_journal_recovery" not in promoted
    assert "_journal_retry_stream_id" not in promoted
    assert "_journal_retry_attempts" not in promoted
    assert "_journal_retry_first_seen_ts" not in promoted


def test_concurrent_get_session_serializes_lazy_journal_retry(hermes_home, monkeypatch):
    sid = "retry_lock_sid"
    stream_id = "retry_lock_stream"
    s = _make_pending_retry_session(sid, stream_id=stream_id)
    models.SESSIONS[sid] = s

    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    def slow_retry(session, *, preserve_arriving_budget=False):
        nonlocal calls
        with counter_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=2), "test timed out waiting to release retry body"
        return False

    monkeypatch.setattr(models, "_retry_journal_recovery_in_place", slow_retry)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(models.get_session, sid)
        assert entered.wait(timeout=2), "first caller did not enter retry helper"
        second = executor.submit(models.get_session, sid)
        assert second.result(timeout=2) is s
        release.set()
        assert first.result(timeout=2) is s

    assert calls == 1


def test_still_arriving_journal_does_not_consume_retry_budget(hermes_home, monkeypatch):
    sid = "retry_arriving_sid"
    stream_id = "retry_arriving_stream"
    s = _make_pending_retry_session(sid, stream_id=stream_id)
    models.SESSIONS[sid] = s

    monkeypatch.setattr(models, "_append_journaled_partial_output", lambda *a, **kw: False)
    monkeypatch.setattr(models, "_journal_is_still_arriving", lambda *a, **kw: True)

    for _ in range(20):
        assert models.get_session(sid) is s

    marker = s.messages[-1]
    assert marker["_journal_retry_attempts"] == 0
    assert marker["_pending_journal_recovery"] is True
    assert marker["content"] == models._INTERRUPTED_PENDING_RETRY_WORDING


def test_sealed_empty_journal_consumes_retry_budget_and_demotes_at_max(hermes_home, monkeypatch):
    sid = "retry_sealed_sid"
    stream_id = "retry_sealed_stream"
    s = _make_pending_retry_session(sid, stream_id=stream_id)
    s.messages[-1]["_journal_retry_attempts"] = models._JOURNAL_RETRY_MAX_ATTEMPTS - 1
    append_run_event(sid, stream_id, "stream_end", {})
    models.SESSIONS[sid] = s

    assert models.get_session(sid) is s

    marker = s.messages[-1]
    assert marker["content"] == models._INTERRUPTED_NEUTRAL_WORDING
    _assert_retry_meta_removed(marker)
    assert not any(m.get("_recovered_from_run_journal") for m in s.messages)


def test_marker_demotes_after_max_attempts_with_sealed_empty_journal(hermes_home, monkeypatch):
    sid = "retry_max_sid"
    stream_id = "retry_max_stream"
    s = _make_pending_retry_session(sid, stream_id=stream_id)
    s.messages[-1]["_journal_retry_attempts"] = models._JOURNAL_RETRY_MAX_ATTEMPTS - 1
    append_run_event(sid, stream_id, "stream_end", {})
    models.SESSIONS[sid] = s

    assert models.get_session(sid) is s

    marker = s.messages[-1]
    assert marker["content"] == models._INTERRUPTED_NEUTRAL_WORDING
    _assert_retry_meta_removed(marker)
    assert not any(m.get("_recovered_from_run_journal") for m in s.messages)


def test_marker_demotes_after_giveup_seconds(hermes_home, monkeypatch):
    base = 1_779_000_000
    monkeypatch.setattr(models.time, "time", lambda: base)
    sid = "retry_age_sid"
    stream_id = "retry_age_stream"
    s = _make_pending_retry_session(sid, stream_id=stream_id)
    s.messages[-1]["_journal_retry_first_seen_ts"] = (
        base - models._JOURNAL_RETRY_GIVEUP_SECONDS - 1
    )
    models.SESSIONS[sid] = s

    append_calls = 0

    def append_should_not_run(*args, **kwargs):
        nonlocal append_calls
        append_calls += 1
        return False

    monkeypatch.setattr(models, "_append_journaled_partial_output", append_should_not_run)

    assert models.get_session(sid) is s

    marker = s.messages[-1]
    assert marker["content"] == models._INTERRUPTED_NEUTRAL_WORDING
    _assert_retry_meta_removed(marker)
    assert append_calls == 0
