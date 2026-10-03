"""Named providers without allowlists discover their own endpoint catalog."""
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from api import config, profiles, providers, routes


@pytest.fixture
def catalog(monkeypatch, tmp_path, request):
    requests = []
    model_ids = getattr(request, "param", ["auto", "org/local:fast"])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"data": [{"id": model} for model in model_ids]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda _pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {"key_source": "none"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(providers, "_provider_has_key", lambda _pid: False)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models.json")
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0)
    monkeypatch.setattr(config, "cfg", {
        "model": {"provider": "openai-codex", "default": "gpt-5.5", "api_key": "other-provider-secret"},
        "providers": {"local_router": {"base_url": endpoint, "default_model": "retired-model"}},
    })
    config.invalidate_models_cache()
    routes._clear_live_models_cache()
    yield endpoint, requests
    routes._clear_live_models_cache()
    config.invalidate_models_cache()
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.mark.parametrize("active", ["openai-codex", "local_router", None])
def test_named_provider_catalog_and_selection(catalog, active):
    endpoint, requests = catalog
    config.cfg["model"] = {"provider": active, "default": "auto" if active == "local_router" else ""}
    result = config.get_available_models()
    groups = {group["provider_id"]: group for group in result["groups"]}
    models = groups["local-router"]["models"]
    assert {row["id"] for row in models} == {"@local-router:auto", "@local-router:org/local:fast"}
    for row in models:
        wire_model = row["id"].split(":", 1)[1]
        assert config.resolve_model_provider(row["id"]) == (wire_model, "local_router", endpoint)
    assert requests == [("/v1/models", None)]


def test_named_provider_live_refresh(catalog, monkeypatch):
    endpoint, requests = catalog
    monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)
    result = routes._handle_live_models(None, urlparse("/api/models/live?provider=local-router"))
    assert {row["id"] for row in result["models"]} == {"@local-router:auto", "@local-router:org/local:fast"}
    for row in result["models"]:
        selected = config.model_with_provider_context(row["id"], result["provider"])
        assert config.resolve_model_provider(selected) == (row["id"].split(":", 1)[1], "local_router", endpoint)
    assert requests == [("/v1/models", None)]


@pytest.mark.parametrize("key_field", ["key_env", "api_key_env", "api_key"])
def test_named_provider_uses_request_profile_key(catalog, monkeypatch, tmp_path, key_field):
    _endpoint, requests = catalog
    base = tmp_path / ".hermes"
    for name in ("work", "personal"):
        profile_home = base / "profiles" / name
        profile_home.mkdir(parents=True)
        (profile_home / ".env").write_text(f"LOCAL_ROUTER_KEY={name}-key\n")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("LOCAL_ROUTER_KEY", "process-secret")
    config.cfg["providers"]["local_router"][key_field] = (
        "${LOCAL_ROUTER_KEY}" if key_field == "api_key" else "LOCAL_ROUTER_KEY"
    )
    try:
        for name in ("work", "personal"):
            profiles.set_request_profile(name)
            with profiles.profile_env_for_active_request_readonly("test"):
                assert config._read_live_provider_model_ids("local-router") == ["auto", "org/local:fast"]
    finally:
        profiles.clear_request_profile()
    assert requests == [
        ("/v1/models", "Bearer work-key"),
        ("/v1/models", "Bearer personal-key"),
    ]


def test_named_provider_allowlist_and_offline_default(catalog, monkeypatch):
    _endpoint, requests = catalog
    entry = config.cfg["providers"]["local_router"]
    entry["models"] = ["curated-a", "curated-b"]
    assert config._read_live_provider_model_ids("local-router") == ["curated-a", "curated-b"]
    assert requests == []
    del entry["models"]
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")))
    assert config._read_live_provider_model_ids("local-router") == ["retired-model"]



