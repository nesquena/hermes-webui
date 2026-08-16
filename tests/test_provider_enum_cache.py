"""Regression tests for the provider-auth enumeration cache."""

import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor


def _install_fake_models(monkeypatch, provider_fn):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = provider_fn
    fake_auth = types.ModuleType("hermes_cli.auth")
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)


def _clear_cache(config):
    with config._PROVIDER_ENUM_CACHE_LOCK:
        pending = config._clear_provider_enum_cache_locked()
    for event in pending:
        event.set()


def test_concurrent_cold_misses_are_coalesced(monkeypatch):
    import api.config as config

    calls = 0
    calls_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def enumerate_providers():
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=5)
        return [{"id": "openai", "authenticated": True}]

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(config._list_available_providers_cached, "default") for _ in range(8)]
        assert started.wait(timeout=5)
        time.sleep(0.05)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert calls == 1
    assert all(result == [{"id": "openai", "authenticated": True}] for result in results)


def test_ttl_starts_after_slow_enumeration_completes(monkeypatch):
    import api.config as config

    clock = [0.0]
    monkeypatch.setattr(config.time, "monotonic", lambda: clock[0])
    calls = 0

    def enumerate_providers():
        nonlocal calls
        calls += 1
        clock[0] = 10.0
        return []

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    config._list_available_providers_cached("default")
    clock[0] = 115.0
    config._list_available_providers_cached("default")

    assert calls == 1


def test_profiles_have_independent_inflight_refreshes(monkeypatch):
    import api.config as config

    calls = []
    barrier = threading.Barrier(2)

    def enumerate_providers():
        calls.append(threading.current_thread().name)
        barrier.wait(timeout=5)
        return []

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(config._list_available_providers_cached, ("one", "two")))

    assert results == [[], []]
    assert len(calls) == 2


def test_failed_refresh_releases_waiters_and_is_retryable(monkeypatch):
    import api.config as config

    calls = 0
    calls_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def enumerate_providers():
        nonlocal calls
        with calls_lock:
            calls += 1
            call_no = calls
        if call_no == 1:
            started.set()
            assert release.wait(timeout=5)
            raise RuntimeError("probe failed")
        return []

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(config._list_available_providers_cached, "default")
        assert started.wait(timeout=5)
        second = executor.submit(config._list_available_providers_cached, "default")
        release.set()
        assert isinstance(first.exception(timeout=5), RuntimeError)
        assert second.result(timeout=5) == []

    assert calls == 2
    assert config._list_available_providers_cached("default") == []
    assert calls == 2


def test_invalidate_models_cache_drops_provider_enum_cache(monkeypatch):
    import api.config as config

    current = [{"id": "openai", "authenticated": True}]

    def enumerate_providers():
        return list(current)

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    assert config._list_available_providers_cached("default") == [
        {"id": "openai", "authenticated": True}
    ]

    current[:] = [{"id": "anthropic", "authenticated": True}]
    assert config._list_available_providers_cached("default") == [
        {"id": "openai", "authenticated": True}
    ]

    config.invalidate_models_cache()
    assert config._list_available_providers_cached("default") == [
        {"id": "anthropic", "authenticated": True}
    ]


def test_invalidate_during_inflight_refresh_discards_stale_result(monkeypatch):
    """Mid-flight invalidation: the stale first probe must never escape.

    E1 starts probing, invalidation lands, E2 starts probing while E1 is
    still in flight, then E1 completes with its pre-invalidation result.
    The stale result must be discarded (never returned to the caller, never
    published) — both callers observe the fresh post-invalidation
    enumeration, and exactly two probes ran (no ABA-induced third probe).
    """
    import api.config as config

    calls = 0
    calls_lock = threading.Lock()
    started1 = threading.Event()
    started2 = threading.Event()
    release1 = threading.Event()
    release2 = threading.Event()
    current = [{"id": "anthropic", "authenticated": True}]

    def enumerate_providers():
        nonlocal calls
        with calls_lock:
            calls += 1
            call_no = calls
        if call_no == 1:
            started1.set()
            assert release1.wait(timeout=5)
            return [{"id": "openai", "authenticated": True}]
        started2.set()
        assert release2.wait(timeout=5)
        return list(current)

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(config._list_available_providers_cached, "default")
        assert started1.wait(timeout=5)
        config.invalidate_models_cache()
        # E2 starts while E1 is STILL in flight (the case the old test never
        # exercised — it only started E2 after E1 finished).
        second = executor.submit(config._list_available_providers_cached, "default")
        assert started2.wait(timeout=5)
        # Third caller coalesces onto E2's in-flight refresh; under the old
        # (ABA) cleanup E1 would pop E2's event, waking this waiter into a
        # third probe.
        third = executor.submit(config._list_available_providers_cached, "default")
        release1.set()
        release2.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)
        third_result = third.result(timeout=5)

    # The stale first result must be discarded: EVERY caller sees the fresh
    # post-invalidation enumeration, and exactly two probes ran.
    fresh = [{"id": "anthropic", "authenticated": True}]
    assert first_result == fresh
    assert second_result == fresh
    assert third_result == fresh
    assert calls == 2
    assert config._list_available_providers_cached("default") == fresh
    assert calls == 2


