"""Regression tests for static-asset compression + cache headers in _serve_static.

Pre-fix shape:
  /static/* served raw bytes with `Cache-Control: no-store` and no
  `Content-Encoding`. A page reload over a slow link re-downloaded the
  full ~2.4 MB shell on every visit, even though every reference in
  static/index.html and static/sw.js carries `?v=__WEBUI_VERSION__`
  fingerprinting that already guarantees a fresh URL on redeploy.

Fix: _serve_static now negotiates gzip when the client opts in, emits
weak ETags for conditional GETs, and sends `max-age=31536000, immutable`
when the request URL carries a `?v=…` fingerprint (`max-age=300`
otherwise). Bytes + headers are cached in-process and invalidated on
(size, mtime) change so a redeploy is picked up without a restart.

These tests pin both halves — header policy AND the cache-invalidation
contract — so future refactors of _serve_static cannot silently
re-introduce no-store or break the gzip/304 path.
"""

import gzip
from types import SimpleNamespace
from urllib.parse import urlparse

import api.config as api_config


class _FakeHandler:
    """Minimal request handler stand-in matching tests/test_session_static_assets.py."""

    def __init__(self, request_headers=None):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.headers = dict(request_headers or {})

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


def _make_static_file(static_root, name, content):
    path = static_root / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def _serve(routes, path, query="", request_headers=None):
    """Invoke _serve_static via the real urllib parse path."""
    parsed = urlparse(f"http://x{path}{('?' + query) if query else ''}")
    h = _FakeHandler(request_headers)
    routes._serve_static(h, parsed)
    return h


import pytest


@pytest.fixture
def isolated_static(tmp_path, monkeypatch):
    """Stand up an isolated static/ tree and rebind _serve_static to use it.

    Yields the static_root Path so tests can drop files into it.
    """
    from api import routes

    static_root = tmp_path / "static"
    static_root.mkdir()

    # Patch the cache so cross-test state cannot leak.
    monkeypatch.setattr(routes, "_STATIC_CACHE", {}, raising=True)
    monkeypatch.setattr(api_config, "get_static_root", lambda: static_root)
    yield static_root


# ── Tests ─────────────────────────────────────────────────────────────────


def test_plain_get_returns_raw_bytes_with_etag(isolated_static):
    from api import routes
    payload = b"console.log('hello');\n" * 200  # > 1 KB so gzip-eligible
    _make_static_file(isolated_static, "ui.js", payload)

    h = _serve(routes, "/static/ui.js")
    assert h.status == 200
    assert h.header("Content-Type") == "application/javascript; charset=utf-8"
    assert h.header("Content-Encoding") is None  # no gzip without Accept-Encoding
    assert h.header("ETag") is not None and h.header("ETag").startswith('W/"')
    assert h.header("Cache-Control") == "public, max-age=300"  # no fingerprint
    assert bytes(h.body) == payload


def test_gzip_negotiated_when_client_accepts(isolated_static):
    from api import routes
    payload = (b"a" * 50_000)
    _make_static_file(isolated_static, "ui.js", payload)

    h = _serve(routes, "/static/ui.js", request_headers={"Accept-Encoding": "gzip, deflate"})
    assert h.status == 200
    assert h.header("Content-Encoding") == "gzip"
    assert h.header("Vary") == "Accept-Encoding"
    assert gzip.decompress(bytes(h.body)) == payload
    assert int(h.header("Content-Length")) == len(h.body) < len(payload)


def test_fingerprinted_url_gets_immutable_cache(isolated_static):
    from api import routes
    _make_static_file(isolated_static, "ui.js", b"x" * 2000)

    h = _serve(routes, "/static/ui.js", query="v=abc1234")
    assert h.header("Cache-Control") == "public, max-age=31536000, immutable"


def test_empty_fingerprint_value_gets_short_cache(isolated_static):
    """Only a non-empty version token is an immutable-cache fingerprint."""
    from api import routes
    _make_static_file(isolated_static, "ui.js", b"x" * 2000)

    h = _serve(routes, "/static/ui.js", query="v=")
    assert h.header("Cache-Control") == "public, max-age=300"


