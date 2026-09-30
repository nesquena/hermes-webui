"""Behavioral proof for the WebUI and Agent provider namespace boundary."""

from __future__ import annotations

import copy
import os
import sys
import types
from types import SimpleNamespace

import pytest

import api.config as config
import api.onboarding as onboarding
import api.providers as providers
import api.routes as routes


def _install_hermes_modules(monkeypatch, roster=None, model_ids=None):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    auth = types.ModuleType("hermes_cli.auth")
    models.list_available_providers = lambda: list(roster or [])
    models.provider_model_ids = lambda _provider: list(model_ids or [])
    auth.get_auth_status = lambda _provider: {}
    hermes_cli.models = models
    hermes_cli.auth = auth
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)


@pytest.fixture(autouse=True)
def _restore_config_module_state():
    cache_obj = config._cfg_cache
    cache_state = copy.deepcopy(cache_obj)
    cfg_obj = config.cfg
    cfg_state = copy.deepcopy(cfg_obj)
    cfg_state_values = {
        name: getattr(config, name)
        for name in ("_cfg_mtime", "_cfg_path", "_cfg_fingerprint")
    }
    model_state = {
        name: copy.deepcopy(getattr(config, name))
        for name in (
            "_available_models_cache",
            "_available_models_cache_ts",
            "_available_models_live_rebuild_ts",
            "_available_models_cache_source_fingerprint",
            "_models_cache_provenance",
            "_advertised_model_ids_memo",
            "_cache_build_in_progress",
            "_yaml_file_cache",
        )
    }
    pool_cache_obj = config._CREDENTIAL_POOL_CACHE
    pool_cache_state = copy.deepcopy(pool_cache_obj)
    yield
    cache_obj.clear()
    cache_obj.update(cache_state)
    config._cfg_cache = cache_obj
    cfg_obj.clear()
    cfg_obj.update(cfg_state)
    config.cfg = cfg_obj
    for name, value in cfg_state_values.items():
        setattr(config, name, value)
    for name, value in model_state.items():
        setattr(config, name, value)
    pool_cache_obj.clear()
    pool_cache_obj.update(pool_cache_state)
    config._CREDENTIAL_POOL_CACHE = pool_cache_obj


def test_canonical_identity_preservation_matrix():
    expected = {
        "mistralai": "mistral",
        "mistral": "mistral",
        "google": "google",
        "gemini": "gemini",
        "x-ai": "x-ai",
        "qwen": "qwen",
        "ollama": "ollama",
        "custom": "custom",
        "custom:local": "custom:local",
        "future_provider": "future-provider",
    }
    assert {raw: config._canonicalise_provider_id(raw) for raw in expected} == expected


@pytest.mark.parametrize(
    "ambient_mistral_key",
    [None, "leaked-suite-key"],
    ids=["clean-environment", "conflicting-environment"],
)
def test_configured_mistral_key_becomes_selectable_and_savable(
    monkeypatch, tmp_path, ambient_mistral_key
):
    cfg = {"providers": {"mistralai": {"api_key": "legacy-key"}},
           "model": {"provider": "mistralai"}}
    if ambient_mistral_key is not None:
        monkeypatch.setenv("MISTRAL_API_KEY", ambient_mistral_key)
    else:
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(config, "_thread_local_env_value", lambda _name, default="": default)
    monkeypatch.setattr(providers, "_thread_local_env_value", lambda _name, default="": default)
    monkeypatch.setattr(providers, "_load_env_file", lambda _path: {})
    monkeypatch.setattr(providers, "_pool_entry_payloads", lambda _provider: [])
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "_cfg_cache", cfg)
    monkeypatch.setattr(config, "_cfg_fingerprint", None)
    assert config._canonical_provider_config(cfg, "mistral")["api_key"] == "legacy-key"
    writes = []
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    monkeypatch.setattr(providers, "_write_env_file", lambda _path, update: writes.append(update))
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _pid: False)
    monkeypatch.setattr(providers, "invalidate_models_cache", lambda: None)
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda _pid: None)
    monkeypatch.setattr(providers, "invalidate_providers_cache", lambda: None)
    assert providers._provider_has_key("mistralai")
    assert providers._get_provider_api_key("mistral") == "legacy-key"
    assert providers.set_provider_key("mistralai", "mistral-secret-key")["provider"] == "mistral"
    assert writes == [{"MISTRAL_API_KEY": "mistral-secret-key"}]


