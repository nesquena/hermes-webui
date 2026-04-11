"""
Sprint 34 tests: Hermes control center should reset to Conversation on close.
"""

import pathlib


REPO = pathlib.Path(__file__).parent.parent


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


def test_settings_panel_has_explicit_reset_helper():
    src = read("static/panels.js")
    assert "function _resetSettingsPanelState()" in src
    assert "_settingsSection = 'conversation';" in src
    assert "switchSettingsSection('conversation');" in src


def test_all_real_settings_close_paths_use_shared_hide_helper():
    src = read("static/panels.js")
    assert "function _hideSettingsPanel()" in src
    assert src.count("_hideSettingsPanel();") >= 4
    assert "$('settingsOverlay').style.display='none';" not in src
    assert "$('settingsOverlay').style.display = 'none';" not in src
