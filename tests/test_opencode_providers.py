"""
Tests for OpenCode Zen and OpenCode Go provider support.
Verifies provider registration in display/model catalogs and
env-var fallback detection.
"""
import os
import sys
import types
import api.config as config


# ── Provider registration ─────────────────────────────────────────────

def test_opencode_zen_in_provider_display():
    assert "opencode-zen" in config._PROVIDER_DISPLAY
    assert config._PROVIDER_DISPLAY["opencode-zen"] == "OpenCode Zen"


def test_opencode_go_in_provider_display():
    assert "opencode-go" in config._PROVIDER_DISPLAY
    assert config._PROVIDER_DISPLAY["opencode-go"] == "OpenCode Go"


def test_opencode_zen_in_provider_models():
    assert "opencode-zen" in config._PROVIDER_MODELS
    ids = [m["id"] for m in config._PROVIDER_MODELS["opencode-zen"]]
    assert "claude-opus-4-6" in ids
    assert "gpt-5.4-pro" in ids
    assert "glm-5.1" in ids


def test_opencode_go_in_provider_models():
    assert "opencode-go" in config._PROVIDER_MODELS
    ids = [m["id"] for m in config._PROVIDER_MODELS["opencode-go"]]
    assert "glm-5.1" in ids
    assert "glm-5" in ids
    assert "mimo-v2-pro" in ids


# ── Env-var fallback detection ────────────────────────────────────────

def _models_with_env_key(monkeypatch, env_var, expected_provider_display):
    """Helper: fake hermes_cli unavailable, set an env var, check detection."""
    # Force the env-var fallback path by making hermes_cli import fail
    fake_mod = types.ModuleType("hermes_cli.models")
    fake_mod.list_available_providers = None  # will raise on call
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_mod)
    monkeypatch.delattr(fake_mod, "list_available_providers")

    old_cfg = dict(config.cfg)
    config.cfg["model"] = {}
    config.cfg.pop("custom_providers", None)
    monkeypatch.setenv(env_var, "test-key")
    try:
        result = config.get_available_models()
        providers = [g["provider"] for g in result["groups"]]
        assert expected_provider_display in providers, (
            f"Expected {expected_provider_display} in {providers}"
        )
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


def test_opencode_zen_detected_via_env_key(monkeypatch):
    _models_with_env_key(monkeypatch, "OPENCODE_ZEN_API_KEY", "OpenCode Zen")


def test_opencode_go_detected_via_env_key(monkeypatch):
    _models_with_env_key(monkeypatch, "OPENCODE_GO_API_KEY", "OpenCode Go")
