"""Regression coverage for issue #5210 worker-bound request dispatch."""

from __future__ import annotations

import http.client
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from server import QuietHTTPServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _worker_count(server: _ObservedQuietHTTPServer) -> int:
    with server.worker_lock:
        return server.worker_starts


def _wait_for_worker_count(server: _ObservedQuietHTTPServer, expected: int, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _worker_count(server)
        if current == expected:
            return
        if current > expected:
            raise AssertionError(f"expected worker count {expected}, got {current}")
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for worker count {expected}, got {_worker_count(server)}")


def _assert_worker_count_stays(server: _ObservedQuietHTTPServer, expected: int, *, duration: float = 0.35) -> None:
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        current = _worker_count(server)
        assert current == expected, f"expected worker count {expected}, got {current}"
        time.sleep(0.01)


def _wait_for_worker_slot_release(server: _ObservedQuietHTTPServer, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server._request_worker_slots.acquire(blocking=False):
            server._request_worker_slots.release()
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for worker slot release")


def _request(port: int, path: str, *, use_ssl: bool = False, timeout: float = 2.0) -> tuple[int, dict[str, str], bytes]:
    if use_ssl:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=timeout, context=context)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k: v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


def _start_request_thread(port: int, path: str, *, use_ssl: bool = False) -> tuple[threading.Thread, dict[str, object]]:
    result: dict[str, object] = {}

    def runner() -> None:
        try:
            result["value"] = _request(port, path, use_ssl=use_ssl, timeout=5.0)
        except Exception as exc:  # pragma: no cover - only used for failure diagnostics
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, result


def _generate_self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


class _ObservedQuietHTTPServer(QuietHTTPServer):
    def __init__(self, *args, **kwargs):
        self.worker_lock = threading.Lock()
        self.worker_starts = 0
        self.worker_started = threading.Event()
        self.release_event = threading.Event()
        self.hold_entered = threading.Event()
        self.error_entered = threading.Event()
        self.disconnect_entered = threading.Event()
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        with self.worker_lock:
            self.worker_starts += 1
            self.worker_started.set()
        return super().process_request_thread(request, client_address)


class _GateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # pragma: no cover - noise suppression
        pass

    def _send_ok(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/hold":
            self.server.hold_entered.set()
            if not self.server.release_event.wait(timeout=5):
                raise TimeoutError("release_event not set")
            self._send_ok()
            return
        if self.path == "/boom":
            self.server.error_entered.set()
            if not self.server.release_event.wait(timeout=5):
                raise TimeoutError("release_event not set")
            raise RuntimeError("boom")
        if self.path == "/disconnect":
            self.server.disconnect_entered.set()
            if not self.server.release_event.wait(timeout=5):
                raise TimeoutError("release_event not set")
            self._send_ok()
            return
        if self.path == "/fast":
            self._send_ok()
            return
        self.send_error(404)


class _ServerRunner:
    def __init__(self, handler_cls, *, ssl_context=None):
        self.port = _free_port()
        self.httpd = _ObservedQuietHTTPServer(("127.0.0.1", self.port), handler_cls)
        self.httpd.ssl_context = ssl_context
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        time.sleep(0.1)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.release_event.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def test_over_capacity_plain_http_gets_fast_503_without_starting_handler(monkeypatch):
    monkeypatch.setattr(QuietHTTPServer, "max_request_workers", 1, raising=False)

    with _ServerRunner(_GateHandler) as srv:
        hold_thread, hold_result = _start_request_thread(srv.port, "/hold")
        assert srv.httpd.hold_entered.wait(timeout=5)
        _wait_for_worker_count(srv.httpd, 1)

        status, headers, body = _request(srv.port, "/fast")

        assert status == 503
        assert headers["Connection"].lower() == "close"
        assert body == b""
        _assert_worker_count_stays(srv.httpd, 1)

        srv.httpd.release_event.set()
        hold_thread.join(timeout=5)
        assert "error" not in hold_result, hold_result.get("error")
        assert hold_result["value"][0] == 200


def test_slow_overflow_cleanup_does_not_block_later_rejects(monkeypatch):
    monkeypatch.setattr(QuietHTTPServer, "max_request_workers", 1, raising=False)
    drain_entered = threading.Event()
    release_drain = threading.Event()
    original_drain = QuietHTTPServer._drain_request_input_nonblocking
    calls = 0

    def blocking_first_drain(self, request) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            drain_entered.set()
            assert release_drain.wait(timeout=5)
            return
        original_drain(self, request)

    monkeypatch.setattr(QuietHTTPServer, "_drain_request_input_nonblocking", blocking_first_drain)

    with _ServerRunner(_GateHandler) as srv:
        hold_thread, hold_result = _start_request_thread(srv.port, "/hold")
        assert srv.httpd.hold_entered.wait(timeout=5)
        _wait_for_worker_count(srv.httpd, 1)

        slow_sock = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
        try:
            assert drain_entered.wait(timeout=5)
            status, headers, body = _request(srv.port, "/fast", timeout=1)
            assert status == 503
            assert headers["Connection"].lower() == "close"
            assert body == b""
            _assert_worker_count_stays(srv.httpd, 1)
        finally:
            release_drain.set()
            slow_sock.close()

        srv.httpd.release_event.set()
        hold_thread.join(timeout=5)
        assert "error" not in hold_result, hold_result.get("error")
        assert hold_result["value"][0] == 200


def test_worker_slot_releases_after_request_finishes(monkeypatch):
    monkeypatch.setattr(QuietHTTPServer, "max_request_workers", 1, raising=False)

    with _ServerRunner(_GateHandler) as srv:
        hold_thread, hold_result = _start_request_thread(srv.port, "/hold")
        assert srv.httpd.hold_entered.wait(timeout=5)
        _wait_for_worker_count(srv.httpd, 1)

        status, headers, body = _request(srv.port, "/fast")
        assert status == 503
        assert headers["Connection"].lower() == "close"
        assert body == b""

        srv.httpd.release_event.set()
        hold_thread.join(timeout=5)
        assert "error" not in hold_result, hold_result.get("error")
        assert hold_result["value"][0] == 200

        status, headers, body = _request(srv.port, "/fast")
        assert status == 200
        assert headers["Connection"].lower() == "close"
        assert body == b"ok"


@pytest.mark.parametrize("mode", ["boom", "disconnect"])
def test_worker_slot_releases_after_handler_error_or_disconnect(monkeypatch, mode):
    monkeypatch.setattr(QuietHTTPServer, "max_request_workers", 1, raising=False)

    with _ServerRunner(_GateHandler) as srv:
        if mode == "boom":
            held_thread, _ = _start_request_thread(srv.port, "/boom")
            assert srv.httpd.error_entered.wait(timeout=5)
        else:
            sock = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
            sock.sendall(
                b"GET /disconnect HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            held_thread = None
            assert srv.httpd.disconnect_entered.wait(timeout=5)

        _wait_for_worker_count(srv.httpd, 1)
        status, headers, body = _request(srv.port, "/fast")
        assert status == 503
        assert headers["Connection"].lower() == "close"
        assert body == b""

        if mode == "boom":
            srv.httpd.release_event.set()
            held_thread.join(timeout=5)
            assert not held_thread.is_alive()
        else:
            sock.close()
            srv.httpd.release_event.set()
        _wait_for_worker_slot_release(srv.httpd)

        status, headers, body = _request(srv.port, "/fast")
        assert status == 200
        assert headers["Connection"].lower() == "close"
        assert body == b"ok"


def test_long_running_request_does_not_reject_healthy_in_cap_request(monkeypatch):
    monkeypatch.setattr(QuietHTTPServer, "max_request_workers", 2, raising=False)

    with _ServerRunner(_GateHandler) as srv:
        hold_thread, hold_result = _start_request_thread(srv.port, "/hold")
        assert srv.httpd.hold_entered.wait(timeout=5)
        _wait_for_worker_count(srv.httpd, 1)

        status, headers, body = _request(srv.port, "/fast")
        assert status == 200
        assert headers["Connection"].lower() == "close"
        assert body == b"ok"
        _wait_for_worker_count(srv.httpd, 2)

        srv.httpd.release_event.set()
        hold_thread.join(timeout=5)
        assert "error" not in hold_result, hold_result.get("error")
        assert hold_result["value"][0] == 200


def test_tls_overflow_does_not_force_accept_loop_handshake(monkeypatch, tmp_path):
    monkeypatch.setattr(QuietHTTPServer, "max_request_workers", 1, raising=False)
    cert, key = _generate_self_signed_cert(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)

    with _ServerRunner(_GateHandler, ssl_context=server_context) as srv:
        hold_thread, hold_result = _start_request_thread(srv.port, "/hold", use_ssl=True)
        assert srv.httpd.hold_entered.wait(timeout=5)
        _wait_for_worker_count(srv.httpd, 1)

        raw = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
        try:
            raw.settimeout(2)
            assert raw.recv(1) == b""
        finally:
            raw.close()

        _assert_worker_count_stays(srv.httpd, 1)

        srv.httpd.release_event.set()
        hold_thread.join(timeout=5)
        assert "error" not in hold_result, hold_result.get("error")
        assert hold_result["value"][0] == 200
