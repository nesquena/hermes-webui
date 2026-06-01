"""Keyboard contract for treating Numpad Enter as a submit shortcut."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def test_boot_defines_numpad_enter_helper():
    assert "function _isNumpadEnter(e)" in BOOT_JS
    assert "e.code==='NumpadEnter'" in BOOT_JS
    assert "e.location===KeyboardEvent.DOM_KEY_LOCATION_NUMPAD" in BOOT_JS


def test_ctrl_enter_mode_allows_numpad_enter_to_submit():
    ctrl_branch = BOOT_JS.split("if(window._sendKey==='ctrl+enter'||_mobileDefault){", 1)[1]
    ctrl_branch = ctrl_branch.split("} else {", 1)[0]
    assert "isNumpadEnter" in ctrl_branch
    assert "if(isNumpadEnter||e.ctrlKey||e.metaKey){e.preventDefault();send();}" in ctrl_branch


def test_ime_guard_runs_before_numpad_enter_detection():
    enter_branch = BOOT_JS.split("if(e.key==='Enter'){")[-1]
    ime_idx = enter_branch.index("if(_isImeEnter(e)){return;}")
    numpad_idx = enter_branch.index("const isNumpadEnter=_isNumpadEnter(e);")
    assert ime_idx < numpad_idx