def test_session_restore_preserves_established_provider_ids():
    assert routes._clean_session_model_provider("unknown_provider") == "unknown_provider"
    for provider in ("", "default", "@", "@:"):
        assert routes._clean_session_model_provider(provider) is None
    for provider in (
        "ollama",
        "google",
        "x-ai",
        "qwen",
        "custom",
        "custom:local",
        "custom:my_proxy",
        "unknown",
    ):
        assert routes._clean_session_model_provider(provider) == provider
    assert routes._clean_session_model_provider(
        "@custom:my_proxy:vendor/model"
    ) == "custom:my_proxy"
    assert routes._clean_session_model_provider("mistralai") == "mistral"


def test_session_restore_preserves_underscore_custom_provider_metadata(monkeypatch):
    cfg = {
        "model": {"provider": "anthropic", "default": "claude-sonnet-4-6"},
        "custom_providers": [
            {
                "name": "my_proxy",
                "base_url": "https://my-proxy.example/v1",
                "api_key": "my-proxy-key",
                "models": {"vendor/model": {"context_length": 8192}},
            },
            {
                "name": "another_proxy",
                "base_url": "https://another-proxy.example/v1",
                "api_key": "another-proxy-key",
                "models": {"other/other-model": {"context_length": 2048}},
            },
        ],
    }
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    session = SimpleNamespace(
        model="model", model_provider="custom:my_proxy", profile=None
    )

    provider = routes._resolve_effective_session_model_provider_for_display(session)
    model = routes._resolve_effective_session_model_for_display(session)
    assert provider == "custom:my_proxy"
    assert model == "vendor/model"

    resolved = routes._session_context_length_lookup_state(model, provider)
    assert resolved == (
        "vendor/model",
        "custom:my_proxy",
        "https://my-proxy.example/v1",
        "my-proxy-key",
    )
    metadata = routes._context_length_lookup_inputs_for_model(
        model,
        provider,
        base_url=resolved[2],
        api_key=resolved[3],
        cfg=cfg,
    )
    assert metadata.config_context_length == 8192
    assert metadata.base_url == "https://my-proxy.example/v1"
    assert metadata.api_key == "my-proxy-key"


def test_provider_cards_keep_google_and_gemini_separate(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "get_config", lambda: {
        "model": {"provider": "google"},
        "providers": {"google": {"api_key": "google-key"}, "gemini": {"api_key": "gemini-key"}},
    })
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _pid: False)
    monkeypatch.setattr(providers, "_PROVIDER_DISPLAY", {"google": "Google", "gemini": "Gemini"})
    monkeypatch.setattr(providers, "_PROVIDER_MODELS", {"google": [], "gemini": []})
    monkeypatch.setattr(providers, "_OAUTH_PROVIDERS", set())
    monkeypatch.setattr(providers, "_provider_is_oauth", lambda _pid: False)
    monkeypatch.setattr(providers, "_get_cached_providers", lambda _key: None)
    monkeypatch.setattr(providers, "_store_cached_providers", lambda _key, result: result)
    monkeypatch.setattr(providers, "plugin_model_provider_ids", lambda: [])
    monkeypatch.setattr(providers, "is_plugin_model_provider", lambda _pid: False)
    monkeypatch.setattr(providers, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(providers, "_read_visible_codex_cache_model_ids", lambda: [])
    monkeypatch.setattr(providers, "_models_from_live_provider_ids", lambda _pid, ids: [])
    result = providers.get_providers()
    cards = {card["id"] for card in result["providers"]}
    assert {"google", "gemini"}.issubset(cards)


def test_provider_key_operations_select_google_env_slot(monkeypatch, tmp_path):
    writes = []
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(providers, "_write_env_file", lambda _path, update: writes.append(update))
    monkeypatch.setattr(providers, "invalidate_models_cache", lambda: None)
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda _pid: None)
    monkeypatch.setattr(providers, "invalidate_providers_cache", lambda: None)
    result = providers.set_provider_key("google", "google-secret-key")
    assert result["provider"] == "google"
    assert writes == [{"GOOGLE_API_KEY": "google-secret-key"}]


