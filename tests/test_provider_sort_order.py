"""Tests for provider/model-picker sort ordering.

Feature: configured and custom providers float to the top in both
the Settings provider list and the model picker dropdown.

Sort tiers (both endpoints):
  0 — active provider
  1 — custom:* providers
  2 — providers with a configured key (credential pool or config.yaml api_key)
  3 — everyone else, alphabetically by provider id
"""
from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

import api.config as config
import api.providers as providers_mod


# ---------------------------------------------------------------------------
# Helpers — lightweight stubs so we don't need a real hermes-agent install
# ---------------------------------------------------------------------------

def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli so detection is deterministic in tests."""
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
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    config.invalidate_models_cache()


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Invalidate the TTL model cache around each test."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _model_list(*ids):
    """Helper: build [{"id": x, "label": x}, ...] for _PROVIDER_MODELS."""
    return [{"id": m, "label": m} for m in ids]


def _setup_config(tmp_path, monkeypatch, yaml_text, provider_models=None):
    """Write config.yaml, point config module at it, install stubs."""
    _install_fake_hermes_cli(monkeypatch)

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)

    # Point auth store at a non-existent file so it stays empty
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: auth_path)

    # Inject provider models if given
    if provider_models:
        for pid, models in provider_models.items():
            monkeypatch.setitem(config._PROVIDER_MODELS, pid, models)

    config.reload_config()


def _teardown_config():
    config.reload_config()


# ===================================================================
# get_providers() sort order  (api/providers.py)
# ===================================================================

class TestProviderSortOrder:
    """providers returned by get_providers() should respect tier ordering."""

    def test_active_provider_comes_first(self, tmp_path, monkeypatch):
        """The active provider (from config model.provider) is tier-0."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openrouter\n  default: test-model\n"
            "providers:\n"
            "  anthropic:\n    api_key: sk-test-123\n"
            "  openai: {}\n"
            "  openrouter:\n    api_key: sk-or-123\n"
        )

        result = providers_mod.get_providers()
        prov_ids = [p["id"] for p in result["providers"]]

        assert prov_ids[0] == "openrouter", (
            f"Expected openrouter first (active), got order: {prov_ids}"
        )
        _teardown_config()

    def test_custom_provider_before_plain(self, tmp_path, monkeypatch):
        """custom:* providers (tier-1) sort before providers with keys (tier-2)."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai: {}\n"
            "  anthropic:\n    api_key: sk-ant-123\n"
            "custom_providers:\n"
            "  - name: MyLocal\n"
            "    base_url: http://localhost:8080/v1\n"
            "    api_key: local-key\n"
            "    models:\n"
            "      - local-model-a\n"
        )

        result = providers_mod.get_providers()
        prov_ids = [p["id"] for p in result["providers"]]

        openai_idx = prov_ids.index("openai")
        custom_idx = prov_ids.index("custom:mylocal")
        anthropic_idx = prov_ids.index("anthropic")

        assert openai_idx < custom_idx < anthropic_idx, (
            f"Expected openai < custom:mylocal < anthropic, got {prov_ids}"
        )
        _teardown_config()

    def test_has_key_provider_before_no_key(self, tmp_path, monkeypatch):
        """Providers with keys (tier-2) come before those without (tier-3)."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai: {}\n"
            "  deepseek:\n    api_key: sk-ds-123\n"
            "  google: {}\n"
            "  groq: {}\n"
            "  xai:\n    api_key: xai-key\n"
        )

        result = providers_mod.get_providers()
        prov_ids = [p["id"] for p in result["providers"]]

        deepseek_idx = prov_ids.index("deepseek")
        xai_idx = prov_ids.index("xai")
        google_idx = prov_ids.index("google")
        groq_idx = prov_ids.index("groq")

        assert deepseek_idx < google_idx, (
            f"deepseek(has_key) at {deepseek_idx} should be before google at {google_idx}"
        )
        assert deepseek_idx < groq_idx
        assert xai_idx < google_idx
        assert xai_idx < groq_idx
        _teardown_config()


# ===================================================================
# get_available_models() sort order  (api/config.py)
# ===================================================================

class TestModelPickerSortOrder:
    """Model picker groups should follow the same tier ordering."""

    def test_active_provider_group_is_first(self, tmp_path, monkeypatch):
        """The group for the active provider appears first in groups list."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: anthropic\n  default: claude-3-5-sonnet\n"
            "providers:\n"
            "  openai:\n    api_key: sk-oai-123\n"
            "  anthropic: {}\n",
            provider_models={
                "openai": _model_list("gpt-4", "gpt-4o"),
                "anthropic": _model_list("claude-3-5-sonnet"),
            },
        )

        result = config.get_available_models()
        group_ids = [g.get("provider_id") for g in result.get("groups", [])]

        assert group_ids[0] == "anthropic", (
            f"Expected anthropic first (active), got: {group_ids}"
        )
        _teardown_config()

    def test_custom_groups_before_configured(self, tmp_path, monkeypatch):
        """custom:* groups sort before providers that merely have keys."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai:\n    api_key: sk-oai-123\n"
            "custom_providers:\n"
            "  - name: MyLocal\n"
            "    base_url: http://localhost:8080/v1\n"
            "    api_key: local-key\n"
            "    models:\n"
            "      - local-a\n",
            provider_models={
                "openai": _model_list("gpt-4"),
            },
        )

        result = config.get_available_models()
        group_ids = [g.get("provider_id") for g in result.get("groups", [])]

        if "custom:mylocal" in group_ids:
            custom_idx = group_ids.index("custom:mylocal")
            for i, gid in enumerate(group_ids):
                if gid not in ("openai", "custom:mylocal"):
                    assert custom_idx < i, (
                        f"custom:mylocal at {custom_idx} should precede {gid} at {i}"
                    )
        _teardown_config()

    def test_configured_key_groups_before_no_key(self, tmp_path, monkeypatch):
        """Providers with api_key in config sort before those without."""
        _setup_config(tmp_path, monkeypatch,
            "model:\n  provider: openai\n  default: gpt-4\n"
            "providers:\n"
            "  openai:\n    api_key: sk-oai-123\n"
            "  deepseek:\n    api_key: sk-ds-123\n"
            "  google: {}\n",
            provider_models={
                "openai": _model_list("gpt-4"),
                "deepseek": _model_list("deepseek-chat"),
                "google": _model_list("gemini-pro"),
            },
        )

        result = config.get_available_models()
        group_ids = [g.get("provider_id") for g in result.get("groups", [])]

        if "deepseek" in group_ids and "google" in group_ids:
            ds_idx = group_ids.index("deepseek")
            google_idx = group_ids.index("google")
            assert ds_idx < google_idx, (
                f"deepseek (has key) at {ds_idx} should precede google (no key) at {google_idx}"
            )
        _teardown_config()
