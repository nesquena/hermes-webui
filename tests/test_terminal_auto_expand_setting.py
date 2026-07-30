"""Regression tests for the terminal auto-expand-on-output setting."""

import json


def test_terminal_auto_expand_on_output_defaults_disabled_and_round_trips(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["terminal_auto_expand_on_output"] is False

    saved = config.save_settings({"terminal_auto_expand_on_output": False})
    assert saved["terminal_auto_expand_on_output"] is False
    assert json.loads(settings_path.read_text(encoding="utf-8"))["terminal_auto_expand_on_output"] is False

    saved = config.save_settings({"terminal_auto_expand_on_output": True})
    assert saved["terminal_auto_expand_on_output"] is True
    assert json.loads(settings_path.read_text(encoding="utf-8"))["terminal_auto_expand_on_output"] is True


def test_terminal_auto_expand_on_output_is_a_valid_boolean_setting():
    import api.config as config

    assert "terminal_auto_expand_on_output" in config._SETTINGS_DEFAULTS
    assert "terminal_auto_expand_on_output" in config._SETTINGS_BOOL_KEYS
    assert "terminal_auto_expand_on_output" in config._SETTINGS_ALLOWED_KEYS
