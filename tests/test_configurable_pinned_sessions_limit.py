"""Regression checks for configurable pinned session limits."""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_pin_limit_setting_is_exposed_and_wired_through_ui():
    assert '"pinned_sessions_limit": 3' in CONFIG_PY
    assert '"pinned_sessions_limit": (1, 99)' in CONFIG_PY
    assert '"session_archive_after_days": 7' in CONFIG_PY
    assert '"session_archive_after_days": {7, 14, 30, 90}' in CONFIG_PY
    assert 'id="settingsPinnedSessionsLimit"' in INDEX_HTML
    assert 'type="number"' in INDEX_HTML
    assert 'min="1"' in INDEX_HTML
    assert 'max="99"' in INDEX_HTML
    assert 'payload.pinned_sessions_limit=parseInt(pinnedLimitField.value,10)' in PANELS_JS
    assert "settings.pinned_sessions_limit" in PANELS_JS
    assert "window._pinnedSessionsLimit=parseInt(s.pinned_sessions_limit||3,10)||3" in BOOT_JS
    assert "function _getPinnedSessionsLimit()" in SESSIONS_JS
    assert "function _pinnedSessionsLimit()" not in SESSIONS_JS
    assert "_pinnedSessionCount()>=_getPinnedSessionsLimit()" not in SESSIONS_JS
    assert "await api('/api/session/pin'" in SESSIONS_JS


def test_pinned_sessions_limit_persists_integer_and_rejects_invalid_values(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    saved = config.save_settings({"pinned_sessions_limit": 5})
    assert saved["pinned_sessions_limit"] == 5
    assert json.loads(settings_path.read_text(encoding="utf-8"))["pinned_sessions_limit"] == 5

    saved = config.save_settings({"pinned_sessions_limit": "7"})
    assert saved["pinned_sessions_limit"] == 7
    assert json.loads(settings_path.read_text(encoding="utf-8"))["pinned_sessions_limit"] == 7

    saved = config.save_settings({"pinned_sessions_limit": 0})
    assert saved["pinned_sessions_limit"] == 7
    assert json.loads(settings_path.read_text(encoding="utf-8"))["pinned_sessions_limit"] == 7

    saved = config.save_settings({"pinned_sessions_limit": 100})
    assert saved["pinned_sessions_limit"] == 7
    assert json.loads(settings_path.read_text(encoding="utf-8"))["pinned_sessions_limit"] == 7


def test_pinned_sessions_limit_and_archive_after_setting_contracts():
    import api.config as config

    assert config._SETTINGS_DEFAULTS["pinned_sessions_limit"] == 3
    assert config._SETTINGS_INT_RANGES["pinned_sessions_limit"] == (1, 99)
    assert "pinned_sessions_limit" in config._SETTINGS_ALLOWED_KEYS
    assert "pinned_sessions_limit" not in config._SETTINGS_BOOL_KEYS

    assert config._SETTINGS_DEFAULTS["session_archive_after_days"] == 7
    assert config._SETTINGS_INT_CHOICES["session_archive_after_days"] == {7, 14, 30, 90}
    assert "session_archive_after_days" in config._SETTINGS_ALLOWED_KEYS
    assert "session_archive_after_days" not in config._SETTINGS_BOOL_KEYS
