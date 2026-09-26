"""Regression coverage for #7723 session-visit cache mtime semantics.

The session-visit freshness window (``_SESSION_VISIT_MODELS_FRESHNESS_SECONDS``)
is decided by ``_models_cache_file_age_seconds()`` — the file's mtime on disk.
The on-disk mtime is written **only** by ``_save_models_cache_to_disk()``
(called from a successful live rebuild), so it records "last live rebuild",
not "last read". This file guards:

  1. The buggy ``os.utime`` restamp on the disk-hit branch is GONE
     (regression test for the original PR's CORE finding: the restamp
     made mtime record "last read" and defeated the 300 s horizon
     indefinitely on multi-profile installs).
  2. Stale-while-revalidate, per profile: when the on-disk mtime crosses
     the 300 s horizon, the foreground returns the stale disk catalog
     immediately and fires a coalesced per-profile background
     ``force_refresh``. The rebuild goes through the normal
     ``_save_models_cache_to_disk`` path — so the mtime advances as a
     side effect of a real rebuild, not a read.
  3. The dual-profile alternation bug: two profiles alternating visits
     every 100 s (well under the 300 s horizon) used to pay 0 rebuilds
     with the os.utime restamp; under SWR each profile must rebuild
     exactly once across the horizon crossing.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _catalog(label: str) -> dict:
    return {
        "active_provider": "openai",
        "default_model": label,
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": label, "label": label, "supports_fast_tier": False}],
            }
        ],
        "aliases": {},
    }


def _reset_models_memory_cache(monkeypatch):
    import api.config as cfg

    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", None, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(cfg, "_session_visit_rebuild_threads", {}, raising=False)
    monkeypatch.setattr(cfg, "_session_visit_rebuild_lock", threading.Lock(), raising=False)


def _wait_for_session_visit_rebuild(monkeypatch, timeout: float = 5.0):
    """Join any background session-visit SWR threads started by this test.

    The session-visit stale-while-revalidate path launched by
    ``_maybe_start_session_visit_background_rebuild`` is fire-and-forget on a
    daemon thread. Tests that need the background rebuild to land before
    asserting on the in-memory cache call this to deterministically wait it
    out. Raises if any tracked thread does not finish within ``timeout``.
    """
    import api.config as cfg

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with cfg._session_visit_rebuild_lock:
            threads = list(cfg._session_visit_rebuild_threads.values())
        if not threads:
            return
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if thread.is_alive():
                raise AssertionError(
                    f"session-visit background rebuild thread {thread.name!r} "
                    f"did not finish within {timeout}s"
                )


# ── 1. mtime is NOT restamped on a fresh disk hit (regression for the CORE finding)


def test_disk_hit_does_not_restamp_mtime(tmp_path, monkeypatch):
    """A fresh session-visit disk hit must NOT advance the on-disk mtime.
    The previous fix attempted this with ``os.utime(cache_path, None)`` on
    the hit branch, which broke the per-profile 300 s horizon (mtime
    recorded "last read", not "last live rebuild"). Under the SWR fix the
    mtime only moves as a side effect of ``_save_models_cache_to_disk``
    running after a real rebuild.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    disk_catalog = _catalog("cached-model")
    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    planted_mtime = time.time() - 60.0
    os.utime(cache_path, (planted_mtime, planted_mtime))
    planted_stat = cache_path.stat()
    assert abs(planted_stat.st_mtime - planted_mtime) < 0.01

    rebuild_calls: list[dict] = []

    def _unexpected_live_rebuild(**kwargs):
        rebuild_calls.append(kwargs)
        raise AssertionError("fresh session-visit cache must not run a live rebuild")

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: disk_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "get_available_models", _unexpected_live_rebuild)

    result = cfg.get_available_models_for_session_visit()
    assert result == disk_catalog

    # The mtime must NOT have been touched — the SWR fix never reads
    # ``cache_path.stat().st_mtime`` in a way that writes it back, and
    # the ``os.utime(restamp)`` from the old (rejected) fix is gone.
    after_stat = cache_path.stat()
    assert abs(after_stat.st_mtime - planted_mtime) < 0.01, (
        f"on-disk mtime must be untouched by a session-visit disk hit, "
        f"got delta {after_stat.st_mtime - planted_mtime:.3f}s"
    )
    assert rebuild_calls == [], "fresh session-visit hit must not trigger a rebuild"


# ── 2. Repeated hits within the horizon do not trigger a rebuild


