"""Forwarded client address with an IPv6 zone ID falls back to the socket peer.

``_login_client_ip`` picks the client address for the login limiter and, since
#8637, for the TTS limiter (``_TtsRateLimiter._get_client_key``) and its 429 log
line. With per-client buckets opted in it honours the forwarded client from a
listed trusted proxy.

``ipaddress.ip_address("fe80::1%<zone>")`` accepts free-form zone text and keeps
it verbatim, control characters included, and ``str()`` echoes it. A zone ID
means nothing off-host, so a scoped forwarded client is treated like any other
unusable forwarded value: the socket peer keys the bucket. The request log
already treats a scoped hop as unresolved (see
tests/test_7864_scoped_ipv6_and_warning_lock.py); this keeps the limiters and
the TTS log line consistent with it.
"""
import http.client
import io
import json
import logging
from types import SimpleNamespace

import pytest

from api import auth, passkeys, routes

_PROXY = "10.0.0.5"
_OPT_IN_ENV = {
    "HERMES_WEBUI_LOGIN_RATE_LIMIT_PER_CLIENT": "1",
    "HERMES_WEBUI_TRUST_FORWARDED_FOR": "1",
    "HERMES_WEBUI_TRUSTED_PROXY_CIDRS": f"{_PROXY}/32",
}

# Zone IDs ipaddress accepts. Control characters sit inside the suffix: the
# resolver strips each hop, so a trailing one would be trimmed before parsing.
_SCOPED_CLIENTS = [
    "fe80::1%eth0",
    "2001:db8::7%1",
    "fe80::1%eth0\r\n\tNEXT",
    "fe80::1%eth0\nNEXT",
    "fe80::1%eth0\rNEXT",
    "fe80::1%\x1b[31mNEXT",
    "fe80::1%eth0\x00NEXT",
]

_CONTROL_CHARS = {chr(c) for c in range(0x20)} | {"\x7f"}


class _Handler:
    def __init__(self, peer, headers, body=b""):
        self.command = "POST"
        self.client_address = (peer, 12345)
        if isinstance(headers, http.client.HTTPMessage):
            self.headers = headers
        else:
            self.headers = http.client.HTTPMessage()
            for key, value in headers.items():
                self.headers[key] = value
        self.headers["Content-Length"] = str(len(body))
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass


def _wire_headers(raw: bytes) -> http.client.HTTPMessage:
    """Parse header bytes the way the HTTP server does (obs-fold kept)."""
    return http.client.parse_headers(io.BytesIO(raw))


@pytest.fixture(autouse=True)
def _per_client_opt_in(monkeypatch):
    for key, value in _OPT_IN_ENV.items():
        monkeypatch.setenv(key, value)


def _has_control_chars(text: str) -> bool:
    return any(ch in _CONTROL_CHARS for ch in text)


# ── the shared helper and the TTS limiter key ─────────────────────────────────


@pytest.mark.parametrize("client", _SCOPED_CLIENTS)
def test_scoped_forwarded_client_keys_on_socket_peer(client):
    handler = _Handler(_PROXY, {"X-Forwarded-For": client})
    assert routes._login_client_ip(handler) == _PROXY


@pytest.mark.parametrize("client", _SCOPED_CLIENTS)
def test_scoped_forwarded_client_with_matching_real_ip_keys_on_socket_peer(client):
    # Agreeing X-Real-IP does not make a zone ID usable.
    handler = _Handler(_PROXY, {"X-Forwarded-For": client, "X-Real-IP": client})
    assert routes._login_client_ip(handler) == _PROXY


@pytest.mark.parametrize("client", _SCOPED_CLIENTS)
def test_tts_limiter_key_ignores_scoped_forwarded_client(client):
    handler = _Handler(_PROXY, {"X-Forwarded-For": client})
    assert routes._TtsRateLimiter()._get_client_key(handler) == _PROXY


def test_folded_header_from_the_wire_keys_on_socket_peer():
    # A header continued onto a second line keeps the CRLF in its value.
    headers = _wire_headers(b"X-Forwarded-For: fe80::1%s1\r\n\tNEXT\r\n\r\n")
    assert headers.get_all("X-Forwarded-For") == ["fe80::1%s1\r\n\tNEXT"]
    handler = _Handler(_PROXY, headers)
    assert routes._login_client_ip(handler) == _PROXY
    assert routes._TtsRateLimiter()._get_client_key(handler) == _PROXY


def test_scoped_forwarded_clients_share_the_peer_bucket():
    # Each zone ID used to be a distinct limiter key for the same address.
    limiter = routes._TtsRateLimiter(window_seconds=60.0)
    assert limiter.check(_Handler(_PROXY, {"X-Forwarded-For": "fe80::1%a"})) is True
    for zone in ("b", "c", "eth0\nNEXT"):
        handler = _Handler(_PROXY, {"X-Forwarded-For": f"fe80::1%{zone}"})
        assert limiter.check(handler) is False


# ── no regression for usable forwarded clients ────────────────────────────────


