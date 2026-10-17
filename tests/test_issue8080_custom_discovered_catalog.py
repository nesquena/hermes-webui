"""Named discovery snapshots must not retain models removed upstream (#8080)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def catalog_scene(monkeypatch, tmp_path):
    from api import config, providers

    state = {"ids": ["model-a", "model-b"], "calls": 0, "fail": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["calls"] += 1
            if state["fail"]:
                self.send_error(503)
                return
            payload = json.dumps({"data": [{"id": mid} for mid in state["ids"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # This is the report's custom_providers + discovered metadata shape, with
    # two models rather than twelve; there is deliberately no singular model.
    entry = {
        "name": "Test Gateway",
        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "api_key": "dummy-test-key",
        "models_discovered": True,
        "models": {"model-a": {}, "model-b": {}},
    }
    cfg = {"model": {"provider": "custom:test-gateway"}, "custom_providers": [entry]}
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "8080"})
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(providers, "_PROVIDER_DISPLAY", {})
    monkeypatch.setattr(providers, "_PROVIDER_MODELS", {})
    monkeypatch.setattr(providers, "_OAUTH_PROVIDERS", frozenset())
    monkeypatch.setattr(providers, "plugin_model_provider_ids", lambda: set())
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    # Avoid host-wide proxy discovery so the only HTTP call is this local fixture.
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    config.invalidate_models_cache()
    providers.invalidate_providers_cache()
    try:
        yield config, providers, entry, state
    finally:
        config.invalidate_models_cache()
        providers.invalidate_providers_cache()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def picker_ids(config):
    group = next(g for g in config.get_available_models()["groups"]
                 if g["provider_id"] == "custom:test-gateway")
    return [m["id"] for m in group["models"] + group.get("extra_models", [])]


def card(providers):
    return next(p for p in providers.get_providers()["providers"] if p.get("is_custom"))


def test_discovered_catalog_drops_retired_id_from_picker(catalog_scene):
    config, _providers, _entry, state = catalog_scene
    state["ids"] = ["model-a", "model-new"]
    assert picker_ids(config) == ["model-a", "model-new"]
    assert state["calls"] > 0


def test_provider_card_uses_the_same_discovered_catalog(catalog_scene):
    config, providers, _entry, state = catalog_scene
    state["ids"] = ["model-a"]
    response = card(providers)
    assert [m["id"] for m in response["models"]] == ["model-a"]
    assert response["models_total"] == 1
    assert picker_ids(config) == ["model-a"]
    assert state["calls"] > 0


@pytest.mark.parametrize("active", [True, False])
def test_card_keeps_overflow_and_provider_local_colon_ids(catalog_scene, active):
    config, providers, _entry, state = catalog_scene
    if not active:
        config.cfg["model"]["provider"] = "other"
    state["ids"] = [f"model-{i}:latest" for i in range(30)]
    response = card(providers)
    group = next(g for g in config.get_available_models()["groups"]
                 if g["provider_id"] == "custom:test-gateway")
    assert group["extra_models"]
    assert response["models_total"] == 30
    assert {m["id"] for m in response["models"]} == set(state["ids"])


def test_discovered_catalog_retains_singular_sticky_default(catalog_scene):
    config, providers, entry, state = catalog_scene
    entry["model"] = "sticky-default"
    state["ids"] = ["model-a"]
    assert picker_ids(config) == ["model-a", "sticky-default"]
    assert {m["id"] for m in card(providers)["models"]} == {"model-a", "sticky-default"}


@pytest.mark.parametrize("discover_models", [False, "false"])
def test_discovery_opt_out_keeps_pin_and_does_not_probe(catalog_scene, discover_models):
    config, providers, entry, state = catalog_scene
    entry["discover_models"] = discover_models
    state["ids"] = ["model-new"]
    assert picker_ids(config) == ["model-a", "model-b"]
    assert card(providers)["models_total"] == 2
    assert state["calls"] == 0


def test_curated_catalog_remains_pinned(catalog_scene):
    config, providers, entry, state = catalog_scene
    entry.pop("models_discovered")
    state["ids"] = ["model-new"]
    assert picker_ids(config) == ["model-a", "model-b"]
    assert card(providers)["models_total"] == 2
    assert state["calls"] == 0


@pytest.mark.parametrize("failure", ["empty", "error"])
def test_unavailable_discovery_keeps_snapshot_fallback(catalog_scene, failure):
    config, providers, _entry, state = catalog_scene
    state["ids"] = []
    state["fail"] = failure == "error"
    assert picker_ids(config) == ["model-a", "model-b"]
    assert card(providers)["models_total"] == 2


def test_refresh_clears_warm_provider_card(catalog_scene, monkeypatch):
    from api import routes
    from urllib.parse import urlparse

    config, providers, _entry, state = catalog_scene
    assert card(providers)["models_total"] == 2
    state["ids"] = ["model-a"]
    monkeypatch.setattr(routes, "j", lambda _handler, payload, *args, **kwargs: payload)
    # Bind to the production refresh endpoint rather than manually clearing both caches.
    # Supply an authorized request/body at the transport seam; the refresh
    # dispatch and both cache implementations remain the production code.
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"provider": "custom:test-gateway"})
    payload = routes.handle_post(object(), urlparse("/api/models/refresh"))
    assert payload["ok"] is True
    assert card(providers)["models_total"] == 1
    assert picker_ids(config) == ["model-a"]
