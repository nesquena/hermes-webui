"""Regression tests for bootstrap port-availability preflight (issue #8111)."""

from __future__ import annotations

import contextlib
import errno
import http.server
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402


class _StubSocket:
    """Controlled socket so bind outcomes never depend on machine settings."""

    instances: list[_StubSocket] = []

    def __init__(
        self,
        family: int = socket.AF_INET,
        sock_type: int = socket.SOCK_STREAM,
        *args: object,
        **kwargs: object,
    ) -> None:
        self.family = family
        self.sock_type = sock_type
        self.bind_calls: list[tuple] = []
        self.closed = False
        _StubSocket.instances.append(self)

    def setsockopt(self, *args: object, **kwargs: object) -> None:
        return None

    def bind(self, address: tuple) -> None:
        self.bind_calls.append(address)

    def close(self) -> None:
        self.closed = True


class _BusySocket(_StubSocket):
    """bind() reports the port as taken, whatever the host allows."""

    def bind(self, address: tuple) -> None:
        self.bind_calls.append(address)
        raise OSError(errno.EADDRINUSE, "Address already in use")


class _RefusingSocket(_StubSocket):
    """bind() fails like a non-local or otherwise invalid address."""

    def bind(self, address: tuple) -> None:
        self.bind_calls.append(address)
        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")


def _occupy(host: str = "127.0.0.1") -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def test_available_port_passes() -> None:
    sock, port = _occupy()
    sock.close()
    # Port just released; preflight should not raise.
    bootstrap._check_port_available("127.0.0.1", port)


def test_occupied_port_raises_with_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.socket, "socket", _BusySocket)
    monkeypatch.setattr(
        bootstrap, "_port_is_available", lambda host, candidate: candidate == 9101
    )
    with pytest.raises(RuntimeError) as excinfo:
        bootstrap._check_port_available("127.0.0.1", 9099)
    message = str(excinfo.value)
    assert "Port 9099 on 127.0.0.1 is already in use" in message
    assert "./start.sh 9101" in message
    assert "HERMES_WEBUI_PORT=9101" in message
    assert "no configuration was changed" in message


def test_no_free_alternative_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.socket, "socket", _BusySocket)
    monkeypatch.setattr(bootstrap, "_port_is_available", lambda host, candidate: False)
    with pytest.raises(RuntimeError) as excinfo:
        bootstrap._check_port_available("127.0.0.1", 9099)
    assert "No free alternative port" in str(excinfo.value)


def test_find_free_port_skips_taken_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    taken = {9000, 9001}
    monkeypatch.setattr(
        bootstrap, "_port_is_available", lambda host, candidate: candidate not in taken
    )
    assert bootstrap._find_free_port("127.0.0.1", 8999) == 9002


def test_wildcard_host_kept_for_bind_check() -> None:
    assert bootstrap._bind_host_for_check("") == "0.0.0.0"
    assert bootstrap._bind_host_for_check("0.0.0.0") == "0.0.0.0"
    assert bootstrap._bind_host_for_check("::") == "::"
    assert bootstrap._bind_host_for_check("[::]") == "::"
    assert bootstrap._bind_host_for_check("192.168.1.5") == "192.168.1.5"


@pytest.mark.parametrize("host", ["", "0.0.0.0"])
def test_ipv4_wildcard_check_binds_wildcard_address(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    bootstrap._check_port_available(host, 8787)
    stub = _StubSocket.instances[-1]
    assert stub.family == socket.AF_INET
    assert stub.bind_calls == [("0.0.0.0", 8787)]


@pytest.mark.parametrize("host", ["::", "[::]"])
def test_ipv6_wildcard_check_binds_wildcard_address(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    bootstrap._check_port_available(host, 8787)
    stub = _StubSocket.instances[-1]
    assert stub.family == socket.AF_INET6
    assert stub.bind_calls == [("::", 8787)]


def test_specific_host_checked_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    bootstrap._check_port_available("192.168.1.5", 8787)
    assert _StubSocket.instances[-1].bind_calls == [("192.168.1.5", 8787)]


def test_invalid_host_reports_bind_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.socket, "socket", _RefusingSocket)
    with pytest.raises(RuntimeError) as excinfo:
        bootstrap._check_port_available("203.0.113.1", 8787)
    assert "Cannot bind 203.0.113.1:8787" in str(excinfo.value)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="bind semantics for reused/overlapping addresses differ by platform",
)
def test_real_occupied_loopback_port_raises() -> None:
    sock, port = _occupy("127.0.0.1")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            bootstrap._check_port_available("127.0.0.1", port)
        assert "already in use" in str(excinfo.value)
    finally:
        sock.close()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="bind semantics for reused/overlapping addresses differ by platform",
)
def test_real_wildcard_check_catches_interface_only_listener() -> None:
    sock, port = _occupy("127.0.0.1")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            bootstrap._check_port_available("0.0.0.0", port)
        assert "already in use" in str(excinfo.value)
    finally:
        sock.close()


