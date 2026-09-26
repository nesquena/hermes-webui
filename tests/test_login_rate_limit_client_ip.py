"""Login rate limiting keys on the client, not on a trusted reverse proxy.

Behind a reverse proxy every request's socket peer is the proxy. The password
and passkey login limiters must key on the forwarded client IP when
HERMES_WEBUI_TRUST_FORWARDED_FOR=1 and the raw peer is a trusted proxy (the
onboarding gate's trust model), and on the raw socket peer otherwise, so a
direct client cannot pick a fresh bucket by sending forwarded headers.
"""
import http.client
import io
from types import SimpleNamespace

import pytest

from api import auth, passkeys, routes

LOGIN_PATHS = ["/api/auth/login", "/api/auth/passkey/login"]


class _Handler:
    def __init__(self, peer, headers):
        self.client_address = (peer, 12345)
        self.headers = http.client.HTTPMessage()
        self.headers["Content-Length"] = "0"
        for key, value in headers.items():
            self.headers[key] = value
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass


@pytest.fixture(autouse=True)
def _failing_logins(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_LOGIN_ATTEMPTS_FILE", tmp_path / ".login_attempts.json")
    monkeypatch.setattr(auth, "_login_attempts", {})
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: True)
    monkeypatch.setattr(auth, "verify_password", lambda password: False)

    def reject(body, handler):
        raise passkeys.PasskeyError("assertion rejected")

    monkeypatch.setattr(passkeys, "finish_login", reject)
    monkeypatch.delenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", raising=False)


def _login(path, peer, headers):
    handler = _Handler(peer, headers)
    routes.handle_post(handler, SimpleNamespace(path=path))
    return handler.status


@pytest.mark.parametrize("path", LOGIN_PATHS)
def test_failed_logins_behind_trusted_proxy_do_not_lock_out_other_clients(path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    attacker = {"X-Forwarded-For": "203.0.113.10"}
    for _ in range(auth._LOGIN_MAX_ATTEMPTS):
        assert _login(path, "127.0.0.1", attacker) == 401

    assert _login(path, "127.0.0.1", attacker) == 429
    assert _login(path, "127.0.0.1", {"X-Forwarded-For": "203.0.113.20"}) == 401


@pytest.mark.parametrize("path", LOGIN_PATHS)
@pytest.mark.parametrize(
    "trust_forwarded, peer, header",
    [
        # Opt-in off: forwarded headers are ignored even from loopback.
        (False, "127.0.0.1", "X-Forwarded-For"),
        # Opt-in on, but the peer is not a trusted proxy.
        (True, "198.51.100.7", "X-Forwarded-For"),
        # Trusted proxy, but X-Real-IP is not an IP address.
        (True, "127.0.0.1", "X-Real-IP"),
    ],
)
def test_unusable_forwarded_value_keys_on_socket_peer(path, trust_forwarded, peer, header, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1" if trust_forwarded else "")
    # A fresh forwarded value on every attempt must not buy a fresh bucket.
    values = [
        f"client-{i}" if header == "X-Real-IP" else f"203.0.113.{i}"
        for i in range(auth._LOGIN_MAX_ATTEMPTS + 1)
    ]
    for value in values[:-1]:
        assert _login(path, peer, {header: value}) == 401

    assert _login(path, peer, {header: values[-1]}) == 429
