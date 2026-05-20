"""Regression tests for sidebar tab visibility feature.

Covers backend validation round-trip, frontend static contracts,
i18n coverage, and the key integration points that have broken before.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_backend_round_trip_and_validation(monkeypatch, tmp_path):
    """hidden_tabs defaults to [], saves/reloads, rejects non-list, filters empty strings."""
    import api.config as config
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["hidden_tabs"] == [], "default must be empty list"

    saved = config.save_settings({"hidden_tabs": ["kanban", "insights"]})
    assert saved["hidden_tabs"] == ["kanban", "insights"]
    assert config.load_settings()["hidden_tabs"] == ["kanban", "insights"]

    # Non-list is rejected, default preserved
    bad = config.save_settings({"hidden_tabs": "not-a-list"})
    assert bad["hidden_tabs"] == ["kanban", "insights"]

    # Empty strings filtered, empty list clears
    saved = config.save_settings({"hidden_tabs": ["kanban", "", "  ", "logs"]})
    assert saved["hidden_tabs"] == ["kanban", "logs"]
    cleared = config.save_settings({"hidden_tabs": []})
    assert cleared["hidden_tabs"] == []

    # Must NOT be in bool keys (would corrupt the list)
    assert "hidden_tabs" not in config._SETTINGS_BOOL_KEYS
    assert "hidden_tabs" in config._SETTINGS_ALLOWED_KEYS


def test_frontend_static_contracts():
    """All required HTML, JS, CSS, and boot elements exist with correct wiring."""
    # HTML: container in Appearance pane
    assert 'id="tabVisibilityChips"' in INDEX_HTML
    assert 'data-i18n="settings_label_tab_visibility"' in INDEX_HTML
    assert 'data-i18n="settings_desc_tab_visibility"' in INDEX_HTML
    appearance_start = INDEX_HTML.find('id="settingsPaneAppearance"')
    prefs_start = INDEX_HTML.find('id="settingsPanePreferences"', appearance_start + 1)
    chips_pos = INDEX_HTML.find('id="tabVisibilityChips"')
    assert appearance_start < chips_pos < prefs_start, \
        "tabVisibilityChips must be inside Appearance pane"

    # JS: constants, functions, and wiring
    assert "_ALWAYS_VISIBLE_TABS" in PANELS_JS
    assert "'chat'" in PANELS_JS.split("_ALWAYS_VISIBLE_TABS")[1][:80]
    assert "'settings'" in PANELS_JS.split("_ALWAYS_VISIBLE_TABS")[1][:80]
    assert "_HIDDEN_TABS_LS_KEY" in PANELS_JS
    assert "hermes-webui-hidden-tabs" in PANELS_JS
    for fn in ("_getHiddenTabs", "_setHiddenTabs", "_applyTabVisibility",
               "_renderTabVisibilityChips", "_toggleTabVisibilityChip"):
        assert f"function {fn}(" in PANELS_JS, f"panels.js must define {fn}()"

    # Toggle must autosave and respect always-visible tabs
    toggle_block = PANELS_JS[PANELS_JS.find("function _toggleTabVisibilityChip"):]
    toggle_body = toggle_block[:toggle_block.find("\nfunction ", 1) or 2000]
    assert "_scheduleAppearanceAutosave" in toggle_body
    assert "_ALWAYS_VISIBLE_TABS" in toggle_body

    # Appearance payload must include hidden_tabs
    payload_block = PANELS_JS[PANELS_JS.find("function _appearancePayloadFromUi"):]
    payload_body = payload_block[:payload_block.find("\nfunction ", 1) or 2000]
    assert "hidden_tabs" in payload_body
    assert "_getHiddenTabs" in payload_body

    # CSS: hidden class and chip styles
    assert ".nav-tab-hidden" in STYLE_CSS
    assert "display:none" in STYLE_CSS.split(".nav-tab-hidden")[1][:80].replace(" ", "")
    assert ".tab-visibility-chip" in STYLE_CSS

    # No flash-prevention script in <head> (DOM elements don't exist at that point)
    head_end = INDEX_HTML.find("</head>")
    assert "hermes-webui-hidden-tabs" not in INDEX_HTML[:head_end]


def test_boot_restores_visibility_from_localstorage():
    """boot.js must call _applyTabVisibility at boot time so hidden tabs take effect."""
    assert "_restoreTabVisibility" in BOOT_JS
    block = BOOT_JS[BOOT_JS.find("_restoreTabVisibility"):][:1500]
    assert "_applyTabVisibility" in block, \
        "boot.js must call _applyTabVisibility so tabs are hidden before first paint"


def test_i18n_coverage():
    """Label and description keys must exist in all locales with matching counts."""
    label_count = I18N_JS.count("settings_label_tab_visibility")
    desc_count = I18N_JS.count("settings_desc_tab_visibility")
    assert label_count >= 11, f"Expected ≥11 locales, found {label_count}"
    assert desc_count >= 11, f"Expected ≥11 locales, found {desc_count}"
    assert label_count == desc_count, \
        f"Label ({label_count}) and desc ({desc_count}) counts must match"


def test_settings_session_tracking():
    """Settings open/close lifecycle must track hidden_tabs for discard revert."""
    # _beginSettingsPanelSession snapshots hidden_tabs
    begin_block = PANELS_JS[PANELS_JS.find("function _beginSettingsPanelSession"):]
    begin_body = begin_block[:begin_block.find("\nfunction ", 1) or 2000]
    assert "_settingsHiddenTabsOnOpen" in begin_body

    # _applySavedSettingsUi updates the baseline on full save
    apply_block = PANELS_JS[PANELS_JS.find("function _applySavedSettingsUi"):]
    apply_body = apply_block[:apply_block.find("\nfunction ", 1) or 5000]
    assert "_settingsHiddenTabsOnOpen" in apply_body, \
        "_applySavedSettingsUi must update _settingsHiddenTabsOnOpen after full save"