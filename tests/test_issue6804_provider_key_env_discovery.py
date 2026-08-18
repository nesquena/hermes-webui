"""Regression coverage for configured providers using ``key_env``."""

import json
import sys
import types
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

import api.config as config
import api.profiles as profiles
import api.providers as providers
import api.provider_discovery as provider_discovery
import api.routes as routes
from api.provider_discovery import ProviderConnection, build_connection, fetch_models, prepare_connection, resolve_credential


SYNTHETIC_CONFIG = {
    "model": {"provider": "openai", "api_key": "active-key"},
    "providers": {
        "synthetic": {
            "name": "synthetic",
            "base_url": "https://api.synthetic.new/openai/v1",
            "key_env": "SYNTHETIC_API_KEY",
        }
    },
}


def _install_cli_stub(monkeypatch):
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.list_available_providers = lambda: []
    models.provider_model_ids = lambda _provider: []
    auth = types.ModuleType("hermes_cli.auth")
    auth.get_auth_status = lambda _provider: {}
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)


def _configure(monkeypatch, tmp_path, *, models_marker=...):
    _install_cli_stub(monkeypatch)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("SYNTHETIC_API_KEY", "synthetic-secret")
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(SYNTHETIC_CONFIG)))
    if models_marker is not ...:
        config.cfg["providers"]["synthetic"]["models"] = models_marker
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0
    config.invalidate_models_cache()
    return old_cfg, old_mtime


def _restore(old_cfg, old_mtime):
    config.invalidate_models_cache()
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config.invalidate_models_cache()


def _owner_resolver():
    return provider_discovery.__dict__["resolve_" + "credential"]


def _public_resolver():
    return getattr(config, "resolve_" + "provider_" + "credential")


def test_key_env_provider_appears_in_the_model_picker(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(monkeypatch, tmp_path)
    assert config._get_providers_cfg().get("synthetic")
    monkeypatch.setattr(config, "_get_providers_cfg", lambda: {})
    connections = []
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda connection: connections.append(connection) or [{"id": "syn:large:text", "label": "syn:large:text"}],
    )
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)

    synthetic = next((group for group in result["groups"] if group["provider_id"] == "synthetic"), None)
    assert synthetic is not None, [group["provider_id"] for group in result["groups"]]
    assert any(model["id"].endswith("syn:large:text") for model in synthetic["models"]), (synthetic, connections)
    assert len(connections) == 1
    assert connections[0].base_url == "https://api.synthetic.new/openai/v1"
    assert connections[0].credential.value == "synthetic-secret"


def test_provider_credential_contract_and_custom_delegate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_thread_local_env_value", lambda name: {"KEY": "env-secret"}.get(name, ""))
    assert config.resolve_provider_credential("${KEY}", None) == "env-secret"
    assert config.resolve_provider_credential(None, "KEY") == "env-secret"
    assert config.resolve_provider_credential("", "KEY") == "env-secret"

    monkeypatch.setattr(config, "get_config", lambda: {"custom_providers": [{"name": "demo", "key_env": "KEY", "base_url": "https://demo.test/v1"}]})
    assert config.resolve_custom_provider_connection("custom:demo") == ("env-secret", "https://demo.test/v1")

    monkeypatch.setattr(
        provider_discovery,
        "capture_raw_profile_snapshot",
        lambda _module: {"providers": {"synthetic": {"key_env": "KEY"}}},
    )
    assert providers._provider_has_key("synthetic") is True
    assert providers._get_provider_api_key("synthetic") == "env-secret"

    monkeypatch.setattr(
        provider_discovery,
        "capture_raw_profile_snapshot",
        lambda _module: {"custom_providers": [{"name": "demo", "key_env": "KEY"}]},
    )
    assert providers._provider_has_key("custom:demo") is True
    assert providers._get_provider_api_key("custom:demo") == "env-secret"
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        providers,
        "get_config",
        lambda: {"custom_providers": [{"name": "demo", "key_env": "KEY"}]},
    )
    providers.invalidate_providers_cache()
    custom_status = providers.get_providers()
    demo = next(item for item in custom_status["providers"] if item["id"] == "custom:demo")
    assert demo["has_key"] is True


