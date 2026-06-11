"""Structural tests for #3996 — close idle SSE on hidden tabs to free the
HTTP/1.1 connection pool (#3992), plus the reopen-on-re-show correctness fix.

Two idle persistent SSE connections per window (gateway stream + per-session
stream) plus the always-present session-events stream meant 2 windows x 3 = 6 =
the browser's per-origin HTTP/1.1 connection limit, so any subsequent fetch()
queued behind a saturated pool and timed out. #3996 adds Page Visibility API
hooks that close those streams while the tab is hidden and reopen them on
re-show, mirroring the existing ensureSessionEventsSSE() pattern.

These are source-grep checks (the hooks live in static JS with no server round
trip). The key regression guard is that the *per-session* stream actually
reopens on re-show: stopSessionStream() nulls _sessionStreamSessionId, so the
reopen path must NOT depend on that variable (it captures the id into a
dedicated _sessionStreamHiddenSid before closing).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_gateway_sse_has_visibility_hook():
    """startGatewaySSE installs a visibilitychange hook that closes on hide."""
    assert "_hermesGatewaySSEVisibilityHook" in SESSIONS_JS
    # Closes on hide, and skips opening while hidden to save pool slots.
    assert "stopGatewaySSE()" in SESSIONS_JS
    start_idx = SESSIONS_JS.find("function startGatewaySSE()")
    assert start_idx != -1
    block = SESSIONS_JS[start_idx:start_idx + 1200]
    assert "visibilitychange" in block
    assert "document.hidden" in block


def test_session_stream_has_visibility_hook():
    """startSessionStream installs a visibilitychange hook."""
    assert "_hermesSessionStreamVisibilityHook" in MESSAGES_JS
    start_idx = MESSAGES_JS.find("function startSessionStream(sid)")
    assert start_idx != -1
    block = MESSAGES_JS[start_idx:start_idx + 1600]
    assert "visibilitychange" in block
    assert "document.hidden" in block
    # Must skip opening a new EventSource while the tab is hidden.
    assert "if (typeof document !== 'undefined' && document.hidden) {" in block


def test_session_stream_reopens_from_dedicated_var_not_nulled_id():
    """Regression: the per-session SSE must reopen on re-show.

    stopSessionStream() sets _sessionStreamSessionId = null, so the visibility
    reopen path must capture the id into a dedicated holder BEFORE closing and
    reopen from that — otherwise the stream closes on hide and never comes back.
    """
    # Dedicated holder declared at module scope.
    assert "_sessionStreamHiddenSid" in MESSAGES_JS, (
        "expected a dedicated holder var for the hidden-tab session id"
    )

    start_idx = MESSAGES_JS.find("function startSessionStream(sid)")
    block = MESSAGES_JS[start_idx:start_idx + 1600]

    # On hide: capture the id, then stop.
    assert "_sessionStreamHiddenSid = _sessionStreamSessionId" in block
    # On re-show: reopen from the captured holder, NOT from the nulled id.
    assert "else if (_sessionStreamHiddenSid)" in block
    assert "void startSessionStream(resumeSid)" in block or \
           "startSessionStream(_sessionStreamHiddenSid)" in block

    # Guard against the buggy form that reopens off the (already-nulled) id.
    assert "else if (_sessionStreamSessionId) {\n        void startSessionStream(_sessionStreamSessionId)" not in block, (
        "reopen must not depend on _sessionStreamSessionId — stopSessionStream() nulls it"
    )


def test_stop_session_stream_still_nulls_session_id():
    """Confirms the precondition the reopen fix defends against."""
    stop_idx = MESSAGES_JS.find("function stopSessionStream()")
    assert stop_idx != -1
    block = MESSAGES_JS[stop_idx:stop_idx + 400]
    assert "_sessionStreamSessionId = null" in block


def test_session_stream_hidden_open_preserves_id_for_reopen():
    """A session opened while the tab is ALREADY hidden must still reopen on re-show.

    Codex CORE: startSessionStream(sid) sets _sessionStreamSessionId and then
    returns early when document.hidden. If it doesn't also record the id in the
    dedicated holder, the visibility handler (which reopens only from the holder)
    never reattaches — silently dropping bg_task_complete / server_turn_started
    for a session loaded in a background tab. The hidden-skip path must set
    _sessionStreamHiddenSid = sid before returning.
    """
    start_idx = MESSAGES_JS.find("function startSessionStream(sid)")
    block = MESSAGES_JS[start_idx:start_idx + 1700]
    # Find the hidden-tab early-return skip path (distinct from the in-hook
    # `if (document.hidden)` branch) and assert it preserves the id.
    hidden_idx = block.find("!== 'undefined' && document.hidden) {")
    assert hidden_idx != -1, "expected a braced hidden-tab skip block (not a bare return)"
    hidden_block = block[hidden_idx:hidden_idx + 160]
    assert "_sessionStreamHiddenSid = sid" in hidden_block, (
        "hidden-tab skip must record the pending session id for reopen on re-show"
    )

