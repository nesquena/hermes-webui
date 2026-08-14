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
        config._PROVIDER_ENUM_CACHE.clear()
        config._PROVIDER_ENUM_CACHE_INFLIGHT.clear()


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