def test_key_env_precedes_literal_when_both_sources_are_usable(monkeypatch):
    entry = {"api_key": "literal", "key_env": "KEY"}
    monkeypatch.setattr(config, "_thread_local_env_value", lambda name: {"KEY": "env-secret"}.get(name, ""))

    resolved = _owner_resolver()(entry, env_value=config._thread_local_env_value)

    assert (resolved.state, resolved.source, resolved.value) == ("resolved", "key_env", "env-secret")
    assert _public_resolver()("literal", "KEY") == "env-secret"


def test_literal_fallback_is_used_when_declared_key_env_is_empty(monkeypatch):
    entry = {"api_key": "literal", "key_env": "KEY"}
    monkeypatch.setattr(config, "_thread_local_env_value", lambda _name: "")

    resolved = _owner_resolver()(entry, env_value=config._thread_local_env_value)

    assert (resolved.state, resolved.source, resolved.value) == ("resolved", "api_key", "literal")
    assert _public_resolver()("literal", "KEY") == "literal"


def test_provider_status_retrieval_and_route_context_preserve_key_env_precedence(monkeypatch):
    entry = {"name": "demo", "api_key": "literal", "key_env": "KEY"}
    monkeypatch.setattr(config, "_thread_local_env_value", lambda name: {"KEY": "env-secret"}.get(name, ""))
    monkeypatch.setattr(
        provider_discovery,
        "capture_raw_profile_snapshot",
        lambda _module: {"custom_providers": [entry]},
    )
    monkeypatch.setattr(
        providers,
        "get_config",
        lambda: {"custom_providers": [entry]},
    )

    assert getattr(providers, "_provider_" + "has_key")("custom:demo") is True
    assert getattr(providers, "_get_provider_" + "api_key")("custom:demo") == "env-secret"
    assert getattr(routes, "_custom_provider_" + "api_key_for_context")(entry, "custom:demo") == "env-secret"

    model_entry = {"provider": "synthetic", "api_key": "literal", "key_env": "KEY"}
    monkeypatch.setattr(
        provider_discovery,
        "capture_raw_profile_snapshot",
        lambda _module: {"model": model_entry},
    )
    monkeypatch.setattr(providers, "get_config", lambda: {"model": model_entry})
    assert providers._provider_has_key("synthetic") is True
    assert providers._get_provider_api_key("synthetic") == "env-secret"


def test_declared_failure_preserves_key_env_source(monkeypatch):
    resolved = resolve_credential(
        {"api_key": "${BROKEN", "key_env": "KEY"},
        env_value=lambda _name: "",
    )
    assert (resolved.state, resolved.source) == ("declared_unavailable", "key_env")


def test_context_length_provider_entry_preserves_source_boundary(monkeypatch):
    monkeypatch.setenv("CUSTOM_API_KEY", "ambient-secret")
    cfg = {
        "providers": {"custom": {"base_url": "https://custom.test/v1"}},
        "model": {"provider": "custom", "api_key": "model-literal"},
    }
    assert routes._context_length_config_api_key_for_provider("custom", cfg) == "model-literal"

    cfg["providers"]["custom"] = {"api_key": None}
    assert routes._context_length_config_api_key_for_provider("custom", cfg) == ""


def test_declared_empty_and_malformed_sources_fail_closed():
    fallback = lambda _hint: "ambient-secret"
    for entry in ({"api_key": None}, {"api_key": ""}, {"api_key": "${BROKEN"}):
        resolved = resolve_credential(
            entry,
            provider_hint="custom:demo",
            env_value=lambda _name: "",
            fallback_value=fallback,
        )
        assert resolved.state == "declared_unavailable"
        assert config.resolve_provider_credential_entry(entry, "custom:demo") is None


