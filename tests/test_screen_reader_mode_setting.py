"""Regression tests for the opt-in screen reader mode setting.

The setting is a general accessibility switch. Its first consumer is the
composer terminal, where it enables xterm.js ``screenReaderMode``.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_JS = (ROOT / "static" / "terminal.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_screen_reader_mode_defaults_disabled_and_round_trips(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    assert config.load_settings()["screen_reader_mode"] is False

    saved = config.save_settings({"screen_reader_mode": True})
    assert saved["screen_reader_mode"] is True
    assert json.loads(settings_path.read_text(encoding="utf-8"))["screen_reader_mode"] is True

    saved = config.save_settings({"screen_reader_mode": False})
    assert saved["screen_reader_mode"] is False
    assert json.loads(settings_path.read_text(encoding="utf-8"))["screen_reader_mode"] is False


def test_screen_reader_mode_is_a_valid_boolean_setting():
    import api.config as config

    assert config._SETTINGS_DEFAULTS["screen_reader_mode"] is False
    assert "screen_reader_mode" in config._SETTINGS_BOOL_KEYS
    assert "screen_reader_mode" in config._SETTINGS_ALLOWED_KEYS


def test_new_terminal_is_created_with_the_current_screen_reader_mode():
    ctor = TERMINAL_JS[TERMINAL_JS.index("new window.Terminal({"):]
    ctor = ctor[: ctor.index("});")]
    assert "screenReaderMode:!!window._screenReaderMode" in ctor


def _function_source(source, name):
    match = re.search(r"function " + name + r"\([^)]*\)\{.*?\n\}\n", source, re.S)
    assert match, f"{name}() must exist"
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_setter_updates_the_flag_and_an_existing_terminal_live():
    script = (
        "const window={};\n"
        "const TERMINAL_UI={term:null};\n"
        + _function_source(TERMINAL_JS, "applyTerminalScreenReaderMode")
        + _function_source(UI_JS, "setScreenReaderMode")
        + """
const results=[];
setScreenReaderMode(1);  // no terminal yet: must not throw
results.push(window._screenReaderMode);
TERMINAL_UI.term={options:{}};
setScreenReaderMode(true);
results.push(TERMINAL_UI.term.options.screenReaderMode);
setScreenReaderMode(undefined);
results.push(window._screenReaderMode, TERMINAL_UI.term.options.screenReaderMode);
console.log(JSON.stringify(results));
"""
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out) == [True, True, False, False]


def test_every_runtime_path_goes_through_the_setter():
    assert "setScreenReaderMode(s.screen_reader_mode)" in BOOT_JS
    # Preferences panel: hydrate, then the saved value after each autosave.
    # A change waits for the server so an older in-flight autosave cannot
    # flip the mode back after the box was checked.
    assert "setScreenReaderMode(screenReaderModeCb.checked);" in PANELS_JS
    assert "screenReaderModeCb.addEventListener('change',_schedulePreferencesAutosave" in PANELS_JS
    assert "payload.screen_reader_mode=screenReaderModeCb.checked;" in PANELS_JS
    assert "setScreenReaderMode(saved&&saved.screen_reader_mode);" in PANELS_JS
    # Full Settings save.
    assert "body.screen_reader_mode=!!($('settingsScreenReaderMode')||{}).checked;" in PANELS_JS
    assert "setScreenReaderMode(saved&&saved.screen_reader_mode!==undefined?saved.screen_reader_mode:body.screen_reader_mode);" in PANELS_JS
    # Nobody writes the flag around the setter.
    for name, source in (("boot.js", BOOT_JS), ("panels.js", PANELS_JS), ("terminal.js", TERMINAL_JS)):
        assert "window._screenReaderMode=" not in source, name


def test_settings_checkbox_is_labelled_described_and_translated():
    assert 'id="settingsScreenReaderMode"' in INDEX_HTML
    assert 'aria-describedby="settingsScreenReaderModeDesc"' in INDEX_HTML
    assert 'id="settingsScreenReaderModeDesc"' in INDEX_HTML
    locales = len(re.findall(r"settings_label_terminal_auto_expand:", I18N_JS))
    assert I18N_JS.count("settings_label_screen_reader_mode:") == locales
    assert I18N_JS.count("settings_desc_screen_reader_mode:") == locales


def test_settings_search_finds_the_row_by_accessibility_terms():
    """Settings search reads data-settings-search, so people looking for "accessibility" or a screen reader's name
    find the row (the label alone only matches "screen reader")."""
    row = re.search(
        r'<div class="settings-field" data-settings-search="([^"]*)">\s*<label[^>]*>\s*'
        r'<input type="checkbox" id="settingsScreenReaderMode"',
        INDEX_HTML,
    )
    assert row, "the Screen reader mode row must carry data-settings-search keywords"
    terms = row.group(1).lower().split()
    for term in ("accessibility", "a11y", "nvda", "voiceover", "terminal"):
        assert term in terms, term
    assert "settingsSearch" in PANELS_JS, "Settings search must still read data-settings-search"
