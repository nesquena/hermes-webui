"""Tests for custom_providers scanning in get_providers().

Verifies that config.yaml custom_providers entries (e.g. glmcode, timicc)
are surfaced in the /api/providers response alongside built-in providers.
"""

import json
import os
import sys
import types

import api.config as config
import api.profiles as profiles
from tests._pytest_port import BASE


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli so tests are deterministic and offline."""
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

    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


class TestCustomProvidersInGetProviders:
    """Unit tests for custom_providers scanning in get_providers()."""

    def _setup_cfg(self, custom_providers, active_provider=None):
        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {"provider": active_provider or "anthropic"}
        if custom_providers is not None:
            config.cfg["custom_providers"] = custom_providers
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0
        return old_cfg, old_mtime

    def _restore_cfg(self, old_cfg, old_mtime):
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    def test_custom_provider_with_models(self, monkeypatch, tmp_path):
        """glmcode custom provider with models should appear in provider list."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("GLMCODE_API_KEY", "test-glm-key-12345678")

        old_cfg, old_mtime = self._setup_cfg([
            {
                "name": "glmcode",
                "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                "api_key": "${GLMCODE_API_KEY}",
                "api_mode": "openai_compatible",
                "model": "glm-5.1",
            },
        ])

        from api.providers import get_providers
        try:
            result = get_providers()
            provider_ids = {p["id"] for p in result["providers"]}
            assert "custom:glmcode" in provider_ids, (
                f"custom:glmcode missing; got: {sorted(provider_ids)}"
            )

            glmcode = [p for p in result["providers"] if p["id"] == "custom:glmcode"][0]
            assert glmcode["has_key"] is True, (
                "glmcode should detect key from ${GLMCODE_API_KEY} env var"
            )
            assert glmcode["configurable"] is True, (
                "custom providers should now be configurable via WebUI"
            )
            assert glmcode["key_source"] == "config_yaml"
            assert glmcode["display_name"] == "glmcode"
            assert glmcode["is_custom"] is True, (
                "custom providers should be flagged as is_custom"
            )
            assert glmcode["base_url"] == "https://open.bigmodel.cn/api/coding/paas/v4", (
                f"Expected base_url, got: {glmcode['base_url']}"
            )
            assert glmcode["api_mode"] == "openai_compatible", (
                f"Expected api_mode openai_compatible, got: {glmcode['api_mode']}"
            )
            assert glmcode["models_total"] == 1, (
                f"Expected models_total is 1, got: {glmcode['models_total']}"
            )

            # Model list — single model entry
            model_ids = {m["id"] for m in glmcode["models"]}
            assert "glm-5.1" in model_ids, (
                f"Expected glm-5.1 in models, got: {model_ids}"
            )
        finally:
            self._restore_cfg(old_cfg, old_mtime)

    def test_custom_provider_with_multi_models(self, monkeypatch, tmp_path):
        """Custom provider with `models` list should expose all entries."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test-12345678")

        old_cfg, old_mtime = self._setup_cfg([
            {
                "name": "deepseek",
                "base_url": "https://api.deepseek.com",
                "api_key": "${DEEPSEEK_API_KEY}",
                "api_mode": "openai_compatible",
                "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
            },
        ])

        from api.providers import get_providers
        try:
            result = get_providers()
            provider_ids = {p["id"] for p in result["providers"]}
            assert "custom:deepseek" in provider_ids

            ds = [p for p in result["providers"] if p["id"] == "custom:deepseek"][0]
            assert ds["has_key"] is True
            model_ids = {m["id"] for m in ds["models"]}
            assert model_ids == {"deepseek-v4-flash", "deepseek-v4-pro"}, (
                f"Expected v4 models, got: {model_ids}"
            )
        finally:
            self._restore_cfg(old_cfg, old_mtime)

    def test_custom_provider_no_key(self, monkeypatch, tmp_path):
        """Custom provider without a configured key should show has_key=False."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        # Ensure TIMICC_API_KEY is not set
        monkeypatch.delenv("TIMICC_API_KEY", raising=False)

        old_cfg, old_mtime = self._setup_cfg([
            {
                "name": "timicc-claude",
                "base_url": "https://timicc.com/v1",
                "api_key": "${TIMICC_API_KEY}",
                "api_mode": "anthropic_messages",
            },
        ])

        from api.providers import get_providers
        try:
            result = get_providers()
            # TIMICC_API_KEY env var is not set → has_key should be False
            cp = [p for p in result["providers"] if p["id"] == "custom:timicc-claude"]
            assert len(cp) == 1
            assert cp[0]["has_key"] is False
            assert cp[0]["key_source"] == "none"
        finally:
            self._restore_cfg(old_cfg, old_mtime)

    def test_empty_custom_providers_no_crash(self, monkeypatch, tmp_path):
        """get_providers should not crash when custom_providers is empty list."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg, old_mtime = self._setup_cfg([])

        from api.providers import get_providers
        try:
            result = get_providers()
            # No crash, still returns built-in providers
            provider_ids = {p["id"] for p in result["providers"]}
            # Should not contain any custom: entries
            custom_ids = {pid for pid in provider_ids if pid.startswith("custom:")}
            assert len(custom_ids) == 0, (
                f"Empty custom_providers should not produce entries, got: {custom_ids}"
            )
        finally:
            self._restore_cfg(old_cfg, old_mtime)

    def test_custom_provider_bare_api_key(self, monkeypatch, tmp_path):
        """Custom provider with inline api_key (not env ref) should show has_key=True."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg, old_mtime = self._setup_cfg([
            {
                "name": "my-proxy",
                "base_url": "https://proxy.example.com/v1",
                "api_key": "sk-inline-key-12345678",
            },
        ])

        from api.providers import get_providers
        try:
            result = get_providers()
            cp = [p for p in result["providers"] if p["id"] == "custom:my-proxy"]
            assert len(cp) == 1
            assert cp[0]["has_key"] is True
        finally:
            self._restore_cfg(old_cfg, old_mtime)

    def test_custom_provider_parenthesized_port_uses_safe_provider_id(self, monkeypatch, tmp_path):
        """Local setup names with ports must expose the same safe id used by routing."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("LOCAL_PORT_API_KEY", "sk-local-port-test-12345678")

        old_cfg, old_mtime = self._setup_cfg([
            {
                "name": "Local (127.0.0.1:15721)",
                "base_url": "http://127.0.0.1:15721/v1",
                "api_key": "${LOCAL_PORT_API_KEY}",
                "model": "deepseek-v4-flash",
            },
        ])

        from api.providers import _get_provider_api_key, _provider_has_key, get_providers
        try:
            provider_id = "custom:local-127.0.0.1-15721"
            result = get_providers()
            provider_ids = {p["id"] for p in result["providers"]}
            assert provider_id in provider_ids
            assert "custom:Local (127.0.0.1:15721)" not in provider_ids
            assert "custom:local-(127.0.0.1:15721)" not in provider_ids

            local = [p for p in result["providers"] if p["id"] == provider_id][0]
            assert local["display_name"] == "Local (127.0.0.1:15721)"
            assert local["has_key"] is True
            assert _provider_has_key(provider_id) is True
            assert _get_provider_api_key(provider_id) == "sk-local-port-test-12345678"
        finally:
            self._restore_cfg(old_cfg, old_mtime)

    def test_custom_provider_no_name_skipped(self, monkeypatch, tmp_path):
        """Malformed custom provider without name should be silently skipped."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg, old_mtime = self._setup_cfg([
            {"base_url": "https://no-name.example.com/v1"},
        ])

        from api.providers import get_providers
        try:
            result = get_providers()
            custom_ids = {p["id"] for p in result["providers"] if p["id"].startswith("custom:")}
            assert len(custom_ids) == 0, (
                f"Entry without name should be skipped, got: {custom_ids}"
            )
        finally:
            self._restore_cfg(old_cfg, old_mtime)


