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
import urllib.parse

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
