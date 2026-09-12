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
        for attr in ("_zai_quota_transport_locks",):
            table = getattr(providers, attr, None)
            if isinstance(table, dict):
                table.clear()
    providers._zai_quota_epoch = 0
    monkeypatch.setattr(config, "_get_provider_base_url", lambda pid: None, raising=False)
    yield
    with providers._zai_quota_cache_lock:
        providers._zai_quota_cache.clear()
        providers._zai_quota_flights.clear()
        for attr in ("_zai_quota_transport_locks",):
            table = getattr(providers, attr, None)
            if isinstance(table, dict):
                table.clear()


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


def test_monitor_url_dns_name_with_127_prefix_is_not_loopback():
    # "127.evil.example" is a DNS name, not a loopback address; a bearer must
    # never be sent there over plain http (review finding on the first fix).
    assert providers._zai_monitor_url("http://127.evil.example/v1") is None
    assert providers._zai_monitor_url("http://127.0.0.1.0/v1") is None
    # Genuine loopback forms stay allowed.
    assert providers._zai_monitor_url("http://localhost:9000/v1") is not None
    assert providers._zai_monitor_url("http://127.0.0.2/v1") is not None
    assert providers._zai_monitor_url("http://[::1]:9000/v1") is not None


def test_monitor_url_rejects_invalid_port_and_userinfo():
    assert providers._zai_monitor_url("https://api.z.ai:notaport/v4") is None
    assert providers._zai_monitor_url("https://u:p@api.z.ai/v4") is None


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


# ── Re-gate round: forced refresh joins the in-flight request ───────────────


def _thread_parked_in_wait(thread):
    """True when the thread is blocked inside an Event/Condition .wait()."""
    import sys
    frame = sys._current_frames().get(thread.ident)
    return frame is not None and frame.f_code.co_name == "wait"


