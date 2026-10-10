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


def test_log_request_omits_forwarded_for_from_untrusted_peer(log_output):
    """#7863: a direct client must not be able to write `forwarded_for`.

    The socket peer here is a public address (192.0.2.10, TEST-NET-1), i.e. NOT
    a trusted proxy — so the left-most X-Forwarded-For hop it supplied is
    attacker-chosen and must not be recorded. The field is omitted entirely
    rather than filled with a value nobody can trust, because the log feeds
    fail2ban-style jails that would ban an arbitrary third party.

    This test previously asserted the opposite contract (the first hop was
    logged verbatim). Issue #7863 calls that out explicitly as behaviour that
    "would need updating": `[0]` is the end of the chain the client itself
    writes, and nothing verified the peer was a proxy.
    """
    class Headers:
        def get(self, key):
            assert key == "X-Forwarded-For"
            return "203.0.113.7, 198.51.100.9"

        def get_all(self, key):
            # The resolver walks the full chain, so the double must be able to
            # serve it — without this the helper would raise, get swallowed by
            # log_request's `except`, and the field would be missing for the
            # wrong reason (an exception, not the peer check).
            assert key == "X-Forwarded-For"
            return ["203.0.113.7, 198.51.100.9"]

    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "401")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "192.0.2.10"
    assert "forwarded_for" not in record


def test_log_request_records_resolved_client_from_trusted_proxy(log_output):
    """#7863: a trusted (loopback) proxy peer lets the real client IP be logged.

    Same-host reverse-proxy / tunnel deployments are exactly the case the field
    exists for: `remote` is just 127.0.0.1, so the resolved client IP comes from
    walking the X-Forwarded-For chain right-to-left and returning the first hop
    that is not itself a trusted proxy. Here neither hop is trusted, so the
    right-most one (198.51.100.9) is the client the chain actually describes —
    NOT the left-most 203.0.113.7, which is the end a direct client writes.
    """
    class Headers:
        def get(self, key):
            assert key == "X-Forwarded-For"
            return "203.0.113.7, 198.51.100.9"

        def get_all(self, key):
            assert key == "X-Forwarded-For"
            return ["203.0.113.7, 198.51.100.9"]

    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("127.0.0.1", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "401")

    line = log_output.getvalue().strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "127.0.0.1"
    assert record["forwarded_for"] == "198.51.100.9"
