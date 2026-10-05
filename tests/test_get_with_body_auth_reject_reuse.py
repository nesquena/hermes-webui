"""A body-bearing GET that fails auth must not poison a keep-alive connection.

The sibling files cover the WRITE paths: a rejected POST/PUT/PATCH/DELETE arms the
close through ``check_auth_or_close``. ``do_GET`` was the documented residual — it
called plain ``check_auth``, so a GET that *declares a body* and then fails auth
answered its 401/302 and left those bytes queued in ``rfile``. On a keep-alive
socket the next request is then parsed starting mid-body, and the client gets an
error about a request it never sent.

A GET with a body is unusual but entirely legal, and it is what a proxy, a
``fetch`` with a body, or a scripted client can produce.

Both halves of the contract are asserted here, because the naive fix (close every
auth-failed GET) would regress the body-less login-redirect flow:

* body-bearing + auth-failed -> connection MUST close (framing unresynchronisable)
* body-less  + auth-failed -> connection MUST stay alive (nothing queued)

``test_unfixed_handler_reproduces_the_poisoning`` is the negative control: the same
socket exchange against a handler that answers without arming the close shows the
leftover body surfacing as a bogus ``400 Bad request syntax``, proving the exchange
really does exercise the bug rather than passing vacuously.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = b'{"q": "search"}'


def _get_with_body(path=b"/api/sessions"):
    return (
        b"GET " + path + b" HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(BODY)).encode() + b"\r\n"
        b"\r\n" + BODY
    )


BODYLESS_GET = (
    b"GET /api/sessions HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"\r\n"
)
FOLLOWING_GET = b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n"


def _declares_body(handler):
    """The production rule: framing declares a body, so it is queued in rfile."""
    raw = handler.headers.get("Content-Length")
    if raw is not None:
        stripped = raw.strip(" \t")
        if not (stripped.isascii() and stripped.isdigit()):
            return True  # unreadable framing -> fail closed
        try:
            if int(stripped) > 0:
                return True
        except ValueError:
            return True
    return bool((handler.headers.get("Transfer-Encoding") or "").strip())


class _AuthRejectingHandler(BaseHTTPRequestHandler):
    """Mirrors do_GET's shape: reject before the body is ever read."""

    protocol_version = "HTTP/1.1"
    arm_close = True

    def do_GET(self):
        if self.path == "/api/health":
            payload = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        # Auth failure BEFORE the body is read — the residual's exact shape.
        if self.arm_close and _declares_body(self):
            self.close_connection = True
        payload = b'{"error": "unauthorized"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if self.arm_close and _declares_body(self):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class _UnfixedHandler(_AuthRejectingHandler):
    arm_close = False


def _exchange(handler_cls, request_bytes):
    """Pipeline `request_bytes` + a follow-up GET down ONE socket; return the reply."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(srv.server_address, timeout=5) as s:
            s.sendall(request_bytes + FOLLOWING_GET)
            s.settimeout(5)
            chunks = []
            while True:
                try:
                    b = s.recv(4096)
                except socket.timeout:
                    break
                if not b:
                    break
                chunks.append(b)
            return b"".join(chunks)
    finally:
        srv.shutdown()
        srv.server_close()


def test_body_bearing_get_that_fails_auth_closes_the_connection():
    raw = _exchange(_AuthRejectingHandler, _get_with_body())
    assert b"401" in raw, raw[:200]
    assert b"Connection: close" in raw, f"401 did not advertise close: {raw[:200]}"
    # The follow-up GET must NEVER be answered on this socket.
    assert raw.count(b"HTTP/1.1") == 1, f"connection was reused: {raw[:300]}"
    assert b'"status": "ok"' not in raw, f"follow-up GET was served: {raw[:300]}"
    # And the leftover body must never surface as a bogus request line.
    assert b"Bad request syntax" not in raw, raw[:300]
    assert b"501" not in raw, raw[:300]


def test_bodyless_get_that_fails_auth_keeps_keep_alive():
    """The over-close half: nothing is queued, so the socket must stay usable."""
    raw = _exchange(_AuthRejectingHandler, BODYLESS_GET)
    assert b"401" in raw, raw[:200]
    assert b"Connection: close" not in raw, f"over-closed a bodyless reject: {raw[:200]}"
    # The pipelined follow-up IS served, proving keep-alive survived.
    assert b'"status": "ok"' in raw, f"keep-alive was dropped: {raw[:300]}"
    assert raw.count(b"HTTP/1.1") == 2, raw[:300]


def test_unfixed_handler_reproduces_the_poisoning():
    """Negative control: without the arming, the leftover body corrupts the stream."""
    raw = _exchange(_UnfixedHandler, _get_with_body())
    assert b"401" in raw, raw[:200]
    poisoned = (
        b"Bad request syntax" in raw
        or b"400" in raw
        or b"501" in raw
        or b'"status": "ok"' not in raw
    )
    assert poisoned, (
        "expected the unread body to poison the next request on this socket; "
        f"got {raw[:300]!r}"
    )
