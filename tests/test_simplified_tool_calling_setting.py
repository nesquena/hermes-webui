"""Regression tests for the deprecated Simplified tool calling setting."""

import json


def test_simplified_tool_calling_defaults_enabled_but_legacy_false_is_ignored(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["simplified_tool_calling"] is True

    saved = config.save_settings({"simplified_tool_calling": False})
    assert saved["simplified_tool_calling"] is True
    assert json.loads(settings_path.read_text(encoding="utf-8"))["simplified_tool_calling"] is True

    settings_path.write_text(
        json.dumps({"simplified_tool_calling": False}),
        encoding="utf-8",
    )
    loaded = config.load_settings()
    assert loaded["simplified_tool_calling"] is True


def test_simplified_tool_calling_is_legacy_compatibility_not_user_setting():
    import api.config as config

    assert "simplified_tool_calling" in config._SETTINGS_DEFAULTS
    assert "simplified_tool_calling" in config._SETTINGS_LEGACY_DROP_KEYS
    assert "simplified_tool_calling" not in config._SETTINGS_BOOL_KEYS
    assert "simplified_tool_calling" not in config._SETTINGS_ALLOWED_KEYS
