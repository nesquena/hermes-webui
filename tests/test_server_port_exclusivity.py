"""Duplicate-instance guard: a second server on the same port must be detected
and refused before bind, not silently shared (#3289)."""

from __future__ import annotations

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests._pytest_port import TEST_PORT


# ── SO_EXCLUSIVEADDRUSE on Windows ──────────────────────────────────────────

@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-only socket option')
def test_exclusive_addr_use_set_on_windows():
    from server import QuietHTTPServer
    port = TEST_PORT + 901
    httpd = QuietHTTPServer(('127.0.0.1', port), BaseHTTPRequestHandler)
    try:
        val = httpd.socket.getsockopt(
            socket.SOL_SOCKET,
            getattr(socket, 'SO_EXCLUSIVEADDRUSE', -5),
        )
        assert val != 0, 'SO_EXCLUSIVEADDRUSE should be set on Windows'
    finally:
        httpd.server_close()


# ── Live-listener probe ─────────────────────────────────────────────────────

def test_probe_detects_live_server():
    """_abort_if_already_serving must call sys.exit when a live server responds."""
    from server import _abort_if_already_serving

    port = TEST_PORT + 902

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        def log_message(self, *a):
            pass

    httpd = HTTPServer(('127.0.0.1', port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(SystemExit):
            _abort_if_already_serving('127.0.0.1', port)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_probe_allows_startup_when_nothing_listening():
    """_abort_if_already_serving must return normally on a free port."""
    from server import _abort_if_already_serving

    port = TEST_PORT + 903
    _abort_if_already_serving('127.0.0.1', port)


def test_probe_allows_startup_on_unresponsive_socket():
    """A socket that accepts but never responds (e.g. dying instance still in
    kernel backlog) should not block startup."""
    from server import _abort_if_already_serving

    port = TEST_PORT + 904
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', port))
    srv.listen(1)
    try:
        _abort_if_already_serving('127.0.0.1', port)
    finally:
        srv.close()


def test_probe_normalizes_wildcard_host():
    """0.0.0.0 and :: should probe 127.0.0.1, not the literal wildcard."""
    from server import _abort_if_already_serving

    port = TEST_PORT + 905
    _abort_if_already_serving('0.0.0.0', port)
    _abort_if_already_serving('::', port)
