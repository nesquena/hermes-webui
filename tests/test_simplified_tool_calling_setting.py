"""Regression tests for the Simplified tool calling setting."""

import json


def test_simplified_tool_calling_defaults_enabled_and_round_trips(
    monkeypatch, tmp_path
):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["simplified_tool_calling"] is True

    saved = config.save_settings({"simplified_tool_calling": False})
    assert saved["simplified_tool_calling"] is False
    assert (
        json.loads(settings_path.read_text(encoding="utf-8"))["simplified_tool_calling"]
        is False
    )

    saved = config.save_settings({"simplified_tool_calling": True})
    assert saved["simplified_tool_calling"] is True


def test_simplified_tool_calling_is_a_valid_boolean_setting():
    import api.config as config

    assert "simplified_tool_calling" in config._SETTINGS_DEFAULTS
    assert "simplified_tool_calling" in config._SETTINGS_BOOL_KEYS
    assert "simplified_tool_calling" in config._SETTINGS_ALLOWED_KEYS
