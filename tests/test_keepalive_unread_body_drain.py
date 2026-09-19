"""Regression tests for keep-alive connection sync after early-exit requests.

A rejected POST (auth redirect 302 / 401) returns before the route handler
reads the request body. With HTTP/1.1 keep-alive, those unread body bytes were
then parsed as the next request line, corrupting the connection — observed in
production as:

    501 Unsupported method ('{"session_id":"..."}GET')

whenever the browser fired a rejected POST and then a GET on the same
connection (via a keep-alive reverse proxy). The fix drains any unread
Content-Length body (or closes the connection when it cannot) before the next
request is read.

These tests boot the real ``server.Handler`` on a loopback socket and assert
the connection stays in sync across an early-exit POST.
"""
from __future__ import annotations

import http.client
import io
import json
import socket
import threading
from unittest import mock

import pytest

import api.auth
from api.helpers import MAX_BODY_BYTES
from http.server import BaseHTTPRequestHandler
from server import Handler, QuietHTTPServer, _BodyTrackingReader


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerRunner:
    """Boot the real Handler on a loopback port.

    With the ``password_auth`` fixture active, write requests hit the auth
    gate and are rejected 401 before any route handler reads the request
    body — the exact early-exit path that poisoned keep-alive connections in
    production.
    """

    def __init__(self):
        self.port = _free_port()
        self.httpd = QuietHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def password_auth(monkeypatch):
    """Enable password auth for the in-process server (auth gate active).

    get_password_hash caches per process; reset the cache so the env var set
    here is honored, and restore state afterwards.
    """
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "keepalive-regression-test")
    monkeypatch.setattr(api.auth, "_AUTH_HASH_COMPUTED", False, raising=False)
    monkeypatch.setattr(api.auth, "_AUTH_HASH_CACHE", None, raising=False)
    yield
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)


def _post_then_get_same_connection(port: int) -> tuple[int, bytes, int, bytes]:
    """POST a body-carrying request that gets rejected early, then GET on the
    same keep-alive connection. Returns (post_status, post_body, get_status,
    get_body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = json.dumps({"session_id": "20260827_080609_a123e437"}).encode()
        conn.request(
            "POST",
            "/api/session/import_cli",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        post_status, post_body = resp.status, resp.read()

        # Second request on the SAME connection. Without the drain fix the
        # server parses the leftover body bytes as this request's method line.
        conn.request("GET", "/api/session?session_id=20260827_080609_a123e437")
        resp2 = conn.getresponse()
        get_status, get_body = resp2.status, resp2.read()
        return post_status, post_body, get_status, get_body
    finally:
        conn.close()


def test_unread_post_body_does_not_poison_next_keepalive_request(password_auth):
    """A body-carrying POST rejected before read_body must not corrupt the
    next request on the same keep-alive connection.

    Reproduces the production failure: POST /api/session/import_cli rejected
    by the auth gate (body never read), then GET /api/session on the same
    connection parsed the leftover JSON body as the request method → 501 HTML.
    """
    with _ServerRunner() as srv:
        post_status, post_body, get_status, get_body = _post_then_get_same_connection(
            srv.port
        )

    # The POST is rejected by the auth gate without its body being read.
    assert post_status == 401, post_body[:200]

    # The follow-up request must be parsed cleanly: a well-formed 401 JSON
    # API response, NOT the BaseHTTPRequestHandler HTML error page built from
    # a corrupted request line.
    assert get_status != 501, get_body[:200]
    assert not get_body.lstrip().startswith(b"<!DOCTYPE"), get_body[:200]
    assert get_status == 401, get_body[:200]
    assert b"Authentication required" in get_body


def _unsupported_te_then_get(
    port: int, headers: dict, body: bytes, check_post_status: bool = True
) -> tuple[int, bytes, bool]:
    """POST with unframable transfer framing (rejected early by the auth gate),
    then GET on the same keep-alive connection.

    Returns (get_status, get_body, connection_alive) — connection_alive False
    means the server closed the socket after the rejected request (fail-closed),
    so the GET was never answered on that connection."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST",
            "/api/session/import_cli",
            body=body,
            headers={"Content-Type": "application/json", **headers},
        )
        resp = conn.getresponse()
        post_status, _ = resp.status, resp.read()
        if check_post_status:
            assert post_status == 401, post_status

        # Next request on the same connection: if the server failed closed it
        # closed the socket (RemoteDisconnected); if it left the body on the
        # socket, those bytes are parsed as this request line → 501 HTML.
        conn.request("GET", "/api/session?session_id=20260827_080609_a123e437")
        resp2 = conn.getresponse()
        return resp2.status, resp2.read(), True
    except (http.client.RemoteDisconnected, ConnectionError, OSError):
        # Server closed the connection after the rejected request: fail-closed.
        return 0, b"", False
    finally:
        conn.close()


