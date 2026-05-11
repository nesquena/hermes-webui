"""Regression coverage for the shell/home route fallback.

The WebUI shell should never render a JSON error page for `/`, even if
index.html serving fails during a restart/update race. API routes still keep
their normal JSON error behavior; this only pins the shell route contract.
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


class _BrokenIndexPath:
    def read_text(self, *args, **kwargs):
        raise RuntimeError("simulated index.html read failure")


def test_home_route_internal_error_returns_html_503_not_json(monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "_INDEX_HTML_PATH", _BrokenIndexPath())

    handler = _FakeHandler()
    assert routes.handle_get(handler, urlparse("http://example.com/")) is True

    assert handler.status == 503
    assert (handler.header("Content-Type") or "").startswith("text/html; charset=utf-8")
    assert handler.header("Cache-Control") == "no-store"

    body = bytes(handler.body).decode("utf-8")
    assert "Hermes is restarting" in body
    assert "application/json" not in (handler.header("Content-Type") or "")
    assert '"error"' not in body