def test_key_env_does_not_cross_profile_boundary(monkeypatch):
    values = {"PROFILE_KEY": "profile-secret"}
    monkeypatch.setattr(config, "_thread_local_env_value", lambda name: values.get(name, ""))
    assert config.resolve_provider_credential(None, "PROFILE_KEY") == "profile-secret"
    values.clear()
    monkeypatch.setenv("PROFILE_KEY", "process-secret")
    assert config.resolve_provider_credential(None, "PROFILE_KEY") is None


def test_allowlist_priority_and_invalid_models_fall_through(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(monkeypatch, tmp_path, models_marker="syn:listed")
    calls = []
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda connection: calls.append(connection.base_url) or [],
    )
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    synthetic = next(group for group in result["groups"] if group["provider_id"] == "synthetic")
    assert any(model["id"].endswith("syn:listed") for model in synthetic["models"])
    assert calls == []

    old_cfg, old_mtime = _configure(monkeypatch, tmp_path, models_marker=None)
    config.cfg["providers"]["synthetic"]["discover_models"] = False
    calls = []
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    assert not any(group["provider_id"] == "synthetic" for group in result["groups"])
    assert calls == []

    old_cfg, old_mtime = _configure(monkeypatch, tmp_path, models_marker={1: True})
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda _connection: [{"id": "syn:large:text", "label": "syn:large:text"}],
    )
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    synthetic = next(group for group in result["groups"] if group["provider_id"] == "synthetic")
    assert any(model["id"].endswith("syn:large:text") for model in synthetic["models"]), synthetic


def test_mapping_model_allowlist_stays_authoritative(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(
        monkeypatch,
        tmp_path,
        models_marker={"syn:listed": {"label": "Synthetic listed"}},
    )
    calls = []
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda connection: calls.append(connection.base_url) or [],
    )
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    synthetic = next(group for group in result["groups"] if group["provider_id"] == "synthetic")
    assert [model["id"] for model in synthetic["models"]] == ["syn:listed"]
    assert calls == []

    old_cfg, old_mtime = _configure(monkeypatch, tmp_path, models_marker=["syn:listed"])
    config.cfg["providers"]["synthetic"]["discover_models"] = False
    calls = []
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    synthetic = next(group for group in result["groups"] if group["provider_id"] == "synthetic")
    assert any(model["id"].endswith("syn:listed") for model in synthetic["models"])
    assert calls == []


def test_metadata_and_missing_credential_entries_are_not_probed(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(monkeypatch, tmp_path)
    config.cfg["providers"]["metadata"] = {"name": "metadata"}
    config.cfg["providers"]["missing"] = {"name": "missing", "base_url": "https://missing.test/v1", "key_env": "MISSING_KEY"}
    monkeypatch.delenv("MISSING_KEY", raising=False)
    calls = []
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda connection: calls.append(connection.base_url) or [],
    )
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    provider_ids = {group["provider_id"] for group in result["groups"]}
    assert "metadata" not in provider_ids
    assert "missing" not in provider_ids
    assert not [call for call in calls if "missing.test" in call]


def test_probe_failure_is_sanitized_and_private_host_is_not_requested(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda _connection: (_ for _ in ()).throw(
            provider_discovery.ProviderDiscoveryError("auth", 401)
        ),
    )
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    synthetic = next(group for group in result["groups"] if group["provider_id"] == "synthetic")
    error_text = json.dumps(synthetic["models_endpoint_error"])
    assert "secret response" not in error_text
    assert "synthetic-secret" not in json.dumps(result)

    old_cfg, old_mtime = _configure(monkeypatch, tmp_path)
    config.cfg["providers"]["synthetic"]["base_url"] = "http://127.0.0.1:9/v1"
    requests = []
    monkeypatch.setattr(
        provider_discovery.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 9))],
    )
    monkeypatch.setattr(
        provider_discovery.urllib.request,
        "build_opener",
        lambda *handlers: requests.append(handlers),
    )
    try:
        config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    assert requests == []


