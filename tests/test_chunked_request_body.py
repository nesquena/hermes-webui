"""read_body() decodes Transfer-Encoding: chunked request bodies.

Reverse proxies that stream an HTTP/2 client request to an HTTP/1.1 origin —
notably Cloudflare Tunnel (cloudflared) — forward request bodies with
``Transfer-Encoding: chunked`` and no ``Content-Length``. Before the fix,
``read_body()`` only honoured ``Content-Length``, so every proxied POST
arrived with an empty body. For JSON login that meant the parsed password was
``""`` and the server returned 401 "Invalid password" no matter what the user
typed; other POSTs (settings, message send) silently lost their payload too.

These tests assert the observable behaviour of ``read_body`` — the decoded
dict — not a source string. The chunked cases fail against the pre-fix code
(they get ``{}`` back) and pass after it.
"""
import email.message
import io
import json

from api.helpers import read_body


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler's body-reading surface.

    Uses a real ``email.message.Message`` so header lookups are
    case-insensitive, exactly as ``http.server`` delivers them (cloudflared
    sends the header lowercased as ``transfer-encoding``).
    """

    def __init__(self, raw: bytes, headers: dict):
        self.rfile = io.BytesIO(raw)
        msg = email.message.Message()
        for key, value in headers.items():
            msg[key] = value
        self.headers = msg
        self.close_connection = False


def _one_chunk(body: bytes) -> bytes:
    """Frame *body* as a single HTTP chunk followed by the terminator."""
    return b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)


def test_chunked_body_is_decoded_lowercase_header():
    payload = {"password": "correct horse battery staple", "n": 42}
    body = json.dumps(payload).encode()
    # cloudflared casing: lowercase header name, and NO Content-Length.
    handler = _FakeHandler(
        _one_chunk(body),
        {"transfer-encoding": "chunked", "content-type": "application/json"},
    )
    assert read_body(handler) == payload


def test_chunked_body_reassembled_across_multiple_chunks():
    # A body split across several chunks must be reassembled in order; a
    # single-chunk test could not catch a reader that drops all but one chunk.
    payload = {"blob": "x" * 5000, "tail": "end"}
    body = json.dumps(payload).encode()
    p1, p2, p3 = body[:100], body[100:3000], body[3000:]
    raw = (
        b"%x\r\n%s\r\n" % (len(p1), p1)
        + b"%x\r\n%s\r\n" % (len(p2), p2)
        + b"%x\r\n%s\r\n" % (len(p3), p3)
        + b"0\r\n\r\n"
    )
    handler = _FakeHandler(raw, {"Transfer-Encoding": "chunked"})
    assert read_body(handler) == payload


def test_chunked_with_extension_on_size_line():
    # Chunk-size lines may carry ';'-delimited extensions; they must be ignored.
    payload = {"ok": True}
    body = json.dumps(payload).encode()
    raw = b"%x;ext=1\r\n%s\r\n0\r\n\r\n" % (len(body), body)
    handler = _FakeHandler(raw, {"Transfer-Encoding": "chunked"})
    assert read_body(handler) == payload


def test_content_length_body_still_works():
    # The existing Content-Length path must be unchanged.
    payload = {"password": "correct horse battery staple"}
    body = json.dumps(payload).encode()
    handler = _FakeHandler(
        body,
        {"Content-Length": str(len(body)), "Content-Type": "application/json"},
    )
    assert read_body(handler) == payload


def test_empty_body_returns_empty_dict():
    handler = _FakeHandler(b"", {})
    assert read_body(handler) == {}
