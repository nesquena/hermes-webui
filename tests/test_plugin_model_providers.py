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
            assert yandex["models_total"] >= 1
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
