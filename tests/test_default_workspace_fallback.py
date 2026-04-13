import json
from pathlib import Path

import api.config as config


def test_resolve_default_workspace_falls_back_to_existing_home_work(monkeypatch, tmp_path):
    preferred = tmp_path / "work"
    preferred.mkdir()
    state_dir = tmp_path / "state"

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    resolved = config.resolve_default_workspace("/definitely/not/usable")

    assert resolved == preferred.resolve()



def test_save_settings_rewrites_bad_default_workspace_to_fallback(monkeypatch, tmp_path):
    preferred = tmp_path / "work"
    preferred.mkdir()
    state_dir = tmp_path / "state"
    settings_file = tmp_path / "settings.json"

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", preferred)

    saved = config.save_settings({"default_workspace": "/definitely/not/usable"})
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))

    assert saved["default_workspace"] == str(preferred.resolve())
    assert on_disk["default_workspace"] == str(preferred.resolve())
