"""Regression tests for #1527/#1530 LM Studio base_url ownership.

When a local OpenAI-compatible endpoint is configured as LM Studio, model
discovery must trust the configured provider before guessing from the URL host.
LAN IPs, Tailscale names, and reverse proxies do not contain "lmstudio" in the
hostname, but the config block already says which provider owns that base_url.
"""

from __future__ import annotations

import json
import socket
import urllib.request

import pytest

import api.config as config
import api.profiles as profiles


_API_KEY_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GLM_API_KEY",
    "KIMI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_GO_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "LM_API_KEY",
    "LMSTUDIO_API_KEY",
    "OLLAMA_API_KEY",
    "LOCAL_API_KEY",
    "API_KEY",
)


class _ModelsResponse:
    def __init__(self, model_ids: list[str]):
        self._model_ids = model_ids

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps({"data": [{"id": mid} for mid in self._model_ids]}).encode()


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    for var in _API_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    config.invalidate_models_cache()
    yield
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config.invalidate_models_cache()


def _write_config(tmp_path, monkeypatch, text: str) -> None:
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    config.reload_config()
    config.invalidate_models_cache()


def _mock_model_discovery(monkeypatch, model_ids: list[str], resolved_ip: str) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _ModelsResponse(model_ids),
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved_ip, 0))
        ],
    )


def _groups_by_id() -> dict[str, dict]:
    return {
        group["provider_id"]: group
        for group in config.get_available_models()["groups"]
    }


@pytest.mark.parametrize(
    ("base_url", "resolved_ip"),
    [
        ("http://192.168.1.22:1234/v1", "192.168.1.22"),
        ("http://my-mac.tailnet.example:1234/v1", "192.168.1.22"),
        ("https://lm.internal.example.com/v1", "192.168.1.22"),
    ],
)
def test_lmstudio_configured_base_url_keeps_discovered_models(
    tmp_path,
    monkeypatch,
    base_url: str,
    resolved_ip: str,
):
    _write_config(
        tmp_path,
        monkeypatch,
        f"""
model:
  provider: lmstudio
  default: qwen3.6-35b-a3b@q6_k
  base_url: {base_url}
providers:
  lmstudio:
    api_key: local-key
""",
    )
    _mock_model_discovery(
        monkeypatch,
        ["qwen3.6-35b-a3b@q6_k", "second-lmstudio-model"],
        resolved_ip,
    )

    groups = _groups_by_id()
    assert "custom" not in groups
    assert "lmstudio" in groups
    model_ids = {model["id"] for model in groups["lmstudio"]["models"]}
    assert {"qwen3.6-35b-a3b@q6_k", "second-lmstudio-model"} <= model_ids


def test_custom_configured_base_url_is_not_reclassified_as_ollama(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        monkeypatch,
        """
model:
  provider: custom
  default: custom-model
  base_url: http://localhost:4000/v1
providers:
  custom:
    api_key: local-key
""",
    )
    _mock_model_discovery(monkeypatch, ["custom-model", "custom-extra"], "127.0.0.1")

    groups = _groups_by_id()
    assert "ollama" not in groups
    assert "custom" in groups
    model_ids = {model["id"] for model in groups["custom"]["models"]}
    assert {"custom-model", "custom-extra"} <= model_ids


def test_lmstudio_session_model_resolves_to_configured_base_url(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        monkeypatch,
        """
model:
  provider: lmstudio
  default: qwen3.6-35b-a3b@q6_k
  base_url: http://192.168.1.22:1234/v1
providers:
  lmstudio:
    api_key: local-key
""",
    )

    model, provider, base_url = config.resolve_model_provider(
        "qwen3.6-35b-a3b@q6_k"
    )

    assert model == "qwen3.6-35b-a3b@q6_k"
    assert provider == "lmstudio"
    assert base_url == "http://192.168.1.22:1234/v1"
