"""Regression: prefer_cache resolutions must not wait on an in-flight rebuild.

The ``GET /api/session?...&resolve_model=1`` display path relies on
``get_available_models(prefer_cache=True, wait_for_inflight_rebuild=False)``
being non-blocking ("serve the warm/disk cache or a network-free minimal
catalog; never run or wait for the live provider probe"). The shared wait
gate previously made every such call queue behind an in-flight rebuild for
up to the rebuild budget (observed 4447-4843ms ==
``_LIVE_REBUILD_BUDGET_SECONDS``) — and, worse, even the wait-skip still
parked on ``_available_models_cache_lock`` while the bounded builder held it
across ``build_done.wait()``.

The wait is pinned with a deterministic tripwire: any attempt to wait on
the rebuild condition fails immediately, instead of relying on wall-clock
assertions that a loaded CI box can turn into flaky scheduling failures.

The routing-side contract is pinned too (CORE re-review): the foreign-
session provider repair keeps ``wait_for_inflight_rebuild=True`` and
MUST still join an in-flight rebuild so a poisoned session is never routed
to a stale backend. The mixed-flag contract (``prefer_cache`` +
``force_refresh``) is rejected at entry.
"""

import threading
import time

import pytest

import api.config as cfg


class _WaitWildcard:
    """Proxy a Condition.

    ``wait_for`` either raises (display path: waiting is forbidden) or
    records + pretends success (repair path: waiting is mandatory), depending
    on the ``blocking`` flag.
    """

    def __init__(self, target, *, blocking=True):
        self._target = target
        self._blocking = blocking
        self.wait_calls = []

    def wait_for(self, predicate, timeout=None):
        self.wait_calls.append(timeout)
        if self._blocking:
            raise AssertionError(
                "display prefer_cache resolution attempted to wait on the "
                "catalog rebuild"
            )
        if callable(predicate):
            try:
                predicate()
            except Exception:
                pass
        return True

    def __enter__(self):
        return self._target.__enter__()

    def __exit__(self, *exc_info):
        return self._target.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._target, name)


def _pin_cfg_mtime(monkeypatch):
    """Avoid a real config reload inside the resolver under test."""
    try:
        monkeypatch.setattr(
            cfg, "_cfg_mtime", cfg._get_config_path().stat().st_mtime, raising=False
        )
    except Exception:
        pass


def _cold_caches(monkeypatch):
    """Cold memory/disk caches so the prefer_cache fall-through is exercised."""
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    _pin_cfg_mtime(monkeypatch)


def test_display_prefer_cache_does_not_wait_on_inflight_rebuild(monkeypatch):
    _cold_caches(monkeypatch)
    # A rebuild is in progress and will NOT finish during the call.
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_cv", _WaitWildcard(cfg._cache_build_cv))

    result = cfg.get_available_models(
        prefer_cache=True, wait_for_inflight_rebuild=False
    )

    assert isinstance(result, dict)


def test_display_prefer_cache_skips_lock_held_by_rebuild(monkeypatch):
    """CORE: display resolution must not queue behind the rebuild RLock.

    The bounded rebuild path holds ``_available_models_cache_lock`` while it
    blocks on ``build_done.wait()``. A non-waiting reader used to resolve
    into that same RLock and pay the whole budget anyway. It must instead
    fall back to the lock-free snapshot immediately.
    """
    _cold_caches(monkeypatch)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

    release_event = threading.Event()
    holder = threading.Thread(
        target=_hold_lock_briefly,
        args=(cfg._available_models_cache_lock, release_event, 0.5),
        daemon=True,
    )
    holder.start()
    try:
        # Give the holder a moment to actually own the lock.
        time.sleep(0.15)
        started = time.monotonic()
        result = cfg.get_available_models(
            prefer_cache=True, wait_for_inflight_rebuild=False
        )
        elapsed = time.monotonic() - started
    finally:
        release_event.set()
        holder.join(timeout=2.0)

    assert isinstance(result, dict)
    assert (
        elapsed < 0.4
    ), f"display prefer_cache queued on the rebuild lock for {elapsed:.2f}s"


def test_repair_caller_still_waits_on_inflight_rebuild(monkeypatch):
    """CORE: routing-authoritative prefer_cache must JOIN the rebuild.

    The foreign-session provider repair uses
    ``get_available_models(prefer_cache=True, wait_for_inflight_rebuild=True)``
    and MUST wait for the authoritative catalog. Skipping this wait can
    send a chat turn to a stale backend while the rebuild is in flight.
    """
    _cold_caches(monkeypatch)
    # Rebuild in progress; simulate it finishing quickly (non-blocking
    # recording wait wrapper).
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)
    cv = _WaitWildcard(cfg._cache_build_cv, blocking=False)
    monkeypatch.setattr(cfg, "_cache_build_cv", cv)

    result = cfg.get_available_models(
        prefer_cache=True, wait_for_inflight_rebuild=True
    )

    assert isinstance(result, dict)
    assert cv.wait_calls, (
        "routing-authoritative prefer_cache did not wait for the in-flight "
        "rebuild"
    )


def test_mixed_prefer_cache_and_force_refresh_is_rejected(monkeypatch):
    """The contradictory flag combination must fail fast at entry."""
    _cold_caches(monkeypatch)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_cv", _WaitWildcard(cfg._cache_build_cv))

    with pytest.raises(ValueError, match="mutually exclusive"):
        cfg.get_available_models(prefer_cache=True, force_refresh=True)


def _hold_lock_briefly(lock, release_event, seconds):
    """Own ``lock`` for up to ``seconds+1``, waiting for the release signal."""
    lock.acquire()
    try:
        release_event.wait(timeout=seconds + 1.0)
    finally:
        lock.release()