"""Regression tests for issue #1195.

When the WebUI creates a session for a profile whose directory doesn't exist yet
(e.g. 'ayan' in ~/.hermes/profiles/ayan/), the session must be routed to that
profile's sessions dir — NOT to the default ~/.hermes/ dir.  Otherwise sessions
created in WebUI for 'ayan' appear in TUI under 'awen' or 'default', breaking
profile isolation.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_profiles_module(base_home: Path):
    """Hot-reload api.profiles with a fresh _DEFAULT_HERMES_HOME."""
    os.environ["HERMES_BASE_HOME"] = str(base_home)
    os.environ["HERMES_HOME"] = str(base_home)

    # Save and remove so we get a fresh import
    _saved = {name: sys.modules[name] for name in ["api.config", "api.profiles"]
              if name in sys.modules}
    for name in ["api.config", "api.profiles"]:
        if name in sys.modules:
            del sys.modules[name]

    profiles = importlib.import_module("api.profiles")

    # Restore so other tests aren't broken
    sys.modules.update(_saved)
    return profiles


def test_get_hermes_home_routes_to_profile_dir_when_dir_missing():
    """Issue #1195: get_hermes_home_for_profile should return the profile dir
    even when that directory does not exist yet.

    WebUI can create sessions for a profile before TUI has ever initialised it.
    The routing must NOT fallback to default home in that case — otherwise
    session files end up in the wrong profile directory.
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        base.mkdir(parents=True)
        # Deliberately NO profiles/ayan subdir — simulating a WebUI-only profile

        profiles = _reload_profiles_module(base)

        result = profiles.get_hermes_home_for_profile("ayan")
        expected = base / "profiles" / "ayan"

        assert result == expected, (
            f"Expected {expected}, got {result}. "
            "Sessions for 'ayan' would be saved to the wrong directory!"
        )


def test_get_hermes_home_routes_to_existing_profile_dir():
    """Existing profile dir should still be returned as-is."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        base.mkdir(parents=True)
        profile_dir = base / "profiles" / "ayan"
        profile_dir.mkdir(parents=True)

        profiles = _reload_profiles_module(base)
        result = profiles.get_hermes_home_for_profile("ayan")

        assert result == profile_dir


def test_default_profile_routes_to_default_home():
    """'default' profile should always route to _DEFAULT_HERMES_HOME."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        base.mkdir(parents=True)

        profiles = _reload_profiles_module(base)
        result = profiles.get_hermes_home_for_profile("default")

        assert result == base


def test_none_empty_profile_routes_to_default_home():
    """None / empty profile should route to default home."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        base.mkdir(parents=True)

        profiles = _reload_profiles_module(base)

        for name in [None, "", "default"]:
            result = profiles.get_hermes_home_for_profile(name)
            assert result == base, f"Profile {name!r} should route to default home"


def test_path_traversal_still_rejected():
    """Path traversal attempts must still be rejected (security regression guard)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        base.mkdir(parents=True)

        profiles = _reload_profiles_module(base)

        for malicious in ["../../etc", "../..", "%2e%2e/etc"]:
            result = profiles.get_hermes_home_for_profile(malicious)
            assert result == base, (
                f"Path traversal {malicious!r} should be rejected and fallback to default"
            )
