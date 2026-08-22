import json

import pytest

from api import log_stream
from server import Handler


@pytest.fixture(autouse=True)
def _log_via_print(monkeypatch):
    """Route access logs through plain print() so capsys can observe them.

    These tests assert on record *content*; durable_print normally bypasses
    sys.stdout via the fd duplicated at startup (#0095), which capsys cannot
    see. tests/test_access_log_stdout_hijack.py covers the durable fd path.
    """
    monkeypatch.setattr(log_stream, "_log_fd", None)


def test_log_request_handles_malformed_request_without_path(capsys):
    """Malformed request lines can call log_request before path is assigned."""
    handler = Handler.__new__(Handler)
    handler.command = None

    Handler.log_request(handler, "400")

    line = capsys.readouterr().out.strip()
    assert line.startswith("[webui] ")
    record = json.loads(line.removeprefix("[webui] "))
    assert record["method"] == "-"
    assert record["path"] == "-"
    assert record["status"] == 400
    assert record["remote"] == "-"


def test_log_request_includes_remote_address(capsys):
    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = {}

    Handler.log_request(handler, "401")

    line = capsys.readouterr().out.strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "192.0.2.10"
    assert "forwarded_for" not in record


def test_log_request_includes_first_forwarded_for_address(capsys):
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

    line = capsys.readouterr().out.strip()
    record = json.loads(line.removeprefix("[webui] "))
    assert record["remote"] == "192.0.2.10"
    assert record["forwarded_for"] == "203.0.113.7"
