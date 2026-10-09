"""Regression tests for bootstrap port-availability preflight (issue #8111)."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402


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


def test_occupied_port_raises_with_suggestion() -> None:
    sock, port = _occupy()
    try:
        with pytest.raises(RuntimeError) as excinfo:
            bootstrap._check_port_available("127.0.0.1", port)
        message = str(excinfo.value)
        assert str(port) in message
        assert "./start.sh" in message
        assert "HERMES_WEBUI_PORT" in message
    finally:
        sock.close()


def test_find_free_port_returns_available() -> None:
    sock, port = _occupy()
    try:
        alternative = bootstrap._find_free_port("127.0.0.1", port)
        assert alternative is not None
        assert alternative > port
        assert bootstrap._port_is_available("127.0.0.1", alternative)
    finally:
        sock.close()


def test_wildcard_host_normalized_to_loopback() -> None:
    assert bootstrap._bind_host_for_check("0.0.0.0") == "127.0.0.1"
    assert bootstrap._bind_host_for_check("") == "127.0.0.1"
    assert bootstrap._bind_host_for_check("::") == "127.0.0.1"
    assert bootstrap._bind_host_for_check("192.168.1.5") == "192.168.1.5"


def test_invalid_host_reports_bind_error() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        bootstrap._check_port_available("203.0.113.1", 8787)
    assert "Cannot bind" in str(excinfo.value)
