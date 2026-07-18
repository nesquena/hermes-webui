"""Tests for the Plugin Lifecycle (install/update/remove) settings UI.

Backend: api/plugin_lifecycle.py (mechanism, ungated) + api/routes.py routes
(policy: fail-closed HERMES_WEBUI_ALLOW_PLUGIN_WRITE gate on every write).
Frontend: panels.js + index.html + i18n.js additions to the existing
Settings -> Plugins pane.

This is the HIGHEST-RISK write surface added in this WebUI package: a
successful install clones and imports arbitrary Python from a Git repository
into the running Hermes agent process. No real ``hermes_cli`` is invoked —
every test that exercises the happy path injects a fake
``hermes_cli.plugins_cmd`` module (matching the pattern already used by
tests/test_moa_model_picker_provider.py for hermes_cli.moa_config).
"""
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


@pytest.fixture
def fake_plugins_cmd(monkeypatch):
    """Install a fake hermes_cli.plugins_cmd module and return it.

    Restores the real sys.modules entries (if any) on teardown so other
    tests in the suite that rely on hermes_cli being genuinely absent (or
    present) aren't affected.
    """
    fake_pkg = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setattr(fake_pkg, "__path__", [], raising=False)
    fake_cmd = types.ModuleType("hermes_cli.plugins_cmd")

    calls = {"install": [], "update": [], "remove": []}

    def _discover_all_plugins():
        return [
            ("bundled-one", "1.0", "a bundled plugin", "bundled", "/x", "bundled-one"),
            ("my-plugin", "0.3", "a user plugin with git checkout", "git", "/y", "my-plugin"),
            ("copied-plugin", "", "user plugin, no .git dir", "user", "/z", "copied-plugin"),
        ]

    def _resolve_git_url(identifier):
        if identifier == "owner/repo":
            return "https://github.com/owner/repo.git", None
        if identifier == "owner/repo/sub":
            return "https://github.com/owner/repo.git", "sub"
        if identifier == "http://insecure.example/repo.git":
            return "http://insecure.example/repo.git", None
        raise ValueError(f"Invalid plugin identifier: '{identifier}'.")

    def dashboard_install_plugin(identifier, *, force, enable):
        calls["install"].append({"identifier": identifier, "force": force, "enable": enable})
        return {
            "ok": True,
            "plugin_name": "my-plugin",
            "warnings": [],
            "missing_env": ["MY_API_KEY"],
            "after_install_path": None,
            "enabled": enable,
        }

    def dashboard_update_user_plugin(name):
        calls["update"].append(name)
        if name == "not-a-git-checkout":
            return {"ok": False, "error": f"Plugin '{name}' is not a git checkout; cannot pull updates."}
        return {"ok": True, "name": name, "output": "Already up to date", "unchanged": True}

    def dashboard_remove_user_plugin(name):
        calls["remove"].append(name)
        if name == "bundled-one":
            return {"ok": False, "error": "Bundled plugins cannot be removed from the dashboard."}
        return {"ok": True, "name": name}

    fake_cmd._discover_all_plugins = _discover_all_plugins
    fake_cmd._resolve_git_url = _resolve_git_url
    fake_cmd.dashboard_install_plugin = dashboard_install_plugin
    fake_cmd.dashboard_update_user_plugin = dashboard_update_user_plugin
    fake_cmd.dashboard_remove_user_plugin = dashboard_remove_user_plugin
    fake_cmd._calls = calls

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd", fake_cmd)
    return fake_cmd


