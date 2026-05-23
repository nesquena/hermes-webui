"""Backend contract tests for the session archive cutoff setting."""

import json


def test_archive_after_setting_defaults_and_validation_contract():
    import api.config as config

    assert config._SETTINGS_DEFAULTS["session_archive_after_days"] == 7
    assert config._SETTINGS_INT_CHOICES["session_archive_after_days"] == {7, 14, 30, 90}
    assert "session_archive_after_days" in config._SETTINGS_ALLOWED_KEYS
    assert "session_archive_after_days" not in config._SETTINGS_BOOL_KEYS
    assert "session_archive_after_days" not in config._SETTINGS_ENUM_VALUES
    assert "session_archive_after_days" not in config._SETTINGS_INT_RANGES


def test_archive_after_setting_persists_integer_choices(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    saved = config.save_settings({"session_archive_after_days": 14})
    assert saved["session_archive_after_days"] == 14
    assert json.loads(settings_path.read_text(encoding="utf-8"))["session_archive_after_days"] == 14

    saved = config.save_settings({"session_archive_after_days": "30"})
    assert saved["session_archive_after_days"] == 30
    assert json.loads(settings_path.read_text(encoding="utf-8"))["session_archive_after_days"] == 30


def test_archive_after_setting_rejects_invalid_values_preserving_previous(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    assert config.save_settings({"session_archive_after_days": 14})["session_archive_after_days"] == 14

    for bad_value in (8, 0, "bad"):
        saved = config.save_settings({"session_archive_after_days": bad_value})
        assert saved["session_archive_after_days"] == 14
        assert json.loads(settings_path.read_text(encoding="utf-8"))["session_archive_after_days"] == 14


def test_load_settings_normalizes_invalid_archive_after_value(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"session_archive_after_days": "bad"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["session_archive_after_days"] == 7