@pytest.mark.parametrize(
    "forwarded, expected",
    [
        ("198.51.100.7", "198.51.100.7"),
        ("2001:db8::7", "2001:db8::7"),
        ("2001:DB8:0:0::7", "2001:db8::7"),
        ("fe80::1", "fe80::1"),
        ("203.0.113.9, 2001:db8::8", "2001:db8::8"),
    ],
)
def test_plain_forwarded_client_from_listed_proxy_is_honoured(forwarded, expected):
    handler = _Handler(_PROXY, {"X-Forwarded-For": forwarded})
    assert routes._login_client_ip(handler) == expected
    assert routes._TtsRateLimiter()._get_client_key(handler) == expected


def test_plain_forwarded_client_with_matching_real_ip_is_honoured():
    handler = _Handler(
        _PROXY, {"X-Forwarded-For": "2001:db8::7", "X-Real-IP": "2001:db8::7"}
    )
    assert routes._login_client_ip(handler) == "2001:db8::7"


def test_scoped_hop_left_of_the_client_does_not_matter():
    # Only the resolved (right-most untrusted) hop is the client.
    handler = _Handler(_PROXY, {"X-Forwarded-For": "fe80::1%eth0, 198.51.100.7"})
    assert routes._login_client_ip(handler) == "198.51.100.7"


# ── the login limiter key ──────────────────────────────────────────────────────


@pytest.fixture
def _failing_logins(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_LOGIN_ATTEMPTS_FILE", tmp_path / ".login_attempts.json")
    monkeypatch.setattr(auth, "_login_attempts", {})
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "_passkey_feature_flag_enabled", lambda: True)
    monkeypatch.setattr(auth, "verify_password", lambda password: False)

    def reject(body, handler):
        raise passkeys.PasskeyError("assertion rejected")

    monkeypatch.setattr(passkeys, "finish_login", reject)


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/passkey/login"])
def test_login_limiter_keys_scoped_forwarded_client_on_socket_peer(path, _failing_logins):
    zones = [f"z{i}" for i in range(auth._LOGIN_MAX_ATTEMPTS)] + ["eth0\nNEXT"]
    for zone in zones[:-1]:
        handler = _Handler(_PROXY, {"X-Forwarded-For": f"fe80::1%{zone}"})
        routes.handle_post(handler, SimpleNamespace(path=path))
        assert handler.status == 401

    # A fresh zone ID must not buy a fresh bucket.
    handler = _Handler(_PROXY, {"X-Forwarded-For": f"fe80::1%{zones[-1]}"})
    routes.handle_post(handler, SimpleNamespace(path=path))
    assert handler.status == 429
    assert set(auth._login_attempts) == {_PROXY}


# ── the TTS 429 log line ──────────────────────────────────────────────────────


@pytest.fixture
def _tts_route(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    # A 60-second window instead of the route's 2 seconds, so a slow host
    # can't let the second request fall outside the window.
    routes._handle_tts._tts_limiter = routes._TtsRateLimiter(window_seconds=60.0)
    yield
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


def _tts_request(headers):
    # An unknown voice returns 400 at the allowlist after the limiter recorded
    # the client, so no engine is reached.
    body = json.dumps({"text": "hello", "voice": "not-a-real-voice", "engine": "edge"})
    handler = _Handler(_PROXY, headers, body.encode())
    routes._handle_tts(handler, None)
    return handler.status


def _rate_limit_lines(caplog):
    return [
        r.getMessage() for r in caplog.records if "TTS rate limit hit" in r.getMessage()
    ]


@pytest.mark.parametrize("client", [c for c in _SCOPED_CLIENTS if "NEXT" in c])
def test_tts_rate_limit_log_line_has_no_control_characters(client, _tts_route, caplog):
    caplog.set_level(logging.WARNING, logger=routes.logger.name)
    # The same forwarded value twice: the second request is throttled and logged.
    assert _tts_request({"X-Forwarded-For": client}) == 400
    assert _tts_request({"X-Forwarded-For": client}) == 429

    lines = _rate_limit_lines(caplog)
    assert not any(_has_control_chars(line) for line in lines), lines
    assert lines == [f"TTS rate limit hit for client={_PROXY}"]


def test_tts_rate_limit_log_line_for_folded_wire_header(_tts_route, caplog):
    caplog.set_level(logging.WARNING, logger=routes.logger.name)
    raw = b"X-Forwarded-For: fe80::1%s1\r\n\tNEXT\r\n\r\n"
    assert _tts_request(_wire_headers(raw)) == 400
    assert _tts_request(_wire_headers(raw)) == 429

    lines = _rate_limit_lines(caplog)
    assert not any(_has_control_chars(line) for line in lines), lines
    assert lines == [f"TTS rate limit hit for client={_PROXY}"]


def test_tts_rate_limit_log_line_keeps_plain_forwarded_client(_tts_route, caplog):
    caplog.set_level(logging.WARNING, logger=routes.logger.name)
    assert _tts_request({"X-Forwarded-For": "2001:db8::7"}) == 400
    assert _tts_request({"X-Forwarded-For": "2001:db8::7"}) == 429
    assert _rate_limit_lines(caplog) == ["TTS rate limit hit for client=2001:db8::7"]