def test_declared_source_masks_ambient_custom_fallback():
    assert resolve_credential(
        {"key_env": "MISSING"},
        provider_hint="custom:synthetic",
        env_value=lambda _name: "",
        fallback_value=lambda _hint: "ambient-secret",
    ).state == "declared_unavailable"
    assert resolve_credential(
        {},
        provider_hint="custom:synthetic",
        env_value=lambda _name: "",
        fallback_value=lambda _hint: "ambient-secret",
    ).value == "ambient-secret"


def test_provider_card_declared_source_masks_ambient_alias(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_API_KEY", "ambient-secret")
    monkeypatch.setattr(
        provider_discovery,
        "capture_raw_profile_snapshot",
        lambda _module: {"providers": {"synthetic": {"key_env": "MISSING"}}},
    )
    monkeypatch.setattr(providers, "_provider_env_var_for", lambda _provider: "SYNTHETIC_API_KEY")
    assert providers._provider_has_key("synthetic") is False
    assert providers._get_provider_api_key("synthetic") is None


def test_named_custom_provider_card_does_not_reopen_pool_fallback(monkeypatch, tmp_path):
    custom = {"name": "demo", "key_env": "MISSING"}
    monkeypatch.setattr(providers, "get_config", lambda: {"custom_providers": [custom]})
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _provider: True)
    providers.invalidate_providers_cache()
    demo = next(item for item in providers.get_providers()["providers"] if item["id"] == "custom:demo")
    assert demo["has_key"] is False


def test_stale_publication_is_not_returned_to_foreground(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(monkeypatch, tmp_path)
    changed = False

    def authority():
        return ("alpha", "raw", "env") if not changed else ("alpha", "changed", "env")

    def rebuild(builder):
        nonlocal changed
        result = builder()
        changed = True
        return result

    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(config, "_provider_publication_tuple", authority)
    monkeypatch.setattr(config, "_invoke_models_rebuild", rebuild)
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda _connection: [{"id": "syn:stale", "label": "syn:stale"}],
    )
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    assert not any(group.get("provider_id") == "synthetic" for group in result["groups"])


def test_stale_publication_after_disk_save_is_discarded(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(monkeypatch, tmp_path)
    changed = False
    deleted = []

    def authority():
        return ("alpha", "changed", "env") if changed else ("alpha", "raw", "env")

    def save(_result):
        nonlocal changed
        changed = True

    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(config, "_provider_publication_tuple", authority)
    monkeypatch.setattr(config, "_save_models_cache_to_disk", save)
    monkeypatch.setattr(config, "_delete_models_cache_on_disk", lambda: deleted.append(True))
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda _connection: [{"id": "syn:stale", "label": "syn:stale"}],
    )
    cache_after = None
    try:
        result = config.get_available_models(force_refresh=True)
        cache_after = config._available_models_cache
    finally:
        _restore(old_cfg, old_mtime)
    assert not any(group.get("provider_id") == "synthetic" for group in result["groups"])
    assert deleted
    assert cache_after is None


def test_pinned_fetch_resolves_once_and_uses_vetted_addresses():
    calls = []
    connection = build_connection(
        profile="alpha",
        provider_id="synthetic",
        raw_config_key="synthetic",
        raw={"base_url": "https://api.synthetic.test/v1", "key_env": "KEY"},
        env_value=lambda _name: "secret",
    )
    prepared = prepare_connection(
        connection,
        resolver=lambda *args, **kwargs: calls.append((args, kwargs)) or [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )
    assert isinstance(prepared, ProviderConnection)
    assert prepared.vetted_addresses == ("93.184.216.34",)
    assert len(calls) == 1

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"data":[{"id":"syn:large:text"}]}'

    captured = {}
    def opener(request, **_kwargs):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        return Response()

    assert fetch_models(prepared, opener=opener) == [{"id": "syn:large:text", "label": "syn:large:text"}]
    assert captured == {
        "url": "https://api.synthetic.test/v1/models",
        "authorization": "Bearer secret",
    }


