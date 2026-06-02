"""Regression: self-hosted provider setup in Settings → Providers (#3260).

Post-onboarding users must be able to configure Ollama / LM Studio / custom
OpenAI-compatible endpoints from the WebUI without rerunning the wizard or
editing config.yaml by hand. Local Ollama must remain independent of
OLLAMA_API_KEY used for Ollama Cloud (#1410).
"""

from __future__ import annotations

import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import api.config as config
import api.profiles as profiles
from tests._pytest_port import BASE


def _install_fake_hermes_cli(monkeypatch):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text), e.code
        except Exception:
            return {"error": body_text}, e.code


def _isolate_writes(monkeypatch, tmp_path):
    from api import onboarding as ob
    from api import providers as prov

    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr(ob, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ob, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(prov, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LM_API_KEY", raising=False)
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return cfg_path


class TestSelfHostedProviderCatalog:
    def test_ollama_exposes_self_hosted_flags(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_writes(monkeypatch, tmp_path)
        from api.providers import get_providers

        by_id = {p["id"]: p for p in get_providers()["providers"]}
        assert by_id["ollama"]["is_self_hosted"] is True
        assert by_id["ollama"]["configurable"] is False
        assert by_id["ollama"]["key_optional"] is True
        assert by_id["ollama"]["requires_base_url"] is True

    def test_lmstudio_uses_self_hosted_card_not_api_key_only(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_writes(monkeypatch, tmp_path)
        from api.providers import get_providers

        by_id = {p["id"]: p for p in get_providers()["providers"]}
        assert by_id["lmstudio"]["is_self_hosted"] is True
        assert by_id["lmstudio"]["configurable"] is False

    def test_ollama_cloud_still_api_key_configurable(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_writes(monkeypatch, tmp_path)
        from api.providers import get_providers

        by_id = {p["id"]: p for p in get_providers()["providers"]}
        assert not by_id["ollama-cloud"].get("is_self_hosted")
        assert by_id["ollama-cloud"]["configurable"] is True


class TestIssue1410OllamaCloudIndependence:
    def test_cloud_env_var_does_not_mark_local_self_hosted_configured(
        self, monkeypatch, tmp_path,
    ):
        _install_fake_hermes_cli(monkeypatch)
        cfg_path = _isolate_writes(monkeypatch, tmp_path)
        cfg_path.write_text("model:\n  provider: anthropic\n  default: claude-sonnet-4.6\n", encoding="utf-8")
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic", "default": "claude-sonnet-4.6"}
        monkeypatch.setenv("OLLAMA_API_KEY", "sk-cloud-only")

        from api.providers import get_providers

        by_id = {p["id"]: p for p in get_providers()["providers"]}
        assert by_id["ollama"]["has_key"] is False
        assert by_id["ollama"]["self_hosted_configured"] is False
        assert by_id["ollama-cloud"]["has_key"] is True


class TestApplySelfHostedProviderSetup:
    def test_apply_configures_ollama_without_onboarding_gate(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        cfg_path = _isolate_writes(monkeypatch, tmp_path)
        cfg_path.write_text(
            "model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4.6\n",
            encoding="utf-8",
        )
        config.cfg.clear()
        config.cfg["model"] = {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}

        from api.providers import apply_self_hosted_provider_setup

        body = apply_self_hosted_provider_setup(
            {
                "provider": "ollama",
                "model": "qwen3:32b",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": "",
            },
        )
        assert body.get("ok") is True
        assert body.get("provider") == "ollama"

        saved = config._load_yaml_config_file(cfg_path)
        assert saved["model"]["provider"] == "ollama"
        assert saved["model"]["default"] == "qwen3:32b"
        assert saved["model"]["base_url"] == "http://127.0.0.1:11434/v1"
        assert not (tmp_path / ".env").exists() or "OLLAMA_API_KEY" not in (tmp_path / ".env").read_text()

        from api.providers import get_providers

        by_id = {p["id"]: p for p in get_providers()["providers"]}
        assert by_id["ollama"]["self_hosted_configured"] is True
        assert by_id["ollama"]["has_key"] is False

    def test_rejects_unknown_provider(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_writes(monkeypatch, tmp_path)
        from api.providers import apply_self_hosted_provider_setup

        body = apply_self_hosted_provider_setup(
            {"provider": "openrouter", "model": "x", "base_url": "http://localhost/v1"},
        )
        assert body.get("ok") is False
        assert "unsupported" in body.get("error", "").lower()

    def test_requires_model(self, monkeypatch, tmp_path):
        _install_fake_hermes_cli(monkeypatch)
        _isolate_writes(monkeypatch, tmp_path)
        from api.providers import apply_self_hosted_provider_setup

        body = apply_self_hosted_provider_setup(
            {"provider": "ollama", "base_url": "http://127.0.0.1:11434/v1"},
        )
        assert body.get("ok") is False
        assert "model" in body.get("error", "").lower()
