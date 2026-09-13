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

    # Deterministic instead of a sleep(0.05) guess at scheduler timing
    # (#7007 round 6 audit): wait until all 8 workers have actually ENTERED
    # the cached lookup. Because the owner is still blocked on `release` at
    # that point, the cache cannot be populated yet, so every entered
    # follower must resolve to the in-flight event — which is exactly the
    # coalescing this test asserts. With a fixed sleep, a slow scheduler
    # could let followers arrive only after the owner had already returned
    # and filled the cache, so `calls == 1` would hold vacuously without any
    # coalescing having happened.
    entered = threading.Semaphore(0)

    def _enter_and_lookup():
        entered.release()
        return config._list_available_providers_cached("default")

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_enter_and_lookup) for _ in range(8)]
        assert started.wait(timeout=5)
        for _ in range(8):
            assert entered.acquire(timeout=5), "not every worker entered the lookup"
        assert calls == 1, "the owner must still be the only prober at this point"
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

    # Also verify the warm-hit path deepcopies (mutate hit copy, not just cold miss)
    hit = config._list_available_providers_cached("default")
    hit.append({"id": "hit-injected", "authenticated": True})
    hit[0]["nested"]["k"].append(100)
    hit_again = config._list_available_providers_cached("default")
    assert hit_again == [{"id": "openai", "authenticated": True, "nested": {"k": [1, 2]}}]


def test_invalidate_provider_models_cache_drops_provider_enum_cache(monkeypatch):
    """invalidate_provider_models_cache() (the POST /api/models/refresh path
    hit right after a provider is authenticated) must clear the provider-enum
    cache and bump the generation exactly like invalidate_models_cache() does
    — otherwise a just-authenticated provider stays missing from the picker
    for up to the 120s TTL, and a detached rebuild in flight when the auth
    happened can't detect the invalidation and publishes a stale catalog."""
    import api.config as config

    def enumerate_providers():
        return [{"id": "openai", "authenticated": True}]

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)

    assert config._list_available_providers_cached("default") == [
        {"id": "openai", "authenticated": True}
    ]
    generation_before = config._available_models_cache_generation

    config.invalidate_provider_models_cache("anthropic")

    assert config._available_models_cache_generation > generation_before
    with config._PROVIDER_ENUM_CACHE_LOCK:
        assert "default" not in config._PROVIDER_ENUM_CACHE
    assert not config._cache_build_in_progress


def test_follower_fails_open_after_deadline_instead_of_waiting_forever(monkeypatch):
    """A follower must not loop on a hung owner's 30s wait forever: past its
    overall deadline it fails open with the last known (even if stale)
    enumeration rather than blocking indefinitely.

    Round 5: the owner installs itself into `_PROVIDER_ENUM_CACHE_INFLIGHT`
    (while holding `_PROVIDER_ENUM_CACHE_LOCK`) BEFORE calling the probe —
    so entry into the fake probe below is itself proof the owner is already
    installed. Signal that from inside the probe instead of a bare
    `sleep(0.05)` guess at scheduler timing.
    """
    import api.config as config

    hang = threading.Event()
    owner_probing = threading.Event()

    def enumerate_providers():
        # Owner never returns — simulates a probe that hangs completely.
        # Reaching this line proves the owner already installed itself as
        # inflight (that happens before the probe is ever called).
        owner_probing.set()
        hang.wait(timeout=10)
        return [{"id": "should-not-be-seen", "authenticated": True}]

    _install_fake_models(monkeypatch, enumerate_providers)
    _clear_cache(config)
    # Prime a stale-but-present entry so the fallback has something to return.
    with config._PROVIDER_ENUM_CACHE_LOCK:
        config._PROVIDER_ENUM_CACHE["default"] = (
            time.monotonic() - config._PROVIDER_ENUM_CACHE_TTL_SECONDS - 1,
            [{"id": "stale-openai", "authenticated": True}],
        )
    monkeypatch.setattr(config, "_PROVIDER_ENUM_CACHE_FOLLOWER_DEADLINE_SECONDS", 0.2)

    owner_thread = threading.Thread(
        target=lambda: config._list_available_providers_cached("default"),
        daemon=True,
    )
    owner_thread.start()
    assert owner_probing.wait(timeout=5), "owner never reached the probe — not installed as inflight"

    started = time.monotonic()
    result = config._list_available_providers_cached("default")
    elapsed = time.monotonic() - started

    assert result == [{"id": "stale-openai", "authenticated": True}]
    assert elapsed < 5.0, "follower waited far past its configured deadline"

    hang.set()
    owner_thread.join(timeout=5)
