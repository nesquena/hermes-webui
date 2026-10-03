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
    assert record["client_ip"] == "-"


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
    assert record["client_ip"] == "192.0.2.10"
    assert "forwarded_for" not in record


def test_log_request_includes_first_forwarded_for_address(log_output):
    class Headers:
        def get(self, key, default=None):
            if key == "X-Forwarded-For":
                return "203.0.113.7, 198.51.100.9"
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
    # Untrusted raw peer cannot spoof client_ip via headers:
    assert record["client_ip"] == "192.0.2.10"
    assert record["forwarded_for"] == "203.0.113.7"


def test_log_request_resolves_client_ip_from_trusted_proxy_loopback(log_output):
    class Headers:
        def get(self, key, default=None):
            if key == "X-Forwarded-For":
                return "203.0.113.7, 198.51.100.9"
            return default

    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/health"
    handler.client_address = ("127.0.0.1", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "200")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "127.0.0.1"
    # From trusted proxy, walks right-to-left to pick first non-trusted hop
    assert record["client_ip"] == "198.51.100.9"
    assert record["forwarded_for"] == "203.0.113.7"


def test_log_request_skips_intermediate_trusted_proxies(monkeypatch, log_output):
    class Headers:
        def get(self, key, default=None):
            if key == "X-Forwarded-For":
                return "203.0.113.7, 198.51.100.9"
            return default

    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "198.51.100.0/24")

    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/health"
    handler.client_address = ("127.0.0.1", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "200")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "127.0.0.1"
    # Intermediate proxy 198.51.100.9 is trusted, so client_ip is the real client 203.0.113.7
    assert record["client_ip"] == "203.0.113.7"


def test_log_request_fails_closed_on_malformed_forwarded_chain_from_trusted_proxy(log_output):
    class Headers:
        def get(self, key, default=None):
            if key == "X-Forwarded-For":
                return "not-a-valid-ip"
            return default

    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/health"
    handler.client_address = ("127.0.0.1", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "200")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "127.0.0.1"
    # Malformed chain fails closed to '-' so fail2ban / downstream security won't ban innocent IPs
    assert record["client_ip"] == "-"

