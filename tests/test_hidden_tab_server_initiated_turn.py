"""Hidden-tab server-initiated turn render (self-wake / cron / restart).

A turn started SERVER-SIDE (self-wake, cron, restart hook) fans a
``server_turn_started`` frame onto the per-session live-view SSE channel so an
open tab renders it without a manual refresh. But while a tab is HIDDEN the
WebUI deliberately does NOT hold that persistent SSE open (connection-pool
budget — see issue #3992 / #4151). So a hidden tab missed server-initiated
turns and only reconciled on the next user interaction.

This bridges the gap with a lightweight poll of ``/api/session/status`` (one
short GET per tick, NOT a held connection) that attaches the existing live
renderer when it sees a *live* ``active_stream_id``. These are source-lock
tests pinning the contract:

- backend ``session_status`` exposes ``active_stream_id``, but only when the
  stream is genuinely live (present in STREAMS / ACTIVE_RUNS) — a stale id left
  over from a crashed/restarted run must surface as ``None`` so the poller never
  attaches a renderer to a dead stream;
- frontend declares the poll lifecycle (start/stop/attach) and starts it on
  BOTH hidden-tab paths: a session opened while already hidden, AND a visible
  tab that transitions to hidden via the ``visibilitychange`` hook.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSION_OPS = (REPO_ROOT / "api" / "session_ops.py").read_text(encoding="utf-8")


# ── Backend: session_status exposes a LIVE-validated active_stream_id ───────

def test_session_status_exposes_active_stream_id_field():
    """session_status() must return an active_stream_id key for the poller."""
    assert "'active_stream_id'" in SESSION_OPS
    # It is derived through the live-validation helper, not the raw attribute,
    # so a stale id from a crashed/restarted run is not surfaced.
    assert "_live_active_stream_id(" in SESSION_OPS


def test_live_active_stream_id_is_stale_safe():
    """The helper only returns an id that is actually live in STREAMS/ACTIVE_RUNS.

    Exercises the real helper: a made-up id (not in either registry) must come
    back as None; an id present in STREAMS or ACTIVE_RUNS must be returned.
    """
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from types import SimpleNamespace
    from api import config as cfg
    from api.session_ops import _live_active_stream_id

    assert _live_active_stream_id(SimpleNamespace(active_stream_id=None)) is None
    assert _live_active_stream_id(SimpleNamespace(active_stream_id="ghost-not-in-any-registry")) is None

    with cfg.STREAMS_LOCK:
        cfg.STREAMS["live-streams-id"] = object()
    try:
        assert _live_active_stream_id(SimpleNamespace(active_stream_id="live-streams-id")) == "live-streams-id"
    finally:
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.pop("live-streams-id", None)

    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS["live-runs-id"] = object()
    try:
        assert _live_active_stream_id(SimpleNamespace(active_stream_id="live-runs-id")) == "live-runs-id"
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.pop("live-runs-id", None)


# ── Frontend: poll lifecycle declared ──────────────────────────────────────

def test_frontend_declares_hidden_poll_lifecycle():
    """The hidden-tab active-stream poll start/stop/attach functions exist."""
    assert "function _startHiddenActiveStreamPoll(sid)" in MESSAGES_JS
    assert "function _stopHiddenActiveStreamPoll()" in MESSAGES_JS
    assert "function _attachServerInitiatedStream(sid, streamId, recovered)" in MESSAGES_JS


def test_hidden_poll_hits_session_status_and_attaches_as_replay():
    """The poll tick fetches /api/session/status and attaches mid-flight turns.

    A server-initiated turn caught by the poll is already in progress, so it
    must attach via the reconnecting/replay path (recovered=true) — the same
    path the server_turn_started on-subscribe replay uses — rather than
    expecting token 0.
    """
    start = MESSAGES_JS.find("function _startHiddenActiveStreamPoll(sid)")
    assert start != -1
    body = MESSAGES_JS[start:start + 1400]
    assert "api/session/status?session_id=" in body
    assert "d.active_stream_id" in body
    # attaches as replay (recovered=true) — turn is already mid-flight
    assert "_attachServerInitiatedStream(sid, streamId, true)" in body


def test_hidden_poll_started_on_both_hidden_paths():
    """The poll must start on BOTH ways a tab ends up hidden with a session.

    (1) visibilitychange → hidden: an already-open visible tab going to the
        background still needs the bridge, so the hook's hidden branch starts it.
    (2) startSessionStream early-return: a session loaded while the tab is
        ALREADY hidden never opens the SSE, so its skip path starts it too.
    """
    # Path 1: inside the visibilitychange hook's hidden branch. Anchor on the
    # session-stream hook specifically (there are other unrelated
    # visibilitychange listeners in the file).
    hook_idx = MESSAGES_JS.find("_hermesSessionStreamVisibilityHook")
    assert hook_idx != -1
    hook_block = MESSAGES_JS[hook_idx:hook_idx + 900]
    assert "_startHiddenActiveStreamPoll(_sessionStreamHiddenSid)" in hook_block

    # Path 2: inside startSessionStream's hidden early-return skip.
    start_idx = MESSAGES_JS.find("function startSessionStream(sid)")
    block = MESSAGES_JS[start_idx:start_idx + 2900]
    skip_idx = block.find("!== 'undefined' && document.hidden) {")
    assert skip_idx != -1
    skip_block = block[skip_idx:skip_idx + 400]
    assert "_startHiddenActiveStreamPoll(sid)" in skip_block


def test_hidden_poll_stops_on_session_teardown():
    """stopSessionStream() must also tear down the hidden poll (session switch)."""
    stop_idx = MESSAGES_JS.find("function stopSessionStream()")
    assert stop_idx != -1
    block = MESSAGES_JS[stop_idx:stop_idx + 400]
    assert "_stopHiddenActiveStreamPoll()" in block
