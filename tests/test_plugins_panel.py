"""Regression coverage for issue #539: Settings plugin/hook visibility."""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse


def read(path: str) -> str:
    from pathlib import Path
    return Path(path).read_text(encoding="utf-8")


class _FakeManifest:
    def __init__(self, *, name, key, version="", description="", provides_hooks=None, path=None, kind="standalone"):
        self.name = name
        self.key = key
        self.version = version
        self.description = description
        self.provides_hooks = provides_hooks or []
        self.path = path
        self.source = "user"
        self.kind = kind


class _FakeLoadedPlugin:
    def __init__(self, manifest, *, enabled=True, hooks_registered=None, error=None):
        self.manifest = manifest
        self.enabled = enabled
        self.hooks_registered = hooks_registered or []
        self.error = error


class _FakePluginManager:
    def __init__(self, plugins):
        self._plugins = plugins
        self.discover_calls = []

    def discover_and_load(self, force=False):
        self.discover_calls.append(force)


class TestPluginsApi:
    def _capture_plugins_response(self, manager):
        import api.routes as routes
        captured = {}

        def fake_j(handler, payload, status=200, extra_headers=None):
            captured["payload"] = payload
            captured["status"] = status
            return True

        handler = MagicMock()
        with patch("api.routes.j", side_effect=fake_j), \
             patch("api.routes._get_plugin_manager_for_visibility", return_value=manager):
            handled = routes.handle_get(handler, urlparse("/api/plugins"))

        assert handled is True
        assert captured.get("status") == 200
        return captured["payload"]

    def test_api_plugins_exposes_sanitized_metadata_and_hook_names(self):
        manager = _FakePluginManager({
            "guard": _FakeLoadedPlugin(
                _FakeManifest(
                    name="guard",
                    key="guard",
                    version="1.2.3",
                    description="Blocks unsafe tool calls",
                    path="/home/michael/.hermes/plugins/guard",
                ),
                enabled=True,
                hooks_registered=["pre_tool_call", "post_tool_call"],
            )
        })

        payload = self._capture_plugins_response(manager)

        assert payload["supported_hooks"] == [
            "pre_tool_call",
            "post_tool_call",
            "pre_llm_call",
            "post_llm_call",
        ]
        assert payload["plugins"] == [{
            "name": "guard",
            "key": "guard",
            "version": "1.2.3",
            "description": "Blocks unsafe tool calls",
            "enabled": True,
            "kind": "standalone",
            "activation": "enabled",
            "hooks": ["pre_tool_call", "post_tool_call"],
        }]
        serialized = repr(payload)
        assert "/home/michael" not in serialized
        assert "callback" not in serialized.lower()
        assert "source" not in payload["plugins"][0]
        assert "path" not in payload["plugins"][0]
        assert manager.discover_calls == [False]

    def test_api_plugins_empty_state_payload_when_no_plugins_loaded(self):
        payload = self._capture_plugins_response(_FakePluginManager({}))

        assert payload["plugins"] == []
        assert payload["empty"] is True
        assert payload["supported_hooks"] == [
            "pre_tool_call",
            "post_tool_call",
            "pre_llm_call",
            "post_llm_call",
        ]

    def test_api_plugins_filters_non_visibility_hooks_and_manifest_paths(self):
        manager = _FakePluginManager({
            "mixed": _FakeLoadedPlugin(
                _FakeManifest(
                    name="mixed",
                    key="mixed",
                    version="0.1",
                    description="Mixed hooks",
                    provides_hooks=["/tmp/not-a-hook", "pre_llm_call", "on_session_end"],
                    path="/secret/plugin.py",
                ),
                enabled=False,
                hooks_registered=["post_llm_call", "pre_gateway_dispatch", "post_llm_call"],
            )
        })

        payload = self._capture_plugins_response(manager)

        plugin = payload["plugins"][0]
        assert plugin["hooks"] == ["pre_llm_call", "post_llm_call"]
        assert plugin["enabled"] is False
        assert plugin["kind"] == "standalone"
        assert plugin["activation"] == "disabled"
        assert "/tmp/not-a-hook" not in repr(payload)
        assert "/secret" not in repr(payload)

    def test_api_plugins_marks_exclusive_plugins_as_active_provider(self):
        # Exclusive plugins (e.g. memory.provider: noema) leave loaded.enabled
        # False by design — the bundled discovery scanner records the manifest
        # but defers loading to the category's own activation path. Without
        # the `activation`/`kind` fields, the panel rendered them as
        # "Disabled + no hooks" indistinguishable from broken plugins (#2659).
        manager = _FakePluginManager({
            "noema": _FakeLoadedPlugin(
                _FakeManifest(
                    name="noema",
                    key="noema",
                    version="0.1.0",
                    description="Structured memory backed by a Noema Cortex",
                    kind="exclusive",
                ),
                enabled=False,
                hooks_registered=[],
                error="exclusive plugin — activate via <category>.provider config",
            )
        })

        payload = self._capture_plugins_response(manager)

        plugin = payload["plugins"][0]
        assert plugin["kind"] == "exclusive"
        assert plugin["activation"] == "exclusive"
        # `enabled` stays False for back-compat with older WebUI clients that
        # key off it directly; new clients must read `activation`.
        assert plugin["enabled"] is False
        assert plugin["hooks"] == []
        # The raw error string is intentionally NOT surfaced — it can contain
        # filesystem paths or other internals on other plugin kinds.
        assert "exclusive plugin" not in repr(payload)

    def test_api_plugins_marks_active_model_provider(self):
        # model-provider plugins that loaded successfully (loaded.enabled True)
        # get the "provider" activation badge so the panel can distinguish
        # them from standalone hook plugins.
        manager = _FakePluginManager({
            "openrouter": _FakeLoadedPlugin(
                _FakeManifest(
                    name="openrouter",
                    key="openrouter",
                    version="1.0.0",
                    description="OpenRouter model provider",
                    kind="model-provider",
                ),
                enabled=True,
                hooks_registered=[],
            )
        })

        payload = self._capture_plugins_response(manager)

        plugin = payload["plugins"][0]
        assert plugin["kind"] == "model-provider"
        assert plugin["activation"] == "provider"
        assert plugin["enabled"] is True


