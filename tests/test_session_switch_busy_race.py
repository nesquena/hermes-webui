"""Regression coverage for session-switch busy-state race and live-turn restore.

Switching from a streaming session to an idle one must clear S.busy before the
async _ensureMessagesLoaded gap. Otherwise _isSessionLocallyStreaming() treats
the newly opened session as locally streaming while messages are still loading.

Switching back to a streaming session must restore the snapshotted live turn
instead of rebuilding thinking/worklog chrome from scratch.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
UI_SRC = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start != -1, f"missing {signature}"
    brace = src.find("{", start)
    assert brace != -1, f"missing opening brace for {signature}"
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : i]
    raise AssertionError(f"could not extract function body for {signature}")


def test_loadSession_clears_busy_before_async_message_load_when_server_idle():
    body = _function_body(SESSIONS_SRC, "async function loadSession(")

    idle_reset = body.find("if(!activeStreamId){")
    assert idle_reset != -1, "loadSession must gate idle cleanup on missing active_stream_id"
    idle_block = body[idle_reset : idle_reset + 500]
    assert "S.busy=false" in idle_block, "idle switch must clear S.busy immediately"
    assert "S.activeStreamId=null" in idle_block, "idle switch must clear S.activeStreamId immediately"

    ensure_load = body.find("await _ensureMessagesLoaded(sid")
    assert ensure_load != -1, "loadSession must still lazy-load messages for idle sessions"
    assert idle_reset < ensure_load, (
        "S.busy must be cleared before _ensureMessagesLoaded so session-list polling "
        "during the async gap does not mark the new session as locally streaming"
    )


def test_loadSession_snapshots_live_turn_before_wiping_message_pane():
    body = _function_body(SESSIONS_SRC, "async function loadSession(")

    snap_pos = body.find("snapshotLiveTurnHtmlForSession(currentSid)")
    # Anchor on the actual loading-placeholder marker (unique), not the
    # whitespace-sensitive innerHTML literal which also matches the
    # "Session not available" error handler. (Maintainer review.)
    wipe_pos = body.find("Loading conversation...")
    assert snap_pos != -1, "loadSession must snapshot the outgoing live turn before switching"
    assert wipe_pos != -1, "loadSession must still show the loading placeholder on switch"
    assert snap_pos < wipe_pos, "snapshot must run before msgInner is replaced with the loading placeholder"


def test_loadSession_restores_live_turn_on_active_stream_return_path():
    body = _function_body(SESSIONS_SRC, "async function loadSession(")

    # The restore that actually fires on switch-back is the Phase 2a path: after
    # loadInflightState() rehydrates INFLIGHT for an active stream, the streaming
    # branch calls restoreLiveTurnHtmlForSession(sid). (The old Phase-2b idle-branch
    # call was unreachable — INFLIGHT is always seeded by then — so assert the live
    # Phase 2a path. Maintainer review.)
    phase2a = body.find("Phase 2a")
    assert phase2a != -1, "loadSession must keep the Phase 2a streaming-restore branch"
    inflight_load = body.find("loadInflightState(sid", phase2a)
    assert inflight_load != -1, "Phase 2a must rehydrate INFLIGHT from persisted state for an active stream"
    restore = body.find("restoreLiveTurnHtmlForSession(sid)", inflight_load)
    assert restore != -1, (
        "the active-stream return path must restore the snapshotted live-turn HTML "
        "after rehydrating INFLIGHT (Phase 2a), instead of rebuilding the worklog shell"
    )


def test_activity_timer_reads_pending_started_at():
    body = _function_body(UI_SRC, "function _activityElapsedStartedAt(")
    assert "pending_started_at" in body
    assert "data-turn-started-at" in body or "turnStartedAt" in body
