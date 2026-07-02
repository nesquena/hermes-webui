import importlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from api.runtime_adapter import (
    ControlResult,
    RunStartResult,
    RunStatus,
    RunEventStream,
    StartRunRequest,
)
from api.runtime_adapters.agent_runs import (
    AgentRunsError,
    AgentRunsClient,
    AgentRunsAdapter,
    _map_agent_event_to_dict,
)
from api.runtime_adapters import _reset_adapter_instance_for_test

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


def _call_post(handler, path, body=None):
    routes = _routes()
    parsed = urlparse(path)
    bp = body or {}
    with patch("api.routes.read_body", return_value=bp), patch(
        "api.routes._check_csrf", return_value=True
    ), patch("api.routes._check_same_origin_browser_request", return_value=True):
        return routes.handle_post(handler, parsed)


def _capture_j():
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None, pretty=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    return captured, fake_j


def _patch_j(fake_j):
    return patch("api.helpers.j", side_effect=fake_j)


# ── Adapter start_run ────────────────────────────────────────────────────


class TestAgentRunsAdapterStartRun:
    def test_start_run_posts_to_expected_path(self):
        client = MagicMock(spec=AgentRunsClient)
        client.start_run.return_value = {
            "run_id": "run_1",
            "session_id": "sess_1",
            "status": "started",
        }
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        request = StartRunRequest(
            session_id="sess_1",
            message="hello",
            workspace="/tmp",
            profile="default",
            model="claude-sonnet-4-5",
            toolsets=["file", "bash"],
        )
        result = adapter.start_run(request)
        client.start_run.assert_called_once()
        call_args = client.start_run.call_args[0][0]
        assert call_args["session_id"] == "sess_1"
        assert call_args["message"] == "hello"
        assert call_args["workspace"] == "/tmp"
        assert call_args["profile"] == "default"
        assert call_args["model"] == "claude-sonnet-4-5"
        assert call_args["toolsets"] == ["file", "bash"]
        assert call_args["metadata"]["client"] == "webui"
        assert "client_version" in call_args["metadata"]
        assert result.run_id == "run_1"
        assert result.session_id == "sess_1"

    def test_start_run_auth_header_not_exposed_in_errors(self):
        client = MagicMock(spec=AgentRunsClient)
        client.start_run.side_effect = AgentRunsError(
            "agent_runtime_auth_error",
            "Hermes Agent runtime API rejected authentication.",
            safe_to_retry=False,
        )
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        request = StartRunRequest(session_id="sess_1", message="test")
        result = adapter.start_run(request)
        assert result.status == "error"
        error_payload = result.payload
        assert error_payload["error"] == "agent_runtime_auth_error"
        assert "Bearer" not in json.dumps(error_payload)
        assert "Authorization" not in json.dumps(error_payload)
        assert "token" not in error_payload.get("message", "").lower()


# ── Adapter get_run ──────────────────────────────────────────────────────


class TestAgentRunsAdapterGetRun:
    def test_get_run_fetches_status(self):
        client = MagicMock(spec=AgentRunsClient)
        client.get_status.return_value = {
            "run_id": "run_1",
            "session_id": "sess_1",
            "status": "running",
            "controls": ["cancel"],
        }
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        status = adapter.get_run("run_1")
        client.get_status.assert_called_once_with("run_1")
        assert status.run_id == "run_1"
        assert status.status == "running"
        assert status.active_controls == ["cancel"]

    def test_get_run_maps_terminal_status(self):
        client = MagicMock(spec=AgentRunsClient)
        client.get_status.return_value = {
            "run_id": "run_1",
            "session_id": "sess_1",
            "status": "completed",
            "controls": [],
        }
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        status = adapter.get_run("run_1")
        assert status.terminal_state == "completed"
        assert status.status == "completed"

    def test_get_run_handles_error_gracefully(self):
        client = MagicMock(spec=AgentRunsClient)
        client.get_status.side_effect = AgentRunsError(
            "agent_runtime_unreachable",
            "not reachable",
            safe_to_retry=True,
        )
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        status = adapter.get_run("run_1")
        assert status.status == "unknown"


# ── Adapter observe_run ──────────────────────────────────────────────────


