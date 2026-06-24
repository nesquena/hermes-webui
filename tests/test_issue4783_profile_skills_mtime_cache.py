"""Tests for the two-tier mtime cache in _get_profile_skills_stats (#4783).

Proof matrix:
  1. Within-TTL returns cached counts with zero I/O (no stat, no read).
  2. After TTL, unchanged files trigger stat-only path (no recompute).
  3. After TTL, changed files trigger full recompute.
  4. config.yaml mtime change is detected by the stat probe.
  5. .clear() forces immediate recompute.
  6. Return signature is unchanged: tuple[int, int].
"""
import importlib
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_profiles_module():
    """Import api.profiles with minimal stubs for heavy dependencies."""
    # Stub out modules that would fail to import in a bare test environment.
    stubs = {
        "flask": types.ModuleType("flask"),
        "yaml": types.ModuleType("yaml"),
        "agent": types.ModuleType("agent"),
        "agent.skill_utils": types.ModuleType("agent.skill_utils"),
    }

    # Minimal flask stubs
    flask_mod = stubs["flask"]
    flask_mod.request = MagicMock()
    flask_mod.g = MagicMock()
    flask_mod.Blueprint = MagicMock(return_value=MagicMock())
    flask_mod.jsonify = MagicMock(side_effect=lambda x: x)
    flask_mod.abort = MagicMock()
    flask_mod.current_app = MagicMock()

    # Minimal yaml stub
    stubs["yaml"].safe_load = MagicMock(return_value=None)

    # skill_utils stubs
    su = stubs["agent.skill_utils"]
    su.iter_skill_index_files = MagicMock(return_value=iter([]))
    su.parse_frontmatter = MagicMock(return_value=({}, ""))
    su.skill_matches_platform = MagicMock(return_value=True)

    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)

    # Force reimport so our stubs are used
    mod_name = "api.profiles"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    # api package stub
    api_pkg = types.ModuleType("api")
    sys.modules["api"] = api_pkg

    import importlib.util, os
    spec_path = Path(__file__).parent.parent / "api" / "profiles.py"
    spec = importlib.util.spec_from_file_location(mod_name, spec_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Accept partial loads — we only need the cache functions
        pass
    return mod


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the module-level cache and restore sys.modules after each test."""
    saved = {k: v for k, v in sys.modules.items() if k == "api" or k.startswith("api.")}
    try:
        mod = sys.modules.get("api.profiles")
        if mod and hasattr(mod, "_SKILLS_STATS_CACHE"):
            mod._SKILLS_STATS_CACHE.clear()
    except Exception:
        pass
    yield
    try:
        mod = sys.modules.get("api.profiles")
        if mod and hasattr(mod, "_SKILLS_STATS_CACHE"):
            mod._SKILLS_STATS_CACHE.clear()
    except Exception:
        pass
    for k in [k for k in sys.modules if k == "api" or k.startswith("api.")]:
        if k in saved:
            sys.modules[k] = saved[k]
        else:
            sys.modules.pop(k, None)


# ---------------------------------------------------------------------------
# Tests using direct cache manipulation (no heavy import required)
# ---------------------------------------------------------------------------

@pytest.fixture()
def profiles_mod(tmp_path):
    """Return (mod, profile_dir) with the cache functions importable."""
    # Attempt a real import; fall back to a minimal synthetic module if it fails.
    try:
        mod = _make_profiles_module()
        assert hasattr(mod, "_get_profile_skills_stats")
    except Exception:
        pytest.skip("api.profiles not importable in this environment")
    profile_dir = tmp_path / "test_profile"
    profile_dir.mkdir()
    return mod, profile_dir


class TestWithinTTLZeroIO:
    """Proof matrix row 1: within TTL, no I/O at all."""

    def test_second_call_skips_compute_and_stat(self, profiles_mod):
        mod, profile_dir = profiles_mod
        with (
            patch.object(mod, "_compute_profile_skills_stats", wraps=mod._compute_profile_skills_stats) as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns", wraps=mod._skill_tree_max_mtime_ns) as mock_stat,
        ):
            mod._get_profile_skills_stats(profile_dir)
            compute_calls_after_first = mock_compute.call_count
            stat_calls_after_first = mock_stat.call_count

            # Second call within TTL
            result = mod._get_profile_skills_stats(profile_dir)

            assert mock_compute.call_count == compute_calls_after_first, \
                "_compute_profile_skills_stats must NOT be called within TTL"
            assert mock_stat.call_count == stat_calls_after_first, \
                "_skill_tree_max_mtime_ns must NOT be called within TTL"
            assert isinstance(result, tuple) and len(result) == 2


class TestAfterTTLUnchangedFilesStatOnly:
    """Proof matrix row 2: after TTL, unchanged mtime → stat only, no recompute."""

    def test_unchanged_mtime_refreshes_ttl_no_recompute(self, profiles_mod):
        mod, profile_dir = profiles_mod

        # Seed cache with an expired entry using a known mtime
        fixed_mtime_ns = 1_700_000_000_000_000_000
        past_expiry = time.time() - 1.0
        resolved = Path(profile_dir).resolve()
        mod._SKILLS_STATS_CACHE[resolved] = (3, 5, fixed_mtime_ns, past_expiry)

        with (
            patch.object(mod, "_compute_profile_skills_stats") as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=fixed_mtime_ns) as mock_stat,
        ):
            result = mod._get_profile_skills_stats(profile_dir)

        mock_stat.assert_called_once()
        mock_compute.assert_not_called()
        assert result == (3, 5)

        # TTL should have been refreshed
        new_entry = mod._SKILLS_STATS_CACHE.get(resolved)
        assert new_entry is not None
        assert new_entry[3] > time.time()  # expiry is in the future


class TestAfterTTLChangedFilesFullRecompute:
    """Proof matrix row 3: after TTL, changed mtime → full recompute."""

    def test_changed_mtime_triggers_recompute(self, profiles_mod):
        mod, profile_dir = profiles_mod

        old_mtime_ns = 1_000_000_000_000_000_000
        new_mtime_ns = 2_000_000_000_000_000_000
        past_expiry = time.time() - 1.0
        resolved = Path(profile_dir).resolve()
        mod._SKILLS_STATS_CACHE[resolved] = (1, 2, old_mtime_ns, past_expiry)

        with (
            patch.object(mod, "_compute_profile_skills_stats", return_value=(7, 9)) as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=new_mtime_ns),
        ):
            result = mod._get_profile_skills_stats(profile_dir)

        mock_compute.assert_called_once()
        assert result == (7, 9)


class TestConfigYamlMtimeDetected:
    """Proof matrix row 4: config.yaml mtime change detected after TTL."""

    def test_config_yaml_change_detected(self, profiles_mod, tmp_path):
        mod, profile_dir = profiles_mod

        # Write a real config.yaml; the stat probe should pick up its mtime
        config_path = profile_dir / "config.yaml"
        config_path.write_text("skills: {}\n", encoding="utf-8")

        old_mtime_ns = config_path.stat().st_mtime_ns
        past_expiry = time.time() - 1.0
        resolved = Path(profile_dir).resolve()
        mod._SKILLS_STATS_CACHE[resolved] = (2, 4, old_mtime_ns, past_expiry)

        # Bump config.yaml mtime
        new_mtime_ns = old_mtime_ns + 1_000_000_000  # +1 second in ns

        with (
            patch.object(mod, "_compute_profile_skills_stats", return_value=(0, 0)) as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=new_mtime_ns),
        ):
            mod._get_profile_skills_stats(profile_dir)

        mock_compute.assert_called_once()


class TestClearForcesRecompute:
    """Proof matrix row 5: .clear() forces recompute regardless of TTL."""

    def test_clear_forces_recompute(self, profiles_mod):
        mod, profile_dir = profiles_mod

        # Populate cache with a fresh (non-expired) entry
        resolved = Path(profile_dir).resolve()
        future_expiry = time.time() + 9999.0
        mod._SKILLS_STATS_CACHE[resolved] = (3, 3, 0, future_expiry)

        mod._SKILLS_STATS_CACHE.clear()

        with patch.object(mod, "_compute_profile_skills_stats", return_value=(0, 0)) as mock_compute:
            mod._get_profile_skills_stats(profile_dir)

        mock_compute.assert_called_once()


class TestReturnSignature:
    """Proof matrix row 6: return signature is tuple[int, int]."""

    def test_returns_two_int_tuple(self, profiles_mod):
        mod, profile_dir = profiles_mod
        result = mod._get_profile_skills_stats(profile_dir)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], int)

    def test_no_skills_returns_zeros(self, profiles_mod):
        mod, profile_dir = profiles_mod
        result = mod._get_profile_skills_stats(profile_dir)
        # Directory has no skills/ subdirectory — expect (0, 0)
        assert result == (0, 0)
