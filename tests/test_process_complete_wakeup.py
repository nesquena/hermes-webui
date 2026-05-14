"""Tests for the process_complete SSE event (terminal notify_on_complete wakeup).

Companion to RFC `rfc-notify-on-complete-wakeup.md` and draft PR
`draft-pr-process-complete-event.md`. Verifies the seven file changes wire
the bytes correctly:

  1. api/config.py exports PENDING_PROCESS_COMPLETIONS, PROCESS_SESSION_INDEX,
     PROCESS_SESSION_INDEX_LOCK.
  2. api/background_process.py exposes start_drain_thread / register_process_session
     and formats wakeup_prompt deterministically.
  3. api/streaming.py exports HERMES_SESSION_PLATFORM=webui + HERMES_SESSION_CHAT_ID
     into the agent's process env (Break B fix from the spike).
  4. api/routes.py atomically discards the PENDING_PROCESS_COMPLETIONS marker.
  5. static/messages.js subscribes to 'process_complete' and re-POSTs
     wakeup_prompt to /api/chat/start.
  6. server.py starts the drain thread at startup.
  7. CHANGELOG.md entry.

These are structural (source-grep) checks plus pure-function tests; full
end-to-end behavior requires a live WebUI + agent and is exercised via the
manual trial described in the draft PR §3.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Test 1: api.config exports
# ---------------------------------------------------------------------------

def test_config_exports_process_complete_state():
    """api.config must export the state shared with the drain thread."""
    from api.config import (
        PENDING_PROCESS_COMPLETIONS,
        PROCESS_SESSION_INDEX,
        PROCESS_SESSION_INDEX_LOCK,
        PROCESS_COMPLETE_EVENTS_SEEN,
    )

    assert isinstance(PENDING_PROCESS_COMPLETIONS, set)
    assert isinstance(PROCESS_SESSION_INDEX, dict)
    assert hasattr(PROCESS_SESSION_INDEX_LOCK, "__enter__")  # threading.Lock
    assert isinstance(PROCESS_COMPLETE_EVENTS_SEEN, dict)


# ---------------------------------------------------------------------------
# Test 2: background_process module — public API + format_wakeup_prompt
# ---------------------------------------------------------------------------

def test_background_process_module_api():
    """api.background_process must expose the drain control + register API."""
    from api import background_process

    for name in (
        "start_drain_thread",
        "stop_drain_thread",
        "register_process_session",
        "unregister_process_session",
        "format_wakeup_prompt",
    ):
        assert callable(getattr(background_process, name)), f"missing: {name}"


def test_format_wakeup_prompt_completion_shape():
    """Completion events produce [IMPORTANT: ...] payload mirroring CLI."""
    from api.background_process import format_wakeup_prompt

    msg = format_wakeup_prompt({
        "type": "completion",
        "session_id": "proc-abc",
        "command": "sleep 5 && echo done",
        "exit_code": 0,
        "output": "done\n",
    })
    assert msg.startswith("[IMPORTANT:")
    assert msg.endswith("]")
    assert "proc-abc" in msg
    assert "exit_code=0" in msg
    assert "sleep 5 && echo done" in msg
    assert "done" in msg


def test_format_wakeup_prompt_watch_match_shape():
    from api.background_process import format_wakeup_prompt

    msg = format_wakeup_prompt({
        "type": "watch_match",
        "session_id": "proc-xyz",
        "command": "npm run dev",
        "pattern": "compiled successfully",
        "output": "webpack compiled successfully",
        "suppressed": 0,
    })
    assert "watch pattern" in msg
    assert "compiled successfully" in msg


def test_format_wakeup_prompt_truncates_large_output():
    from api.background_process import format_wakeup_prompt

    big = "x" * 20_000
    msg = format_wakeup_prompt({
        "type": "completion",
        "session_id": "p",
        "command": "c",
        "exit_code": 0,
        "output": big,
    })
    # Truncation kicks in well below 20k chars
    assert len(msg) < 6000
    assert "truncated" in msg.lower() or "…" in msg


def test_register_process_session_idempotent_and_unregister():
    """register_process_session binds + unregister clears."""
    from api import background_process, config as cfg

    background_process.register_process_session("sess-key-1", "session-1")
    with cfg.PROCESS_SESSION_INDEX_LOCK:
        assert cfg.PROCESS_SESSION_INDEX.get("sess-key-1") == "session-1"
    background_process.unregister_process_session("sess-key-1")
    with cfg.PROCESS_SESSION_INDEX_LOCK:
        assert "sess-key-1" not in cfg.PROCESS_SESSION_INDEX


# ---------------------------------------------------------------------------
# Test 3: streaming.py exports HERMES_SESSION_PLATFORM into agent env (Break B)
# ---------------------------------------------------------------------------

def test_streaming_source_exports_session_platform_and_chat_id():
    """streaming.py must set HERMES_SESSION_PLATFORM=webui + HERMES_SESSION_CHAT_ID
    into both the thread env and the process env so terminal_tool's watcher
    routing gate (terminal_tool.py:~1940) passes for WebUI sessions."""
    src = (REPO_ROOT / "api" / "streaming.py").read_text()
    assert "HERMES_SESSION_PLATFORM" in src, (
        "streaming.py must export HERMES_SESSION_PLATFORM into the agent env"
    )
    assert "'webui'" in src and "HERMES_SESSION_PLATFORM" in src
    assert "HERMES_SESSION_CHAT_ID" in src, (
        "streaming.py must export HERMES_SESSION_CHAT_ID into the agent env"
    )


def test_streaming_registers_process_session_with_drain():
    src = (REPO_ROOT / "api" / "streaming.py").read_text()
    assert "register_process_session" in src, (
        "streaming.py must call register_process_session at chat start to bind "
        "HERMES_SESSION_KEY to the WebUI session_id for the drain thread."
    )


# ---------------------------------------------------------------------------
# Test 4: routes.py atomically consumes PENDING_PROCESS_COMPLETIONS
# ---------------------------------------------------------------------------

def test_routes_consumes_pending_process_completions_marker():
    """routes.py must discard the PENDING_PROCESS_COMPLETIONS marker for the
    session in _start_chat_stream_for_session — atomic on read, mirrors the
    PENDING_GOAL_CONTINUATION pattern (#1932)."""
    src = (REPO_ROOT / "api" / "routes.py").read_text()
    assert "PENDING_PROCESS_COMPLETIONS" in src
    # Discard must be present (atomic consume on stream start)
    assert "PENDING_PROCESS_COMPLETIONS.discard" in src, (
        "routes.py must call PENDING_PROCESS_COMPLETIONS.discard(session_id) "
        "atomically when starting a new chat stream"
    )


def test_routes_exposes_process_complete_ack_endpoint():
    src = (REPO_ROOT / "api" / "routes.py").read_text()
    assert '/api/process-complete-ack' in src, (
        "routes.py must expose POST /api/process-complete-ack for the frontend"
    )
    assert "_handle_process_complete_ack" in src


# ---------------------------------------------------------------------------
# Test 5: static/messages.js subscribes to process_complete and re-POSTs
# ---------------------------------------------------------------------------

def test_frontend_subscribes_to_process_complete_event():
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    assert "addEventListener('process_complete'" in js, (
        "static/messages.js must subscribe to the 'process_complete' SSE event"
    )
    # Must re-POST wakeup_prompt to /api/chat/start for the actual agent wakeup
    assert "wakeup_prompt" in js
    assert "api/chat/start" in js


def test_frontend_dedupes_by_process_id():
    """The handler must dedupe by (session_id, process_id) to survive
    SSE-reconnect buffered replays."""
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    assert "_seenProcessCompleteIds" in js
    assert "dedupeKey" in js


# ---------------------------------------------------------------------------
# Test 6: server.py starts the drain thread
# ---------------------------------------------------------------------------

def test_server_starts_drain_thread_on_startup():
    src = (REPO_ROOT / "server.py").read_text()
    assert "start_drain_thread" in src, (
        "server.py must start the process_complete drain thread at startup"
    )
    assert "stop_drain_thread" in src, (
        "server.py must stop the drain thread on shutdown"
    )


# ---------------------------------------------------------------------------
# Test 7: CHANGELOG entry
# ---------------------------------------------------------------------------

def test_changelog_has_process_complete_entry():
    src = (REPO_ROOT / "CHANGELOG.md").read_text()
    assert "process_complete" in src or "notify_on_complete" in src.lower(), (
        "CHANGELOG.md must mention the new process_complete event / "
        "notify_on_complete wakeup fix"
    )


# ---------------------------------------------------------------------------
# Test 8: Drain thread end-to-end — synthesize a queue event, observe routing
# ---------------------------------------------------------------------------

def test_drain_routes_completion_event_to_pending_set(monkeypatch):
    """A completion_queue event for a registered session_key must land in
    PENDING_PROCESS_COMPLETIONS and emit on the SSE channel."""
    from api import background_process, config as cfg

    # Set up a fake stream channel that records emitted events
    emitted: list = []

    class _FakeChannel:
        def put_nowait(self, item):
            emitted.append(item)

    sid = "session-drain-1"
    stream_id = "stream-drain-1"
    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = _FakeChannel()
    cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}

    # Register the process→session mapping
    background_process.register_process_session(sid, sid)

    # Synthesize a completion event and run the routing function directly
    evt = {
        "type": "completion",
        "session_id": "proc-42",
        "session_key": sid,
        "command": "sleep 1",
        "exit_code": 0,
        "output": "ok\n",
    }
    try:
        background_process._process_one(evt)
        # Marker set
        assert sid in cfg.PENDING_PROCESS_COMPLETIONS
        # SSE event emitted
        assert emitted, "drain must emit at least one SSE event"
        ev_name, data = emitted[0]
        assert ev_name == "process_complete"
        assert data["session_id"] == sid
        assert data["process_id"] == "proc-42"
        assert "wakeup_prompt" in data
        assert data["exit_code"] == 0

        # Idempotency: a second identical event must not double-emit
        emitted.clear()
        background_process._process_one(evt)
        assert not emitted, "duplicate process_id must be deduped"
    finally:
        cfg.PENDING_PROCESS_COMPLETIONS.discard(sid)
        cfg.PROCESS_COMPLETE_EVENTS_SEEN.pop(sid, None)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.pop(stream_id, None)
        cfg.ACTIVE_RUNS.pop(stream_id, None)
        background_process.unregister_process_session(sid)


def test_drain_ignores_unmapped_session_keys():
    """Events whose session_key isn't in PROCESS_SESSION_INDEX (e.g. cron
    processes that share the same registry) must be silently dropped — they
    don't belong to a WebUI session."""
    from api import background_process, config as cfg

    before = set(cfg.PENDING_PROCESS_COMPLETIONS)
    evt = {
        "type": "completion",
        "session_id": "proc-unbound",
        "session_key": "nobody-registered-this",
        "command": "x",
        "exit_code": 0,
        "output": "",
    }
    background_process._process_one(evt)
    assert set(cfg.PENDING_PROCESS_COMPLETIONS) == before
