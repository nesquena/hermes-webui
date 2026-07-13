"""Regression tests — session-open must never block on a live model-catalog probe.

Proven root cause (live thread-stack + Chrome DevTools waterfall on modern
hardware: /api/session 7.09s, model_resolve=7073ms; /api/models?freshness=
session_visit 4.0s; a ~60s sidebar because every queued request stacks behind
the blocked catalog thread under ThreadingHTTPServer + the browser's 6-conn
per-host cap):

    /api/session?resolve_model=1
      → _resolve_effective_session_model_for_display
        → _resolve_compatible_session_model_state
          → get_available_models(prefer_cache=True)   # docstring: "never blocks"
            → BLOCKED up to 60s on _cache_build_cv.wait_for(...)   # contract broken

    /api/models?freshness=session_visit
      → get_available_models_for_session_visit
        → get_available_models(force_refresh=True)
          → _build_available_models_uncached → _read_live_provider_model_ids
            → hermes_cli.models._fetch_anthropic_models → urllib HTTPS  # BLOCKED

Two contract fixes, asserted here as invariants (not snapshots):

1. prefer_cache=True must resolve WITHOUT waiting — even while another thread
   holds _available_models_cache_lock across an in-flight cold rebuild. It
   serves warm-memory (non-blocking lock) → disk → stale-disk → minimal-static.

2. get_available_models_for_session_visit is stale-while-revalidate: with ANY
   shape-valid cache it returns immediately and refreshes on a detached daemon.
   Only a true cold boot (no cache at all) may fall back to the *bounded*
   rebuild, which is itself capped at _LIVE_REBUILD_BUDGET_SECONDS.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _catalog(default_model: str = "anthropic/claude-sonnet-4") -> dict:
    return {
        "active_provider": "anthropic",
        "default_model": default_model,
        "configured_model_badges": {},
        "groups": [],
        "aliases": {},
    }


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Fix 1 — prefer_cache must never block, even behind an in-flight rebuild
# ---------------------------------------------------------------------------


def test_prefer_cache_does_not_block_when_rebuild_holds_lock(monkeypatch):
    """The core contract violation: a concurrent cold rebuild holds
    _available_models_cache_lock across its bounded ``build_done`` wait AND has
    _cache_build_in_progress set. Before the fix, prefer_cache fell through to
    the ``should_wait`` condition-variable wait and the blocking lock acquire,
    stalling session-open for seconds on a catalog already cached on disk. It
    must now resolve from disk immediately without touching either.
    """
    from api import config as cfg

    cfg.invalidate_models_cache()
    monkeypatch.setattr(
        cfg, "_load_models_cache_from_disk", lambda: _catalog(), raising=True
    )

    # prefer_cache must never invoke the live rebuild seam.
    monkeypatch.setattr(
        cfg,
        "_invoke_models_rebuild",
        lambda _b: (_ for _ in ()).throw(
            AssertionError("prefer_cache triggered the live rebuild seam")
        ),
        raising=True,
    )

    holding = threading.Event()
    release = threading.Event()

    def _holder():
        # Mirror the real bounded-rebuild critical section: hold the cache lock
        # AND mark a build in progress across a long wait.
        with cfg._available_models_cache_lock:
            with cfg._cache_build_cv:
                cfg._cache_build_in_progress = True
            holding.set()
            release.wait(timeout=5.0)
            with cfg._cache_build_cv:
                cfg._cache_build_in_progress = False
                cfg._cache_build_cv.notify_all()

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    try:
        assert holding.wait(timeout=2.0), "holder thread never acquired the lock"

        t0 = time.monotonic()
        result = cfg.get_available_models(prefer_cache=True)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.5, (
            f"prefer_cache blocked {elapsed:.2f}s while a rebuild held the lock — "
            f"it must resolve from cache without waiting"
        )
        assert result["default_model"] == "anthropic/claude-sonnet-4"
    finally:
        release.set()
        t.join(timeout=2.0)
        # Leave the module global clean for other tests in the process.
        with cfg._cache_build_cv:
            cfg._cache_build_in_progress = False
            cfg._cache_build_cv.notify_all()


def test_prefer_cache_serves_stale_disk_when_no_fresh_cache(monkeypatch):
    """prefer_cache with no fresh memory/disk cache but a shape-valid STALE disk
    cache must serve the stale payload rather than blocking or probing.
    """
    from api import config as cfg

    cfg.invalidate_models_cache()
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None, raising=True)
    monkeypatch.setattr(
        cfg,
        "_load_stale_models_cache_from_disk",
        lambda **_kw: _catalog("anthropic/claude-opus-4"),
        raising=True,
    )
    monkeypatch.setattr(
        cfg,
        "_invoke_models_rebuild",
        lambda _b: (_ for _ in ()).throw(
            AssertionError("prefer_cache triggered the live rebuild seam")
        ),
        raising=True,
    )

    result = cfg.get_available_models(prefer_cache=True)
    assert result["default_model"] == "anthropic/claude-opus-4"


# ---------------------------------------------------------------------------
# Fix 2 — session_visit is stale-while-revalidate (serve now, refresh later)
# ---------------------------------------------------------------------------


def test_session_visit_serves_stale_without_blocking(monkeypatch):
    """A stale (past-freshness-horizon) cache must be served immediately with a
    background refresh kicked off — NEVER a synchronous live rebuild on the
    request thread.
    """
    from api import config as cfg

    cfg.invalidate_models_cache()
    # Force the "stale or missing" branch: age check reports no fresh file.
    monkeypatch.setattr(
        cfg, "_models_cache_file_age_seconds", lambda *a, **k: None, raising=True
    )
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None, raising=True)
    monkeypatch.setattr(
        cfg, "_load_stale_models_cache_from_disk", lambda **_kw: _catalog(), raising=True
    )

    spawned = {"n": 0}

    def _spy_spawn():
        spawned["n"] += 1
        return True

    monkeypatch.setattr(
        cfg, "_spawn_session_visit_background_refresh", _spy_spawn, raising=True
    )

    # If the stale path ran a synchronous live rebuild, this fires.
    monkeypatch.setattr(
        cfg,
        "_invoke_models_rebuild",
        lambda _b: (_ for _ in ()).throw(
            AssertionError("session_visit ran a synchronous rebuild on a stale cache")
        ),
        raising=True,
    )

    t0 = time.monotonic()
    result = cfg.get_available_models_for_session_visit()
    elapsed = time.monotonic() - t0

    assert spawned["n"] == 1, "stale cache must trigger exactly one background refresh"
    assert elapsed < 0.5, f"session_visit blocked {elapsed:.2f}s serving a stale cache"
    assert result["default_model"] == "anthropic/claude-sonnet-4"


def test_session_visit_cold_boot_uses_bounded_rebuild(monkeypatch):
    """True cold boot — NO cache on disk at all — is the only branch allowed to
    wait, and even then it uses the *bounded* rebuild (capped at
    _LIVE_REBUILD_BUDGET_SECONDS), never an unbounded blocking probe.
    """
    from api import config as cfg

    cfg.invalidate_models_cache()
    monkeypatch.setattr(
        cfg, "_models_cache_file_age_seconds", lambda *a, **k: None, raising=True
    )
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None, raising=True)
    monkeypatch.setattr(
        cfg, "_load_stale_models_cache_from_disk", lambda **_kw: None, raising=True
    )
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.4, raising=True)

    rebuilt = {"n": 0}

    def _slow_rebuild(_builder):
        rebuilt["n"] += 1
        time.sleep(0.8)  # 2x budget — stands in for a hung provider probe
        return _catalog()

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_rebuild, raising=True)

    t0 = time.monotonic()
    result = cfg.get_available_models_for_session_visit()
    elapsed = time.monotonic() - t0

    assert rebuilt["n"] == 1, "cold boot with no cache must attempt the bounded rebuild"
    assert elapsed < 2.0, (
        f"cold-boot rebuild was not bounded ({elapsed:.2f}s) — the "
        f"{cfg._LIVE_REBUILD_BUDGET_SECONDS}s budget did not apply"
    )
    # Structurally valid, usable catalog regardless of which fallback served it.
    assert isinstance(result, dict)
    for k in ("active_provider", "default_model", "configured_model_badges", "groups"):
        assert k in result, f"cold-boot fallback catalog missing {k!r}"


# ---------------------------------------------------------------------------
# Background-refresh helper — coalescing + guard cleanup
# ---------------------------------------------------------------------------


def test_background_refresh_coalesces_concurrent_spawns(monkeypatch):
    """A burst of session-opens must spawn ONE refresh daemon, not N. The
    in-flight guard clears once the refresh finishes so the next horizon can
    refresh again.
    """
    from api import config as cfg

    cfg._session_visit_refresh_in_flight.clear()

    release = threading.Event()
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def _blocking_force(*_a, **_k):
        with calls_lock:
            calls["n"] += 1
        release.wait(timeout=5.0)
        return _catalog()

    # The worker body calls get_available_models(force_refresh=True).
    monkeypatch.setattr(cfg, "get_available_models", _blocking_force, raising=True)

    try:
        first = cfg._spawn_session_visit_background_refresh()
        # Let the worker enter and latch the guard.
        assert _wait_until(lambda: calls["n"] == 1, timeout=2.0), "worker never started"
        second = cfg._spawn_session_visit_background_refresh()
        third = cfg._spawn_session_visit_background_refresh()

        assert first is True, "first spawn should win the claim"
        assert second is False and third is False, (
            "concurrent spawns must coalesce onto the in-flight refresh"
        )
        assert calls["n"] == 1, "only the winning spawn may run the live refresh"
    finally:
        release.set()

    assert _wait_until(
        lambda: not cfg._session_visit_refresh_in_flight, timeout=2.0
    ), "in-flight guard must clear after the refresh completes"


def test_background_refresh_reuses_get_available_models_force_refresh(monkeypatch):
    """The background refresh must reuse the single source of truth
    (get_available_models(force_refresh=True)), not a bespoke probe path.
    """
    from api import config as cfg

    cfg._session_visit_refresh_in_flight.clear()

    seen = {"force_refresh": None}
    done = threading.Event()

    def _capture(*_a, **kwargs):
        seen["force_refresh"] = kwargs.get("force_refresh")
        done.set()
        return _catalog()

    monkeypatch.setattr(cfg, "get_available_models", _capture, raising=True)

    assert cfg._spawn_session_visit_background_refresh() is True
    assert done.wait(timeout=2.0), "background worker never called get_available_models"
    assert seen["force_refresh"] is True, (
        "background refresh must call get_available_models(force_refresh=True)"
    )
    assert _wait_until(
        lambda: not cfg._session_visit_refresh_in_flight, timeout=2.0
    )


# ---------------------------------------------------------------------------
# Source-grep wiring guards (match the repo's existing guard-test style)
# ---------------------------------------------------------------------------


def test_prefer_cache_resolves_before_blocking_lock():
    """White-box guard: the prefer_cache fast path must be positioned BEFORE the
    ``with _available_models_cache_lock:`` blocking acquire, so a refactor can't
    silently reintroduce the blocking behaviour.
    """
    src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    i = src.find("def get_available_models(")
    assert i != -1
    j = src.find("\ndef ", i + 1)
    body = src[i:j]
    pc = body.find("if prefer_cache:")
    lock = body.find("with _available_models_cache_lock:")
    assert pc != -1 and lock != -1
    assert pc < lock, (
        "the prefer_cache fast path must resolve BEFORE the blocking "
        "_available_models_cache_lock acquire"
    )
    assert "acquire(blocking=False)" in body[pc:lock], (
        "prefer_cache must read the memory cache under a NON-BLOCKING lock acquire"
    )


def test_session_visit_stale_path_spawns_background_refresh():
    """White-box guard: the session-visit stale branch must serve the cached
    payload and spawn a background refresh — never a synchronous force_refresh.
    """
    src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    i = src.find("def get_available_models_for_session_visit(")
    assert i != -1
    j = src.find("\ndef ", i + 1)
    body = src[i:j]
    assert "_spawn_session_visit_background_refresh()" in body, (
        "session_visit must spawn a background refresh on the stale path"
    )
    # The stale branch returns before the cold-boot rebuild. Anchor on the
    # code-only stage markers (comments also mention force_refresh, so match
    # the _mark() labels which appear solely in executable lines).
    spawn = body.find('_mark("stale_served_background_refresh")')
    cold = body.find('_mark("cold_boot_bounded_rebuild_start")')
    assert spawn != -1 and cold != -1
    assert spawn < cold, (
        "the stale-serve + background-refresh must precede the cold-boot "
        "bounded rebuild"
    )


def test_spawn_helper_exists_and_uses_detached_worker():
    from api import config as cfg

    assert hasattr(cfg, "_spawn_session_visit_background_refresh")
    assert hasattr(cfg, "_session_visit_refresh_in_flight")
    src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    i = src.find("def _spawn_session_visit_background_refresh(")
    j = src.find("\ndef ", i + 1)
    body = src[i:j]
    # Profile isolation on the detached worker (#3957 pattern).
    assert "profile_scope_for_detached_worker" in body
    assert "daemon=True" in body