def test_unfingerprinted_url_gets_short_cache(isolated_static):
    from api import routes
    _make_static_file(isolated_static, "ui.js", b"x" * 2000)

    h = _serve(routes, "/static/ui.js")
    assert h.header("Cache-Control") == "public, max-age=300"


def test_conditional_get_returns_304(isolated_static):
    from api import routes
    _make_static_file(isolated_static, "ui.js", b"hello world\n" * 100)

    first = _serve(routes, "/static/ui.js", query="v=abc")
    etag = first.header("ETag")
    assert etag is not None

    second = _serve(routes, "/static/ui.js", query="v=abc",
                    request_headers={"If-None-Match": etag})
    assert second.status == 304
    assert second.header("ETag") == etag
    assert second.header("Cache-Control") == "public, max-age=31536000, immutable"
    assert second.header("Vary") == "Accept-Encoding"
    assert bytes(second.body) == b""


def test_etag_changes_when_file_changes(isolated_static):
    """Cache must invalidate when (size, mtime) changes — guards redeploy correctness."""
    import time
    from api import routes

    f = _make_static_file(isolated_static, "ui.js", b"v1" * 1000)
    first = _serve(routes, "/static/ui.js")
    etag_v1 = first.header("ETag")

    # Touch with a later mtime (1 s granularity matches the ETag formula).
    time.sleep(1.1)
    f.write_bytes(b"v2-different-content" * 50)

    second = _serve(routes, "/static/ui.js")
    etag_v2 = second.header("ETag")
    assert etag_v1 != etag_v2
    # Old ETag now produces a 200, not a stale 304.
    third = _serve(routes, "/static/ui.js", request_headers={"If-None-Match": etag_v1})
    assert third.status == 200


def test_etag_changes_for_same_size_edits_within_same_second(isolated_static):
    """The cache signature must keep sub-second mtime precision."""
    import os
    from api import routes

    f = _make_static_file(isolated_static, "ui.js", b"a" * 2048)
    second = 1_900_000_000
    os.utime(f, ns=(second * 1_000_000_000, second * 1_000_000_000))

    first = _serve(routes, "/static/ui.js")
    etag_v1 = first.header("ETag")

    f.write_bytes(b"b" * 2048)
    os.utime(f, ns=(second * 1_000_000_000 + 123_000_000,
                    second * 1_000_000_000 + 123_000_000))

    second_response = _serve(routes, "/static/ui.js")
    assert second_response.header("ETag") != etag_v1
    assert bytes(second_response.body) == b"b" * 2048


def test_image_is_not_gzipped(isolated_static):
    """Already-compressed binary types must skip gzip to avoid wasted CPU."""
    from api import routes
    # 4 KB of pseudo-PNG (real header doesn't matter, only the MIME does)
    _make_static_file(isolated_static, "favicon.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000)

    h = _serve(routes, "/static/favicon.png", request_headers={"Accept-Encoding": "gzip"})
    assert h.status == 200
    assert h.header("Content-Encoding") is None
    assert h.header("Content-Type") == "image/png"


def test_tiny_file_is_not_gzipped(isolated_static):
    """Files under 1 KB skip gzip — framing overhead exceeds savings."""
    from api import routes
    _make_static_file(isolated_static, "tiny.js", b"export {};\n")

    h = _serve(routes, "/static/tiny.js", request_headers={"Accept-Encoding": "gzip"})
    assert h.status == 200
    assert h.header("Content-Encoding") is None


def test_path_traversal_still_rejected(isolated_static):
    """Sandbox check from the original implementation must remain intact."""
    from api import routes
    _make_static_file(isolated_static, "ui.js", b"ok")
    # Try to break out of static/ — must 404, not serve external files.
    h = _serve(routes, "/static/../api/routes.py")
    assert h.status == 404
