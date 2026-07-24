"""Regression tests for iPad sidebar scroll freeze.

On touch-primary devices (iPad, iPhone), the session-list sidebar scroll
freezes when scrolled below the top. Three root causes addressed:

1. CSS: .sidebar used overflow:visible, causing scroll-chain/rubber-band
   issues on iPadOS WebKit. Changed to overflow:hidden so the scroll
   surface stays contained within .session-list. Resize handle moved to
   right:0 so it isn't clipped.

2. CSS: .session-list was missing -webkit-overflow-scrolling:touch,
   which iOS Safari needs for smooth momentum scrolling. Without it,
   overscroll-behavior-y:contain makes the list feel "stuck" at the
   boundary because there's no rubber-band effect.

3. JS: renderSessionListFromCache() does innerHTML='' on every call,
   which terminates the browser's native momentum scroll gesture on
   touch devices. Background callers (SSE syncs, unread updates, gateway
   polls) can trigger this mid-scroll. Added a touch-aware guard that
   defers background renders while the user is actively scrolling.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


# ── CSS: sidebar overflow ──────────────────────────────────────────────────


def test_sidebar_uses_overflow_hidden_not_visible():
    """The sidebar container must use overflow:hidden, not overflow:visible.

    overflow:visible on iPadOS WebKit causes scroll chaining and rubber-band
    issues that freeze the session-list scroller. overflow:hidden keeps the
    scroll surface contained within .session-list.
    """
    match = re.search(r'\.sidebar\{width:300px[^}]+\}', STYLE_CSS)
    assert match, ".sidebar base rule not found"
    rule = match.group(0)
    assert "overflow:hidden" in rule, \
        f".sidebar must use overflow:hidden, got: {rule}"


def test_sidebar_resize_handle_not_clipped():
    """The resize handle must sit inside the sidebar border (right:0, not right:-2px).

    With overflow:hidden, a right:-2px handle would be clipped and invisible.
    """
    match = re.search(r'^\.sidebar \.resize-handle\{right:[^}]+\}', STYLE_CSS, re.MULTILINE)
    assert match, ".sidebar .resize-handle positioning rule not found"
    rule = match.group(0)
    assert "right:0" in rule, \
        f".sidebar .resize-handle must use right:0, got: {rule}"


def test_session_list_has_webkit_overflow_scrolling_touch():
    """The session list must have -webkit-overflow-scrolling:touch for iOS momentum scroll."""
    assert "-webkit-overflow-scrolling:touch" in STYLE_CSS


def test_session_list_scroll_boundary_unchanged():
    """The session-list scroll boundary must remain intact."""
    assert "overscroll-behavior-y:contain" in STYLE_CSS
    assert "touch-action:pan-y" in STYLE_CSS
    assert "overflow-anchor:none" in STYLE_CSS


# ── JS: touch scroll guard ─────────────────────────────────────────────────


def test_touch_primary_helper_exists():
    """A helper to detect touch-primary devices must exist."""
    assert "function _isTouchPrimary()" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_isTouchPrimary")
    assert "pointer:coarse" in fn


def test_touch_scroll_detection_exists():
    """A function to detect active touch scrolling on the session list must exist."""
    assert "function _isSessionListTouchScrolling()" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_isSessionListTouchScrolling")
    assert "_isTouchPrimary()" in fn
    assert "SESSION_LIST_TOUCH_INTERACTION_IDLE_MS" in fn
    assert "_sessionListPointerActive" in fn


def test_touch_defer_function_exists():
    """A function to defer renderSessionListFromCache on touch must exist."""
    assert "function _deferRenderSessionListFromCache()" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_deferRenderSessionListFromCache")
    assert "setTimeout" in fn
    assert "renderSessionListFromCache" in fn


def test_render_session_list_accepts_force_option():
    """renderSessionListFromCache must accept an opts parameter with force flag."""
    assert "function renderSessionListFromCache(opts)" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "renderSessionListFromCache")
    assert "opts&&opts.force" in fn
    assert "_isSessionListTouchScrolling()" in fn
    assert "_deferRenderSessionListFromCache()" in fn


def test_touch_idle_constant_exists():
    """A longer idle window for touch devices must be defined."""
    assert "SESSION_LIST_TOUCH_INTERACTION_IDLE_MS" in SESSIONS_JS
    match = re.search(r'SESSION_LIST_TOUCH_INTERACTION_IDLE_MS\s*=\s*(\d+)', SESSIONS_JS)
    assert match
    value = int(match.group(1))
    assert value > 700, f"touch idle should be > 700ms, got {value}"


def test_deferred_timer_cleared_on_render():
    """When a non-deferred render proceeds, the pending touch timer must be cleared."""
    fn = _extract_fn(SESSIONS_JS, "renderSessionListFromCache")
    assert "_pendingTouchDeferredRenderTimer" in fn
    assert "clearTimeout(_pendingTouchDeferredRenderTimer)" in fn


def test_virtual_scroll_render_uses_force():
    """The scroll-driven virtual render must bypass the touch guard so rows appear during scroll."""
    # With virtualization disabled on touch, the scroll listener bails early.
    # This test verifies the force bypass exists for non-touch virtual scroll.
    fn = _extract_fn(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    assert "{force:true}" in fn, \
        "Virtual scroll render must use {force:true} so new rows load during scroll"


def test_virtual_resync_render_uses_force():
    """The post-render virtual window resync must also bypass the touch guard."""
    fn = _extract_fn(SESSIONS_JS, "_resyncSessionVirtualWindowAfterRender")
    assert "{force:true}" in fn, \
        "Virtual resync render must use {force:true} so scroll correction works during touch scroll"


def test_virtualization_disabled_on_touch():
    """_sessionVirtualWindow must return a batched window on touch-primary devices."""
    fn = _extract_fn(SESSIONS_JS, "_sessionVirtualWindow")
    assert "_isTouchPrimary()" in fn, \
        "_sessionVirtualWindow must check _isTouchPrimary() to enable batched rendering on touch"
    assert "SESSION_TOUCH_INITIAL_BATCH" in fn, \
        "_sessionVirtualWindow must use SESSION_TOUCH_INITIAL_BATCH for the initial batch size"
    assert "virtualized:false" in fn, \
        "_sessionVirtualWindow must return virtualized:false on touch devices"


def test_scroll_listener_skips_on_touch():
    """The scroll-driven RAF must be skipped entirely on touch devices."""
    fn = _extract_fn(SESSIONS_JS, "_scheduleSessionVirtualizedRender")
    assert "_isTouchPrimary()" in fn, \
        "_scheduleSessionVirtualizedRender must bail early on touch devices to preserve momentum"


def test_touch_sentinel_observer_exists():
    """An IntersectionObserver-based sentinel must exist for incremental batch loading."""
    assert "function _ensureTouchSentinelObserver" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    assert "IntersectionObserver" in fn
    assert "rootMargin" in fn


def test_touch_sentinel_setup_exists():
    """A setup function must create and observe the sentinel element."""
    assert "function _setupTouchSentinel" in SESSIONS_JS
    fn = _extract_fn(SESSIONS_JS, "_setupTouchSentinel")
    assert "data-touch-sentinel" in fn
    assert "Loading more" in fn


def test_touch_batch_constants_exist():
    """Batch size constants must be defined for incremental loading."""
    assert "SESSION_TOUCH_INITIAL_BATCH" in SESSIONS_JS
    assert "SESSION_TOUCH_BATCH_SIZE" in SESSIONS_JS
    # Initial batch should be large enough to fill a viewport
    m1 = re.search(r'SESSION_TOUCH_INITIAL_BATCH\s*=\s*(\d+)', SESSIONS_JS)
    assert m1, "INITIAL_BATCH value not found"
    assert int(m1.group(1)) >= 40, "initial batch should be >= 40 rows"
    m2 = re.search(r'SESSION_TOUCH_BATCH_SIZE\s*=\s*(\d+)', SESSIONS_JS)
    assert m2, "BATCH_SIZE value not found"
    assert int(m2.group(1)) >= 20, "batch size should be >= 20 rows"


def test_touch_batch_reset_on_filter_change():
    """The batch count must reset when the search filter or total changes."""
    assert "sessionTouchPrevFilter" in SESSIONS_JS
    assert "sessionTouchPrevTotal" in SESSIONS_JS
    assert "SESSION_TOUCH_INITIAL_BATCH" in SESSIONS_JS


def test_ensure_touch_sentinel_disconnects_old_observer():
    """_ensureTouchSentinelObserver must disconnect existing observer before creating a new one."""
    fn = _extract_fn(SESSIONS_JS, "_ensureTouchSentinelObserver")
    assert "disconnect()" in fn
    assert "_touchSentinelObserver=null" in fn