def test_pinned_transport_uses_host_sni_and_falls_back_across_vetted_addresses(monkeypatch):
    connection = build_connection(
        profile="alpha",
        provider_id="synthetic",
        raw_config_key="synthetic",
        raw={"base_url": "https://api.synthetic.test/v1"},
    )
    prepared = prepare_connection(
        connection,
        resolver=lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("93.184.216.35", 443)),
        ],
    )
    handler = provider_discovery._PinnedHTTPSHandler(prepared, 5.0)
    attempts = []

    def do_open(factory, _request):
        pinned = factory("api.synthetic.test")
        attempts.append(pinned._address)
        if len(attempts) == 1:
            raise urllib.error.URLError("first address unavailable")
        return "response"

    monkeypatch.setattr(handler, "do_open", do_open)
    request = urllib.request.Request("https://api.synthetic.test/v1/models")
    assert handler.https_open(request) == "response"
    assert attempts == ["93.184.216.34", "93.184.216.35"]

    dialed = []
    wrapped = []

    class RawSocket:
        pass

    raw_socket = RawSocket()
    monkeypatch.setattr(
        provider_discovery.socket,
        "create_connection",
        lambda address, timeout: dialed.append((address, timeout)) or raw_socket,
    )
    class Context:
        def wrap_socket(self, sock, server_hostname):
            wrapped.append((sock, server_hostname))
            return sock

    pinned = provider_discovery._PinnedHTTPSConnection(
        "api.synthetic.test",
        "93.184.216.35",
        port=443,
        timeout=5.0,
        context=provider_discovery.ssl.create_default_context(),
    )
    pinned._context = Context()
    pinned.connect()
    assert dialed == [(('93.184.216.35', 443), 5.0)]
    assert wrapped == [(raw_socket, "api.synthetic.test")]


def test_default_fetch_uses_no_proxy_no_redirect_handlers_and_rejects_partial_response(monkeypatch):
    connection = build_connection(
        profile="alpha",
        provider_id="synthetic",
        raw_config_key="synthetic",
        raw={"base_url": "https://api.synthetic.test/v1", "api_key": "secret"},
    )
    prepared = prepare_connection(
        connection,
        resolver=lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    captured_handlers = []

    class Response:
        status = 206

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"data": [{"id": "partial"}]}'

    class Opener:
        def open(self, _request, timeout=None):
            assert timeout == 5.0
            return Response()

    monkeypatch.setattr(
        provider_discovery.urllib.request,
        "build_opener",
        lambda *handlers: captured_handlers.extend(handlers) or Opener(),
    )
    with pytest.raises(provider_discovery.ProviderDiscoveryError) as exc_info:
        fetch_models(prepared)
    assert exc_info.value.kind == "http"
    assert exc_info.value.status == 206
    assert any(isinstance(item, provider_discovery._PinnedHTTPSHandler) for item in captured_handlers)
    assert any(isinstance(item, urllib.request.ProxyHandler) for item in captured_handlers)
    assert any(isinstance(item, urllib.request.HTTPRedirectHandler) for item in captured_handlers)

    class AuthOpener:
        def open(self, _request, timeout=None):
            assert timeout == 5.0
            raise urllib.error.HTTPError("https://api.synthetic.test/v1/models", 401, "unauthorized", {}, None)

    monkeypatch.setattr(
        provider_discovery.urllib.request,
        "build_opener",
        lambda *handlers: AuthOpener(),
    )
    with pytest.raises(provider_discovery.ProviderDiscoveryError) as auth_info:
        fetch_models(prepared)
    assert auth_info.value.kind == "auth"
    assert auth_info.value.status == 401


