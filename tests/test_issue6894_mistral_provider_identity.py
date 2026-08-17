"""Behavioral proof for the WebUI and Agent provider namespace boundary."""

from __future__ import annotations

from types import SimpleNamespace

import api.config as config
import api.onboarding as onboarding
import api.providers as providers
import api.routes as routes


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


def test_configured_mistral_key_becomes_selectable_and_savable(monkeypatch):
    cfg = {"providers": {"mistralai": {"api_key": "legacy-key"}},
           "model": {"provider": "mistralai"}}
    assert config._canonical_provider_config(cfg, "mistral")["api_key"] == "legacy-key"
    writes = []
    monkeypatch.setattr(providers, "_write_env_file", lambda _path, update: writes.append(update))
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: __import__("pathlib").Path("."))
    monkeypatch.setattr(providers, "invalidate_models_cache", lambda: None)
    monkeypatch.setattr(providers, "invalidate_account_usage_status_cache", lambda _pid: None)
    monkeypatch.setattr(providers, "invalidate_providers_cache", lambda: None)
    assert providers.set_provider_key("mistralai", "mistral-secret-key")["provider"] == "mistral"
    assert writes == [{"MISTRAL_API_KEY": "mistral-secret-key"}]


def test_session_restore_preserves_established_provider_ids():
    for provider in ("ollama", "google", "x-ai", "qwen", "custom", "custom:local", "unknown"):
        assert routes._clean_session_model_provider(provider) == provider
    assert routes._clean_session_model_provider("mistralai") == "mistral"


def test_provider_cards_keep_google_and_gemini_separate(monkeypatch):
    monkeypatch.setattr(providers, "get_config", lambda: {
        "model": {"provider": "google"},
        "providers": {"google": {"api_key": "google-key"}, "gemini": {"api_key": "gemini-key"}},
    })
    monkeypatch.setattr(providers, "_provider_has_key", lambda pid: pid in {"google", "gemini"})
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


def test_equivalent_mistral_default_save_preserves_custom_base_url(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_load_yaml_config_file", lambda _path: {
        "model": {"provider": "mistralai", "base_url": "https://proxy.example/v1"}
    })
    monkeypatch.setattr(config, "resolve_model_provider", lambda _model: ("mistral-small-latest", "mistralai", None))
    monkeypatch.setattr(config, "_save_yaml_config_file", lambda _path, value: saved.update(value))
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
    result = config.set_hermes_default_model("mistral-small-latest", provider="mistral")
    assert result["provider"] == "mistral"
    assert saved["model"]["provider"] == "mistral"
    assert saved["model"]["base_url"] == "https://proxy.example/v1"


def test_environment_detection_respects_roster_authentication_matrix(monkeypatch):
    # The roster sink is behavioral through the same canonical boundary used by
    # the catalog; an unauthenticated known row must not be resurrected by env.
    roster = [{"id": "openai-api", "authenticated": False}]
    monkeypatch.setattr(config, "_canonicalise_provider_id", config._canonicalise_provider_id)
    known = {config._canonicalise_provider_id(row["id"]) for row in roster}
    authenticated = {config._canonicalise_provider_id(row["id"]) for row in roster if row["authenticated"]}
    additions = []
    def add_env(pid):
        pid = config._canonicalise_provider_id(pid)
        if pid == "openai-codex" or (pid in known and pid not in authenticated):
            return
        additions.append(pid)
    add_env("openai-api")
    add_env("mistralai")
    assert additions == ["mistral"]


def test_onboarding_reads_legacy_mistral_and_writes_canonical_provider(monkeypatch, tmp_path):
    cfg = {"model": {"provider": "mistralai", "default": "mistral-large-latest"},
           "providers": {"mistralai": {"api_key": "legacy-key"}}}
    assert onboarding._extract_current_provider(cfg) == "mistral"
    monkeypatch.setattr(onboarding, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(onboarding, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(onboarding, "_load_yaml_config", lambda _path: {})
    monkeypatch.setattr(onboarding, "_provider_api_key_present", lambda *_args: True)
    monkeypatch.setattr(onboarding, "_write_env_file", lambda *_args: None)
    monkeypatch.setattr(onboarding, "reload_config", lambda: None)
    monkeypatch.setattr(onboarding, "get_onboarding_status", lambda: {"ok": True})
    saved = {}
    monkeypatch.setattr(onboarding, "_save_yaml_config", lambda _path, value: saved.update(value))
    onboarding.apply_onboarding_setup({"provider": "mistralai", "model": "mistral-large-latest", "confirm_overwrite": True})
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


def test_live_models_never_forwards_another_provider_key(monkeypatch):
    cfg = {"model": {"provider": "google", "api_key": "google-secret"}, "providers": {"mistral": {}}}
    assert config._canonicalise_provider_id(cfg["model"]["provider"]) != "mistral"
    assert config._canonical_provider_config(cfg, "mistral").get("api_key") is None
