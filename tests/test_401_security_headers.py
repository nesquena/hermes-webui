from types import SimpleNamespace
from urllib.parse import urlparse

import api.auth as auth


class _Handler:
    def __init__(self):
        self.headers = {}
        self.sent = []
        self.body = b""

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent.append((key, value))

    def end_headers(self):
        pass

    @property
    def wfile(self):
        outer = self

        class _WFile:
            def write(self, body):
                outer.body += body

        return _WFile()


def _headers(handler):
    return {k.lower(): v for k, v in handler.sent}


def test_api_unauthorized_response_includes_security_headers(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: None)
    handler = _Handler()

    ok = auth.check_auth(handler, urlparse("/api/private"))

    headers = _headers(handler)
    assert ok is False
    assert handler.status == 401
    assert headers["cache-control"] == "no-store"
    assert "content-security-policy" in headers
    assert headers["x-content-type-options"] == "nosniff"