def test_mistral_partial_config_merge_precedence_and_cleanup():
    cfg = {"providers": {
        "mistralai": {"api_key": "legacy", "models": ["legacy-model"], "base_url": "legacy-url"},
        "mistral": {"api_key": "canonical", "models": ["canonical-model"]},
    }}
    merged = config._canonical_provider_config(cfg, "mistralai")
    assert merged["api_key"] == "canonical"
    assert merged["base_url"] == "legacy-url"
    assert merged["models"] == ["legacy-model", "canonical-model"]
    assert set(config._canonical_provider_config_keys(cfg, "mistral")) == {"mistralai", "mistral"}


def test_canonical_provider_model_metadata_wins_alias_duplicate():
    cfg = {"providers": {
        "mistralai": {"models": [{"id": "shared-model", "label": "Legacy label"}]},
        "mistral": {"models": [{"id": "shared-model", "label": "Canonical label"}]},
    }}
    merged = config._canonical_provider_config(cfg, "mistral")
    assert merged["models"] == [{"id": "shared-model", "label": "Canonical label"}]


def test_equivalent_mistral_default_save_preserves_custom_base_url(monkeypatch, tmp_path):
    saved = {}
    cfg = {"model": {"provider": "mistralai", "base_url": "https://proxy.example/v1"}}
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_load_yaml_config_file", lambda _path: cfg.copy())
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "_save_yaml_config_file", lambda _path, value: saved.update(value))
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    result = config.set_hermes_default_model("mistral-small-latest", provider="mistral")
    assert result["provider"] == "mistral"
    assert saved["model"]["provider"] == "mistral"
    assert saved["model"]["base_url"] == "https://proxy.example/v1"


def test_get_provider_base_url_uses_legacy_provider_config_for_canonical_mistral(monkeypatch):
    monkeypatch.setattr(config, "cfg", {
        "providers": {"mistralai": {"base_url": "https://proxy.example/v1/"}},
        "model": {"provider": "google", "base_url": "https://model.example/v1/"},
    })
    assert config._get_provider_base_url("mistral") == "https://proxy.example/v1"

    monkeypatch.setattr(config, "cfg", {
        "model": {"provider": "mistralai", "base_url": "https://model.example/v1/"},
    })
    assert config._get_provider_base_url("mistral") == "https://model.example/v1"

    monkeypatch.setattr(config, "cfg", {
        "model": {"provider": "google", "base_url": "https://model.example/v1/"},
    })
    assert config._get_provider_base_url("mistral") is None


def test_remove_provider_key_canonicalizes_legacy_mistralai_request(monkeypatch, tmp_path):
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model": {"provider": "mistral", "api_key": "active-mistral-key"},
        "providers": {
            "mistralai": {"api_key": "legacy-mistral-key"},
            "mistral": {"api_key": "canonical-mistral-key"},
            "google": {"api_key": "google-key"},
        },
    }), encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(providers, "_write_env_file", lambda *_args: None)
    monkeypatch.setattr(providers, "invalidate_models_cache", lambda: None)
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda _pid: None)
    monkeypatch.setattr(providers, "invalidate_providers_cache", lambda: None)

    assert providers.remove_provider_key("mistralai")["ok"] is True
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "api_key" not in saved["providers"]["mistralai"]
    assert "api_key" not in saved["providers"]["mistral"]
    assert "api_key" not in saved["model"]
    assert saved["providers"]["google"]["api_key"] == "google-key"


