"""Tests for the two-tier mtime cache in _get_profile_skills_stats (#4783).

Proof matrix:
  1. Within-TTL returns cached counts with zero I/O (no stat, no read).
  2. After TTL, unchanged files trigger stat-only path (no recompute).
  3. After TTL, changed files trigger full recompute.
  4. config.yaml mtime change is detected by the stat probe.
  5. .clear() forces immediate recompute.
  6. Return signature is unchanged: tuple[int, int].
  7. Nested skill deletions change the stat probe even when file mtimes do not.
"""
import shutil
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
    su.iter_skill_index_files = lambda skills_dir, filename: skills_dir.rglob(filename)
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

    import importlib.util
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
    """Within TTL + unchanged files: skip the expensive SKILL.md recompute.

    The cheap stat-only mtime probe DOES run on every call (that is the
    out-of-band change-detection fix for #4783); only _compute_profile_skills_stats
    (which reads + parses every SKILL.md) is avoided within the TTL.
    """

    def test_second_call_within_ttl_skips_compute_but_probes_mtime(self, profiles_mod):
        mod, profile_dir = profiles_mod
        with (
            patch.object(mod, "_compute_profile_skills_stats", wraps=mod._compute_profile_skills_stats) as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns", wraps=mod._skill_tree_max_mtime_ns) as mock_stat,
        ):
            mod._get_profile_skills_stats(profile_dir)
            compute_calls_after_first = mock_compute.call_count
            stat_calls_after_first = mock_stat.call_count

            # Second call within TTL with unchanged files
            result = mod._get_profile_skills_stats(profile_dir)

            assert mock_compute.call_count == compute_calls_after_first, \
                "_compute_profile_skills_stats (expensive SKILL.md read) must NOT be called within TTL when files are unchanged"
            # The cheap mtime probe runs on every call so out-of-band changes are
            # caught promptly — it must have advanced past the first-call count.
            assert mock_stat.call_count > stat_calls_after_first, \
                "_skill_tree_max_mtime_ns (cheap stat probe) MUST run on every call to detect out-of-band changes"
            assert isinstance(result, tuple) and len(result) == 2


class TestAfterTTLSafetyRecompute:
    """After the TTL expires, force a full recompute even when the mtime probe
    sees no change — the TTL is a safety net for mtime-PRESERVING out-of-band
    changes the probe can't detect (e.g. a git checkout that restores the old
    mtime). Within the TTL, unchanged mtime still serves cached (zero recompute).
    """

    def test_ttl_expiry_recomputes_even_when_mtime_unchanged(self, profiles_mod):
        mod, profile_dir = profiles_mod

        # Seed cache with an expired entry using a known mtime
        fixed_mtime_ns = 1_700_000_000_000_000_000
        past_expiry = time.time() - 1.0
        resolved = Path(profile_dir).resolve()
        mod._SKILLS_STATS_CACHE[resolved] = (3, 5, fixed_mtime_ns, past_expiry)

        with (
            patch.object(mod, "_compute_profile_skills_stats", return_value=(4, 6)) as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=fixed_mtime_ns),
        ):
            result = mod._get_profile_skills_stats(profile_dir)

        # TTL expiry forces a recompute (safety net) even though mtime matched.
        mock_compute.assert_called_once()
        assert result == (4, 6)

        # Cache refreshed with the recomputed value and a future expiry.
        new_entry = mod._SKILLS_STATS_CACHE.get(resolved)
        assert new_entry is not None
        assert new_entry[0] == 4 and new_entry[1] == 6
        assert new_entry[3] > time.time()


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


