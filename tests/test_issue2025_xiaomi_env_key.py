"""Regression coverage for #2025: Xiaomi MiMo should honor XIAOMI_API_KEY."""

from __future__ import annotations

import builtins

import api.config as config
import api.onboarding as onboarding
import api.providers as providers


def _force_env_fallback(monkeypatch):
    """Force get_available_models() down its explicit env-var fallback path."""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("hermes_cli.models", "hermes_cli.auth"):
            raise ImportError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _run_available_models_with_cfg(monkeypatch, tmp_path, cfg):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path, raising=False)
    config.cfg.clear()
    config.cfg.update(cfg)
    config._cfg_mtime = 0.0
    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


def test_xiaomi_api_key_env_var_detects_model_group(monkeypatch, tmp_path):
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("XIAOMI_API_KEY", "test-xiaomi-key")

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
    groups = {group["provider_id"]: group for group in result["groups"]}

    assert "xiaomi" in groups
    assert groups["xiaomi"]["provider"] == "Xiaomi"
    assert "mimo-v2.5-pro" in {model["id"] for model in groups["xiaomi"]["models"]}


def test_xiaomi_provider_settings_detects_env_key(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("XIAOMI_API_KEY", "test-xiaomi-key")

    assert providers._PROVIDER_ENV_VAR["xiaomi"] == "XIAOMI_API_KEY"
    assert providers._provider_has_key("xiaomi") is True


def test_onboarding_lists_xiaomi_api_key_help():
    setup = onboarding._SUPPORTED_PROVIDER_SETUPS["xiaomi"]

    assert setup["env_var"] == "XIAOMI_API_KEY"
    assert setup["default_base_url"] == "https://api.xiaomimimo.com/v1"
    assert {model["id"] for model in setup["models"]} >= {"mimo-v2.5-pro"}
