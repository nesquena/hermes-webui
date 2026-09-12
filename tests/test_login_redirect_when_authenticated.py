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
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin

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
        # Enforce the production header codec: the stdlib
        # BaseHTTPRequestHandler.send_header() buffers
        # ("%s: %s\r\n" % (keyword, value)).encode("latin-1", "strict"), so a
        # non-Latin-1 Location raises there. A fake that only stores Python
        # strings cannot see that boundary.
        ("%s: %s\r\n" % (name, value)).encode("latin-1", "strict")
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


_PCT_ONLY_TRIPLETS = re.compile(r"^(?:[^%]|%[0-9A-Fa-f]{2})*$")


def _assert_ascii_uri(location):
    """RFC 3986: `%` is legal only as the start of a two-hex-digit triplet."""
    assert location.isascii(), location
    assert _PCT_ONLY_TRIPLETS.match(location), location


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


def test_trusted_header_request_without_next_redirects_to_mount_root(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler) is True
    assert handler.status == 302
    # `./`, not `/`: from `<mount>/login` it resolves to the mount root, so a
    # subpath deployment such as `/hermes/` is not sent to the site root.
    # Mirrors the `_safeNextPath()` default in static/login.js.
    assert _location(handler) == ["./"]


@pytest.mark.parametrize(
    "query",
    [
        "next=%2F%2Fevil.example.com%2F",
        "next=https%3A%2F%2Fevil.example.com%2F",
        "next=%2Flogin%3Fnext%3D%2Flogin",
    ],
)
def test_unsafe_next_falls_back_to_mount_root(monkeypatch, query):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler, query=query) is True
    assert handler.status == 302
    assert _location(handler) == ["./"]


@pytest.mark.parametrize(
    ("page_url", "query", "expected"),
    [
        # No `next`: land on the mount root, wherever the app is mounted.
        ("https://host/login", "", "https://host/"),
        ("https://host/hermes/login", "", "https://host/hermes/"),
        # Unsafe `next`: same fallback.
        ("https://host/login", "next=%2F%2Fevil.example.com%2F", "https://host/"),
        ("https://host/hermes/login", "next=%2F%2Fevil.example.com%2F", "https://host/hermes/"),
        # A present `next` is a browser-side root-absolute path: the app's own
        # producers (static/ui.js, workspace.js, boot.js) build it from
        # window.location.pathname, which already carries the mount prefix, and
        # static/login.js navigates to it verbatim. Emit it as-is so it resolves
        # exactly like the password flow — prefixing `./` would double the
        # mount (`/hermes/hermes/session/...`).
        ("https://host/login", "next=%2Fsession%2Fabc123", "https://host/session/abc123"),
        (
            "https://host/hermes/login",
            "next=%2Fhermes%2Fsession%2Fabc123",
            "https://host/hermes/session/abc123",
        ),
    ],
)
def test_location_resolves_under_root_and_subpath_mounts(
    monkeypatch, page_url, query, expected
):
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler, query=query) is True
    (location,) = _location(handler)
    assert urljoin(page_url, location) == expected


def test_non_latin1_next_is_percent_encoded_for_the_header(monkeypatch):
    """`parse_qs()` decodes `%E4%BD%A0%E5%A5%BD` to `/你好`, and the stdlib
    `send_header()` encodes header values as strict Latin-1, so passing the
    decoded string through raised `UnicodeEncodeError` (a 500 instead of the
    redirect). The `Location` must go out as an ASCII URI."""
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler, query="next=%2F%E4%BD%A0%E5%A5%BD") is True
    assert handler.status == 302
    assert _location(handler) == ["/%E4%BD%A0%E5%A5%BD"]
    _assert_ascii_uri(_location(handler)[0])


def test_latin1_encodable_non_ascii_next_is_still_percent_encoded(monkeypatch):
    """`é` fits Latin-1, so the stdlib does not raise — but a raw non-ASCII
    byte is not a valid header value either. It must be UTF-8 percent-encoded."""
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert _get_login(handler, query="next=%2Fsession%2Fcaf%C3%A9") is True
    assert _location(handler) == ["/session/caf%C3%A9"]
    _assert_ascii_uri(_location(handler)[0])


def test_existing_escapes_and_delimiters_survive_encoding(monkeypatch):
    """Encoding must not double a `%xx` that survived `parse_qs()` (a
    once-encoded `?` the target page owns) or touch URI delimiters."""
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    # parse_qs() decodes this to `/session/abc%3Fx?q=1&r=2`.
    assert (
        _get_login(handler, query="next=%2Fsession%2Fabc%253Fx%3Fq%3D1%26r%3D2")
        is True
    )
    assert _location(handler) == ["/session/abc%3Fx?q=1&r=2"]
    _assert_ascii_uri(_location(handler)[0])


@pytest.mark.parametrize(
    ("query", "decoded", "expected"),
    [
        ("next=%2Fsession%2F100%25", "/session/100%", "/session/100%25"),
        ("next=%2Fa%25Z", "/a%Z", "/a%25Z"),
        ("next=%2Fa%25ZZ", "/a%ZZ", "/a%25ZZ"),
        # Mixed: the valid `%3F` survives once, the lone `%` is escaped.
        ("next=%2Fa%253F%25", "/a%3F%", "/a%3F%25"),
        ("next=%2Fa%25%253F", "/a%%3F", "/a%25%3F"),
    ],
)
def test_malformed_percent_is_escaped_while_valid_triplets_survive(
    monkeypatch, query, decoded, expected
):
    """A `%` that does not start a `%HH` triplet is not a valid URI character.
    `parse_qs()` hands the route a decoded string in which a literal `%` may
    appear; it must go out as `%25` while an existing valid triplet is left
    alone (not doubled to `%253F`)."""
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_AUTH_HEADER", "Remote-User")
    handler = _Handler(headers={"Remote-User": "alice"})

    assert parse_qs(query)["next"][0] == decoded  # pin the route's actual input

    assert _get_login(handler, query=query) is True
    assert handler.status == 302
    assert _location(handler) == [expected]
    _assert_ascii_uri(_location(handler)[0])


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
