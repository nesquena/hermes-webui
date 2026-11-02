"""Clarify and inline edit follow the same Enter decision as the composer.

The composer keydown branches stay where they are, including ``_mobileDefault``.
The shared helper is only for the clarify card and the message-edit textarea.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _helper() -> str:
    start = BOOT_JS.index("function _composerShouldSendOnKey")
    end = BOOT_JS.index("window._composerShouldSendOnKey", start)
    return BOOT_JS[start:end]


def _composer_listener() -> str:
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
    raise AssertionError("composer keydown listener missing")


def test_helper_includes_mobile_default_and_not_the_touch_swallow():
    helper = _helper()
    assert "_mobileDefault" in helper
    assert "_isTouchOnlyDevice()" in helper
    assert "window._sendKey==='ctrl+enter'||_mobileDefault" in helper
    assert "lesolcoSwallowClick" not in helper
    assert "lesolcoSwallowClick" not in BOOT_JS


def test_clarify_and_edit_call_helper_composer_branches_stay():
    clarify = MESSAGES_JS[
        MESSAGES_JS.index("function showClarifyCard") :
        MESSAGES_JS.index("\nfunction ", MESSAGES_JS.index("function showClarifyCard") + 1)
    ]
    assert "window._composerShouldSendOnKey" in clarify
    edit_at = UI_JS.index("bar.querySelector('.msg-edit-send').click()")
    edit = UI_JS[max(0, edit_at - 280) : edit_at + 40]
    assert "window._composerShouldSendOnKey" in edit
    listener = _composer_listener()
    assert "_mobileDefault" in listener
    assert "if(window._sendKey==='ctrl+enter'||_mobileDefault){" in listener
    assert "_composerShouldSendOnKey" not in listener