# ---------- review follow-up (#8112): an occupied port may be our own WebUI --


def _raise_in_use(*_args: object, **_kwargs: object) -> None:
    raise bootstrap.PortInUseError(
        "Port 8787 on 127.0.0.1 is already in use by another service."
    )


def _stub_main_up_to_preflight(monkeypatch: pytest.MonkeyPatch, argv: list) -> None:
    """Stub what main() touches before the preflight; pin argv and supervisor env."""
    monkeypatch.setattr(bootstrap, "ensure_supported_platform", lambda: None)
    monkeypatch.setattr(bootstrap, "open_browser", lambda url: None)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"] + argv)
    for name in (
        "INVOCATION_ID",
        "JOURNAL_STREAM",
        "NOTIFY_SOCKET",
        "XPC_SERVICE_NAME",
        "SUPERVISOR_ENABLED",
        "HERMES_WEBUI_FOREGROUND",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("host", ["", "0.0.0.0", "::", "[::]"])
def test_already_serving_scheme_probes_localhost_for_wildcard(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    seen: list = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        assert kwargs.get("markers") == bootstrap._HERMES_HEALTH_MARKERS
        seen.append((url, timeout))
        return "http"

    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    assert bootstrap._already_serving_scheme(host, 8787) == "http"
    assert seen == [("http://localhost:8787/health", 1.0)]


def test_already_serving_scheme_empty_when_nothing_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap, "wait_for_health", lambda url, timeout=0.0, **kwargs: ""
    )
    assert bootstrap._already_serving_scheme("127.0.0.1", 8787) == ""


def test_occupied_port_with_healthy_webui_reports_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")

    assert bootstrap.main() == 0
    assert "already running" in capsys.readouterr().out


def test_occupied_port_with_healthy_webui_opens_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_main_up_to_preflight(monkeypatch, [])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")
    opened: list = []
    monkeypatch.setattr(bootstrap, "open_browser", opened.append)

    assert bootstrap.main() == 0
    assert opened == ["http://localhost:" + str(bootstrap.DEFAULT_PORT)]


def test_occupied_port_by_foreign_listener_still_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "")

    with pytest.raises(RuntimeError, match="already in use"):
        bootstrap.main()


def test_occupied_port_in_foreground_keeps_duplicate_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_main_up_to_preflight(monkeypatch, ["--foreground"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")

    with pytest.raises(RuntimeError, match="already in use"):
        bootstrap.main()


def test_port_conflict_raises_the_dedicated_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a conflict is recoverable; main() catches just this type."""
    monkeypatch.setattr(bootstrap.socket, "socket", _BusySocket)
    monkeypatch.setattr(bootstrap, "_port_is_available", lambda host, candidate: False)
    with pytest.raises(bootstrap.PortInUseError):
        bootstrap._check_port_available("127.0.0.1", 9099)


@contextlib.contextmanager
def _serve(body: bytes) -> Iterator[int]:
    """A /health listener that answers 200 with exactly ``body``.

    Yields the port; the server runs on a background thread.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with http.server.HTTPServer(("127.0.0.1", port), _Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield port
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def test_foreign_status_ok_listener_is_not_the_webui() -> None:
    """A generic status-ok body is a foreign listener, not our WebUI (#8112)."""
    with _serve(b'{"status": "ok"}') as port:
        assert bootstrap._already_serving_scheme("127.0.0.1", port) == ""


def test_webui_health_payload_still_takes_the_running_path() -> None:
    """The WebUI payload from api/routes.py::_handle_health is recognised."""
    body = (
        b'{"status": "ok", "sessions": 0, "server_started_at": 1.0, '
        b'"uptime_seconds": 1.0}'
    )
    with _serve(body) as port:
        assert bootstrap._already_serving_scheme("127.0.0.1", port) == "http"


def test_invalid_bind_address_is_not_recovered_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EADDRNOTAVAIL stays an error even when a probe would answer.

    With a remote --host the bind fails locally while a health probe can still
    reach the machine named there; reporting that as "already running" would
    hide a bootstrap that started nothing at all.
    """
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser"])
    monkeypatch.setattr(bootstrap.socket, "socket", _RefusingSocket)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")

    with pytest.raises(RuntimeError, match="Cannot bind"):
        bootstrap.main()