@pytest.mark.parametrize(
    "te_headers",
    [
        {"Transfer-Encoding": "gzip"},  # unsupported TE, no usable framing
        {"Transfer-Encoding": "gzip", "Content-Length": "43"},  # conflicting TE+CL
        {"Transfer-Encoding": "chunked"},  # pins existing close behavior
        {"Transfer-Encoding": "gzip, chunked"},
    ],
)
def test_transfer_encoding_fails_closed(password_auth, te_headers):
    """Any nonempty Transfer-Encoding must fail closed: the server implements
    no transfer framing, so the body cannot be drained reliably and keep-alive
    must not reuse the connection (review finding, PR #7368).

    Reproduces the maintainer-verified desync: POST with ``Transfer-Encoding:
    gzip`` rejected by the auth gate leaves the body on the socket; the next
    request line parses those bytes → 501 HTML. The TE+Content-Length case
    must also close rather than implicitly trusting the content length."""
    body = b'{"session_id":"20260827_080609_a123e437"}'
    with _ServerRunner() as srv:
        get_status, get_body, connection_alive = _unsupported_te_then_get(
            srv.port,
            te_headers,
            body,
        )

    # Fail-closed: after a TE request the server must close the connection
    # rather than parsing a following request on it.
    assert connection_alive is False, (get_status, get_body[:200])


def test_negative_content_length_fails_closed(password_auth):
    """A negative Content-Length must close rather than leave a desynced
    keep-alive connection. On Python 3.13 parse_request does NOT reject the
    framing (the drain sees length < 0, skips the loop, keeps the connection):
    the body then desyncs the next request → 501 HTML (reproduced)."""
    body = b'{"session_id":"20260827_080609_a123e437"}'
    with _ServerRunner() as srv:
        get_status, get_body, connection_alive = _unsupported_te_then_get(
            srv.port,
            {"Content-Length": "-5"},
            body,
            check_post_status=False,  # stdlib may 400-close before the handler
        )

    assert connection_alive is False, (get_status, get_body[:200])


def test_oversized_content_length_fails_closed_without_draining():
    """A Content-Length ``read_body`` would refuse must close, not drain.

    A rejected request claiming more than MAX_BODY_BYTES must fail the
    connection closed immediately: draining it would burn bandwidth and pin
    a thread, and ``read_body`` itself closes on such lengths — the drain
    mirrors that cap so both paths use the same limit. Asserts zero bytes
    are consumed (pre-cap code would read the buffered bytes first).
    """
    body = b"x" * 100
    handler = Handler.__new__(Handler)
    handler.rfile = _BodyTrackingReader(io.BytesIO(body))  # type: ignore[assignment]
    handler.headers = {"Content-Length": str(MAX_BODY_BYTES + 1)}  # type: ignore[assignment]
    handler.close_connection = False

    handler._drain_unread_body()

    assert handler.close_connection is True
    assert handler.rfile.count == 0  # type: ignore[attr-defined]
    assert handler.rfile._inner.getvalue() == body  # type: ignore[attr-defined]


def test_body_byte_counter_resets_per_request(password_auth):
    """After a request whose body WAS read, a later early-exit POST must still drain.

    Regression for the review finding on PR #7368: the rfile wrapper persists
    for the whole keep-alive connection, so a cumulative counter left the
    drain computing a per-request remainder against a connection-lifetime
    total — silently skipping the unread body and reproducing the original
    corruption on the next request.

    Sequence on ONE connection (all bodies exactly 41 bytes so the stale
    counter would exactly cancel the new body):
      1. POST /api/auth/login  (public path, body read by route → counter = 41)
      2. POST /api/session/import_cli (auth gate rejects 401, body UNREAD)
      3. GET  /api/session     — must be clean JSON 401, not an HTML error page
    """
    # Both bodies must be the SAME length so the stale connection-lifetime
    # counter exactly equals the second request's Content-Length.
    def _padded(pairs: dict, target: int) -> bytes:
        probe = json.dumps({**pairs, "pad": ""})
        n = target - len(probe)  # each extra pad char adds exactly 1 byte
        assert n >= 0, (probe, target)
        return json.dumps({**pairs, "pad": "0" * n}).encode()

    cli_body = _padded({"session_id": "20260827_080609_a123e437"}, 60)
    login_body = _padded({"username": "x", "password": "y"}, 60)
    assert len(login_body) == len(cli_body)

    with _ServerRunner() as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        try:
            # 1. Body-reading request (public, CSRF-exempt): route consumes the body.
            conn.request(
                "POST",
                "/api/auth/login",
                body=login_body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(login_body))},
            )
            r1 = conn.getresponse()
            login_status, login_resp = r1.status, r1.read()
            assert login_status == 401, login_resp[:200]
            assert not login_resp.lstrip().startswith(b"<!DOCTYPE"), login_resp[:200]

            # 2. Early-exit request: rejected by the auth gate before read_body.
            conn.request(
                "POST",
                "/api/session/import_cli",
                body=cli_body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(cli_body))},
            )
            r2 = conn.getresponse()
            post_status, post_resp = r2.status, r2.read()
            assert post_status == 401, post_resp[:200]

            # 3. Without a per-request counter reset the stale count (41) cancels
            # this body (41) and the drain skips it, corrupting this request.
            conn.request("GET", "/api/session?session_id=20260827_080609_a123e437")
            r3 = conn.getresponse()
            get_status, get_body = r3.status, r3.read()
        finally:
            conn.close()

    assert get_status != 501, get_body[:200]
    assert not get_body.lstrip().startswith(b"<!DOCTYPE"), get_body[:200]
    assert get_status == 401, get_body[:200]
    assert b"Authentication required" in get_body


