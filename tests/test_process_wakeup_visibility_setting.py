import json


def test_process_wakeup_visibility_defaults_on_and_can_be_disabled(tmp_path, monkeypatch):
    import api.config as config

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    assert config.load_settings()["show_background_wakeups"] is True

    saved = config.save_settings({"show_background_wakeups": False})

    assert saved["show_background_wakeups"] is False
    assert json.loads(settings_file.read_text(encoding="utf-8"))[
        "show_background_wakeups"
    ] is False
