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

# ── #7568: in-flight rebuild overlap must be non-authoritative ──────────────


def _warm_catalog(monkeypatch, *, provider_ids):
    """Warm memory cache holding exactly these provider group ids.

    The payload carries the FULL /api/models shape (``_is_valid_models_cache``
    requires active_provider / default_model / configured_model_badges /
    groups) or the warm-cache read is rejected as stale and the branch under
    test is never reached.
    """
    monkeypatch.setattr(
        cfg,
        "_available_models_cache",
        {
            "active_provider": provider_ids[0] if provider_ids else "",
            "default_model": "",
            "configured_model_badges": {},
            "groups": [
                {"provider_id": pid, "models": [], "extra_models": []}
                for pid in provider_ids
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    # Match the runtime source fingerprint exactly, or the warm cache is
    # rejected as stale and the test would silently exercise the minimal
    # catalog fallback instead of the branch under test.
    monkeypatch.setattr(
        cfg,
        "_available_models_cache_source_fingerprint",
        cfg._models_cache_source_fingerprint(),
        raising=False,
    )
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    _pin_cfg_mtime(monkeypatch)


def test_warm_cache_overlapping_rebuild_is_non_authoritative(monkeypatch):
    """#7568 CORE finding 1: a warm snapshot served to a no-wait display
    caller WHILE a rebuild is in flight must be marked non-authoritative.

    The warm cache predates the provider the user just added (the in-flight
    rebuild is the one that will add it). A display caller that repairs the
    persisted ``@copilot:gpt-5.5`` / ``copilot`` pair against this snapshot
    returns ``gpt-5.5`` / ``openai-codex``, and ``_resolveSessionModelFor
    DisplaySoon()`` writes that back into ``S.session.model`` /
    ``model_provider`` — which the next send uses, so the user's choice is
    lost for real.
    """
    _warm_catalog(monkeypatch, provider_ids=["openai"])
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

    result = cfg.get_available_models(
        prefer_cache=True, wait_for_inflight_rebuild=False
    )

    assert result.get("_non_authoritative") is True, (
        "a warm cache served while a rebuild overlaps must be marked "
        "non-authoritative so the display resolver preserves the persisted pair"
    )
    assert result.get("_non_authoritative_reason") == "no_wait_warm_cache_during_rebuild"
    # The catalog content is still the valid warm snapshot — marking changes
    # authority, not content.
    assert [g["provider_id"] for g in result.get("groups", [])] == ["openai"]


def test_warm_cache_without_rebuild_stays_authoritative(monkeypatch):
    """No overlap → the warm snapshot keeps its normal authority.

    Otherwise every ordinary display resolution would stop repairing a stale
    pair, which is the repair the feature exists to perform.
    """
    _warm_catalog(monkeypatch, provider_ids=["openai"])
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)

    result = cfg.get_available_models(
        prefer_cache=True, wait_for_inflight_rebuild=False
    )

    assert result.get("_non_authoritative") is None
    assert [g["provider_id"] for g in result.get("groups", [])] == ["openai"]


def test_warm_cache_overlapping_rebuild_is_authoritative_for_routing(monkeypatch):
    """The routing-authoritative caller (provider repair) must NOT be
    downgraded: it still needs the real catalog to decide the fallback, and
    it is willing to wait for the rebuild."""
    _warm_catalog(monkeypatch, provider_ids=["openai"])
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_cv", _WaitWildcard(cfg._cache_build_cv, blocking=False))

    result = cfg.get_available_models(
        prefer_cache=True, wait_for_inflight_rebuild=True
    )

    assert result.get("_non_authoritative") is None, (
        "wait_for_inflight_rebuild=True declares a routing-authoritative "
        "caller; downgrading it would silently disable the provider repair"
    )


def test_marking_does_not_mutate_the_shared_cache_entry(monkeypatch):
    """The in-memory cache entry must stay clean for authoritative readers.

    Marking is done on a deep copy wherever the marked document could be the
    shared object; here the warm-cache branch marks the object it is about to
    return, so this pins that it was copied first (otherwise a later
    authoritative read would inherit ``_non_authoritative`` from the same
    object identity the module cached).
    """
    warm = {
        "active_provider": "openai",
        "default_model": "",
        "configured_model_badges": {},
        "groups": [{"provider_id": "openai", "models": [], "extra_models": []}],
    }
    _warm_catalog(monkeypatch, provider_ids=["openai"])
    monkeypatch.setattr(cfg, "_available_models_cache", warm, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(
        cfg,
        "_available_models_cache_source_fingerprint",
        cfg._models_cache_source_fingerprint(),
        raising=False,
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

    result = cfg.get_available_models(
        prefer_cache=True, wait_for_inflight_rebuild=False
    )

    assert result.get("_non_authoritative") is True
    assert warm.get("_non_authoritative") is None, (
        "the module-level cache entry was marked in place; an authoritative "
        "reader would now inherit the non-authoritative flag (#7568)"
    )


# ── #7568 CORE re-review: a REAL builder owns the lock across its wait ───────
# The previous lock-contention test parked a bare thread on the RLock, which
# proved only that the reader does not queue on the lock. The maintainer's
# 2026-10-19 re-review asked for the shape that actually reproduces the
# reported stall: a builder that owns ``_available_models_cache_lock`` AND
# blocks inside it on its own condition for the full rebuild budget, with the
# ``prefer_cache`` reader required to return BEFORE that builder finishes.
# The reader must not merely be fast in absolute terms — it must win the race
# against a rebuild that is still holding the lock.


class _FakeRebuildBudget:
    """Own the catalog lock and hold it across a condition wait.

    Mirrors the real bounded rebuild in ``get_available_models``: it takes
    ``_available_models_cache_lock`` and then blocks on ``build_done.wait()``
    for the rebuild budget. A reader that queues on that RLock pays the whole
    budget no matter how the condvar wait is gated (CORE #7568).
    """

    def __init__(self, cfg, budget):
        self._cfg = cfg
        self._budget = budget
        self.finished = threading.Event()
        self.hold_started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        lock = self._cfg._available_models_cache_lock
        cond = getattr(self._cfg, "_available_models_build_done", None)
        lock.acquire()
        try:
            self.hold_started.set()
            if cond is not None:
                with cond:
                    cond.wait(timeout=self._budget)
            else:
                time.sleep(self._budget)
        finally:
            lock.release()
            self.finished.set()

    def join(self, timeout):
        self._thread.join(timeout=timeout)
        return self.finished.is_set()


def test_display_prefer_cache_beats_a_real_builder_holding_the_lock(monkeypatch):
    """CORE: the display reader must return while the rebuild still owns the lock.

    This is the shape the 2026-10-19 re-review asked for: a real builder
    holding ``_available_models_cache_lock`` across its own budget wait, and
    the ``prefer_cache`` reader required to finish BEFORE that builder
    releases. Asserting only "the reader is fast" is not enough — a reader
    that returns in 50ms but only after the builder finished would still
    stall every session-open model resolution behind a real rebuild.
    """
    _cold_caches(monkeypatch)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

    budget = 0.9
    builder = _FakeRebuildBudget(cfg, budget)
    try:
        assert builder.hold_started.wait(timeout=2.0), "builder never took the lock"
        # The builder owns the RLock right now and is blocked inside it.
        assert not builder.finished.is_set(), "builder already released the lock"

        started = time.monotonic()
        result = cfg.get_available_models(
            prefer_cache=True, wait_for_inflight_rebuild=False
        )
        elapsed = time.monotonic() - started

        # The load-bearing assertion: the reader returned while the builder
        # still holds the lock. If the reader queued on the RLock it could
        # not possibly be here yet.
        assert not builder.finished.is_set(), (
            "the display reader only returned AFTER the rebuild finished — it "
            "queued on _available_models_cache_lock for the whole budget (#7568)"
        )
    finally:
        builder.join(timeout=budget + 2.0)

    assert isinstance(result, dict)
    assert elapsed < budget, (
        f"display prefer_cache took {elapsed:.2f}s against a {budget:.2f}s rebuild"
    )
    assert result.get("_non_authoritative") is True, (
        "a resolution that overlapped a real rebuild must be marked "
        "non-authoritative so no caller routes on it"
    )


# ── #7568 re-gate 2026-10-29: lock admission + epoch fence ─────────────────


def _disk_catalog(provider_id: str) -> dict:
    return {
        "active_provider": provider_id,
        "default_model": "stale-default",
        "configured_model_badges": {},
        "groups": [
            {"provider_id": provider_id, "models": [], "extra_models": []}
        ],
    }


def _hold_lock(seconds: float = 0.6):
    release_event = threading.Event()
    holder = threading.Thread(
        target=_hold_lock_briefly,
        args=(cfg._available_models_cache_lock, release_event, seconds),
        daemon=True,
    )
    holder.start()
    time.sleep(0.15)  # let the holder actually own the lock
    return holder, release_event


def test_lock_busy_without_rebuild_keeps_the_authoritative_answer(monkeypatch):
    """should-fix (#7568 re-gate 2026-10-29): a short-lived holder that is NOT
    a rebuild must not push a display reader onto the minimal catalog.

    Every authoritative fast path holds ``_available_models_cache_lock`` while
    it deep-copies the catalog (1 ms at 15 models, 32 ms at 5,000). With a warm
    cache and NO rebuild in flight, those brief holds were indistinguishable
    from a budget-length rebuild, so concurrent readers received a
    non-authoritative minimal catalog in 27-40 of 80 answers. The visible
    damage: an alias echo (``claude`` for ``anthropic``) and legacy pairs left
    unrepaired, because the non-authoritative marker tells the display
    resolver to preserve instead of repair.
    """
    _warm_catalog(monkeypatch, provider_ids=["anthropic"])
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)

    holder, release_event = _hold_lock()
    try:
        result = cfg.get_available_models(
            prefer_cache=True, wait_for_inflight_rebuild=False
        )
    finally:
        release_event.set()
        holder.join(timeout=3.0)

    assert result.get("_non_authoritative") is None, (
        "a busy-but-not-rebuilding lock downgraded an ordinary display "
        "resolution — the reader must queue for the short holder instead"
    )
    assert [g["provider_id"] for g in result.get("groups", [])] == ["anthropic"], (
        "the reader must serve the warm authoritative snapshot, not the "
        "minimal static catalog"
    )


def test_lock_busy_disk_fallback_serves_uninvalidated_snapshot(monkeypatch):
    """Control for the epoch fence: with no invalidation between the unlocked
    disk read and the lock-busy return, the disk snapshot IS the intended
    non-authoritative fallback (same reason the post-lock path would use it).
    """
    _cold_caches(monkeypatch)
    monkeypatch.setattr(
        cfg,
        "_load_models_cache_from_disk",
        lambda: _disk_catalog("disk-provider"),
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

    holder, release_event = _hold_lock()
    try:
        result = cfg.get_available_models(
            prefer_cache=True, wait_for_inflight_rebuild=False
        )
    finally:
        release_event.set()
        holder.join(timeout=3.0)

    assert result.get("_non_authoritative") is True
    assert result.get("_non_authoritative_reason") == "no_wait_lock_busy_disk_cache"
    assert [g["provider_id"] for g in result.get("groups", [])] == ["disk-provider"]


def test_lock_busy_disk_fallback_is_epoch_fenced(monkeypatch):
    """should-fix (#7568 re-gate 2026-10-29): the lock-busy disk fallback must
    honor the same ``disk_epoch == _models_rebuild_seq`` fence as the post-lock
    path.

    Reproduces the invalidation race: the unlockeded disk read captures epoch
    N, an invalidation advances ``_models_rebuild_seq`` before the fallback
    returns, and the lock is busy. Serving those bytes would hand out a
    catalog the invalidation deliberately cleared — the post-lock path fences
    exactly this case, and the lock-busy path must not be a hole in it.
    """
    _cold_caches(monkeypatch)

    def _load_then_invalidate():
        # Simulate invalidation landing AFTER the epoch was captured at the
        # top of get_available_models but BEFORE the lock-busy return.
        monkeypatch.setattr(
            cfg, "_models_rebuild_seq", cfg._models_rebuild_seq + 1, raising=False
        )
        return _disk_catalog("invalidated-disk-provider")

    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", _load_then_invalidate)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

    holder, release_event = _hold_lock()
    try:
        result = cfg.get_available_models(
            prefer_cache=True, wait_for_inflight_rebuild=False
        )
    finally:
        release_event.set()
        holder.join(timeout=3.0)

    assert result.get("_non_authoritative_reason") != "no_wait_lock_busy_disk_cache", (
        "the lock-busy path served a disk snapshot the invalidation had "
        "already advanced past — it must apply the epoch fence"
    )
    assert result.get("_non_authoritative_reason") == "no_wait_minimal_static_catalog"
    assert [
        g.get("provider_id") for g in result.get("groups", [])
    ] != ["invalidated-disk-provider"], (
        "the invalidated disk default leaked into the display answer"
    )


def test_lock_busy_recheck_falls_back_when_queued_builder_starts(monkeypatch):
    """P1: a short holder can hand the lock to a queued builder, not the reader.

    The reader's non-blocking acquire fails while no rebuild is in flight. If it
    then makes one unbounded blocking acquire, a queued builder can win the lock,
    set ``_cache_build_in_progress`` and hold it for the whole rebuild. The
    no-wait display caller must recheck the flag after a bounded acquire and
    serve the lock-free fallback instead of waiting for that builder.
    """
    _warm_catalog(monkeypatch, provider_ids=["anthropic"])
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)

    raw_lock = cfg._available_models_cache_lock
    reader_blocking = threading.Event()
    builder_queued = threading.Event()
    builder_holding = threading.Event()
    release_builder = threading.Event()
    reader_done = threading.Event()
    observed = {}
    errors = []

    class _QueuedBuilderSchedulingLock:
        """Force the exact reader → queued-builder interleaving on a real RLock."""

        def __init__(self, target):
            self._target = target
            self._reader_first_blocking = True

        def acquire(self, blocking=True, *args, **kwargs):
            name = threading.current_thread().name
            if name == "display-reader" and blocking and self._reader_first_blocking:
                self._reader_first_blocking = False
                reader_blocking.set()
                if not builder_queued.wait(2):
                    raise RuntimeError("builder did not queue")
                if not builder_holding.wait(2):
                    raise RuntimeError("builder did not start its rebuild")
                return self._target.acquire(blocking, *args, **kwargs)
            if name == "queued-builder":
                builder_queued.set()
            return self._target.acquire(blocking, *args, **kwargs)

        def release(self):
            return self._target.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc_info):
            self.release()

    wrapper = _QueuedBuilderSchedulingLock(raw_lock)
    monkeypatch.setattr(cfg, "_available_models_cache_lock", wrapper, raising=False)

    def _builder():
        try:
            wrapper.acquire()
            try:
                cfg._cache_build_in_progress = True
                builder_holding.set()
                release_builder.wait(10)
                cfg._cache_build_in_progress = False
            finally:
                wrapper.release()
        except BaseException as exc:  # surface thread failures in the test thread
            errors.append(exc)

    def _reader():
        try:
            observed["result"] = cfg.get_available_models(
                prefer_cache=True, wait_for_inflight_rebuild=False
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            reader_done.set()

    raw_lock.acquire()
    holder_owned = True
    reader = threading.Thread(target=_reader, name="display-reader", daemon=True)
    builder = threading.Thread(target=_builder, name="queued-builder", daemon=True)
    try:
        reader.start()
        assert reader_blocking.wait(2), "reader never reached blocking admission"
        builder.start()
        assert builder_queued.wait(2), "builder never queued for the lock"
        raw_lock.release()
        holder_owned = False
        assert builder_holding.wait(2), "builder never acquired the lock"

        # The fixed reader must finish while the builder still owns the lock
        # and keeps the rebuild flag set. Waiting on its completion (rather than
        # sleeping a fixed interval) also lets a busy CI worker schedule the
        # reader late without changing the expected result. On the old code the
        # wait expires; releasing the builder then cleans up and preserves the
        # discriminating assertion instead of hanging the suite.
        completed_before_release = reader_done.wait(timeout=5.0)
        release_builder.set()
        reader.join(3)
        builder.join(3)
    finally:
        release_builder.set()
        if holder_owned:
            raw_lock.release()
        reader.join(3)
        builder.join(3)

    assert not errors, errors
    assert not reader.is_alive(), "display reader remained blocked on the builder"
    assert not builder.is_alive(), "builder did not finish"
    assert completed_before_release, (
        "display reader did not finish while the queued builder still held "
        "the lock with the rebuild flag set"
    )
    result = observed["result"]
    assert result.get("_non_authoritative") is True, (
        "the reader waited for the queued builder and returned the warm "
        "authoritative catalog instead of taking the no-wait fallback"
    )
    assert result.get("_non_authoritative_reason") == "no_wait_minimal_static_catalog"
