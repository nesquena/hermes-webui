"""Send button long-press action picker contract.

The primary send button is compact, but while an agent is running users need
Codex-like access to queue/steer/interrupt without typing slash commands.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_send_button_action_menu_markup_exists():
    assert 'id="sendActionMenu"' in INDEX
    assert 'aria-label="Send action menu"' in INDEX
    for action in ("send", "queue", "steer", "interrupt", "stop"):
        assert f'data-send-action="{action}"' in INDEX


def test_send_button_action_menu_has_long_press_and_contextmenu_handlers():
    assert "function openSendActionMenu" in UI_JS
    assert "function closeSendActionMenu" in UI_JS
    assert "function handleSendActionMenuPick" in UI_JS
    assert "SEND_ACTION_LONG_PRESS_MS" in BOOT_JS
    assert "btn.addEventListener('pointerdown'" in BOOT_JS
    assert "btn.addEventListener('contextmenu'" in BOOT_JS
    assert "openSendActionMenu({source:'longpress'}" in BOOT_JS


def test_send_action_picker_executes_explicit_composer_action():
    assert "async function executeComposerAction" in UI_JS
    assert "handleSendActionMenuPick(action)" in UI_JS
    assert "await executeComposerAction(action)" in UI_JS
    assert "return send({forceAction:action})" in UI_JS


def test_send_function_accepts_forced_busy_action():
    assert "async function send(options={})" in (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    messages_js = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "const forcedAction=options.forceAction" in messages_js
    assert "forcedAction==='steer'" in messages_js
    assert "forcedAction==='interrupt'" in messages_js
    assert "forcedAction==='queue'" in messages_js


def test_send_action_menu_css_is_present_and_accessible():
    assert ".send-action-menu" in STYLE
    assert ".send-action-menu.open" in STYLE
    assert ".send-action-menu-item" in STYLE
    assert "aria-hidden" in INDEX
    assert "role=\"menu\"" in INDEX
    assert "role=\"menuitem\"" in INDEX


def test_busy_action_picker_markup_is_visible_contract():
    assert 'id="busyActionPicker"' in INDEX
    assert 'role="group"' in INDEX
    assert 'aria-label="Action while Hermes is working"' in INDEX
    for action in ("queue", "steer", "interrupt"):
        assert f'data-busy-action="{action}"' in INDEX


def test_busy_action_picker_sends_with_explicit_action():
    assert "function updateBusyActionPicker" in UI_JS
    assert "async function handleBusyActionPickerPick" in UI_JS
    assert "await executeComposerAction(action)" in UI_JS
    assert "handleBusyActionPickerPick(btn.dataset.busyAction)" in UI_JS


def test_busy_action_picker_only_shows_while_busy():
    assert "const shouldShow=!!isBusy" in UI_JS
    assert "picker.classList.toggle('visible',shouldShow)" in UI_JS
    assert "picker.setAttribute('aria-hidden',shouldShow?'false':'true')" in UI_JS
    assert "updateBusyActionPicker(action)" in UI_JS


def test_busy_action_picker_css_is_present():
    assert ".busy-action-picker" in STYLE
    assert ".busy-action-picker.visible" in STYLE
    assert ".busy-action-option" in STYLE
    assert ".busy-action-option.active" in STYLE
