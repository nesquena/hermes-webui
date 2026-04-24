"""
Tests for OpenCode Zen and OpenCode Go provider support.
Verifies provider registration in display/model catalogs and
env-var fallback detection.
"""
import os
import sys
import types
import pytest
import api.config as config


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the models TTL cache before and after every test in this file."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


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


def test_live_models_handler_delegates_to_provider_model_ids():
    """_handle_live_models must delegate to the agent's provider_model_ids()
    rather than maintain its own per-provider fetch logic.
    """
    import pathlib
    routes_src = (pathlib.Path(__file__).parent.parent / "api" / "routes.py").read_text()
    assert "provider_model_ids" in routes_src, (
        "_handle_live_models must call hermes_cli.models.provider_model_ids() "
        "to delegate all provider-specific live-fetch logic to the agent"
    )
    # The old per-provider base_url hardcoding should be gone
    assert "https://api.openai.com/v1" not in routes_src, (
        "_handle_live_models must not hardcode api.openai.com — "
        "provider resolution is handled by the agent"
    )
    assert "not_supported" not in routes_src, (
        "_handle_live_models must not return not_supported for any provider — "
        "provider_model_ids() falls back to static list automatically"
    )


def test_live_models_ui_no_longer_skips_any_provider():
    """_fetchLiveModels in ui.js must not exclude any provider from live fetching.
    Previously anthropic, google, and gemini were skipped — now provider_model_ids()
    handles them all (with graceful fallback to static lists).
    """
    import pathlib
    ui_src = (pathlib.Path(__file__).parent.parent / "static" / "ui.js").read_text()
    # The old exclusion list must be gone
    assert "includes(provider)" not in ui_src or "anthropic" not in ui_src[:ui_src.find("includes(provider)")+100], (
        "_fetchLiveModels must not skip anthropic, google, or gemini — "
        "the backend now returns live models for all providers"
    )
