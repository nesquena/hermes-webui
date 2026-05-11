from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from api import auth, routes

REPO = Path(__file__).resolve().parents[1]


class _Headers(dict):
    def get(self, key, default=None):
        for existing, value in self.items():
            if existing.lower() == key.lower():
                return value
        return default


class _Handler(SimpleNamespace):
    pass


def _handler(*, cookie: str | None = None, csrf_token: str | None = None, origin: str | None = None):
    headers = _Headers({"Host": "localhost:8787"})
    if cookie:
        headers["Cookie"] = f"{auth.COOKIE_NAME}={cookie}"
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    if origin:
        headers["Origin"] = origin
    return _Handler(headers=headers)


def test_session_csrf_token_round_trips_for_valid_session(monkeypatch):
    monkeypatch.setattr(auth, "get_password_hash", lambda: "enabled")
    cookie = auth.create_session()

    token = auth.csrf_token_for_session(cookie)

    assert token
    assert auth.verify_csrf_token(cookie, token) is True
    assert auth.verify_csrf_token(cookie, "wrong-token") is False


def test_authenticated_unsafe_request_without_origin_requires_csrf_token(monkeypatch):
    monkeypatch.setattr(auth, "get_password_hash", lambda: "enabled")
    cookie = auth.create_session()

    assert routes._check_csrf(_handler(cookie=cookie), path="/api/settings") is False


def test_authenticated_unsafe_request_accepts_valid_csrf_token_without_origin(monkeypatch):
    monkeypatch.setattr(auth, "get_password_hash", lambda: "enabled")
    cookie = auth.create_session()
    token = auth.csrf_token_for_session(cookie)

    assert routes._check_csrf(_handler(cookie=cookie, csrf_token=token), path="/api/settings") is True


def test_login_endpoint_remains_exempt_from_csrf_token(monkeypatch):
    monkeypatch.setattr(auth, "get_password_hash", lambda: "enabled")

    assert routes._check_csrf(_handler(), path="/api/auth/login") is True


def test_cross_origin_request_is_rejected_even_with_valid_csrf_token(monkeypatch):
    monkeypatch.setattr(auth, "get_password_hash", lambda: "enabled")
    cookie = auth.create_session()
    token = auth.csrf_token_for_session(cookie)

    assert routes._check_csrf(
        _handler(cookie=cookie, csrf_token=token, origin="https://evil.example"),
        path="/api/settings",
    ) is False


def test_auth_status_exposes_csrf_token_only_when_logged_in():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    auth_status_block = src[src.find('if parsed.path == "/api/auth/status"') : src.find('if parsed.path in ("/manifest.json"')]

    assert "csrf_token_for_session" in auth_status_block
    assert 'payload["csrf_token"]' in auth_status_block


def test_fetch_patch_adds_csrf_header_to_same_origin_unsafe_requests():
    src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

    assert "function _csrfFetchArgs" in src
    assert "X-CSRF-Token" in src
    assert "_csrfFetchArgs(...args)" in src
