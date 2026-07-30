"""Regression tests for issue #2572 CSRF rejection diagnostics."""

import hmac
import io
import json
import time
from types import SimpleNamespace

import api.auth as auth
import api.routes as routes


class _FakeHandler:
    def __init__(self, headers=None, body=b"{}"):
        self.headers = headers or {}
        self.client_address = ("127.0.0.1", 12345)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass


def _signed_cookie(raw_token: str) -> str:
    sig = hmac.new(auth._signing_key(), raw_token.encode(), "sha256").hexdigest()
    auth._sessions[raw_token] = time.time() + 60
    return f"{raw_token}.{sig}"


def _json_body(handler: _FakeHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def test_origin_mismatch_csrf_rejection_has_diagnostic_error(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    handler = _FakeHandler(
        {
            "Origin": "https://evil.example",
            "Host": "127.0.0.1:8787",
        }
    )

    routes.handle_post(handler, SimpleNamespace(path="/api/providers/delete"))

    assert handler.status == 403
    assert _json_body(handler)["error"] == "Cross-origin mismatch - check reverse proxy headers"


def test_token_mismatch_csrf_rejection_has_reload_error(monkeypatch):
    cookie = _signed_cookie("z" * 64)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    try:
        handler = _FakeHandler(
            {
                "Origin": "http://127.0.0.1:8787",
                "Host": "127.0.0.1:8787",
                "Cookie": f"{auth.COOKIE_NAME}={cookie}",
            }
        )

        routes.handle_post(handler, SimpleNamespace(path="/api/providers/delete"))

        assert handler.status == 403
        assert _json_body(handler)["error"] == "Session expired - reload the page"
    finally:
        auth._sessions.pop("z" * 64, None)