def test_authenticated_codex_roster_preserves_picker_membership(monkeypatch, tmp_path):
    for authenticated in (True, False):
        cfgfile = tmp_path / f"codex-{authenticated}.yaml"
        cfgfile.write_text(
            "model:\n  provider: google\n  default: gemini-2.5-pro\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda cfgfile=cfgfile: cfgfile)
        monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
        monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
        monkeypatch.setattr(config, "_thread_local_env_value", lambda name, default="": {
            "OPENAI_API_KEY": "openai-key",
        }.get(name, default))
        _install_hermes_modules(monkeypatch, roster=[
            {"id": "openai-codex", "authenticated": authenticated},
            {"id": "openai-api", "authenticated": False},
        ])
        config.reload_config()
        config.invalidate_models_cache()

        catalog = config.get_available_models(force_refresh=True)
        provider_ids = {group.get("provider_id") for group in catalog["groups"]}
        assert ("openai-codex" in provider_ids) is authenticated
        assert "openai-api" not in provider_ids
        assert catalog["active_provider"] == "google"


def test_environment_detection_respects_roster_authentication_matrix(monkeypatch, tmp_path):
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        "model:\n  provider: google\n  default: gemini-2.5-pro\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_thread_local_env_value", lambda name, default="": {
        "MISTRAL_API_KEY": "mistral-env-key"
    }.get(name, default))
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    _install_hermes_modules(
        monkeypatch,
        roster=[{"id": "openai-api", "authenticated": False}],
    )
    config.reload_config()
    config.invalidate_models_cache()

    catalog = config.get_available_models(force_refresh=True)
    provider_ids = {group.get("provider_id") for group in catalog["groups"]}
    assert catalog["active_provider"] == "google"
    assert "google" in provider_ids
    assert "mistral" in provider_ids
    assert "openai-api" not in provider_ids


