"""Regression coverage for configured providers using ``key_env``."""

import json
import sys
import types
import urllib.error
import urllib.request

import api.config as config
import api.profiles as profiles
import api.providers as providers


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


class _ModelsResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"data": [{"id": "syn:large:text"}]}).encode()


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
    requests = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: (requests.append((request, timeout)) or _ModelsResponse()))
    try:
        result = config.get_available_models(force_refresh=True)
    finally:
        _restore(old_cfg, old_mtime)

    synthetic = next((group for group in result["groups"] if group["provider_id"] == "synthetic"), None)
    assert synthetic is not None, [group["provider_id"] for group in result["groups"]]
    assert any(model["id"].endswith("syn:large:text") for model in synthetic["models"]), (synthetic, requests)
    assert len(requests) == 1
    request, _timeout = requests[0]
    assert request.full_url == "https://api.synthetic.new/openai/v1/models"
    assert request.get_header("Authorization") == "Bearer synthetic-secret"


def test_provider_credential_contract_and_custom_delegate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_thread_local_env_value", lambda name: {"KEY": "env-secret"}.get(name, ""))
    assert config.resolve_provider_credential("literal", "KEY") == "literal"
    assert config.resolve_provider_credential("${KEY}", None) == "env-secret"
    assert config.resolve_provider_credential(None, "KEY") == "env-secret"

    monkeypatch.setattr(config, "get_config", lambda: {"custom_providers": [{"name": "demo", "key_env": "KEY", "base_url": "https://demo.test/v1"}]})
    assert config.resolve_custom_provider_connection("custom:demo") == ("env-secret", "https://demo.test/v1")

    monkeypatch.setattr(providers, "get_config", lambda: {"providers": {"synthetic": {"key_env": "KEY"}}})
    assert providers._provider_has_key("synthetic") is True
    assert providers._get_provider_api_key("synthetic") == "env-secret"

    monkeypatch.setattr(
        providers,
        "get_config",
        lambda: {"custom_providers": [{"name": "demo", "key_env": "KEY"}]},
    )
    assert providers._provider_has_key("custom:demo") is True
    assert providers._get_provider_api_key("custom:demo") == "env-secret"
    monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
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
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _ModelsResponse())
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
