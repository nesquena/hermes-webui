"""Regression coverage for #2305 — new profiles created without clone get bundled skills."""

import json
import os
import pathlib
import shutil
import urllib.error
import urllib.request

import pytest

import api.profiles as profiles


REPO = pathlib.Path(__file__).resolve().parent.parent


class TestProfileCreateSkillSeeding:
    """Verify that create_profile_api seeds bundled skills for fresh profiles."""

    def test_create_profile_api_seeds_skills_when_clone_from_none(self, monkeypatch, tmp_path):
        """Fresh profile (clone_from=None) should call seed_profile_skills."""
        base = tmp_path / ".hermes"
        monkeypatch.setenv("HERMES_BASE_HOME", str(base))
        monkeypatch.delenv("HERMES_HOME", raising=False)

        # Ensure hermes_cli is not importable so we hit the fallback path,
        # then simulate seed_profile_skills being available by monkeypatching
        # the import in create_profile_api.
        seed_calls = []

        def fake_seed(profile_dir, quiet=False):
            seed_calls.append((str(profile_dir), quiet))
            # Create a dummy skill file so the profile looks seeded
            skills_dir = pathlib.Path(profile_dir) / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "test-skill-2305").mkdir(parents=True, exist_ok=True)
            (skills_dir / "test-skill-2305" / "SKILL.md").write_text(
                "---\nname: test-skill-2305\n---\n", encoding="utf-8"
            )

        # Patch the hermes_cli.profiles module in sys.modules
        import sys
        fake_module = type(sys)("hermes_cli.profiles")
        fake_module.seed_profile_skills = fake_seed
        fake_module.create_profile = lambda *args, **kwargs: None
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_module)

        # Also need hermes_cli itself
        if "hermes_cli" not in sys.modules:
            monkeypatch.setitem(sys.modules, "hermes_cli", type(sys)("hermes_cli"))

        # Re-import api.profiles to pick up the patched environment
        monkeypatch.delitem(sys.modules, "api.profiles", raising=False)
        import importlib
        profiles_mod = importlib.import_module("api.profiles")

        result = profiles_mod.create_profile_api("test-seed-2305", clone_from=None)

        assert len(seed_calls) == 1, f"Expected 1 seed call, got {len(seed_calls)}"
        assert "test-seed-2305" in seed_calls[0][0]
        assert seed_calls[0][1] is True  # quiet=True

    def test_create_profile_api_skips_seed_when_clone_from_set(self, monkeypatch, tmp_path):
        """Cloned profile should NOT call seed_profile_skills (skills copied from source)."""
        base = tmp_path / ".hermes"
        (base / "profiles" / "source" / "skills" / "existing-skill").mkdir(parents=True)
        (base / "profiles" / "source" / "skills" / "existing-skill" / "SKILL.md").write_text(
            "---\nname: existing-skill\n---\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_BASE_HOME", str(base))
        monkeypatch.delenv("HERMES_HOME", raising=False)

        seed_calls = []

        def fake_seed(profile_dir, quiet=False):
            seed_calls.append((str(profile_dir), quiet))

        import sys
        fake_module = type(sys)("hermes_cli.profiles")
        fake_module.seed_profile_skills = fake_seed
        fake_module.create_profile = lambda *args, **kwargs: None
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_module)

        if "hermes_cli" not in sys.modules:
            monkeypatch.setitem(sys.modules, "hermes_cli", type(sys)("hermes_cli"))

        monkeypatch.delitem(sys.modules, "api.profiles", raising=False)
        import importlib
        profiles_mod = importlib.import_module("api.profiles")

        result = profiles_mod.create_profile_api("test-clone-2305", clone_from="source", clone_config=True)

        assert len(seed_calls) == 0, f"Expected 0 seed calls for cloned profile, got {len(seed_calls)}"

    def test_seed_failure_is_non_fatal(self, monkeypatch, tmp_path):
        """If seed_profile_skills raises, profile creation should still succeed."""
        base = tmp_path / ".hermes"
        monkeypatch.setenv("HERMES_BASE_HOME", str(base))
        monkeypatch.delenv("HERMES_HOME", raising=False)

        def failing_seed(profile_dir, quiet=False):
            raise RuntimeError("Simulated seed failure")

        import sys
        fake_module = type(sys)("hermes_cli.profiles")
        fake_module.seed_profile_skills = failing_seed
        fake_module.create_profile = lambda *args, **kwargs: None
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake_module)

        if "hermes_cli" not in sys.modules:
            monkeypatch.setitem(sys.modules, "hermes_cli", type(sys)("hermes_cli"))

        monkeypatch.delitem(sys.modules, "api.profiles", raising=False)
        import importlib
        profiles_mod = importlib.import_module("api.profiles")

        # Should not raise
        result = profiles_mod.create_profile_api("test-fail-2305", clone_from=None)

        assert result["name"] == "test-fail-2305"
        assert result["skill_count"] == 0  # Empty because seed failed


class TestProfileCreateSkillSeedingStatic:
    """Static analysis — verify the seed call exists in the source."""

    def test_create_profile_api_contains_seed_call(self):
        src = (REPO / "api" / "profiles.py").read_text(encoding="utf-8")
        assert "seed_profile_skills" in src, (
            "api/profiles.py must call seed_profile_skills for #2305"
        )

    def test_seed_is_conditional_on_clone_from_none(self):
        src = (REPO / "api" / "profiles.py").read_text(encoding="utf-8")
        # Find the create_profile_api function
        fn_start = src.find("def create_profile_api(")
        assert fn_start != -1
        fn_end = src.find("\ndef ", fn_start + 1)
        if fn_end == -1:
            fn_end = len(src)
        fn_body = src[fn_start:fn_end]

        assert "if clone_from is None:" in fn_body, (
            "seed_profile_skills must be gated on clone_from is None"
        )
        assert "seed_profile_skills" in fn_body

    def test_seed_failure_wrapped_in_try_except(self):
        src = (REPO / "api" / "profiles.py").read_text(encoding="utf-8")
        fn_start = src.find("def create_profile_api(")
        assert fn_start != -1
        fn_end = src.find("\ndef ", fn_start + 1)
        if fn_end == -1:
            fn_end = len(src)
        fn_body = src[fn_start:fn_end]

        assert "except ImportError:" in fn_body, (
            "seed call must handle hermes_cli being unavailable"
        )
        assert "except Exception" in fn_body, (
            "seed call must handle general failures non-fatally"
        )
