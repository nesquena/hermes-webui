"""Regression test for PR #7598 — /sessions must serve the SPA shell, not 404.

Bug shape (pre-fix):
  When password auth is enabled, the server redirects to /sessions after a successful
  login. However, /sessions (plural) was missing from the explicit path allowlist in
  handle_get(), so the server returned a 404 "not found" instead of serving index.html.

  The bug was invisible without auth because the SPA handles /sessions client-side and
  the server route was never hit — only the server-side redirect after login exposed it.

Fix: handle_get() now includes "/sessions" alongside "/" and "/index.html" in the set
of paths that serve the SPA shell.
"""

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


def test_sessions_route_serves_spa_shell():
    """/sessions (plural) must serve the HTML index, not a 404.

    This is the exact failure mode PR #7598 fixes: after login with auth enabled,
    the server redirects to /sessions, which was missing from the SPA-shell
    allowlist in handle_get().
    """
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/sessions")
    assert handle_get(handler, parsed) is not False
    assert handler.status == 200
    ct = handler.header("Content-Type") or ""
    assert ct.startswith("text/html"), f"expected text/html, got {ct!r}"
    assert b"<!doctype html>" in bytes(handler.body[:200]).lower()


def test_sessions_route_with_query_string_serves_spa_shell():
    """/sessions?foo=bar must still serve the HTML index.

    Ensures query-string variants of /sessions are handled correctly because
    matching uses the parsed path (without query string).
    """
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/sessions?limit=50&offset=0")
    assert handle_get(handler, parsed) is not False
    assert handler.status == 200
    ct = handler.header("Content-Type") or ""
    assert ct.startswith("text/html"), f"expected text/html, got {ct!r}"


def test_sessions_route_does_not_return_404():
    """Regression guard: /sessions must never return 404.

    Before the fix, handle_get() returned False for /sessions, which the
    caller converted into a 404 'not found' JSON response.
    """
    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/sessions")
    result = handle_get(handler, parsed)
    assert result is not False, "/sessions must be handled (not 404)"
    assert handler.status == 200
