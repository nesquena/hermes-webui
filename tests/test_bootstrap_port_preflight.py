"""Regression tests for bootstrap port-availability preflight (issue #8111)."""

from __future__ import annotations

import errno
import socket
import sys
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