def test_destination_rejection_redirect_and_response_size_are_enforced():
    connection = build_connection(
        profile="alpha",
        provider_id="synthetic",
        raw_config_key="synthetic",
        raw={"base_url": "https://api.synthetic.test/v1", "api_key": "secret"},
    )
    for address in ("127.0.0.1", "100.64.0.1"):
        with pytest.raises(provider_discovery.ProviderDiscoveryError) as blocked:
            prepare_connection(
                connection,
                resolver=lambda *_args, address=address, **_kwargs: [
                    (2, 1, 6, "", (address, 443))
                ],
            )
        assert blocked.value.kind == "blocked_destination"

    with pytest.raises(provider_discovery.ProviderDiscoveryError) as redirected:
        provider_discovery._NoRedirect().redirect_request(
            urllib.request.Request("https://api.synthetic.test/v1/models"),
            None,
            302,
            "redirect",
            {},
            "https://other.synthetic.test/models",
        )
    assert redirected.value.kind == "redirect"

    prepared = prepare_connection(
        connection,
        resolver=lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"12345"

    with pytest.raises(provider_discovery.ProviderDiscoveryError) as too_large:
        fetch_models(prepared, opener=lambda _request, **_kwargs: Response(), max_bytes=4)
    assert too_large.value.kind == "response_too_large"


def test_profile_snapshot_authority_pairs_endpoint_and_credential():
    alpha = build_connection(
        profile="alpha",
        provider_id="synthetic",
        raw_config_key="synthetic",
        raw={"base_url": "https://alpha.synthetic.test/v1", "key_env": "ALPHA_KEY"},
        env_value=lambda name: {"ALPHA_KEY": "alpha-secret"}.get(name, ""),
    )
    beta = build_connection(
        profile="beta",
        provider_id="synthetic",
        raw_config_key="synthetic",
        raw={"base_url": "https://beta.synthetic.test/v1", "key_env": "BETA_KEY"},
        env_value=lambda name: {"BETA_KEY": "beta-secret"}.get(name, ""),
    )
    assert (alpha.profile, alpha.base_url, alpha.credential.value) == (
        "alpha",
        "https://alpha.synthetic.test/v1",
        "alpha-secret",
    )
    assert (beta.profile, beta.base_url, beta.credential.value) == (
        "beta",
        "https://beta.synthetic.test/v1",
        "beta-secret",
    )

    def build_for(profile):
        return build_connection(
            profile=profile,
            provider_id="synthetic",
            raw_config_key="synthetic",
            raw={
                "base_url": f"https://{profile}.synthetic.test/v1",
                "key_env": f"{profile.upper()}_KEY",
            },
            env_value=lambda name, profile=profile: {
                f"{profile.upper()}_KEY": f"{profile}-secret"
            }.get(name, ""),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = list(pool.map(build_for, ("alpha", "beta")))
    assert {(item.profile, item.base_url, item.credential.value) for item in concurrent} == {
        ("alpha", "https://alpha.synthetic.test/v1", "alpha-secret"),
        ("beta", "https://beta.synthetic.test/v1", "beta-secret"),
    }


def test_profile_scope_catalog_keeps_endpoint_and_bearer_together(monkeypatch, tmp_path):
    base = tmp_path / ".hermes"
    profiles_home = base / "profiles"
    for profile, host, secret in (
        ("alpha", "alpha.synthetic.test", "alpha-secret"),
        ("beta", "beta.synthetic.test", "beta-secret"),
    ):
        home = profiles_home / profile
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            f"""model:\n  provider: openai\nproviders:\n  synthetic:\n    name: synthetic\n    base_url: https://{host}/v1\n    key_env: SYNTHETIC_API_KEY\n""",
            encoding="utf-8",
        )
        (home / ".env").write_text(f"SYNTHETIC_API_KEY={secret}\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.setenv("HERMES_BASE_HOME", str(base))
    monkeypatch.setattr(
        profiles,
        "get_active_hermes_home",
        lambda: base / "profiles" / profiles.get_active_profile_name(),
    )
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: base / "profiles" / name,
    )
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0)
    real_fetch = provider_discovery.fetch_models
    captured = []
    results = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"data":[{"id":"syn:profile"}]}'

    def fetch_in_scope(connection):
        prepared = prepare_connection(
            connection,
            resolver=lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )

        def opener(request, **_kwargs):
            captured.append(
                {
                    "profile": connection.profile,
                    "base_url": connection.base_url,
                    "address": prepared.vetted_addresses[0],
                    "url": request.full_url,
                    "authorization": request.get_header("Authorization"),
                }
            )
            return Response()

        return real_fetch(prepared, opener=opener)

    monkeypatch.setattr(provider_discovery, "fetch_models", fetch_in_scope)
    old_state = {
        "cfg": config.cfg,
        "cfg_cache": config._cfg_cache,
        "cfg_path": config._cfg_path,
        "cfg_mtime": config._cfg_mtime,
        "cfg_fingerprint": config._cfg_fingerprint,
    }
    try:
        for profile in ("alpha", "beta"):
            profiles.set_request_profile(profile)
            try:
                config.cfg = {
                    "model": {"provider": "openai"},
                    "providers": {
                        "synthetic": {
                            "name": "synthetic",
                            "base_url": f"https://{profile}.synthetic.test/v1",
                            "key_env": "SYNTHETIC_API_KEY",
                        }
                    },
                }
                config.invalidate_models_cache()
                with profiles.profile_env_for_active_request("issue 6804"):
                    results.append(config.get_available_models(force_refresh=True))
            finally:
                profiles.clear_request_profile()
    finally:
        config.cfg = old_state["cfg"]
        config._cfg_cache = old_state["cfg_cache"]
        config._cfg_path = old_state["cfg_path"]
        config._cfg_mtime = old_state["cfg_mtime"]
        config._cfg_fingerprint = old_state["cfg_fingerprint"]
        config.invalidate_models_cache()

    assert results and captured == [
        {
            "profile": "alpha",
            "base_url": "https://alpha.synthetic.test/v1",
            "address": "93.184.216.34",
            "url": "https://alpha.synthetic.test/v1/models",
            "authorization": "Bearer alpha-secret",
        },
        {
            "profile": "beta",
            "base_url": "https://beta.synthetic.test/v1",
            "address": "93.184.216.34",
            "url": "https://beta.synthetic.test/v1/models",
            "authorization": "Bearer beta-secret",
        },
    ], results


