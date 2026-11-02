"""Sidebar swipe and Ctrl/Cmd+B stay quiet while the user is typing."""

from pathlib import Path

BOOT = (Path(__file__).resolve().parents[1] / "static" / "boot.js").read_text(encoding="utf-8")


def test_swipe_aborts_on_composer_input_or_keyboard_band():
    start = BOOT.index("function _onPwaSidebarSwipeStart")
    body = BOOT[start : BOOT.index("function _onPwaSidebarSwipeMove", start)]
    assert "function _swipeHitsRect" in BOOT
    assert "function _swipeInKeyboardBand" in BOOT
    assert "_swipeHitsRect(point,'composerWrap')" in body
    assert "_swipeHitsRect(point,'msg')" in body
    assert "_swipeInKeyboardBand(point)" in body


def test_ctrl_b_aborts_when_the_focused_field_is_text():
    start = BOOT.index("e.key==='b'||e.key==='B'")
    body = BOOT[start : BOOT.index("// Cmd/Ctrl+/", start)]
    assert "document.activeElement" in body
    assert "_isTextEl(active)" in body
