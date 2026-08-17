"""Regression coverage for configured providers using ``key_env``."""

import json
import sys
import types
import urllib.error
import urllib.request

import pytest

import api.config as config
import api.profiles as profiles
import api.providers as providers
import api.provider_discovery as provider_discovery
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
    assert config.resolve_provider_credential("literal", "KEY") == "literal"
    assert config.resolve_provider_credential("${KEY}", None) == "env-secret"
    assert config.resolve_provider_credential(None, "KEY") == "env-secret"

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
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, **_kwargs: calls.append(request.full_url))
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
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, **_kwargs: calls.append(request.full_url))
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

    old_cfg, old_mtime = _configure(monkeypatch, tmp_path, models_marker=["syn:listed"])
    config.cfg["providers"]["synthetic"]["discover_models"] = False
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, **_kwargs: calls.append(request.full_url))
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
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, **_kwargs: calls.append(request.full_url))
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
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.HTTPError("https://api.synthetic.new/openai/v1/models", 401, "secret response", {}, None)))
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
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, **_kwargs: requests.append(request.full_url))
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