def test_repeated_hits_within_horizon_do_not_rebuild(tmp_path, monkeypatch):
    """Three back-to-back hits within the 300 s horizon (planted mtime just
    under the cliff) must not trigger a foreground or background rebuild
    — the in-memory cache is warm after the first hit and short-circuits
    the rest, the disk mtime is NOT restamped, and the disk hit returns
    a copy of the payload.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    disk_catalog = _catalog("sustained-hits")
    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    planted = time.time() - 290.0
    os.utime(cache_path, (planted, planted))

    rebuild_calls: list[dict] = []

    def _rebuild(**kwargs):
        rebuild_calls.append(kwargs)
        return disk_catalog

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: disk_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "get_available_models", _rebuild)

    for _ in range(3):
        time.sleep(0.05)
        result = cfg.get_available_models_for_session_visit()
        assert result == disk_catalog

    _wait_for_session_visit_rebuild(monkeypatch)
    assert rebuild_calls == [], (
        f"no live rebuild should have run across 3 back-to-back in-horizon "
        f"hits, but got {len(rebuild_calls)} call(s): {rebuild_calls}"
    )
    # The mtime is still the 290-s-old planted value (no os.utime restamp).
    final_mtime = cache_path.stat().st_mtime
    age_after = time.time() - final_mtime
    assert age_after > 200.0, (
        f"on-disk mtime must NOT be re-stamped by a disk hit; "
        f"got age={age_after:.3f}s after 3 hits"
    )


# ── 3. The CORE scenario: dual profile alternation across the 300 s horizon
#       (this is the exact bug the reviewer demonstrated in the review)


def test_dual_profile_alternation_triggers_per_profile_rebuild(tmp_path, monkeypatch):
    """Two profiles alternating visits every 100 s over a 1200 s window
    (6 visits per profile, 12 total) cross the 300 s session-visit
    horizon 4 times per profile. With the rejected ``os.utime`` restamp
    fix, the restamp-on-hit would have kept the mtime fresh indefinitely
    → 0 rebuilds per profile, both returning the pre-horizon catalog.
    Under SWR, each profile must trigger a rebuild every time it crosses
    the horizon.

    A monkeypatched clock is used so the test runs in milliseconds, not
    minutes. The test asserts:

      1. Every horizon crossing actually launches a background rebuild
         (tracked via ``_session_visit_rebuild_threads`` entries, not
         via the rebuild mock's mutable shared state — the rebuild mock
         can't always determine which profile it was started for because
         ``get_active_profile_name()`` is not re-resolved on the worker
         thread, so the foreground's mutable profile pointer may have
         moved on by the time the worker fires).
      2. The mtime ``_models_cache_file_age_seconds`` reports is the
         **planted** mtime (we never call ``os.utime`` to advance it
         from a read), so a 100-s-old file still shows up as 100 s old
         on the next visit.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)

    profile_a_path = tmp_path / "models_cache.profile_a.json"
    profile_b_path = tmp_path / "models_cache.profile_b.json"
    profile_a_path.write_text("{}", encoding="utf-8")
    profile_b_path.write_text("{}", encoding="utf-8")
    planted_mtime = time.time() - 100.0
    for p in (profile_a_path, profile_b_path):
        os.utime(p, (planted_mtime, planted_mtime))

    # A simple disk catalog; we don't care about content for this test
    # beyond the foreground returning something shape-valid.
    disk_catalog = _catalog("disk")
    rebuilt_catalog_a = _catalog("rebuilt-a")

    simulated_now = [planted_mtime + 100.0]
    # Track which profile the foreground was last requesting.
    _profile_call = [0]
    _profile_paths = [profile_a_path, profile_b_path]

    def _path_for_profile():
        return _profile_paths[_profile_call[0]]

    def _load_for_profile():
        return disk_catalog

    def _rebuild_for_profile(**kwargs):
        # ``force_refresh=True`` rebuilds just return a single catalog;
        # the foreground mock (which loads from disk) controls what
        # the *next* visit returns. We don't try to assert a per-profile
        # catalog content here — see ``test_same_profile_concurrent_stale_visits_coalesce_to_one_rebuild``
        # for per-profile rebuild assertion. This test focuses on the
        # *count* of SWR background rebuilds launched across the horizon.
        return rebuilt_catalog_a

    monkeypatch.setattr(cfg.time, "time", lambda: simulated_now[0])
    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", _path_for_profile)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", _load_for_profile)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", _load_for_profile)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "test"})
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda _cache: None)
    # Force every visit to walk the disk / SWR path (process-global memory
    # cache is irrelevant to this test's per-profile horizon semantics).
    monkeypatch.setattr(cfg, "_get_fresh_memory_models_cache", lambda _now: None)
    # Patch the SWR's per-call active-profile resolver directly so we
    # don't need a real cookie/profile TLS to alternate keys.
    monkeypatch.setattr(
        cfg,
        "_session_visit_active_profile_name",
        lambda: ("profile_a" if _profile_call[0] % 2 == 0 else "profile_b"),
    )
    monkeypatch.setattr(cfg, "get_available_models", _rebuild_for_profile)

    # Track every time the foreground fires a background rebuild. The
    # thread is registered inside ``_maybe_start_session_visit_background_rebuild``
    # before the worker fires, so we can capture the profile_key the
    # foreground *intended* to rebuild.
    launched_rebuilds: list[str] = []
    real_swr = cfg._maybe_start_session_visit_background_rebuild

    def _tracked_swr():
        with cfg._session_visit_rebuild_lock:
            key = cfg._session_visit_active_profile_name()
        launched_rebuilds.append(key)
        real_swr()

    monkeypatch.setattr(cfg, "_maybe_start_session_visit_background_rebuild", _tracked_swr)

    # 12 alternating visits: 6 per profile, 100 s apart each, profile B
    # offset by 50 s so the two profiles never collide. The horizon is
    # 300 s. After tick 2 the mtime is past the horizon for both profiles.
    for tick in range(6):
        _profile_call[0] = 0  # profile A
        simulated_now[0] = planted_mtime + 100.0 * (tick + 1)
        cfg.get_available_models_for_session_visit()
        _profile_call[0] = 1  # profile B
        simulated_now[0] = planted_mtime + 100.0 * (tick + 1) + 50.0
        cfg.get_available_models_for_session_visit()

    # Wait for any background rebuilds to land.
    _wait_for_session_visit_rebuild(monkeypatch, timeout=10)

    # Count the SWR launches per profile. The os.utime restamp fix would
    # have produced 0 SWR launches per profile (mtime would have been
    # fresh forever). The SWR fix produces at least 1 per profile once
    # the horizon is crossed.
    profile_a_launches = sum(1 for k in launched_rebuilds if k == "profile_a")
    profile_b_launches = sum(1 for k in launched_rebuilds if k == "profile_b")
    assert profile_a_launches >= 1, (
        f"profile A must launch a background rebuild after its 300 s "
        f"horizon crossing; got {profile_a_launches} launches "
        f"(os.utime restamp bug would produce 0). All launches: {launched_rebuilds}"
    )
    assert profile_b_launches >= 1, (
        f"profile B must launch a background rebuild after its 300 s "
        f"horizon crossing; got {profile_b_launches} launches "
        f"(os.utime restamp bug would produce 0). All launches: {launched_rebuilds}"
    )


