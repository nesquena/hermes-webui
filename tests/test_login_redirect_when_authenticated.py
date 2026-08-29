"""`GET /login` must not park an already-authenticated client on the form.

`/login` is in `PUBLIC_PATHS`, so `check_auth()` returns early and never
reconciles the request's identity — the route used to render the password form
unconditionally. With trusted-header (reverse-proxy SSO) auth that is a dead
end: iOS relaunches an installed Home Screen web app at its last URL, so a
device that once landed on `/login` sits on a form it cannot use forever while
every request it makes is already authenticated. A cookie-authenticated user
who navigates to `/login` hit the same dead end.

The route now redirects an authenticated request to its validated `next`
destination, and still renders the form for everyone else.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

import api.auth as auth
import api.profiles as profiles
import api.routes as routes


class _Handler:
    def __init__(self, *, headers=None, client_address=("127.0.0.1", 12345)):
        self.headers = dict(headers or {})
        self.client_address = client_address
        self.command = "GET"
        self.path = "/login"
        self.request = SimpleNamespace()
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def body_text(self):
        return self.wfile.getvalue().decode("utf-8")

    def header_values(self, name):
        return [value for key, value in self.sent_headers if key == name]


@pytest.fixture(autouse=True)
def isolated_auth_state(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.setattr(auth, "_SESSIONS_FILE", tmp_path / ".sessions.json")
    monkeypatch.setattr(auth, "is_password_auth_enabled", lambda: False)
    monkeypatch.setattr(auth, "are_passkeys_enabled", lambda: False)
    monkeypatch.setattr(auth, "is_oidc_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"bot_name": "Hermes"})
    for key in (
        "HERMES_WEBUI_TRUSTED_AUTH_HEADER",
        "HERMES_WEBUI_TRUSTED_GROUPS_HEADER",
        "HERMES_WEBUI_TRUSTED_PROXY_CIDRS",
    ):
        monkeypatch.delenv(key, raising=False)
    auth._sessions.clear()
    auth._TRUSTED_AUTH_WARNINGS_EMITTED.clear()
    profiles.clear_request_profile()
    yield
    auth._sessions.clear()
    auth._TRUSTED_AUTH_WARNINGS_EMITTED.clear()
    profiles.clear_request_profile()


def _get_login(handler, query=""):
    return routes.handle_get(handler, SimpleNamespace(path="/login", query=query))


def _location(handler):
    return handler.header_values("Location")


def test_trusted_header_request_redirects_to_safe_next(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler, query="next=%2Fsession%2Fabc123") is True
    assert handler.status == 302
    assert _location(handler) == ["/session/abc123"]
    assert handler.header_values("Cache-Control") == ["no-store"]
    assert handler.body_text() == ""
    # The session minted while reconciling the trusted header must ride along,
    # or the redirect target bounces the client straight back to /login.
    assert any(
        cookie.startswith("hermes_session=")
        for cookie in handler.header_values("Set-Cookie")
    )


def test_trusted_header_request_without_next_redirects_to_root(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler) is True
    assert handler.status == 302
    assert _location(handler) == ["/"]


@pytest.mark.parametrize(
    "query",
    [
        "next=%2F%2Fevil.example.com%2F",
        "next=https%3A%2F%2Fevil.example.com%2F",
        "next=%2Flogin%3Fnext%3D%2Flogin",
    ],
)
def test_unsafe_next_falls_back_to_root(monkeypatch, query):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler, query=query) is True
    assert handler.status == 302
    assert _location(handler) == ["/"]


def test_cookie_authenticated_request_redirects(monkeypatch):
    monkeypatch.setattr(auth, "is_password_auth_enabled", lambda: True)
    cookie = auth.create_session()
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})

    assert _get_login(handler, query="next=%2Fsession%2Fabc123") is True
    assert handler.status == 302
    assert _location(handler) == ["/session/abc123"]


def test_unauthenticated_request_still_renders_the_form(monkeypatch):
    monkeypatch.setattr(auth, "is_password_auth_enabled", lambda: True)
    handler = _Handler()

    _get_login(handler, query="next=%2Fsession%2Fabc123")

    assert handler.status == 200
    assert _location(handler) == []
    assert "<form" in handler.body_text()


def test_untrusted_peer_header_still_renders_the_form(monkeypatch):
    """A spoofed trusted header from a non-proxy peer must not redirect."""
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(
        headers={"Remote-User": "alice"},
        client_address=("10.0.0.5", 12345),
    )

    _get_login(handler, query="next=%2Fsession%2Fabc123")

    assert handler.status == 200
    assert _location(handler) == []
    assert "<form" in handler.body_text()


def test_expired_cookie_still_renders_the_form(monkeypatch):
    monkeypatch.setattr(auth, "is_password_auth_enabled", lambda: True)
    cookie = auth.create_session()
    auth.invalidate_session(cookie)
    handler = _Handler(headers={"Cookie": f"hermes_session={cookie}"})

    _get_login(handler)

    assert handler.status == 200
    assert _location(handler) == []
    assert "<form" in handler.body_text()


def test_auth_disabled_still_renders_the_form():
    handler = _Handler()

    _get_login(handler)

    assert auth.is_auth_enabled() is False
    assert handler.status == 200
    assert _location(handler) == []
    assert "<form" in handler.body_text()
