"""Regression tests for credential_pool provider detection in /api/models."""

import json
import sys
import types

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline."""
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: (
        ["gpt-oss:20b", "qwen3:30b-a3b"] if pid == "ollama-cloud" else []
    )

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)


def _call_get_available_models(monkeypatch, tmp_path, auth_payload):
    """Call get_available_models() with auth.json pinned to a temp Hermes home."""
    _install_fake_hermes_cli(monkeypatch)

    (tmp_path / "auth.json").write_text(json.dumps(auth_payload), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        # Pin mtime to avoid reload_config() clobbering our in-memory cfg patch.
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


def test_ollama_cloud_manual_credential_shows_group(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "ollama-cloud": [
                {
                    "id": "abc123",
                    "label": "ollama-manual",
                    "source": "manual",
                    "auth_type": "api_key",
                    "base_url": "https://ollama.com/v1",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    providers = [g["provider"] for g in result.get("groups", [])]
    assert "Ollama Cloud" in providers, f"Expected Ollama Cloud in {providers}"


def test_copilot_gh_cli_only_credential_hidden(monkeypatch, tmp_path):
    auth_payload = {
        "version": 1,
        "providers": {},
        "active_provider": "openai-codex",
        "credential_pool": {
            "copilot": [
                {
                    "id": "def456",
                    "label": "gh auth token",
                    "source": "gh_cli",
                    "auth_type": "api_key",
                    "base_url": "https://api.githubcopilot.com",
                }
            ]
        },
    }

    result = _call_get_available_models(monkeypatch, tmp_path, auth_payload)
    providers = [g["provider"] for g in result.get("groups", [])]
    assert "GitHub Copilot" not in providers, (
        "GitHub Copilot should be hidden when only ambient gh auth token is present; "
        f"got {providers}"
    )
