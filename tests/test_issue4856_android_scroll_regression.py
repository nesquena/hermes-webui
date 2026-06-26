"""Regression tests for #4856: Android scroll-to-top on every interaction."""

from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")

_FUNC_MARKER = "window._fixMobileScrollJank=function _fixMobileScrollJank(){"
_RAF_MARKER = "requestAnimationFrame(()=>{"


def _extract_fix_mobile_scroll_jank(src: str) -> str:
    idx = src.find(_FUNC_MARKER)
    assert idx != -1, "_fixMobileScrollJank not found in ui.js"
    depth = 0
    for i, ch in enumerate(src[idx:], idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
    raise AssertionError("Could not extract _fixMobileScrollJank")


def _extract_raf_body(fn_src: str) -> str:
    idx = fn_src.find(_RAF_MARKER)
    assert idx != -1, "requestAnimationFrame callback not found in function"
    depth = 0
    for i, ch in enumerate(fn_src[idx:], idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return fn_src[idx : i + 1]
    raise AssertionError("Could not extract rAF body")


def test_fix_sets_none_not_auto():
    # Base-fails/head-passes: function must set 'none' to suppress Chromium
    # scroll-anchor re-selection; 'auto' is already the CSS default so setting
    # it is a no-op and leaves the anchor engine active during the DOM wipe.
    fn = _extract_fix_mobile_scroll_jank(UI_JS)
    assert "overflowAnchor='none'" in fn, (
        "_fixMobileScrollJank() must set overflowAnchor='none' to suppress "
        "Chromium scroll-anchor re-selection during the DOM wipe (#4856)."
    )
    assert "overflowAnchor='auto'" not in fn, (
        "_fixMobileScrollJank() must not set overflowAnchor='auto'; that is "
        "already the CSS resting value on mobile and is a no-op."
    )


def test_raf_cleanup_checks_none():
    # The rAF guard must check for 'none' so it only clears the inline style
    # it set; checking 'auto' was always false and left the inline style on.
    fn = _extract_fix_mobile_scroll_jank(UI_JS)
    raf = _extract_raf_body(fn)
    assert "overflowAnchor==='none'" in raf, (
        "The rAF cleanup in _fixMobileScrollJank() must check for 'none' so it "
        "clears the inline style after the synchronous scrollTop write lands."
    )
    assert "overflowAnchor==='auto'" not in raf, (
        "The rAF cleanup must not check for 'auto'; that check was always false."
    )


def test_rebuild_path_calls_fix_before_wipe():
    # renderMessages() must call _fixMobileScrollJank() before innerHTML='' so
    # anchor suppression is active during the full wipe-and-rebuild window.
    fix_idx = UI_JS.find("window._fixMobileScrollJank()")
    assert fix_idx != -1, (
        "renderMessages() must call window._fixMobileScrollJank() before innerHTML=''."
    )
    wipe_idx = UI_JS.find("innerHTML=''", fix_idx)
    assert wipe_idx != -1, (
        "innerHTML='' not found after _fixMobileScrollJank() call site."
    )
    assert fix_idx < wipe_idx, (
        "_fixMobileScrollJank() must be called before innerHTML='' in renderMessages()."
    )


def test_rebuild_path_marks_dom_wipe_scroll_as_programmatic():
    # During innerHTML='' the scroller can transiently collapse to clientHeight
    # and clamp scrollTop to 0. That browser event must be suppressed as
    # programmatic; otherwise the scroll listener treats it as user upward
    # intent and disables live auto-follow.
    fix_idx = UI_JS.find("window._fixMobileScrollJank()")
    assert fix_idx != -1, "renderMessages() guard call not found"
    wipe_idx = UI_JS.find("innerHTML=''", fix_idx)
    assert wipe_idx != -1, "innerHTML='' not found after _fixMobileScrollJank()"
    window = UI_JS[fix_idx:wipe_idx]
    assert "_programmaticScroll=true" in window, (
        "renderMessages() must mark the DOM wipe/rebuild scroll event as "
        "programmatic before innerHTML='' can clamp scrollTop."
    )
    assert "_programmaticScrollSetAt=performance.now()" in window
    assert UI_JS.find("_deferClearProgrammaticScroll(160)", wipe_idx) != -1, (
        "renderMessages() must clear the programmatic-scroll suppression after "
        "the rebuild/post-render paint window."
    )


def test_recent_render_scroll_artifact_window_suppresses_upward_unpin():
    # Some browsers emit a follow-up scroll event shortly after renderMessages()
    # finishes (for example while late layout settles after a send). With no
    # wheel/touch intent, that post-render upward delta is still a render
    # artifact and must not disable live follow.
    assert "let _lastMessageRenderAt=-Infinity" in UI_JS
    assert "_lastMessageRenderAt=performance.now()" in UI_JS
    assert "function _recentMessageRenderArtifactWindow" in UI_JS
    listener_idx = UI_JS.find("el.addEventListener('scroll'")
    assert listener_idx != -1, "messages scroll listener not found"
    listener = UI_JS[listener_idx: listener_idx + 2500]
    assert "_recentMessageRenderArtifactWindow(1400)" in listener
    assert "!_recentMessageTouchScrollIntent()" in listener
    assert "!_recentNonMessageScrollIntent()" in listener
    assert listener.find("return;") < listener.find("if(movedUp){"), (
        "recent render artifact scrolls must return before the movedUp branch "
        "can mark the reader unpinned."
    )


def test_streaming_tick_calls_fix_before_dom_writes():
    # The streaming render tick in messages.js must call _fixMobileScrollJank()
    # before _lastRenderMs=performance.now() so anchor suppression covers every
    # incremental DOM update during streaming.
    guard_idx = MESSAGES_JS.find("window._fixMobileScrollJank")
    assert guard_idx != -1, (
        "The streaming tick must call window._fixMobileScrollJank() before DOM writes."
    )
    render_idx = MESSAGES_JS.find("_lastRenderMs=performance.now()")
    assert render_idx != -1, "streaming render timestamp not found in messages.js"
    assert guard_idx < render_idx, (
        "The mobile scroll-jank guard must run before streaming DOM work begins."
    )
