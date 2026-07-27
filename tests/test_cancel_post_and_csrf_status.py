"""POST /api/chat/cancel + auth status csrf_token."""

from io import BytesIO
from urllib.parse import urlparse


class _Handler:
    def __init__(self, *, headers=None, body=b"{}"):
        self.client_address = ("127.0.0.1", 12345)
        self.headers = headers or {"Content-Type": "application/json", "Content-Length": str(len(body))}
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status = None
        self.command = "POST"
        self.path = "/api/chat/cancel"
        self._response = None

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass


def test_get_cancel_returns_405(monkeypatch):
    from api import routes

    cap = {}

    def _bad(_h, msg, code=400):
        cap["bad"] = (msg, code)
        return True

    monkeypatch.setattr(routes, "bad", _bad)
    handler = _Handler()
    handler.command = "GET"
    routes.handle_get(handler, urlparse("/api/chat/cancel?stream_id=x"))
    assert cap["bad"][1] == 405
    assert "POST" in cap["bad"][0]


def test_auth_status_includes_csrf_when_logged_in(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("api.auth.is_oidc_auth_enabled", lambda: False)
    monkeypatch.setattr("api.auth.is_trusted_auth_enabled", lambda: False)
    monkeypatch.setattr("api.auth.get_password_hash", lambda: "hash")
    monkeypatch.setattr("api.auth._passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr("api.passkeys.registered_credentials", lambda: [])
    monkeypatch.setattr(
        "api.auth.ensure_trusted_auth_session",
        lambda _h: {"auth_type": "password"},
    )
    monkeypatch.setattr("api.auth.parse_cookie", lambda _h: "tok.sig")
    monkeypatch.setattr("api.auth.csrf_token_for_session", lambda _c: "csrf-abc")
    monkeypatch.setattr("api.auth.verify_session", lambda _c: True)

    payload = {}

    def _j(_h, obj, **_k):
        payload.update(obj)
        return True

    monkeypatch.setattr(routes, "j", _j)
    handler = _Handler()
    handler.command = "GET"
    routes.handle_get(handler, urlparse("/api/auth/status"))
    assert payload.get("csrf_token") == "csrf-abc"
    assert payload.get("authenticated") is True
    assert payload.get("method") == "password"


def test_auth_status_empty_csrf_when_logged_out(monkeypatch):
    from api import routes

    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr("api.auth.is_oidc_auth_enabled", lambda: False)
    monkeypatch.setattr("api.auth.is_trusted_auth_enabled", lambda: False)
    monkeypatch.setattr("api.auth.get_password_hash", lambda: "hash")
    monkeypatch.setattr("api.auth._passkey_feature_flag_enabled", lambda: False)
    monkeypatch.setattr("api.passkeys.registered_credentials", lambda: [])
    monkeypatch.setattr("api.auth.ensure_trusted_auth_session", lambda _h: None)

    payload = {}

    def _j(_h, obj, **_k):
        payload.update(obj)
        return True

    monkeypatch.setattr(routes, "j", _j)
    handler = _Handler()
    handler.command = "GET"
    routes.handle_get(handler, urlparse("/api/auth/status"))
    assert payload.get("csrf_token") == ""
    assert payload.get("authenticated") is False
    assert payload.get("method") == "password"
