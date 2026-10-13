"""Regression coverage for native Mistral identity and legacy config keys."""

from __future__ import annotations

import copy
import json
import sys
import types
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import api.config as config
import api.onboarding as onboarding
import api.profiles as profiles
import api.providers as providers
import api.routes as routes


@pytest.fixture(autouse=True)
def _restore_config_caches():
    cache_refs = {
        name: getattr(config, name)
        for name in (
            "_cfg_cache",
            "_available_models_cache",
            "_CREDENTIAL_POOL_CACHE",
        )
    }
    cache_values = {
        name: copy.deepcopy(value) for name, value in cache_refs.items()
    }
    cfg_ref = config.cfg
    cfg_value = copy.deepcopy(cfg_ref)
    scalar_state = {
        name: getattr(config, name)
        for name in ("_cfg_mtime", "_cfg_path", "_cfg_fingerprint")
    }
    model_state = {
        name: copy.deepcopy(getattr(config, name))
        for name in (
            "_available_models_cache_ts",
            "_available_models_live_rebuild_ts",
            "_available_models_cache_source_fingerprint",
            "_models_cache_provenance",
            "_advertised_model_ids_memo",
            "_cache_build_in_progress",
            "_yaml_file_cache",
        )
    }
    thread_state = {
        name: getattr(config._thread_ctx, name, None)
        for name in ("env", "block_process_env_fallback")
    }
    yield
    for name, value in cache_refs.items():
        if isinstance(value, dict):
            value.clear()
            value.update(cache_values[name])
        setattr(config, name, value)
    cfg_ref.clear()
    cfg_ref.update(cfg_value)
    config.cfg = cfg_ref
    for name, value in scalar_state.items():
        setattr(config, name, value)
    for name, value in model_state.items():
        setattr(config, name, value)
    for name, value in thread_state.items():
        if value is None:
            if hasattr(config._thread_ctx, name):
                delattr(config._thread_ctx, name)
        else:
            setattr(config._thread_ctx, name, value)
    providers.invalidate_providers_cache()


def _install_hermes_modules(monkeypatch, roster=(), model_ids=None):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models._PROVIDER_ALIASES = {
        "mistral": "mistralai",
        "mistralai": "mistralai",
        "google": "gemini",
    }
    models.list_available_providers = lambda: list(roster)
    models.provider_model_ids = lambda provider: list((model_ids or {}).get(provider, []))
    auth = types.ModuleType("hermes_cli.auth")
    auth.get_auth_status = lambda _provider: {"key_source": "api_key"}
    hermes_cli.models = models
    hermes_cli.auth = auth
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)


def _configure_profile(monkeypatch, tmp_path, cfg):
    home = tmp_path / "profile"
    home.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "webui-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.yaml"
    auth_path = home / "auth.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BASE_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "respec-profile")
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: home)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: auth_path)
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: home)
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: home)
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _pid: False)
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(profiles, "profile_env_for_active_request_readonly", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _path: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "_onboarding_gate_allows", lambda _handler: True)
    config._thread_ctx.env = {}
    config._thread_ctx.block_process_env_fallback = False
    config.reload_config()
    config.invalidate_models_cache()
    providers.invalidate_providers_cache()
    return config_path, auth_path, home


def _call_route(monkeypatch, method, path, *, body=None, query=""):
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400, **_kwargs: {"error": message, "status": status},
    )
    monkeypatch.setattr(routes, "read_body", lambda handler: handler.payload)
    handler = SimpleNamespace(
        payload=body or {},
        headers={},
        client_address=("127.0.0.1", 12345),
        command=method,
    )
    parsed = SimpleNamespace(path=path, query=query)
    if method == "GET":
        return routes.handle_get(handler, parsed)
    return routes.handle_post(handler, parsed)


def _anthropic_config():
    return {
        "model": {
            "provider": "anthropic",
            "default": "claude-sonnet-4-6",
            "api_key": "synthetic-anthropic-key",
        },
        "providers": {"anthropic": {"api_key": "synthetic-anthropic-key"}},
    }


