"""Regression tests for #1909 session-bound CSRF token first slice."""

import hmac
import io
import time
from types import SimpleNamespace

import api.auth as auth
import api.routes as routes


def _signed_cookie(raw_token: str) -> str:
    sig = hmac.new(auth._signing_key(), raw_token.encode(), "sha256").hexdigest()
    auth._sessions[raw_token] = time.time() + 60
    return f"{raw_token}.{sig}"


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


def test_csrf_token_is_bound_to_auth_session():
    cookie_a = _signed_cookie("a" * 64)
    cookie_b = _signed_cookie("b" * 64)
    try:
        token_a = auth.csrf_token_for_session(cookie_a)
        token_b = auth.csrf_token_for_session(cookie_b)

        assert token_a and token_b and token_a != token_b
        assert auth.verify_csrf_token(cookie_a, token_a)
        assert not auth.verify_csrf_token(cookie_b, token_a)
        assert not auth.verify_csrf_token(cookie_a, "not-the-token")
    finally:
        auth._sessions.pop("a" * 64, None)
        auth._sessions.pop("b" * 64, None)


def test_authenticated_same_origin_browser_post_requires_session_csrf_token(monkeypatch):
    cookie = _signed_cookie("c" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    try:
        base_headers = {
            "Origin": "http://127.0.0.1:8787",
            "Host": "127.0.0.1:8787",
            "Cookie": f"{auth.COOKIE_NAME}={cookie}",
        }
        assert not routes._check_csrf(_FakeHandler(base_headers.copy()))

        headers_with_token = {**base_headers, auth.CSRF_HEADER_NAME: token}
        assert routes._check_csrf(_FakeHandler(headers_with_token))
    finally:
        auth._sessions.pop("c" * 64, None)


def test_authenticated_allowed_public_origin_accepts_valid_csrf_token(monkeypatch):
    cookie = _signed_cookie("f" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://myapp.example.com:8000")
    try:
        headers = {
            "Origin": "https://myapp.example.com:8000",
            "Host": "proxy.internal",
            "Cookie": f"{auth.COOKIE_NAME}={cookie}",
            auth.CSRF_HEADER_NAME: token,
        }
        assert routes._check_csrf(_FakeHandler(headers))
    finally:
        auth._sessions.pop("f" * 64, None)


def test_authenticated_reverse_proxy_same_origin_accepts_valid_csrf_token(monkeypatch):
    cookie = _signed_cookie("g" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_HOST", "1")
    try:
        headers = {
            "Origin": "https://example.com",
            "Host": "127.0.0.1:8787",
            "X-Forwarded-Host": "example.com:443",
            "Cookie": f"{auth.COOKIE_NAME}={cookie}",
            auth.CSRF_HEADER_NAME: token,
        }
        assert routes._check_csrf(_FakeHandler(headers))
    finally:
        auth._sessions.pop("g" * 64, None)


def test_authenticated_forwarded_host_is_ignored_without_proxy_opt_in(monkeypatch):
    cookie = _signed_cookie("h" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_HOST", raising=False)
    try:
        headers = {
            "Origin": "https://example.com",
            "Host": "127.0.0.1:8787",
            "X-Forwarded-Host": "example.com:443",
            "Cookie": f"{auth.COOKIE_NAME}={cookie}",
            auth.CSRF_HEADER_NAME: token,
        }
        assert not routes._check_csrf(_FakeHandler(headers))
    finally:
        auth._sessions.pop("h" * 64, None)


def test_non_browser_mcp_style_authenticated_post_remains_compatible(monkeypatch):
    cookie = _signed_cookie("d" * 64)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    try:
        handler = _FakeHandler({"Cookie": f"{auth.COOKIE_NAME}={cookie}"})
        assert routes._check_csrf(handler)
    finally:
        auth._sessions.pop("d" * 64, None)


def test_login_route_remains_csrf_exempt(monkeypatch):
    handler = _FakeHandler(
        {
            "Content-Length": "2",
            "Content-Type": "application/json",
            "Origin": "http://evil.example",
            "Host": "127.0.0.1:8787",
        }
    )

    def fail_if_called(_handler):
        raise AssertionError("/api/auth/login must not require a pre-login CSRF token")

    monkeypatch.setattr(routes, "_check_csrf", fail_if_called)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)

    routes.handle_post(handler, SimpleNamespace(path="/api/auth/login"))
    assert handler.status == 200


def test_index_shell_includes_csrf_fetch_and_sendbeacon_injection():
    src = (routes._INDEX_HTML_PATH).read_text(encoding="utf-8")

    assert "csrfToken:__CSRF_TOKEN_JSON__" in src
    assert "X-Hermes-CSRF-Token" in src
    assert "window.fetch=function" in src
    assert "navigator.sendBeacon=function" in src
    assert "auth\\/login|csp-report" in src


def test_index_shell_injects_session_bound_csrf_token(monkeypatch):
    cookie = _signed_cookie("e" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)

    captured = {}

    def fake_t(_handler, body, *, content_type=None, **_kwargs):
        captured["body"] = body
        captured["content_type"] = content_type
        return True

    import api.extensions as extensions

    monkeypatch.setattr(routes, "t", fake_t)
    monkeypatch.setattr(extensions, "inject_extension_tags", lambda html: html)

    try:
        handler = _FakeHandler({"Cookie": f"{auth.COOKIE_NAME}={cookie}"})
        assert routes.handle_get(handler, SimpleNamespace(path="/", query="")) is True
        assert captured["content_type"] == "text/html; charset=utf-8"
        assert f"csrfToken:{token!r}".replace("'", '"') in captured["body"]
    finally:
        auth._sessions.pop("e" * 64, None)