def _read_raw_response(fp) -> tuple[int, bytes]:
    """Read one HTTP/1.1 response from a buffered socket file.

    Returns (status, body). Raises on timeout/EOF before the status line.
    """
    status_line = fp.readline(8192)
    assert status_line, "expected an HTTP response status line, got EOF"
    parts = status_line.split()
    assert parts[:1] == [b"HTTP/1.1"] and len(parts) >= 2, status_line[:200]
    status = int(parts[1])
    length = 0
    while True:
        line = fp.readline(8192)
        assert line, "EOF while reading response headers"
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip() or 0)
    body = b""
    while len(body) < length:
        chunk = fp.read(length - len(body))
        if not chunk:
            break
        body += chunk
    return status, body


def test_stale_headers_fail_closed(password_auth):
    """A request that fails before header parsing must not drain under stale framing.

    Regression for the review finding on PR #7368: ``Handler`` is reused for
    the whole keep-alive connection, so without a per-request ``headers``
    reset a request B with an overlong request line (stdlib returns before
    assigning current headers) would inherit request A's ``Content-Length``
    in the ``finally`` drain and consume bytes belonging to malformed B or
    pipelined request C (``remaining = L - 0`` after the counter reset).

    Sequence on ONE raw socket:
      1. POST /api/auth/login with a 60-byte body (route reads it → 401 JSON)
      2. Overlong request line pipelined with a valid GET (request C)
    The server must answer the overlong line (414) and then close without
    ever answering C — C bytes must not be consumed as B's body.
    """
    login_body = json.dumps(
        {"username": "x", "password": "y", "pad": "0" * 15}
    ).encode()
    assert len(login_body) == 60, len(login_body)

    with _ServerRunner() as srv:
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        sock.settimeout(5)
        try:
            fp = sock.makefile("rb")
            # 1. Body-reading request: route consumes the 60-byte body.
            req_a = (
                "POST /api/auth/login HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(login_body)}\r\n"
                "Connection: keep-alive\r\n"
                "\r\n"
            ).encode() + login_body
            sock.sendall(req_a)
            login_status, login_resp = _read_raw_response(fp)
            assert login_status == 401, login_resp[:200]
            assert not login_resp.lstrip().startswith(b"<!DOCTYPE"), login_resp[:200]

            # 2. Overlong request line + pipelined valid GET on the same socket.
            # The line is sized just over the stdlib 65536-byte limit so the
            # leftover line tail is SMALLER than request A's Content-Length:
            # without the headers reset the stale drain (remaining = L - 0)
            # consumes the tail plus the head of pipelined C as B's "body".
            req_b = (
                f"GET /{'A' * 65530} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Connection: keep-alive\r\n"
                "\r\n"
            ).encode()
            req_c = (
                "GET /api/session?session_id=20260827_080609_a123e437 HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Connection: keep-alive\r\n"
                "\r\n"
            ).encode()
            sock.sendall(req_b + req_c)
            err_status, err_body = _read_raw_response(fp)
            assert err_status == 414, (err_status, err_body[:200])

            # Fail-closed: the server closes instead of consuming C as B's
            # body — EOF, never a second HTTP response.
            tail = fp.read()
            assert tail == b"", tail[:200]
        finally:
            sock.close()


def test_stale_headers_never_authorize_a_drain():
    """The per-request finally must close, never drain, without current headers.

    If a request fails before header parsing assigns current headers while
    the connection is still marked reusable (e.g. a stdlib path that answers
    an error without closing), the finally must close the connection rather
    than consume bytes under any framing. ``handle_one_request`` clears
    ``headers`` before dispatch, so the stub below sees ``None`` — there is
    no stale request-A framing left to inherit.

    ``c_bytes`` stands in for bytes after malformed B (pipelined request C):
    without the fail-closed boundary the drain would consume bytes as B's
    "body" and leave the connection open; fixed, the connection closes and
    every byte is untouched.
    """
    c_bytes = (
        b"GET /api/session?session_id=20260827_080609_a123e437 HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
    )

    handler = Handler.__new__(Handler)
    handler.rfile = _BodyTrackingReader(io.BytesIO(c_bytes))  # type: ignore[assignment]
    handler.headers = None  # type: ignore[assignment]
    handler.close_connection = False

    def _return_before_headers(handler_self):
        # Simulates a stdlib path that answers before parse_request assigns
        # current headers (overlong request line) without closing. The
        # per-request reset in handle_one_request has already cleared headers.
        assert handler_self.headers is None
        assert handler_self.close_connection is False

    with mock.patch.object(
        BaseHTTPRequestHandler, "handle_one_request", _return_before_headers
    ):
        handler.handle_one_request()

    assert handler.close_connection is True
    assert handler.rfile.count == 0  # type: ignore[attr-defined]
    assert handler.rfile._inner.getvalue() == c_bytes  # type: ignore[attr-defined]