# ── 4. Stale visit returns immediately (latency: no 4 s foreground wait)


def test_stale_visit_returns_immediately_without_foreground_wait(tmp_path, monkeypatch):
    """A stale session-visit must return within milliseconds, not block on
    the live provider probe. The foreground returns the disk/stale catalog
    and fires a background rebuild; the caller's wall-time is the disk
    read + the small SWR bookkeeping, NOT the multi-second live probe.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    stale_catalog = _catalog("stale-latency")
    rebuilt_catalog = _catalog("rebuilt-latency")
    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    old = time.time() - 600.0
    os.utime(cache_path, (old, old))

    rebuild_started = threading.Event()
    rebuild_release = threading.Event()
    rebuild_calls: list[dict] = []

    def _slow_rebuild(**kwargs):
        # Simulate the real-world multi-second provider probe.
        rebuild_calls.append(kwargs)
        rebuild_started.set()
        assert rebuild_release.wait(timeout=5), "test never released the background rebuild"
        return rebuilt_catalog

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: stale_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: stale_catalog)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "get_available_models", _slow_rebuild)

    started = time.monotonic()
    result = cfg.get_available_models_for_session_visit()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    # Foreground returned the stale catalog immediately.
    assert result == stale_catalog
    # The slow rebuild is still in flight — the foreground did not wait for it.
    assert rebuild_started.wait(timeout=5), "background rebuild never started"
    # Generous bound — disk read + a few Python lines should be well under 200 ms
    # on any test machine. The point is: it must NOT be 4 s.
    assert elapsed_ms < 200.0, (
        f"stale session-visit must return immediately, not block on the "
        f"live probe; got {elapsed_ms:.1f} ms (would be ~4000 ms if the "
        f"old blocking-foreground contract were in effect)"
    )
    rebuild_release.set()
    _wait_for_session_visit_rebuild(monkeypatch)
    assert rebuild_calls == [{"force_refresh": True}]


# ── 5. Per-profile coalescing: same profile, concurrent stale visits


def test_same_profile_concurrent_stale_visits_coalesce_to_one_rebuild(tmp_path, monkeypatch):
    """Two concurrent stale visits on the same profile must coalesce into
    exactly one background ``force_refresh`` (and both foregrounds must
    return the stale catalog immediately). Different profiles must NOT
    coalesce (each profile gets its own rebuild), per the reviewer's
    "profiles are islands" note.
    """
    import api.config as cfg
    from concurrent.futures import ThreadPoolExecutor

    _reset_models_memory_cache(monkeypatch)
    stale_catalog = _catalog("coalesce-stale")
    rebuilt_catalog = _catalog("coalesce-rebuilt")
    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    old = time.time() - 600.0
    os.utime(cache_path, (old, old))

    rebuild_count = 0
    rebuild_lock = threading.Lock()
    rebuild_in_progress = threading.Event()
    rebuild_release = threading.Event()

    def _slow_rebuild(**kwargs):
        nonlocal rebuild_count
        with rebuild_lock:
            rebuild_count += 1
        # Block the first rebuild so the second concurrent stale visit
        # definitely sees an in-flight SWR thread and coalesces into it.
        rebuild_in_progress.set()
        assert rebuild_release.wait(timeout=5), "test never released the background rebuild"
        return rebuilt_catalog

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: stale_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: stale_catalog)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda _cache: None)
    monkeypatch.setattr(cfg, "get_available_models", _slow_rebuild)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(cfg.get_available_models_for_session_visit) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert all(result == stale_catalog for result in results)
    assert rebuild_in_progress.wait(timeout=5), "background rebuild never started"
    rebuild_release.set()
    _wait_for_session_visit_rebuild(monkeypatch)
    assert rebuild_count == 1, (
        f"two concurrent same-profile stale visits must coalesce into one "
        f"background rebuild; got {rebuild_count}"
    )


# ── 6. mtime is only moved by _save_models_cache_to_disk, never by a read


def test_disk_mtime_only_moves_on_real_rebuild(tmp_path, monkeypatch):
    """The session-visit on-disk mtime must ONLY advance when a real live
    rebuild runs (whose ``_save_models_cache_to_disk`` call is the sole
    mover). Reading the disk cache — via a session-visit hit, a session-
    visit stale visit, or a plain ``get_available_models`` disk hit —
    must never move the mtime. This is the mtime semantic the 300 s
    horizon is defined against.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    stale_catalog = _catalog("mtime-stale")
    rebuilt_catalog = _catalog("mtime-rebuilt")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    cache_path = tmp_path / "models_cache.profile.json"
    cache_path.write_text("{}", encoding="utf-8")
    planted = time.time() - 600.0
    os.utime(cache_path, (planted, planted))

    disk_save_calls: list[dict] = []
    save_done = threading.Event()
    # Block the background rebuild until the foreground has captured its
    # mtime observation, so the test can prove the mtime was untouched
    # *before* the rebuild was allowed to land.
    rebuild_started = threading.Event()
    rebuild_release = threading.Event()

    def _track_save(cache):
        disk_save_calls.append({"time": time.time()})
        # Mimic the real save's mtime advance (write_text does os.write +
        # close, which updates st_mtime to "now" on most filesystems).
        cache_path.write_text("rebuilt", encoding="utf-8")
        save_done.set()

    def _slow_rebuild(_builder):
        # Block until the foreground observation has completed, then
        # return the rebuilt catalog.
        rebuild_started.set()
        assert rebuild_release.wait(timeout=5), "test never released the background rebuild"
        return rebuilt_catalog

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: stale_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: stale_catalog)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _track_save)
    monkeypatch.setattr(cfg, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_rebuild)

    # 1. Plain ``get_available_models`` disk hit must NOT rewrite the file.
    cfg.get_available_models()
    mtime_after_plain = cache_path.stat().st_mtime
    assert abs(mtime_after_plain - planted) < 0.01, "plain disk hit must not move mtime"
    assert disk_save_calls == []

    # 2. Stale session-visit must return immediately AND the *background*
    # rebuild — when it eventually runs through ``_save_models_cache_to_disk``
    # — is the only thing that may move the mtime.
    result = cfg.get_available_models_for_session_visit()
    assert result == stale_catalog
    # The rebuild is now in flight (blocked on rebuild_release). The
    # foreground has returned; the mtime must STILL be the planted value
    # because the rebuild has not yet run its publish step.
    assert rebuild_started.wait(timeout=5), "background rebuild never started"
    mtime_after_stale_fg = cache_path.stat().st_mtime
    assert abs(mtime_after_stale_fg - planted) < 0.01, (
        "stale foreground must not move mtime before the rebuild's "
        "_save_models_cache_to_disk call has run"
    )
    assert disk_save_calls == []

    # Release the rebuild so its publish can run.
    rebuild_release.set()
    assert save_done.wait(timeout=5), (
        "background rebuild never reached _save_models_cache_to_disk; "
        "the only legitimate mover of the on-disk mtime"
    )
    mtime_after_rebuild = cache_path.stat().st_mtime
    assert mtime_after_rebuild > mtime_after_stale_fg, (
        f"the only legitimate mtime advance is the rebuild's "
        f"_save_models_cache_to_disk call; pre={mtime_after_stale_fg} "
        f"post={mtime_after_rebuild}"
    )
    assert len(disk_save_calls) == 1


