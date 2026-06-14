"""Structural tests for #4151 — PWA two-window connection-pool saturation.

#3992/#3996 close the idle SSE streams on the Page Visibility API
(`visibilitychange` / `document.hidden`). But a PWA *standalone* window does NOT
reliably fire `visibilitychange` when it loses focus to another window of the
same app — `document.hidden` only flips on minimize. So two side-by-side PWA
windows both stay `visibilityState==='visible'`, each holds its sidebar SSE
streams open, and 2x3 = 6 = the per-origin HTTP/1.1 connection limit; every
later fetch() queues behind the saturated pool and times out (#4151).

The fix makes the two GLOBAL sidebar streams (session-events + gateway) also
close on window `blur` (gated on `document.hasFocus()`, the signal
`visibilitychange` misses) and reopen on `focus`, via a shared
`_sidebarSseBackgrounded()` predicate and a debounced `_installSidebarSseFocusHook()`.

CRITICAL SCOPE GUARD (the regression these tests lock): the PER-SESSION live
stream (`startSessionStream` in messages.js) must stay visibility-only and must
NOT be torn down on blur — it carries live `bg_task_complete` toasts +
`server_turn_started` live-view that an unfocused-but-VISIBLE window must still
receive. So the focus hook lives in sessions.js and only touches
`_closeSessionEventsSSE()` + `stopGatewaySSE()`.

Source-grep checks (the hooks live in static JS with no server round trip).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_backgrounded_predicate_uses_hasfocus_not_only_hidden():
    """_sidebarSseBackgrounded() must consult document.hasFocus(), not only document.hidden.

    document.hidden alone is exactly what misses the PWA blur case; the predicate
    has to treat a visible-but-unfocused window as backgrounded.
    """
    assert "function _sidebarSseBackgrounded()" in SESSIONS_JS
    start = SESSIONS_JS.find("function _sidebarSseBackgrounded()")
    block = SESSIONS_JS[start:start + 320]
    assert "document.hidden" in block
    assert "document.hasFocus" in block
    assert "!document.hasFocus()" in block


def test_focus_hook_closes_both_global_sidebar_streams():
    """The blur path closes the session-events AND gateway streams."""
    assert "function _installSidebarSseFocusHook()" in SESSIONS_JS
    start = SESSIONS_JS.find("function _installSidebarSseFocusHook()")
    block = SESSIONS_JS[start:start + 1700]
    # Installed once.
    assert "_hermesSidebarSseFocusHook" in block
    # Blur listener tears down both global streams.
    assert "window.addEventListener('blur'" in block
    assert "_closeSessionEventsSSE()" in block
    assert "stopGatewaySSE()" in block
    # Focus listener reopens both and refreshes the list.
    assert "window.addEventListener('focus'" in block
    assert "ensureSessionEventsSSE()" in block
    assert "startGatewaySSE()" in block


def test_blur_close_is_debounced_and_rechecks_focus_at_fire_time():
    """A transient blur must not thrash the streams.

    The blur close is scheduled on a timer and re-checks _sidebarSseBackgrounded()
    when it fires, so focus returning during the debounce cancels the teardown.
    """
    start = SESSIONS_JS.find("function _installSidebarSseFocusHook()")
    block = SESSIONS_JS[start:start + 1700]
    assert "_sidebarSseBlurCloseTimer" in block
    assert "setTimeout(" in block
    # Re-check guards the actual close so a returned focus is a no-op.
    assert "if(_sidebarSseBackgrounded()){" in block
    # Focus listener clears any pending blur-close timer.
    assert "clearTimeout(_sidebarSseBlurCloseTimer)" in block


def test_focus_reopen_does_not_thrash_the_gateway_stream():
    """The focus handler must NOT unconditionally restart the gateway stream.

    startGatewaySSE() begins with an unconditional stopGatewaySSE() (it is NOT
    idempotent, unlike ensureSessionEventsSSE()'s `if(_sessionEventsSSE) return`).
    On a transient blur shorter than the 1s debounce, the blur-close timer is
    cleared and the gateway stream is never torn down — so an unconditional
    startGatewaySSE() on the following focus would drop+reconnect the live gateway,
    cancel its poll fallback, and reset probe/warning state on every window switch
    (the exact thrash the debounce exists to prevent, in the multi-window scenario
    #4151 targets). The reopen must therefore be guarded on the gateway actually
    being closed. (greptile P1.)
    """
    start = SESSIONS_JS.find("function _installSidebarSseFocusHook()")
    block = SESSIONS_JS[start:start + 1700]
    focus_idx = block.find("window.addEventListener('focus'")
    assert focus_idx != -1
    focus_body = block[focus_idx:]
    # The gateway reopen is guarded on the stream being closed (mirrors the
    # session-events idempotency), not called unconditionally.
    assert "if(!_gatewaySSE) startGatewaySSE()" in focus_body, (
        "focus handler must guard startGatewaySSE() on `!_gatewaySSE` so a "
        "transient blur+focus does not drop+reconnect a still-open gateway stream"
    )
    # And it must NOT call startGatewaySSE() bare (unguarded) on focus.
    assert "\n    startGatewaySSE();" not in focus_body


def test_session_events_open_guard_uses_backgrounded_predicate():
    """ensureSessionEventsSSE installs the focus hook and gates open on the predicate."""
    start = SESSIONS_JS.find("function ensureSessionEventsSSE()")
    assert start != -1
    block = SESSIONS_JS[start:start + 700]
    assert "_installSidebarSseFocusHook()" in block
    # Open guard is the focus-aware predicate, not the old hidden-only check.
    assert "if(_sidebarSseBackgrounded()) return;" in block


def test_gateway_open_guard_uses_backgrounded_predicate():
    """startGatewaySSE installs the focus hook and gates open on the predicate."""
    start = SESSIONS_JS.find("function startGatewaySSE()")
    assert start != -1
    block = SESSIONS_JS[start:start + 700]
    assert "_installSidebarSseFocusHook()" in block
    assert "if(_sidebarSseBackgrounded()) return;" in block


def test_per_session_stream_NOT_closed_on_blur():
    """REGRESSION GUARD: the per-session live stream must stay visibility-only.

    startSessionStream carries live bg_task_complete toasts + server_turn_started
    live-view that an unfocused-but-visible window must still get. The focus hook
    must NOT tear it down, so:
      (a) messages.js must not add a window 'blur' listener that calls stopSessionStream, and
      (b) the focus hook in sessions.js must not reference stopSessionStream/startSessionStream.
    """
    # (a) the per-session stream file must not tear down the stream on blur.
    #     (A pre-existing composer speech-synthesis blur handler on _msgEl is
    #     fine — the guard is specifically that no blur path calls
    #     stopSessionStream.) Scan each blur-listener body in messages.js.
    import re
    for m in re.finditer(r"addEventListener\(\s*['\"]blur['\"]", MESSAGES_JS):
        tail = MESSAGES_JS[m.start():m.start() + 200]
        assert "stopSessionStream" not in tail, (
            "a blur listener in messages.js tears down the per-session stream — "
            "that regresses live bg_task_complete / server_turn_started for an "
            "unfocused-but-visible window (#4151 scope guard)"
        )
    # (b) the sidebar focus hook only manages the two global streams.
    start = SESSIONS_JS.find("function _installSidebarSseFocusHook()")
    block = SESSIONS_JS[start:start + 1700]
    assert "stopSessionStream" not in block
    assert "startSessionStream" not in block


def test_per_session_stream_still_visibility_gated():
    """Sanity: the per-session stream keeps its existing visibility hook untouched."""
    assert "_hermesSessionStreamVisibilityHook" in MESSAGES_JS
    start = MESSAGES_JS.find("function startSessionStream(sid)")
    block = MESSAGES_JS[start:start + 1700]
    assert "visibilitychange" in block
    assert "document.hidden" in block