def test_catalog_preserves_webui_active_provider_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    _install_hermes_modules(monkeypatch)

    expected = {"google": "google", "x-ai": "x-ai", "qwen": "qwen", "mistralai": "mistral"}
    for raw_provider, expected_provider in expected.items():
        cfgfile = tmp_path / f"{raw_provider}.yaml"
        cfgfile.write_text(
            f"model:\n  provider: {raw_provider}\n  default: test-model\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda cfgfile=cfgfile: cfgfile)
        config.reload_config()
        config.invalidate_models_cache()
        catalog = config.get_available_models(force_refresh=True)
        assert catalog["active_provider"] == expected_provider


def test_catalog_merges_canonical_and_legacy_provider_models(monkeypatch, tmp_path):
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        "model:\n  provider: mistral\n  default: canonical-model\n"
        "providers:\n  mistral:\n    models:\n      - canonical-model\n"
        "  mistralai:\n    models:\n      - legacy-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    _install_hermes_modules(monkeypatch)
    config.reload_config()
    config.invalidate_models_cache()

    catalog = config.get_available_models(force_refresh=True)
    group = next(group for group in catalog["groups"] if group.get("provider_id") == "mistral")
    model_ids = {model["id"] for model in group["models"]}
    assert {"canonical-model", "legacy-model"}.issubset(model_ids)


def test_onboarding_reads_legacy_mistral_and_writes_canonical_provider(monkeypatch, tmp_path):
    cfg = {"model": {"provider": "mistralai", "default": "mistral-large-latest"},
           "providers": {"mistralai": {"api_key": "legacy-key"}}}
    assert onboarding._extract_current_provider(cfg) == "mistral"
    assert not onboarding._provider_api_key_present(
        "mistralai",
        {"model": {"provider": "google", "api_key": "google-key"}},
        {},
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  provider: mistralai\n  default: mistral-large-latest\n"
        "providers:\n  mistralai:\n    api_key: legacy-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: tmp_path)
    assert onboarding._provider_api_key_present("mistralai", cfg, {})
    monkeypatch.setattr(onboarding, "reload_config", lambda: None)
    monkeypatch.setattr(onboarding, "get_onboarding_status", lambda: {"ok": True})
    onboarding.apply_onboarding_setup({"provider": "mistralai", "model": "mistral-large-latest", "confirm_overwrite": True})
    saved = onboarding._load_yaml_config(config_path)
    assert saved["model"]["provider"] == "mistral"


def test_live_models_adapts_only_final_agent_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "_get_cached_live_models", lambda _key: None)
    monkeypatch.setattr(routes, "_set_cached_live_models", lambda _key, _value: None)
    monkeypatch.setattr(routes, "j", lambda _handler, value: value)
    monkeypatch.setattr(routes, "_live_models_cache_key", lambda pid: pid)
    monkeypatch.setattr(routes, "_OPENAI_COMPAT_ENDPOINTS", {"mistral": "https://api.mistral.ai/v1"})
    monkeypatch.setattr(config, "get_config", lambda: {"model": {"provider": "mistralai"}})
    fake_models = SimpleNamespace(provider_model_ids=lambda provider: calls.append(provider) or ["mistral-small-latest"])
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.models", fake_models)
    payload = routes._handle_live_models(None, SimpleNamespace(query="provider=mistralai"))
    assert payload["provider"] == "mistral"
    assert calls == ["mistral"]


def test_live_models_uses_legacy_provider_credentials(monkeypatch):
    import json
    import urllib.request

    cfg = {
        "model": {"provider": "google"},
        "providers": {"mistralai": {"api_key": "legacy-mistral-key"}},
    }
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "mistral-live-model"}]}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        requests.append({"url": req.full_url, "authorization": req.headers.get("Authorization"), "timeout": timeout})
        return Response()

    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(routes, "_get_cached_live_models", lambda _key: None)
    monkeypatch.setattr(routes, "_set_cached_live_models", lambda _key, _value: None)
    monkeypatch.setattr(routes, "_live_models_cache_key", lambda pid: pid)
    monkeypatch.setattr(routes, "j", lambda _handler, value: value)
    monkeypatch.setattr(routes, "_OPENAI_COMPAT_ENDPOINTS", {"mistral": "https://api.mistral.ai/v1"})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _install_hermes_modules(monkeypatch, model_ids=[])

    payload = routes._handle_live_models(None, SimpleNamespace(query="provider=mistralai"))
    assert requests == [{
        "url": "https://api.mistral.ai/v1/models",
        "authorization": "Bearer legacy-mistral-key",
        "timeout": 8,
    }]
    assert payload["provider"] == "mistral"
    assert "mistral-live-model" in {model["id"] for model in payload["models"]}


