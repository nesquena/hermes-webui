import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from api.runtime_adapters.agent_runs import (
    AgentRunsError,
    AgentRunsClient,
    AgentRunsAdapter,
    _agent_runs_error_from_urllib,
    _map_agent_event_to_dict,
)


class TestAgentRunsError:
    def test_unreachable_error(self):
        err = AgentRunsError(
            "agent_runtime_unreachable",
            "Hermes Agent runtime API is not reachable at configured base URL.",
            safe_to_retry=True,
        )
        d = err.to_dict()
        assert d["error"] == "agent_runtime_unreachable"
        assert d["safe_to_retry"] is True
        assert "API key" not in d["message"]
        assert "token" not in d["message"]
        assert "Authorization" not in d["message"]

    def test_timeout_error(self):
        err = AgentRunsError(
            "agent_runtime_timeout",
            "Hermes Agent runtime API timed out.",
            safe_to_retry=True,
        )
        d = err.to_dict()
        assert d["error"] == "agent_runtime_timeout"
        assert d["safe_to_retry"] is True

    def test_auth_error(self):
        err = AgentRunsError(
            "agent_runtime_auth_error",
            "Hermes Agent runtime API rejected authentication.",
            safe_to_retry=False,
        )
        d = err.to_dict()
        assert d["error"] == "agent_runtime_auth_error"
        assert d["safe_to_retry"] is False

    def test_bad_response_error(self):
        err = AgentRunsError(
            "agent_runtime_bad_response",
            "Hermes Agent runtime API returned an invalid response.",
            safe_to_retry=True,
        )
        d = err.to_dict()
        assert d["error"] == "agent_runtime_bad_response"
        assert d["safe_to_retry"] is True

    def test_error_does_not_leak_tokens(self):
        err = AgentRunsError(
            "agent_runtime_auth_error",
            "Hermes Agent runtime API rejected authentication.",
            safe_to_retry=False,
        )
        d = err.to_dict()
        assert "sk-" not in json.dumps(d)
        assert "Bearer" not in json.dumps(d)
        assert "api_key" not in json.dumps(d).lower() or "REDACTED" in json.dumps(d)


class TestErrorMappingFromUrllib:
    def test_connection_refused_maps_to_unreachable(self):
        exc = ConnectionRefusedError("Connection refused")
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_unreachable"
        assert err.safe_to_retry is True

    def test_connection_reset_maps_to_unreachable(self):
        exc = ConnectionResetError("Connection reset")
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_unreachable"

    def test_timeout_maps_to_timeout(self):
        exc = TimeoutError("timed out")
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_timeout"
        assert err.safe_to_retry is True

    def test_401_maps_to_auth_error(self):
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:8642/v1/runs/test",
            401,
            "Unauthorized",
            {},
            MagicMock(),
        )
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_auth_error"
        assert err.safe_to_retry is False

    def test_403_maps_to_auth_error(self):
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:8642/v1/runs/test",
            403,
            "Forbidden",
            {},
            MagicMock(),
        )
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_auth_error"
        assert err.safe_to_retry is False

    def test_500_maps_to_bad_response_retryable(self):
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:8642/v1/runs/test",
            500,
            "Internal Server Error",
            {},
            MagicMock(),
        )
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_bad_response"
        assert err.safe_to_retry is True

    def test_404_maps_to_bad_response(self):
        exc = urllib.error.HTTPError(
            "http://127.0.0.1:8642/v1/runs/test",
            404,
            "Not Found",
            {},
            MagicMock(),
        )
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_bad_response"
        assert err.safe_to_retry is False

    def test_os_error_maps_to_unreachable(self):
        exc = OSError("Network unreachable")
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_unreachable"

    def test_generic_error_maps_to_bad_response(self):
        exc = ValueError("something unexpected")
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_bad_response"
        assert err.safe_to_retry is True

    def test_timeout_string_in_message(self):
        exc = Exception("Connection timed out after 60 seconds")
        err = _agent_runs_error_from_urllib(exc)
        assert err.error == "agent_runtime_timeout"


class TestEventMapping:
    def test_maps_agent_event_to_dict(self):
        raw = {
            "event_id": "run_abc:1",
            "seq": 1,
            "run_id": "run_abc",
            "session_id": "sess_1",
            "type": "token.delta",
            "created_at": 1.0,
            "terminal": False,
            "payload": {"text": "hello"},
        }
        result = _map_agent_event_to_dict(raw)
        assert result["event_id"] == "run_abc:1"
        assert result["seq"] == 1
        assert result["type"] == "token.delta"
        assert result["payload"]["text"] == "hello"

    def test_maps_event_with_string_payload(self):
        raw = {
            "event_id": "run_x:1",
            "seq": 1,
            "run_id": "run_x",
            "session_id": "sess_1",
            "type": "token.delta",
            "created_at": 1.0,
            "terminal": False,
            "payload": '{"text": "parsed"}',
        }
        result = _map_agent_event_to_dict(raw)
        assert result["payload"]["text"] == "parsed"

    def test_maps_event_with_invalid_string_payload(self):
        raw = {
            "event_id": "run_x:1",
            "seq": 1,
            "run_id": "run_x",
            "session_id": "sess_1",
            "type": "token.delta",
            "created_at": 1.0,
            "terminal": False,
            "payload": "not valid json {{{",
        }
        result = _map_agent_event_to_dict(raw)
        assert result["payload"] == {}

    def test_maps_none_to_empty_dict(self):
        result = _map_agent_event_to_dict(None)
        assert result == {}

    def test_maps_non_dict_to_empty_dict(self):
        result = _map_agent_event_to_dict("string")
        assert result == {}


class TestAgentRunsClientEnv:
    def test_from_env_requires_base_url(self):
        with pytest.raises(ValueError, match="HERMES_WEBUI_AGENT_RUNS_BASE_URL"):
            AgentRunsClient.from_env(environ={})

    def test_from_env_constructs_with_api_key(self):
        client = AgentRunsClient.from_env(
            environ={
                "HERMES_WEBUI_AGENT_RUNS_BASE_URL": "http://127.0.0.1:8642",
                "HERMES_WEBUI_AGENT_RUNS_API_KEY": "test-key-123",
            }
        )
        assert client.base_url == "http://127.0.0.1:8642"
        assert client.api_key == "test-key-123"

    def test_from_env_trailing_slash_stripped(self):
        client = AgentRunsClient.from_env(
            environ={
                "HERMES_WEBUI_AGENT_RUNS_BASE_URL": "http://127.0.0.1:8642/",
            }
        )
        assert client.base_url == "http://127.0.0.1:8642"

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError, match="http\\(s\\)"):
            AgentRunsClient(base_url="file:///etc/passwd")

    def test_auth_header_present_when_api_key_set(self):
        client = AgentRunsClient(
            base_url="http://127.0.0.1:8642", api_key="secret-token"
        )
        headers = client._headers()
        assert headers["Authorization"] == "Bearer secret-token"

    def test_auth_header_absent_when_no_api_key(self):
        client = AgentRunsClient(base_url="http://127.0.0.1:8642", api_key="")
        headers = client._headers()
        assert "Authorization" not in headers