@pytest.mark.parametrize("discover", [False, "false", "no", "0"])
def test_named_provider_discovery_opt_out(catalog, discover):
    _endpoint, requests = catalog
    config.cfg["providers"]["local_router"]["discover_models"] = discover
    assert config._read_live_provider_model_ids("local-router") == ["retired-model"]
    assert requests == []


@pytest.mark.parametrize("legacy_cache", [None, 3, 4])
def test_named_provider_survives_slow_catalog_rebuild(catalog, monkeypatch, legacy_cache):
    _endpoint, requests = catalog
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.01)
    if legacy_cache:
        config._save_models_cache_to_disk({
            "active_provider": "openai-codex", "default_model": "gpt-5.5",
            "configured_model_badges": {},
            "groups": [{"provider": "OpenAI Codex", "provider_id": "openai-codex",
                        "models": [{"id": "gpt-5.5", "label": "GPT"}]}],
            "aliases": {},
        })
        cache_path = config._get_models_cache_path()
        saved = json.loads(cache_path.read_text())
        # The released catalog schema omitted named endpoints on this path.
        saved["_schema_version"] = legacy_cache
        cache_path.write_text(json.dumps(saved))

    release_probe = threading.Event()
    def slow_provider_discovery():
        release_probe.wait()
        return []
    monkeypatch.setattr(sys.modules["hermes_cli.models"], "list_available_providers", slow_provider_discovery)
    try:
        result = config.get_available_models()
        groups = {group["provider_id"]: group for group in result["groups"]}
        assert [row["id"] for row in groups["local-router"]["models"]] == ["@local-router:retired-model"]
        assert requests == []
    finally:
        release_probe.set()
        with config._cache_build_cv:
            assert config._cache_build_cv.wait_for(lambda: not config._cache_build_in_progress, timeout=5)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("active", ["openai-codex", "local_router"])
@pytest.mark.parametrize("active_base_url", [False, True])
def test_ambiguous_named_provider_identity_is_rejected(catalog, monkeypatch, reverse, active, active_base_url):
    endpoint, requests = catalog
    entries = [
        ("local_router", {"base_url": endpoint, "api_key": "first-key", "models": ["only-first"]}),
        ("local-router", {"base_url": endpoint.replace("/v1", "/second/v1"), "api_key": "second-key"}),
    ]
    config.cfg["providers"] = dict(reversed(entries) if reverse else entries)
    config.cfg["providers"]["safe_router"] = {"base_url": endpoint.replace("/v1", "/safe/v1"), "api_key": "safe-key", "default_model": "auto"}
    config.cfg["model"] = {"provider": active, "default": "only-first" if active == "local_router" else "gpt-5.5"}
    if active_base_url and active == "local_router":
        config.cfg["model"]["base_url"] = endpoint
    full = config.get_available_models()
    static = config._static_models_catalog_without_live_probes()
    for result in (full, static):
        groups = {group["provider_id"]: group for group in result["groups"]}
        assert not ({"local-router", "local_router"} & set(groups))
        assert "safe-router" in groups
    assert requests == [("/safe/v1/models", "Bearer safe-key")]

    monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)
    for spelling in ("local_router", "local-router", "LOCAL_ROUTER"):
        with pytest.raises(config.AmbiguousCustomProviderError, match="Rename"):
            config._read_live_provider_model_ids(spelling)
        with pytest.raises(config.AmbiguousCustomProviderError, match="Rename"):
            config.resolve_model_provider(f"@{spelling}:org/local:fast")
        with pytest.raises(config.AmbiguousCustomProviderError, match="Rename"):
            config.model_with_provider_context("org/local:fast", spelling)
        live = routes._handle_live_models(None, urlparse(f"/api/models/live?provider={spelling}"))
        assert live["models"] == []
        assert "Rename" in live["error"]
    with pytest.raises(config.AmbiguousCustomProviderError, match="Rename"):
        config.resolve_model_provider("only-first")
    assert requests == [("/safe/v1/models", "Bearer safe-key")]


