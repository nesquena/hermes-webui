from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_pwa_edge_swipe_gesture_is_registered_for_mobile_sidebar():
    assert "function _installPwaSidebarSwipeGesture" in BOOT_JS
    assert "window.addEventListener('pointerdown', _onPwaSidebarSwipeStart" in BOOT_JS
    assert "window.addEventListener('pointermove', _onPwaSidebarSwipeMove" in BOOT_JS
    assert "window.addEventListener('pointerup', _onPwaSidebarSwipeEnd" in BOOT_JS
    assert "window.addEventListener('pointercancel', _onPwaSidebarSwipeCancel" in BOOT_JS


def test_pwa_sidebar_swipe_is_edge_gated_standalone_and_horizontal():
    assert "_isPwaStandalone()" in BOOT_JS
    assert "_PWA_SIDEBAR_SWIPE_EDGE" in BOOT_JS
    assert "_PWA_SIDEBAR_SWIPE_TRIGGER" in BOOT_JS
    assert "_PWA_SIDEBAR_SWIPE_MAX_VERTICAL" in BOOT_JS
    assert "clientX>_PWA_SIDEBAR_SWIPE_EDGE" in BOOT_JS.replace(" ", "")
    assert "dx>=_PWA_SIDEBAR_SWIPE_TRIGGER" in BOOT_JS.replace(" ", "")
    assert "Math.abs(dy)<=_PWA_SIDEBAR_SWIPE_MAX_VERTICAL" in BOOT_JS.replace(" ", "")
    assert "dx>Math.abs(dy)*1.5" in BOOT_JS.replace(" ", "")

    assert "input,textarea,select,button,a,[contenteditable=\"true\"],.topbar-chips,.composer-left,.sidebar,.rightpanel" in BOOT_JS
    assert ".messages" not in BOOT_JS[BOOT_JS.find("function _isInteractiveSwipeTarget"):BOOT_JS.find("function _openMobileSidebarFromGesture")]


def test_pwa_sidebar_swipe_opens_existing_mobile_drawer_without_desktop_collapse():
    assert "_openMobileSidebarFromGesture" in BOOT_JS
    assert "sidebar.classList.remove('sidebar-collapsed')" in BOOT_JS
    assert "sidebar.classList.add('mobile-open')" in BOOT_JS
    assert "overlay.classList.add('visible')" in BOOT_JS
    assert "toggleSidebar(" not in BOOT_JS[BOOT_JS.find("function _openMobileSidebarFromGesture"):BOOT_JS.find("function _installPwaSidebarSwipeGesture")]


def test_pwa_sidebar_swipe_does_not_disable_horizontal_scrollers_globally():
    compact = STYLE_CSS.replace(" ", "")
    assert "html{touch-action" not in compact
    assert "body{touch-action" not in compact
    assert ".layout{touch-action" not in compact
