"""Regression coverage for #3717 provider-scoped context-length overrides."""

import sys
import types
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def _install_fake_context_resolver(monkeypatch):
    calls = []
    mod = types.ModuleType("agent.model_metadata")

    def _fake(
        model,
        base_url="",
        *args,
        config_context_length=None,
        provider="",
        custom_providers=None,
        **kwargs,
    ):
        calls.append(
            {
                "model": model,
                "base_url": base_url,
                "config_context_length": config_context_length,
                "provider": provider,
                "custom_providers": custom_providers,
            }
        )
        return config_context_length or 256000

    mod.get_model_context_length = _fake
    if "agent" not in sys.modules:
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", mod)
    return calls


def test_route_resolver_uses_provider_model_context_length(monkeypatch):
    import api.config as config
    import api.routes as routes

    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.example/v1",
                    "models": {
                        "provider-model": {"context_length": 777000},
                    },
                },
            },
            "custom_providers": [],
        },
    )

    result = routes._resolve_context_length_for_session_model(
        "provider-model",
        "openrouter",
    )

    assert result == 777000
    assert calls[-1]["config_context_length"] == 777000
    assert calls[-1]["base_url"] == "https://openrouter.example/v1"
    assert calls[-1]["provider"] == "openrouter"


def test_route_resolver_uses_provider_model_context_length_without_base_url(monkeypatch):
    import api.config as config
    import api.routes as routes

    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "providers": {
                "anthropic": {
                    "models": {
                        "provider-model": {"context_length": 777000},
                    },
                },
            },
            "custom_providers": [],
        },
    )

    result = routes._resolve_context_length_for_session_model(
        "provider-model",
        "anthropic",
    )

    assert result == 777000
    assert calls[-1]["config_context_length"] == 777000
    assert calls[-1]["base_url"] == ""
    assert calls[-1]["provider"] == "anthropic"


def test_route_resolver_uses_named_custom_provider_base_url(monkeypatch):
    import api.config as config
    import api.routes as routes

    custom_providers = [
        {
            "name": "ZenMux",
            "base_url": "https://zenmux.example/v1",
            "models": {
                "custom-model": {"context_length": "888000"},
            },
        }
    ]
    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "custom_providers": custom_providers,
        },
    )

    result = routes._resolve_context_length_for_session_model(
        "custom-model",
        "custom:zenmux",
    )

    assert result == 888000
    assert calls[-1]["config_context_length"] == 888000
    assert calls[-1]["base_url"] == "https://zenmux.example/v1"
    assert calls[-1]["provider"] == "custom:zenmux"
    assert calls[-1]["custom_providers"] is custom_providers


def test_global_context_length_remains_default_model_only(monkeypatch):
    import api.config as config
    import api.routes as routes

    calls = _install_fake_context_resolver(monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "default-model",
                "context_length": 123456,
            },
            "providers": {
                "openai": {
                    "base_url": "https://openai.example/v1",
                    "models": {},
                },
            },
        },
    )

    result = routes._resolve_context_length_for_session_model("other-model", "openai")

    assert result == 256000
    assert calls[-1]["config_context_length"] is None
    assert calls[-1]["base_url"] == "https://openai.example/v1"


def test_streaming_fallbacks_use_shared_provider_context_helper():
    assert STREAMING_PY.count("_context_length_lookup_inputs_for_model(") >= 2
    assert "_cfg_base_url = getattr(agent, 'base_url', '') or resolved_base_url or ''" in STREAMING_PY
    assert "base_url=_cfg_base_url" in STREAMING_PY
    assert "config_context_length=_cfg_ctx_len" in STREAMING_PY
    assert "provider=_cfg_provider" in STREAMING_PY
    assert "custom_providers=_cfg_custom_providers" in STREAMING_PY
    assert "_cfg_base_url" in STREAMING_PY


def test_route_helper_keeps_all_context_length_sources_aligned():
    assert "def _context_length_lookup_inputs_for_model" in ROUTES_PY
    # Hardened against an explicit null ``providers:`` key (salvage of #3967):
    # ``cfg.get("providers") or {}`` instead of ``cfg.get("providers", {})`` so
    # a config.yaml with ``providers:`` (None) degrades to an empty mapping.
    assert 'cfg.get("providers") or {}' in ROUTES_PY
    assert 'cfg.get("custom_providers")' in ROUTES_PY
    assert "_model_matches_configured_default" in ROUTES_PY
