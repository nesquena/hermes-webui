"""Regression coverage for #2679: optional hiding of empty-chat suggestions."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
STYLE = REPO_ROOT / "static" / "style.css"
PANELS = REPO_ROOT / "static" / "panels.js"
BOOT = REPO_ROOT / "static" / "boot.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def test_hide_suggestions_setting_is_default_off_and_allowed():
    src = CONFIG.read_text(encoding="utf-8")
    assert '"hide_empty_state_suggestions": False' in src
    assert '"hide_empty_state_suggestions",' in src


def test_settings_preferences_expose_hide_suggestions_toggle():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsHideSuggestions"' in html
    assert 'data-i18n="settings_label_hide_suggestions"' in html
    assert 'data-i18n="settings_desc_hide_suggestions"' in html


def test_empty_state_has_hideable_suggestions_hook_and_css():
    html = INDEX.read_text(encoding="utf-8")
    css = STYLE.read_text(encoding="utf-8")
    assert 'class="suggestion-grid"' in html
    assert '.empty-state.no-suggestions .suggestion-grid{display:none}' in css


def test_boot_applies_saved_hide_suggestions_preference():
    js = BOOT.read_text(encoding="utf-8")
    assert "function applyEmptyStateSuggestionPref()" in js
    assert "window._hideEmptyStateSuggestions=s.hide_empty_state_suggestions===true" in js
    assert "window._hideEmptyStateSuggestions=false" in js
    assert "$('emptyState').classList.toggle('no-suggestions',window._hideEmptyStateSuggestions===true)" in js


def test_panels_round_trip_and_hot_apply_hide_suggestions():
    js = PANELS.read_text(encoding="utf-8")
    assert "const hideSuggestionsCb=$('settingsHideSuggestions');" in js
    assert "payload.hide_empty_state_suggestions=hideSuggestionsCb.checked;" in js
    assert "hideSuggestionsCb.checked=settings.hide_empty_state_suggestions===true;" in js
    assert "window._hideEmptyStateSuggestions=hideSuggestionsCb.checked;" in js
    assert "if(typeof applyEmptyStateSuggestionPref==='function') applyEmptyStateSuggestionPref();" in js


def test_hide_suggestions_i18n_all_locales_and_changelog():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_hide_suggestions:") == 13
    assert js.count("settings_desc_hide_suggestions:") == 13
    changelog = CHANGELOG.read_text(encoding="utf-8")
    assert "#2679" in changelog
    assert "hide_empty_state_suggestions" in changelog
