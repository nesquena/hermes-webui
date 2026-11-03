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

import json
import urllib.request

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
        """The issue's exact repro end to end. resolve_model_provider already
        scanned for ownership on master; the provider-id resolver the catalog
        builders use did not, so assert on it too."""
        model, provider, base_url = resolve_model_provider("claude-sonnet-5@default")
        assert provider == "custom:gateway-claude"
        assert model == "claude-sonnet-5@default"
        assert base_url == SHARED_BASE_URL
        assert cfg_mod._resolve_configured_provider_id(
            "custom", shared_gateway_cfg, base_url=SHARED_BASE_URL,
            resolve_alias=False, model_id="claude-sonnet-5@default",
        ) == "custom:gateway-claude"

    @pytest.mark.parametrize(("discover", "owner"), [
        (True, "custom:gateway-claude"),   # discovered catalog is not ownership
        (False, "custom:gateway-openai-chat"),  # re-pinned mapping still is
    ])
    def test_discovered_catalog_does_not_claim_ownership(
        self, shared_gateway_cfg, discover, owner
    ):
        first = shared_gateway_cfg["custom_providers"][0]
        first["models"]["claude-sonnet-5@default"] = {}
        first["models_discovered"] = True
        if not discover:
            first["discover_models"] = False
        assert _named_custom_provider_slug_for_base_url(
            SHARED_BASE_URL, shared_gateway_cfg, model_id="claude-sonnet-5@default"
        ) == owner
        assert cfg_mod._minimal_static_models_catalog()["active_provider"] == owner

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


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("own_key, own_auth", [("e2-key", "Bearer e2-key"), ("", "")])
def test_shared_url_entry_with_own_key_keeps_its_live_models(monkeypatch, own_key, own_auth):
    """model.api_key belongs to entry one while entry two owns the default:
    the active probe (run with model.api_key) must not stand in for entry
    two's own probe, including an anonymous one when entry two has no key."""
    catalogs = {"Bearer e1-key": ["gpt-x"], own_auth: ["claude-default", "claude-live"]}

    def fake_urlopen(req, timeout=None):
        ids = catalogs.get(req.get_header("Authorization") or "", [])
        return _FakeResponse({"data": [{"id": i} for i in ids]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        cfg_mod.socket, "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    monkeypatch.setitem(cfg_mod.cfg, "model", {
        "default": "claude-default", "provider": "custom",
        "base_url": SHARED_BASE_URL, "api_key": "e1-key",
    })
    monkeypatch.setitem(cfg_mod.cfg, "custom_providers", [
        {"name": "Gateway OpenAI", "base_url": SHARED_BASE_URL,
         "api_key": "e1-key", "models": {"gpt-x": {}}},
        {"name": "Gateway Claude", "base_url": SHARED_BASE_URL,
         "api_key": own_key, "model": "claude-default"},
    ])
    cfg_mod.invalidate_models_cache()
    # Pin the loaded-config path and mtime: tests/test_issue4662_reload_config_mtime_cache.py
    # leaves _cfg_path and _cfg_mtime pointing at its deleted tmp config, so without this
    # get_available_models() sees a stale mtime and reloads config.yaml over this in-memory
    # seed, an order-dependent failure of the same class as #8226. A missing config.yaml
    # must pin 0.0, the value get_available_models() reads for it.
    monkeypatch.setattr(cfg_mod, "_cfg_path", cfg_mod._get_config_path())
    try:
        current_mtime = cfg_mod.Path(cfg_mod._get_config_path()).stat().st_mtime
    except OSError:
        current_mtime = 0.0
    monkeypatch.setattr(cfg_mod, "_cfg_mtime", current_mtime)
    try:
        groups = cfg_mod.get_available_models(force_refresh=True)["groups"]
    finally:
        cfg_mod.invalidate_models_cache()
    claude = [m["id"] for g in groups if g.get("provider_id") == "custom:gateway-claude"
              for m in g["models"]]
    assert claude == ["claude-default", "claude-live"]
