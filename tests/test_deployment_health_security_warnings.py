import json
import os
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


def _default_handler(remote_addr="127.0.0.1", headers=None):
    import http.client

    hdrs = http.client.HTTPMessage()
    if headers:
        for k, v in headers.items():
            hdrs[k] = v
    handler = MagicMock()
    handler.headers = hdrs
    handler.client_address = (remote_addr, 54321)
    return handler


class TestServerAuthWarnings:
    """Server and auth-related security warnings."""

    def test_loopback_without_password_no_public_bind_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.auth as auth_mod

        monkeypatch.setattr(auth_mod, "get_password_hash", lambda: None)

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        assert captured["payload"]["network"]["public_bind_warning"] is False
        warns = captured["payload"]["security"]["warnings"]
        assert not any("public interface" in w.lower() for w in warns)

    def test_public_bind_without_password_produces_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "0.0.0.0")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.auth as auth_mod

        monkeypatch.setattr(auth_mod, "get_password_hash", lambda: None)

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        assert captured["payload"]["network"]["public_bind_warning"] is True
        warns = captured["payload"]["security"]["warnings"]
        assert any("public interface" in w.lower() for w in warns)

    def test_public_bind_with_password_no_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "0.0.0.0")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.auth as auth_mod

        monkeypatch.setattr(auth_mod, "get_password_hash", lambda: "fake_hash")

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        assert not any("public interface" in w.lower() and "without password" in w.lower() for w in warns)

    def test_http_likely_public_access_produces_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/tmp/ws", "exists": True, "writable": True,
        })

        captured, fake_j = _capture_j()
        handler = _default_handler(remote_addr="203.0.113.42")
        handler.request = None
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        assert any("HTTP" in w for w in warns)

    def test_tailscale_client_suppresses_http_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        captured, fake_j = _capture_j()
        handler = _default_handler(remote_addr="100.64.0.1")
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        payload = captured["payload"]
        assert payload["network"]["tailscale_likely"] is True
        warns = payload["security"]["warnings"]
        assert not any("HTTP" in w for w in warns)

    def test_loopback_localhost_suppresses_http_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        captured, fake_j = _capture_j()
        handler = _default_handler(remote_addr="127.0.0.1")
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        assert not any("HTTP" in w for w in warns)

    def test_cf_tunnel_headers_suppress_http_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        cf_headers = {
            "CF-Connecting-IP": "1.2.3.4",
            "CF-Ray": "abc123",
            "Cf-Visitor": '{"scheme":"https"}',
            "X-Forwarded-Proto": "https",
        }
        captured, fake_j = _capture_j()
        handler = _default_handler(remote_addr="203.0.113.42", headers=cf_headers)
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        payload = captured["payload"]
        assert payload["network"]["cloudflare_tunnel_likely"] is True
        warns = payload["security"]["warnings"]
        assert not any("HTTP" in w for w in warns)


class TestRuntimeWarnings:
    """Runtime adapter-related warnings."""

    def test_legacy_direct_adapter_produces_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        assert any("legacy-direct" in w.lower() for w in warns)

    def test_legacy_journal_adapter_does_not_produce_legacy_direct_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        assert not any("legacy-direct" in w.lower() for w in warns)

    def test_agent_runs_unreachable_produces_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:18642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/tmp/ws", "exists": True, "writable": True,
        })

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        payload = captured["payload"]
        assert payload["runtime"]["runtime_adapter"] == "agent-runs"
        assert payload["runtime"]["agent_runtime_reachable"] is False
        warns = payload["security"]["warnings"]
        assert any("not reachable" in w.lower() for w in warns)

    def test_agent_runs_reachable_does_not_produce_unreachable_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        class FakeResponse:
            status = 200
            def read(self):
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=None, *args, **kwargs):
            return FakeResponse()

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/tmp/ws", "exists": True, "writable": True,
        })

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            with patch("urllib.request.urlopen", fake_urlopen):
                _call_get(handler, "http://localhost/api/deployment/health")

        payload = captured["payload"]
        assert payload["runtime"]["runtime_adapter"] == "agent-runs"
        assert payload["runtime"]["agent_runtime_reachable"] is True
        warns = payload["security"]["warnings"]
        assert not any("not reachable" in w.lower() for w in warns)


class TestSecurityWarnings:
    """OS isolation and secret-exclusion security warnings."""

    def test_local_terminal_backend_reports_not_isolated(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        sec = captured["payload"]["security"]
        assert sec["os_isolation_status"] in ("not_isolated", "isolated", "unknown")

    def test_local_terminal_backend_has_warning(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "not_isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "local")

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        sec = captured["payload"]["security"]
        assert sec["os_isolation_status"] == "not_isolated"
        warns = sec["warnings"]
        assert any("sandbox" in w.lower() for w in warns)

    def test_explicit_isolated_backend_reports_isolated(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        sec = captured["payload"]["security"]
        assert sec["os_isolation_status"] == "isolated"

    def test_warnings_do_not_include_api_keys(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "sk-secret-key-12345")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "not_isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "local")
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": None, "exists": False, "writable": False,
        })

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        warns_str = json.dumps(warns)
        assert "sk-secret-key-12345" not in warns_str

    def test_warnings_do_not_include_tokens(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "not_isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "local")
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": None, "exists": False, "writable": False,
        })

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        warns = captured["payload"]["security"]["warnings"]
        warns_str = json.dumps(warns)
        assert "Bearer" not in warns_str
        assert "Authorization" not in warns_str

    def test_warnings_do_not_include_passwords(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "my-secret-pw")
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.deployment_health as dh

        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "not_isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "local")
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": None, "exists": False, "writable": False,
        })

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        payload_str = json.dumps(captured["payload"])
        assert "my-secret-pw" not in payload_str

    def test_passwordless_auth_not_enabled_in_safe_config(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "")
        import api.config as config_module

        monkeypatch.setattr(config_module, "HOST", "127.0.0.1")
        monkeypatch.setattr(config_module, "PORT", 8787)

        import api.auth as auth_mod
        import api.deployment_health as dh

        monkeypatch.setattr(auth_mod, "get_password_hash", lambda: None)
        monkeypatch.setattr(dh, "_resolve_os_isolation_status", lambda: "isolated")
        monkeypatch.setattr(dh, "_resolve_terminal_backend", lambda: "remote")
        monkeypatch.setattr(dh, "_providers_configured", lambda: True)
        monkeypatch.setattr(dh, "_resolve_workspace_info", lambda: {
            "path": "/tmp/ws", "exists": True, "writable": True,
        })

        captured, fake_j = _capture_j()
        handler = _default_handler()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/deployment/health")

        assert captured["payload"]["server"]["password_auth_enabled"] is False
        assert captured["payload"]["network"]["public_bind_warning"] is False
