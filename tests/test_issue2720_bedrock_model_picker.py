"""Regression coverage for #2720: Bedrock models must appear in the WebUI model picker."""

from __future__ import annotations

import builtins

import api.config as config


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
    old_cfg_path = config._cfg_path
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
        # get_available_models() stamps config._cfg_path to the monkeypatched
        # tmp path as a side effect of its staleness check; restore it so a
        # later test's get_available_models() doesn't see path_changed=True and
        # reload config from disk over that test's in-memory cfg (isolation leak
        # that flaked test_issue3691 / test_issue1426 under cross-file ordering).
        config._cfg_path = old_cfg_path
        config.invalidate_models_cache()


def test_bedrock_in_provider_display():
    """_PROVIDER_DISPLAY must have a human-readable label for 'bedrock'."""
    assert "bedrock" in config._PROVIDER_DISPLAY, (
        "_PROVIDER_DISPLAY is missing 'bedrock' — the group header in the model picker "
        "will fall back to 'Bedrock' (title-cased id) instead of 'AWS Bedrock'"
    )
    assert config._PROVIDER_DISPLAY["bedrock"] == "AWS Bedrock"


def test_bedrock_in_provider_models():
    """_PROVIDER_MODELS must have a static fallback list for 'bedrock'."""
    assert "bedrock" in config._PROVIDER_MODELS, (
        "_PROVIDER_MODELS is missing 'bedrock' — the group builder falls to the "
        "else/auto-detected branch where an empty model list silently drops the group"
    )
    assert len(config._PROVIDER_MODELS["bedrock"]) > 0, (
        "_PROVIDER_MODELS['bedrock'] must have at least one static fallback model"
    )


def test_bedrock_static_models_have_required_fields():
    """Every static bedrock model entry must have both 'id' and 'label'."""
    for model in config._PROVIDER_MODELS["bedrock"]:
        assert "id" in model and model["id"], f"Missing id in bedrock model entry: {model}"
        assert "label" in model and model["label"], f"Missing label in bedrock model entry: {model}"


def test_bedrock_aws_credentials_detected_in_env_fallback(monkeypatch, tmp_path):
    """AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY must trigger bedrock group (no hermes_cli)."""
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
    groups = {group["provider_id"]: group for group in result["groups"]}

    assert "bedrock" in groups, (
        "bedrock group missing from model picker even with AWS_ACCESS_KEY_ID and "
        "AWS_SECRET_ACCESS_KEY set — env-var fallback path does not detect bedrock (#2720)"
    )
    assert groups["bedrock"]["provider"] == "AWS Bedrock"
    assert len(groups["bedrock"]["models"]) > 0


def test_bedrock_missing_secret_key_not_detected(monkeypatch, tmp_path):
    """Only AWS_ACCESS_KEY_ID (without the secret) must NOT trigger bedrock group."""
    _force_env_fallback(monkeypatch)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {"model": {}})
    groups = {group["provider_id"]: group for group in result["groups"]}

    assert "bedrock" not in groups, (
        "bedrock must not appear when only AWS_ACCESS_KEY_ID is set without the secret"
    )
