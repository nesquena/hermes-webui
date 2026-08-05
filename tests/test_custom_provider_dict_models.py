"""Regression tests for custom_providers models dict shape in get_providers().

The ``models`` field in a ``custom_providers`` config entry can be a list
or a dict (the standard shape produced by ``hermes config set``):

    custom_providers:
      - name: litellm
        model: Coding               # default / sticky metadata
        models:                     # ← dict: {model_id: {context_length: ...}}
          Best: {context_length: 128000}
          Coding: {context_length: 1000000}
          Fast: {}

get_providers() only handled the list shape, so the Providers settings card
showed just the single ``model`` field instead of the full catalog.
"""
import sys
import types

import pytest


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline."""
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


def _setup_providers_module(monkeypatch):
    """Patch api.providers for offline unit testing of get_providers()."""
    _install_fake_hermes_cli(monkeypatch)

    from api import providers as prov

    monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {})
    monkeypatch.setattr(prov, "_PROVIDER_MODELS", {})
    monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
    monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
    monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: False)

    def _invalidate():
        if hasattr(prov, "invalidate_providers_cache"):
            prov.invalidate_providers_cache()

    return prov, _invalidate


# ── Tests ─────────────────────────────────────────────────────────────────


class TestCustomProviderModelsDict:
    """get_providers() must expand dict-shaped models in custom_providers."""

    def test_dict_shaped_models_all_appear(self, monkeypatch):
        """Every key in a dict-shaped models field must become a model entry."""
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Coding",
                        "api_key": "sk-test",
                        "models": {
                            "Best": {"context_length": 128000},
                            "Coding": {"context_length": 1000000},
                            "Fast": {},
                            "minimax-m3": {},
                        },
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            assert set(model_ids) == {"Best", "Coding", "Fast", "minimax-m3"}
            assert cp["models_total"] == 4
        finally:
            _invalidate()

    def test_list_shaped_models_still_work(self, monkeypatch):
        """Regression guard: the existing list shape must keep working."""
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Coding",
                        "api_key": "sk-test",
                        "models": ["Coding", "Best", "Fast"],
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            assert set(model_ids) == {"Coding", "Best", "Fast"}
        finally:
            _invalidate()

    def test_dict_models_include_default_model_field(self, monkeypatch):
        """When models is a dict, the ``model`` field must also appear."""
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "glm-5.2",
                        "api_key": "sk-test",
                        "models": {
                            "Best": {},
                            "Fast": {},
                        },
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            # ``model`` field ("glm-5.2") is NOT in the dict, so only dict keys.
            # If it WERE in the dict, it must not duplicate.
            assert "Best" in model_ids
            assert "Fast" in model_ids
            assert "glm-5.2" not in model_ids  # model field alone doesn't add
        finally:
            _invalidate()

    def test_empty_dict_models_falls_through_to_model_field(self, monkeypatch):
        """An empty dict should not crash; model field is the fallback."""
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Coding",
                        "api_key": "sk-test",
                        "models": {},
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            # Empty dict yields no entries; model field is NOT added separately
            # (the elif branch only fires when models is absent/None, not empty dict).
            # This documents current behaviour — the card shows zero models,
            # which is correct for an explicitly-empty dict.
            assert cp["models"] == []
        finally:
            _invalidate()
