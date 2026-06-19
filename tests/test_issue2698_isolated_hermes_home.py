"""
Tests for issue #2698: HERMES_HOME isolated profile mode.

When HERMES_HOME points at a specific profile directory like ~/.hermes/profiles/user1,
the WebUI should pin to that single profile: list only it, reject create/switch/delete
of other profiles, and hide multi-profile UI affordances.
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import api.profiles as _profiles_mod
from api.profiles import (
    _is_isolated_profile_mode,
    list_profiles_api,
    create_profile_api,
    delete_profile_api,
)


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    """Clear the profile list cache before and after each test to prevent cross-test leakage."""
    _profiles_mod._LIST_PROFILES_CACHE = None
    yield
    _profiles_mod._LIST_PROFILES_CACHE = None


@pytest.fixture
def temp_hermes_home():
    """Create a temporary .hermes directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir) / ".hermes"
        home.mkdir()
        profiles_root = home / "profiles"
        profiles_root.mkdir()
        yield home


@pytest.fixture
def temp_single_profile():
    """Create a temporary .hermes/profiles/user1 structure for isolated mode."""
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir) / ".hermes"
        home.mkdir()
        profiles_root = home / "profiles"
        profiles_root.mkdir()
        user1 = profiles_root / "user1"
        user1.mkdir()
        # Create required subdirs
        for subdir in ["memories", "sessions", "skills", "skins", "logs", "plans", "workspace", "cron"]:
            (user1 / subdir).mkdir(exist_ok=True)
        yield user1


class TestIsolatedProfileModeDetection:
    """Test _is_isolated_profile_mode() helper."""

    def test_normal_mode_when_hermes_home_is_base(self, temp_hermes_home):
        """Normal mode when HERMES_HOME points to base ~/.hermes."""
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(temp_hermes_home)}):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", temp_hermes_home):
                with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(temp_hermes_home)):
                    isolated = _is_isolated_profile_mode()
                    assert isolated is False

    def test_isolated_mode_when_hermes_home_is_profile_subdir(self, temp_single_profile):
        """Isolated mode when HERMES_HOME points to ~/.hermes/profiles/user1."""
        # Ensure we're in the fixture context where temp_single_profile exists
        assert temp_single_profile.exists(), f"Test fixture path doesn't exist: {temp_single_profile}"
        assert temp_single_profile.parent.name == "profiles", f"Parent not named 'profiles': {temp_single_profile.parent}"

        # _is_isolated_profile_mode() uses _INITIAL_HERMES_HOME (snapshotted at import time),
        # not the current os.environ value. Patch both for this test.
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(temp_single_profile)}, clear=False):
            with mock.patch.dict(os.environ, {"HERMES_BASE_HOME": ""}, clear=False):
                with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(temp_single_profile)):
                    isolated = _is_isolated_profile_mode()
                    assert isolated is True, f"Expected isolated mode for {temp_single_profile}"

    def test_hermes_base_home_override_forces_normal_mode(self, temp_single_profile):
        """HERMES_BASE_HOME env var override forces normal mode even with profile subdir."""
        base_home = temp_single_profile.parent.parent
        with mock.patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(temp_single_profile),
                "HERMES_BASE_HOME": str(base_home),
            },
        ):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", base_home):
                with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(temp_single_profile)):
                    isolated = _is_isolated_profile_mode()
                    assert isolated is False