class TestPluginLifecycleModule:
    """api/plugin_lifecycle.py — mechanism, no gating (policy lives in routes.py)."""

    def test_unavailable_without_hermes_cli(self, monkeypatch):
        """hermes_cli is an optional dependency; missing it must degrade, not crash."""
        monkeypatch.setitem(sys.modules, "hermes_cli", None)
        monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd", None)
        from api import plugin_lifecycle as pl

        data = pl.list_installed_plugins()
        assert data["plugins"] == []
        assert data["unavailable"] is True
        assert "hermes-agent" in data["error"]

        with pytest.raises(pl.PluginLifecycleUnavailable):
            pl.resolve_plugin_source("owner/repo")
        with pytest.raises(pl.PluginLifecycleUnavailable):
            pl.install_plugin("owner/repo")
        with pytest.raises(pl.PluginLifecycleUnavailable):
            pl.update_plugin("my-plugin")
        with pytest.raises(pl.PluginLifecycleUnavailable):
            pl.remove_plugin("my-plugin")

    def test_list_installed_plugins_annotates_source(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        data = pl.list_installed_plugins()
        by_key = {p["key"]: p for p in data["plugins"]}

        assert by_key["bundled-one"]["removable"] is False
        assert by_key["bundled-one"]["updatable"] is False
        assert by_key["copied-plugin"]["removable"] is True
        assert by_key["copied-plugin"]["updatable"] is False
        assert by_key["my-plugin"]["removable"] is True
        assert by_key["my-plugin"]["updatable"] is True

    def test_list_installed_plugins_sorted_by_key(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        data = pl.list_installed_plugins()
        keys = [p["key"] for p in data["plugins"]]
        assert keys == sorted(keys, key=str.lower)

    def test_resolve_plugin_source_is_pure_no_clone(self, fake_plugins_cmd):
        """resolve must never call the install/clone path."""
        from api import plugin_lifecycle as pl

        result = pl.resolve_plugin_source("owner/repo/sub")
        assert result == {
            "identifier": "owner/repo/sub",
            "git_url": "https://github.com/owner/repo.git",
            "subdir": "sub",
            "insecure_scheme": False,
        }
        assert fake_plugins_cmd._calls["install"] == []

    def test_resolve_plugin_source_flags_insecure_scheme(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        result = pl.resolve_plugin_source("http://insecure.example/repo.git")
        assert result["insecure_scheme"] is True

    def test_resolve_plugin_source_rejects_empty_identifier(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        with pytest.raises(ValueError, match="identifier is required"):
            pl.resolve_plugin_source("")

    def test_install_plugin_delegates_to_dashboard_function(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        result = pl.install_plugin("owner/repo", force=True, enable=False)
        assert result["ok"] is True
        assert result["plugin_name"] == "my-plugin"
        assert fake_plugins_cmd._calls["install"] == [
            {"identifier": "owner/repo", "force": True, "enable": False}
        ]

    def test_install_plugin_rejects_empty_identifier(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        with pytest.raises(ValueError, match="identifier is required"):
            pl.install_plugin("   ")

    def test_update_plugin_delegates(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        result = pl.update_plugin("my-plugin")
        assert result == {"ok": True, "name": "my-plugin", "output": "Already up to date", "unchanged": True}
        assert fake_plugins_cmd._calls["update"] == ["my-plugin"]

    def test_remove_plugin_rejects_bundled(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        result = pl.remove_plugin("bundled-one")
        assert result["ok"] is False
        assert "Bundled plugins cannot be removed" in result["error"]

    def test_remove_plugin_delegates_for_user_plugin(self, fake_plugins_cmd):
        from api import plugin_lifecycle as pl

        result = pl.remove_plugin("copied-plugin")
        assert result == {"ok": True, "name": "copied-plugin"}


class TestPluginLifecycleRoutes:
    """Route registration + fail-closed gate behavior in api/routes.py."""

    def test_routes_registered(self):
        for marker in (
            '"/api/plugins/installed"',
            '"/api/plugins/resolve"',
            '"/api/plugins/install"',
            "HERMES_WEBUI_ALLOW_PLUGIN_WRITE",
        ):
            assert marker in ROUTES_PY, f"Missing {marker} in routes.py"

    def test_get_installed_always_readable_reports_write_allowed_false_when_gated(self, monkeypatch):
        from api import routes

        monkeypatch.delenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", raising=False)
        monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
        monkeypatch.setattr(
            "api.plugin_lifecycle.list_installed_plugins",
            lambda: {"plugins": [], "unavailable": False},
        )

        result = routes.handle_get(object(), SimpleNamespace(path="/api/plugins/installed", query=""))

        assert result["write_allowed"] is False

    def test_get_installed_reports_write_allowed_true_when_gate_open(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
        monkeypatch.setattr(
            "api.plugin_lifecycle.list_installed_plugins",
            lambda: {"plugins": [], "unavailable": False},
        )

        result = routes.handle_get(object(), SimpleNamespace(path="/api/plugins/installed", query=""))

        assert result["write_allowed"] is True

    def test_resolve_route_ungated(self, monkeypatch, fake_plugins_cmd):
        """Resolve is pure and read-only, so it must work even with the gate closed."""
        from api import routes

        monkeypatch.delenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", raising=False)
        monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

        result = routes.handle_get(
            object(),
            SimpleNamespace(path="/api/plugins/resolve", query="identifier=owner%2Frepo"),
        )

        assert result["git_url"] == "https://github.com/owner/repo.git"

    def test_install_route_returns_403_when_gate_closed(self, monkeypatch):
        from api import routes

        monkeypatch.delenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", raising=False)
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {"identifier": "owner/repo"})
        captured = {}

        def fake_j(_handler, payload, **kwargs):
            captured["payload"] = payload
            captured["status"] = kwargs.get("status")
            return payload

        monkeypatch.setattr(routes, "j", fake_j)

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/install", query=""))

        assert captured["status"] == 403
        assert captured["payload"]["allowed"] is False
        assert "HERMES_WEBUI_ALLOW_PLUGIN_WRITE" in captured["payload"]["error"]

    def test_update_and_remove_routes_return_403_when_gate_closed(self, monkeypatch):
        from api import routes

        monkeypatch.delenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", raising=False)
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {})
        statuses = []
        monkeypatch.setattr(
            routes, "j", lambda _handler, payload, **kwargs: statuses.append(kwargs.get("status")) or payload
        )

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/my-plugin/update", query=""))
        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/my-plugin/remove", query=""))

        assert statuses == [403, 403]

    def test_install_route_succeeds_when_gate_open(self, monkeypatch, fake_plugins_cmd):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {"identifier": "owner/repo"})
        captured = {}
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, **kwargs: captured.update(payload=payload, status=kwargs.get("status")) or payload,
        )

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/install", query=""))

        assert captured["status"] == 200
        assert captured["payload"]["ok"] is True
        assert captured["payload"]["plugin_name"] == "my-plugin"
        assert fake_plugins_cmd._calls["install"] == [
            {"identifier": "owner/repo", "force": False, "enable": True}
        ]

    def test_update_route_succeeds_when_gate_open(self, monkeypatch, fake_plugins_cmd):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {})
        captured = {}
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, **kwargs: captured.update(payload=payload, status=kwargs.get("status")) or payload,
        )

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/my-plugin/update", query=""))

        assert captured["status"] == 200
        assert fake_plugins_cmd._calls["update"] == ["my-plugin"]

    def test_remove_route_name_is_url_decoded(self, monkeypatch, fake_plugins_cmd):
        """Plugin keys can be namespaced (e.g. 'observability/langfuse') and arrive URL-encoded."""
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {})
        monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

        routes.handle_post(
            object(),
            SimpleNamespace(path="/api/plugins/observability%2Flangfuse/remove", query=""),
        )

        assert fake_plugins_cmd._calls["remove"] == ["observability/langfuse"]

    def test_remove_route_returns_400_for_bundled_plugin(self, monkeypatch, fake_plugins_cmd):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {})
        captured = {}
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, **kwargs: captured.update(payload=payload, status=kwargs.get("status")) or payload,
        )

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/bundled-one/remove", query=""))

        assert captured["status"] == 400
        assert captured["payload"]["ok"] is False

    def test_install_route_returns_503_when_hermes_cli_unavailable(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {"identifier": "owner/repo"})
        monkeypatch.setattr(routes, "bad", lambda _handler, msg, status=400: {"ok": False, "error": msg, "status": status})
        monkeypatch.setitem(sys.modules, "hermes_cli", None)
        monkeypatch.setitem(sys.modules, "hermes_cli.plugins_cmd", None)

        result = routes.handle_post(object(), SimpleNamespace(path="/api/plugins/install", query=""))

        assert result["status"] == 503


