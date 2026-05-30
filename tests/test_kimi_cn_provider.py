"""Regression tests for Kimi China provider visibility in the model picker."""

import builtins

import api.config as config
from api.providers import _PROVIDER_ENV_VAR


def _force_env_fallback(monkeypatch):
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


def test_kimi_coding_cn_has_static_catalog():
    assert config._PROVIDER_DISPLAY.get("kimi-coding-cn") == "Kimi / Moonshot (China)"
    ids = [m["id"] for m in config._PROVIDER_MODELS.get("kimi-coding-cn", [])]
    assert "kimi-k2.6" in ids


def test_kimi_cn_api_key_detects_china_provider_only(monkeypatch, tmp_path):
    _force_env_fallback(monkeypatch)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_CN_API_KEY", "test-cn-key")

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
    groups = {g["provider_id"]: g for g in result["groups"]}

    assert "kimi-coding-cn" in groups, f"kimi-coding-cn group missing: {groups.keys()}"
    assert groups["kimi-coding-cn"]["provider"] == "Kimi / Moonshot (China)"
    assert "kimi-k2.6" in {m["id"] for m in groups["kimi-coding-cn"]["models"]}
    assert "kimi-coding" not in groups


def test_kimi_coding_cn_active_provider_gets_picker_group(monkeypatch, tmp_path):
    _force_env_fallback(monkeypatch)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_CN_API_KEY", raising=False)

    result = _run_available_models_with_cfg(
        monkeypatch,
        tmp_path,
        {
            "model": {"provider": "kimi-coding-cn", "default": "kimi-k2.6"},
            "providers": {},
        },
    )
    groups = {g["provider_id"]: g for g in result["groups"]}

    assert result["active_provider"] == "kimi-coding-cn"
    assert result["default_model"] == "kimi-k2.6"
    assert "kimi-coding-cn" in groups


def test_kimi_coding_cn_key_can_be_managed_from_provider_settings():
    assert _PROVIDER_ENV_VAR.get("kimi-coding-cn") == "KIMI_CN_API_KEY"
