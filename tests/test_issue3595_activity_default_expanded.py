"""Pin the full behavioral contract for the Activity expanded-default setting."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_setting_in_defaults():
    src = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert '"activity_feed_expanded_default"' in src or "'activity_feed_expanded_default'" in src, \
        "activity_feed_expanded_default must exist in _SETTINGS_DEFAULTS"
    # Verify default is False
    assert re.search(r'["\']activity_feed_expanded_default["\']:\s*False', src), \
        "activity_feed_expanded_default default must be False (collapsed)"


def test_setting_in_bool_keys():
    src = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert re.search(r'_SETTINGS_BOOL_KEYS\b.*?activity_feed_expanded_default', src, re.DOTALL), \
        "activity_feed_expanded_default must appear inside _SETTINGS_BOOL_KEYS (not just anywhere in config.py)"


def test_boot_initializes_window_flag():
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    assert "activityFeedExpandedDefault" in src, \
        "boot.js must initialize window._activityFeedExpandedDefault from settings"


def test_ensure_activity_group_checks_flag():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_activityFeedExpandedDefault" in src, \
        "ensureActivityGroup must check window._activityFeedExpandedDefault"


def test_per_turn_override():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert re.search(r"savedState\s*===\s*['\"]closed['\"]", src), \
        "ensureActivityGroup must check savedState==='closed' so per-turn persistence overrides the global default"


def test_collapse_class_applied():
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "tool-call-group-collapsed" in src, \
        "The collapsed class 'tool-call-group-collapsed' must be present in ui.js for the activity group"


def test_settings_checkbox_exists():
    src = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "settings_label_activity_feed_expanded_default" in src, \
        "index.html must have a settings checkbox with data-i18n for activity_feed_expanded_default"


def test_panels_wiring():
    src = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "activity_feed_expanded_default" in src, \
        "panels.js must read/write the activity_feed_expanded_default setting"


def test_i18n_keys():
    src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "settings_label_activity_feed_expanded_default" in src, \
        "i18n.js must have the label key for the setting"
    assert "settings_desc_activity_feed_expanded_default" in src, \
        "i18n.js must have the description key for the setting"
