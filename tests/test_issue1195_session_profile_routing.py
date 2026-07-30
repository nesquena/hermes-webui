"""Tests for issue #1195: sessions must route to the correct profile directory
even when that profile directory does not exist yet on disk."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_hermes_home(base: Path, profile_name: str | None = None) -> Path:
    """Create a temp HERMES_HOME (with optional profile dir) and return it."""
    hermes_home = base / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    if profile_name:
        (hermes_home / "profiles" / profile_name).mkdir(parents=True, exist_ok=True)
    return hermes_home


# ── tests ────────────────────────────────────────────────────────────────────

class TestGetHermesHomeForProfile:
    """get_hermes_home_for_profile() must return the profile path regardless of
    whether the directory already exists on disk (#1195)."""

    @pytest.fixture(autouse=True)
    def _patch_default_home(self, tmp_path):
        """Patch _DEFAULT_HERMES_HOME to a temp directory for isolation."""
        from api.profiles import _DEFAULT_HERMES_HOME as real_default

        fake_home = tmp_path / ".hermes"
        fake_home.mkdir(parents=True)
        with patch("api.profiles._DEFAULT_HERMES_HOME", fake_home):
            yield fake_home, real_default

    def test_existing_profile_returns_profile_dir(self, _patch_default_home):
        fake_home, _ = _patch_default_home
        from api.profiles import get_hermes_home_for_profile

        # Create an existing profile directory
        profile_dir = fake_home / "profiles" / "ayan"
        profile_dir.mkdir(parents=True)

        result = get_hermes_home_for_profile("ayan")
        assert result == profile_dir

    def test_nonexistent_profile_still_returns_profile_path(self, _patch_default_home):
        """Core bug fix: profile dir doesn't exist yet but should still route there."""
        fake_home, _ = _patch_default_home
        from api.profiles import get_hermes_home_for_profile

        # Do NOT create the profile directory
        expected = fake_home / "profiles" / "newprofile"
        assert not expected.exists()  # confirm it doesn't exist

        result = get_hermes_home_for_profile("newprofile")
        assert result == expected, "Should route to profile path even when dir missing"

    def test_none_returns_default(self, _patch_default_home):
        fake_home, _ = _patch_default_home
        from api.profiles import get_hermes_home_for_profile

        result = get_hermes_home_for_profile(None)
        assert result == fake_home

    def test_empty_string_returns_default(self, _patch_default_home):
        fake_home, _ = _patch_default_home
        from api.profiles import get_hermes_home_for_profile

        result = get_hermes_home_for_profile("")
        assert result == fake_home

    def test_default_string_returns_default(self, _patch_default_home):
        fake_home, _ = _patch_default_home
        from api.profiles import get_hermes_home_for_profile

        result = get_hermes_home_for_profile("default")
        assert result == fake_home