class TestPluginLifecycleHTML:
    def test_markers_exist(self):
        for marker in (
            'id="pluginLifecycleGateNote"',
            'id="pluginLifecycleForm"',
            'id="pluginInstallIdentifier"',
            'id="btnPluginInstall"',
            'id="pluginResolvePreview"',
            'id="pluginInstallResult"',
            'id="pluginLifecycleList"',
            'id="pluginLifecycleEmpty"',
        ):
            assert marker in INDEX_HTML, f"Missing {marker} in index.html"

    def test_lifecycle_section_inside_plugins_pane(self):
        pane_start = INDEX_HTML.find('id="settingsPanePlugins"')
        pane_end = INDEX_HTML.find('id="settingsPaneExtensions"')
        lifecycle_idx = INDEX_HTML.find('id="pluginLifecycleForm"')
        assert pane_start >= 0 and pane_end > pane_start
        assert pane_start < lifecycle_idx < pane_end

    def test_meta_text_no_longer_claims_read_only(self):
        """The panel gained write actions; the description must not still say read-only."""
        pane_start = INDEX_HTML.find('id="settingsPanePlugins"')
        pane_end = INDEX_HTML.find('id="settingsPaneExtensions"')
        segment = INDEX_HTML[pane_start:pane_end]
        assert "read-only" not in segment.lower()


