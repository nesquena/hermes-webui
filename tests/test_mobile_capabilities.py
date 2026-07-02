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


class TestMobileCapabilities:
    """/api/mobile/capabilities returns 200 with stable JSON."""

    def test_returns_200(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert captured["status"] == 200

    def test_server_is_hermes_webui(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert captured["payload"]["server"] == "hermes-webui"

    def test_includes_api_version(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert "api_version" in captured["payload"]

    def test_includes_hermex_ios_min(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert "hermex_ios_min" in captured["payload"]["compatible_clients"]

    def test_includes_all_feature_keys(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        features = captured["payload"]["features"]
        expected_keys = {
            "sessions", "resumable_runs", "run_dashboard", "approvals",
            "clarify", "workspace_search", "deployment_health",
            "file_uploads", "voice_metadata",
        }
        for key in expected_keys:
            assert key in features, f"missing feature key: {key}"

    def test_reflects_runtime_adapter_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert captured["payload"]["runtime"]["adapter"] == "legacy-direct"

    def test_reflects_runtime_adapter_legacy_journal(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert captured["payload"]["runtime"]["adapter"] == "legacy-journal"
        assert captured["payload"]["runtime"]["resumable_events"] is True

    def test_reflects_runtime_adapter_agent_runs(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")
        from api.runtime_adapters import _reset_adapter_instance_for_test

        _reset_adapter_instance_for_test()
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        assert captured["payload"]["runtime"]["adapter"] == "agent-runs"
        assert captured["payload"]["runtime"]["resumable_events"] is True
        assert captured["payload"]["features"]["approvals"] is True
        assert captured["payload"]["features"]["clarify"] is True

    def test_does_not_include_secrets(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        payload_str = json.dumps(captured["payload"])
        lower = payload_str.lower()
        for secret in ("api_key", "token", "password", "secret"):
            assert secret not in lower, f"found secret pattern '{secret}' in payload"

    def test_default_mode_features(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/capabilities")
        features = captured["payload"]["features"]
        assert features["approvals"] is False
        assert features["clarify"] is False
        assert features["run_dashboard"] is True
        assert features["sessions"] is True