def test_named_custom_catalog_inherits_shared_custom_provider_key(monkeypatch, tmp_path):
    import json
    import urllib.request

    import api.profiles as profiles

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        "model:\n  provider: custom:demo\n  base_url: https://proxy.example/v1\n"
        "providers:\n  custom:\n    api_key: shared-custom-key\n"
        "custom_providers:\n  - name: Demo\n    base_url: https://proxy.example/v1\n",
        encoding="utf-8",
    )
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "demo-live-model"}]}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.headers.get("Authorization"), timeout))
        return Response()

    for name in tuple(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN")) or name == "API_KEY":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr(config, "_thread_local_env_value", lambda _name, default="": default)
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(config, "_pool_entry_payloads", lambda _provider: [])
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _provider: False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _install_hermes_modules(monkeypatch)
    config.reload_config()
    config.invalidate_models_cache()

    catalog = config.get_available_models(force_refresh=True)
    group = next(group for group in catalog["groups"] if group.get("provider_id") == "custom:demo")
    assert calls == [("https://proxy.example/v1/models", "Bearer shared-custom-key", 5.0)]
    assert "demo-live-model" in {model["id"] for model in group["models"]}


def test_named_custom_catalog_prefers_own_key_over_shared_custom_key(monkeypatch, tmp_path):
    import json
    import urllib.request

    import api.profiles as profiles

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        "model:\n  provider: custom:demo\n  base_url: https://proxy.example/v1\n"
        "providers:\n  custom:\n    api_key: shared-custom-key\n"
        "custom_providers:\n  - name: Demo\n    api_key: named-custom-key\n"
        "    base_url: https://proxy.example/v1\n",
        encoding="utf-8",
    )
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "named-live-model"}]}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.headers.get("Authorization"), timeout))
        return Response()

    for name in tuple(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN")) or name == "API_KEY":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr(config, "_thread_local_env_value", lambda _name, default="": default)
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(config, "_pool_entry_payloads", lambda _provider: [])
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _provider: False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _install_hermes_modules(monkeypatch)
    config.reload_config()
    config.invalidate_models_cache()

    catalog = config.get_available_models(force_refresh=True)
    group = next(group for group in catalog["groups"] if group.get("provider_id") == "custom:demo")
    assert calls == [("https://proxy.example/v1/models", "Bearer named-custom-key", 5.0)]
    assert "named-live-model" in {model["id"] for model in group["models"]}


def test_inactive_named_custom_catalog_inherits_shared_custom_provider_key(monkeypatch, tmp_path):
    import json
    import urllib.request

    import api.profiles as profiles

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        "model:\n  provider: google\n  default: gemini-2.5-pro\n"
        "providers:\n  google:\n    api_key: google-key\n"
        "  custom:\n    api_key: shared-custom-key\n"
        "custom_providers:\n  - name: Demo\n    base_url: https://proxy.example/v1\n",
        encoding="utf-8",
    )
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "inactive-live-model"}]}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.headers.get("Authorization"), timeout))
        return Response()

    for name in tuple(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN")) or name == "API_KEY":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr(config, "_thread_local_env_value", lambda _name, default="": default)
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(config, "_pool_entry_payloads", lambda _provider: [])
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _provider: False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _install_hermes_modules(monkeypatch)
    config.reload_config()
    config.invalidate_models_cache()

    catalog = config.get_available_models(force_refresh=True)
    group = next(group for group in catalog["groups"] if group.get("provider_id") == "custom:demo")
    assert calls == [("https://proxy.example/v1/models", "Bearer shared-custom-key", 5.0)]
    assert "@custom:demo:inactive-live-model" in {model["id"] for model in group["models"]}


def test_catalog_endpoint_uses_canonical_equivalent_key_not_custom_key(monkeypatch, tmp_path):
    import json
    import urllib.request

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        "model:\n  provider: mistralai\n  default: mistral-large-latest\n"
        "  base_url: https://proxy.example/v1\n"
        "providers:\n  mistralai:\n    api_key: legacy-mistral-key\n"
        "  custom:\n    api_key: wrong-custom-key\n",
        encoding="utf-8",
    )
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": []}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.headers.get("Authorization"), timeout))
        return Response()

    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _install_hermes_modules(monkeypatch)
    config.reload_config()
    config.invalidate_models_cache()

    config.get_available_models(force_refresh=True)
    mistral_calls = [
        call for call in calls if call[0] == "https://proxy.example/v1/models"
    ]
    assert mistral_calls == [(
        "https://proxy.example/v1/models",
        "Bearer legacy-mistral-key",
        5.0,
    )]
    assert all(authorization != "Bearer wrong-custom-key" for _, authorization, _ in calls)


def test_live_models_never_forwards_another_provider_key(monkeypatch):
    cfg = {"model": {"provider": "google", "api_key": "google-secret"}, "providers": {"mistral": {}}}
    assert config._canonicalise_provider_id(cfg["model"]["provider"]) != "mistral"
    assert config._canonical_provider_config(cfg, "mistral").get("api_key") is None


