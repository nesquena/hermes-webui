"""Regression coverage for #6160: hide entire new-chat empty-state welcome panel."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
STYLE = REPO_ROOT / "static" / "style.css"
PANELS = REPO_ROOT / "static" / "panels.js"
BOOT = REPO_ROOT / "static" / "boot.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"


def test_hide_welcome_setting_is_default_off_and_allowed():
    src = CONFIG.read_text(encoding="utf-8")
    assert '"hide_empty_state_welcome": False' in src
    assert '"hide_empty_state_welcome",' in src


def test_settings_preferences_expose_hide_welcome_toggle():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsHideEmptyWelcome"' in html
    assert 'data-i18n="settings_label_hide_empty_welcome"' in html
    assert 'data-i18n="settings_desc_hide_empty_welcome"' in html


def test_empty_state_has_hideable_welcome_css():
    css = STYLE.read_text(encoding="utf-8")
    assert ".empty-state.no-welcome{display:none!important}" in css


def test_boot_applies_saved_hide_welcome_preference():
    js = BOOT.read_text(encoding="utf-8")
    assert "function applyEmptyStateSuggestionPref()" in js
    assert "window._hideEmptyStateWelcome=s.hide_empty_state_welcome===true" in js
    assert "window._hideEmptyStateWelcome=false" in js
    assert "$('emptyState').classList.toggle('no-welcome',window._hideEmptyStateWelcome===true)" in js


def test_panels_round_trip_and_hot_apply_hide_welcome():
    js = PANELS.read_text(encoding="utf-8")
    assert "const hideEmptyWelcomeCb=$('settingsHideEmptyWelcome');" in js
    assert "payload.hide_empty_state_welcome=hideEmptyWelcomeCb.checked;" in js
    assert "hideEmptyWelcomeCb.checked=settings.hide_empty_state_welcome===true;" in js
    assert "window._hideEmptyStateWelcome=hideEmptyWelcomeCb.checked;" in js
    assert "if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();" in js


def test_hide_welcome_i18n_all_locales():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_hide_empty_welcome:") == 15
    assert js.count("settings_desc_hide_empty_welcome:") == 15