def test_static_named_slash_default_routes_from_emitted_id(catalog):
    endpoint, requests = catalog
    config.cfg["providers"]["local_router"]["default_model"] = "org/local:fast"
    result = config._static_models_catalog_without_live_probes()
    group = next(group for group in result["groups"] if group["provider_id"] == "local-router")
    assert len(group["models"]) == 1
    assert config.resolve_model_provider(group["models"][0]["id"]) == ("org/local:fast", "local_router", endpoint)
    assert requests == []


@pytest.mark.parametrize("catalog", [["@openai-api:local-only", "@local-router:auto", "org/local:fast"]], indirect=True)
@pytest.mark.parametrize("active", ["openai-codex", "local_router"])
def test_named_opaque_ids_survive_picker_and_persisted_selection(catalog, monkeypatch, active):
    endpoint, _requests = catalog
    wire_ids = {"@openai-api:local-only", "@local-router:auto", "org/local:fast"}
    config.cfg["model"] = {"provider": active, "default": "@openai-api:local-only" if active == "local_router" else "gpt-5.5"}
    config.cfg["providers"]["local_router"]["api_key"] = "local-key"
    result = config.get_available_models()
    group = next(group for group in result["groups"] if group["provider_id"] == "local-router")
    monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)
    live = routes._handle_live_models(None, urlparse("/api/models/live?provider=local-router"))
    for rows in (group["models"], live["models"]):
        assert {row["id"] for row in rows} == {f"@local-router:{model}" for model in wire_ids}
        for row in rows:
            wire_model = row["id"].split(":", 1)[1]
            # Session ingress preserves the picker identity. Reuse precisely
            # that stored pair for the next turn, as streaming does.
            stored_model, stored_provider = routes._session_model_state_from_request(row["id"], "local-router")
            assert stored_model == row["id"]
            for _turn in range(2):
                selected = config.model_with_provider_context(stored_model, stored_provider)
                assert config.resolve_model_provider(selected) == (wire_model, "local_router", endpoint)
    for wire_model in wire_ids:
        config.cfg["providers"]["local_router"]["models"] = [wire_model]
        static = config._static_models_catalog_without_live_probes()
        row = next(group for group in static["groups"] if group["provider_id"] == "local-router")["models"][0]
        assert config.resolve_model_provider(row["id"]) == (wire_model, "local_router", endpoint)


def test_restored_raw_named_model_keeps_explicit_owner(catalog):
    endpoint, _requests = catalog
    # Restored dropdown options can submit data-model separately from provider.
    model, provider = routes._session_model_state_from_request("@openai-api:local-only", "local-router")
    assert model == "@local-router:@openai-api:local-only"
    assert config.resolve_model_provider(config.model_with_provider_context(model, provider)) == (
        "@openai-api:local-only", "local_router", endpoint,
    )


def test_minimal_fallback_rejects_ambiguous_active_provider(catalog):
    endpoint, requests = catalog
    config.cfg["model"] = {"provider": "local_router", "default": "auto"}
    config.cfg["providers"]["local-router"] = {"base_url": endpoint.replace("/v1", "/second/v1")}
    assert config._minimal_static_models_catalog()["groups"] == []
    assert requests == []


@pytest.mark.parametrize("catalog_path", ["minimal", "static", "full"])
def test_named_default_keeps_opaque_model_owner(catalog, catalog_path):
    endpoint, requests = catalog
    config.cfg["model"] = {"provider": "local_router", "default": "@openai-api:local-only"}
    builders = {"minimal": config._minimal_static_models_catalog, "static": config._static_models_catalog_without_live_probes, "full": config.get_available_models}
    result = builders[catalog_path]()
    rows = [row for group in result["groups"] for row in group["models"]]
    row = next(row for row in rows if "local-only" in row["id"])
    assert config.resolve_model_provider(row["id"]) == ("@openai-api:local-only", "local_router", endpoint)
    assert len(requests) == (1 if catalog_path == "full" else 0)
