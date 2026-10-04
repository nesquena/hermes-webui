"""Regression tests for #7176: shared base_url across custom_providers[].

A gateway fronting several LLM APIs on one physical base_url is declared as
several custom_providers[] entries sharing that base_url, one per api_mode.
_named_custom_provider_slug_for_base_url() did a plain first-match scan by
base_url, so every model routed through the FIRST-declared entry — producing
an opaque 401 for models owned by later entries.

The fix threads the model being resolved into the scan: the entry that
declares the model (exact `model:` or a `models:` allowlist key) outranks
declaration order; when no model is given (or no entry declares it) the
historical first-match stands.
"""

import pytest

import api.config as cfg_mod
from api.config import (
    _named_custom_provider_slug_for_base_url,
    resolve_model_provider,
)

SHARED_BASE_URL = "https://gateway.example/v1"


@pytest.fixture()
def shared_gateway_cfg():
    """Install the issue's fixture config and restore cfg afterwards."""
    old_model = cfg_mod.cfg.get("model")
    old_providers = cfg_mod.cfg.get("custom_providers")
    cfg_mod.cfg["model"] = {
        "default": "claude-sonnet-5@default",
        "provider": "custom",
        "base_url": SHARED_BASE_URL,
        "api_mode": "anthropic_messages",
    }
    cfg_mod.cfg["custom_providers"] = [
        {
            "name": "Gateway OpenAI Chat",  # declared FIRST — must not win
            "base_url": SHARED_BASE_URL,
            "api_mode": "chat_completions",
            "models": {"gpt-5": {}, "gpt-5-mini": {}},
        },
        {
            "name": "Gateway Claude",  # owns claude-sonnet-5@default
            "base_url": SHARED_BASE_URL,
            "api_mode": "anthropic_messages",
            "models": {
                "claude-sonnet-5@default": {},
                "claude-opus-5@default": {},
            },
        },
    ]
    yield cfg_mod.cfg
    cfg_mod.cfg["model"] = old_model
    if old_providers is None:
        cfg_mod.cfg.pop("custom_providers", None)
    else:
        cfg_mod.cfg["custom_providers"] = old_providers


class TestSlugForBaseUrlModelOwnership:
    def test_model_owner_outranks_declaration_order(self, shared_gateway_cfg):
        slug = _named_custom_provider_slug_for_base_url(
            SHARED_BASE_URL, shared_gateway_cfg, model_id="claude-sonnet-5@default"
        )
        assert slug == "custom:gateway-claude"

    def test_model_owned_by_first_entry_still_resolves_first(self, shared_gateway_cfg):
        slug = _named_custom_provider_slug_for_base_url(
            SHARED_BASE_URL, shared_gateway_cfg, model_id="gpt-5-mini"
        )
        assert slug == "custom:gateway-openai-chat"

    def test_no_model_id_keeps_historical_first_match(self, shared_gateway_cfg):
        slug = _named_custom_provider_slug_for_base_url(
            SHARED_BASE_URL, shared_gateway_cfg
        )
        assert slug == "custom:gateway-openai-chat"

    def test_unknown_model_falls_back_to_first_match(self, shared_gateway_cfg):
        slug = _named_custom_provider_slug_for_base_url(
            SHARED_BASE_URL, shared_gateway_cfg, model_id="unlisted-model"
        )
        assert slug == "custom:gateway-openai-chat"


class TestResolveModelProviderRouting:
    def test_configured_default_routes_to_owning_entry(self, shared_gateway_cfg):
        """The issue's exact repro: the configured default model must resolve
        to custom:gateway-claude, not the first-declared entry."""
        model, provider, base_url = resolve_model_provider("claude-sonnet-5@default")
        assert provider == "custom:gateway-claude"
        assert model == "claude-sonnet-5@default"
        assert base_url == SHARED_BASE_URL

    def test_singular_model_field_owns_too(self):
        old_model = cfg_mod.cfg.get("model")
        old_providers = cfg_mod.cfg.get("custom_providers")
        cfg_mod.cfg["model"] = {
            "default": "solo-1",
            "provider": "custom",
            "base_url": SHARED_BASE_URL,
        }
        cfg_mod.cfg["custom_providers"] = [
            {"name": "First", "base_url": SHARED_BASE_URL,
             "models": {"other": {}}},
            {"name": "Second", "base_url": SHARED_BASE_URL,
             "model": "solo-1"},
        ]
        try:
            slug = _named_custom_provider_slug_for_base_url(
                SHARED_BASE_URL, cfg_mod.cfg, model_id="solo-1"
            )
            assert slug == "custom:second"
        finally:
            cfg_mod.cfg["model"] = old_model
            if old_providers is None:
                cfg_mod.cfg.pop("custom_providers", None)
            else:
                cfg_mod.cfg["custom_providers"] = old_providers


class TestMinimalCatalogActiveProvider:
    def test_minimal_catalog_reports_owning_slug(self, shared_gateway_cfg):
        """_minimal_static_models_catalog forwards the effective default so the
        picker badge shows the owning entry, not the first-declared one."""
        from api import config as config_mod

        catalog = config_mod._minimal_static_models_catalog()
        assert catalog["active_provider"] == "custom:gateway-claude"