def _write_profile_env(home, **values):
    (home / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def _group(catalog, provider_id):
    return next(
        group
        for group in catalog["groups"]
        if group.get("provider_id") == provider_id
    )


@pytest.mark.parametrize("key_source", ["providers-route", "profile-env"])
def test_issue6894_mistral_catalog_and_default_roundtrip(
    monkeypatch, tmp_path, key_source
):
    _install_hermes_modules(
        monkeypatch,
        roster=[{"id": "anthropic", "authenticated": True}],
    )
    _configure_profile(monkeypatch, tmp_path, _anthropic_config())

    if key_source == "providers-route":
        response = _call_route(monkeypatch, "GET", "/api/providers")
        mistral_card = next(
            provider
            for provider in response["providers"]
            if provider["display_name"] == "Mistral"
        )
        saved = _call_route(
            monkeypatch,
            "POST",
            "/api/providers",
            body={"provider": mistral_card["id"], "api_key": "synthetic-mistral-key"},
        )
        assert saved["ok"] is True
    else:
        _write_profile_env(
            tmp_path / "profile", MISTRAL_API_KEY="synthetic-mistral-key"
        )

    config.invalidate_models_cache()
    catalog = _call_route(monkeypatch, "GET", "/api/models")
    mistral_groups = [
        group
        for group in catalog["groups"]
        if group.get("provider_id") == "mistral"
    ]
    assert len(mistral_groups) == 1, "configured Mistral key should produce one catalog group"
    model_ids = {model["id"] for model in mistral_groups[0]["models"]}
    assert "@mistral:mistral-small-latest" in model_ids

    saved_default = _call_route(
        monkeypatch,
        "POST",
        "/api/default-model",
        body={"model": "mistral-small-latest", "provider": "mistral"},
    )
    assert saved_default["provider"] == "mistral"
    settings = _call_route(monkeypatch, "GET", "/api/settings")
    assert settings["default_model_provider"] == "mistral"
    assert settings["default_model"] == "mistral-small-latest"


def test_mistral_alias_and_single_entry_provider_config(monkeypatch):
    _install_hermes_modules(monkeypatch)
    assert config._resolve_provider_alias("mistralai") == "mistral"
    assert config._resolve_provider_alias("mistral") == "mistral"
    assert config._resolve_provider_alias("google") == "gemini"
    assert config._resolve_provider_alias("future_provider") == "future_provider"
    assert config._canonicalise_provider_id("google") == "google"

    legacy = {"api_key": "legacy-key", "models": {"legacy-model": {"context_length": 8192}}}
    canonical = {"api_key": "canonical-key"}
    both = {"providers": {"mistralai": legacy, "mistral": canonical}}
    assert config._get_provider_cfg("mistral", config_obj=both) is canonical
    assert config._get_provider_cfg("mistralai", config_obj=both) is legacy
    assert config._get_provider_cfg(
        "mistral", config_obj={"providers": {"mistralai": legacy}}
    ) is legacy
    assert config._get_provider_cfg(
        "mistralai", config_obj={"providers": {"mistral": canonical}}
    ) is canonical
    assert config._get_provider_cfg(
        "mistral", config_obj={"providers": {"mistral": None, "mistralai": legacy}}
    ) == {}
    assert config._get_provider_cfg(
        "other", config_obj={"providers": {"other_alias": legacy}}
    ) == {}


@pytest.mark.parametrize("provider_key", ["mistralai", "MistralAI", "MISTRAL"])
def test_mistral_config_adapters_keep_the_original_entry(monkeypatch, tmp_path, provider_key):
    _install_hermes_modules(monkeypatch)
    legacy = {
        "api_key": "legacy-mistral-key",
        "base_url": "https://mistral-proxy.example/v1",
        "models": {"mistral-configured-model": {"context_length": 32768}},
    }
    cfg = _anthropic_config()
    cfg["providers"][provider_key] = legacy
    cfg["providers"]["snake_case_provider"] = {
        "api_key": "raw-key",
        "models": {"raw-model": {"context_length": 4096}},
    }
    _configure_profile(monkeypatch, tmp_path, cfg)

    assert config._get_provider_cfg("mistral") is legacy or config._get_provider_cfg("mistral")["api_key"] == "legacy-mistral-key"
    assert config._get_provider_base_url("mistral") == "https://mistral-proxy.example/v1"
    providers.invalidate_providers_cache()
    cards = providers.get_providers()["providers"]
    mistral_cards = [provider for provider in cards if provider["display_name"] == "Mistral"]
    assert len(mistral_cards) == 1
    assert mistral_cards[0]["id"] == "mistral"
    assert mistral_cards[0]["has_key"] is True
    assert "mistral-configured-model" in {model["id"] for model in mistral_cards[0]["models"]}
    assert any(provider["id"] == "snake_case_provider" for provider in cards)


@pytest.mark.parametrize(
    ("mistral_roster_row", "expected"),
    [(None, True), (True, True), (False, False)],
    ids=["omitted", "authenticated", "explicitly-rejected"],
)
def test_mistral_roster_supplement_respects_explicit_rows(
    monkeypatch, tmp_path, mistral_roster_row, expected
):
    roster = [{"id": "anthropic", "authenticated": True}]
    if mistral_roster_row is not None:
        roster.append({"id": "mistralai", "authenticated": mistral_roster_row})
    _install_hermes_modules(monkeypatch, roster=roster)
    _, _, home = _configure_profile(monkeypatch, tmp_path, _anthropic_config())
    _write_profile_env(home, MISTRAL_API_KEY="profile-mistral-key")
    assert providers._provider_has_key("mistral") is True

    catalog = config.get_available_models(force_refresh=True)
    assert ("mistral" in {g.get("provider_id") for g in catalog["groups"]}) is expected


def test_omitted_roster_supplement_uses_only_the_active_profile(monkeypatch, tmp_path):
    _install_hermes_modules(
        monkeypatch,
        roster=[{"id": "anthropic", "authenticated": True}],
    )
    _, _, home = _configure_profile(monkeypatch, tmp_path, _anthropic_config())
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "ambient-server-key")
    config._thread_ctx.block_process_env_fallback = True
    config._thread_ctx.env = {}
    assert providers._provider_has_key("mistral") is False
    assert "mistral" not in {
        group.get("provider_id")
        for group in config.get_available_models(force_refresh=True)["groups"]
    }

    _write_profile_env(home, MISTRAL_API_KEY="named-profile-key")
    config.invalidate_models_cache()
    catalog = config.get_available_models(force_refresh=True)
    assert "mistral" in {group.get("provider_id") for group in catalog["groups"]}
    assert "MISTRAL_API_KEY=named-profile-key" in (home / ".env").read_text(encoding="utf-8")