def test_invalidate_during_inflight_exception_preserves_new_owner(monkeypatch):
    """Exception twin: a failed stale owner must not retire the new owner.

    E1 starts probing, invalidation lands, E2 starts probing, then E1 raises.
    The identity-owned exception cleanup must leave E2's in-flight slot
    intact — E2 completes and publishes fresh, a coalesced third caller gets
    the fresh result, and no third probe is launched.
    """
    import api.config as config

    calls = 0
    calls_lock = threading.Lock()
    started1 = threading.Event()
    started2 = threading.Event()
    release1 = threading.Event()
    release2 = threading.Event()
    current = [{"id": "anthropic", "authenticated": True}]

    def enumerate_providers():
        nonlocal calls
        with calls_lock:
            calls += 1
            call_no = calls
        if call_no == 1:
            started1.set()
            assert release1.wait(timeout=5)
            raise RuntimeError("stale probe failed")
        started2.set()
        assert release2.wait(timeout=5)
        return list(current)

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(config._list_available_providers_cached, "default")
        assert started1.wait(timeout=5)
        config.invalidate_models_cache()
        second = executor.submit(config._list_available_providers_cached, "default")
        assert started2.wait(timeout=5)
        third = executor.submit(config._list_available_providers_cached, "default")
        release1.set()
        release2.set()
        assert isinstance(first.exception(timeout=5), RuntimeError)
        fresh = [{"id": "anthropic", "authenticated": True}]
        assert second.result(timeout=5) == fresh
        assert third.result(timeout=5) == fresh

    # E1's failure must not have retired E2's slot (which would let a waiter
    # launch a third probe): exactly two probes ran, fresh result cached.
    assert calls == 2
    assert config._list_available_providers_cached("default") == fresh
    assert calls == 2


def test_enum_cache_clear_happens_inside_outer_lock(monkeypatch):
    """invalidate_models_cache() clears the enum cache while holding the
    outer models lock (atomic two-phase invalidation).

    Regression for the CORE race: if the enum cache were cleared in a
    separate lock phase after the outer lock was released, a concurrent
    catalog rebuild landing in the gap could reuse the pre-credential-change
    enumeration and publish a catalog missing the just-authenticated
    provider.
    """
    import api.config as config

    observed: dict[str, bool] = {}
    orig_clear = config._clear_provider_enum_cache_locked

    def tracking_clear():
        # Called under _PROVIDER_ENUM_CACHE_LOCK; report whether the outer
        # models lock is ALSO held by this thread right now.
        observed["outer_held"] = config._available_models_cache_lock._is_owned()
        return orig_clear()

    monkeypatch.setattr(config, "_clear_provider_enum_cache_locked", tracking_clear)

    def enumerate_providers():
        return [{"id": "openai", "authenticated": True}]

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)
    config.invalidate_models_cache()
    assert observed.get("outer_held") is True


def test_oauth_credential_mutation_routes_through_full_invalidation(monkeypatch):
    """OAuth link/unlink must go through the full model invalidation boundary
    so the provider-enumeration cache is dropped, not just the credential
    pool / providers caches."""
    import api.config as config

    invalidations = []
    orig_invalidate = config.invalidate_models_cache

    def tracking_invalidate():
        invalidations.append(True)
        return orig_invalidate()

    monkeypatch.setattr(config, "invalidate_models_cache", tracking_invalidate)

    def enumerate_providers():
        return [{"id": "anthropic", "authenticated": True}]

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)
    assert config._list_available_providers_cached("default") == [
        {"id": "anthropic", "authenticated": True}
    ]

    from api.oauth import _invalidate_provider_state_caches

    _invalidate_provider_state_caches("anthropic")
    assert invalidations, "OAuth mutation did not route through invalidate_models_cache()"


def test_returned_enumeration_is_isolated_from_mutation(monkeypatch):
    """Callers may mutate the returned list / nested rows without corrupting
    the cached snapshot (mutable-aliasing regression)."""
    import api.config as config

    def enumerate_providers():
        return [{"id": "openai", "authenticated": True, "nested": {"k": [1, 2]}}]

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    got = config._list_available_providers_cached("default")
    assert got == [{"id": "openai", "authenticated": True, "nested": {"k": [1, 2]}}]
    # Mutate the returned snapshot aggressively; the cache must not change.
    got.append({"id": "injected", "authenticated": True})
    got[0]["authenticated"] = False
    got[0]["nested"]["k"].append(99)

    again = config._list_available_providers_cached("default")
    assert again == [{"id": "openai", "authenticated": True, "nested": {"k": [1, 2]}}]
