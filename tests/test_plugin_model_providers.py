"""Regression tests for model-provider plugin discovery in WebUI.

Plugin profiles under ``plugins/model-providers/<name>/`` are auto-registered
in the Hermes agent CLI.  WebUI must expose them in Settings → Providers and
the model picker without hardcoding each slug.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import api.config as config
import api.profiles as profiles
from api.plugin_providers import invalidate_plugin_model_provider_cache


def _install_fake_yandex_plugin(monkeypatch):
    profile = SimpleNamespace(
        name="yandex",
        display_name="Yandex AI Studio",
        env_vars=("YANDEX_API_KEY", "YANDEX_FOLDER_ID"),
        auth_type="api_key",
        aliases=("yandex-ai-studio",),
    )

    def _fake_list_providers():
        return [profile]

    fake_providers = types.ModuleType("providers")
    fake_providers.list_providers = _fake_list_providers
    monkeypatch.setitem(sys.modules, "providers", fake_providers)
    invalidate_plugin_model_provider_cache()


def _install_fake_hermes_cli(monkeypatch, *, authenticated: bool = True, model_ids: list[str] | None = None):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {
            "id": "yandex",
            "label": "Yandex AI Studio",
            "aliases": [],
            "authenticated": authenticated,
        }
    ]
    fake_models.provider_model_ids = lambda pid: list(model_ids or []) if pid == "yandex" else []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda pid: (
        {
            "logged_in": True,
            "configured": True,
            "key_source": "YANDEX_API_KEY",
        }
        if pid == "yandex"
        else {}
    )

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)


class TestPluginModelProvidersSettings:
    def test_get_providers_includes_plugin_model_provider(self, monkeypatch, tmp_path):
        _install_fake_yandex_plugin(monkeypatch)
        _install_fake_hermes_cli(monkeypatch, model_ids=["deepseek-v4-flash/latest"])
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        env_path = tmp_path / ".env"
        env_path.write_text("YANDEX_API_KEY=test-yandex-key-12345\n", encoding="utf-8")

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {"provider": "gemini"}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers

        try:
            result = get_providers()
            yandex = next((p for p in result["providers"] if p["id"] == "yandex"), None)
            assert yandex is not None, "plugin model-provider must appear in Settings → Providers"
            assert yandex["display_name"] == "Yandex AI Studio"
            assert yandex["has_key"] is True
            assert yandex["configurable"] is True
            assert yandex.get("is_plugin_provider") is True
            assert yandex["models_total"] >= 1
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config.invalidate_models_cache()

    def test_get_providers_plugin_key_source_from_auth_store(self, monkeypatch, tmp_path):
        """Credential-pool auth must not be misreported as config_yaml."""
        _install_fake_yandex_plugin(monkeypatch)

        fake_pkg = types.ModuleType("hermes_cli")
        fake_pkg.__path__ = []
        fake_models = types.ModuleType("hermes_cli.models")
        fake_models.list_available_providers = lambda: []
        fake_models.provider_model_ids = lambda pid: []
        fake_auth = types.ModuleType("hermes_cli.auth")
        fake_auth.get_auth_status = lambda pid: (
            {
                "logged_in": True,
                "configured": True,
                "key_source": "credential_pool:yandex",
            }
            if pid == "yandex"
            else {}
        )
        monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
        monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
        monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers

        try:
            result = get_providers()
            yandex = next((p for p in result["providers"] if p["id"] == "yandex"), None)
            assert yandex is not None
            assert yandex["has_key"] is True
            assert yandex["key_source"] == "credential_pool:yandex"
            assert yandex["key_source"] != "config_yaml"
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config.invalidate_models_cache()

    def test_set_provider_key_accepts_plugin_env_var(self, monkeypatch, tmp_path):
        _install_fake_yandex_plugin(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api.providers import set_provider_key

        result = set_provider_key("yandex", "test-yandex-key-abcdef")
        assert result["ok"] is True
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "YANDEX_API_KEY=test-yandex-key-abcdef" in env_text


class TestPluginOnlyExcludesStaticProviders:
    def test_bundled_agent_profiles_are_not_plugin_only(self, monkeypatch):
        """Agent bundled profiles must not hijack WebUI static/custom paths."""
        _install_fake_yandex_plugin(monkeypatch)
        from api.plugin_providers import (
            effective_provider_display_name,
            is_plugin_model_provider,
            plugin_model_provider_ids,
        )
        from api.config import _PROVIDER_DISPLAY

        assert is_plugin_model_provider("yandex") is True
        assert "yandex" in plugin_model_provider_ids()
        for static_pid in ("custom", "gemini", "nous", "anthropic"):
            assert is_plugin_model_provider(static_pid) is False, static_pid
            assert static_pid not in plugin_model_provider_ids()
        assert effective_provider_display_name("custom", _PROVIDER_DISPLAY) == "Custom"
        assert effective_provider_display_name("gemini", _PROVIDER_DISPLAY) == "Gemini"


class TestPluginModelProvidersPanelFilter:
    def test_providers_panel_includes_plugin_model_providers(self):
        src = open("static/panels.js", encoding="utf-8").read()
        assert "p.is_plugin_provider" in src
        assert "filter(p=>p.configurable||p.is_oauth||p.is_custom||p.is_plugin_provider||p.is_self_hosted)" in src


class TestPluginModelProvidersPicker:
    def test_model_picker_includes_authenticated_plugin_provider(self, monkeypatch, tmp_path):
        _install_fake_yandex_plugin(monkeypatch)
        _install_fake_hermes_cli(
            monkeypatch,
            authenticated=True,
            model_ids=["gpt://folder/deepseek-v4-flash/latest"],
        )
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {"provider": "gemini", "default": "gemini-2.5-flash"}
        config.cfg["providers"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        config.invalidate_models_cache()
        try:
            models = config.get_available_models()
            yandex_group = next(
                (g for g in models.get("groups", []) if g.get("provider_id") == "yandex"),
                None,
            )
            assert yandex_group is not None, "authenticated plugin provider must appear in picker"
            assert yandex_group["provider"] == "Yandex AI Studio"
            assert len(yandex_group.get("models") or []) >= 1
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config.invalidate_models_cache()


class TestPluginFallbackModelsInStaticCatalog:
    """Regression tests for the network-free /api/models catalog path.

    ``_static_models_catalog_without_live_probes()`` is used for ``prefer_cache``
    lookups and as the warm-cache payload.  Plugin-only providers (e.g. 9router)
    are not in ``_PROVIDER_MODELS`` and rarely ship a ``models:`` allowlist in
    ``providers.<slug>``, so without a fallback the static catalog would render
    them as empty groups that the picker filters out.  The fix is to surface
    the ``ProviderProfile.fallback_models`` declared by the plugin itself on
    this cold path.
    """

    def test_static_catalog_surfaces_plugin_fallback_models(
        self, monkeypatch, tmp_path
    ):
        _install_fake_yandex_plugin(monkeypatch)
        _install_fake_hermes_cli(monkeypatch, authenticated=True, model_ids=[])
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # _provider_has_key reads the env var directly, not the .env file.
        # Set it so the static catalog's plugin-detection branch fires.
        monkeypatch.setenv("YANDEX_API_KEY", "test-y...n")

        env_path = tmp_path / ".env"
        env_path.write_text("YANDEX_API_KEY=test-y...n", encoding="utf-8")

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        # No `providers.yandex.models:` allowlist, no `models:` in cfg at all.
        # Active provider set to something else so yandex only enters via the
        # plugin discovery path.
        config.cfg["model"] = {"provider": "gemini", "default": "gemini-2.5-flash"}
        config.cfg["providers"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        config.invalidate_models_cache()
        try:
            catalog = config._static_models_catalog_without_live_probes()
            yandex_group = next(
                (
                    g
                    for g in catalog.get("groups", [])
                    if g.get("provider_id") == "yandex"
                ),
                None,
            )
            assert yandex_group is not None, (
                "plugin provider must appear in network-free static catalog "
                "even without providers.<slug>.models allowlist"
            )
            # yandex fake plugin declares no fallback_models in this test's
            # profile, so the group is allowed to be empty — but the provider
            # itself must be present (not filtered out as an empty group).
            assert yandex_group.get("models") == [] or yandex_group.get("models")
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config.invalidate_models_cache()

    def test_static_catalog_uses_provider_fallback_models_when_declared(
        self, monkeypatch, tmp_path
    ):
        """When the plugin declares ``fallback_models``, the static catalog
        surfaces them as the group's model list."""
        fallback = (
            "gh/claude-sonnet-4.5",
            "ag/claude-sonnet-4-6",
            "ds/deepseek-chat",
        )
        profile = SimpleNamespace(
            name="myplugin",
            display_name="My Plugin",
            env_vars=("MYPLUGIN_API_KEY",),
            auth_type="api_key",
            aliases=(),
            fallback_models=fallback,
        )

        def _fake_list_providers():
            return [profile]

        fake_providers = types.ModuleType("providers")
        fake_providers.list_providers = _fake_list_providers
        monkeypatch.setitem(sys.modules, "providers", fake_providers)

        from api.plugin_providers import invalidate_plugin_model_provider_cache

        invalidate_plugin_model_provider_cache()

        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # _provider_has_key reads the env var directly.
        monkeypatch.setenv("MYPLUGIN_API_KEY", "test-m...n")
        (tmp_path / ".env").write_text(
            "MYPLUGIN_API_KEY=test-m...n", encoding="utf-8"
        )

        # Mock the plugin provider's API key as authenticated via hermes_cli.auth
        fake_pkg = types.ModuleType("hermes_cli")
        fake_pkg.__path__ = []
        fake_models = types.ModuleType("hermes_cli.models")
        fake_models.list_available_providers = lambda: []
        fake_models.provider_model_ids = lambda pid: []
        fake_auth = types.ModuleType("hermes_cli.auth")
        fake_auth.get_auth_status = lambda pid: (
            {"logged_in": True, "configured": True, "key_source": "env_file"}
            if pid == "myplugin"
            else {}
        )
        monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
        monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
        monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {"provider": "gemini", "default": "gemini-2.5-flash"}
        config.cfg["providers"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        config.invalidate_models_cache()
        try:
            catalog = config._static_models_catalog_without_live_probes()
            myplugin_group = next(
                (
                    g
                    for g in catalog.get("groups", [])
                    if g.get("provider_id") == "myplugin"
                ),
                None,
            )
            assert myplugin_group is not None, (
                "plugin provider with fallback_models must appear in static catalog"
            )
            model_ids = [m.get("id") for m in myplugin_group.get("models", [])]
            assert model_ids == list(fallback), (
                f"static catalog should use plugin's fallback_models, got {model_ids}"
            )
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
            config.invalidate_models_cache()


class TestPluginModelProviderRouting:
    """Regression test for routing surfaced plugin-only models.

    Surfacing a plugin-only model in the picker is only half the fix: the
    selected ``(model, model_provider)`` pair must also *route* to the plugin.
    ``model_with_provider_context()`` historically let any slash-bearing model
    ID fall through to a bare passthrough, which made a surfaced plugin model
    such as ``gh/claude-sonnet-4.5`` inherit the default provider instead of
    the plugin that surfaced it.  The fix emits an explicit ``@plugin:model``
    hint for plugin-only providers before that bare passthrough.
    """

    def test_plugin_only_model_routes_to_plugin_provider(self, monkeypatch):
        profile = SimpleNamespace(
            name="myplugin",
            display_name="My Plugin",
            env_vars=("MYPLUGIN_API_KEY",),
            auth_type="api_key",
            aliases=(),
            fallback_models=("gh/claude-sonnet-4.5",),
        )

        def _fake_list_providers():
            return [profile]

        fake_providers = types.ModuleType("providers")
        fake_providers.list_providers = _fake_list_providers
        monkeypatch.setitem(sys.modules, "providers", fake_providers)
        invalidate_plugin_model_provider_cache()

        old_cfg = dict(config.cfg)
        config.cfg.clear()
        # Default provider is something else; plugin has no `providers:` block.
        # Without the plugin-routing branch the slash-bearing model ID would
        # fall through to the bare passthrough and inherit the default.
        config.cfg["model"] = {"provider": "gemini", "default": "gemini-2.5-flash"}
        config.cfg["providers"] = {}
        try:
            assert config._is_plugin_model_provider("myplugin"), (
                "test setup: fake plugin provider must be recognised"
            )
            routed = config.model_with_provider_context(
                "gh/claude-sonnet-4.5", "myplugin"
            )
            assert routed == "@myplugin:gh/claude-sonnet-4.5", (
                "surfaced plugin-only model must route to its plugin, "
                f"got {routed!r}"
            )
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            invalidate_plugin_model_provider_cache()

    def test_plugin_provider_routes_even_when_it_is_the_configured_provider(self, monkeypatch):
        # #5909 gate finding: the plugin-routing branch must run BEFORE the
        # `provider == config_provider` bare-passthrough. When the ACTIVE plugin
        # provider is also the configured default, returning a bare model would
        # drop the '@plugin:' hint and route to the wrong backend.
        profile = SimpleNamespace(
            name="gmi",
            display_name="GMI",
            env_vars=("GMI_API_KEY",),
            auth_type="api_key",
            aliases=(),
            fallback_models=("anthropic/claude-sonnet-4.6",),
        )

        def _fake_list_providers():
            return [profile]

        fake_providers = types.ModuleType("providers")
        fake_providers.list_providers = _fake_list_providers
        monkeypatch.setitem(sys.modules, "providers", fake_providers)
        invalidate_plugin_model_provider_cache()

        old_cfg = dict(config.cfg)
        config.cfg.clear()
        # The plugin IS the configured provider now.
        config.cfg["model"] = {"provider": "gmi", "default": "anthropic/claude-sonnet-4.6"}
        config.cfg["providers"] = {}
        try:
            assert config._is_plugin_model_provider("gmi")
            routed = config.model_with_provider_context(
                "anthropic/claude-sonnet-4.6", "gmi"
            )
            assert routed == "@gmi:anthropic/claude-sonnet-4.6", (
                "an active plugin provider must keep its '@plugin:' routing hint "
                "even when it equals the configured provider, got "
                f"{routed!r}"
            )
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            invalidate_plugin_model_provider_cache()
