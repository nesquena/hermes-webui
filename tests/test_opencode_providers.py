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


def test_openai_codex_model_catalog_includes_gpt54():
    """openai-codex catalog must include gpt-5.4 and the standard Codex lineup."""
    assert "openai-codex" in config._PROVIDER_MODELS
    ids = [m["id"] for m in config._PROVIDER_MODELS["openai-codex"]]
    assert "gpt-5.4" in ids, f"gpt-5.4 missing from openai-codex catalog: {ids}"
    assert "gpt-5.4-mini" in ids, f"gpt-5.4-mini missing from openai-codex catalog: {ids}"
    assert "gpt-5.3-codex" in ids, f"gpt-5.3-codex missing from openai-codex catalog: {ids}"
    assert "gpt-5.2-codex" in ids, f"gpt-5.2-codex missing from openai-codex catalog: {ids}"


def test_openai_codex_display_name():
    """openai-codex must have a human-readable display name."""
    assert "openai-codex" in config._PROVIDER_DISPLAY
    assert config._PROVIDER_DISPLAY["openai-codex"] == "OpenAI Codex"


def test_live_models_handler_uses_codex_agent_path():
    """_handle_live_models for openai-codex must use get_codex_model_ids(), not the
    standard /v1/models endpoint (which returns 403 for OAuth-based Codex auth).
    Verify structurally that the routes.py handler has a dedicated codex branch.
    """
    import pathlib
    routes_src = (pathlib.Path(__file__).parent.parent / "api" / "routes.py").read_text()
    # Must have a dedicated openai-codex branch before any base_url assignment
    assert 'provider == "openai-codex"' in routes_src, (
        "_handle_live_models must have a dedicated openai-codex branch "
        "that uses get_codex_model_ids() instead of /v1/models"
    )
    # Must delegate to the agent's get_codex_model_ids
    assert "get_codex_model_ids" in routes_src, (
        "_handle_live_models must call hermes_cli.codex_models.get_codex_model_ids() "
        "for openai-codex provider"
    )
    # Must NOT route openai-codex through the standard OpenAI base URL
    # (the old bug: openai-codex was grouped with openai and sent to api.openai.com)
    codex_block_start = routes_src.find('provider == "openai-codex"')
    openai_base_url_line = routes_src.find('"https://api.openai.com/v1"', codex_block_start)
    openai_base_url_before = routes_src.find('"https://api.openai.com/v1"')
    assert openai_base_url_before > codex_block_start or openai_base_url_line == -1, (
        "openai-codex must be handled before the api.openai.com/v1 fallback, "
        "not grouped with it"
    )
