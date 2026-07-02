import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

_ROUTES_MOD = None


def _routes():
    global _ROUTES_MOD
    if _ROUTES_MOD is None:
        import api.routes as mod

        _ROUTES_MOD = mod
    return _ROUTES_MOD


def _call_get(handler, path):
    routes = _routes()
    return routes.handle_get(handler, urlparse(path))


def _capture_j():
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None, pretty=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    return captured, fake_j


def _patch_j(fake_j):
    return patch("api.helpers.j", side_effect=fake_j)


def _default_handler():
    import http.client

    headers = http.client.HTTPMessage()
    client_addr = ("127.0.0.1", 54321)
    handler = MagicMock()
    handler.headers = headers
    handler.client_address = client_addr
    return handler


class TestDeploymentHealthResponse:
    """GET /api/deployment/health response shape and content."""

    def test_returns_200(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["status"] == 200

    def test_includes_status_top_level(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert "status" in captured["payload"]

    def test_includes_server_section(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        server = captured["payload"]["server"]
        assert "host" in server
        assert "port" in server
        assert "password_auth_enabled" in server
        assert "https" in server
        assert "secure_cookie" in server

    def test_includes_network_section(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        network = captured["payload"]["network"]
        assert "tailscale_likely" in network
        assert "cloudflare_tunnel_likely" in network
        assert "public_bind_warning" in network

    def test_includes_runtime_section(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        runtime = captured["payload"]["runtime"]
        assert "webui_version" in runtime
        assert "runtime_adapter" in runtime
        assert "agent_runtime_reachable" in runtime
        assert "agent_api_version" in runtime

    def test_includes_providers_section(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        providers = captured["payload"]["providers"]
        assert "configured" in providers
        assert "model_list_reachable" in providers

    def test_includes_workspace_section(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        workspace = captured["payload"]["workspace"]
        assert "path" in workspace
        assert "exists" in workspace
        assert "writable" in workspace

    def test_includes_security_section(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        security = captured["payload"]["security"]
        assert "os_isolation_status" in security
        assert "terminal_backend" in security
        assert "warnings" in security

    def test_includes_runtime_adapter(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["payload"]["runtime"]["runtime_adapter"] == "legacy-journal"

    def test_does_not_include_secrets(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        payload_str = json.dumps(captured["payload"])
        lower = payload_str.lower()
        for secret in ("api_key", "token", "bearer"):
            assert secret not in lower, f"found secret pattern '{secret}' in payload"

    def test_schema_is_stable(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        payload = captured["payload"]
        top_keys = {"status", "server", "network", "runtime", "providers",
                    "workspace", "security"}
        assert set(payload.keys()) == top_keys, f"unexpected top-level keys: {set(payload.keys()) - top_keys}"

    def test_status_warning_when_warnings_exist(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.deployment_health as dh

        captured, fake_j = _capture_j()
        handler = _default_handler()

        def mock_resolve_os():
            return "not_isolated"

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", mock_resolve_os)
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "local")
        monkeypatch.setattr(dh, "_providers_configured", lambda: False)

        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": None, "exists": False, "writable": False,
        })

        import api.config as config_module
        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warnings = captured["payload"]["security"]["warnings"]
        assert len(warnings) > 0
        assert captured["payload"]["status"] == "warning"

    def test_status_ok_under_safe_config(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        import api.deployment_health as dh

        captured, fake_j = _capture_j()
        handler = _default_handler()

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)

        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/tmp/ws", "exists": True, "writable": True,
        })

        import api.config as config_module
        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        assert captured["payload"]["status"] == "ok"
        assert len(captured["payload"]["security"]["warnings"]) == 0

    def test_providers_configured_when_config_has_providers_section(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["payload"]["providers"]["configured"] is True

    def test_providers_configured_false_when_no_providers(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_providers_configured", lambda: False)
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["payload"]["providers"]["configured"] is False

    def test_model_list_reachable_is_null(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["payload"]["providers"]["model_list_reachable"] is None

    def test_runtime_adapter_reported_for_legacy_journal(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["payload"]["runtime"]["runtime_adapter"] == "legacy-journal"

    def test_runtime_adapter_reported_for_agent_runs(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["payload"]["runtime"]["runtime_adapter"] == "agent-runs"

    def test_runtime_adapter_reported_for_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")
        assert captured["payload"]["runtime"]["runtime_adapter"] == "legacy-direct"


class TestWorkspaceInfo:
    """Workspace path/exists/writable detection."""

    def test_missing_workspace_produces_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.deployment_health as dh

        captured, fake_j = _capture_j()
        handler = _default_handler()

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": None, "exists": False, "writable": False,
        })

        import api.config as config_module
        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        assert any("missing" in w.lower() or "unavailable" in w.lower() for w in warns)

    def test_existing_writable_workspace_reports_true(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        import api.deployment_health as dh

        captured, fake_j = _capture_j()
        handler = _default_handler()

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/tmp/test-ws", "exists": True, "writable": True,
        })

        import api.config as config_module
        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        ws = captured["payload"]["workspace"]
        assert ws["exists"] is True
        assert ws["writable"] is True
        assert len(captured["payload"]["security"]["warnings"]) == 0

    def test_existing_non_writable_reports_false(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.deployment_health as dh

        captured, fake_j = _capture_j()
        handler = _default_handler()

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/tmp/test-ws", "exists": True, "writable": False,
        })

        import api.config as config_module
        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        ws = captured["payload"]["workspace"]
        assert ws["exists"] is True
        assert ws["writable"] is False
        warns = captured["payload"]["security"]["warnings"]
        assert any("writable" in w.lower() for w in warns)

    def test_workspace_path_does_not_leak_host_paths(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.deployment_health as dh

        captured, fake_j = _capture_j()
        handler = _default_handler()

        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/home/user/workspace", "exists": True, "writable": True,
        })

        import api.config as config_module
        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        payload_str = json.dumps(captured["payload"])
        assert "/etc/" not in payload_str
        assert "/root" not in payload_str


class TestMobileIntegration:
    """/api/mobile/capabilities reports deployment_health true."""

    def test_mobile_capabilities_deployment_health_true(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        features = captured["payload"]["features"]
        assert features["deployment_health"] is True
