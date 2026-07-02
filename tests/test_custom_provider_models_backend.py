"""Tests for set_custom_provider_models() — backend model allowlist management.

Verifies:
1. Dict-form models round-trip correctly (dict keys → UI → save preserves dict keys)
2. List-form models work
3. Empty models list removes the field
4. Unknown provider returns error
5. get_providers() surfaces dict-form models (read path)
"""
import os
import sys
import types
from pathlib import Path

import pytest
import yaml

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _make_config_yaml(tmp_path, content: dict) -> str:
    """Write a config.yaml and return its path. Also patches config to use it."""
    path = tmp_path / "config.yaml"
    with open(path, "w") as f:
        yaml.dump(content, f)
    return str(path)


@pytest.fixture(autouse=True)
def _reset(request, monkeypatch):
    """Reset config state before and after each test."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = os.environ.get("HERMES_HOME")
    yield
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    if old_path is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = old_path


class TestSetCustomProviderModels:
    """Backend tests for set_custom_provider_models()."""

    def _setup(self, monkeypatch, tmp_path, cfg_content):
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        cfg_path = _make_config_yaml(tmp_path, cfg_content)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Also patch _get_config_path to return our temp file so
        # set_custom_provider_models can read/write it directly
        monkeypatch.setattr(config, "_get_config_path", lambda: Path(cfg_path))
        config._cfg_mtime = 0.0
        config.cfg["custom_providers"] = cfg_content.get("custom_providers", [])
        config.cfg["model"] = cfg_content.get("model", {})
        return cfg_path

    def test_dict_form_round_trip(self, monkeypatch, tmp_path):
        """Dict-form models should survive a save round-trip: read keys → save subset → re-read preserves dict keys + metadata."""
        self._setup(monkeypatch, tmp_path, {
            "model": {"provider": "custom:testgw"},
            "custom_providers": [
                {
                    "name": "testgw",
                    "base_url": "http://gw:8080/v1",
                    "models": {
                        "model-a": {"context_length": 32000},
                        "model-b": {"context_length": 64000, "label": "Big model"},
                        "model-c": {},
                    },
                }
            ],
        })

        from api.providers import set_custom_provider_models, get_providers

        # Step 1: get_providers should surface all 3 dict keys
        result = get_providers()
        gw = [p for p in result["providers"] if p["id"] == "custom:testgw"][0]
        assert gw["models_total"] == 3
        model_ids = {m["id"] for m in gw["models"]}
        assert model_ids == {"model-a", "model-b", "model-c"}, f"Got: {model_ids}"

        # Step 2: save a subset (remove model-c)
        save_result = set_custom_provider_models("custom:testgw", ["model-a", "model-b"])
        assert save_result["ok"] is True, f"Save failed: {save_result.get('error')}"

        # Step 3: re-read config.yaml directly — dict keys must be preserved
        cfg_path = config._get_config_path()
        with open(cfg_path) as f:
            raw = yaml.safe_load(f)
        cp = raw["custom_providers"][0]
        assert isinstance(cp["models"], dict), f"Expected dict, got {type(cp['models'])}"
        assert list(cp["models"].keys()) == ["model-a", "model-b"]
        # Metadata must be preserved for model-a and model-b
        assert cp["models"]["model-a"] == {"context_length": 32000}
        assert cp["models"]["model-b"] == {"context_length": 64000, "label": "Big model"}

    def test_list_form_round_trip(self, monkeypatch, tmp_path):
        """List-form models should also round-trip correctly."""
        self._setup(monkeypatch, tmp_path, {
            "model": {"provider": "custom:testgw"},
            "custom_providers": [
                {
                    "name": "testgw",
                    "base_url": "http://gw:8080/v1",
                    "models": ["model-x", "model-y", "model-z"],
                }
            ],
        })

        from api.providers import set_custom_provider_models

        # Save a subset
        save_result = set_custom_provider_models("custom:testgw", ["model-x", "model-z"])
        assert save_result["ok"] is True

        cfg_path = config._get_config_path()
        with open(cfg_path) as f:
            raw = yaml.safe_load(f)
        cp = raw["custom_providers"][0]
        assert isinstance(cp["models"], list), f"Expected list, got {type(cp['models'])}"
        assert cp["models"] == ["model-x", "model-z"]

    def test_empty_models_removes_field(self, monkeypatch, tmp_path):
        """Saving an empty models list should remove the models field entirely (restoring show-all behaviour)."""
        self._setup(monkeypatch, tmp_path, {
            "model": {"provider": "custom:testgw"},
            "custom_providers": [
                {
                    "name": "testgw",
                    "base_url": "http://gw:8080/v1",
                    "models": ["model-a", "model-b"],
                }
            ],
        })

        from api.providers import set_custom_provider_models

        save_result = set_custom_provider_models("custom:testgw", [])
        assert save_result["ok"] is True

        cfg_path = config._get_config_path()
        with open(cfg_path) as f:
            raw = yaml.safe_load(f)
        cp = raw["custom_providers"][0]
        assert "models" not in cp, "models field should be removed when empty list is saved"

    def test_unknown_provider_returns_error(self, monkeypatch, tmp_path):
        """Calling set_custom_provider_models with a non-existent provider should return error."""
        self._setup(monkeypatch, tmp_path, {
            "model": {"provider": "custom:testgw"},
            "custom_providers": [{"name": "realgw", "base_url": "http://gw:8080/v1"}],
        })

        from api.providers import set_custom_provider_models

        result = set_custom_provider_models("custom:nonexistent", ["model-x"])
        assert result["ok"] is False
        assert "not found" in result.get("error", "")

    def test_get_providers_shows_dict_models(self, monkeypatch, tmp_path):
        """get_providers() must surface dict-form models (the read-path fix)."""
        self._setup(monkeypatch, tmp_path, {
            "model": {"provider": "custom:testgw"},
            "custom_providers": [
                {
                    "name": "testgw",
                    "base_url": "http://gw:8080/v1",
                    "models": {
                        "fast-model": {"context_length": 16000},
                        "big-model": {"context_length": 128000},
                    },
                }
            ],
        })

        from api.providers import get_providers

        result = get_providers()
        gw = [p for p in result["providers"] if p["id"] == "custom:testgw"]
        assert len(gw) == 1, f"testgw not found: {[p['id'] for p in result['providers']]}"
        model_ids = {m["id"] for m in gw[0]["models"]}
        assert model_ids == {"fast-model", "big-model"}, f"Got: {model_ids}"
        assert gw[0]["models_total"] == 2
