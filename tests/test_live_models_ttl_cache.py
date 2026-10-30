"""Regression tests for /api/models/live backend TTL caching."""

import http.client
import io
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent


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
    assert first["models"] == [{"id": "openai/gpt-test", "label": "GPT Test", "supports_fast_tier": True}]


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
    assert second["models"] == [{"id": "openai/gpt-test", "label": "GPT Test", "supports_fast_tier": True}]


def test_live_models_endpoint_respects_picker_visibility_budget(monkeypatch):
    import api.config as config
    import api.routes as routes

    _install_provider_model_ids(
        monkeypatch,
        lambda provider: [f"{provider}/model-{idx}" for idx in range(40)],
    )
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    payload = routes._handle_live_models(object(), parsed)

    assert payload["count"] == config._MODEL_PICKER_VISIBLE_TARGET
    assert [m["id"] for m in payload["models"]] == [
        f"openai/model-{idx}" for idx in range(config._MODEL_PICKER_VISIBLE_TARGET)
    ]


# ── #6919: POST /api/models/refresh must invalidate the live-models cache ────

def _post_handler(body: dict):
    raw = json.dumps(body).encode()
    handler = SimpleNamespace(
        command="POST",
        rfile=io.BytesIO(raw),
        wfile=io.BytesIO(),
        headers=http.client.HTTPMessage(),
        client_address=("127.0.0.1", 12345),
        status=None,
    )
    handler.headers["Content-Length"] = str(len(raw))
    return handler


def _refresh(provider: str) -> None:
    import api.routes as routes

    routes.handle_post(_post_handler({"provider": provider}), SimpleNamespace(path="/api/models/refresh"))


def test_models_refresh_evicts_live_models_cache(monkeypatch):
    """#6919: after POST /api/models/refresh reports success, the next
    /api/models/live request must refetch — not serve the pre-refresh catalog
    from _LIVE_MODELS_CACHE."""
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed = urlparse("/api/models/live?provider=openai")
    first = routes._handle_live_models(object(), parsed)
    assert first["models"][0]["id"] == "openai/model-1"  # cache primed

    _refresh("openai")

    second = routes._handle_live_models(object(), parsed)
    assert second["models"][0]["id"] == "openai/model-2", (
        "#6919: refresh reported success but /api/models/live still served the "
        "pre-refresh cached catalog"
    )
    assert calls == ["openai", "openai"]  # the second request really refetched


def test_models_refresh_evicts_only_requested_provider(monkeypatch):
    """Provider-scoped invalidation: refreshing one provider must not evict
    another provider's live-model entry."""
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len([c for c in calls if c == provider])}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)

    parsed_a = urlparse("/api/models/live?provider=openai")
    parsed_b = urlparse("/api/models/live?provider=custom")
    routes._handle_live_models(object(), parsed_a)  # prime openai
    routes._handle_live_models(object(), parsed_b)  # prime custom
    assert calls == ["openai", "custom"]

    _refresh("openai")

    routes._handle_live_models(object(), parsed_a)  # openai refetches
    routes._handle_live_models(object(), parsed_b)  # custom must stay cached
    assert calls == ["openai", "custom", "openai"], (
        "#6919: refreshing openai also evicted custom's live-model entry"
    )


def test_models_refresh_evicts_alias_normalized_key(monkeypatch):
    """The refresh route must evict under the same normalized provider key
    /api/models/live uses ('z.ai' -> 'zai')."""
    import api.config as config
    import api.routes as routes

    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        return [f"{provider}/model-{len(calls)}"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _patch_live_models_basics(monkeypatch, routes)
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda name: {"z.ai": "zai"}.get(name, name))

    parsed = urlparse("/api/models/live?provider=z.ai")
    first = routes._handle_live_models(object(), parsed)
    assert first["models"][0]["id"] == "zai/model-1"  # primed under 'zai'

    _refresh("z.ai")

    second = routes._handle_live_models(object(), parsed)
    assert second["models"][0]["id"] == "zai/model-2", (
        "#6919: refresh with an alias did not evict the normalized cache key"
    )
    assert calls == ["zai", "zai"]


def test_live_models_finish_fences_stale_generation():
    """The in-flight writer must not repopulate _LIVE_MODELS_CACHE with
    pre-refresh bytes: _finish checks the generation captured at request
    start, and the refresh route bumps the generation."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert "def _live_models_generation" in src
    assert "def _bump_live_models_generation" in src
    m = re.search(r"def _finish\(payload: dict\):(.{0,600})", src, re.DOTALL)
    assert m is not None, "_finish closure not found in _handle_live_models"
    assert "_live_models_generation()" in m.group(1), (
        "#6919: _finish does not fence on the generation captured at request start"
    )
    assert "_bump_live_models_generation()" in src.split('if parsed.path == "/api/models/refresh":')[1][:750], (
        "#6919: the refresh route does not bump the live-models generation"
    )