class TestPluginsSettingsUi:
    def test_settings_sidebar_has_plugins_section(self):
        html = read("static/index.html")
        js = read("static/panels.js")

        assert 'data-settings-section="plugins"' in html
        assert "settingsPanePlugins" in html
        assert "'plugins'" in js
        assert "loadPluginsPanel()" in js

    def test_plugins_panel_has_list_and_empty_state(self):
        html = read("static/index.html")

        assert 'id="pluginsList"' in html
        assert 'id="pluginsEmpty"' in html
        assert "No Hermes plugins are currently visible" in html

    def test_plugins_panel_fetches_api_and_renders_hook_badges_safely(self):
        js = read("static/panels.js")

        assert "api('/api/plugins')" in js
        assert "_buildPluginCard" in js
        assert "plugin-hook-badge" in js
        assert "esc(plugin.description" in js
        segment = js[js.find("function _buildPluginCard"):js.find("// ── Providers panel")]
        assert ".path" not in segment
        assert ".callback" not in segment

    def test_plugins_panel_renders_active_provider_badge(self):
        # The card must distinguish exclusive/provider activation from a plain
        # "Enabled" state and use the dedicated empty-hooks message for
        # provider plugins instead of "No registered lifecycle hooks" (#2659).
        js = read("static/panels.js")
        segment = js[js.find("function _buildPluginCard"):js.find("// ── Providers panel")]

        assert "plugin.activation" in segment
        assert "'exclusive'" in segment
        assert "'provider'" in segment
        assert "plugins_active_provider" in segment
        assert "plugins_provider_no_hooks" in segment
        # Graceful fallback when the older payload shape (no `activation` field)
        # is returned — the card should still resolve a badge from `enabled`.
        assert "plugin.enabled===false" in segment

    def test_plugins_panel_i18n_strings_present(self):
        i18n = read("static/i18n.js")

        assert "plugins_active_provider:" in i18n
        assert "plugins_provider_no_hooks:" in i18n