def test_google_pool_alias_keeps_raw_loader_and_profile_cache_keys(monkeypatch, tmp_path):
    _install_hermes_modules(monkeypatch)
    config_path, auth_path_a, _ = _configure_profile(
        monkeypatch, tmp_path, _anthropic_config()
    )
    calls = []

    class Pool:
        def entries(self):
            return [SimpleNamespace(source="api_key", label="Google", key_source="api_key")]

    agent = types.ModuleType("agent")
    agent.__path__ = []
    credential_pool = types.ModuleType("agent.credential_pool")
    credential_pool.load_pool = lambda provider: calls.append(provider) or Pool()
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", credential_pool)

    def write_auth(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"credential_pool": {"google": [{"source": "api_key"}]}}),
            encoding="utf-8",
        )

    write_auth(auth_path_a)
    catalog_a = config.get_available_models(force_refresh=True)
    assert "gemini" in {group.get("provider_id") for group in catalog_a["groups"]}
    assert "google" not in {group.get("provider_id") for group in catalog_a["groups"]}
    assert (str(auth_path_a), "google") in config._CREDENTIAL_POOL_CACHE

    auth_path_b = tmp_path / "other-profile" / "auth.json"
    write_auth(auth_path_b)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: auth_path_b)
    config.invalidate_models_cache()
    catalog_b = config.get_available_models(force_refresh=True)
    assert "gemini" in {group.get("provider_id") for group in catalog_b["groups"]}
    assert calls == ["google", "google"]
    assert (str(auth_path_a), "google") not in config._CREDENTIAL_POOL_CACHE
    assert (str(auth_path_b), "google") in config._CREDENTIAL_POOL_CACHE

    static_catalog = config._static_models_catalog_without_live_probes()
    static_ids = {group.get("provider_id") for group in static_catalog["groups"]}
    assert "gemini" in static_ids
    assert "google" not in static_ids
    assert config._get_config_path() == config_path


