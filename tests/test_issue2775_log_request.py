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


def test_log_request_never_records_client_writable_forwarded_hop(log_output):
    """#7863: the first X-Forwarded-For value is written by the client.

    It is no longer logged. This peer is not a trusted proxy (and the operator
    has not opted in), so the trustworthy value is the raw socket peer.
    """
    class Headers:
        def get_all(self, key):
            return ["203.0.113.7, 198.51.100.9"] if key == "X-Forwarded-For" else []

        def get(self, key, default=None):
            return default

    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "401")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "192.0.2.10"
    assert record["client_ip"] == "192.0.2.10"
    assert "forwarded_for" not in record
