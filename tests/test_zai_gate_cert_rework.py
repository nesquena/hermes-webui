"""Regressions for the gate-cert review round on nesquena/hermes-webui#7203.

Covers the eight reproduced blockers:
1.  Credential trust boundary: the monitor request must go to the CONFIGURED
    provider origin (base_url), never the hard-coded api.z.ai host, and the
    credential must not be sent at all when the origin cannot be verified.
2.  Redirects must be rejected before the Authorization header can reach a
    different origin.
3.  The response body read must be bounded by a strict cap.
4.  A failed single-flight must be shared terminal state: waiters must not
    each re-fetch (no N-request burst after one owner failure).
5.  A failed forced refresh must evict the stale cached success: the next
    ordinary request returns unavailable, not the old "available".
6.  The pre-existing Z.AI local credential-pool snapshot must survive: fall
    back to it on failure paths and merge its breakdown on remote success.

Run via ./scripts/test.sh per AGENTS.md.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import api.providers as providers
import api.config as config


def _payload(pct):
    return {"success": True, "code": 200, "data": {"level": "lite", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": pct,
         "nextResetTime": 1787281782649}]}}


_LITE = _payload(9)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.read_args = []

    def read(self, n=-1):
        self.read_args.append(n)
        if isinstance(n, int) and n > 0:
            return self._body[:n]
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecorderOpener:
    """Stands in for the urllib opener; records every credentialed request."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def open(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "authorization": request.get_header("Authorization"),
            "timeout": timeout,
        })
        if self.error is not None:
            raise self.error
        return self.response


def _ok_opener(payload=None):
    body = json.dumps(payload or _LITE).encode("utf-8")
    return _RecorderOpener(response=_FakeResponse(body))


def _pool_snapshot(available=2, total=2):
    return SimpleNamespace(
        provider="zai", source="local_pool", title="Credential pool", plan=None,
        windows=(), details=(f"{available}/{total} credentials available",),
        available=available > 0,
        unavailable_reason=None if available > 0 else "All pool credentials are unavailable.",
        fetched_at=datetime.now(timezone.utc),
        pool={"total_credentials": total, "queried_credentials": 0,
              "available_credentials": available,
              "exhausted_credentials": total - available, "dead_credentials": 0,
              "failed_credentials": 0, "plans": [], "next_reset_at": None,
              "best_remaining_by_window": [], "credentials": []},
    )


@pytest.fixture(autouse=True)
def isolate_zai(monkeypatch):
    for name in ("ZAI_PEAK_TZ", "ZAI_PEAK_MULTIPLIER", "ZAI_OFFPEAK_MULTIPLIER"):
        monkeypatch.delenv(name, raising=False)
    with providers._zai_quota_cache_lock:
        providers._zai_quota_cache.clear()
        providers._zai_quota_flights.clear()
    providers._zai_quota_epoch = 0
    monkeypatch.setattr(config, "_get_provider_base_url", lambda pid: None, raising=False)
    yield
    with providers._zai_quota_cache_lock:
        providers._zai_quota_cache.clear()
        providers._zai_quota_flights.clear()


def _set_key(monkeypatch):
    monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: "test-key")


def _set_base(monkeypatch, url):
    monkeypatch.setattr(config, "_get_provider_base_url", lambda pid: url, raising=False)


# ── Blocker 1: configured-origin gate ───────────────────────────────────────

def test_monitor_url_derived_from_configured_origin():
    assert providers._zai_monitor_url(
        "https://open.bigmodel.cn/api/coding/paas/v4"
    ) == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    assert providers._zai_monitor_url(None) == \
        "https://api.z.ai/api/monitor/usage/quota/limit"


def test_monitor_url_rejects_unparseable_and_plain_http():
    assert providers._zai_monitor_url("not-a-url") is None
    assert providers._zai_monitor_url("ftp://api.z.ai/x") is None
    assert providers._zai_monitor_url("http://api.z.ai/api/paas/v4") is None
    # Loopback may stay plain http (local proxies are an operator's choice).
    assert providers._zai_monitor_url("http://127.0.0.1:8080/v1") == \
        "http://127.0.0.1:8080/api/monitor/usage/quota/limit"


