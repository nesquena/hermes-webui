"""Tests for rename/duplicate profile management actions.

Plan reference: Phase 1B. These tests exercise the API helpers directly
(not via HTTP) because they have no third-party dependencies and run fast.
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
    os.environ["HERMES_BASE_HOME"] = str(base_home)
    os.environ["HERMES_HOME"] = str(base_home)

    _saved = {name: sys.modules[name] for name in ["api.config", "api.profiles"]
              if name in sys.modules}
    for name in ["api.config", "api.profiles"]:
        if name in sys.modules:
            del sys.modules[name]

    profiles = importlib.import_module("api.profiles")
    sys.modules.update(_saved)
    api_pkg = sys.modules.get("api")
    if api_pkg is not None:
        for name, module in _saved.items():
            setattr(api_pkg, name.rsplit(".", 1)[-1], module)
    return profiles


def _seed_named_profile(base: Path, name: str) -> Path:
    profile_dir = base / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text("model:\n  default: gpt-5\n", encoding="utf-8")
    return profile_dir


# ── rename ──────────────────────────────────────────────────────────────────

def test_rename_refuses_default():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.rename_profile_api("default", "renamed")


def test_rename_refuses_traversal():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.rename_profile_api("coder", "../escape")


def test_rename_refuses_same_name():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.rename_profile_api("coder", "coder")


def test_rename_refuses_existing_destination():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        _seed_named_profile(base, "planner")
        profiles = _reload_profiles_module(base)
        with pytest.raises(FileExistsError):
            profiles.rename_profile_api("coder", "planner")


def test_rename_fallback_moves_directory():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        src = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        result = profiles.rename_profile_api("coder", "engineer")
        assert result == {"ok": True, "old_name": "coder", "new_name": "engineer", "was_active": False}
        assert not src.exists()
        assert (base / "profiles" / "engineer" / "config.yaml").exists()


# ── duplicate ───────────────────────────────────────────────────────────────

def test_duplicate_refuses_same_name():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.duplicate_profile_api("coder", "coder")


def test_duplicate_refuses_existing_destination():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        _seed_named_profile(base, "planner")
        profiles = _reload_profiles_module(base)
        with pytest.raises(FileExistsError):
            profiles.duplicate_profile_api("coder", "planner")


def test_duplicate_fallback_copies_config():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        result = profiles.duplicate_profile_api("coder", "coder-copy")
        assert isinstance(result, dict)
        copied_dir = base / "profiles" / "coder-copy"
        assert copied_dir.is_dir()
        assert (copied_dir / "config.yaml").exists()
        assert "gpt-5" in (copied_dir / "config.yaml").read_text(encoding="utf-8")


# ── 32-char cap on new profile names (rework v3.1) ──────────────────────────


def test_create_refuses_name_over_32_chars():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        too_long = "a" * 33  # one past the cap
        with pytest.raises(ValueError, match="32 characters"):
            profiles.create_profile_api(too_long)


def test_create_accepts_name_exactly_32_chars():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        # The regex permits this; the cap permits this; both should agree.
        exactly_32 = "a" * 32
        # hermes_cli is not available in tests, so the fallback runs and
        # creates the directory directly — no exception means success.
        profiles.create_profile_api(exactly_32)
        assert (base / "profiles" / exactly_32).is_dir()


def test_rename_refuses_name_over_32_chars():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        too_long = "a" * 33
        with pytest.raises(ValueError, match="32 characters"):
            profiles.rename_profile_api("coder", too_long)


def test_duplicate_refuses_name_over_32_chars():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        too_long = "a" * 33
        with pytest.raises(ValueError, match="32 characters"):
            profiles.duplicate_profile_api("coder", too_long)