def test_custom_default_save_preserves_paired_provider_identity(monkeypatch, tmp_path):
    import yaml

    cfgfile = tmp_path / "config.yaml"
    original = {
        "model": {
            "provider": "custom:my_proxy",
            "base_url": "https://underscore.example/v1",
        },
        "custom_providers": [
            {"name": "my_proxy", "base_url": "https://underscore.example/v1", "api_key": "underscore-key"},
            {"name": "my-proxy", "base_url": "https://hyphen.example/v1", "api_key": "hyphen-key"},
        ],
    }
    cfgfile.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda model: (model, "custom:my_proxy", None),
    )
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

    result = config.set_hermes_default_model("vendor/model", provider="custom:my_proxy")
    saved = yaml.safe_load(cfgfile.read_text(encoding="utf-8"))

    assert result["provider"] == "custom:my_proxy"
    assert saved["model"]["provider"] == "custom:my_proxy"
    assert saved["model"]["base_url"] == "https://underscore.example/v1"


def test_custom_live_handler_preserves_paired_provider_identity(monkeypatch):
    import json
    import urllib.request
    from urllib.parse import urlparse

    import api.profiles as profiles

    cfg = {
        "custom_providers": [
            {
                "name": "my_proxy",
                "base_url": "https://underscore.example/v1",
                "api_key": "underscore-key",
                "models": ["underscore-model"],
            },
            {
                "name": "my-proxy",
                "base_url": "https://hyphen.example/v1",
                "api_key": "hyphen-key",
                "models": ["hyphen-model"],
            },
        ]
    }
    dispatched = []
    requests = []

    class Response:
        def __init__(self, model_id):
            self.model_id = model_id

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": self.model_id}]}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        requests.append((req.full_url, req.headers.get("Authorization"), timeout))
        model_id = "underscore-model" if "underscore.example" in req.full_url else "hyphen-model"
        return Response(model_id)

    _install_hermes_modules(monkeypatch)
    sys.modules["hermes_cli.models"].provider_model_ids = (
        lambda provider: dispatched.append(provider) or []
    )
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "proof-profile")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    routes._clear_live_models_cache()
    try:
        parsed = urlparse("/api/models/live?provider=custom:my_proxy")
        first = routes._handle_live_models(object(), parsed)
        second = routes._handle_live_models(object(), parsed)

        assert dispatched == ["custom:my_proxy"]
        assert requests == [(
            "https://underscore.example/v1/models",
            "Bearer underscore-key",
            routes.CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS,
        )]
        assert first == second
        assert first["provider"] == "custom:my_proxy"
        assert [model["id"] for model in first["models"]] == ["underscore-model"]
        assert ("proof-profile", "custom:my_proxy") in routes._LIVE_MODELS_CACHE
        assert ("proof-profile", "custom:my-proxy") not in routes._LIVE_MODELS_CACHE
    finally:
        routes._clear_live_models_cache()


def test_custom_provider_key_lookup_and_recovery_keep_exact_identity(monkeypatch):
    from datetime import datetime, timedelta, timezone

    cfg = {
        "custom_providers": [
            {"name": "my_proxy", "api_key": "underscore-replacement-key"},
            {"name": "my-proxy", "api_key": "hyphen-key"},
        ]
    }
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    monkeypatch.setattr(providers, "_load_env_file", lambda _path: {})
    monkeypatch.setattr(providers, "_thread_local_env_value", lambda _name, default="": default)
    monkeypatch.setattr(providers, "_pool_entry_payloads", lambda provider: [
        {
            "last_status": "exhausted",
            "last_status_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            "last_error_code": "401",
            "secret_fingerprint": providers._credential_secret_fingerprint("underscore-old-key"),
        }
    ] if provider == "custom:my_proxy" else [])

    assert providers._get_provider_api_key("custom:my_proxy") == "underscore-replacement-key"
    assert providers._provider_has_key("custom:my_proxy")
    assert not providers.provider_has_usable_pool_credential("custom:my_proxy")
    assert providers.provider_has_process_wakeup_recovery_credential("custom:my_proxy")

    cfg["custom_providers"] = [cfg["custom_providers"][0]]
    assert providers._get_provider_api_key("custom:my_proxy") == "underscore-replacement-key"
    assert providers._provider_has_key("custom:my_proxy")


