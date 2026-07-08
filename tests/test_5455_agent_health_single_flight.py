"""Regression tests for single-flight remote-gateway health probing (#5455, #2476).

Pre-fix behaviour: ``_probe_remote_gateway`` held ``_remote_probe_lock`` only
around the cache read and the cache write; the network probe itself
(``_http_probe`` per path, each up to ~2s) ran with no lock held.  On a cold
cache a dashboard that fans out to N panels therefore fired N concurrent probe
sets at the (possibly dead) gateway, each blocking for up to ~6s.

Fix: single-flight.  The first "leader" thread marks the base_url in-flight and
runs the probe; latecomers wait on ``_remote_probe_cond`` for the leader's
cached result instead of probing themselves.  The leader always clears the
marker and wakes waiters (even on error) so no waiter can deadlock.

These tests pin that only ONE probe set runs under concurrency, that all callers
observe the same result, and that a probe exception does not hang waiters.
"""

from __future__ import annotations

import threading
import time

import pytest

from api import agent_health


@pytest.fixture(autouse=True)
def _clear_cache():
    agent_health._reset_remote_probe_cache_for_tests()
    yield
    agent_health._reset_remote_probe_cache_for_tests()


def _run_concurrent(base_url: str, n: int, timeout: float = 20.0) -> list[object]:
    """Fire *n* threads at ``_probe_remote_gateway`` simultaneously.

    Returns each thread's result (or the exception it raised).  Uses a Barrier
    so all threads reach the call together and genuinely contend on a cold cache.
    """
    barrier = threading.Barrier(n)
    results: list[object] = [None] * n

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            results[idx] = agent_health._probe_remote_gateway(base_url)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            results[idx] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    # No thread may still be running — a hang would mean a single-flight deadlock.
    assert not any(t.is_alive() for t in threads), "probe threads did not finish"
    return results


def test_concurrent_cold_cache_probes_only_once(monkeypatch):
    base_url = "http://gw:9999"
    call_count = 0
    count_lock = threading.Lock()

    def fake_http_probe(url, timeout_s, *, api_key=None):
        nonlocal call_count
        with count_lock:
            call_count += 1
        # Hold the leader in the probe long enough for latecomers to pile up and
        # wait, so a lock-less stampede would show as call_count > 1.
        time.sleep(0.3)
        return (True, 200, None, b"{}")  # 2xx on the first path

    monkeypatch.setattr(agent_health, "_http_probe", fake_http_probe)

    results = _run_concurrent(base_url, n=8)

    # Single-flight: exactly one leader ran the probe (first path returned 2xx).
    assert call_count == 1, f"expected 1 probe, got {call_count}"
    # Every caller observed the same alive result.
    for r in results:
        assert isinstance(r, dict), r
        assert r["alive"] is True
        assert r["details"]["endpoint"] == base_url + "/health/detailed"


def test_concurrent_down_gateway_probes_paths_once_each(monkeypatch):
    base_url = "http://gw:9998"
    call_count = 0
    count_lock = threading.Lock()

    def fake_http_probe(url, timeout_s, *, api_key=None):
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.2)
        return (False, None, "URLError", None)  # never ok -> walks all paths

    monkeypatch.setattr(agent_health, "_http_probe", fake_http_probe)

    results = _run_concurrent(base_url, n=6)

    # One leader walked every path exactly once; latecomers did not re-probe.
    assert call_count == len(agent_health._REMOTE_PROBE_PATHS), (
        f"expected {len(agent_health._REMOTE_PROBE_PATHS)} probes, got {call_count}"
    )
    for r in results:
        assert isinstance(r, dict), r
        assert r["alive"] is False
        assert r["details"]["reason"] == "remote_gateway_unreachable"


def test_slow_walk_exceeding_ttl_still_single_flight(monkeypatch):
    """A probe walk LONGER than the cache TTL must not collapse single-flight.

    Regression for a born-expired cache: the leader wrote
    ``expires_at = entry_time + TTL``.  Walking every path of a hung gateway
    takes len(paths) * timeout, which exceeds the TTL, so the cache line was
    already stale the instant the leader stored it.  Latecomers woken right
    after then missed the cache and each re-probed the dead gateway — the exact
    #5455/#2476 fan-out the fix is meant to prevent (and worse: serialized).

    Here the per-path sleep makes the total walk exceed a shrunk TTL.  The fix
    expires from the COMPLETION time, so the freshly written result stays valid
    long enough for every waiter to read it: still exactly one probe set.
    """
    base_url = "http://gw:9996"
    # Walk = len(paths) * 0.25s; keep TTL well below that but comfortably above
    # thread wake/reacquire latency so the completion-time cache is readable.
    monkeypatch.setattr(agent_health, "_REMOTE_PROBE_CACHE_TTL_S", 0.4)
    call_count = 0
    count_lock = threading.Lock()

    def fake_http_probe(url, timeout_s, *, api_key=None):
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.25)
        return (False, None, "URLError", None)  # down -> leader walks all paths

    monkeypatch.setattr(agent_health, "_http_probe", fake_http_probe)

    results = _run_concurrent(base_url, n=8)

    # One leader walked every path once; no waiter re-probed despite walk > TTL.
    assert call_count == len(agent_health._REMOTE_PROBE_PATHS), (
        f"born-expired cache regressed single-flight: expected "
        f"{len(agent_health._REMOTE_PROBE_PATHS)} probes, got {call_count}"
    )
    for r in results:
        assert isinstance(r, dict), r
        assert r["alive"] is False


def test_probe_exception_wakes_waiters_without_deadlock(monkeypatch):
    """A crash inside the leader's probe must not leave waiters hung on the cond."""
    base_url = "http://gw:9997"
    boom = RuntimeError("simulated probe crash")

    def fake_http_probe(url, timeout_s, *, api_key=None):
        time.sleep(0.2)
        raise boom

    monkeypatch.setattr(agent_health, "_http_probe", fake_http_probe)

    results = _run_concurrent(base_url, n=5)

    # Every caller surfaced the error (no result cached); crucially, the
    # _run_concurrent join assertion already proved none of them deadlocked.
    for r in results:
        assert isinstance(r, RuntimeError), r
        assert "simulated probe crash" in str(r)
