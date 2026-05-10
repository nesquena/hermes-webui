"""Regression tests for #1240 — WebUI model catalog should delegate to Hermes CLI.

The WebUI picker should not freeze ordinary providers to its static
``_PROVIDER_MODELS`` snapshot when Hermes CLI can return a fresher provider
catalog. Static lists remain a fallback only.
"""

from __future__ import annotations

import sys
import types

import api.config as config


_PROVIDER_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_GO_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "OLLAMA_CLOUD_API_KEY",
    "OLLAMA_API_KEY",
    "NOUS_API_KEY",
    "NVIDIA_API_KEY",
)


def _scrub_provider_env(monkeypatch):
    for name in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _install_fake_hermes_cli(monkeypatch, *, provider_id: str, live_ids, raise_on_lookup: bool = False):
    """Install a hermes_cli stub that reports one authenticated provider."""
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {"id": provider_id, "authenticated": True}
    ]

    calls: list[str] = []

    def provider_model_ids(pid):
        calls.append(pid)
        if raise_on_lookup:
            raise RuntimeError("simulated provider_model_ids failure")
        return list(live_ids) if pid == provider_id else []

    fake_models.provider_model_ids = provider_model_ids

    fake_auth = types.ModuleType("hermes_cli.auth")

    def get_auth_status(pid):
        if pid == provider_id:
            return {"logged_in": True, "key_source": ""}
        return {"logged_in": False, "key_source": ""}

    fake_auth.get_auth_status = get_auth_status

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    config.invalidate_models_cache()
    return calls


def _configure(monkeypatch, tmp_path, *, provider: str, default: str = ""):
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": provider, "default": default},
            "providers": {},
            "fallback_providers": [],
        },
    )
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    config.invalidate_models_cache()


def _provider_group(result: dict, provider_id: str) -> dict:
    return next(g for g in result["groups"] if g.get("provider_id") == provider_id)


def _ids(group: dict) -> list[str]:
    return [m.get("id") for m in group.get("models", [])]


def test_generic_provider_uses_hermes_cli_catalog_before_static_snapshot(monkeypatch, tmp_path):
    """A normal provider should show fresh CLI-discovered models.

    ``claude-sonnet-5.0`` is intentionally absent from WebUI's static Anthropic
    list. Before this fix the group came entirely from ``_PROVIDER_MODELS`` and
    this model was invisible even though Hermes CLI knew about it.
    """
    _scrub_provider_env(monkeypatch)
    calls = _install_fake_hermes_cli(
        monkeypatch,
        provider_id="anthropic",
        live_ids=["claude-opus-4.7", "claude-sonnet-5.0"],
    )
    _configure(monkeypatch, tmp_path, provider="anthropic", default="claude-opus-4.7")

    result = config.get_available_models()
    group = _provider_group(result, "anthropic")

    assert calls == ["anthropic"]
    assert _ids(group) == ["claude-opus-4.7", "claude-sonnet-5.0"]
    assert group["models"][1]["label"] == "Claude Sonnet 5.0"


def test_generic_provider_keeps_static_catalog_as_cli_failure_fallback(monkeypatch, tmp_path):
    _scrub_provider_env(monkeypatch)
    calls = _install_fake_hermes_cli(
        monkeypatch,
        provider_id="anthropic",
        live_ids=[],
        raise_on_lookup=True,
    )
    _configure(monkeypatch, tmp_path, provider="anthropic", default="claude-opus-4.7")

    result = config.get_available_models()
    group = _provider_group(result, "anthropic")

    assert calls == ["anthropic"]
    assert "claude-opus-4.7" in _ids(group)
    assert "claude-sonnet-4.6" in _ids(group)


def test_generic_provider_prefixes_live_ids_when_not_active_provider(monkeypatch, tmp_path):
    """Provider-qualified live IDs must route through the selected provider."""
    _scrub_provider_env(monkeypatch)
    calls = _install_fake_hermes_cli(
        monkeypatch,
        provider_id="anthropic",
        live_ids=["claude-sonnet-5.0"],
    )
    # Anthropic is authenticated via Hermes CLI, but OpenAI is the active
    # default. The Anthropic row still has to be pickable/routable.
    _configure(monkeypatch, tmp_path, provider="openai", default="gpt-5.5")

    result = config.get_available_models()
    group = _provider_group(result, "anthropic")

    assert "anthropic" in calls
    assert _ids(group) == ["@anthropic:claude-sonnet-5.0"]