class TestPluginLifecycleJS:
    def test_functions_exist(self):
        for fn in (
            "async function loadPluginLifecyclePanel",
            "function _renderPluginLifecycle",
            "function _buildPluginLifecycleRow",
            "function _bindPluginLifecycleControls",
            "async function _previewPluginSource",
            "async function _installPluginFromForm",
            "async function _updateInstalledPlugin",
            "async function _removeInstalledPlugin",
        ):
            assert fn in PANELS_JS, f"Missing {fn} in panels.js"

    def test_calls_expected_endpoints(self):
        assert "/api/plugins/installed" in PANELS_JS
        assert "/api/plugins/resolve?identifier=" in PANELS_JS
        assert "/api/plugins/install" in PANELS_JS
        assert "/update'" in PANELS_JS
        assert "/remove'" in PANELS_JS

    def test_install_and_remove_use_confirm_dialog(self):
        for fn_name in ("_installPluginFromForm", "_updateInstalledPlugin", "_removeInstalledPlugin"):
            idx = PANELS_JS.find(f"async function {fn_name}")
            assert idx >= 0
            body = PANELS_JS[idx:idx + 900]
            assert "showConfirmDialog" in body, f"{fn_name} must confirm before acting"

    def test_load_plugins_panel_also_loads_lifecycle_and_gates_tab_hide(self):
        idx = PANELS_JS.find("async function loadPluginsPanel")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 1400]
        assert "loadPluginLifecyclePanel" in body
        assert "write_allowed" in body
        # #3457 behavior preserved: tab still hides on empty, now also gate-aware.
        assert "data-settings-section=\"plugins\"" in body
        assert ".empty" in body

    def test_gate_note_shown_when_write_not_allowed(self):
        idx = PANELS_JS.find("function _renderPluginLifecycle")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 900]
        assert "pluginLifecycleGateNote" in body
        assert "write_allowed" in body
        assert "settings_plugin_lifecycle_gate_disabled" in body


class TestPluginLifecycleI18n:
    REQUIRED_KEYS = [
        "settings_plugin_lifecycle_title",
        "settings_plugin_lifecycle_desc",
        "settings_plugin_lifecycle_gate_disabled",
        "settings_plugin_install_placeholder",
        "settings_plugin_btn_install",
        "settings_plugin_btn_update",
        "settings_plugin_btn_remove",
        "settings_plugin_installed_title",
        "settings_plugin_installed_empty",
        "settings_plugin_resolves_to",
        "settings_plugin_insecure_scheme",
        "settings_plugin_confirm_install_title",
        "settings_plugin_confirm_install_msg",
        "settings_plugin_confirm_update_title",
        "settings_plugin_confirm_update_msg",
        "settings_plugin_confirm_remove_title",
        "settings_plugin_confirm_remove_msg",
        "settings_plugin_install_success",
        "settings_plugin_install_failed",
        "settings_plugin_update_success",
        "settings_plugin_update_unchanged",
        "settings_plugin_update_failed",
        "settings_plugin_remove_success",
        "settings_plugin_remove_failed",
        "settings_plugin_missing_env",
    ]

    def test_all_keys_present(self):
        for key in self.REQUIRED_KEYS:
            assert key in I18N_JS, f"Missing i18n key '{key}' in i18n.js"

    def test_keys_only_added_to_english_locale(self):
        for key in self.REQUIRED_KEYS:
            count = I18N_JS.count(f"{key}:")
            assert count == 1, f"i18n key '{key}' found {count} times — expected exactly 1 (en only)"

    def test_keys_precede_de_locale_block(self):
        en_start = I18N_JS.find("\n  en: {")
        de_start = I18N_JS.find("\n  de: {")
        assert en_start >= 0 and de_start > en_start
        for key in self.REQUIRED_KEYS:
            key_idx = I18N_JS.find(f"{key}:")
            assert en_start < key_idx < de_start, f"'{key}' must be inside LOCALES.en"