def test_credential_goes_to_configured_origin_not_hardcoded_host(monkeypatch):
    opener = _ok_opener()
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    _set_key(monkeypatch)
    _set_base(monkeypatch, "https://open.bigmodel.cn/api/coding/paas/v4")
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["status"] == "available"
    assert len(opener.calls) == 1
    assert opener.calls[0]["url"].startswith("https://open.bigmodel.cn/")
    assert "api.z.ai" not in opener.calls[0]["url"]
    assert opener.calls[0]["authorization"] == "Bearer test-key"


def test_unverified_origin_sends_no_credential(monkeypatch):
    opener = _ok_opener()
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    _set_key(monkeypatch)
    _set_base(monkeypatch, "not-a-url")
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["ok"] is False
    assert opener.calls == []  # fail closed: no request, no bearer anywhere


def test_cache_identity_partitions_by_monitor_origin(monkeypatch):
    opener = _ok_opener()
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    _set_key(monkeypatch)
    _set_base(monkeypatch, "https://open.bigmodel.cn/api/coding/paas/v4")
    providers.get_provider_quota("zai", refresh=True)
    _set_base(monkeypatch, "https://api.z.ai/api/coding/paas/v4")
    providers.get_provider_quota("zai", refresh=True)
    assert len(opener.calls) == 2  # different origin -> different cache entry
    _set_base(monkeypatch, "https://open.bigmodel.cn/api/coding/paas/v4")
    providers.get_provider_quota("zai")  # first origin still cached
    assert len(opener.calls) == 2


# ── Blocker 2: redirects rejected ───────────────────────────────────────────

def test_redirect_handler_never_builds_a_redirect_request():
    handler = providers._ZaiNoRedirectHandler()
    assert handler.redirect_request(
        None, None, 302, "Found", None, "https://evil.example/steal") is None


def test_redirect_response_fails_closed(monkeypatch):
    opener = _RecorderOpener(error=urllib.error.HTTPError(
        "url", 302, "Found", None, None))
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    _set_key(monkeypatch)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert "redirect" in result["message"].lower()


# ── Blocker 3: bounded response body ────────────────────────────────────────

def test_response_read_is_capped(monkeypatch):
    opener = _ok_opener()
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    _set_key(monkeypatch)
    providers.get_provider_quota("zai", refresh=True)
    cap = providers._ZAI_QUOTA_MAX_RESPONSE_BYTES
    assert opener.response.read_args == [cap + 1]  # cap plus exactly one byte


def test_oversized_response_fails_closed(monkeypatch):
    cap = providers._ZAI_QUOTA_MAX_RESPONSE_BYTES
    opener = _RecorderOpener(response=_FakeResponse(b"x" * (cap + 1)))
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    _set_key(monkeypatch)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["ok"] is False
    assert result["status"] == "unavailable"


# ── Blocker 4: one shared terminal failure per flight ───────────────────────

def test_failed_flight_is_shared_no_waiter_fanout(monkeypatch):
    calls = {"n": 0}
    release = threading.Event()

    def failing(api_key, monitor_url=None):
        calls["n"] += 1
        release.wait(timeout=10)
        raise urllib.error.URLError("unavailable owner")

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", failing)
    _set_key(monkeypatch)
    results = []

    def caller():
        results.append(providers.get_provider_quota("zai"))

    threads = [threading.Thread(target=caller) for _ in range(5)]
    threads[0].start()
    for _ in range(400):
        with providers._zai_quota_cache_lock:
            if providers._zai_quota_flights:
                break
        time.sleep(0.005)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(timeout=10)
    assert calls["n"] == 1  # one transport call total, not five
    assert len(results) == 5
    assert all(r["status"] == "unavailable" for r in results)


