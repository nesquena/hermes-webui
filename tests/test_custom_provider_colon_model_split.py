"""Regression test: custom provider IDs must not eat model-name colons.

When a custom provider (e.g. ``custom:litellm-proxy``) serves a model whose
name contains colons (e.g. ``qwen3.6:27b-vision``), the ``@provider:model``
dropdown value becomes ``@custom:litellm-proxy:qwen3.6:27b-vision``. The
``rsplit(":", 1)`` logic in ``resolve_model_provider()`` must correctly
separate the provider from the model, not concatenate part of the model name
into the provider ID.

This covers the colon-parsing family of bugs:
  - #1948: custom provider ids containing host:port
  - #2047: custom provider name with parentheses/port
  - The ``:free`` / ``:thinking`` suffix variant
"""
import pytest
from api.config import resolve_model_provider


@pytest.fixture
def custom_provider_cfg(monkeypatch):
    cfg = {
        "model": {
            "default": "qwen3.6:27b-vision",
            "provider": "custom:litellm-proxy",
            "base_url": "http://localhost:8000/v1",
            "api_key": "nokey",
        },
        "custom_providers": [
            {
                "name": "litellm-proxy",
                "base_url": "http://localhost:8000/v1",
                "model": "qwen3.6:27b-vision",
            },
        ],
    }
    monkeypatch.setattr("api.config.cfg", cfg)
    return cfg


@pytest.fixture
def host_port_cfg(monkeypatch):
    cfg = {
        "model": {
            "default": "Qwen3",
            "provider": "custom:10.8.71.41:8080",
            "base_url": "http://10.8.71.41:8080/v1",
        },
        "custom_providers": [
            {
                "name": "10.8.71.41:8080",
                "base_url": "http://10.8.71.41:8080/v1",
                "model": "Qwen3",
            },
        ],
    }
    monkeypatch.setattr("api.config.cfg", cfg)
    return cfg


class TestCustomProviderColonModelSplit:
    def test_simple_model_name(self, custom_provider_cfg):
        model, provider, _ = resolve_model_provider(
            "@custom:litellm-proxy:simple-model"
        )
        assert provider == "custom:litellm-proxy"
        assert model == "simple-model"

    def test_model_name_with_colon(self, custom_provider_cfg):
        model, provider, _ = resolve_model_provider(
            "@custom:litellm-proxy:qwen3.6:27b-vision"
        )
        assert provider == "custom:litellm-proxy"
        assert model == "qwen3.6:27b-vision"

    def test_model_name_with_free_suffix(self, custom_provider_cfg):
        model, provider, _ = resolve_model_provider(
            "@custom:litellm-proxy:qwen3.6:27b-vision:free"
        )
        assert provider == "custom:litellm-proxy"
        assert model == "qwen3.6:27b-vision:free"

    def test_model_name_with_nothink_suffix(self, custom_provider_cfg):
        model, provider, _ = resolve_model_provider(
            "@custom:litellm-proxy:qwen3.6:27b-nothink"
        )
        assert provider == "custom:litellm-proxy"
        assert model == "qwen3.6:27b-nothink"


class TestHostPortProviderSlug:
    def test_host_port_provider_simple_model(self, host_port_cfg):
        model, provider, _ = resolve_model_provider(
            "@custom:10.8.71.41:8080:Qwen3"
        )
        assert provider == "custom:10.8.71.41:8080"
        assert model == "Qwen3"


class TestNonCustomProviders:
    def test_openrouter_provider_qualified(self, custom_provider_cfg):
        model, provider, _ = resolve_model_provider(
            "@openrouter:anthropic/claude-sonnet-4.6"
        )
        assert provider == "openrouter"
        assert model == "anthropic/claude-sonnet-4.6"

    def test_openrouter_free_suffix(self, custom_provider_cfg):
        model, provider, _ = resolve_model_provider(
            "@openrouter:tencent/hy3-preview:free"
        )
        assert provider == "openrouter"
        assert model == "tencent/hy3-preview:free"
