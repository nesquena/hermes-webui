"""Keyboard contract for the Shift+Enter send-key preference."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")


def _listener_body() -> str:
    signature = "$('msg').addEventListener('keydown',e=>"
    start = BOOT_JS.index(signature)
    brace = BOOT_JS.index("{", start)
    depth = 0
    for idx in range(brace, len(BOOT_JS)):
        char = BOOT_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return BOOT_JS[start : idx + 1]
    raise AssertionError("could not extract composer keydown listener")


def test_settings_expose_shift_enter_send_key_option():
    assert '<option value="shift+enter">Shift+Enter (Enter for newline)</option>' in INDEX_HTML
    assert '"send_key": {"enter", "ctrl+enter", "shift+enter"}' in CONFIG_PY


def test_shift_enter_mode_sends_only_with_shift_enter():
    handler = _listener_body()
    shift_branch = handler.split("if(window._sendKey==='shift+enter')", 1)[1]
    shift_branch = shift_branch.split("} else if(window._sendKey==='ctrl+enter'||_mobileDefault)", 1)[0]
    assert "if(e.shiftKey){e.preventDefault();send();}" in shift_branch
    assert "if(!e.shiftKey){e.preventDefault();send();}" not in shift_branch


def test_shift_enter_mode_leaves_plain_enter_to_textarea_when_autocomplete_open():
    handler = _listener_body()
    dropdown_enter = handler[
        handler.index("if(e.key==='Enter'&&!e.shiftKey)") :
        handler.index("// Send key: respect user preference.")
    ]
    assert "if(window._sendKey==='shift+enter')" in dropdown_enter
    guarded = dropdown_enter.split("if(window._sendKey==='shift+enter')", 1)[1].split("e.preventDefault();", 1)[0]
    assert "return;" in guarded


def test_existing_enter_and_ctrl_enter_modes_stay_available():
    handler = _listener_body()
    assert "if(!e.shiftKey){e.preventDefault();send();}" in handler
    assert "if(isNumpadEnter||e.ctrlKey||e.metaKey){e.preventDefault();send();}" in handler
