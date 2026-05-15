"""Regression tests for PR #1505 — /session/static/* must serve static assets, not the HTML index.

Bug shape (pre-fix):
  Browsers visiting /session/<id> resolved relative `<link rel="stylesheet" href="static/style.css">`
  references against `/session/`, producing requests like /session/static/style.css. The
  catch-all `parsed.path.startswith("/session/")` matched FIRST and returned the HTML index
  with Content-Type: text/html, which strict-MIME browsers refused to apply as a stylesheet.

Fix: handle_get() now intercepts /session/static/* BEFORE the catch-all and delegates to
_serve_static() with the /session prefix stripped. check_auth() also exempts /session/static/*
from auth (same policy as /static/*).

These tests pin both the routing fix AND the auth exemption so a future refactor of either
path can't silently re-introduce the MIME-type bug.
"""

from types import SimpleNamespace
from urllib.parse import urlparse


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

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


def test_session_static_css_returns_text_css_mime(monkeypatch):
    """/session/<id>/static/style.css must return Content-Type: text/css, not text/html.

    This is the exact failure mode PR #1505 fixes: strict-MIME browsers refuse to apply
    a stylesheet served as text/html.
    """
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/session/static/style.css")
    assert handle_get(handler, parsed) is True
    assert handler.status == 200
    ct = handler.header("Content-Type") or ""
    assert ct.startswith("text/css"), f"expected text/css, got {ct!r}"
    # Sanity: real CSS bytes, not the 100KB HTML index page
    assert b"<!doctype html>" not in handler.body[:200].lower()


def test_session_static_js_returns_javascript_mime(monkeypatch):
    """/session/<id>/static/ui.js must return application/javascript, not text/html."""
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/session/static/ui.js")
    assert handle_get(handler, parsed) is True
    assert handler.status == 200
    ct = handler.header("Content-Type") or ""
    assert ct.startswith("application/javascript"), f"expected application/javascript, got {ct!r}"


def test_session_html_route_still_serves_index():
    """Sibling regression: /session/<id> (no /static/) must still return the HTML index.

    The new /session/static/ guard is positioned before the catch-all; this test ensures
    the catch-all itself wasn't accidentally reordered or weakened.
    """
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/session/abc123def456")
    handle_get(handler, parsed)
    assert handler.status == 200
    ct = handler.header("Content-Type") or ""
    assert ct.startswith("text/html"), f"expected text/html, got {ct!r}"
    # And the body really is the HTML index, not a 404 stub
    assert b"<!doctype html>" in bytes(handler.body[:200]).lower()


def test_session_static_path_traversal_blocked():
    """Path-traversal sandbox in _serve_static must still apply after the prefix strip.

    /session/static/../../etc/passwd → strips to /static/../../etc/passwd → _serve_static
    resolves and rejects via relative_to(static_root) → 404.
    """
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/session/static/../../etc/passwd")
    handle_get(handler, parsed)
    assert handler.status == 404


def test_session_static_auth_exemption(monkeypatch):
    """/session/static/* must be auth-exempt (same policy as /static/*).

    Without this exemption, anonymous browser navigation to /session/<id> would
    302-redirect every stylesheet/script to /login, breaking the page even when
    the HTML index itself loaded correctly.
    """
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")

    from api.auth import check_auth, _invalidate_password_hash_cache

    _invalidate_password_hash_cache()

    # /session/static/* is public (matches /static/* policy)
    handler = _FakeHandler()
    assert check_auth(handler, SimpleNamespace(path="/session/static/style.css", query="")) is True

    # Confirm the /static/ baseline still works (regression guard)
    handler = _FakeHandler()
    assert check_auth(handler, SimpleNamespace(path="/static/style.css", query="")) is True

    # And confirm a non-static /session/* path still requires auth
    handler = _FakeHandler()
    assert check_auth(handler, SimpleNamespace(path="/session/abc123", query="")) is False


def test_session_static_favicon_512_returns_png():
    """/session/static/favicon-512.png must return image/png with a PNG signature.

    Firefox Android fetches PWA icons from the manifest's icon URLs. When the
    page is /session/<id>, the manifest's relative icon paths resolve to
    /session/static/favicon-512.png. This test ensures the existing
    /session/static/* alias serves the real PNG icon, not the HTML index.
    See #2226.
    """
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/session/static/favicon-512.png")
    assert handle_get(handler, parsed) is True
    assert handler.status == 200
    ct = handler.header("Content-Type") or ""
    assert ct.startswith("image/png"), f"expected image/png, got {ct!r}"
    # PNG signature: first 8 bytes are \x89PNG\r\n\x1a\n
    body = bytes(handler.body)
    assert body[:4] == b"\x89PNG", "favicon-512.png must start with PNG signature"
