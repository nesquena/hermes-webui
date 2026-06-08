"""Regression tests for /api/models/live backend TTL caching."""

import sys
import types
from urllib.parse import urlparse


def _install_provider_model_ids(monkeypatch, fn):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _patch_live_models_basics(monkeypatch, routes, profile="default"):
    import api.config as config
    import api.profiles as profiles

    routes._clear_live_models_cache()
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(config, "get_config", lambda: {"model": {"provider": "openai"}})
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: profile)


def test_live_models_cache_hits_within_ttl(monkeypatch):
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return ["openai/gpt-test"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    second = routes._handle_live_models(object(), parsed)

    assert calls == ["openai"]
    assert first == second
    assert first["models"] == [{"id": "openai/gpt-test", "label": "GPT Test"}]


def test_live_models_cache_expires(monkeypatch):
    import api.routes as routes

    now = [1000.0]
    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    now[0] += routes._LIVE_MODELS_CACHE_TTL + 1
    second = routes._handle_live_models(object(), parsed)

    assert calls == ["openai", "openai"]
    assert first["models"][0]["id"] == "openai/model-1"
    assert second["models"][0]["id"] == "openai/model-2"


def test_live_models_cache_is_profile_scoped(monkeypatch):
    import api.routes as routes
    import api.profiles as profiles

    active_profile = ["default"]
    calls = []

    def provider_model_ids(provider):
        calls.append((active_profile[0], provider))
        return [f"{provider}/{active_profile[0]}-model"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active_profile[0])

    parsed = urlparse("/api/models/live?provider=openai")
    default_payload = routes._handle_live_models(object(), parsed)
    active_profile[0] = "research"
    research_payload = routes._handle_live_models(object(), parsed)
    again_payload = routes._handle_live_models(object(), parsed)

    assert calls == [("default", "openai"), ("research", "openai")]
    assert default_payload["models"][0]["id"] == "openai/default-model"
    assert research_payload["models"][0]["id"] == "openai/research-model"
    assert again_payload == research_payload


def test_live_models_cache_returns_deep_copies(monkeypatch):
    import api.routes as routes

    _install_provider_model_ids(monkeypatch, lambda provider: ["openai/gpt-test"])
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    first["models"].clear()
    first["provider"] = "mutated"

    second = routes._handle_live_models(object(), parsed)

    assert second["provider"] == "openai"
    assert second["models"] == [{"id": "openai/gpt-test", "label": "GPT Test"}]
