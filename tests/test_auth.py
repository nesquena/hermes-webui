"""Unit tests for cookie security hardening (Issue #1909, Slice 3).

Covers:
- SameSite=Lax on the auth cookie
- _is_loopback() helper
- _is_secure_context() priority logic:
    1. HERMES_WEBUI_SECURE override
    2. Direct TLS (getpeercert)
    3. HERMES_WEBUI_TRUST_FORWARDED_PROTO opt-in for X-Forwarded-Proto
    4. Otherwise -> not Secure (plain HTTP, regardless of client address)
"""

import http.cookies
import io

from api.auth import _is_loopback, _is_secure_context, set_auth_cookie, COOKIE_NAME


# ── Mock handler helpers ─────────────────────────────────────────────────────


class _MockRequest:
    """Fake socket-like request object."""
    def __init__(self, *, has_peercert: bool = False):
        if has_peercert:
            self.getpeercert = lambda: {'subject': ()}


class _MockHandler:
    """Minimal BaseHTTPRequestHandler stand-in."""

    def __init__(
        self,
        client_address=('127.0.0.1', 12345),
        headers=None,
        request=None,
    ):
        self.client_address = client_address
        self.headers = headers or {}
        self.request = request
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(b'')

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def _set_cookie_header(self):
        for name, val in self.sent_headers:
            if name == 'Set-Cookie':
                return val
        return ''


# ── _is_loopback tests ───────────────────────────────────────────────────────


def test_is_loopback_127_0_0_1():
    assert _is_loopback('127.0.0.1') is True


def test_is_loopback_127_255_255_255():
    assert _is_loopback('127.255.255.255') is True


def test_is_loopback_ipv6_loopback():
    assert _is_loopback('::1') is True


def test_is_loopback_ipv4_mapped_ipv6_loopback():
    assert _is_loopback('::ffff:127.0.0.1') is True


def test_is_loopback_private_not_loopback():
    assert _is_loopback('10.0.0.1') is False


def test_is_loopback_rfc1918_not_loopback():
    assert _is_loopback('192.168.1.1') is False


# ── samesite=Lax tests ──────────────────────────────────────────────────────


def test_samesite_lax_in_cookie(monkeypatch):
    """set_auth_cookie must emit SameSite=Lax."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    handler = _MockHandler()
    set_auth_cookie(handler, 'test-token-value')
    cookie_header = handler._set_cookie_header()
    assert cookie_header, "Set-Cookie header was not sent"
    # Parse via http.cookies to be case/whitespace independent
    c = http.cookies.SimpleCookie()
    c.load(cookie_header)
    assert COOKIE_NAME in c
    assert c[COOKIE_NAME]['samesite'].lower() == 'lax'


# ── _is_secure_context tests ─────────────────────────────────────────────────


def test_secure_not_set_for_loopback(monkeypatch):
    """Loopback client with no TLS and no env vars → not secure."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', raising=False)
    handler = _MockHandler(client_address=('127.0.0.1', 9999))
    assert _is_secure_context(handler) is False


def test_plain_http_non_loopback_not_secure(monkeypatch):
    """Plain HTTP from a LAN IP must NOT set Secure. Regression test for PR #3562."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', raising=False)
    handler = _MockHandler(client_address=('10.0.0.1', 9999))
    assert _is_secure_context(handler) is False


def test_plain_http_rfc1918_class_a_not_secure(monkeypatch):
    """192.168.x.x over plain HTTP must not be secure."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', raising=False)
    handler = _MockHandler(client_address=('192.168.1.50', 9999))
    assert _is_secure_context(handler) is False


def test_plain_http_tailscale_not_secure(monkeypatch):
    """Tailscale CGNAT range (100.64.x.x) over plain HTTP must not be secure."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', raising=False)
    handler = _MockHandler(client_address=('100.64.0.1', 9999))
    assert _is_secure_context(handler) is False


def test_trust_forwarded_proto_opt_in(monkeypatch):
    """With opt-in env var set and X-Forwarded-Proto: https → secure."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    monkeypatch.setenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', '1')
    handler = _MockHandler(
        client_address=('127.0.0.1', 9999),
        headers={'X-Forwarded-Proto': 'https'},
    )
    assert _is_secure_context(handler) is True


def test_forwarded_proto_ignored_without_opt_in(monkeypatch):
    """Without opt-in, X-Forwarded-Proto: https on loopback is ignored → not secure."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', raising=False)
    handler = _MockHandler(
        client_address=('127.0.0.1', 9999),
        headers={'X-Forwarded-Proto': 'https'},
    )
    assert _is_secure_context(handler) is False


def test_hermes_webui_secure_override_on(monkeypatch):
    """HERMES_WEBUI_SECURE=1 forces secure True regardless of other conditions."""
    monkeypatch.setenv('HERMES_WEBUI_SECURE', '1')
    # Loopback, no TLS, no forwarded-proto opt-in; override must win
    handler = _MockHandler(client_address=('127.0.0.1', 9999))
    assert _is_secure_context(handler) is True


def test_hermes_webui_secure_override_off(monkeypatch):
    """HERMES_WEBUI_SECURE=0 forces secure False regardless of other conditions."""
    monkeypatch.setenv('HERMES_WEBUI_SECURE', '0')
    # Explicit override must win even for non-loopback addresses
    handler = _MockHandler(client_address=('10.0.0.1', 9999))
    assert _is_secure_context(handler) is False


def test_direct_tls_socket_is_secure(monkeypatch):
    """Direct TLS socket (getpeercert present) → secure, regardless of address."""
    monkeypatch.delenv('HERMES_WEBUI_SECURE', raising=False)
    monkeypatch.delenv('HERMES_WEBUI_TRUST_FORWARDED_PROTO', raising=False)
    tls_request = _MockRequest(has_peercert=True)
    handler = _MockHandler(
        client_address=('127.0.0.1', 9999),
        request=tls_request,
    )
    assert _is_secure_context(handler) is True