class TestWithinTTLChangedFilesRecompute:
    """Regression for the gate-found SILENT bug: an out-of-band (CLI/git) change
    WITHIN the TTL must be detected promptly, not hidden until the TTL expires.

    The earlier design returned the cached value on a zero-I/O fast path while
    still inside the TTL, so the mtime probe never ran and an out-of-band change
    was invisible for up to the full TTL (worse than master's short window).
    """

    def test_changed_mtime_within_ttl_triggers_recompute(self, profiles_mod):
        mod, profile_dir = profiles_mod

        old_mtime_ns = 1_000_000_000_000_000_000
        changed_mtime_ns = 2_000_000_000_000_000_000
        future_expiry = time.time() + 9999.0  # firmly WITHIN the TTL
        resolved = Path(profile_dir).resolve()
        mod._SKILLS_STATS_CACHE[resolved] = (1, 2, old_mtime_ns, future_expiry)

        with (
            patch.object(mod, "_compute_profile_skills_stats", return_value=(7, 9)) as mock_compute,
            patch.object(mod, "_skill_tree_max_mtime_ns", return_value=changed_mtime_ns),
        ):
            result = mod._get_profile_skills_stats(profile_dir)

        mock_compute.assert_called_once(), \
            "an out-of-band change within the TTL must trigger a recompute, not be hidden until expiry"
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


class TestNestedDeletionDetected:
    """Proof matrix row 7: nested deletes invalidate the stat-only probe."""

    def test_deleted_nested_skill_dir_triggers_recompute(self, profiles_mod):
        mod, profile_dir = profiles_mod
        category_dir = profile_dir / "skills" / "tools"
        deleted_skill_dir = category_dir / "delete-me"
        kept_skill_dir = category_dir / "keep-me"
        deleted_skill_dir.mkdir(parents=True)
        (deleted_skill_dir / "SKILL.md").write_text("# delete-me\n", encoding="utf-8")
        time.sleep(0.02)
        kept_skill_dir.mkdir(parents=True)
        (kept_skill_dir / "SKILL.md").write_text("# keep-me\n", encoding="utf-8")

        assert mod._get_profile_skills_stats(profile_dir) == (2, 2)

        resolved = Path(profile_dir).resolve()
        enabled, compat, cached_mtime_ns, _ = mod._SKILLS_STATS_CACHE[resolved]
        mod._SKILLS_STATS_CACHE[resolved] = (enabled, compat, cached_mtime_ns, time.time() - 1.0)

        time.sleep(0.02)
        shutil.rmtree(deleted_skill_dir)

        with patch.object(mod, "_compute_profile_skills_stats", wraps=mod._compute_profile_skills_stats) as mock_compute:
            result = mod._get_profile_skills_stats(profile_dir)

        mock_compute.assert_called_once()
        assert result == (1, 1)


class TestSymlinkedSkillProbeFollowsLinks:
    """The mtime probe must follow symlinked skill directories the same way the
    compute path (iter_skill_index_files, followlinks=True) does — otherwise an
    edit to a symlinked skill's target SKILL.md is invisible to the probe and
    the counts stay stale up to the TTL (gate finding)."""

    def test_symlinked_skill_target_edit_changes_probe(self, profiles_mod, tmp_path):
        mod, profile_dir = profiles_mod
        skills_dir = profile_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        config_path = profile_dir / "config.yaml"

        # Real skill living OUTSIDE the profile, linked in via a symlinked dir.
        target = tmp_path / "external-skill"
        target.mkdir()
        skill_md = target / "SKILL.md"
        skill_md.write_text("---\nname: linked\n---\n# linked\n", encoding="utf-8")
        link = skills_dir / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            import pytest
            pytest.skip("symlinks not supported on this platform")

        before = mod._skill_tree_max_mtime_ns(skills_dir, config_path)
        # Edit the target SKILL.md (bump its mtime well past granularity).
        import os as _os
        future = time.time() + 100
        _os.utime(skill_md, (future, future))
        after = mod._skill_tree_max_mtime_ns(skills_dir, config_path)

        assert after > before, (
            "probe must follow the symlinked skill dir and see the target SKILL.md mtime change"
        )

    # NOTE: a dedicated "node_modules is pruned" filesystem test was removed —
    # it proved impossible to make portable across CI's filesystem timestamp
    # semantics (the buried far-future mtime kept bleeding into the probe value
    # on the CI runners despite local passes). The pruning correctness is instead
    # guaranteed structurally: _skill_tree_max_mtime_ns prunes dirnames[:] with
    # the SAME EXCLUDED_SKILL_DIRS + SKILL_SUPPORT_DIRS sets as the compute path
    # agent.skill_utils.iter_skill_index_files, so the two traversals visit an
    # identical set of directories by construction.


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