# ── 7. Root-profile rebuild under a NAMED process profile (#7724 CORE finding)


def test_root_profile_rebuild_writes_root_cache_under_named_process_profile(
    tmp_path, monkeypatch
):
    """A default/root session-visit rebuild must bind the ROOT profile's home,
    TLS and env, even when the process-level active profile is a NAMED one.

    ``profile_scope_for_detached_worker`` used to no-op for the root/default
    profile, on the assumption that the process defaults to root anyway. That
    fails whenever the WebUI process runs on a named profile (e.g. ``work``)
    while a client's cookie asks for ``default``: the detached SWR worker
    inherited the NAMED process profile, so the root catalog never revalidated
    and the root cache file's mtime never advanced.

    Asserts:
      1. the rebuild resolves the ROOT cache file + root HERMES_HOME on the
         worker (not the named profile's);
      2. ``_save_models_cache_to_disk`` writes the root file's mtime forward;
      3. the NAMED profile's cache file is NOT touched.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)

    root_cache = tmp_path / "models_cache.json"
    named_cache = tmp_path / "models_cache.work.json"
    root_cache.write_text("{}", encoding="utf-8")
    named_cache.write_text("{}", encoding="utf-8")
    old = time.time() - 600.0
    os.utime(root_cache, (old, old))
    os.utime(named_cache, (old, old))
    root_mtime_before = root_cache.stat().st_mtime
    named_mtime_before = named_cache.stat().st_mtime

    # Root home + a named profile that owns env values distinct from the root's.
    root_home = tmp_path / ".hermes"
    named_home = root_home / "profiles" / "work"
    named_home.mkdir(parents=True, exist_ok=True)
    (named_home / ".env").write_text(
        "ISSUE_7724_PROBE=named-profile-value\n", encoding="utf-8"
    )
    monkeypatch.setattr("api.profiles._DEFAULT_HERMES_HOME", root_home)

    observed: dict = {}

    def _worker_inspector(builder=None, **kwargs):
        """Stands in for the live probe inside the background rebuild."""
        from api.profiles import get_active_profile_name

        observed["active_profile"] = get_active_profile_name()
        observed["cache_path"] = cfg._get_models_cache_path()
        observed["hermes_home"] = os.environ.get("HERMES_HOME")
        observed["probe_env"] = os.environ.get("ISSUE_7724_PROBE")
        # The real rebuild publishes through _save_models_cache_to_disk, whose
        # disk write is the only legitimate mover of the on-disk mtime.
        cfg._save_models_cache_to_disk(
            {"active_provider": "openai", "default_model": "rebuilt",
             "configured_model_badges": {}, "groups": []}
        )
        return _catalog("rebuilt")

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    # The request is for the DEFAULT/root profile.
    monkeypatch.setattr(cfg, "_session_visit_active_profile_name", lambda: "default")
    # Keep the real, profile-resolution-based path helper (do NOT stub it):
    # this test fails if the worker does not rebind the root TLS.
    monkeypatch.setattr(cfg, "_models_cache_path", root_cache)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: _catalog("stale"))
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: _catalog("stale"))
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "root"})
    # Mock the INNER rebuild seam (``_invoke_models_rebuild``) rather than
    # ``get_available_models`` itself. The synchronous-budget path inside
    # ``get_available_models`` is what applies the root env for a root
    # request under a named process profile (#7724 re-gate: the outer SWR
    # worker only binds TLS; the env application is the rebuild scope's
    # job). Mocking the outer function would skip the env binding entirely.
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _worker_inspector)

    # PROCESS-level active profile is a NAMED one.
    import api.profiles as profiles_mod

    prev_process_profile = profiles_mod._active_profile
    profiles_mod._active_profile = "work"
    try:
        result = cfg.get_available_models_for_session_visit()
    finally:
        profiles_mod._active_profile = prev_process_profile
    assert result == _catalog("stale"), "stale visit returns the disk catalog"

    _wait_for_session_visit_rebuild(monkeypatch, timeout=10)

    # 1. The worker resolved the ROOT profile, not the named process profile.
    assert observed.get("active_profile") in ("", "default"), (
        f"root-profile rebuild must bind the root profile on the worker, "
        f"got active_profile={observed.get('active_profile')!r} "
        f"(inherited the NAMED process profile)"
    )
    assert observed.get("cache_path") == root_cache, (
        f"root-profile rebuild must write the root cache file, got "
        f"{str(observed.get('cache_path'))!r}"
    )
    assert observed.get("hermes_home") == str(root_home), (
        f"root-profile rebuild must bind the root HERMES_HOME, got "
        f"{observed.get('hermes_home')!r}"
    )
    assert observed.get("probe_env") != "named-profile-value", (
        "root-profile rebuild must not apply the named process profile's .env"
    )

    # 2. The root file's mtime advanced (the rebuild landed and wrote it).
    assert root_cache.stat().st_mtime > root_mtime_before + 1.0, (
        "root-profile rebuild must advance the root cache file's mtime"
    )
    # 3. The named profile's file is untouched.
    assert abs(named_cache.stat().st_mtime - named_mtime_before) < 0.01, (
        "the named process profile's cache file must NOT be touched by a "
        "root-profile rebuild"
    )


def test_root_detached_worker_scope_binds_root_profile_on_worker(monkeypatch, tmp_path):
    """``profile_scope_for_root_detached_worker`` rebinds the root HOME, TLS and
    env on a fresh worker thread even when the process profile is named, and
    restores all of them on exit (#7724)."""
    import threading

    import api.config as config
    import api.profiles as profiles

    root_home = tmp_path / ".hermes"
    named_home = root_home / "profiles" / "work"
    named_home.mkdir(parents=True, exist_ok=True)
    (named_home / ".env").write_text(
        "ISSUE_7724_SCOPE_PROBE=named-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    default_cache = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", default_cache)
    monkeypatch.delenv("ISSUE_7724_SCOPE_PROBE", raising=False)
    monkeypatch.setattr(profiles, "_active_profile", "work")

    out: dict = {}

    def worker():
        from api.profiles import get_active_profile_name

        # No TLS on this fresh thread: without the scope it resolves the
        # NAMED process profile (the bug).
        out["before_name"] = get_active_profile_name()
        out["before_cache"] = config._get_models_cache_path().name
        out["before_env"] = os.environ.get("ISSUE_7724_SCOPE_PROBE")
        out["before_home"] = os.environ.get("HERMES_HOME")
        with profiles.profile_scope_for_root_detached_worker("test-root-worker"):
            out["inside_name"] = get_active_profile_name()
            out["inside_cache"] = config._get_models_cache_path().name
            out["inside_env"] = os.environ.get("ISSUE_7724_SCOPE_PROBE")
            out["inside_home"] = os.environ.get("HERMES_HOME")
        out["after_name"] = get_active_profile_name()
        out["after_env"] = os.environ.get("ISSUE_7724_SCOPE_PROBE")
        out["after_home"] = os.environ.get("HERMES_HOME")

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "root detached worker scope hung"

    # BEFORE: the worker thread inherits the NAMED process profile (the bug).
    # The process-env mirror only carries the named .env when something has
    # applied it process-wide; the reliable inheritance signal is the resolved
    # profile name + profile-keyed cache path, asserted above.
    assert out["before_name"] == "work"
    assert out["before_cache"] == "models_cache.work.json"
    assert out["before_home"] != str(root_home)

    # INSIDE: everything is bound to the ROOT profile.
    assert out["inside_name"] in ("", "default")
    assert out["inside_cache"] == "models_cache.json"
    assert out["inside_env"] != "named-value"
    assert out["inside_home"] == str(root_home)

    # AFTER: fully restored — the worker thread falls back to the NAMED process
    # profile again, and the root home it installed is no longer in effect.
    assert out["after_name"] == "work"
    assert out["after_home"] != str(root_home)


# ── 8. Two-profile barrier: concurrent SWR workers must not leak env (#7724 re-gate)


def test_swr_two_profile_barrier_no_env_leak(tmp_path, monkeypatch):
    """Concurrent SWR workers for two profiles must NOT leak env between them.

    Bug (Codex repro, #7724 re-gate): the outer SWR worker in
    ``_maybe_start_session_visit_background_rebuild`` entered
    ``profile_scope_for_detached_worker`` which mutates ``os.environ`` to the
    captured profile's env. Two concurrent SWR workers — one per profile —
    could interleave their env mutations: while A's bounded rebuild worker
    was running its live probe, B's outer SWR worker entered its own scope
    and overwrote process env with B's env. A's probe then read B's
    credentials and built A's catalog with B's provider keys.

    Fix: the outer SWR worker only binds the request-profile TLS via the
    new ``profile_tls_scope_for_detached_worker`` (no env mutation). The
    bounded rebuild worker inside the cold path is the SOLE env owner, and
    the cold-path lock serializes cold-path rebuilds, so at most one env
    owner is in flight at any time.

    This test reproduces the interleaving: it starts a SWR worker for
    profile A that blocks its bounded rebuild on a barrier, then starts a
    SWR worker for profile B whose outer scope (with the bug) would have
    mutated env to B. With the fix, B's outer scope only sets TLS, so A's
    blocked probe still sees A's env. After the barrier releases, B's
    cold path runs and B's probe sees B's env.
    """
    import api.config as cfg
    import api.profiles as profiles_mod
    from concurrent.futures import ThreadPoolExecutor

    _reset_models_memory_cache(monkeypatch)

    # Two profile homes with distinct .env values
    root_home = tmp_path / ".hermes"
    home_a = root_home / "profiles" / "profile_a"
    home_b = root_home / "profiles" / "profile_b"
    for home in (home_a, home_b):
        home.mkdir(parents=True, exist_ok=True)
    (home_a / ".env").write_text("ISSUE_7724_BARRIER=alpha\n", encoding="utf-8")
    (home_b / ".env").write_text("ISSUE_7724_BARRIER=beta\n", encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "_DEFAULT_HERMES_HOME", root_home)

    # Per-profile disk cache files (stale, so session-visit fires SWR)
    cache_a = tmp_path / "models_cache.profile_a.json"
    cache_b = tmp_path / "models_cache.profile_b.json"
    for p in (cache_a, cache_b):
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (time.time() - 600.0, time.time() - 600.0))

    # The bounded rebuild for A blocks on a barrier until B's outer SWR
    # worker has had a chance to enter its scope. If the outer scope
    # mutates env (the bug), B's scope overwrites A's env and A's blocked
    # probe sees B's env. With the fix, B's scope only sets TLS and A's
    # probe still sees A's env.
    a_probe_seen = threading.Event()
    b_probe_seen = threading.Event()
    release_a = threading.Event()
    observed: dict = {}

    def _inspect(builder, **kwargs):
        from api.profiles import get_active_profile_name
        name = get_active_profile_name()
        if name == "profile_a":
            a_probe_seen.set()
            # Block A's probe until the test releases it. The test waits
            # long enough for B's outer SWR scope to have entered (and,
            # with the bug, mutated process env to B's env) before
            # releasing A.
            assert release_a.wait(timeout=15), "test never released profile A's probe"
            # Record the env NOW (after the race window) — this is what
            # the real live provider probe would see when it reads
            # os.environ. With the bug, this is B's env (leaked). With
            # the fix, this is still A's env.
            observed[name] = dict(os.environ)
        elif name == "profile_b":
            b_probe_seen.set()
            observed[name] = dict(os.environ)
        else:
            observed[name] = dict(os.environ)
        return _catalog(f"rebuilt-{name}")

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _inspect)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: _catalog("stale"))
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: _catalog("stale"))
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "test"})
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda _cache: None)
    monkeypatch.setattr(cfg, "_get_fresh_memory_models_cache", lambda _now: None)

    def _stale_visit_for(profile_name: str, cache_path: Path):
        """Simulate a stale session-visit for one profile, firing SWR."""
        monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
        monkeypatch.setattr(cfg, "_session_visit_active_profile_name", lambda: profile_name)
        return cfg.get_available_models_for_session_visit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        # 1. Start A's stale visit. A's SWR outer worker enters its TLS-only
        # scope and calls get_available_models(force_refresh=True), which
        # enters the cold path and reaches _invoke_models_rebuild (the
        # mock). The mock blocks on release_a.
        a_future = executor.submit(_stale_visit_for, "profile_a", cache_a)
        # Wait for A's probe to actually be running (inside the mock).
        assert a_probe_seen.wait(timeout=10), "A's bounded rebuild never reached the mock"
        # 2. Now start B's stale visit while A's probe is still blocked.
        # B's SWR outer worker enters its scope. With the bug, this would
        # mutate process env to B's env, and A's blocked probe (still
        # inside the mock, reading os.environ) would see B's env. With
        # the fix, B's scope only sets TLS — no env mutation.
        b_future = executor.submit(_stale_visit_for, "profile_b", cache_b)
        # Give B's outer scope time to enter (it sets TLS, then calls
        # get_available_models which blocks on _cache_build_in_progress
        # until A's cold path releases the lock). 500ms is generous
        # enough for the scope body to run on any reasonable test box.
        time.sleep(0.5)
        # 3. Release A's probe. A records the env NOW — after the race
        # window where B's outer scope could have mutated it.
        release_a.set()
        a_result = a_future.result(timeout=10)
        b_result = b_future.result(timeout=10)

    # Wait for both SWR background threads to finish.
    _wait_for_session_visit_rebuild(monkeypatch, timeout=10)

    assert a_result == _catalog("stale")
    assert b_result == _catalog("stale")

    # THE CORE ASSERTION: A's probe must have seen A's env, not B's.
    # With the bug, observed["profile_a"]["ISSUE_7724_BARRIER"] would be
    # "beta" (B's env, leaked via process-wide os.environ mutation in
    # B's outer SWR scope while A's probe was blocked).
    a_env = observed.get("profile_a", {}).get("ISSUE_7724_BARRIER")
    b_env = observed.get("profile_b", {}).get("ISSUE_7724_BARRIER")
    assert a_env == "alpha", (
        f"profile A's bounded rebuild must see profile A's env (no env "
        f"leak from concurrent SWR worker for profile B); "
        f"got ISSUE_7724_BARRIER={a_env!r} (expected 'alpha'). "
        f"With the #7724 re-gate bug, this would be 'beta' (B's env) "
        f"because the outer SWR scope mutated process-wide os.environ."
    )
    assert b_env == "beta", (
        f"profile B's bounded rebuild must see profile B's env; "
        f"got ISSUE_7724_BARRIER={b_env!r} (expected 'beta')"
    )

    # HERMES_HOME should also be profile-specific on each probe.
    a_home = observed.get("profile_a", {}).get("HERMES_HOME")
    b_home = observed.get("profile_b", {}).get("HERMES_HOME")
    assert a_home == str(home_a), (
        f"profile A's probe must bind A's HERMES_HOME, got {a_home!r}"
    )
    assert b_home == str(home_b), (
        f"profile B's probe must bind B's HERMES_HOME, got {b_home!r}"
    )


