import json
import io

import pytest

from server import Handler


@pytest.fixture
def log_output(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr("api.request_logging._STREAM", output)
    return output


def test_log_request_handles_malformed_request_without_path(log_output):
    """Malformed request lines can call log_request before path is assigned."""
    handler = Handler.__new__(Handler)
    handler.command = None

    Handler.log_request(handler, "400")

    line = log_output.getvalue().strip()
    assert line.startswith("[webui] ")
    record = json.loads(line.removeprefix("[webui] "))
    assert record["method"] == "-"
    assert record["path"] == "-"
    assert record["status"] == 400
    assert record["remote"] == "-"


def test_log_request_includes_remote_address(log_output):
    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = {}

    Handler.log_request(handler, "401")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "192.0.2.10"
    assert "forwarded_for" not in record


def test_log_request_includes_first_forwarded_for_address(log_output):
    class Headers:
        def get(self, key):
            assert key == "X-Forwarded-For"
            return "203.0.113.7, 198.51.100.9"

    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "401")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "192.0.2.10"
    assert record["forwarded_for"] == "203.0.113.7"
