"""Regression tests for #4718 — post-profile-switch sidebar pre-warm.

Measurement on v0.51.610 (@rodboev's CDP profiling) showed the dominant remaining
cold-switch cost is the `/api/sessions` payload build (~780ms), paid by the client's
sidebar GET that lands right after `POST /api/profile/switch` returns (~200ms). The
gateway-watcher stop() join — the original Phase-2 suspect — is ~0.4ms and not the
bottleneck. And #4769's `sidebar_source=webui` path means the sidebar no longer calls
`get_cli_sessions()` at all, so the relevant cache is the `_session_list_cache` layer
(`_get_cached_session_list_payload`), not the lower CLI-sessions cache.

`warm_session_list_cache()` therefore warms the SAME `_session_list_cache` the sidebar
reads, mirroring the GET handler's key + builder, so the post-switch GET hits warm.
These tests pin that behavior.
"""

from __future__ import annotations

import threading
import time

import pytest

import api.routes as routes


@pytest.fixture(autouse=True)
def _reset_warm_state():
    with routes._SESSION_LIST_WARM_LOCK:
        routes._SESSION_LIST_WARM_INFLIGHT.clear()
    yield
    with routes._SESSION_LIST_WARM_LOCK:
        routes._SESSION_LIST_WARM_INFLIGHT.clear()


def _patch_common(monkeypatch, build_counter=None, build_hook=None):
    """Patch the GET-handler collaborators so warm_session_list_cache exercises the
    real cache path without DB/settings I/O."""
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": False,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
        "agent_session_source_filter": None,
    })
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "work", raising=False)
    # Patch the profiles module symbol the function imports lazily, too.
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")

    def _fake_build(**kwargs):
        if build_counter is not None:
            build_counter["n"] += 1
        if build_hook is not None:
            build_hook()
        return {"sessions": [{"session_id": "s1"}], "active_profile": "work"}

    monkeypatch.setattr(routes, "_build_session_list_cache_payload", _fake_build)
    # Clear any residual cache entries for a clean key space.
    try:
        routes._session_list_cache_clear()
    except Exception:
        pass


def test_warm_populates_the_sidebar_cache(monkeypatch):
    """After warming, the exact sidebar key must be a fresh cache hit (no rebuild)."""
    counter = {"n": 0}
    _patch_common(monkeypatch, build_counter=counter)

    ran = routes.warm_session_list_cache("work")
    assert ran is True
    assert counter["n"] == 1, "warm should have built exactly once"

    # Reconstruct the same key the sidebar GET would use and confirm it's a fresh hit.
    key = routes._session_list_cache_key(
        active_profile="work",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        exclude_hidden=True,
        visible_only=True,
        source_filter=None,
        sidebar_source="webui",
    )
    cached, is_fresh = routes._session_list_cache_get(key, allow_stale=False)
    assert cached is not None and is_fresh, "sidebar key should be warm after pre-warm"


def test_warm_skips_when_already_fresh(monkeypatch):
    """A second warm while the entry is still fresh must NOT rebuild."""
    counter = {"n": 0}
    _patch_common(monkeypatch, build_counter=counter)

    assert routes.warm_session_list_cache("work") is True
    assert counter["n"] == 1
    # Second warm: entry is fresh → skipped, no rebuild.
    assert routes.warm_session_list_cache("work") is False
    assert counter["n"] == 1, "fresh entry must not be rebuilt by a second warm"


def test_warm_dedups_concurrent_warms(monkeypatch):
    """Concurrent warms for the same profile must not fan out duplicate builds."""
    counter = {"n": 0}
    gate = threading.Event()

    def _slow():
        gate.wait(timeout=5)

    _patch_common(monkeypatch, build_counter=counter, build_hook=_slow)

    results = {}
    threads = []
    for i in range(5):
        def _c(idx=i):
            results[idx] = routes.warm_session_list_cache("work")
        t = threading.Thread(target=_c)
        threads.append(t)
        t.start()

    time.sleep(0.3)  # let them reach the in-flight guard
    gate.set()
    for t in threads:
        t.join(timeout=5)

    # Exactly one warm should have done the build; the rest deduped (returned False).
    assert counter["n"] == 1, f"expected single build under dedup, got {counter['n']}"
    assert sum(1 for v in results.values() if v is True) == 1
    assert sum(1 for v in results.values() if v is False) == 4


