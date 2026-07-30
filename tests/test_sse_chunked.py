"""Tests for opt-in chunked transfer-encoding on SSE responses.

See api/sse_chunked.py: behind a buffering reverse proxy (jupyter-server-proxy)
unframed SSE is buffered until-close, so HERMES_WEBUI_SSE_CHUNKED opts into
explicit HTTP/1.1 chunk framing. Default off must preserve the historical
unframed wire format byte-for-byte.
"""

import io

from api.sse_chunked import chunked_sse_enabled, end_sse_headers


class _Handler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers_sent = []
        self.ended = False

    def send_header(self, name, value):
        self.headers_sent.append((name, value))

    def end_headers(self):
        self.ended = True


def _dechunk(raw: bytes) -> bytes:
    """Decode an HTTP/1.1 chunked body (no trailing zero-length terminator)."""
    out = bytearray()
    i = 0
    while i < len(raw):
        nl = raw.index(b"\r\n", i)
        size = int(raw[i:nl], 16)
        start = nl + 2
        out += raw[start : start + size]
        i = start + size + 2  # skip chunk data + trailing CRLF
    return bytes(out)


def test_default_off_is_plain_end_headers(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_SSE_CHUNKED", raising=False)
    assert chunked_sse_enabled() is False

    h = _Handler()
    end_sse_headers(h)
    h.wfile.write(b"id: 1\n")
    h.wfile.write(b"event: token\ndata: {}\n\n")

    assert h.ended is True
    # No Transfer-Encoding header and the body is written verbatim (unframed).
    assert all(name != "Transfer-Encoding" for name, _ in h.headers_sent)
    assert h.wfile.getvalue() == b"id: 1\nevent: token\ndata: {}\n\n"


def test_flag_on_emits_chunked_frames_that_decode_back(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_SSE_CHUNKED", "1")
    assert chunked_sse_enabled() is True

    h = _Handler()
    end_sse_headers(h)
    h.wfile.write(b"id: 1\n")
    h.wfile.write(b"event: token\ndata: {}\n\n")

    assert ("Transfer-Encoding", "chunked") in h.headers_sent
    raw = h.wfile._raw.getvalue()
    # Each write became its own chunk (size prefix present), and the chunked
    # body decodes back to exactly what was written — what any HTTP client sees.
    assert raw.startswith(b"6\r\nid: 1\n\r\n")
    assert _dechunk(raw) == b"id: 1\nevent: token\ndata: {}\n\n"


def test_truthy_values(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "On"):
        monkeypatch.setenv("HERMES_WEBUI_SSE_CHUNKED", val)
        assert chunked_sse_enabled() is True
    for val in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("HERMES_WEBUI_SSE_CHUNKED", val)
        assert chunked_sse_enabled() is False