class TestAgentRunsAdapterObserveRun:
    def test_observe_run_gets_events(self):
        client = MagicMock(spec=AgentRunsClient)
        client.observe_events.return_value = {
            "run_id": "run_1",
            "events": [
                {
                    "event_id": "run_1:1",
                    "seq": 1,
                    "run_id": "run_1",
                    "session_id": "sess_1",
                    "type": "token.delta",
                    "created_at": 1.0,
                    "terminal": False,
                    "payload": {"text": "hi"},
                }
            ],
        }
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        stream = adapter.observe_run("run_1")
        client.observe_events.assert_called_once_with("run_1", after_seq=None)
        assert len(stream.events) == 1
        assert stream.events[0]["type"] == "token.delta"

    def test_observe_run_sends_after_seq(self):
        client = MagicMock(spec=AgentRunsClient)
        client.observe_events.return_value = {"run_id": "run_1", "events": []}
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        adapter.observe_run("run_1", cursor="run_1:5")
        client.observe_events.assert_called_once_with("run_1", after_seq=5)

    def test_observe_run_sends_limit(self):
        client = MagicMock(spec=AgentRunsClient)
        client.observe_events.return_value = {"run_id": "run_1", "events": []}
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        adapter.observe_run("run_1")
        client.observe_events.assert_called_once()


# ── Adapter cancel / approval / clarify ──────────────────────────────────


class TestAgentRunsAdapterControls:
    def test_cancel_run_posts_to_stop(self):
        client = MagicMock(spec=AgentRunsClient)
        client.cancel_run.return_value = {"ok": True, "status": "cancelling"}
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        result = adapter.cancel_run("run_1")
        client.cancel_run.assert_called_once_with("run_1")

    def test_cancel_run_not_supported(self):
        client = MagicMock(spec=AgentRunsClient)
        client.cancel_run.return_value = {
            "error": "not_supported",
            "message": "Cancel not supported.",
        }
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        result = adapter.cancel_run("run_1")
        assert result.accepted is False
        assert result.status == "not_supported"

    def test_resolve_approval_posts_to_approval(self):
        client = MagicMock(spec=AgentRunsClient)
        client.resolve_approval.return_value = {"ok": True}
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        result = adapter.respond_approval("run_1", "appr_1", "once")
        client.resolve_approval.assert_called_once_with("run_1", "appr_1", "once")

    def test_resolve_approval_not_supported(self):
        client = MagicMock(spec=AgentRunsClient)
        client.resolve_approval.return_value = {
            "error": "not_supported",
            "message": "Not supported.",
        }
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        result = adapter.respond_approval("run_1", "appr_1", "accept")
        assert result.accepted is False

    def test_resolve_clarify_posts_to_clarify(self):
        client = MagicMock(spec=AgentRunsClient)
        client.resolve_clarify.return_value = {"ok": True}
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        result = adapter.respond_clarify("run_1", "clar_1", "my answer")
        client.resolve_clarify.assert_called_once_with("run_1", "clar_1", answer="my answer")

    def test_resolve_clarify_not_supported(self):
        client = MagicMock(spec=AgentRunsClient)
        client.resolve_clarify.return_value = {
            "error": "not_supported",
            "message": "Not supported.",
        }
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        result = adapter.respond_clarify("run_1", "clar_1", "answer")
        assert result.accepted is False


# ── Error redaction ──────────────────────────────────────────────────────


class TestErrorRedaction:
    def test_auth_header_not_in_agent_runs_error(self):
        err = AgentRunsError(
            "agent_runtime_auth_error",
            "Hermes Agent runtime API rejected authentication.",
        )
        d = err.to_dict()
        serialized = json.dumps(d)
        assert "Bearer" not in serialized
        assert "sk-" not in serialized
        assert "api_key" not in serialized.lower()

    def test_unreachable_error_does_not_contain_url(self):
        err = AgentRunsError(
            "agent_runtime_unreachable",
            "Hermes Agent runtime API is not reachable at configured base URL.",
        )
        d = err.to_dict()
        assert "http://" not in d["message"].lower() or "base URL" in d["message"]

    def test_adapter_cancel_error_no_credentials(self):
        client = MagicMock(spec=AgentRunsClient)
        client.cancel_run.side_effect = AgentRunsError(
            "agent_runtime_auth_error",
            "Hermes Agent runtime API rejected authentication.",
            safe_to_retry=False,
        )
        adapter = AgentRunsAdapter.__new__(AgentRunsAdapter)
        adapter._client = client
        result = adapter.cancel_run("run_1")
        payload_str = json.dumps(result.payload)
        assert "Bearer" not in payload_str
        assert "sk-" not in payload_str


# ── Route integration: capabilities ──────────────────────────────────────


class TestRouteCapabilitiesAgentRuns:
    def test_capabilities_reports_agent_runs_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        _reset_adapter_instance_for_test()
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/runtime/capabilities")
        assert captured["payload"]["runtime_adapter"] == "agent-runs"
        assert captured["payload"]["supports"]["resumable_events"] is True
        assert captured["payload"]["supports"]["last_event_id"] is True
        assert captured["payload"]["supports"]["cancel"] is True
        assert captured["payload"]["supports"]["approval"] is True
        assert captured["payload"]["supports"]["clarify"] is True


