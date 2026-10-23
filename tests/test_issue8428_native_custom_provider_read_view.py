"""Native custom providers must use the same read paths as legacy providers.

Issue #8428's field-level reproduction is pinned here: no custom_providers
key, an independently named native mapping key, api/key_env/transport,
default_model, dict model metadata, and a provider-level context length.
No Agent installation, live endpoint, or real profile is required.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
import sys
import threading
import types
from urllib.parse import urlparse

import pytest
import yaml

from api import config, profiles, providers, routes


PID = "custom:demo-key"
MODEL = "synthetic-model"
URL = "https://gateway.invalid/v1"
LABEL = "Renamed Demo Gateway"


@pytest.fixture
def native_config():
    return {
        "model": {"provider": "openai", "default": "other-model"},
        "providers": {
            PID: {
                "name": LABEL,
                "api": URL,
                "key_env": "DEMO_GATEWAY_API_KEY",
                "transport": "codex_responses",
                "default_model": MODEL,
                "models": {MODEL: {}},
                "context_length": 123456,
            },
        },
    }


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    # Stub only the external catalog/auth boundary, never the read adapter or
    # the selection/connection/runtime functions under test.
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.list_available_providers = lambda: []
    models.provider_model_ids = lambda _pid: []
    auth = types.ModuleType("hermes_cli.auth")
    auth.get_auth_status = lambda _pid: {}
    auth.read_credential_pool = lambda _pid: []
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)
    monkeypatch.setattr(routes, "j", lambda _handler, body, *a, **k: body)
    monkeypatch.setattr(routes, "_get_cached_live_models", lambda _key: None)
    monkeypatch.setattr(routes, "_set_cached_live_models", lambda *_args: None)

    def no_network(*_args, **_kwargs):
        raise AssertionError("unexpected network access")

    monkeypatch.setattr("socket.getaddrinfo", no_network)
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


def bind(monkeypatch, data):
    monkeypatch.setattr(config, "cfg", data)
    monkeypatch.setattr(config, "get_config", lambda: data)
    monkeypatch.setattr(providers, "get_config", lambda: data)
    providers.invalidate_providers_cache()


def live(provider=PID):
    return routes._handle_live_models(None, urlparse("/api/models/live?provider=" + provider))


def custom_cards():
    return [p for p in providers.get_providers()["providers"] if p["id"] == PID]


def group(catalog):
    return next(g for g in catalog["groups"] if g["provider_id"] == PID)


def test_exact_issue_shape_is_visible_without_mutation(monkeypatch, native_config):
    assert "custom_providers" not in native_config
    before = copy.deepcopy(native_config)
    bind(monkeypatch, native_config)
    entries = config._custom_provider_entries(native_config)
    assert len(entries) == 1
    assert config._custom_provider_slug_from_name(entries[0]["name"]) == PID
    assert entries[0]["base_url"] == URL
    assert entries[0]["model"] == MODEL
    assert entries[0]["context_length"] == 123456
    assert native_config == before
    assert "custom_providers" not in config.get_config()


@pytest.mark.parametrize("legacy", [None, {}, "bad", [], [None, 3]])
def test_explicit_empty_or_malformed_legacy_does_not_enable_native(legacy, native_config):
    native_config["custom_providers"] = legacy
    assert config._custom_provider_entries(native_config) == []


def test_mixed_legacy_wins_by_identity_but_unrelated_native_survives(native_config):
    legacy = {"name": "demo-key", "model": "legacy-model", "base_url": "https://legacy.invalid/v1"}
    native_config["custom_providers"] = [legacy]
    native_config["providers"]["custom:second"] = {"name": LABEL, "api": URL, "default_model": "second"}
    entries = config._custom_provider_entries(native_config)
    assert len(entries) == 2
    assert entries[0] == legacy
    assert entries[1]["model"] == "second"


@pytest.mark.parametrize("enabled", [False, "false", "OFF", "0", "no", 0, None])
def test_disabled_native_is_not_a_read_entry(enabled, native_config):
    native_config["providers"][PID]["enabled"] = enabled
    assert config._custom_provider_entries(native_config) == []


def test_malformed_native_and_builtins_are_not_custom_entries(native_config):
    native_config["providers"].update({
        "openai": {"api_key": "synthetic-built-in", "models": ["built-in"]},
        "only_configured": True,
        "custom:bad": "bad",
        "custom:empty": {},
    })
    entries = config._custom_provider_entries(native_config)
    assert len(entries) == 1
    assert entries[0]["provider_key"] == PID


@pytest.mark.parametrize("store", ["native", "legacy", "mixed"])
def test_cards_live_and_both_catalogs_share_identity(monkeypatch, native_config, store):
    if store != "native":
        native_config["custom_providers"] = [{
            "name": "demo-key", "base_url": URL, "model": MODEL,
            "models": {MODEL: {}, "model-10": {}, "model-2": {}},
            "key_env": "DEMO_GATEWAY_API_KEY",
        }]
        if store == "legacy":
            del native_config["providers"]
    else:
        native_config["providers"][PID]["models"].update({"model-10": {}, "model-2": {}})
    bind(monkeypatch, native_config)
    config._thread_ctx.env = {"DEMO_GATEWAY_API_KEY": "synthetic-owned-key"}
    before = copy.deepcopy(native_config)
    cards = custom_cards()
    assert len(cards) == 1
    assert cards[0]["is_custom"] is True
    assert cards[0]["has_key"] is True
    assert cards[0]["display_name"] == (LABEL if store == "native" else "demo-key")
    assert {m["id"] for m in cards[0]["models"]} == {MODEL, "model-2", "model-10"}
    assert {m["id"] for m in live()["models"]} == {MODEL, "model-2", "model-10"}
    # The static-first cold path and forced refresh must preserve the same
    # stable group id, user-facing label, and current natural ordering.
    cold = config._static_models_catalog_without_live_probes()
    refreshed = config.get_available_models(force_refresh=True)
    for catalog in (cold, refreshed):
        found = group(catalog)
        assert found["provider"] == (LABEL if store == "native" else "demo-key")
        assert [m["id"] for m in found["models"]] == [
            f"@{PID}:model-2", f"@{PID}:model-10", f"@{PID}:{MODEL}",
        ]
    assert native_config == before


@pytest.mark.parametrize("models,flags,expected", [
    ({MODEL: {}}, {}, [MODEL, "new-model"]),
    ({MODEL: {}}, {"models_discovered": True}, [MODEL, "new-model"]),
    ({MODEL: {}}, {"discover_models": False}, [MODEL]),
    ({MODEL: {}}, {"discover_models": "false", "models_discovered": True}, [MODEL]),
    ([MODEL], {}, [MODEL]),
    ('["synthetic-model"]', {}, [MODEL]),
    ([], {}, [MODEL, "new-model"]),
])
def test_live_allowlist_and_discovery_semantics(monkeypatch, native_config, models, flags, expected):
    native_config["providers"][PID].update({"models": models, **flags})
    bind(monkeypatch, native_config)
    config._thread_ctx.env = {"DEMO_GATEWAY_API_KEY": "synthetic-owned-key"}
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": MODEL}, {"id": "new-model"}]}).encode()

    def probe(request, **_kwargs):
        requests.append((request.full_url, request.get_header("Authorization")))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", probe)
    assert [m["id"] for m in live()["models"]] == expected
    assert requests == [(URL + "/models", "Bearer synthetic-owned-key")]


def test_reasoning_and_context_metadata(monkeypatch, native_config):
    record = native_config["providers"][PID]
    record["reasoning_efforts"] = ["low", "high"]
    record["models"][MODEL] = {"reasoning_efforts": ["medium"]}
    bind(monkeypatch, native_config)
    config._thread_ctx.env = {"DEMO_GATEWAY_API_KEY": "synthetic-owned-key"}
    assert config.resolve_model_reasoning_efforts(MODEL, PID) == ["medium"]
    assert config.resolve_model_reasoning_efforts("unlisted", PID) == ["low", "high"]
    inputs = routes._context_length_lookup_inputs_for_model(MODEL, PID, cfg=native_config)
    assert inputs.config_context_length == 123456
    assert inputs.base_url == URL
    assert inputs.api_key == "synthetic-owned-key"


def test_routing_uses_complete_original_owned_record(monkeypatch, native_config):
    record = native_config["providers"][PID]
    record.update({"api_key": "synthetic-owned-key", "credential_pool": {"source": "owned"}, "opaque_metadata": {"keep": True}})
    bind(monkeypatch, native_config)
    assert config.resolve_model_provider(MODEL) == (MODEL, PID, URL)
    assert config.resolve_model_provider(f"@{PID}:{MODEL}") == (MODEL, PID, URL)
    connection = config.resolve_custom_provider_bundle(PID)
    assert connection["source"] == "providers"
    assert connection["is_exact"] is False
    assert connection["record"]["opaque_metadata"] == {"keep": True}
    assert connection["record"]["name"] == LABEL  # not the synthetic list view
    assert connection["base_url"] == URL
    assert connection["api_key"] == "synthetic-owned-key"
    assert connection["owned"]["api_mode"] == "codex_responses"
    assert connection["owned"]["credential_pool"] == {"source": "owned"}
    runtime = {"provider": "openai", "base_url": "https://ambient.invalid/v1", "api_key": "synthetic-ambient-key", "api_mode": "anthropic_messages"}
    bundle = routes._resolve_agent_connection_bundle(PID, runtime["api_key"], runtime["base_url"], runtime)
    assert (bundle["provider"], bundle["base_url"], bundle["api_key"]) == ("custom", URL, "synthetic-owned-key")
    assert bundle["api_mode"] == "codex_responses"
    assert routes._auxiliary_main_runtime(bundle, MODEL)["api_mode"] == "codex_responses"


@pytest.mark.parametrize("bad", ["custom:ghost", "custom:"])
def test_unknown_or_malformed_route_refuses(monkeypatch, native_config, bad):
    bind(monkeypatch, native_config)
    with pytest.raises(config.CustomProviderRouteError):
        routes._resolve_agent_connection_bundle(bad, "synthetic-ambient", "https://ambient.invalid/v1", {"provider": "openai"})
    assert live(bad)["models"] == []


def test_disabled_route_refuses_without_ambient_probe(monkeypatch, native_config):
    native_config["providers"][PID]["enabled"] = False
    native_config["model"].update({"base_url": "https://ambient.invalid/v1", "api_key": "synthetic-ambient-key"})
    bind(monkeypatch, native_config)
    with pytest.raises(config.CustomProviderRouteError):
        routes._resolve_agent_connection_bundle(PID, None, None, {})
    assert live()["models"] == []
    assert not custom_cards()
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        assert not any(g["provider_id"] == PID for g in catalog["groups"])


@pytest.mark.parametrize("field", ["key_env", "api_key", "api_key_env"])
def test_native_credential_references_are_profile_secrets(tmp_path, native_config, field):
    record = native_config["providers"][PID]
    record.pop("key_env")
    record[field] = "${NATIVE_REFERENCE}" if field == "api_key" else "NATIVE_REFERENCE"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(native_config))
    assert "NATIVE_REFERENCE" in profiles._profile_secret_env_names(tmp_path)


@pytest.mark.parametrize("credential", ["key_env", "api_key"])
def test_parallel_profile_reads_never_borrow_process_credentials(monkeypatch, tmp_path, native_config, credential):
    record = native_config["providers"][PID]
    if credential == "api_key":
        record.pop("key_env")
        record["api_key"] = "${DEMO_GATEWAY_API_KEY}"
    bind(monkeypatch, native_config)
    base = tmp_path / "profiles-root"
    for name in ("has-key", "empty"):
        home = base / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(yaml.safe_dump(native_config))
    (base / "profiles/has-key/.env").write_text("DEMO_GATEWAY_API_KEY=synthetic-profile-key\n")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("DEMO_GATEWAY_API_KEY", "synthetic-process-key")
    barrier = threading.Barrier(2)
    requests = []
    request_lock = threading.Lock()

    def unavailable(request, **_kwargs):
        with request_lock:
            requests.append(request.get_header("Authorization"))
        raise OSError("isolated offline probe")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)

    def read(name):
        profiles.set_request_profile(name)
        try:
            with profiles.profile_env_for_active_request_readonly():
                barrier.wait(timeout=5)
                inputs = routes._context_length_lookup_inputs_for_model(MODEL, PID, cfg=native_config)
                connection = config.resolve_custom_provider_connection(PID)
                key = providers._get_provider_api_key(PID)
                card_key = custom_cards()[0]["has_key"]
                result = live()
                return inputs.api_key, connection[0], key, card_key, result["count"]
        finally:
            profiles.clear_request_profile()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with_key, empty = list(pool.map(read, ("has-key", "empty")))
    assert with_key == ("synthetic-profile-key", "synthetic-profile-key", "synthetic-profile-key", True, 1)
    assert empty == ("", None, None, False, 1)
    assert requests == ["Bearer synthetic-profile-key"]
    assert os.environ["DEMO_GATEWAY_API_KEY"] == "synthetic-process-key"


def test_native_pinned_model_repair_uses_session_config(native_config):
    record = native_config["providers"][PID]
    record.update({"models": [MODEL], "discover_models": False})
    assert routes._repair_removed_qualified_custom_provider_model(
        f"@{PID}:removed", PID, config_obj=native_config,
    ) == f"@{PID}:{MODEL}"
    record["models"] = {MODEL: {}}
    record.pop("discover_models")
    assert routes._repair_removed_qualified_custom_provider_model(
        f"@{PID}:removed", PID, config_obj=native_config,
    ) is None


@pytest.mark.parametrize("field", ["api", "url", "base_url"])
@pytest.mark.parametrize("native_key", [PID, "demo-key"])
def test_native_url_spellings_and_mapping_key_identity(monkeypatch, native_config, field, native_key):
    record = native_config["providers"].pop(PID)
    record.pop("api")
    record[field] = URL
    native_config["providers"][native_key] = record
    bind(monkeypatch, native_config)
    assert config.resolve_model_provider(MODEL) == (MODEL, PID, URL)
    assert config.resolve_model_provider(f"@{PID}:{MODEL}") == (MODEL, PID, URL)
    assert config.resolve_custom_provider_connection(PID) == (None, URL)
    assert len(custom_cards()) == 1
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        assert len([g for g in catalog["groups"] if g["provider_id"] == PID]) == 1
        assert not any(g["provider_id"] in {"demo-key", "custom:renamed-demo-gateway"} for g in catalog["groups"])
        assert group(catalog)["provider"] == LABEL


def test_label_rename_cannot_rekey_native_identity(monkeypatch, native_config):
    bind(monkeypatch, native_config)
    for label in (LABEL, "Another Label", "Local (127.0.0.1:1234)"):
        native_config["providers"][PID]["name"] = label
        providers.invalidate_providers_cache()
        assert custom_cards()[0]["display_name"] == label
        assert group(config._static_models_catalog_without_live_probes())["provider"] == label
        assert config.resolve_model_provider(MODEL)[1] == PID


def test_mixed_store_metadata_and_connection_use_legacy_winner(monkeypatch, native_config):
    native_config["providers"][PID].update({"api_key": "synthetic-native-key", "reasoning_efforts": ["high"]})
    legacy = {
        "name": "demo-key", "base_url": "https://legacy.invalid/v1", "model": MODEL,
        "models": {MODEL: {"context_length": 98765}}, "reasoning_efforts": ["low"],
        "transport": "anthropic_messages", "opaque_metadata": "legacy-authority",
    }
    native_config["custom_providers"] = [legacy]
    bind(monkeypatch, native_config)
    assert config.resolve_model_reasoning_efforts(MODEL, PID) == ["low"]
    assert config.resolve_model_provider(MODEL) == (MODEL, PID, legacy["base_url"])
    assert providers._get_provider_api_key(PID) is None
    assert custom_cards()[0]["has_key"] is False
    inputs = routes._context_length_lookup_inputs_for_model(MODEL, PID, cfg=native_config)
    assert (inputs.config_context_length, inputs.base_url, inputs.api_key) == (98765, legacy["base_url"], "")
    bundle = config.resolve_custom_provider_bundle(PID)
    assert bundle["source"] == "custom_providers"
    assert bundle["record"] is legacy
    assert bundle["record"]["opaque_metadata"] == "legacy-authority"
    assert bundle["base_url"] == legacy["base_url"]
    assert bundle["owned"]["api_mode"] == "anthropic_messages"
    assert bundle["keyless"] is True


def test_no_declared_key_is_keyless_not_ambient(monkeypatch, native_config):
    native_config["providers"][PID].pop("key_env")
    bind(monkeypatch, native_config)
    monkeypatch.setenv("DEMO_GATEWAY_API_KEY", "synthetic-process-key")
    monkeypatch.setenv("CUSTOM_DEMO_KEY_API_KEY", "synthetic-process-key")
    bundle = routes._resolve_agent_connection_bundle(PID, "synthetic-ambient", "https://ambient.invalid/v1", {"provider": "openai"})
    assert bundle["provider"] == "custom"
    assert bundle["base_url"] == URL
    assert bundle["api_key"] == config.KEYLESS_CUSTOM_API_KEY
    assert custom_cards()[0]["has_key"] is False


def test_native_copy_cannot_mutate_original_metadata(native_config):
    before = copy.deepcopy(native_config)
    view = config._custom_provider_entries(native_config)
    view[0]["models"][MODEL]["context_length"] = 9
    view[0]["base_url"] = "https://unowned.invalid/v1"
    assert native_config == before


def test_auxiliary_save_never_persists_synthetic_legacy_view(monkeypatch, tmp_path, native_config):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(native_config))
    bind(monkeypatch, native_config)
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    config.set_auxiliary_model("compression", PID, MODEL)
    saved = yaml.safe_load(path.read_text())
    assert "custom_providers" not in saved
    assert saved["providers"] == native_config["providers"]
    assert saved["auxiliary"]["compression"]["base_url"] == URL


def test_native_transport_bundle_preserves_acp_fields(monkeypatch, native_config):
    native_config["providers"][PID].update({
        "transport": "acp", "command": "synthetic-acp", "args": ["--isolated"],
        "api_key": "synthetic-key",
    })
    bind(monkeypatch, native_config)
    bundle = config.resolve_custom_provider_bundle(PID)
    assert bundle["record"]["command"] == "synthetic-acp"
    assert bundle["owned"]["acp_command"] == "synthetic-acp"
    assert bundle["owned"]["acp_args"] == ["--isolated"]


def test_native_probe_uses_trusted_configured_host(monkeypatch, native_config):
    # Exercise the actual catalog SSRF/probe path, not a helper replacement.
    record = native_config["providers"][PID]
    record.update({"api": "http://configured-local.invalid:8080/v1", "models": {}})
    bind(monkeypatch, native_config)
    config._thread_ctx.env = {"DEMO_GATEWAY_API_KEY": "synthetic-owned-key"}
    import socket

    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.20", 8080)),
    ])
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data": [{"id": "probed-model"}]}'

    def probe(request, **_kwargs):
        requests.append(request.full_url)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", probe)
    catalog = config.get_available_models(force_refresh=True)
    assert "@custom:demo-key:probed-model" in {m["id"] for m in group(catalog)["models"]}
    assert requests == ["http://configured-local.invalid:8080/v1/models"]


@pytest.mark.parametrize("native", [None, [], "malformed", 7])
def test_malformed_native_store_is_empty(native):
    assert config._custom_provider_entries({"providers": native}) == []


@pytest.mark.parametrize("legacy", [[], None, "malformed"])
def test_legacy_suppression_is_consistent_across_read_consumers(monkeypatch, native_config, legacy):
    native_config["custom_providers"] = legacy
    bind(monkeypatch, native_config)
    assert not custom_cards()
    assert live()["models"] == []
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        assert not any(g["provider_id"] == PID for g in catalog["groups"])


def test_same_display_label_does_not_merge_distinct_identities(native_config):
    native_config["custom_providers"] = [{"name": LABEL, "base_url": "https://legacy.invalid/v1"}]
    entries = config._custom_provider_entries(native_config)
    assert {config._custom_provider_slug_from_name(e["name"]) for e in entries} == {
        PID, "custom:renamed-demo-gateway",
    }


def test_disabled_shadowed_native_references_are_still_scrubbed(tmp_path, native_config):
    native_config["custom_providers"] = []
    native_config["providers"][PID]["enabled"] = False
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(native_config))
    assert "DEMO_GATEWAY_API_KEY" in profiles._profile_secret_env_names(tmp_path)


def test_constructor_argument_selection_uses_owned_bundle(monkeypatch, native_config):
    native_config["providers"][PID]["api_key"] = "synthetic-owned-key"
    bind(monkeypatch, native_config)
    bundle = routes._resolve_agent_connection_bundle(PID, None, None, {})

    class Constructor:
        def __init__(self, provider, base_url, api_key, api_mode=None):
            self.args = (provider, base_url, api_key, api_mode)

    instance = Constructor(
        provider=bundle["provider"], base_url=bundle["base_url"], api_key=bundle["api_key"],
        **routes._agent_bundle_kwargs(Constructor, bundle),
    )
    assert instance.args == ("custom", URL, "synthetic-owned-key", "codex_responses")


def network_catalog(monkeypatch, ids, *, failed=False):
    """Replace only the outbound HTTP boundary and retain its exact requests."""
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": mid} for mid in ids]}).encode()

    def probe(request, **_kwargs):
        requests.append((request.full_url, request.get_header("Authorization")))
        if failed:
            raise OSError("synthetic offline catalog")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", probe)
    return requests


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("caller_key", [None, "synthetic-second-key"])
def test_native_context_same_url_preserves_requested_record_authority(monkeypatch, reverse, caller_key):
    records = [
        ("custom:first", {"api": URL, "api_key": "synthetic-first-key", "models": {MODEL: {"context_length": 11111}}}),
        ("custom:second", {"api": URL, "api_key": "synthetic-second-key", "models": {MODEL: {"context_length": 22222}}}),
    ]
    data = {"model": {"provider": "custom:second", "base_url": URL}, "providers": dict(reversed(records) if reverse else records)}
    bind(monkeypatch, data)
    inputs = routes._context_length_lookup_inputs_for_model(MODEL, "custom:second", api_key=caller_key, cfg=data)
    assert (inputs.provider, inputs.base_url, inputs.api_key, inputs.config_context_length) == (
        "custom:second", URL, "synthetic-second-key", 22222,
    )
    # Metadata resolvers also receive only the selected authority, never an
    # earlier same-URL record they could independently match a second time.
    assert [entry["name"] for entry in inputs.custom_providers] == ["custom:second"]


@pytest.mark.parametrize("requested", ["custom:ghost", "custom:disabled"])
@pytest.mark.parametrize("reverse", [False, True])
def test_native_context_same_url_cannot_borrow_for_unowned_identity(monkeypatch, requested, reverse):
    records = [
        ("custom:first", {"api": URL, "api_key": "synthetic-first-key", "context_length": 11111, "models": {MODEL: {}}}),
        ("custom:disabled", {"api": URL, "api_key": "synthetic-disabled-key", "context_length": 22222, "enabled": False}),
    ]
    data = {"providers": dict(reversed(records) if reverse else records)}
    bind(monkeypatch, data)
    inputs = routes._context_length_lookup_inputs_for_model(MODEL, requested, base_url=URL, cfg=data)
    assert (inputs.provider, inputs.base_url, inputs.api_key, inputs.config_context_length) == (requested, URL, "", None)
    assert inputs.custom_providers == []


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("by", ["base", "model"])
def test_identity_absent_context_inference_requires_unique_owner(monkeypatch, count, by):
    data = {"providers": {
        f"custom:p{i}": {"api": URL, "api_key": f"synthetic-key-{i}", "models": {MODEL: {"context_length": 12345}}}
        for i in range(count)
    }}
    bind(monkeypatch, data)
    inputs = routes._context_length_lookup_inputs_for_model(MODEL, base_url=URL if by == "base" else None, cfg=data)
    if count == 1:
        assert (inputs.provider, inputs.base_url, inputs.api_key, inputs.config_context_length) == ("custom:p0", URL, "synthetic-key-0", 12345)
    else:
        assert (inputs.provider, inputs.api_key, inputs.config_context_length) == ("", "", None)
        assert inputs.custom_providers == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("bare_key", ["demo-key", "Demo Key", "Demo (Key)"])
def test_native_identity_collision_read_and_runtime_authority_agree(monkeypatch, reverse, bare_key):
    identity = config._custom_provider_slug_from_name(bare_key)
    winner_url = "https://winner.invalid/v1"
    records = [
        (bare_key, {"name": "First label", "api": "https://loser.invalid/v1", "api_key": "synthetic-first-key", "models": {"loser-model": {"context_length": 11111}}}),
        (identity, {"name": LABEL, "api": winner_url, "api_key": "synthetic-winner-key", "default_model": MODEL, "models": {MODEL: {"context_length": 22222}}, "reasoning_efforts": ["high"]}),
    ]
    data = {"model": {"provider": "openai", "default": "other-model"}, "providers": dict(reversed(records) if reverse else records)}
    bind(monkeypatch, data)
    bundle = config.resolve_custom_provider_bundle(identity)
    assert (bundle["base_url"], bundle["api_key"]) == (winner_url, "synthetic-winner-key")
    entries = config._custom_provider_entries(data)
    assert len(entries) == 1
    assert (entries[0]["base_url"], entries[0]["api_key"]) == (bundle["base_url"], bundle["api_key"])
    assert providers._get_provider_api_key(identity) == bundle["api_key"]
    cards = [card for card in providers.get_providers()["providers"] if card["id"] == identity]
    assert len(cards) == 1
    assert cards[0]["display_name"] == LABEL
    assert [m["id"] for m in cards[0]["models"]] == [MODEL]
    assert config.resolve_model_reasoning_efforts(MODEL, identity) == ["high"]
    assert config.resolve_model_provider(MODEL) == (MODEL, identity, winner_url)
    assert config.resolve_model_provider("loser-model")[1] != identity
    assert config.resolve_model_provider(f"@{identity}:{MODEL}") == (MODEL, identity, winner_url)
    inputs = routes._context_length_lookup_inputs_for_model(MODEL, identity, cfg=data)
    assert (inputs.base_url, inputs.api_key, inputs.config_context_length) == (winner_url, bundle["api_key"], 22222)
    runtime = routes._resolve_agent_connection_bundle(identity, "synthetic-ambient", "https://ambient.invalid/v1", {})
    assert (runtime["base_url"], runtime["api_key"]) == (winner_url, bundle["api_key"])
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        found = next(g for g in catalog["groups"] if g["provider_id"] == identity)
        assert [m["id"] for m in found["models"]] == [f"@{identity}:{MODEL}"]
    requests = network_catalog(monkeypatch, [MODEL])
    assert [m["id"] for m in live(identity)["models"]] == [MODEL]
    assert requests == [(winner_url + "/models", "Bearer synthetic-winner-key")]


@pytest.mark.parametrize("reverse", [False, True])
def test_native_normalization_collision_without_exact_key_fails_closed(monkeypatch, reverse):
    records = [
        ("Demo Key", {"api": "https://first.invalid/v1", "api_key": "synthetic-first-key", "models": {MODEL: {}}}),
        ("demo-key", {"api": "https://second.invalid/v1", "api_key": "synthetic-second-key", "models": {MODEL: {}}}),
    ]
    data = {"providers": dict(reversed(records) if reverse else records)}
    bind(monkeypatch, data)
    requests = network_catalog(monkeypatch, ["must-not-appear"])
    for lookup in (
        lambda: config.resolve_custom_provider_bundle(PID),
        lambda: providers._get_provider_api_key(PID),
        lambda: config.resolve_model_provider(f"@{PID}:{MODEL}"),
        lambda: routes._context_length_lookup_inputs_for_model(MODEL, PID, cfg=data),
        lambda: routes._resolve_agent_connection_bundle(PID, None, None, {}),
    ):
        with pytest.raises(config.AmbiguousCustomProviderError):
            lookup()
    assert live()["models"] == []
    # A bad identity is omitted, not a reason to make every reader unavailable.
    assert config._custom_provider_entries(data) == []
    assert not custom_cards()
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        assert not any(g["provider_id"] == PID for g in catalog["groups"])
        assert "must-not-appear" not in json.dumps(catalog)
    assert config.resolve_model_provider(MODEL)[1] != PID
    assert requests == []


@pytest.mark.parametrize("reverse", [False, True])
def test_native_exact_disabled_key_cannot_fall_through_to_enabled_alias(monkeypatch, reverse):
    records = [
        ("demo-key", {"api": URL, "api_key": "synthetic-alias-key", "models": {MODEL: {}}}),
        (PID, {"api": URL, "enabled": False}),
    ]
    data = {"providers": dict(reversed(records) if reverse else records)}
    bind(monkeypatch, data)
    assert config.resolve_custom_provider_bundle(PID)["status"] == config.CUSTOM_SELECTION_MISSING
    assert config._custom_provider_entries(data) == []
    assert providers._get_provider_api_key(PID) is None
    assert live()["models"] == []


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_legacy_precedence_survives_native_identity_collision(monkeypatch, reverse):
    records = [
        ("demo-key", {"api": "https://first.invalid/v1", "api_key": "synthetic-first-key"}),
        (PID, {"api": "https://second.invalid/v1", "api_key": "synthetic-second-key", "enabled": False}),
    ]
    legacy = {"name": "demo-key", "base_url": URL, "api_key": "synthetic-legacy-key", "models": {MODEL: {"context_length": 33333}}}
    data = {"providers": dict(reversed(records) if reverse else records), "custom_providers": [legacy]}
    bind(monkeypatch, data)
    assert config._custom_provider_entries(data) == [legacy]
    assert providers._get_provider_api_key(PID) == "synthetic-legacy-key"
    assert config.resolve_custom_provider_bundle(PID)["record"] is legacy
    inputs = routes._context_length_lookup_inputs_for_model(MODEL, PID, cfg=data)
    assert (inputs.base_url, inputs.api_key, inputs.config_context_length) == (URL, "synthetic-legacy-key", 33333)
    requests = network_catalog(monkeypatch, [MODEL])
    assert live()["count"] == 1
    assert requests == [(URL + "/models", "Bearer synthetic-legacy-key")]


@pytest.mark.parametrize("native_key", [PID, "demo-key"])
@pytest.mark.parametrize("discover", [None, True, False, "false"])
@pytest.mark.parametrize("failed", [False, True])
def test_forced_native_discovered_catalog_probes_nonactive_provider(monkeypatch, native_config, native_key, discover, failed):
    record = native_config["providers"].pop(PID)
    record.update({"api_key": "synthetic-owned-key", "models_discovered": True, "models": {MODEL: {}, "model-10": {}, "model-2": {}}})
    if discover is not None:
        record["discover_models"] = discover
    native_config["providers"][native_key] = record
    assert native_config["model"]["provider"] == "openai"
    assert not native_config["model"].get("base_url")
    bind(monkeypatch, native_config)
    config._thread_ctx.env = {"DEMO_GATEWAY_API_KEY": "synthetic-owned-key"}
    # Catalog probes validate DNS before HTTP; keep both external boundaries
    # offline without replacing the discovery or URL-validation helpers.
    import socket

    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])
    requests = network_catalog(monkeypatch, ["new-model"], failed=failed)
    found = group(config.get_available_models(force_refresh=True))
    expected = ["model-2", "model-10", MODEL]
    pinned = discover is False or discover == "false"
    if not pinned and not failed:
        expected.insert(2, "new-model")
    assert [m["id"] for m in found["models"]] == [f"@{PID}:{mid}" for mid in expected]
    assert requests == ([] if pinned else [(URL + "/models", "Bearer synthetic-owned-key")])
    # The live endpoint shares the discovery predicate; a failed probe still
    # returns every saved id. It always attempts the owned endpoint, even pinned.
    requests.clear()
    expected_live = [MODEL, "model-10", "model-2"] if failed or pinned else ["new-model"]
    assert [m["id"] for m in live()["models"]] == expected_live
    assert requests == [(URL + "/models", "Bearer synthetic-owned-key")]


@pytest.mark.parametrize("native_key", [PID, "demo-key"])
@pytest.mark.parametrize("enabled", [False, "false", "OFF", "0", "no", 0, None, True, "pool-only", "env-only"])
def test_disabled_native_live_refuses_even_with_persisted_pool(monkeypatch, native_key, enabled):
    data = {"providers": {native_key: {"name": LABEL, "key_env": "DEMO_GATEWAY_API_KEY", "models": {MODEL: {}}, "enabled": enabled}}}
    if enabled in ("pool-only", "env-only"):
        data = {}
    bind(monkeypatch, data)
    monkeypatch.setenv("DEMO_GATEWAY_API_KEY", "synthetic-process-key")
    monkeypatch.setenv("CUSTOM_DEMO_KEY_API_KEY", "synthetic-process-key")
    reads, loads, catalogs = [], [], []
    payload = {"source": "auth", "api_key": "synthetic-pool-key", "base_url": "https://pool.invalid/v1"}

    def read_pool(pid):
        reads.append(pid)
        return [] if enabled == "env-only" else [payload]

    def load_pool(pid):
        loads.append(pid)
        entry = types.SimpleNamespace(runtime_api_key="synthetic-pool-key", base_url=payload["base_url"])
        return types.SimpleNamespace(select=lambda: entry)

    def external_catalog(pid):
        catalogs.append(pid)
        return []

    monkeypatch.setattr(sys.modules["hermes_cli.auth"], "read_credential_pool", read_pool)
    monkeypatch.setattr(sys.modules["hermes_cli.models"], "provider_model_ids", external_catalog)
    pool_module = types.ModuleType("agent.credential_pool")
    pool_module.load_pool = load_pool
    monkeypatch.setitem(sys.modules, "agent.credential_pool", pool_module)
    requests = network_catalog(monkeypatch, ["pool-model"])
    # This is the real pool-presence helper, not the old globally-false mock.
    assert config._has_explicit_pool_credentials(PID) is (enabled != "env-only")
    reads.clear()
    result = live()
    allowed = enabled is True or enabled == "pool-only"
    assert result["models"] == ([{"id": "pool-model", "label": "Pool Model"}] if allowed else [])
    assert loads == ([PID] if allowed else [])
    assert requests == ([("https://pool.invalid/v1/models", "Bearer synthetic-pool-key")] if allowed else [])
    if not allowed and enabled != "env-only":
        assert reads == []
        assert catalogs == []
        assert config._custom_provider_entries(data) == []
        assert config.resolve_custom_provider_bundle(PID)["status"] == config.CUSTOM_SELECTION_MISSING


@pytest.mark.parametrize("native_key", [PID, "demo-key"])
def test_metadata_only_native_entry_remains_readable_without_claiming_connection(monkeypatch, native_key):
    data = {"model": {"provider": "openai"}, "providers": {native_key: {"name": LABEL, "models": {MODEL: {}}}}}
    bind(monkeypatch, data)
    assert [m["id"] for m in custom_cards()[0]["models"]] == [MODEL]
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        assert [m["id"] for m in group(catalog)["models"]] == [f"@{PID}:{MODEL}"]
    assert config.resolve_custom_provider_bundle(PID)["status"] == config.CUSTOM_SELECTION_MISSING


def test_bare_native_mapping_key_keeps_legacy_model_prefix_lookup(monkeypatch):
    data = {"model": {"provider": "openai"}, "providers": {"demo-key": {"api": URL, "models": {MODEL: {}}}}}
    bind(monkeypatch, data)
    model_id = f"demo-key/{MODEL}"
    # Like the legacy custom-provider prefix lane, preserve the full model id.
    assert config.resolve_model_provider(model_id) == (model_id, PID, URL)


def test_native_catalog_denied_scheme_sends_no_request(monkeypatch, native_config):
    # Configured hostnames (including private addresses) are deliberately trusted;
    # that is not permission to probe an unsupported scheme. Exercise the real
    # catalog validation and assert the HTTP boundary was never reached.
    native_config["providers"][PID].update({"api": "gopher://denied.invalid/v1", "models": {}})
    bind(monkeypatch, native_config)
    import socket

    monkeypatch.setattr("socket.getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 70)),
    ])
    requests = network_catalog(monkeypatch, ["must-not-appear"])
    catalog = config.get_available_models(force_refresh=True)
    assert requests == []
    assert "must-not-appear" not in json.dumps(catalog)
    assert [m["id"] for m in group(catalog)["models"]] == [f"@{PID}:{MODEL}"]


@pytest.fixture
def outbound_attempts(monkeypatch):
    attempts = []

    def denied(*args, **_kwargs):
        attempts.append(args)
        raise AssertionError("fixed-model catalog must not access DNS or HTTP")

    monkeypatch.setattr("socket.getaddrinfo", denied)
    monkeypatch.setattr("urllib.request.urlopen", denied)
    yield attempts
    # Catalog error handling can swallow a boundary exception: assert the spy
    # independently so a failed outbound attempt cannot masquerade as offline.
    assert attempts == []


@pytest.mark.parametrize("native_key", ["demo-key", PID])
@pytest.mark.parametrize("only_configured", [False, True])
@pytest.mark.parametrize("state", ["native", "disabled", "mixed", "empty-legacy", "bad-legacy"])
def test_only_configured_preserves_nonactive_native_group(
    monkeypatch, outbound_attempts, native_key, only_configured, state,
):
    owned_url, owned_key = "https://owned.invalid/v1", "synthetic-owned"
    data = {
        "model": {"provider": "openai", "default": "other-model"},
        "providers": {
            "only_configured": only_configured,
            native_key: {"api": owned_url, "api_key": owned_key, "models": {MODEL: {}}},
        },
    }
    if state == "disabled":
        data["providers"][native_key]["enabled"] = False
    elif state == "mixed":
        owned_url, owned_key = "https://legacy.invalid/v1", "synthetic-legacy"
        data["custom_providers"] = [{
            "name": "demo-key", "base_url": owned_url, "api_key": owned_key, "models": {MODEL: {}},
        }]
    elif state in {"empty-legacy", "bad-legacy"}:
        data["custom_providers"] = [] if state == "empty-legacy" else "malformed"
    before = copy.deepcopy(data)
    bind(monkeypatch, data)
    assert config.resolve_model_provider("@openai:gpt-5.4") == ("gpt-5.4", "openai", None)
    visible = state in {"native", "mixed"}
    assert len(custom_cards()) == int(visible)
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        ids = [g["provider_id"] for g in catalog["groups"]]
        assert ids.count(PID) == int(visible)
        assert not {"demo-key", "only-configured", "custom:only-configured"}.intersection(ids)
        assert "openai" in ids
        if visible:
            assert [m["id"] for m in group(catalog)["models"]] == [f"@{PID}:{MODEL}"]
    if state == "disabled":
        assert config.resolve_custom_provider_bundle(PID)["status"] == config.CUSTOM_SELECTION_MISSING
        with pytest.raises(config.CustomProviderRouteError):
            routes._resolve_agent_connection_bundle(PID, "synthetic-ambient", "https://ambient.invalid/v1", {})
    else:
        # Explicit legacy suppression is a presentation rule, not a rewrite of
        # the raw runtime selector. Mixed legacy still owns the complete bundle.
        bundle = config.resolve_custom_provider_bundle(PID)
        assert (bundle["base_url"], bundle["api_key"]) == (owned_url, owned_key)
        runtime = routes._resolve_agent_connection_bundle(PID, "synthetic-ambient", "https://ambient.invalid/v1", {})
        assert (runtime["base_url"], runtime["api_key"]) == (owned_url, owned_key)
    assert outbound_attempts == []
    assert data == before


@pytest.mark.parametrize("only_configured", [False, True])
def test_only_configured_keeps_legacy_only_filter_policy(monkeypatch, outbound_attempts, only_configured):
    # Preserve the pre-existing filter: a non-active legacy-only identity with
    # no corresponding providers: key is not admitted by only_configured.
    data = {
        "model": {"provider": "openai", "default": "other-model"},
        "providers": {"only_configured": only_configured},
        "custom_providers": [{"name": "demo-key", "models": {MODEL: {}}}],
    }
    bind(monkeypatch, data)
    catalog = config.get_available_models(force_refresh=True)
    assert any(g["provider_id"] == PID for g in catalog["groups"]) is not only_configured
    assert outbound_attempts == []


@pytest.fixture(params=[False, True], ids=["forward", "reverse"])
def conflicting_native_config(monkeypatch, request):
    records = [
        ("Demo Key", {"api": "https://first.invalid/v1", "api_key": "synthetic-first", "models": {"shared": {}}}),
        ("demo-key", {"api": "https://second.invalid/v1", "api_key": "synthetic-second", "models": {"shared": {}}}),
        ("safe", {"api": "https://safe.invalid/v1", "api_key": "synthetic-safe", "models": {"safe-model": {"context_length": 76543}}}),
        ("openai", {"api_key": "synthetic-openai", "models": {"gpt-5.4": {}}}),
    ]
    data = {
        "model": {"provider": "openai", "default": "gpt-5.4"},
        "providers": dict(reversed(records) if request.param else records),
    }
    bind(monkeypatch, data)
    return data


@pytest.mark.parametrize("model,expected", [
    ("@openai:gpt-5.4", ("gpt-5.4", "openai", None)),
    ("@custom:safe:safe-model", ("safe-model", "custom:safe", "https://safe.invalid/v1")),
])
def test_unrelated_native_ambiguity_does_not_poison_explicit_routes(
    outbound_attempts, conflicting_native_config, model, expected,
):
    assert config.resolve_model_provider(model) == expected
    assert outbound_attempts == []


@pytest.mark.parametrize("boundary", [
    "qualified-model", "raw-bundle", "runtime", "context", "context-with-key", "key-reader", "context-key-reader",
])
def test_requested_native_ambiguity_remains_terminal(
    outbound_attempts, conflicting_native_config, boundary,
):
    data = conflicting_native_config
    with pytest.raises(config.AmbiguousCustomProviderError):
        if boundary == "qualified-model":
            config.resolve_model_provider("@custom:demo-key:shared")
        elif boundary == "raw-bundle":
            config.resolve_custom_provider_bundle(PID)
        elif boundary == "runtime":
            routes._resolve_agent_connection_bundle(PID, "synthetic-ambient", "https://safe.invalid/v1", {})
        elif boundary == "key-reader":
            providers._get_provider_api_key(PID)
        elif boundary == "context-key-reader":
            routes._context_length_config_api_key_for_provider(PID, data)
        else:
            routes._context_length_lookup_inputs_for_model(
                "shared", PID, base_url="https://safe.invalid/v1",
                api_key="synthetic-caller" if boundary == "context-with-key" else None, cfg=data,
            )
    assert outbound_attempts == []


@pytest.mark.parametrize("target", ["custom:ghost", "custom:custom:safe", "custom:custom:demo-key"])
def test_native_ambiguity_cannot_make_unknown_or_doubled_targets_borrow(
    outbound_attempts, conflicting_native_config, target,
):
    data = conflicting_native_config
    bundle = config.resolve_custom_provider_bundle(target)
    assert bundle["status"] == config.CUSTOM_SELECTION_MISSING
    assert (bundle["base_url"], bundle["api_key"]) == (None, None)
    with pytest.raises(config.CustomProviderRouteError):
        routes._resolve_agent_connection_bundle(target, "synthetic-ambient", "https://safe.invalid/v1", {"provider": "openai"})
    inputs = routes._context_length_lookup_inputs_for_model(
        "shared", target, base_url="https://safe.invalid/v1", cfg=data,
    )
    assert (inputs.provider, inputs.api_key, inputs.config_context_length) == (target, "", None)
    assert inputs.custom_providers == []
    assert live(target)["models"] == []
    assert outbound_attempts == []


def test_native_ambiguity_isolated_from_safe_readers_and_live(monkeypatch, conflicting_native_config):
    data = conflicting_native_config
    before = copy.deepcopy(data)
    requests = network_catalog(monkeypatch, ["safe-model"])
    entries = config._custom_provider_entries(data)
    assert [config._custom_provider_slug_from_name(e["name"]) for e in entries] == ["custom:safe"]
    cards = providers.get_providers()["providers"]
    assert not any(card["id"] == PID for card in cards)
    assert next(card for card in cards if card["id"] == "custom:safe")["has_key"] is True
    assert next(card for card in cards if card["id"] == "openai")["has_key"] is True
    for catalog in (config._static_models_catalog_without_live_probes(), config.get_available_models(force_refresh=True)):
        ids = {g["provider_id"] for g in catalog["groups"]}
        assert {"openai", "custom:safe"}.issubset(ids)
        assert PID not in ids
        safe = next(g for g in catalog["groups"] if g["provider_id"] == "custom:safe")
        assert [m["id"] for m in safe["models"]] == ["@custom:safe:safe-model"]
    assert requests == []  # fixed models: neither colliding nor safe lanes probe
    inputs = routes._context_length_lookup_inputs_for_model("safe-model", "custom:safe", cfg=data)
    assert (inputs.api_key, inputs.base_url, inputs.config_context_length) == ("synthetic-safe", "https://safe.invalid/v1", 76543)
    runtime = routes._resolve_agent_connection_bundle("custom:safe", "synthetic-ambient", "https://ambient.invalid/v1", {})
    assert (runtime["base_url"], runtime["api_key"]) == ("https://safe.invalid/v1", "synthetic-safe")
    assert [m["id"] for m in live("custom:safe")["models"]] == ["safe-model"]
    assert requests == [("https://safe.invalid/v1/models", "Bearer synthetic-safe")]
    requests.clear()
    assert live(PID)["models"] == []
    assert live("openai")["provider"] == "openai"
    assert requests == []
    assert data == before