def test_mistral_key_routes_clear_both_legacy_entries(monkeypatch, tmp_path):
    _install_hermes_modules(monkeypatch)
    cfg = {
        "model": {"provider": "mistralai", "default": "mistral-small-latest", "api_key": "active-key"},
        "providers": {
            "mistralai": {"api_key": "legacy-key"},
            "MistralAI": {"api_key": "mixed-case-legacy-key"},
            "MISTRAL": {"api_key": "mixed-case-canonical-key"},
            "mistral": {"api_key": "canonical-key"},
            "google": {"api_key": "google-key"},
        },
    }
    config_path, _, home = _configure_profile(monkeypatch, tmp_path, cfg)

    saved = _call_route(
        monkeypatch,
        "POST",
        "/api/providers",
        body={"provider": "mistralai", "api_key": "synthetic-mistral-key"},
    )
    assert saved["provider"] == "mistral"
    assert "MISTRAL_API_KEY=synthetic-mistral-key" in (home / ".env").read_text(encoding="utf-8")
    assert providers._provider_has_key("mistral")
    assert providers._get_provider_api_key("mistralai") == "synthetic-mistral-key"

    removed = _call_route(
        monkeypatch,
        "POST",
        "/api/providers/delete",
        body={"provider": "mistral"},
    )
    assert removed["ok"] is True
    saved_cfg = config._load_yaml_config_file(config_path)
    assert "api_key" not in saved_cfg["providers"]["mistralai"]
    assert "api_key" not in saved_cfg["providers"]["mistral"]
    assert "api_key" not in saved_cfg["providers"]["MistralAI"]
    assert "api_key" not in saved_cfg["providers"]["MISTRAL"]
    assert "api_key" not in saved_cfg["model"]
    assert saved_cfg["providers"]["google"]["api_key"] == "google-key"


@pytest.mark.parametrize("provider_key", ["mistralai", "MistralAI", "MISTRAL"])
def test_mistral_live_lookup_uses_legacy_config_key(monkeypatch, tmp_path, provider_key):
    import urllib.request

    _install_hermes_modules(monkeypatch)
    cfg = _anthropic_config()
    cfg["providers"][provider_key] = {"api_key": "legacy-mistral-key"}
    _configure_profile(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(routes, "_get_cached_live_models", lambda _key: None)
    monkeypatch.setattr(routes, "_set_cached_live_models", lambda *_args: None)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "mistral-live-model"}]}).encode()

    def fake_urlopen(request, timeout=None):
        requests.append((request.full_url, request.headers.get("Authorization"), timeout))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = routes._handle_live_models(None, SimpleNamespace(query="provider=mistralai"))
    assert requests == [
        ("https://api.mistral.ai/v1/models", "Bearer legacy-mistral-key", 8)
    ]
    assert payload["provider"] == "mistral"
    assert "mistral-live-model" in {model["id"] for model in payload["models"]}