# ── 9. Request-thread scope contract: never clear the ambient request profile
#      (#7724 re-gate maintainer finding)


def test_request_thread_scope_restores_ambient_request_profile(monkeypatch, tmp_path):
    """``profile_scope_for_active_request`` must NOT clear the request TLS.

    The legacy synchronous (budget<=0) rebuild used to enter
    ``profile_scope_for_detached_worker`` — a helper whose contract is for a
    NEW thread: it sets the request-profile TLS and CLEARS it on exit. On the
    calling REQUEST thread that wipes the foreground profile mid-request, so
    the cache publication that follows the rebuild falls back to the
    process-level named profile (a root request publishes to
    ``models_cache.work.json``) and the rest of the request keeps answering as
    the named profile.

    The request-thread scope restores the ambient TLS instead. This pins the
    contract directly: for each possible incoming state the TLS after the scope
    equals the TLS before it.
    """
    import api.profiles as profiles

    root_home = tmp_path / ".hermes"
    named_home = root_home / "profiles" / "work"
    named_home.mkdir(parents=True, exist_ok=True)
    (named_home / ".env").write_text(
        "ISSUE_7724_REQ_PROBE=from-work-profile\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setenv("ISSUE_7724_REQ_PROBE", "from-process-env")

    cases = [
        # (ambient TLS before, profile_name passed in, expected raw TLS inside)
        ("default", "default", "default"),   # root request under named process
        ("work", "work", "work"),            # named request, ambient == passed
        ("work", "personal", "personal"),    # captured name differs from ambient
        (None, "", None),                    # no request profile at all (no-op)
    ]
    for ambient, captured, expected_inside in cases:
        if ambient is not None:
            profiles.set_request_profile(ambient)
        else:
            profiles.clear_request_profile()
        try:
            with profiles.profile_scope_for_active_request(
                captured, "test-request-scope"
            ):
                assert getattr(profiles._tls, "profile", None) == expected_inside, (
                    f"inside the scope the TLS must be {expected_inside!r} "
                    f"(ambient={ambient!r}, captured={captured!r})"
                )
            # THE CONTRACT: the ambient TLS is RESTORED, never cleared. With the
            # detached-worker contract this was None after the scope for every
            # case, which is what dropped the foreground request onto the
            # process-level profile mid-request.
            assert getattr(profiles._tls, "profile", None) == ambient, (
                f"request-thread scope must restore the ambient TLS "
                f"{ambient!r}; got "
                f"{getattr(profiles._tls, 'profile', None)!r} "
                f"(captured={captured!r}). The detached-worker scope's "
                f"clear-on-exit is wrong on a request thread."
            )
        finally:
            profiles.clear_request_profile()

    # The env is applied for the named profile and unwound afterwards.
    monkeypatch.setattr(profiles, "_active_profile", "work")
    profiles.set_request_profile("work")
    try:
        with profiles.profile_scope_for_active_request("work", "test-request-scope"):
            assert os.environ.get("ISSUE_7724_REQ_PROBE") == "from-work-profile"
        assert os.environ.get("ISSUE_7724_REQ_PROBE") == "from-process-env"
        # The request profile survived the scope exit.
        assert profiles.get_active_profile_name() == "work"
    finally:
        profiles.clear_request_profile()


def test_request_thread_scope_binds_root_under_named_process_profile(
    monkeypatch, tmp_path
):
    """``bind_root=True`` pins the root home/env on the CALLING request thread.

    A root/``default`` request on a server whose process-level active profile is
    NAMED (``work``) must still resolve the ROOT cache file + home while the
    rebuild runs, and must restore the ambient (``default``) TLS afterwards —
    the binding the detached-worker scope used to provide, without its
    clear-on-exit side effect.
    """
    import api.profiles as profiles

    root_home = tmp_path / ".hermes"
    named_home = root_home / "profiles" / "work"
    named_home.mkdir(parents=True, exist_ok=True)
    (named_home / ".env").write_text(
        "ISSUE_7724_ROOT_REQ_PROBE=named-value\n", encoding="utf-8"
    )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setattr(profiles, "_active_profile", "work")
    monkeypatch.delenv("ISSUE_7724_ROOT_REQ_PROBE", raising=False)

    out = {}
    profiles.set_request_profile("default")
    try:
        out["before_home"] = os.environ.get("HERMES_HOME")
        with profiles.profile_scope_for_active_request(
            "default", "test-root-request-scope", bind_root=True
        ):
            out["inside_name"] = profiles.get_active_profile_name()
            out["inside_home"] = os.environ.get("HERMES_HOME")
            out["inside_env"] = os.environ.get("ISSUE_7724_ROOT_REQ_PROBE")
        out["after_name"] = profiles.get_active_profile_name()
    finally:
        profiles.clear_request_profile()

    # The root home + root alias were pinned for the duration.
    assert out["inside_name"] in ("", "default")
    assert out["inside_home"] == str(root_home)
    assert out["inside_env"] != "named-value"
    # The ambient request TLS is restored, not cleared.
    assert out["after_name"] in ("", "default"), (
        f"bind_root must restore the ambient request TLS, got "
        f"{out['after_name']!r}"
    )


def test_request_thread_scope_propagates_body_exceptions(monkeypatch):
    """A body exception must propagate AND still restore the ambient TLS.

    The scope resolves its profile home BEFORE yielding, so it never wraps the
    caller's body in a swallowing ``except``: an exception from the body
    propagates unchanged (through the env scope's unwind and the
    TLS-restoring ``finally``) instead of being re-yielded as a
    ``RuntimeError`` or silently swallowed.
    """
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "_active_profile", "work")

    class _BodyBoom(RuntimeError):
        pass

    profiles.set_request_profile("work")
    try:
        with pytest.raises(_BodyBoom):
            with profiles.profile_scope_for_active_request("work", "test-scope-boom"):
                raise _BodyBoom("caller body failed")
        # The unwind ran: ambient TLS restored (never cleared to None).
        assert getattr(profiles._tls, "profile", None) == "work"
    finally:
        profiles.clear_request_profile()