def test_short_failure_cache_prevents_post_failure_burst(monkeypatch):
    rec = SimpleNamespace(calls=0)

    def failing(api_key, monitor_url=None):
        rec.calls += 1
        raise urllib.error.URLError("down")

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", failing)
    _set_key(monkeypatch)
    first = providers.get_provider_quota("zai", refresh=True)
    second = providers.get_provider_quota("zai")  # ordinary, right after
    assert first["status"] == "unavailable"
    assert second["status"] == "unavailable"
    assert rec.calls == 1  # failure marker absorbs the immediate retry
    # Marker is short-lived: after expiry a fresh call may fetch again.
    with providers._zai_quota_cache_lock:
        for key, entry in list(providers._zai_quota_cache.items()):
            providers._zai_quota_cache[key] = (
                entry[0] - providers._ZAI_QUOTA_FAILURE_TTL_SECONDS - 1,
            ) + tuple(entry[1:])
    third = providers.get_provider_quota("zai")
    assert rec.calls == 2
    assert third["status"] == "unavailable"


# ── Blocker 5: failed forced refresh evicts stale success ──────────────────

def test_failed_refresh_evicts_stale_success(monkeypatch):
    def fetch(api_key, monitor_url=None):
        fetch.calls += 1
        if fetch.calls == 2:
            raise urllib.error.URLError("mid-outage refresh")
        return _LITE

    fetch.calls = 0
    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", fetch)
    _set_key(monkeypatch)
    assert providers.get_provider_quota("zai", refresh=True)["status"] == "available"
    assert providers.get_provider_quota("zai", refresh=True)["status"] == "unavailable"
    # The reproduced bug: this ordinary request resurrected the old success.
    assert providers.get_provider_quota("zai")["status"] == "unavailable"


# ── Blocker 6: local pool preserved, merged, and used as fallback ───────────

def test_pool_fallback_on_auth_failure(monkeypatch):
    def fetch(api_key, monitor_url=None):
        raise urllib.error.HTTPError("url", 401, "Unauthorized", None, None)

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", fetch)
    monkeypatch.setattr(providers, "_local_pool_snapshot", lambda p: _pool_snapshot())
    _set_key(monkeypatch)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["ok"] is True
    assert result["status"] == "available"
    assert result["label"] == "Credential pool"
    assert result["account_limits"]["source"] == "local_pool"
    assert result["account_limits"]["pool"]["available_credentials"] == 2


def test_pool_fallback_on_transport_failure(monkeypatch):
    def fetch(api_key, monitor_url=None):
        raise urllib.error.URLError("net down")

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", fetch)
    monkeypatch.setattr(providers, "_local_pool_snapshot", lambda p: _pool_snapshot())
    _set_key(monkeypatch)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["status"] == "available"


def test_pool_fallback_on_parser_failure(monkeypatch):
    monkeypatch.setattr(providers, "_zai_fetch_quota_payload",
                        lambda k, monitor_url=None: {"unexpected": "shape"})
    monkeypatch.setattr(providers, "_local_pool_snapshot", lambda p: _pool_snapshot())
    _set_key(monkeypatch)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["status"] == "available"
    assert result["account_limits"]["source"] == "local_pool"


def test_no_key_falls_back_to_pool(monkeypatch):
    monkeypatch.setattr(providers, "_get_provider_api_key", lambda p: None)
    monkeypatch.setattr(providers, "_local_pool_snapshot", lambda p: _pool_snapshot())
    result = providers.get_provider_quota("zai")
    assert result["status"] == "available"
    assert result["label"] == "Credential pool"


def test_remote_success_merges_pool_breakdown(monkeypatch):
    monkeypatch.setattr(providers, "_zai_fetch_quota_payload",
                        lambda k, monitor_url=None: _LITE)
    monkeypatch.setattr(providers, "_local_pool_snapshot", lambda p: _pool_snapshot())
    _set_key(monkeypatch)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["status"] == "available"
    limits = result["account_limits"]
    assert limits["source"] == "zai_monitor_api"
    assert limits["windows"][0]["label"] == "5-hour"  # remote windows kept
    assert limits["pool"]["available_credentials"] == 2  # pool envelope kept
    joined = " ".join(limits["details"])
    assert "credentials available" in joined
    assert result["peak"]["summary"] in joined  # peak detail still present


def test_unverified_origin_falls_back_to_pool(monkeypatch):
    opener = _ok_opener()
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    monkeypatch.setattr(providers, "_local_pool_snapshot", lambda p: _pool_snapshot())
    _set_key(monkeypatch)
    _set_base(monkeypatch, "not-a-url")
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["status"] == "available"
    assert result["label"] == "Credential pool"
    assert opener.calls == []
