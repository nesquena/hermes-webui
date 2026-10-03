"""Tests for the #7940 stale-while-revalidate skills-stats cache.

Before #7940 the stat-only mtime probe (a full skill-tree walk) ran on EVERY
_get_profile_skills_stats call, which made the probe itself the dominant
/api/profiles cost. The cache is now SWR:

  fresh window  (validated_at < REVALIDATE_AFTER ago): zero-I/O hit.
  stale window  (still inside the hard TTL):            stale counts returned
                                                        immediately + a
                                                        single-flight
                                                        background worker
                                                        re-probes/republishes.
  past hard TTL (300s) / miss / .clear():               synchronous probe +
                                                        compute (safety net).

Cache tuple: (enabled, compat, mtime_ns, hard_expiry, org, validated_at).
The background worker is driven synchronously in these tests through the
_start_skills_stats_revalidate_thread seam.
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_issue4783_profile_skills_mtime_cache import _make_profiles_module


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """_make_profiles_module installs a stub `api` package (and flask/yaml/
    agent stubs via setdefault) into sys.modules. The matching autouse restore
    in test_issue4783 only covers tests in THAT file — without the same
    save/restore here, the bare `api` stub leaks into every test file that
    runs after this one in the same pytest process (and breaks their
    `import api.*` / monkeypatch.setattr("api.x", ...) lookups)."""
    stub_keys = ("flask", "yaml", "agent", "agent.skill_utils")
    marker = object()
    saved = {k: sys.modules.get(k, marker) for k in stub_keys}
    saved.update({k: v for k, v in sys.modules.items() if k == "api" or k.startswith("api.")})
    yield
    for k in [k for k in sys.modules if k in stub_keys or k == "api" or k.startswith("api.")]:
        old = saved.get(k, marker)
        if old is marker:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = old


@pytest.fixture()
def profiles_mod(tmp_path):
    try:
        mod = _make_profiles_module()
        assert hasattr(mod, "_get_profile_skills_stats")
    except Exception:
        pytest.skip("api.profiles not importable in this environment")
    profile_dir = tmp_path / "test_profile"
    profile_dir.mkdir()
    mod._SKILLS_STATS_CACHE.clear()
    mod._SKILLS_STATS_REVALIDATE_INFLIGHT.clear()
    yield mod, profile_dir
    mod._SKILLS_STATS_CACHE.clear()
    mod._SKILLS_STATS_REVALIDATE_INFLIGHT.clear()


def _seed(mod, profile_dir, *, enabled=3, compat=5, mtime=111, expiry=None,
          org=None, validated_at=None):
    resolved = Path(profile_dir).resolve()
    now = time.time()
    mod._SKILLS_STATS_CACHE[resolved] = (
        enabled, compat, mtime,
        now + 9999.0 if expiry is None else expiry,
        org,
        now if validated_at is None else validated_at,
    )
    return resolved


def _sync_worker(mod):
    """Patch the spawn seam so the worker runs inline (deterministic, no thread)."""
    return patch.object(
        mod, "_start_skills_stats_revalidate_thread",
        side_effect=lambda pd: mod._revalidate_skills_stats_worker(pd),
    )


class TestFreshWindowZeroIO:
    def test_fresh_hit_runs_no_probe_no_compute_no_thread(self, profiles_mod):
        mod, profile_dir = profiles_mod
        resolved = _seed(mod, profile_dir)
        with (
            patch.object(mod, "_skill_tree_max_mtime_ns") as mock_probe,
            patch.object(mod, "_compute_profile_skills_stats") as mock_compute,
            patch.object(mod, "_start_skills_stats_revalidate_thread") as mock_spawn,
        ):
            result = mod._get_profile_skills_stats(profile_dir)

        assert result == (3, 5)
        mock_probe.assert_not_called()
        mock_compute.assert_not_called()
        mock_spawn.assert_not_called()
        assert resolved not in mod._SKILLS_STATS_REVALIDATE_INFLIGHT


class TestStaleWindowSWR:
    def test_stale_hit_returns_stale_and_triggers_one_worker(self, profiles_mod):
        mod, profile_dir = profiles_mod
        stale = time.time() - mod._SKILLS_STATS_REVALIDATE_AFTER - 1.0
        resolved = _seed(mod, profile_dir, validated_at=stale)

        with (
            _sync_worker(mod),
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=111) as mock_probe,
        ):
            result = mod._get_profile_skills_stats(profile_dir)

        # Stale counts answered immediately; the worker re-probed off-thread.
        assert result == (3, 5)
        mock_probe.assert_called_once()
        assert resolved not in mod._SKILLS_STATS_REVALIDATE_INFLIGHT

    def test_second_stale_hit_does_not_duplicate_inflight(self, profiles_mod):
        mod, profile_dir = profiles_mod
        stale = time.time() - mod._SKILLS_STATS_REVALIDATE_AFTER - 1.0
        resolved = _seed(mod, profile_dir, validated_at=stale)

        # Park an inflight marker — the trigger must not spawn a second worker.
        mod._SKILLS_STATS_REVALIDATE_INFLIGHT.add(resolved)
        with patch.object(mod, "_start_skills_stats_revalidate_thread") as mock_spawn:
            result = mod._get_profile_skills_stats(profile_dir)

        assert result == (3, 5)
        mock_spawn.assert_not_called()
        mod._SKILLS_STATS_REVALIDATE_INFLIGHT.discard(resolved)


class TestWorkerNoChange:
    def test_no_change_bumps_validated_at_keeps_expiry(self, profiles_mod):
        mod, profile_dir = profiles_mod
        stale = time.time() - mod._SKILLS_STATS_REVALIDATE_AFTER - 1.0
        expiry = time.time() + 9999.0
        resolved = _seed(mod, profile_dir, mtime=222, expiry=expiry, validated_at=stale)
        mod._SKILLS_STATS_REVALIDATE_INFLIGHT.add(resolved)

        with (
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=222),
            patch.object(mod, "_active_org_marker", return_value=None),
            patch.object(mod, "_compute_profile_skills_stats") as mock_compute,
        ):
            mod._revalidate_skills_stats_worker(profile_dir)

        mock_compute.assert_not_called()
        enabled, compat, mtime, new_expiry, org, validated = mod._SKILLS_STATS_CACHE[resolved]
        assert (enabled, compat, mtime, org) == (3, 5, 222, None)
        assert new_expiry == expiry, "no-change worker must NOT extend the hard TTL safety net"
        assert validated > stale
        assert resolved not in mod._SKILLS_STATS_REVALIDATE_INFLIGHT


class TestWorkerChanged:
    def test_changed_probe_recomputes_and_publishes(self, profiles_mod):
        mod, profile_dir = profiles_mod
        stale = time.time() - mod._SKILLS_STATS_REVALIDATE_AFTER - 1.0
        resolved = _seed(mod, profile_dir, mtime=111, validated_at=stale)
        mod._SKILLS_STATS_REVALIDATE_INFLIGHT.add(resolved)

        with (
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=999),
            patch.object(mod, "_active_org_marker", return_value="org-b"),
            patch.object(mod, "_compute_profile_skills_stats", return_value=(8, 10)) as mock_compute,
        ):
            mod._revalidate_skills_stats_worker(profile_dir)

        mock_compute.assert_called_once()
        enabled, compat, mtime, expiry, org, validated = mod._SKILLS_STATS_CACHE[resolved]
        assert (enabled, compat, mtime, org) == (8, 10, 999, "org-b")
        assert expiry > time.time()
        assert validated > stale


class TestWorkerEdgeCases:
    def test_cleared_cache_returns_early(self, profiles_mod):
        mod, profile_dir = profiles_mod
        stale = time.time() - mod._SKILLS_STATS_REVALIDATE_AFTER - 1.0
        resolved = _seed(mod, profile_dir, validated_at=stale)
        mod._SKILLS_STATS_REVALIDATE_INFLIGHT.add(resolved)
        mod._SKILLS_STATS_CACHE.clear()  # a mutation landed between trigger and run

        with patch.object(mod, "_skill_tree_max_mtime_ns") as mock_probe:
            mod._revalidate_skills_stats_worker(profile_dir)

        mock_probe.assert_not_called()  # left for the next request's sync miss
        assert resolved not in mod._SKILLS_STATS_REVALIDATE_INFLIGHT

    def test_worker_exception_is_swallowed_and_inflight_cleared(self, profiles_mod):
        mod, profile_dir = profiles_mod
        stale = time.time() - mod._SKILLS_STATS_REVALIDATE_AFTER - 1.0
        resolved = _seed(mod, profile_dir, validated_at=stale)
        mod._SKILLS_STATS_REVALIDATE_INFLIGHT.add(resolved)

        with patch.object(mod, "_skill_tree_max_mtime_ns", side_effect=OSError("disk gone")):
            mod._revalidate_skills_stats_worker(profile_dir)  # must not raise

        assert resolved not in mod._SKILLS_STATS_REVALIDATE_INFLIGHT

    def test_hard_expiry_takes_synchronous_path(self, profiles_mod):
        mod, profile_dir = profiles_mod
        past = time.time() - 1.0
        _seed(mod, profile_dir, mtime=111, expiry=past, validated_at=past)

        with (
            patch.object(mod, "_start_skills_stats_revalidate_thread") as mock_spawn,
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=111),
            patch.object(mod, "_active_org_marker", return_value=None),
            patch.object(mod, "_compute_profile_skills_stats", return_value=(4, 6)) as mock_compute,
        ):
            result = mod._get_profile_skills_stats(profile_dir)

        # Hard TTL expiry still recomputes synchronously (mtime-preserving
        # change safety net); no SWR trigger on an expired entry.
        mock_compute.assert_called_once()
        mock_spawn.assert_not_called()
        assert result == (4, 6)
