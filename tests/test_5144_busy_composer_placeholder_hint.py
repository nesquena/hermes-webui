"""Regression checks for Issue #5144 busy composer placeholder hints."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = re.search(rf"(^|\n)(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    next_marker = re.search(r"\n(?:function\s+\w+\(|async\s+function\s+\w+\()", src[start + 1:])
    end = start + 1 + next_marker.start() if next_marker else len(src)
    return src[start:end]


def test_setting_defaults_off_and_bool_registration():
    assert '"show_busy_placeholder_hint": False' in CONFIG_PY
    assert '"show_busy_placeholder_hint"' in CONFIG_PY


def test_preferences_wiring_covers_busy_placeholder_hint():
    payload_block = _function_block(PANELS_JS, "_preferencesPayloadFromUi")
    assert "$('settingsShowBusyPlaceholderHint')" in payload_block
    assert "payload.show_busy_placeholder_hint=" in payload_block

    load_block = _function_block(PANELS_JS, "loadSettingsPanel")
    assert "settingsShowBusyPlaceholderHint" in load_block
    assert "show_busy_placeholder_hint" in load_block
    assert "_schedulePreferencesAutosave" in load_block
    assert "_applyBusyComposerPlaceholder" in load_block

    save_block = _function_block(PANELS_JS, "saveSettings")
    assert "const showBusyPlaceholderHint=" in save_block
    assert "body.show_busy_placeholder_hint=showBusyPlaceholderHint===true" in save_block

    autosave_block = _function_block(PANELS_JS, "_autosavePreferencesSettings")
    assert "window._showBusyPlaceholderHint" in autosave_block
    assert "_applyBusyComposerPlaceholder" in autosave_block

    apply_block = _function_block(PANELS_JS, "_applySavedSettingsUi")
    assert "showBusyPlaceholderHint" in apply_block
    assert "window._showBusyPlaceholderHint=showBusyPlaceholderHint===true" in apply_block
    assert "_applyBusyComposerPlaceholder" in apply_block


def test_boot_loads_runtime_flag_and_falls_back_false():
    assert "window._showBusyPlaceholderHint=!!s.show_busy_placeholder_hint" in BOOT_JS
    assert "window._showBusyPlaceholderHint=false" in BOOT_JS


def test_busy_placeholder_helper_preserves_compression_and_drafts():
    helper_block = _function_block(UI_JS, "_applyBusyComposerPlaceholder")
    assert "if(!input)" in helper_block
    assert "if(_compressionPlaceholderSaved!==null)" in helper_block
    assert "if(input.disabled)" in helper_block
    assert "if(_composerHasContent())" in helper_block
    assert "assistantDisplayName()" in helper_block
    assert "window._showBusyPlaceholderHint" in helper_block
    assert "window._defaultMessageMode||'steer'" in helper_block
    assert "composer_placeholder_busy_queue" in helper_block
    assert "composer_placeholder_busy_interrupt" in helper_block
    assert "composer_placeholder_busy_steer" in helper_block

    update_block = _function_block(UI_JS, "updateSendBtn")
    assert "_applyBusyComposerPlaceholder" in update_block

    busy_block = _function_block(UI_JS, "setBusy")
    assert "updateSendBtn();" in busy_block
    assert "_applyBusyComposerPlaceholder" not in busy_block


def test_locale_blocks_cover_new_keys():
    locale_blocks = I18N_JS.count("settings_default_message_mode_steer")
    assert locale_blocks == 15
    for key in [
        "settings_label_busy_placeholder_hint",
        "settings_desc_busy_placeholder_hint",
        "composer_placeholder_busy_queue",
        "composer_placeholder_busy_interrupt",
        "composer_placeholder_busy_steer",
    ]:
        assert I18N_JS.count(key) == locale_blocks, f"{key} should exist in every locale block"
