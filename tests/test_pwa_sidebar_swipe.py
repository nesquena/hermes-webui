from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_pwa_edge_swipe_gesture_is_registered_for_mobile_sidebar():
    assert "function _installPwaSidebarSwipeGesture" in BOOT_JS
    # #4660 review (Codex CORE): the guard element must NOT have its own touch
    # listener — it's pointer-events:none and the window-level capture handlers
    # below see the edge swipe regardless, so taps/scrolls starting in the strip
    # pass through to the app instead of being intercepted.
    assert "guard.addEventListener(" not in BOOT_JS
    assert "_onPwaSidebarEdgeGuardStart" not in BOOT_JS
    assert "window.addEventListener('touchstart', _onPwaSidebarSwipeStart, {capture:true,passive:true})" in BOOT_JS
    assert "window.addEventListener('touchmove', _onPwaSidebarSwipeMove, {capture:true,passive:false})" in BOOT_JS
    assert "window.addEventListener('touchend', _onPwaSidebarSwipeEnd, {capture:true,passive:true})" in BOOT_JS
    assert "window.addEventListener('touchcancel', _onPwaSidebarSwipeCancel, {capture:true,passive:true})" in BOOT_JS
    assert "window.addEventListener('pointerdown', _onPwaSidebarSwipeStart" in BOOT_JS
    assert "window.addEventListener('pointermove', _onPwaSidebarSwipeMove" in BOOT_JS
    assert "window.addEventListener('pointerup', _onPwaSidebarSwipeEnd" in BOOT_JS
    assert "window.addEventListener('pointercancel', _onPwaSidebarSwipeCancel" in BOOT_JS
    assert "function _isTouchPointerEvent" in BOOT_JS
    assert "if(_isTouchPointerEvent(e))return" in BOOT_JS


def test_pwa_sidebar_swipe_is_edge_gated_standalone_and_horizontal():
    assert "_isPwaStandalone()" in BOOT_JS
    assert "_PWA_SIDEBAR_SWIPE_EDGE" in BOOT_JS
    assert "_PWA_SIDEBAR_SWIPE_CLAIM" in BOOT_JS
    assert "_PWA_SIDEBAR_SWIPE_TRIGGER" in BOOT_JS
    assert "_PWA_SIDEBAR_SWIPE_MAX_VERTICAL" in BOOT_JS
    assert "clientX>_PWA_SIDEBAR_SWIPE_EDGE" in BOOT_JS.replace(" ", "")
    assert "dx>=_PWA_SIDEBAR_SWIPE_CLAIM" in BOOT_JS.replace(" ", "")
    assert "e.preventDefault()" in BOOT_JS[BOOT_JS.find("function _onPwaSidebarSwipeMove"):BOOT_JS.find("function _onPwaSidebarSwipeEnd")]
    assert "dx>=_PWA_SIDEBAR_SWIPE_TRIGGER" in BOOT_JS.replace(" ", "")
    assert "Math.abs(dy)<=_PWA_SIDEBAR_SWIPE_MAX_VERTICAL" in BOOT_JS.replace(" ", "")
    assert "dx>Math.abs(dy)*1.5" in BOOT_JS.replace(" ", "")

    assert "input,textarea,select,button,a,[contenteditable=\"true\"],.topbar-chips,.composer-left,.sidebar,.rightpanel" in BOOT_JS
    assert ".messages" not in BOOT_JS[BOOT_JS.find("function _isInteractiveSwipeTarget"):BOOT_JS.find("function _openMobileSidebarFromGesture")]


def test_pwa_sidebar_edge_guard_is_non_interactive_and_swipe_uses_window_capture():
    # #4660 review (Codex CORE): the left edge guard must not intercept hit-testing.
    # It is pointer-events:none (CSS) with NO dedicated touch listener, so taps and
    # vertical scrolls starting in the strip fall through to the .messages scroller.
    # The edge-swipe-to-open gesture is handled by window-level CAPTURE listeners,
    # and _onPwaSidebarSwipeMove only preventDefaults once horizontal intent is set.
    assert "_onPwaSidebarEdgeGuardStart" not in BOOT_JS, (
        "the interactive edge-guard handler must be gone (guard is pointer-events:none)"
    )
    assert "guard.addEventListener(" not in BOOT_JS
    move = BOOT_JS[BOOT_JS.find("function _onPwaSidebarSwipeMove"):BOOT_JS.find("function _onPwaSidebarSwipeEnd")]
    assert "_PWA_SIDEBAR_SWIPE_CLAIM" in move and "e.preventDefault()" in move, (
        "horizontal-intent claim should preventDefault only inside the move handler"
    )


def test_pwa_sidebar_swipe_opens_existing_mobile_drawer_without_desktop_collapse():
    assert "_openMobileSidebarFromGesture" in BOOT_JS
    assert "sidebar.classList.remove('sidebar-collapsed')" in BOOT_JS
    assert "sidebar.classList.add('mobile-open')" in BOOT_JS
    body = BOOT_JS[BOOT_JS.find("function _openMobileSidebarFromGesture"):BOOT_JS.find("function _installPwaSidebarSwipeGesture")]
    assert "overlay.classList.add('visible')" not in body
    assert "toggleSidebar(" not in BOOT_JS[BOOT_JS.find("function _openMobileSidebarFromGesture"):BOOT_JS.find("function _installPwaSidebarSwipeGesture")]


def test_pwa_sidebar_swipe_does_not_disable_horizontal_scrollers_globally():
    compact = STYLE_CSS.replace(" ", "")
    assert "html{touch-action" not in compact
    assert "body{touch-action" not in compact
    assert ".layout{touch-action" not in compact
