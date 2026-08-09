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


def test_post_cancel_requires_csrf_when_auth_enabled(monkeypatch):
    """Browser POST /api/chat/cancel without CSRF must 403 and never cancel."""
    import hmac
    import time

    import api.auth as auth
    from api import routes

    raw = "e" * 64
    sig = hmac.new(auth._signing_key(), raw.encode(), "sha256").hexdigest()
    cookie = f"{raw}.{sig}"
    auth._sessions[raw] = time.time() + 60
    token = auth.csrf_token_for_session(cookie)

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    cancelled = {"n": 0}
    monkeypatch.setattr(
        routes, "cancel_stream", lambda _sid: cancelled.__setitem__("n", cancelled["n"] + 1) or True
    )
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_a, **_k: True)

    responses = []

    def _j(_h, obj, status=200, **_k):
        responses.append((status, obj))
        return True

    monkeypatch.setattr(routes, "j", _j)

    try:
        body = b'{"stream_id":"stream-x"}'
        base = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": "http://127.0.0.1:8787",
            "Host": "127.0.0.1:8787",
            "Cookie": f"{auth.COOKIE_NAME}={cookie}",
        }

        missing = _Handler(headers=base, body=body)
        routes.handle_post(missing, urlparse("/api/chat/cancel"))
        assert responses[-1][0] == 403
        assert cancelled["n"] == 0

        ok_body = b'{"stream_id":"stream-x"}'
        ok_headers = {**base, auth.CSRF_HEADER_NAME: token, "Content-Length": str(len(ok_body))}
        ok = _Handler(headers=ok_headers, body=ok_body)
        routes.handle_post(ok, urlparse("/api/chat/cancel"))
        assert cancelled["n"] == 1
        assert responses[-1][0] == 200
        assert responses[-1][1].get("cancelled") is True
    finally:
        auth._sessions.pop(raw, None)