class TestDeepSeekV4Models:
    """Verify DeepSeek V4 models are in the model lists, V3 is removed."""

    def test_v4_models_in_provider_models(self):
        """_PROVIDER_MODELS['deepseek'] should contain v4 and legacy v3 entries."""
        from api.config import _PROVIDER_MODELS
        ds_models = _PROVIDER_MODELS.get("deepseek", [])
        ids = {m["id"] for m in ds_models}

        assert "deepseek-v4-flash" in ids, f"v4-flash missing: {ids}"
        assert "deepseek-v4-pro" in ids, f"v4-pro missing: {ids}"

        # Legacy models still present (deprecated 2026-07-24, not yet removed)
        assert "deepseek-chat-v3-0324" in ids, (
            f"V3 legacy should remain until deprecation date: {ids}"
        )
        assert "deepseek-reasoner" in ids, (
            f"Reasoner legacy should remain until deprecation date: {ids}"
        )

    def test_zai_models_include_glm_series(self):
        """_PROVIDER_MODELS['zai'] should have GLM-5.x and GLM-4.x models."""
        from api.config import _PROVIDER_MODELS
        zai_models = _PROVIDER_MODELS.get("zai", [])
        ids = {m["id"] for m in zai_models}

        assert "glm-5.1" in ids, f"glm-5.1 missing from zai models: {ids}"
        assert "glm-5" in ids, f"glm-5 missing from zai models: {ids}"
        assert "glm-5-turbo" in ids, f"glm-5-turbo missing from zai models: {ids}"
        assert "glm-4.7" in ids, f"glm-4.7 missing from zai models: {ids}"
        assert "glm-4.5" in ids, f"glm-4.5 missing from zai models: {ids}"
        assert "glm-4.5-flash" in ids, f"glm-4.5-flash missing from zai models: {ids}"

    def test_zai_in_onboarding_setup(self):
        """_SUPPORTED_PROVIDER_SETUPS should have 'zai' entry."""
        from api.onboarding import _SUPPORTED_PROVIDER_SETUPS
        assert "zai" in _SUPPORTED_PROVIDER_SETUPS, (
            "zai provider should be in onboarding quick-setup"
        )
        zai = _SUPPORTED_PROVIDER_SETUPS["zai"]
        assert zai["label"] == "Z.AI / GLM (智谱)"
        assert zai["env_var"] == "GLM_API_KEY"
        assert zai["default_model"] == "glm-5.1"
        assert zai["default_base_url"] == "https://open.bigmodel.cn/api/paas/v4"

    def test_deepseek_onboarding_default_is_v4(self):
        """DeepSeek onboarding default should be v4-flash, not V3."""
        from api.onboarding import _SUPPORTED_PROVIDER_SETUPS
        ds = _SUPPORTED_PROVIDER_SETUPS.get("deepseek", {})
        assert ds.get("default_model") == "deepseek-v4-flash", (
            f"DeepSeek default should be v4-flash, got: {ds.get('default_model')}"
        )
        assert ds.get("default_base_url") == "https://api.deepseek.com", (
            f"Base URL should be bare domain, got: {ds.get('default_base_url')}"
        )
