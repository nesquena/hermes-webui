"""Regression tests for #1909 session-bound CSRF token first slice."""

import hmac
import io
import time
from types import SimpleNamespace

import api.auth as auth
import api.config as api_config
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


def _post_logout(headers):
    handler = _FakeHandler(
        {
            "Content-Length": "2",
            "Content-Type": "application/json",
            "Host": "127.0.0.1:8787",
            **headers,
        }
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/auth/logout", query=""))
    return handler


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


def test_authenticated_null_origin_logout_accepts_valid_session_csrf_token(monkeypatch):
    cookie = _signed_cookie("n" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    try:
        handler = _post_logout(
            {
                "Origin": "null",
                "Cookie": f"{auth.COOKIE_NAME}={cookie}",
                auth.CSRF_HEADER_NAME: token,
            }
        )

        assert handler.status == 200
        assert not auth.verify_session(cookie)
    finally:
        auth._sessions.pop("n" * 64, None)


def test_null_origin_logout_requires_current_session_bound_token(monkeypatch):
    current_cookie = _signed_cookie("q" * 64)
    other_cookie = _signed_cookie("r" * 64)
    expired_cookie = _signed_cookie("s" * 64)
    logged_out_cookie = _signed_cookie("t" * 64)
    expired_token = auth.csrf_token_for_session(expired_cookie)
    logged_out_token = auth.csrf_token_for_session(logged_out_cookie)
    auth._sessions["s" * 64] = time.time() - 1
    auth.invalidate_session(logged_out_cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    try:
        cases = {
            "tokenless": (current_cookie, None),
            "wrong-session token": (
                current_cookie,
                auth.csrf_token_for_session(other_cookie),
            ),
            "expired session": (expired_cookie, expired_token),
            "logged-out session": (logged_out_cookie, logged_out_token),
        }
        for label, (cookie, token) in cases.items():
            headers = {
                "Origin": "null",
                "Cookie": f"{auth.COOKIE_NAME}={cookie}",
            }
            if token:
                headers[auth.CSRF_HEADER_NAME] = token
            handler = _post_logout(headers)

            assert handler.status == 403, label
            assert b"Session expired - reload the page" in handler.wfile.getvalue(), label
    finally:
        for raw_token in ("q", "r", "s", "t"):
            auth._sessions.pop(raw_token * 64, None)


def test_null_origin_logout_bypass_is_literal_and_not_cross_site(monkeypatch):
    cookie = _signed_cookie("u" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    try:
        cases = {
            "cross-origin": {"Origin": "https://evil.example"},
            "uppercase": {"Origin": "NULL"},
            "leading whitespace": {"Origin": " null"},
            "trailing whitespace": {"Origin": "null "},
            "cross-site signal": {
                "Origin": "null",
                "Sec-Fetch-Site": "cross-site",
            },
            "cross-origin referer": {
                "Origin": "null",
                "Referer": "https://evil.example/form",
            },
            "referer only": {"Referer": "null"},
        }
        for label, provenance_headers in cases.items():
            handler = _post_logout(
                {
                    **provenance_headers,
                    "Cookie": f"{auth.COOKIE_NAME}={cookie}",
                    auth.CSRF_HEADER_NAME: token,
                }
            )

            assert handler.status == 403, label
            assert b"Cross-origin mismatch" in handler.wfile.getvalue(), label
    finally:
        auth._sessions.pop("u" * 64, None)


def test_null_origin_logout_remains_rejected_when_auth_is_disabled(monkeypatch):
    cookie = _signed_cookie("v" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    try:
        handler = _post_logout(
            {
                "Origin": "null",
                "Cookie": f"{auth.COOKIE_NAME}={cookie}",
                auth.CSRF_HEADER_NAME: token,
            }
        )

        assert handler.status == 403
        assert b"Cross-origin mismatch" in handler.wfile.getvalue()
    finally:
        auth._sessions.pop("v" * 64, None)


def test_null_origin_cannot_authorize_workspace_escape(monkeypatch):
    cookie = _signed_cookie("w" * 64)
    token = auth.csrf_token_for_session(cookie)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    try:
        handler = _FakeHandler(
            {
                "Origin": "null",
                "Host": "127.0.0.1:8787",
                "Cookie": f"{auth.COOKIE_NAME}={cookie}",
                auth.CSRF_HEADER_NAME: token,
            }
        )
        handler.command = "POST"

        routes._handle_escape_authorize(
            handler,
            SimpleNamespace(path="/api/escape/authorize", query=""),
            body={},
        )

        assert handler.status == 403
        assert b"browser origin required" in handler.wfile.getvalue()
    finally:
        auth._sessions.pop("w" * 64, None)


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
    src = api_config.get_index_html_path().read_text(encoding="utf-8")

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