def test_mistral_live_probe_failure_uses_static_models_without_foreign_key(
    monkeypatch, tmp_path
):
    import urllib.request

    _install_hermes_modules(monkeypatch)
    cfg = {
        "model": {"provider": "anthropic", "api_key": "anthropic-secret"},
        "providers": {"mistralai": {"api_key": "legacy-mistral-key"}},
    }
    _configure_profile(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(routes, "_get_cached_live_models", lambda _key: None)
    monkeypatch.setattr(routes, "_set_cached_live_models", lambda *_args: None)
    requests = []

    def fail_probe(request, timeout=None):
        requests.append((request.full_url, request.headers.get("Authorization"), timeout))
        raise OSError("synthetic probe failure")

    monkeypatch.setattr(urllib.request, "urlopen", fail_probe)
    payload = routes._handle_live_models(None, SimpleNamespace(query="provider=mistral"))
    assert requests == [
        ("https://api.mistral.ai/v1/models", "Bearer legacy-mistral-key", 8)
    ]
    assert payload["provider"] == "mistral"
    assert "mistral-small-latest" in {model["id"] for model in payload["models"]}

    requests.clear()
    _configure_profile(
        monkeypatch,
        tmp_path / "without-mistral-key",
        {
            "model": {"provider": "anthropic", "api_key": "anthropic-secret"},
            "providers": {},
        },
    )
    payload = routes._handle_live_models(None, SimpleNamespace(query="provider=mistral"))
    assert requests == []
    assert payload["provider"] == "mistral"


def test_mistral_metadata_uses_existing_model_parsers(monkeypatch, tmp_path):
    _install_hermes_modules(monkeypatch)
    model_cfg = {
        "models": {
            "mistral-meta": {"context_length": 32768, "reasoning_efforts": ["high"]}
        },
        "reasoning_efforts": ["medium"],
    }
    cfg = _anthropic_config()
    cfg["providers"]["mistralai"] = model_cfg
    _configure_profile(monkeypatch, tmp_path, cfg)

    context = routes._context_length_lookup_inputs_for_model(
        "mistral-meta", "mistral", cfg=cfg
    )
    assert context.config_context_length == 32768
    assert context.api_key == ""
    assert routes._models_config_context_length(
        [
            {"id": "mistral-meta", "context_length": 0},
            {"id": "mistral-meta", "context_length": 4096},
        ],
        "mistral-meta",
    ) == 4096
    assert config._configured_reasoning_effort_lists(
        {"models": [{"id": "mistral-meta", "reasoning_efforts": ["high"]}],
         "reasoning_efforts": ["low"]},
        "mistral-meta",
    ) == [["low"]]
    monkeypatch.setattr(config, "_models_dev_reasoning_efforts", lambda *_args: None)
    monkeypatch.setattr(config, "_heuristic_reasoning_efforts", lambda *_args: [])
    assert config._resolve_model_reasoning_efforts_impl(
        "mistral-meta", "mistral"
    ) == ["high"]


def test_mistral_default_switch_and_session_identity(monkeypatch, tmp_path):
    _install_hermes_modules(monkeypatch)
    cfg = {
        "model": {
            "provider": "mistralai",
            "default": "mistral-large-latest",
            "base_url": "https://mistral-proxy.example/v1",
        },
        "providers": {},
    }
    config_path, _, _ = _configure_profile(monkeypatch, tmp_path, cfg)

    saved = _call_route(
        monkeypatch,
        "POST",
        "/api/default-model",
        body={"model": "mistral-small-latest", "provider": "mistral"},
    )
    assert saved["provider"] == "mistral"
    assert config._load_yaml_config_file(config_path)["model"]["base_url"] == "https://mistral-proxy.example/v1"

    _call_route(
        monkeypatch,
        "POST",
        "/api/default-model",
        body={"model": "claude-sonnet-4-6", "provider": "anthropic"},
    )
    switched = config._load_yaml_config_file(config_path)["model"]
    assert switched["provider"] == "anthropic"
    assert "base_url" not in switched

    custom_cfg = {
        "model": {
            "provider": "custom:first",
            "default": "first-model",
            "base_url": "https://first.example/v1",
        },
        "custom_providers": [
            {"name": "first", "base_url": "https://first.example/v1", "models": ["first-model"]},
            {"name": "second", "base_url": "https://second.example/v1", "models": ["second-model"]},
        ],
    }
    config._save_yaml_config_file(config_path, custom_cfg)
    custom_switch = _call_route(
        monkeypatch,
        "POST",
        "/api/default-model",
        body={"model": "@custom:second:second-model", "provider": "custom:second"},
    )
    assert custom_switch["provider"] == "custom:second"
    custom_saved = config._load_yaml_config_file(config_path)["model"]
    assert custom_saved["default"] == "second-model"
    assert "base_url" not in custom_saved
    with pytest.raises(ValueError, match="model is required"):
        config.set_hermes_default_model("")

    assert routes._clean_session_model_provider("mistralai") == "mistral"
    assert routes._clean_session_model_provider("@mistralai:mistral-small-latest") == "mistral"
    assert routes._clean_session_model_provider("default") is None
    assert routes._clean_session_model_provider("google") == "google"
    assert routes._clean_session_model_provider("future-provider") == "future-provider"
    assert routes._clean_session_model_provider("@custom:my_proxy:vendor/model") == "custom:my_proxy"


def test_legacy_mistral_onboarding_setup_writes_native_provider(monkeypatch, tmp_path):
    _install_hermes_modules(monkeypatch)
    config_path, _, home = _configure_profile(monkeypatch, tmp_path, {
        "model": {"provider": "anthropic", "default": "claude-sonnet-4-6"},
        "providers": {},
    })
    legacy_cfg = {
        "model": {"provider": "mistralai"},
        "providers": {"mistralai": {"api_key": "legacy-key"}},
    }
    assert onboarding._extract_current_provider(legacy_cfg) == "mistral"
    assert onboarding._provider_api_key_present("mistral", legacy_cfg, {}) is True

    monkeypatch.setattr(onboarding, "get_onboarding_status", lambda: {"completed": True})
    response = _call_route(
        monkeypatch,
        "POST",
        "/api/onboarding/setup",
        body={
            "provider": "mistralai",
            "model": "mistral-large-latest",
            "api_key": "synthetic-mistral-key",
            "confirm_overwrite": True,
        },
    )
    assert response["completed"] is True
    saved_cfg = onboarding._load_yaml_config(config_path)
    assert saved_cfg["model"]["provider"] == "mistral"
    assert saved_cfg["model"]["default"] == "mistral-large-latest"
    assert "MISTRAL_API_KEY=synthetic-mistral-key" in (home / ".env").read_text(encoding="utf-8")


@pytest.mark.parametrize("provider, expected", [("google", "gemini"), ("mistralai", "mistral"), ("xai", "x-ai"), ("x-ai", "x-ai"), ("custom:my_proxy", "custom:my_proxy")])
def test_pool_alias_adapter_maps_agent_ids_to_catalog(monkeypatch, provider, expected):
    _install_hermes_modules(monkeypatch)
    assert config._pool_catalog_provider_id(provider) == expected


def test_configured_google_and_gemini_cards_remain_distinct(monkeypatch, tmp_path):
    _install_hermes_modules(monkeypatch)
    cfg = _anthropic_config()
    cfg["providers"].update({"google": {"api_key": "google-key"}, "gemini": {"api_key": "gemini-key"}})
    _configure_profile(monkeypatch, tmp_path, cfg)
    cards = {card["id"]: card for card in providers.get_providers()["providers"]}
    assert cards["google"]["has_key"] is True
    assert cards["gemini"]["has_key"] is True
    assert cards["google"]["id"] != cards["gemini"]["id"]


@pytest.mark.parametrize("readonly", [False, True])
def test_mistral_key_status_and_retrieval_use_agent_pool_alias(monkeypatch, tmp_path, readonly):
    _install_hermes_modules(monkeypatch)
    has_pool_credentials = config._has_explicit_pool_credentials
    _, auth_path, _ = _configure_profile(monkeypatch, tmp_path, _anthropic_config())
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", has_pool_credentials)
    config._thread_ctx.block_process_env_fallback = readonly
    payload = {"source": "manual", "runtime_api_key": "synthetic-pool-key"}
    auth_path.write_text(json.dumps({"credential_pool": {"mistralai": [payload]}}), encoding="utf-8")
    reads = []

    def read_pool(provider):
        reads.append(provider)
        return json.loads(auth_path.read_text(encoding="utf-8"))["credential_pool"].get(provider, [])

    sys.modules["hermes_cli.auth"].read_credential_pool = read_pool
    agent = types.ModuleType("agent")
    agent.__path__ = []
    pool_module = types.ModuleType("agent.credential_pool")
    pool_module.load_pool = lambda provider: SimpleNamespace(entries=lambda: [SimpleNamespace(**entry) for entry in read_pool(provider)])
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", pool_module)

    assert config._resolve_provider_alias("mistral") == "mistral"
    assert config._has_explicit_pool_credentials("mistral") is True
    assert providers._provider_has_key("mistral") is True
    assert providers._get_provider_api_key("mistral") == "synthetic-pool-key"
    assert providers._get_provider_api_key("mistralai") == "synthetic-pool-key"
    assert reads and set(reads) == {"mistralai"}
    if not readonly:
        assert (str(auth_path), "mistralai") in config._CREDENTIAL_POOL_CACHE
        for invalidate in (config.invalidate_credential_pool_cache, config.invalidate_provider_models_cache):
            invalidate("mistral")
            assert (str(auth_path), "mistralai") not in config._CREDENTIAL_POOL_CACHE
            assert providers._get_provider_api_key("mistral") == "synthetic-pool-key"
