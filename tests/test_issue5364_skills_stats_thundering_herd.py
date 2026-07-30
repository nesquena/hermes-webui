"""Tests for the cold-startup thundering-herd fix in api.profiles (#5364).

Background
----------
The two-tier mtime cache from #4783 fixed the per-request SKILL.md rescan, but
left two concurrency holes that only bite at container cold start, when the
frontend fires several profile-data requests at once and the caches are empty:

  1. ``_get_profile_skills_stats`` had NO lock, so concurrent misses on the same
     profile each ran ``os.walk`` + parsed every SKILL.md simultaneously.
  2. ``_build_profile_rows_fast`` ran OUTSIDE ``_LIST_PROFILES_CACHE_LOCK`` in
     ``list_profiles_api``, so every concurrent request rebuilt all rows (each
     walking every profile's skill tree) instead of one building while the rest
     waited.

Under Docker overlay2 with 9 profiles this stacked ~45k concurrent ``stat``
calls and stalled worker threads for 57–70 s (per the report).

These tests prove:
  * concurrent misses on one profile collapse to a SINGLE compute;
  * the per-profile lock registry returns a stable lock per profile;
  * a concurrent ``list_profiles_api`` burst builds the rows exactly ONCE;
  * the #4783 contract is preserved — the cheap mtime probe still runs on every
    call so out-of-band changes stay promptly visible.
"""
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import harness (mirrors tests/test_issue4783_profile_skills_mtime_cache.py)
# ---------------------------------------------------------------------------

def _make_profiles_module():
    """Import api.profiles with minimal stubs for heavy dependencies.

    Returns ``(mod, saved_modules, injected_names)`` so the caller can fully
    restore ``sys.modules`` on teardown. This test intentionally manipulates
    ``sys.modules`` (stubbing flask/yaml/agent and re-importing api.profiles
    in isolation); if those mutations leak, later tests re-import api.config /
    api.routes against the stub ``yaml`` (whose ``safe_load`` returns None) and
    a half-populated ``api`` package, which silently breaks unrelated suites
    (e.g. MCP/provider/config tests whose ``get_config`` patch no longer sees
    real config). So we snapshot every key we touch and restore it exactly.
    """
    stubs = {
        "flask": types.ModuleType("flask"),
        "yaml": types.ModuleType("yaml"),
        "agent": types.ModuleType("agent"),
        "agent.skill_utils": types.ModuleType("agent.skill_utils"),
    }

    flask_mod = stubs["flask"]
    flask_mod.request = MagicMock()
    flask_mod.g = MagicMock()
    flask_mod.Blueprint = MagicMock(return_value=MagicMock())
    flask_mod.jsonify = MagicMock(side_effect=lambda x: x)
    flask_mod.abort = MagicMock()
    flask_mod.current_app = MagicMock()

    stubs["yaml"].safe_load = MagicMock(return_value=None)

    su = stubs["agent.skill_utils"]
    su.iter_skill_index_files = lambda skills_dir, filename: skills_dir.rglob(filename)
    su.parse_frontmatter = MagicMock(return_value=({}, ""))
    su.skill_matches_platform = MagicMock(return_value=True)

    # Snapshot every sys.modules key we are about to mutate so teardown can
    # restore the real modules (not just delete them). Keys absent now are
    # recorded as sentinel so teardown removes any stub we introduced.
    _ABSENT = object()
    touched = set(stubs) | {"api", "api.profiles"}
    saved_modules = {k: sys.modules.get(k, _ABSENT) for k in touched}

    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)

    mod_name = "api.profiles"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    api_pkg = types.ModuleType("api")
    sys.modules["api"] = api_pkg

    import importlib.util
    spec_path = Path(__file__).parent.parent / "api" / "profiles.py"
    spec = importlib.util.spec_from_file_location(mod_name, spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        pass
    return mod, saved_modules, _ABSENT


@pytest.fixture()
def mod(tmp_path):
    saved_modules = {}
    _ABSENT = object()
    try:
        m, saved_modules, _ABSENT = _make_profiles_module()
        assert hasattr(m, "_get_profile_skills_stats")
        assert hasattr(m, "_skills_stats_lock_for")
    except Exception:
        # Restore anything we already mutated before skipping.
        for k, v in saved_modules.items():
            if v is _ABSENT:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        pytest.skip("api.profiles not importable in this environment")
    # Reset both caches + the per-profile lock registry for isolation.
    if hasattr(m, "_SKILLS_STATS_CACHE"):
        m._SKILLS_STATS_CACHE.clear()
    if hasattr(m, "_SKILLS_STATS_LOCKS"):
        m._SKILLS_STATS_LOCKS.clear()
    if hasattr(m, "_LIST_PROFILES_CACHE"):
        m._LIST_PROFILES_CACHE = None
    try:
        yield m
    finally:
        # Fully restore sys.modules: put back the real modules we evicted and
        # drop any stub we introduced, so the manipulation cannot leak into
        # subsequent tests (they re-import the real api.config/api.routes).
        for k, v in saved_modules.items():
            if v is _ABSENT:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ---------------------------------------------------------------------------
# 1. Per-profile compute lock collapses a concurrent-miss herd to one compute
# ---------------------------------------------------------------------------

class TestConcurrentMissComputesOnce:
    def test_concurrent_cache_miss_computes_exactly_once(self, mod, tmp_path):
        profile_dir = tmp_path / "p"
        (profile_dir / "skills").mkdir(parents=True)

        compute_calls = []
        compute_lock = threading.Lock()

        def _slow_compute(_pdir):
            with compute_lock:
                compute_calls.append(1)
            # Widen the race window so every thread would pile in without a lock.
            time.sleep(0.15)
            return (3, 5)

        fixed_mtime = 1_700_000_000_000_000_000
        results = []
        results_lock = threading.Lock()

        n_threads = 24
        barrier = threading.Barrier(n_threads)

        def _worker():
            barrier.wait()  # release all threads simultaneously (cold-boot burst)
            r = mod._get_profile_skills_stats(profile_dir)
            with results_lock:
                results.append(r)

        with (
            patch.object(mod, "_compute_profile_skills_stats", side_effect=_slow_compute),
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=fixed_mtime),
        ):
            threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert sum(compute_calls) == 1, (
            "concurrent cache misses on ONE profile must collapse to a single "
            f"_compute_profile_skills_stats call, got {sum(compute_calls)}"
        )
        assert len(results) == n_threads
        assert all(r == (3, 5) for r in results), \
            "every waiting thread must see the single shared computed result"

    def test_distinct_profiles_compute_in_parallel(self, mod, tmp_path):
        """Different profiles must NOT serialize on each other (per-profile lock,
        not a single global lock) — independent trees compute concurrently."""
        n = 4
        dirs = []
        for i in range(n):
            d = tmp_path / f"p{i}"
            (d / "skills").mkdir(parents=True)
            dirs.append(d)

        in_compute = []
        max_concurrent = [0]
        gate = threading.Lock()

        def _slow_compute(_pdir):
            with gate:
                in_compute.append(1)
                max_concurrent[0] = max(max_concurrent[0], sum(in_compute))
            time.sleep(0.15)
            with gate:
                in_compute.pop()
            return (1, 1)

        barrier = threading.Barrier(n)

        def _worker(pdir):
            barrier.wait()
            mod._get_profile_skills_stats(pdir)

        # A single patched probe returns per-path deterministic mtimes so each
        # profile is a distinct cache key (distinct lock).
        path_mtimes = {Path(d).resolve(): 1_700_000_000_000_000_000 + i
                       for i, d in enumerate(dirs)}

        def _probe(skills_dir, config_path):
            return path_mtimes.get(Path(skills_dir).parent.resolve(), 0)

        with (
            patch.object(mod, "_compute_profile_skills_stats", side_effect=_slow_compute),
            patch.object(mod, "_skill_tree_max_mtime_ns", side_effect=_probe),
        ):
            threads = [threading.Thread(target=_worker, args=(d,)) for d in dirs]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert max_concurrent[0] >= 2, (
            "distinct profiles must be able to compute concurrently — a single "
            "global lock would force max_concurrent==1"
        )