@pytest.mark.parametrize(
    "provider_order,legacy_shape,canonical_shape",
    [
        (("mistralai", "mistral"), "list", "dict"),
        (("mistral", "mistralai"), "list", "dict"),
        (("mistralai", "mistral"), "dict", "list"),
        (("mistral", "mistralai"), "dict", "list"),
    ],
)
def test_mixed_mistral_model_shapes_feed_catalogs_and_cards(
    monkeypatch, tmp_path, provider_order, legacy_shape, canonical_shape
):
    import yaml

    def model_shape(shape, label, unique_id):
        rows = [
            {"id": "shared-model", "label": label},
            {"id": unique_id, "label": f"{label} only"},
        ]
        if shape == "list":
            return rows
        return {
            row["id"]: {"label": row["label"], "context_length": 8192}
            for row in rows
        }

    provider_models = {
        "mistralai": model_shape(legacy_shape, "Legacy label", "legacy-only"),
        "mistral": model_shape(canonical_shape, "Canonical label", "canonical-only"),
    }
    ordered_providers = {provider: {"models": provider_models[provider]} for provider in provider_order}
    cfg = {"model": {"provider": "mistral", "default": "shared-model"}, "providers": ordered_providers}
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(config, "_thread_local_env_value", lambda _name, default="": default)
    _install_hermes_modules(monkeypatch)
    config.reload_config()
    config.invalidate_models_cache()

    merged = config._canonical_provider_config(config.get_config(), "mistral")
    assert set(config._configured_model_ids(merged["models"])) == {
        "shared-model", "legacy-only", "canonical-only"
    }
    merged_options = config._configured_model_options(merged["models"])
    assert next(row["label"] for row in merged_options if row["id"] == "shared-model") == "Canonical label"
    if canonical_shape == "dict":
        canonical_only = next(row for row in merged["models"] if row["id"] == "canonical-only")
        assert canonical_only["context_length"] == 8192

    static_catalog = config._static_models_catalog_without_live_probes()
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0)
    live_catalog = config.get_available_models(force_refresh=True)

    def catalog_options(catalog):
        group = next(group for group in catalog["groups"] if group.get("provider_id") == "mistral")
        options = group["models"]
        ids = [model["id"].split(":")[-1] for model in options]
        assert all(ids.count(model_id) == 1 for model_id in ("shared-model", "legacy-only", "canonical-only"))
        return dict(zip(ids, options))

    for catalog in (static_catalog, live_catalog):
        options = catalog_options(catalog)
        assert {"shared-model", "legacy-only", "canonical-only"}.issubset(options)
        assert options["shared-model"]["label"] == "Canonical label"

    monkeypatch.setattr(providers, "get_config", config.get_config)
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(providers, "_get_cached_providers", lambda _key: None)
    monkeypatch.setattr(providers, "_store_cached_providers", lambda _key, value: value)
    monkeypatch.setattr(providers, "plugin_model_provider_ids", lambda: [])
    monkeypatch.setattr(providers, "is_plugin_model_provider", lambda _pid: False)
    monkeypatch.setattr(providers, "_provider_is_oauth", lambda _pid: False)
    monkeypatch.setattr(providers, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setattr(providers, "_read_visible_codex_cache_model_ids", lambda: [])
    monkeypatch.setattr(providers, "_models_from_live_provider_ids", lambda _pid, _ids: [])
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda _pid: False)
    card = next(card for card in providers.get_providers()["providers"] if card["id"] == "mistral")
    card_model_ids = [model["id"] for model in card["models"]]
    assert all(isinstance(model_id, str) for model_id in card_model_ids)
    assert all(card_model_ids.count(model_id) == 1 for model_id in ("shared-model", "legacy-only", "canonical-only"))
    card_models = {model["id"]: model for model in card["models"]}
    assert {"shared-model", "legacy-only", "canonical-only"}.issubset(card_models)
    assert card_models["shared-model"]["label"] == "Canonical label"
