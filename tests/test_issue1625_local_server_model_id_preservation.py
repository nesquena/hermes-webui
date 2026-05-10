"""Tests for #1625: resolve_model_provider must NOT strip provider prefix on local servers.

Bug shape: with `model.provider: lmstudio`, `model.base_url: http://localhost:1234/v1`,
`model.default: qwen/qwen3.6-27b`, resolve_model_provider() stripped the
"qwen/" prefix because "qwen" matches an entry in _PROVIDER_MODELS — sending
the request to LM Studio with model name "qwen3.6-27b". LM Studio (and Ollama,
llama.cpp, vLLM, TabbyAPI) register models under their full HuggingFace-style
id, so the stripped name didn't match the loaded model and a fresh instance
loaded with default settings, ignoring the user's tuned context length /
parallel slots.

Fix: explicit no-strip path for known local-server providers AND any
base_url pointing at a loopback/private host. OpenAI-compatible proxies
(LiteLLM, OpenRouter relays) on public URLs continue to get prefix-stripping.
"""

import pytest

from api import config as cfg_mod


# ── Helpers ───────────────────────────────────────────────────────────────


def _patch_cfg(monkeypatch, custom_providers=None, **model_overrides):
    """Patch api.config.cfg to a synthetic config dict for the duration of a test."""
    fake_cfg = {
        "model": dict(model_overrides),
        "custom_providers": list(custom_providers or []),
    }
    monkeypatch.setattr(cfg_mod, "cfg", fake_cfg)


# ── Local-server providers preserve full model id ──────────────────────────


@pytest.mark.parametrize(
    "provider_name",
    [
        "lmstudio",
        "lm-studio",  # Opus pre-release NIT
        "ollama",
        "llamacpp",
        "llama-cpp",
        "vllm",
        "tabby",
        "tabbyapi",
        "koboldcpp",
        "textgen",
        "localai",  # Opus pre-release NIT
    ],
)
def test_known_local_server_provider_preserves_full_model_id(
    provider_name, monkeypatch
):
    """Known local-server provider names must preserve the slashed model id
    even when the prefix matches _PROVIDER_MODELS."""
    _patch_cfg(monkeypatch, provider=provider_name, base_url="http://localhost:1234/v1")
    model, provider, base_url = cfg_mod.resolve_model_provider("qwen/qwen3.6-27b")
    assert model == "qwen/qwen3.6-27b", (
        f"Local-server provider {provider_name!r} must preserve the full model id; "
        f"stripping it makes LM Studio/Ollama/etc. load a fresh instance with "
        f"default settings (#1625)."
    )
    assert provider == provider_name
    assert base_url == "http://localhost:1234/v1"


def test_lmstudio_with_huggingface_namespace_preserved(monkeypatch):
    """The reporter's exact case: lmstudio + qwen/qwen3.6-27b + localhost."""
    _patch_cfg(
        monkeypatch,
        provider="lmstudio",
        base_url="http://localhost:1234/v1",
        default="qwen/qwen3.6-27b",
    )
    model, provider, base_url = cfg_mod.resolve_model_provider("qwen/qwen3.6-27b")
    assert model == "qwen/qwen3.6-27b"


def test_lmstudio_with_openai_prefix_preserved(monkeypatch):
    """openai/gpt-oss-120b on LM Studio is a real HuggingFace id; the namespace
    is part of the registry key. Must not be stripped on local servers."""
    _patch_cfg(monkeypatch, provider="lmstudio", base_url="http://localhost:1234/v1")
    model, provider, base_url = cfg_mod.resolve_model_provider("openai/gpt-oss-120b")
    assert model == "openai/gpt-oss-120b", (
        "openai/gpt-oss-120b on LM Studio must preserve the full id (#1625)"
    )


