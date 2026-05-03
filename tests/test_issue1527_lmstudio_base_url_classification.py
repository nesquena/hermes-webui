"""Regression tests for LM Studio base_url provider ownership (#1527)."""

from __future__ import annotations

import json
import socket
import sys
import types
from contextlib import contextmanager

import api.config as config


def _install_fake_hermes_cli(monkeypatch) -> None:
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda _pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)


def _clear_provider_env(monkeypatch) -> None:
    for key in (
        "ANTHROPIC_API_KEY",
        "API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GLM_API_KEY",
        "GOOGLE_API_KEY",
        "HERMES_API_KEY",
        "HERMES_OPENAI_API_KEY",
        "KIMI_API_KEY",
        "LOCAL_API_KEY",
        "LM_API_KEY",
        "LMSTUDIO_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY",
        "MISTRAL_API_KEY",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_ZEN_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@contextmanager
def _temp_config(monkeypatch, tmp_path, yaml_text: str):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    config.reload_config()
    config.invalidate_models_cache()
    try:
        yield
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


def _mock_models_endpoint(monkeypatch, model_id: str = "qwen3-27b") -> None:
    class Response:
        def read(self):
            return json.dumps(
                {"data": [{"id": model_id, "name": model_id}]}
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response())
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.22", 0))],
    )


def _available_models(monkeypatch, tmp_path, yaml_text: str) -> dict:
    _install_fake_hermes_cli(monkeypatch)
    _clear_provider_env(monkeypatch)
    _mock_models_endpoint(monkeypatch)
    with _temp_config(monkeypatch, tmp_path, yaml_text):
        return config.get_available_models()


def _models_for_provider(result: dict, provider_id: str) -> list[str]:
    for group in result["groups"]:
        if group.get("provider_id") == provider_id:
            return [model["id"] for model in group.get("models", [])]
    return []


def test_lmstudio_lan_ip_uses_configured_provider(monkeypatch, tmp_path):
    result = _available_models(
        monkeypatch,
        tmp_path,
        """
model:
  provider: lmstudio
  default: qwen3-27b
  base_url: http://192.168.1.22:1234/v1
providers: {}
""",
    )

    assert "qwen3-27b" in _models_for_provider(result, "lmstudio")
    assert not _models_for_provider(result, "custom")


def test_lmstudio_lan_ip_selection_resolves_to_configured_base_url(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch)
    _clear_provider_env(monkeypatch)
    _mock_models_endpoint(monkeypatch, model_id="qwen3.6-35b-a3b@q6_k")
    base_url = "http://192.168.1.22:1234/v1"

    with _temp_config(
        monkeypatch,
        tmp_path,
        f"""
model:
  provider: lmstudio
  default: qwen3.6-35b-a3b@q6_k
  base_url: {base_url}
providers: {{}}
""",
    ):
        result = config.get_available_models()
        models = _models_for_provider(result, "lmstudio")
        assert "qwen3.6-35b-a3b@q6_k" in models

        selected = config.model_with_provider_context(models[0], "lmstudio")
        model, provider, resolved_base_url = config.resolve_model_provider(selected)

    assert model == "qwen3.6-35b-a3b@q6_k"
    assert provider == "lmstudio"
    assert resolved_base_url == base_url


def test_lmstudio_tailscale_hostname_uses_configured_provider(monkeypatch, tmp_path):
    result = _available_models(
        monkeypatch,
        tmp_path,
        """
model:
  provider: lmstudio
  default: qwen3-27b
  base_url: http://my-mac.tailnet.ts.net:1234/v1
providers: {}
""",
    )

    assert "qwen3-27b" in _models_for_provider(result, "lmstudio")
    assert not _models_for_provider(result, "custom")


def test_lmstudio_reverse_proxy_uses_configured_provider(monkeypatch, tmp_path):
    result = _available_models(
        monkeypatch,
        tmp_path,
        """
model:
  provider: lmstudio
  default: qwen3-27b
  base_url: https://lm.internal.example.com/v1
providers: {}
""",
    )

    assert "qwen3-27b" in _models_for_provider(result, "lmstudio")
    assert not _models_for_provider(result, "custom")


def test_unclaimed_lan_ip_falls_back_to_custom(monkeypatch, tmp_path):
    result = _available_models(
        monkeypatch,
        tmp_path,
        """
model:
  default: qwen3-27b
  base_url: http://192.168.1.22:1234/v1
providers: {}
""",
    )

    assert "qwen3-27b" in _models_for_provider(result, "custom")
    assert not _models_for_provider(result, "lmstudio")


def test_hostname_fallback_still_detects_lmstudio(monkeypatch, tmp_path):
    result = _available_models(
        monkeypatch,
        tmp_path,
        """
model:
  default: qwen3-27b
  base_url: http://lmstudio.internal.example.com/v1
providers: {}
""",
    )

    assert "qwen3-27b" in _models_for_provider(result, "lmstudio")


def test_localhost_fallback_still_detects_ollama(monkeypatch, tmp_path):
    result = _available_models(
        monkeypatch,
        tmp_path,
        """
model:
  default: llama3.2
  base_url: http://localhost:11434/v1
providers: {}
""",
    )

    assert "qwen3-27b" in _models_for_provider(result, "ollama")


def test_configured_provider_matches_each_base_url_not_global_active():
    cfg = {
        "model": {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
        },
        "providers": {
            "ollama": {"base_url": "http://127.0.0.1:11434/v1"},
            "lmstudio": {"base_url": "http://192.168.1.22:1234/v1"},
        },
    }

    assert (
        config._configured_provider_for_base_url(
            "http://192.168.1.22:1234/v1", cfg, active_provider="ollama"
        )
        == "lmstudio"
    )
    assert (
        config._configured_provider_for_base_url(
            "http://127.0.0.1:11434/v1", cfg, active_provider="ollama"
        )
        == "ollama"
    )
