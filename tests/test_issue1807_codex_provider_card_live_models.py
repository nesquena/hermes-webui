"""Regression tests for #1807 -- Codex providers card uses live models."""

import sys
import types

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch, provider_model_ids):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = provider_model_ids

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda pid: {
        "logged_in": pid == "openai-codex",
        "key_source": "oauth",
    }

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)


def _configure_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr(config, "cfg", {
        "model": {"provider": "openai-codex", "default": "gpt-5.5"},
        "providers": {},
        "fallback_providers": [],
    })
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)


def _codex_provider():
    from api.providers import get_providers

    providers = get_providers()["providers"]
    return next(p for p in providers if p["id"] == "openai-codex")


def test_codex_provider_card_prefers_live_account_catalog(monkeypatch, tmp_path):
    live_codex_ids = [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.2",
    ]

    def provider_model_ids(pid):
        return live_codex_ids if pid == "openai-codex" else []

    _install_fake_hermes_cli(monkeypatch, provider_model_ids)
    _configure_codex(monkeypatch, tmp_path)

    codex = _codex_provider()
    ids = [m["id"] for m in codex["models"]]

    assert ids == live_codex_ids
    assert codex["models_total"] == len(live_codex_ids)
    assert "gpt-5.5-mini" not in ids
    assert "gpt-5.2-codex" not in ids
    assert "codex-mini-latest" not in ids


def test_codex_provider_card_keeps_static_fallback_when_live_catalog_empty(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch, lambda _pid: [])
    _configure_codex(monkeypatch, tmp_path)

    codex = _codex_provider()
    ids = [m["id"] for m in codex["models"]]

    assert "gpt-5.5-mini" in ids
    assert "codex-mini-latest" in ids
    assert codex["models_total"] == len(ids)
