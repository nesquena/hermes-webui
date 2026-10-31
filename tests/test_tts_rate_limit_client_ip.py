"""TTS rate limiting: socket peer by default, forwarded client only on opt-in.

Parity with the login limiter (#8610): the TTS per-client throttle keys on the
raw socket peer unless per-client buckets are opted in
(``HERMES_WEBUI_LOGIN_RATE_LIMIT_PER_CLIENT=1``) AND the peer is a listed
trusted proxy (``HERMES_WEBUI_TRUSTED_PROXY_CIDRS``). The old implementation
trusted ANY peer's forwarded headers once ``HERMES_WEBUI_TRUST_FORWARDED_FOR``
was set, so a direct client could pick a fresh bucket (and a log identity) on
every request.
"""
import http.client
from types import SimpleNamespace

import pytest

from api import routes


class _Handler:
    def __init__(self, peer, headers):
        self.client_address = (peer, 12345)
        self.headers = http.client.HTTPMessage()
        for key, value in headers.items() if isinstance(headers, dict) else headers:
            self.headers[key] = value


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin the limiter's clock so the 2 s window never elapses mid-test."""
    now = [1000.0]
    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: now[0]))
    return now


def _limiter():
    return routes._TtsRateLimiter(window_seconds=2.0)


def test_rotating_forwarded_headers_share_one_bucket(monkeypatch, frozen_clock):
    """A direct client must not buy a fresh bucket by rotating forwarded
    headers, even with HERMES_WEBUI_TRUST_FORWARDED_FOR set: the first
    request passes, every follow-up within the window is throttled no matter
    which forwarded value it sends."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    limiter = _limiter()
    assert limiter.check(
        _Handler("203.0.113.5", {"X-Forwarded-For": "198.51.100.0"})
    ) is True
    for i in range(1, 4):
        handler = _Handler("203.0.113.5", {"X-Forwarded-For": f"198.51.100.{i}"})
        assert limiter.check(handler) is False, (
            "rotating forwarded headers bought a fresh bucket: the limiter "
            "trusted an unlisted peer's X-Forwarded-For"
        )


def test_listed_proxy_gets_per_client_buckets(monkeypatch, frozen_clock):
    """With the per-client opt-in and the peer listed in the trusted CIDRs,
    the forwarded client selects the bucket."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("HERMES_WEBUI_LOGIN_RATE_LIMIT_PER_CLIENT", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.0.0.5/32")
    limiter = _limiter()
    a = _Handler("10.0.0.5", {"X-Forwarded-For": "198.51.100.7"})
    b = _Handler("10.0.0.5", {"X-Forwarded-For": "198.51.100.9"})
    assert limiter.check(a) is True
    assert limiter.check(b) is True  # different forwarded client, own bucket
    assert limiter.check(a) is False  # same forwarded client, throttled


def test_unlisted_peer_forwarded_headers_ignored(monkeypatch, frozen_clock):
    """An unlisted peer's forwarded headers never select the bucket, even with
    the opt-in set: the first request passes, every follow-up within the
    window is throttled no matter which forwarded value it sends."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    monkeypatch.setenv("HERMES_WEBUI_LOGIN_RATE_LIMIT_PER_CLIENT", "1")
    monkeypatch.setenv("HERMES_WEBUI_TRUSTED_PROXY_CIDRS", "10.0.0.5/32")
    limiter = _limiter()
    assert limiter.check(
        _Handler("198.51.100.99", {"X-Forwarded-For": "203.0.113.0"})
    ) is True
    for i in range(1, 4):
        handler = _Handler("198.51.100.99", {"X-Forwarded-For": f"203.0.113.{i}"})
        assert limiter.check(handler) is False


def test_session_cookie_overrides_client_key(frozen_clock):
    """The session-cookie override must keep working: an authenticated client
    shares one bucket across IPs, and different cookies get separate buckets."""
    limiter = _limiter()
    h1 = _Handler("203.0.113.5", {})
    h2 = _Handler("203.0.113.6", {})
    assert limiter.check(h1, "user-abc.xyz") is True
    assert limiter.check(h2, "user-abc.xyz") is False  # same cookie, same bucket
    h3 = _Handler("203.0.113.7", {})
    assert limiter.check(h3, "user-def.xyz") is True  # other cookie, own bucket