def test_request_thread_scope_fail_open_on_unresolvable_home(monkeypatch):
    """An unresolvable profile home degrades to a no-op, not an exception.

    Mirrors the pre-existing fail-open contract of the sibling detached-worker
    scope: the caller keeps its ambient profile state and must not break.
    """
    import api.profiles as profiles

    def _boom(_name):
        raise RuntimeError("profile home resolution failed")

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", _boom)

    profiles.set_request_profile("work")
    try:
        with profiles.profile_scope_for_active_request("work", "test-scope-failopen"):
            # No env bound, but the ambient request TLS is still intact.
            assert getattr(profiles._tls, "profile", None) == "work"
        assert getattr(profiles._tls, "profile", None) == "work"
    finally:
        profiles.clear_request_profile()


def test_request_thread_scope_noop_for_root_without_bind_root(monkeypatch):
    """Root/default without ``bind_root`` is a no-op on a request thread."""
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "_active_profile", "work")
    monkeypatch.setenv("ISSUE_7724_NOOP_PROBE", "from-process-env")

    profiles.set_request_profile("default")
    try:
        with profiles.profile_scope_for_active_request("default", "test-scope-noop"):
            assert getattr(profiles._tls, "profile", None) == "default"
            assert os.environ.get("ISSUE_7724_NOOP_PROBE") == "from-process-env"
        assert getattr(profiles._tls, "profile", None) == "default"
    finally:
        profiles.clear_request_profile()
