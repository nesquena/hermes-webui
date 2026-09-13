"""Regression coverage for bounded full-session resolution."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def _install_lightweight_resolve_stubs(monkeypatch, models) -> None:
    """Keep the tests focused on full sidecar materialization."""
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda _session: False)
    monkeypatch.setattr(models, "_repair_stale_pending", lambda _session: False)
    monkeypatch.setattr(models, "_session_has_pending_journal_retry", lambda _session: False)
    # These gate tests replace Session.load with tiny in-memory stubs rather
    # than opening a real sidecar descriptor. Generation validation belongs to
    # the sidecar/CAS regressions; declare these synthetic loads trusted so this
    # module continues to exercise only single-flight slot behavior.
    monkeypatch.setattr(
        models,
        "_session_has_current_persisted_generation",
        lambda _session, authority=None: True,
    )


def _empty_session(models, sid: str):
    return models.Session(session_id=sid, messages=[])


@pytest.fixture(autouse=True)
def _clear_session_cache():
    import api.models as models

    with models.LOCK:
        models.SESSIONS.clear()
    yield
    with models.LOCK:
        models.SESSIONS.clear()


def test_same_session_cold_load_is_single_flight(monkeypatch):
    import api.models as models

    _install_lightweight_resolve_stubs(monkeypatch, models)
    workers = 4
    start = threading.Barrier(workers)
    state_lock = threading.Lock()
    calls = 0
    active = 0
    peak = 0

    def fake_load(cls, sid):
        nonlocal calls, active, peak
        with state_lock:
            calls += 1
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with state_lock:
            active -= 1
        return _empty_session(models, sid)

    monkeypatch.setattr(models.Session, "load", classmethod(fake_load))

    def resolve():
        start.wait(timeout=2)
        return models.get_session("same-session")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = [future.result(timeout=3) for future in [executor.submit(resolve) for _ in range(workers)]]

    assert calls == 1
    assert peak == 1
    assert {result.session_id for result in results} == {"same-session"}


def test_stale_cached_session_is_single_flight(monkeypatch):
    import api.models as models

    _install_lightweight_resolve_stubs(monkeypatch, models)
    stale = _empty_session(models, "stale-session")
    with models.LOCK:
        models.SESSIONS[stale.session_id] = stale
    monkeypatch.setattr(models, "_cached_session_lags_disk", lambda session: session is stale)
    monkeypatch.setattr(models, "_inactive_cache_tail_needs_disk_check", lambda _session: False)

    workers = 4
    start = threading.Barrier(workers)
    state_lock = threading.Lock()
    calls = 0
    active = 0
    peak = 0

    def fake_load(cls, sid):
        nonlocal calls, active, peak
        with state_lock:
            calls += 1
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with state_lock:
            active -= 1
        return _empty_session(models, sid)

    monkeypatch.setattr(models.Session, "load", classmethod(fake_load))

    def resolve():
        start.wait(timeout=2)
        return models.get_session("stale-session")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = [
            future.result(timeout=3)
            for future in [executor.submit(resolve) for _ in range(workers)]
        ]

    assert calls == 1
    assert peak == 1
    assert all(result is results[0] for result in results)
    assert results[0] is not stale


def test_distinct_cold_loads_have_a_global_concurrency_bound(monkeypatch):
    import api.models as models

    _install_lightweight_resolve_stubs(monkeypatch, models)
    session_ids = ("session-a", "session-b", "session-c", "session-d")
    start = threading.Barrier(len(session_ids))
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def fake_load(cls, sid):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with state_lock:
            active -= 1
        return _empty_session(models, sid)

    monkeypatch.setattr(models.Session, "load", classmethod(fake_load))

    def resolve(sid):
        start.wait(timeout=2)
        return models.get_session(sid)

    with ThreadPoolExecutor(max_workers=len(session_ids)) as executor:
        results = [
            future.result(timeout=3)
            for future in [executor.submit(resolve, sid) for sid in session_ids]
        ]

    assert peak == 2
    assert {result.session_id for result in results} == set(session_ids)


def test_metadata_only_load_bypasses_full_resolve_slots(monkeypatch):
    import api.models as models

    _install_lightweight_resolve_stubs(monkeypatch, models)
    heavy_entered = threading.Barrier(3)
    release_heavy = threading.Event()

    def fake_load(cls, sid):
        heavy_entered.wait(timeout=2)
        assert release_heavy.wait(timeout=2)
        return _empty_session(models, sid)

    def fake_load_metadata_only(cls, sid):
        return _empty_session(models, sid)

    monkeypatch.setattr(models.Session, "load", classmethod(fake_load))
    monkeypatch.setattr(models.Session, "load_metadata_only", classmethod(fake_load_metadata_only))

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(models.get_session, "heavy-a")
        second = executor.submit(models.get_session, "heavy-b")
        heavy_entered.wait(timeout=2)
        metadata = executor.submit(models.get_session, "metadata", True)
        try:
            assert metadata.result(timeout=0.5).session_id == "metadata"
        finally:
            release_heavy.set()
        assert first.result(timeout=2).session_id == "heavy-a"
        assert second.result(timeout=2).session_id == "heavy-b"


def test_fresh_cache_hit_bypasses_full_resolve_slots(monkeypatch):
    import api.models as models

    _install_lightweight_resolve_stubs(monkeypatch, models)
    cached = _empty_session(models, "cached")
    with models.LOCK:
        models.SESSIONS[cached.session_id] = cached
    monkeypatch.setattr(models, "_cached_session_lags_disk", lambda _session: False)
    monkeypatch.setattr(models, "_inactive_cache_tail_needs_disk_check", lambda _session: False)

    heavy_entered = threading.Barrier(3)
    release_heavy = threading.Event()

    def fake_load(cls, sid):
        heavy_entered.wait(timeout=2)
        assert release_heavy.wait(timeout=2)
        return _empty_session(models, sid)

    monkeypatch.setattr(models.Session, "load", classmethod(fake_load))

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(models.get_session, "heavy-a")
        second = executor.submit(models.get_session, "heavy-b")
        heavy_entered.wait(timeout=2)
        cache_hit = executor.submit(models.get_session, "cached")
        try:
            assert cache_hit.result(timeout=0.5) is cached
        finally:
            release_heavy.set()
        first.result(timeout=2)
        second.result(timeout=2)


def test_failed_leader_releases_single_flight_state(monkeypatch):
    import api.models as models

    _install_lightweight_resolve_stubs(monkeypatch, models)
    attempts = 0

    def fake_load(cls, sid):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated sidecar failure")
        return _empty_session(models, sid)

    monkeypatch.setattr(models.Session, "load", classmethod(fake_load))

    with pytest.raises(OSError, match="simulated sidecar failure"):
        models.get_session("retryable")

    recovered = models.get_session("retryable")

    assert recovered.session_id == "retryable"
    assert attempts == 2
    assert models._FULL_SESSION_RESOLVE_INFLIGHT == {}