# ── Route integration: run status ────────────────────────────────────────


class TestRouteRunStatusAgentRuns:
    def test_run_status_uses_adapter_in_agent_runs_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        _reset_adapter_instance_for_test()
        mock_adapter = MagicMock()
        mock_adapter.get_run.return_value = RunStatus(
            run_id="run_test", session_id="sess_1", status="running", active_controls=["cancel"]
        )
        with patch(
            "api.runtime_routes._adapter", return_value=mock_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            handler.headers = MagicMock()
            handler.headers.get = lambda k, d=None: "application/json"
            with _patch_j(fake_j):
                _call_get(handler, "http://localhost/api/runs/run_test")
            assert captured["payload"]["run_id"] == "run_test"
            assert captured["payload"]["status"] == "running"


# ── Route integration: run events ────────────────────────────────────────


class TestRouteRunEventsAgentRuns:
    def test_run_events_uses_adapter_in_agent_runs_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        _reset_adapter_instance_for_test()
        mock_adapter = MagicMock()
        mock_adapter.observe_run.return_value = RunEventStream(
            run_id="run_test",
            events=[
                {
                    "event_id": "run_test:1",
                    "seq": 1,
                    "run_id": "run_test",
                    "session_id": "sess_1",
                    "type": "token.delta",
                    "created_at": 1.0,
                    "terminal": False,
                    "payload": {"text": "hello"},
                }
            ],
            cursor="1",
            last_event_id="run_test:1",
        )
        with patch(
            "api.runtime_routes._adapter", return_value=mock_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            handler.headers = MagicMock()
            handler.headers.get = lambda k, d=None: "application/json"
            with _patch_j(fake_j):
                _call_get(handler, "http://localhost/api/runs/run_test/events")
            assert captured["payload"]["run_id"] == "run_test"
            assert len(captured["payload"]["events"]) == 1
            assert captured["payload"]["events"][0]["type"] == "token.delta"

    def test_run_events_respects_after_seq_agent_runs(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        _reset_adapter_instance_for_test()
        mock_adapter = MagicMock()
        mock_adapter.observe_run.return_value = RunEventStream(
            run_id="run_test", events=[], cursor="5", last_event_id=None,
        )
        with patch(
            "api.runtime_routes._adapter", return_value=mock_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            handler.headers = MagicMock()
            handler.headers.get = lambda k, d=None: "application/json"
            with _patch_j(fake_j):
                _call_get(
                    handler,
                    "http://localhost/api/runs/run_test/events?after_seq=3&limit=10",
                )
            mock_adapter.observe_run.assert_called_once_with("run_test", cursor="3")


# ── Route integration: controls ──────────────────────────────────────────


class TestRouteControlsAgentRuns:
    def test_cancel_uses_adapter_in_agent_runs_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        _reset_adapter_instance_for_test()
        mock_adapter = MagicMock()
        mock_adapter.cancel_run.return_value = ControlResult(
            accepted=True, status="cancelling",
        )
        with patch(
            "api.runtime_routes._adapter", return_value=mock_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            with _patch_j(fake_j):
                _call_post(
                    handler,
                    "http://localhost/api/runs/run_test/cancel",
                    {"run_id": "run_test"},
                )
            mock_adapter.cancel_run.assert_called_once_with("run_test")
            assert captured["payload"]["ok"] is True

    def test_approval_uses_adapter_in_agent_runs_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        _reset_adapter_instance_for_test()
        mock_adapter = MagicMock()
        mock_adapter.respond_approval.return_value = ControlResult(
            accepted=True, status="resolved",
        )
        with patch(
            "api.runtime_routes._adapter", return_value=mock_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            with _patch_j(fake_j):
                _call_post(
                    handler,
                    "http://localhost/api/runs/run_test/approval",
                    {"run_id": "run_test", "approval_id": "appr_1", "choice": "once"},
                )
            mock_adapter.respond_approval.assert_called_once_with("run_test", "appr_1", "once")
            assert captured["payload"]["ok"] is True

    def test_clarify_uses_adapter_in_agent_runs_mode(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        _reset_adapter_instance_for_test()
        mock_adapter = MagicMock()
        mock_adapter.respond_clarify.return_value = ControlResult(
            accepted=True, status="resolved",
        )
        with patch(
            "api.runtime_routes._adapter", return_value=mock_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            with _patch_j(fake_j):
                _call_post(
                    handler,
                    "http://localhost/api/runs/run_test/clarify",
                    {"run_id": "run_test", "clarify_id": "clar_1", "response": "yes"},
                )
            mock_adapter.respond_clarify.assert_called_once_with("run_test", "clar_1", "yes")
            assert captured["payload"]["ok"] is True


# ── Legacy compatibility ─────────────────────────────────────────────────


class TestLegacyCompatibility:
    def test_default_mode_routes_still_work(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/runtime/capabilities")
        assert captured["status"] == 200
        assert captured["payload"]["runtime_adapter"] == "legacy-direct"

    def test_legacy_journal_cancel_still_returns_not_supported_when_no_adapter(
        self, monkeypatch
    ):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_post(
                handler,
                "http://localhost/api/runs/test_run/cancel",
                {"run_id": "test_run"},
            )
        assert captured["payload"]["error"] == "not_supported"

    def test_legacy_journal_approval_still_returns_not_supported(
        self, monkeypatch
    ):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_post(
                handler,
                "http://localhost/api/runs/test_run/approval",
                {"run_id": "test_run"},
            )
        assert captured["payload"]["error"] == "not_supported"

    def test_legacy_journal_clarify_still_returns_not_supported(
        self, monkeypatch
    ):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_post(
                handler,
                "http://localhost/api/runs/test_run/clarify",
                {"run_id": "test_run"},
            )
        assert captured["payload"]["error"] == "not_supported"


# ── AgentRunsClient HTTP contract ────────────────────────────────────────


class TestAgentRunsClientContract:
    def test_start_run_path(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642")
        with patch.object(client, "_request_json") as mock_req:
            mock_req.return_value = {"run_id": "r1", "status": "started"}
            client.start_run({"session_id": "s1", "message": "hello"})
            call_req = mock_req.call_args[0][0]
            assert call_req.get_full_url() == "http://127.0.0.1:8642/v1/runs"
            assert call_req.get_method() == "POST"

    def test_get_status_path(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642")
        with patch.object(client, "_request_json") as mock_req:
            mock_req.return_value = {"run_id": "r1", "status": "running"}
            client.get_status("r1")
            call_req = mock_req.call_args[0][0]
            assert "/v1/runs/r1" in call_req.get_full_url()
            assert call_req.get_method() == "GET"

    def test_observe_events_path(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642")
        with patch.object(client, "_request_json") as mock_req:
            mock_req.return_value = {"run_id": "r1", "events": []}
            client.observe_events("r1", after_seq=5, limit=10)
            call_req = mock_req.call_args[0][0]
            url = call_req.get_full_url()
            assert "/v1/runs/r1/events" in url
            assert "after_seq=5" in url
            assert "limit=10" in url
            assert call_req.get_method() == "GET"

    def test_observe_events_no_params(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642")
        with patch.object(client, "_request_json") as mock_req:
            mock_req.return_value = {"run_id": "r1", "events": []}
            client.observe_events("r1")
            call_req = mock_req.call_args[0][0]
            url = call_req.get_full_url()
            assert "?" not in url

    def test_cancel_run_path(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642")
        with patch.object(client, "_request_json") as mock_req:
            mock_req.return_value = {"ok": True}
            client.cancel_run("r1")
            call_req = mock_req.call_args[0][0]
            assert "/v1/runs/r1/stop" in call_req.get_full_url()
            assert call_req.get_method() == "POST"

    def test_resolve_approval_path(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642")
        with patch.object(client, "_request_json") as mock_req:
            mock_req.return_value = {"ok": True}
            client.resolve_approval("r1", "appr_1", "once")
            call_req = mock_req.call_args[0][0]
            assert "/v1/runs/r1/approval" in call_req.get_full_url()
            assert call_req.get_method() == "POST"

    def test_resolve_clarify_path(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642")
        with patch.object(client, "_request_json") as mock_req:
            mock_req.return_value = {"ok": True}
            client.resolve_clarify("r1", "clar_1", "answer here")
            call_req = mock_req.call_args[0][0]
            assert "/v1/runs/r1/clarify" in call_req.get_full_url()
            assert call_req.get_method() == "POST"


# ── Import isolation ─────────────────────────────────────────────────────


def test_agent_runs_adapter_does_not_require_live_http():
    ad = AgentRunsAdapter(base_url="http://127.0.0.1:8642")
    assert ad is not None


def test_route_modules_import_cleanly():
    mod = importlib.import_module("api.runtime_routes")
    assert hasattr(mod, "handle_runtime_capabilities")
    assert hasattr(mod, "handle_run_cancel")
    assert hasattr(mod, "handle_run_approval")
    assert hasattr(mod, "handle_run_clarify")
