import importlib
import sys
from pathlib import Path


def _reload(module_name: str):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def test_config_state_dir_defaults_to_hermes_home_webui(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)

    cfg = _reload("api.config")

    assert cfg.STATE_DIR == hermes_home / "webui"


def test_profiles_resolve_base_home_unwraps_profiles_subdir(monkeypatch, tmp_path):
    profile_home = tmp_path / "hermes" / "profiles" / "webui"
    monkeypatch.delenv("HERMES_BASE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    profiles = _reload("api.profiles")

    assert profiles._resolve_base_hermes_home() == tmp_path / "hermes"
