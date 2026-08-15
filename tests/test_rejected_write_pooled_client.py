"""Pooled-client regression for reject-before-read on HTTP/1.1 keep-alive.

The unit tests beside this one assert `close_connection` is set on each
rejection path, and the raw-socket test drives the failure over one socket.
Neither proves the client side: a pooled client (`http.client`) only avoids
the broken connection when the rejection ADVERTISES `Connection: close`.
Without the advertisement the server drops the socket silently and the next
pooled request dies with BrokenPipeError/RemoteDisconnected — or, without the
close at all, the unread body is parsed as the next request line and the
client gets a 400 about a request it never sent.

This file drives the production handler through the real test server and
asserts both halves: the rejection carries `Connection: close`, and a
follow-up request on the same pooled connection succeeds.

The deprecated `/api/process-complete-ack` path is the probe: it answers 410
before reading its JSON body, by design (it runs ahead of the CSRF gate so a
stale tab gets 410, not 403).
"""

import http.client
import socket
import urllib.parse

import pytest

from tests._pytest_port import BASE

_PORT = urllib.parse.urlparse(BASE).port
_BODY = b'{"stale": true}'


def _reject_with_body_then_pooled_followup() -> tuple[int, str | None, int]:
    """POST a deprecated endpoint with a body, then GET on the same pool."""
    conn = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=10)
    try:
        conn.request(
            "POST",
            "/api/process-complete-ack",
            body=_BODY,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        reject_status = resp.status
        close_header = resp.getheader("Connection")
        resp.read()

        conn.request("GET", "/api/health/agent")
        follow = conn.getresponse()
        follow_status = follow.status
        follow.read()
    finally:
        conn.close()
    return reject_status, close_header, follow_status


def test_rejected_write_advertises_close_and_pooled_followup_succeeds():
    status, close_header, follow_status = _reject_with_body_then_pooled_followup()
    assert status == 410, f"expected the deprecated-path 410, got {status}"
    assert close_header == "close", (
        f"rejection must advertise Connection: close so pooled clients don't "
        f"reuse the dead socket; got {close_header!r}"
    )
    assert follow_status == 200, (
        f"pooled follow-up after a reject-before-read must succeed; got {follow_status}"
    )


# ── Framing the Content-Length gate could not see ─────────────────────────────
#
# Both cases below are pipelined down ONE socket against the real server: the
# reject-before-read request first, an ordinary GET immediately after it in the
# same write. If the rejection closes, the server answers once and EOFs and the
# GET is never seen. If it does not, the leftover body/chunk bytes are parsed as
# the next request line and a second response comes back — a 400 bad-syntax or
# `501 Unsupported method` naming bytes the client never sent as a request.

_FOLLOWING_GET = b"GET /api/health/agent HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: */*\r\n\r\n"
_MULTIPART_UPLOAD_PATHS = (
    "/api/upload",
    "/api/upload/extract",
    "/api/workspace/upload",
    "/api/transcribe",
)
_MULTIPART_PAYLOAD = (
    b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n'
    b"\r\nhello\r\n--x--\r\n"
)
_CHUNKED_MULTIPART_BODY = (
    f"{len(_MULTIPART_PAYLOAD):X}\r\n".encode() + _MULTIPART_PAYLOAD + b"\r\n0\r\n\r\n"
)


def _pipelined_after(request: bytes) -> bytes:
    """Send *request* and a plain GET in one write; return every byte answered."""
    sock = socket.create_connection(("127.0.0.1", _PORT), timeout=10)
    try:
        sock.sendall(request + _FOLLOWING_GET)
        received = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except (TimeoutError, socket.timeout, ConnectionError):
                break
            if not chunk:
                break
            received += chunk
        return received
    finally:
        sock.close()


def _assert_single_closed_response(answered: bytes, status: bytes, leftover: bytes) -> None:
    text = answered.decode("latin-1", errors="replace")
    assert answered.startswith(b"HTTP/1.1 " + status), text
    assert b"Connection: close" in answered, text
    assert answered.count(b"HTTP/1.1 ") == 1, (
        f"the pipelined GET was answered, so the socket stayed open: {text}"
    )
    assert leftover not in answered, f"the unread body reached the request parser: {text}"
    assert b"Bad request syntax" not in answered, text
    assert b"Unsupported method" not in answered, text


@pytest.mark.parametrize("path", _MULTIPART_UPLOAD_PATHS)
def test_chunked_upload_rejection_closes_and_cannot_poison_the_socket(path):
    """A chunked upload has no Content-Length — the old gate read it as empty.

    All four multipart handlers then answered 400 "No file field in request"
    with keep-alive intact, leaving the chunk bytes on the socket.
    """
    answered = _pipelined_after(
        f"POST {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Type: multipart/form-data; boundary=x\r\n"
        "Transfer-Encoding: chunked\r\n"
        "\r\n".encode() + _CHUNKED_MULTIPART_BODY
    )

    _assert_single_closed_response(answered, b"411", _MULTIPART_PAYLOAD)


def test_sidecar_get_with_a_body_closes_and_cannot_poison_the_socket():
    """A provenance-rejected GET that carries a declared body.

    `read_request_body=False` said "no body" and the 403 went out with
    keep-alive; the gate's repro then got `501 Unsupported method ('{}GET')` on
    the same socket. No Origin/Referer/Sec-Fetch-Site here, so provenance fails
    before the body would ever be read.
    """
    answered = _pipelined_after(
        "GET /api/extensions/probe/sidecar/ping HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(_BODY)}\r\n"
        "\r\n".encode() + _BODY
    )

    _assert_single_closed_response(answered, b"403", _BODY)


# ── Framing hidden behind a DUPLICATED header ─────────────────────────────────
#
# `Content-Length: 0` sent ahead of the real length: `Message.get()` returns only
# the first value, so every reader saw an empty body and drained nothing. Both
# cases below were reproduced live on the otherwise-fixed head, poisoning the
# socket exactly as the single-header cases used to:
#   sidecar GET -> 403, then 400 Bad request syntax ('{"stale": true}GET /...')
#   /api/upload -> 400, then 400 Bad request syntax ('--x')


def test_duplicate_content_length_sidecar_get_closes_and_cannot_poison_the_socket():
    answered = _pipelined_after(
        "GET /api/extensions/probe/sidecar/ping HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 0\r\n"
        f"Content-Length: {len(_BODY)}\r\n"
        "\r\n".encode() + _BODY
    )

    _assert_single_closed_response(answered, b"403", _BODY)


@pytest.mark.parametrize("path", _MULTIPART_UPLOAD_PATHS)
def test_duplicate_content_length_upload_closes_and_cannot_poison_the_socket(path):
    answered = _pipelined_after(
        f"POST {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Type: multipart/form-data; boundary=x\r\n"
        "Content-Length: 0\r\n"
        f"Content-Length: {len(_MULTIPART_PAYLOAD)}\r\n"
        "\r\n".encode() + _MULTIPART_PAYLOAD
    )

    _assert_single_closed_response(answered, b"400", _MULTIPART_PAYLOAD)
