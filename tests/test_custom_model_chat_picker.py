"""Regression coverage for WebUI-managed providers in the chat model picker."""

from pathlib import Path
import sys
import types

import pytest

from api import config


@pytest.fixture(autouse=True)
def _isolate_models_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


def _catalog(
    monkeypatch,
    providers: dict,
    *,
    active_provider: str = "openai",
    default_model: str = "gpt-4o-mini",
) -> dict:
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {"id": "openai", "authenticated": True}
    ]
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _provider: {"key_source": "config_yaml"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": active_provider, "default": default_model},
            "providers": {
                "openai": {"models": ["gpt-4o-mini"]},
                **providers,
            },
        },
        raising=False,
    )
    monkeypatch.setattr(config, "_cfg_has_in_memory_overrides", lambda: True)
    monkeypatch.setattr(
        config,
        "_get_auth_store_path",
        lambda: Path("/tmp/hermes-webui-missing-auth-custom-picker.json"),
    )
    return config.get_available_models()


def test_webui_managed_modern_custom_providers_appear_in_chat_picker(monkeypatch):
    payload = _catalog(
        monkeypatch,
        {
            "custom:deepseek": {
                "name": "Black and White (DeepSeek)",
                "api": "https://router.example/v1",
                "transport": "chat_completions",
                "default_model": "deepseek-ai/deepseek-v4-flash-0731",
                "models": {"deepseek-ai/deepseek-v4-flash-0731": {}},
                "key_env": "DEEPSEEK_ROUTER_KEY",
            },
            "custom:gemini": {
                "name": "Black and White (Gemini)",
                "api": "https://router.example/v1",
                "transport": "chat_completions",
                "default_model": "gemini-2.5-pro",
                "models": {
                    "gemini-2.5-pro": {},
                    "gemini-3.7-flash-high": {},
                },
                "key_env": "GEMINI_ROUTER_KEY",
            },
        },
    )

    groups = {group["provider_id"]: group for group in payload["groups"]}
    assert groups["custom:deepseek"]["provider"] == "Black and White (DeepSeek)"
    assert [row["id"] for row in groups["custom:deepseek"]["models"]] == [
        "@custom:deepseek:deepseek-ai/deepseek-v4-flash-0731"
    ]
    assert groups["custom:gemini"]["provider"] == "Black and White (Gemini)"
    assert [row["id"] for row in groups["custom:gemini"]["models"]] == [
        "@custom:gemini:gemini-2.5-pro",
        "@custom:gemini:gemini-3.7-flash-high",
    ]

    fallback_groups = {
        group["provider_id"]: group
        for group in config._static_models_catalog_without_live_probes()["groups"]
    }
    assert fallback_groups["custom:deepseek"] == groups["custom:deepseek"]
    assert fallback_groups["custom:gemini"] == groups["custom:gemini"]


def test_active_webui_managed_custom_provider_keeps_its_exact_model_id(monkeypatch):
    payload = _catalog(
        monkeypatch,
        {
            "custom:deepseek": {
                "name": "Black and White (DeepSeek)",
                "api": "https://router.example/v1",
                "transport": "chat_completions",
                "default_model": "deepseek-ai/deepseek-v4-flash-0731",
                "models": {"deepseek-ai/deepseek-v4-flash-0731": {}},
                "key_env": "DEEPSEEK_ROUTER_KEY",
            }
        },
        active_provider="custom:deepseek",
        default_model="deepseek-ai/deepseek-v4-flash-0731",
    )

    group = next(
        group
        for group in payload["groups"]
        if group["provider_id"] == "custom:deepseek"
    )
    assert [row["id"] for row in group["models"]] == [
        "deepseek-ai/deepseek-v4-flash-0731"
    ]
    fallback_group = next(
        group
        for group in config._static_models_catalog_without_live_probes()["groups"]
        if group["provider_id"] == "custom:deepseek"
    )
    assert [row["id"] for row in fallback_group["models"]] == [
        "deepseek-ai/deepseek-v4-flash-0731"
    ]


def test_disabled_webui_managed_custom_provider_stays_out_of_chat_picker(monkeypatch):
    payload = _catalog(
        monkeypatch,
        {
            "custom:disabled": {
                "name": "Disabled endpoint",
                "api": "https://disabled.example/v1",
                "transport": "chat_completions",
                "default_model": "disabled-model",
                "models": {"disabled-model": {}},
                "key_env": "DISABLED_ROUTER_KEY",
                "enabled": False,
            }
        },
    )

    provider_ids = {group["provider_id"] for group in payload["groups"]}
    assert "custom:disabled" not in provider_ids
    fallback_provider_ids = {
        group["provider_id"]
        for group in config._static_models_catalog_without_live_probes()["groups"]
    }
    assert "custom:disabled" not in fallback_provider_ids