# ---------------------------------------------------------------------------
# 2. Lock registry returns a stable per-profile lock
# ---------------------------------------------------------------------------

class TestLockRegistry:
    def test_same_profile_returns_same_lock(self, mod, tmp_path):
        d = (tmp_path / "p").resolve()
        l1 = mod._skills_stats_lock_for(d)
        l2 = mod._skills_stats_lock_for(d)
        assert l1 is l2

    def test_distinct_profiles_get_distinct_locks(self, mod, tmp_path):
        a = (tmp_path / "a").resolve()
        b = (tmp_path / "b").resolve()
        assert mod._skills_stats_lock_for(a) is not mod._skills_stats_lock_for(b)


# ---------------------------------------------------------------------------
# 3. list_profiles_api single-flights the row build under a concurrent burst
# ---------------------------------------------------------------------------

class TestListProfilesSingleFlight:
    def test_concurrent_list_profiles_builds_rows_once(self, mod):
        build_calls = []
        build_lock = threading.Lock()

        def _slow_build():
            with build_lock:
                build_calls.append(1)
            time.sleep(0.15)
            return [{"name": "default", "path": "/x"}]

        n_threads = 16
        barrier = threading.Barrier(n_threads)
        results = []
        results_lock = threading.Lock()

        def _worker():
            barrier.wait()
            r = mod.list_profiles_api()
            with results_lock:
                results.append(r)

        with (
            patch.object(mod, "_is_isolated_profile_mode", return_value=False),
            patch.object(mod, "get_active_profile_name", return_value="default"),
            patch.object(mod, "_build_profile_rows_fast", side_effect=_slow_build),
        ):
            mod._LIST_PROFILES_CACHE = None
            threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert sum(build_calls) == 1, (
            "a concurrent cold-start burst must build the profile rows exactly "
            f"once (single-flight), got {sum(build_calls)}"
        )
        assert len(results) == n_threads
        assert all(r and r[0]["name"] == "default" for r in results)


# ---------------------------------------------------------------------------
# 4. #4783 contract preserved: the cheap mtime probe still runs on every call
# ---------------------------------------------------------------------------

class TestProbeStillRunsEveryCall:
    def test_probe_runs_on_cache_hit(self, mod, tmp_path):
        profile_dir = tmp_path / "p"
        (profile_dir / "skills").mkdir(parents=True)

        with (
            patch.object(mod, "_compute_profile_skills_stats",
                         wraps=mod._compute_profile_skills_stats) as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns",
                         wraps=mod._skill_tree_max_mtime_ns) as mock_probe,
        ):
            mod._get_profile_skills_stats(profile_dir)
            compute_after_first = mock_compute.call_count
            probe_after_first = mock_probe.call_count

            mod._get_profile_skills_stats(profile_dir)  # within TTL, unchanged

            assert mock_compute.call_count == compute_after_first, \
                "expensive compute must be skipped within TTL when unchanged"
            assert mock_probe.call_count > probe_after_first, \
                "cheap mtime probe MUST still run on every call (#4783 contract)"