def _wait_until_parked(threads, timeout=10.0):
    """Block until every thread is parked inside a .wait() call."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_thread_parked_in_wait(t) for t in threads):
            return True
        time.sleep(0.005)
    return False


def test_concurrent_forced_refreshes_share_one_transport(monkeypatch):
    """Several simultaneous refresh=True callers must share ONE in-flight
    request (re-gate finding: each forced refresh registered its own flight
    and made its own credentialed transport call — 4 calls instead of 1)."""
    calls = {"n": 0}
    release = threading.Event()

    def owner_fetch(api_key, monitor_url=None):
        calls["n"] += 1
        release.wait(timeout=10)
        return _LITE

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", owner_fetch)
    _set_key(monkeypatch)
    start = threading.Barrier(5)  # four callers + the main-thread releaser
    results = []
    lock = threading.Lock()

    def caller():
        start.wait(timeout=10)
        r = providers.get_provider_quota("zai", refresh=True)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=caller) for _ in range(4)]
    for t in threads:
        t.start()
    start.wait(timeout=10)  # release all four callers together
    for _ in range(400):
        with providers._zai_quota_cache_lock:
            if providers._zai_quota_flights:
                break
        time.sleep(0.005)
    with providers._zai_quota_cache_lock:
        assert len(providers._zai_quota_flights) == 1, \
            "concurrent forced refreshes registered more than one flight"
    # Every caller must be parked (owner inside the gated fetch, the other
    # three on the flight event) before the single transport completes.
    assert _wait_until_parked(threads), "a caller never reached a wait point"
    release.set()
    for t in threads:
        t.join(timeout=10)
    assert calls["n"] == 1  # exactly one transport call, not four
    assert len(results) == 4
    assert all(r["status"] == "available" for r in results)
    # One shared terminal result: every caller got the identical payload.
    assert len({r["account_limits"]["fetched_at"] for r in results}) == 1
    assert len({r["account_limits"]["windows"][0]["used_percent"]
                for r in results}) == 1


# ── Verification-review round 2 findings ────────────────────────────────────

def test_waiter_cannot_return_success_after_newer_refresh_fails(monkeypatch):
    """A waiter joined to an older flight must not return its success after a
    newer forced refresh failed. Distinct old-owner and waiter threads are
    started BEFORE the newer refresh (re-gate repair: the previous version
    started only one thread, which was the old owner, not a joined waiter).
    The newer refresh joins the older flight, times out, and takes over via
    the bounded steal; its failure is then the shared terminal truth."""
    import threading as _th
    monkeypatch.setattr(providers, "_ZAI_QUOTA_JOIN_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(providers, "_ZAI_QUOTA_TRANSPORT_WAIT_SECONDS", 0.25)
    old_release = _th.Event()

    def old_owner(api_key, monitor_url=None):
        old_release.wait(timeout=10)
        return _payload(20)

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", old_owner)
    _set_key(monkeypatch)
    owner_result = {}
    waiter_result = {}

    t_owner = _th.Thread(
        target=lambda: owner_result.setdefault(
            "r", providers.get_provider_quota("zai")))
    t_owner.start()
    for _ in range(400):
        with providers._zai_quota_cache_lock:
            if providers._zai_quota_flights:
                break
        time.sleep(0.005)
    # A genuine WAITER: joins the older owner's flight while it is gated.
    t_waiter = _th.Thread(
        target=lambda: waiter_result.setdefault(
            "r", providers.get_provider_quota("zai")))
    t_waiter.start()
    assert _wait_until_parked([t_waiter]), \
        "waiter never joined the older flight"
    # Newer forced refresh: joins the older flight, times out, steals, fails.
    def failing(api_key, monitor_url=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", failing)
    refresh = providers.get_provider_quota("zai", refresh=True)
    assert refresh["status"] == "unavailable"
    old_release.set()
    t_owner.join(timeout=10)
    t_waiter.join(timeout=10)
    assert owner_result["r"]["status"] == "unavailable"
    assert waiter_result["r"]["status"] == "unavailable"


def test_waiter_success_invalidated_by_credential_mutation(monkeypatch):
    """A waiter's joined success must not survive a credential mutation that
    landed while it waited (epoch guard covers publication only — review
    finding: owner=available, waiter=available, cache_entries=0)."""
    import threading as _th
    release = _th.Event()

    def owner(api_key, monitor_url=None):
        release.wait(timeout=10)
        providers.invalidate_zai_quota_cache("zai")  # mid-flight mutation
        return _payload(30)

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", owner)
    _set_key(monkeypatch)
    waiter_result = {}

    def run_waiter():
        waiter_result["r"] = providers.get_provider_quota("zai")

    t_owner = _th.Thread(target=lambda: providers.get_provider_quota("zai"))
    t_owner.start()
    for _ in range(400):
        with providers._zai_quota_cache_lock:
            if providers._zai_quota_flights:
                break
        time.sleep(0.005)
    t_waiter = _th.Thread(target=run_waiter)
    t_waiter.start()
    release.set()
    t_owner.join(timeout=10)
    t_waiter.join(timeout=10)
    assert waiter_result["r"]["status"] == "unavailable"


def test_json_null_body_publishes_failure_marker(monkeypatch):
    """A JSON null body is a terminal parser failure and must publish the
    shared failure marker (review finding: retried without bound)."""
    calls = {"n": 0}

    def null_fetch(api_key, monitor_url=None):
        calls["n"] += 1
        return None

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", null_fetch)
    _set_key(monkeypatch)
    first = providers.get_provider_quota("zai", refresh=True)
    second = providers.get_provider_quota("zai")  # ordinary, right after
    assert first["status"] == "unavailable"
    assert second["status"] == "unavailable"
    assert calls["n"] == 1


def test_truncated_response_fails_closed_to_pool(monkeypatch):
    """http.client.IncompleteRead must fall back to the local pool, not
    propagate (review finding: exception outside the catch tuple)."""
    import http.client as _hc

    def truncated(api_key, monitor_url=None):
        raise _hc.IncompleteRead(partial=b"", expected=10)

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", truncated)
    monkeypatch.setattr(providers, "_local_pool_snapshot", lambda p: _pool_snapshot())
    _set_key(monkeypatch)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["status"] == "available"
    assert result["account_limits"]["source"] == "local_pool"


def test_alias_configured_origin_is_honored(monkeypatch):
    """providers.glm.base_url (alias) must gate the origin exactly like
    providers.zai.base_url (review finding: alias config ignored)."""
    opener = _ok_opener()
    monkeypatch.setattr(providers, "_zai_http_opener", lambda: opener)
    _set_key(monkeypatch)
    seen = {}

    def fake_lookup(pid):
        seen[pid] = True
        return "https://open.bigmodel.cn/api/coding/paas/v4" if pid == "glm" else None

    monkeypatch.setattr(config, "_get_provider_base_url", fake_lookup, raising=False)
    result = providers.get_provider_quota("zai", refresh=True)
    assert result["status"] == "available"
    assert seen.get("glm") is True
    assert len(opener.calls) == 1
    assert opener.calls[0]["url"].startswith("https://open.bigmodel.cn/")


# ── Verification-review round 3 findings ────────────────────────────────────

def test_monitor_url_rejects_malformed_ipv4_loopback_forms():
    # Non-canonical/malformed octets must not pass the loopback gate
    # (round-2 finding: 127.999.999.999 and 127.0.0.256 were accepted).
    for bad in ("http://127.999.999.999/v1", "http://127.0.0.256/v1",
                "http://127.000.000.001/v1", "http://0127.0.0.1/v1",
                "http://127.0.0.1.0/v1"):
        assert providers._zai_monitor_url(bad) is None, bad


def test_monitor_url_accepts_ipv6_mapped_loopback():
    # ::ffff:127.0.0.1 is a legitimate loopback destination
    # (round-2 finding: wrongly rejected).
    assert providers._zai_monitor_url("http://[::ffff:127.0.0.1]:9000/v1") == \
        "http://[::ffff:127.0.0.1]:9000/api/monitor/usage/quota/limit"
    assert providers._zai_monitor_url("http://[::1]/v1") is not None
    # But a mapped NON-loopback address stays rejected on plain http.
    assert providers._zai_monitor_url("http://[::ffff:8.8.8.8]/v1") is None


def test_superseded_owner_caller_also_unavailable(monkeypatch):
    """When a newer refresh takes over an older flight mid-fetch (bounded
    steal after the join timeout), the older owner's own caller must also
    report unavailable (no stale success from either side of the flight —
    closes the pop→assign race window)."""
    import threading as _th
    monkeypatch.setattr(providers, "_ZAI_QUOTA_JOIN_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(providers, "_ZAI_QUOTA_TRANSPORT_WAIT_SECONDS", 0.25)
    old_release = _th.Event()

    def old_owner(api_key, monitor_url=None):
        old_release.wait(timeout=10)
        return _payload(50)

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", old_owner)
    _set_key(monkeypatch)
    owner_result = {}

    def run_owner():
        owner_result["r"] = providers.get_provider_quota("zai")

    t_owner = _th.Thread(target=run_owner)
    t_owner.start()
    for _ in range(400):
        with providers._zai_quota_cache_lock:
            if providers._zai_quota_flights:
                break
        time.sleep(0.005)
    # Newer forced refresh joins the older flight, times out, steals
    # ownership, and fails.
    def failing(api_key, monitor_url=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", failing)
    refresh = providers.get_provider_quota("zai", refresh=True)
    assert refresh["status"] == "unavailable"
    old_release.set()
    t_owner.join(timeout=10)
    # The old owner was superseded: its caller must NOT report its stale
    # success even though the fetch itself succeeded.
    assert owner_result["r"]["status"] == "unavailable"


# ── Re-gate round 2: physical single-flight in the bounded-steal path ───────


def test_two_successive_steals_keep_peak_transport_concurrency_at_one(monkeypatch):
    """Maintainer-required regression (review 5176373527).

    Two successive join timeouts with each old body held open must never
    produce more than ONE live credentialed transport per cache key. The
    steal path must wait for the prior physical owner to acknowledge exit
    (or fail soft) before starting a replacement transport.
    """
    monkeypatch.setattr(providers, "_ZAI_QUOTA_JOIN_TIMEOUT_SECONDS", 0.25)
    live = {"n": 0}
    peak = {"n": 0}
    lock = threading.Lock()
    releases = [threading.Event() for _ in range(3)]
    calls = {"n": 0}

    def held_open_fetch(api_key, monitor_url=None):
        with lock:
            calls["n"] += 1
            idx = calls["n"] - 1
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        try:
            # Hold every body open past any join timeout so steal after
            # steal stacks up if the implementation allows it.
            releases[idx].wait(timeout=10)
            return _payload(30)
        finally:
            with lock:
                live["n"] -= 1

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", held_open_fetch)
    _set_key(monkeypatch)
    results = {}

    def call(refresh):
        results.setdefault(len(results), providers.get_provider_quota("zai", refresh=refresh))

    t_owner = threading.Thread(target=call, args=(False,))
    t_owner.start()
    for _ in range(400):
        with providers._zai_quota_cache_lock:
            if providers._zai_quota_flights:
                break
        time.sleep(0.005)
    # Steal #1: a waiter times out on the owner's flight and elects itself.
    t_w1 = threading.Thread(target=call, args=(True,))
    t_w1.start()
    time.sleep(0.6)  # > join timeout (0.25 s): the steal decision is made
    # Steal #2: a third caller times out on the (now replaced) flight and
    # would repeat the operation under the old implementation.
    t_w2 = threading.Thread(target=call, args=(True,))
    t_w2.start()
    time.sleep(0.6)
    # No matter how the election lands, at most one transport body is live.
    with lock:
        assert peak["n"] <= 1, f"peak live transports hit {peak['n']}"
    # Drain: release every body and join all callers.
    for ev in releases:
        ev.set()
    for t in (t_owner, t_w1, t_w2):
        t.join(timeout=10)
    assert not any(t.is_alive() for t in (t_owner, t_w1, t_w2))
    with lock:
        assert peak["n"] <= 1, f"peak live transports hit {peak['n']} (after drain)"
    assert calls["n"] >= 1  # at least the original owner ran


def test_invalidation_before_timeout_launches_no_retired_credential(monkeypatch):
    """Maintainer-required regression (review 5176373527).

    A waiter that retains the pre-invalidation key/origin while waiting must
    not start that retired credentialed request after an epoch change: the
    epoch and the live key/origin must be re-checked immediately before the
    transport side effect.
    """
    monkeypatch.setattr(providers, "_ZAI_QUOTA_JOIN_TIMEOUT_SECONDS", 0.25)
    owner_release = threading.Event()
    seen = {"key": None, "url": None}

    def held_open_owner(api_key, monitor_url=None):
        seen["key"] = api_key
        seen["url"] = monitor_url
        owner_release.wait(timeout=10)
        return _payload(30)

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", held_open_owner)
    _set_key(monkeypatch)
    _set_base(monkeypatch, "https://good.example/api")
    results = {}

    t_owner = threading.Thread(target=lambda: results.setdefault(
        "owner", providers.get_provider_quota("zai")))
    t_owner.start()
    for _ in range(400):
        with providers._zai_quota_cache_lock:
            if providers._zai_quota_flights:
                break
        time.sleep(0.005)
    # A second caller joins the owner's flight and will time out on it.
    t_waiter = threading.Thread(target=lambda: results.setdefault(
        "waiter", providers.get_provider_quota("zai")))
    t_waiter.start()
    time.sleep(0.1)  # parked on the flight event
    # Credential mutation lands while the waiter is parked: epoch moves.
    providers.invalidate_zai_quota_cache("zai")
    # Let the waiter time out and (if allowed) steal. Any transport the
    # waiter starts must carry the CURRENT live credential, not the one
    # captured before the wait — and none may fire the retired origin.
    fired = {"n": 0}

    def recorder(api_key, monitor_url=None):
        fired["n"] += 1
        return _payload(30)

    monkeypatch.setattr(providers, "_zai_fetch_quota_payload", recorder)
    time.sleep(0.5)  # past the join timeout
    owner_release.set()
    t_owner.join(timeout=10)
    t_waiter.join(timeout=10)
    assert not t_waiter.is_alive()
    # The waiter observed the epoch change: it must not have launched the
    # retired credential/origin at all.
    assert fired["n"] == 0, "a post-invalidation transport fired"


def test_monitor_url_rejects_ipv6_scope_ids():
    # Zone IDs are interface-local, not a stable destination origin
    # (round-3 finding).
    assert providers._zai_monitor_url("http://[::1%eth0]/v1") is None
    assert providers._zai_monitor_url("http://[::ffff:127.0.0.1%eth0]/v1") is None