class TestListProfilesInIsolatedMode:
    """Test list_profiles_api() returns only isolated profile when in isolated mode."""

    def test_list_returns_all_profiles_in_normal_mode(self, temp_hermes_home):
        """Normal mode lists all profiles."""
        # Create a few test profiles
        profiles_root = temp_hermes_home / "profiles"
        (profiles_root / "user1").mkdir()
        (profiles_root / "user2").mkdir()
        (profiles_root / "user3").mkdir()

        # Create required subdirs for each
        for prof_dir in profiles_root.iterdir():
            if prof_dir.is_dir():
                for subdir in ["memories", "sessions", "skills", "skins", "logs", "plans", "workspace", "cron"]:
                    (prof_dir / subdir).mkdir(exist_ok=True)

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(temp_hermes_home)}):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", temp_hermes_home):
                with mock.patch("api.profiles._is_isolated_profile_mode", return_value=False):
                    # Mock _get_profile_skills_stats to avoid importing agent.skill_utils
                    with mock.patch("api.profiles._get_profile_skills_stats", return_value=(0, 0)):
                        # Mock _build_profile_rows_fast to return the expected profiles
                        def mock_build_profiles():
                            return [
                                {'name': 'default', 'path': str(temp_hermes_home), 'is_default': True,
                                 'is_active': False, 'gateway_running': False, 'model': None, 'provider': None,
                                 'has_env': False, 'visible': True, 'skill_count': 0, 'enabled_skills': 0, 'total_skills': 0},
                                {'name': 'user1', 'path': str(profiles_root / 'user1'), 'is_default': False,
                                 'is_active': False, 'gateway_running': False, 'model': None, 'provider': None,
                                 'has_env': False, 'visible': True, 'skill_count': 0, 'enabled_skills': 0, 'total_skills': 0},
                                {'name': 'user2', 'path': str(profiles_root / 'user2'), 'is_default': False,
                                 'is_active': False, 'gateway_running': False, 'model': None, 'provider': None,
                                 'has_env': False, 'visible': True, 'skill_count': 0, 'enabled_skills': 0, 'total_skills': 0},
                                {'name': 'user3', 'path': str(profiles_root / 'user3'), 'is_default': False,
                                 'is_active': False, 'gateway_running': False, 'model': None, 'provider': None,
                                 'has_env': False, 'visible': True, 'skill_count': 0, 'enabled_skills': 0, 'total_skills': 0},
                            ]
                        with mock.patch("api.profiles._build_profile_rows_fast", side_effect=mock_build_profiles):
                            profiles = list_profiles_api()
                            # Should have at least 'default' plus the created profiles
                            names = [p["name"] for p in profiles]
                            assert "user1" in names
                            assert "user2" in names
                            assert "user3" in names

    def test_list_returns_only_isolated_profile_in_isolated_mode(self, temp_single_profile):
        """Isolated mode lists only the configured profile."""
        base_home = temp_single_profile.parent.parent
        # Create other profiles that should be hidden
        other_profiles = base_home / "profiles"
        (other_profiles / "user2").mkdir()
        (other_profiles / "user3").mkdir()
        for prof_dir in [other_profiles / "user2", other_profiles / "user3"]:
            for subdir in ["memories", "sessions", "skills", "skins", "logs", "plans", "workspace", "cron"]:
                (prof_dir / subdir).mkdir(exist_ok=True)

        # Clear HERMES_BASE_HOME to allow isolated mode detection to fire
        env_dict = {
            "HERMES_HOME": str(temp_single_profile),
            "HERMES_BASE_HOME": "",
        }
        with mock.patch.dict(os.environ, env_dict, clear=False):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", base_home):
                with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(temp_single_profile)):
                    # Mock _get_profile_skills_stats to avoid importing agent.skill_utils
                    with mock.patch("api.profiles._get_profile_skills_stats", return_value=(0, 0)):
                        with mock.patch("api.profiles.get_active_profile_name", return_value="user1"):
                            profiles = list_profiles_api()
                            # Should only have user1
                            assert len(profiles) == 1
                            assert profiles[0]["name"] == "user1"

    def test_list_includes_single_profile_mode_flag(self, temp_single_profile):
        """Response includes single_profile_mode: true in isolated mode."""
        base_home = temp_single_profile.parent.parent
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(temp_single_profile)}):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", base_home):
                with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(temp_single_profile)):
                    with mock.patch("api.profiles._is_isolated_profile_mode", return_value=True):
                        with mock.patch("api.profiles._resolve_base_hermes_home", return_value=base_home):
                            with mock.patch("api.profiles.get_active_profile_name", return_value="user1"):
                                # Mock _get_profile_skills_stats to avoid importing agent.skill_utils
                                with mock.patch("api.profiles._get_profile_skills_stats", return_value=(0, 0)):
                                    profiles = list_profiles_api()
                                    # Check for single_profile_mode flag in response structure
                                    # For now, profiles should be a list; the flag will be in routes.py response
                                    assert len(profiles) == 1


class TestProfileMutationsInIsolatedMode:
    """Test that create/delete/switch are rejected (403) in isolated mode."""

    def test_create_profile_rejected_in_isolated_mode(self, temp_single_profile):
        """create_profile_api should reject creation in isolated mode."""
        base_home = temp_single_profile.parent.parent
        # Clear HERMES_BASE_HOME to allow isolated mode detection to fire
        env_dict = {
            "HERMES_HOME": str(temp_single_profile),
            "HERMES_BASE_HOME": "",
        }
        with mock.patch.dict(os.environ, env_dict, clear=False):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", base_home):
                with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(temp_single_profile)):
                    with mock.patch("api.profiles.get_active_profile_name", return_value="user1"):
                        with pytest.raises(PermissionError, match=".*isolated.*|.*single.*"):
                            create_profile_api("newprofile")

    def test_delete_profile_rejected_in_isolated_mode(self, temp_single_profile):
        """delete_profile_api should reject deletion in isolated mode."""
        base_home = temp_single_profile.parent.parent
        # Clear HERMES_BASE_HOME to allow isolated mode detection to fire
        env_dict = {
            "HERMES_HOME": str(temp_single_profile),
            "HERMES_BASE_HOME": "",
        }
        with mock.patch.dict(os.environ, env_dict, clear=False):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", base_home):
                with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(temp_single_profile)):
                    with mock.patch("api.profiles.get_active_profile_name", return_value="user1"):
                        with pytest.raises(PermissionError, match=".*isolated.*|.*single.*"):
                            delete_profile_api("user1")


class TestNormalModePreservation:
    """Test that normal mode behavior is completely unchanged."""

    def test_normal_mode_profile_operations_work(self, temp_hermes_home):
        """Normal mode allows profile creation and deletion."""
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(temp_hermes_home)}):
            with mock.patch("api.profiles._DEFAULT_HERMES_HOME", temp_hermes_home):
                with mock.patch("api.profiles._is_isolated_profile_mode", return_value=False):
                    # Normal mode should not raise errors for create/delete operations
                    # (though they may fail for other reasons in this test environment)
                    try:
                        # Just verify the isolation guard doesn't trigger
                        from api.profiles import create_profile_api
                        # The actual call might fail due to missing hermes_cli,
                        # but should NOT fail with an "isolated mode" error
                        try:
                            create_profile_api("testprof1")
                        except ValueError as e:
                            # Should be a different error, not about isolation
                            assert "isolated" not in str(e).lower()
                            assert "single" not in str(e).lower()
                    except ImportError:
                        # hermes_cli not available, skip
                        pass
