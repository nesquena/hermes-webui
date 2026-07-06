"""Regression coverage for Neuralwatt provider env-var mapping.

Mirrors test_issue2025_xiaomi_env_key.py — ensures the provider ID maps to
the correct env var and that key detection works when the env var is set.
"""

from __future__ import annotations

import builtins

import api.config as config
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
    old_path = getattr(config, "_cfg_path", None)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path, raising=False)
    config.cfg.clear()
    config.cfg.update(cfg)
    config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()


def test_neuralwatt_env_var_mapping():
    """Neuralwatt maps to NEURALWATT_API_KEY in the provider env-var table."""
    assert providers._PROVIDER_ENV_VAR["neuralwatt"] == "NEURALWATT_API_KEY"


def test_neuralwatt_provider_has_key_when_env_set(monkeypatch, tmp_path):
    """Key detection returns True when NEURALWATT_API_KEY is in the environment."""
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("NEURALWATT_API_KEY", "test-neuralwatt-key")

    assert providers._provider_has_key("neuralwatt") is True


def test_neuralwatt_provider_has_key_false_without_env(monkeypatch, tmp_path):
    """Key detection returns False when NEURALWATT_API_KEY is not set."""
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("NEURALWATT_API_KEY", raising=False)

    assert providers._provider_has_key("neuralwatt") is False


def test_neuralwatt_model_group_appears_with_models_in_config(monkeypatch, tmp_path):
    """Neuralwatt appears as a provider group when config.yaml lists its models."""
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("NEURALWATT_API_KEY", "test-neuralwatt-key")

    result = _run_available_models_with_cfg(
        monkeypatch,
        tmp_path,
        {
            "model": {"default": "glm-5.2", "provider": "neuralwatt"},
            "providers": {
                "neuralwatt": {
                    "base_url": "https://api.neuralwatt.com/v1",
                    "key_env": "NEURALWATT_API_KEY",
                    "api_mode": "chat_completions",
                    "default_model": "glm-5.2",
                    "models": ["glm-5.2", "glm-5.2-short"],
                }
            },
        },
    )

    groups = {group["provider_id"]: group for group in result["groups"]}
    assert "neuralwatt" in groups, f"neuralwatt missing from groups: {list(groups.keys())}"
    assert groups["neuralwatt"]["provider"] == "Neuralwatt"
    model_ids = {model["id"] for model in groups["neuralwatt"]["models"]}
    assert "glm-5.2" in model_ids
    assert "glm-5.2-short" in model_ids
