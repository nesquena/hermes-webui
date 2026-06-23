"""Tests for EvoLink provider registration."""

import builtins

import api.config as config


def _force_env_fallback(monkeypatch):
    """Force get_available_models() down the explicit env-var fallback path."""
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
    config.cfg.clear()
    config.cfg.update(cfg)
    config._cfg_mtime = 0.0
    try:
        config.invalidate_models_cache()
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


def test_evolink_provider_models_are_registered():
    models = config._PROVIDER_MODELS.get("evolink", [])
    ids = {m["id"] for m in models}

    assert config._PROVIDER_DISPLAY["evolink"] == "EvoLink"
    assert {"gpt-5.5", "gpt-5.5-mini"}.issubset(ids)


def test_evolink_onboarding_setup_metadata():
    from api.onboarding import _SUPPORTED_PROVIDER_SETUPS

    setup = _SUPPORTED_PROVIDER_SETUPS["evolink"]

    assert setup["label"] == "EvoLink"
    assert setup["env_var"] == "EVOLINK_API_KEY"
    assert setup["default_model"] == "gpt-5.5"
    assert setup["default_base_url"] == "https://direct.evolink.ai/v1"
    assert setup["models"]


def test_evolink_provider_key_is_configurable():
    from api.providers import _PROVIDER_ENV_VAR

    assert _PROVIDER_ENV_VAR["evolink"] == "EVOLINK_API_KEY"


def test_evolink_detected_from_env(monkeypatch, tmp_path):
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("EVOLINK_API_KEY", "test-evolink-key")

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
    groups = {g["provider_id"]: g for g in result["groups"]}

    assert "evolink" in groups
    assert groups["evolink"]["provider"] == "EvoLink"
    ids = {m["id"] for m in groups["evolink"]["models"]}
    assert {"@evolink:gpt-5.5", "@evolink:gpt-5.5-mini"}.issubset(ids)
