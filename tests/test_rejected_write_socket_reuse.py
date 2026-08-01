"""A rejected write must not leave its unread body on a reused connection.

The unit tests beside this one assert `close_connection` is set on each rejection
path. That pins the mechanism but not the consequence: they build a
SimpleNamespace, never open a socket, and would still pass if
BaseHTTPRequestHandler stopped honouring the flag.

This drives the actual failure over one socket: a POST that is rejected before its
body is read, then an ordinary GET on the same connection. Without the fix the
server parses the leftover body as the next request line and answers

    HTTP/1.1 400 Bad request syntax ('{"a": "b"}GET /api/health HTTP/1.1')

which is an error about a request the client never sent - the reason this class of
bug is expensive to diagnose from the client side. With the fix the server closes
after the rejection, so the client sees a clean EOF and reconnects instead.

The two files are complementary and neither replaces the other. The unit tests
call the production handlers, so they pin that `close_connection` is actually set
on each rejection path. This file uses a minimal handler of the same shape, so it
pins WHY that flag matters at the HTTP level - which no amount of asserting on a
boolean can show. Reverting server.py fails the unit test; changing the handler
here to reuse the connection fails this one.
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = b'{"a": "b"}'
REJECTED_WRITE = (
    b"POST /api/session/new HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: " + str(len(BODY)).encode() + b"\r\n"
    b"\r\n" + BODY
)
FOLLOWING_READ = b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n"


class _Rejecting(BaseHTTPRequestHandler):
    """The production shape: reject an unsafe write before reading its body."""

    protocol_version = "HTTP/1.1"
    closes_on_reject = True

    def log_message(self, *args):
        pass  # keep the test output quiet

    def do_POST(self):
        # Deliberately does NOT read the body. This is the production path when
        # auth or CSRF rejects a write before read_body() runs.
        if self.closes_on_reject:
            self.close_connection = True
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def _second_response(*, closes_on_reject: bool) -> bytes:
    """Send a rejected write then a GET down one socket; return the GET's answer."""
    handler = type("_H", (_Rejecting,), {"closes_on_reject": closes_on_reject})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sock = socket.create_connection(server.server_address, timeout=5)
        try:
            sock.sendall(REJECTED_WRITE)
            time.sleep(0.3)
            assert b"403" in sock.recv(4096), "the write should have been rejected"

            sock.sendall(FOLLOWING_READ)
            time.sleep(0.3)
            try:
                return sock.recv(4096)
            except (socket.timeout, ConnectionError):
                return b""
        finally:
            sock.close()
    finally:
        server.shutdown()
        server.server_close()


def test_rejected_write_does_not_corrupt_the_next_request():
    """Closing means the client sees EOF, never a bogus error about its own GET."""
    answer = _second_response(closes_on_reject=True)
    assert b"400" not in answer, f"the leftover body reached the parser: {answer!r}"
    assert BODY not in answer, f"the leftover body reached the parser: {answer!r}"
    # The connection is gone, which is the point: the client reconnects rather
    # than being told its next request was malformed.
    assert answer == b"", f"expected a closed connection, got {answer!r}"


def test_without_the_fix_the_unread_body_corrupts_the_next_request():
    """The bug itself, so the test above is known to be testing something.

    Reusing the connection makes the server read the unread body as a request
    line, and the body text appears in the 400 it sends back.
    """
    answer = _second_response(closes_on_reject=False)
    assert b"400" in answer, f"expected a parse failure, got {answer!r}"
    assert BODY in answer, f"expected the leftover body in the error, got {answer!r}"