def test_warm_never_raises(monkeypatch):
    """A failing build must be swallowed — warming is best-effort and must never break
    the switch path."""
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")

    def _boom(**kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(routes, "_build_session_list_cache_payload", _boom)
    try:
        routes._session_list_cache_clear()
    except Exception:
        pass

    # Must not raise; returns False on failure.
    assert routes.warm_session_list_cache("work") is False


def test_cold_miss_follower_joins_inflight_build_no_double_build():
    """The post-switch effectiveness fix (#4718): on a true COLD miss (no cached or
    stale payload) with a rebuild genuinely in-flight — e.g. the pre-warm is building —
    a second caller (the client's sidebar GET) must JOIN that build and receive its
    result, NOT give up after the old 0.25s wait and redundantly rebuild. Pins that the
    cold-join wait closes the warm-vs-client double-build window.
    """
    routes._session_list_cache_clear()

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    lock = threading.Lock()

    def builder():
        with lock:
            calls["n"] += 1
        started.set()
        # Hold the build well past the OLD 0.25s follower wait to prove the follower
        # now rides the in-flight build instead of timing out and double-building.
        release.wait(timeout=5)
        return {"sessions": [{"session_id": "joined"}], "active_profile": "work"}

    key = routes._session_list_cache_key(
        active_profile="work",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        exclude_hidden=True,
        visible_only=True,
        sidebar_source="webui",
    )
    results = []

    def reader():
        results.append(routes._get_cached_session_list_payload(key=key, builder=builder))

    owner = threading.Thread(target=reader)
    owner.start()
    assert started.wait(2.0), "owner build never started"
    # Follower arrives on a COLD key (no stale payload) while the build is in flight.
    follower = threading.Thread(target=reader)
    follower.start()
    # Give the follower time to pass the OLD 0.25s ceiling; it should still be waiting
    # (joining), not have fallen through to its own build.
    time.sleep(0.5)
    with lock:
        assert calls["n"] == 1, "follower double-built instead of joining the in-flight build"
    release.set()
    owner.join(3)
    follower.join(3)
    assert len(results) == 2
    assert results[0] == results[1] == {"sessions": [{"session_id": "joined"}], "active_profile": "work"}
    assert calls["n"] == 1, "exactly one build should have run for the cold-join case"


def test_cold_miss_follower_rejoins_build_slower_than_one_wait():
    """Re-join hardening (#4718, Opus gate): if the owner's build runs LONGER than a
    single cold-join wait, the follower must re-join and ride it to completion — NOT
    fall through to its own redundant build (which would double-build AND stack latency
    on the largest profiles this path targets). Pins single-build even for a slow owner.

    Driven by shrinking the per-iteration wait so the test's ~0.6s build exceeds one
    wait but stays well under the total deadline.
    """
    routes._session_list_cache_clear()
    orig_wait = routes._SESSIONS_CACHE_COLD_JOIN_WAIT_SECONDS
    orig_max = routes._SESSIONS_CACHE_COLD_JOIN_MAX_WAIT_SECONDS
    routes._SESSIONS_CACHE_COLD_JOIN_WAIT_SECONDS = 0.15  # < build time, forces re-join
    routes._SESSIONS_CACHE_COLD_JOIN_MAX_WAIT_SECONDS = 5.0
    try:
        started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}
        lock = threading.Lock()

        def builder():
            with lock:
                calls["n"] += 1
            started.set()
            release.wait(timeout=5)
            return {"sessions": [{"session_id": "slow"}], "active_profile": "work"}

        key = routes._session_list_cache_key(
            active_profile="work",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            exclude_hidden=True,
            visible_only=True,
            sidebar_source="webui",
        )
        results = []

        def reader():
            results.append(routes._get_cached_session_list_payload(key=key, builder=builder))

        owner = threading.Thread(target=reader)
        owner.start()
        assert started.wait(2.0)
        follower = threading.Thread(target=reader)
        follower.start()
        # Let the follower pass SEVERAL join-wait iterations (0.15s each) while the build
        # is still held — it must keep re-joining, not fall through and build.
        time.sleep(0.6)
        with lock:
            assert calls["n"] == 1, "follower fell through to its own build instead of re-joining"
        release.set()
        owner.join(3)
        follower.join(3)
        assert len(results) == 2
        assert calls["n"] == 1, "exactly one build for a slow owner with a re-joining follower"
    finally:
        routes._SESSIONS_CACHE_COLD_JOIN_WAIT_SECONDS = orig_wait
        routes._SESSIONS_CACHE_COLD_JOIN_MAX_WAIT_SECONDS = orig_max