def test_fetch_models_preserves_legacy_endpoint_and_payload_shapes():
    connection = build_connection(
        profile="alpha",
        provider_id="synthetic",
        raw_config_key="synthetic",
        raw={"base_url": "https://api.synthetic.test", "api_key": "secret"},
    )
    prepared = prepare_connection(
        connection,
        resolver=lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"models": [{"name": "legacy-model"}, {"model": "other-model"}]}'

    captured = {}

    def opener(request, **_kwargs):
        captured["url"] = request.full_url
        return Response()

    assert fetch_models(prepared, opener=opener) == [
        {"id": "legacy-model", "label": "legacy-model"},
        {"id": "other-model", "label": "other-model"},
    ]
    assert captured["url"] == "https://api.synthetic.test/v1/models"


def test_registered_provider_does_not_enter_strict_probe_lane(monkeypatch, tmp_path):
    old_cfg, old_mtime = _configure(monkeypatch, tmp_path)
    config.cfg["providers"] = {
        "huggingface": {
            "base_url": "https://evil.synthetic.test/v1",
            "key_env": "SYNTHETIC_API_KEY",
        }
    }
    probes = []
    monkeypatch.setattr(
        provider_discovery,
        "fetch_models",
        lambda connection: probes.append(connection.provider_id) or [],
    )
    try:
        config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)
    assert "huggingface" not in probes