@pytest.mark.parametrize(
    "provider_name",
    [
        "ollama",
        "lmstudio",
        "lm-studio",
        "vllm",
        "tabby",
    ],
)
def test_named_custom_local_server_provider_preserves_full_model_id_on_lan_host(
    provider_name,
    monkeypatch,
):
    """#1830: custom:<local-server> slugs must keep local-server no-strip semantics.

    Non-loopback hostnames like ollama.lan do not trigger the base_url local
    heuristic, so the provider-id check must recognize custom:<slug> directly.
    """
    _patch_cfg(
        monkeypatch,
        provider=provider_name,
        base_url="http://lan-host:1234/v1",
        default="qwen/qwen3.6-27b",
        custom_providers=[
            {
                "name": provider_name,
                "base_url": "http://lan-host:1234/v1",
                "api_key": "local-key",
            },
        ],
    )
    model, provider, base_url = cfg_mod.resolve_model_provider("qwen/qwen3.6-27b")
    assert model == "qwen/qwen3.6-27b"
    assert provider == f"custom:{provider_name}"
    assert base_url == "http://lan-host:1234/v1"


# ── Loopback / private-IP heuristic ───────────────────────────────────────


@pytest.mark.parametrize(
    "loopback_url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:1234/v1",
        "http://127.0.0.1:8080/openai",
        "http://10.0.0.5:8080/v1",  # private RFC1918
        "http://192.168.1.50:1234/v1",  # private RFC1918
        "http://172.16.0.10:8000/v1",  # private RFC1918
        "http://[::1]:1234/v1",  # IPv6 loopback
    ],
)
def test_loopback_base_url_preserves_full_model_id(loopback_url, monkeypatch):
    """Even with a generic `provider: custom` (or any non-local-server name),
    a base_url pointing at a loopback or private IP must preserve the model id —
    almost certainly a local model server."""
    _patch_cfg(monkeypatch, provider="custom", base_url=loopback_url)
    model, _, _ = cfg_mod.resolve_model_provider("qwen/qwen3.6-27b")
    assert model == "qwen/qwen3.6-27b", (
        f"Loopback/private base_url {loopback_url!r} must preserve the full model id (#1625)"
    )


# ── Backward compat: OpenAI-compatible proxies keep prefix-stripping ─────


def test_public_openai_proxy_still_strips_prefix(monkeypatch):
    """OpenAI-compatible proxies (LiteLLM, public OpenRouter relays) still get
    the strip behavior so 'openai/gpt-5.4' → 'gpt-5.4'."""
    _patch_cfg(
        monkeypatch, provider="openai", base_url="https://litellm.example.com/v1"
    )
    model, provider, base_url = cfg_mod.resolve_model_provider("openai/gpt-5.4")
    assert model == "gpt-5.4", (
        "Public-host openai/* on a non-loopback proxy must continue to strip prefix"
    )


def test_unknown_prefix_on_public_proxy_preserved(monkeypatch):
    """Unknown prefix (zai-org/GLM-5.1) on public proxy passes through full
    (the existing contract — stripping unknown prefixes caused model_not_found)."""
    _patch_cfg(
        monkeypatch, provider="openai", base_url="https://litellm.example.com/v1"
    )
    model, _, _ = cfg_mod.resolve_model_provider("zai-org/GLM-5.1")
    assert model == "zai-org/GLM-5.1"


def test_openrouter_passes_full_unaffected(monkeypatch):
    """OpenRouter always needs the full provider/model path — pre-existing
    contract that the local-server fix must not disturb."""
    _patch_cfg(monkeypatch, provider="openrouter")
    model, provider, _ = cfg_mod.resolve_model_provider("anthropic/claude-sonnet-4.6")
    assert model == "anthropic/claude-sonnet-4.6"
    assert provider == "openrouter"


# ── Helper unit tests ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://localhost:1234", True),
        ("http://127.0.0.1:1234", True),
        ("http://10.0.0.5", True),
        ("http://192.168.1.1:8080", True),
        ("http://[::1]:1234", True),
        ("http://example.com", False),
        ("https://api.openai.com/v1", False),
        ("https://litellm.example.com/v1", False),
        ("", False),
        (None, False),
        ("not-a-url", False),
    ],
)
def test_base_url_points_at_local_server_helper(url, expected):
    assert cfg_mod._base_url_points_at_local_server(url) is expected
