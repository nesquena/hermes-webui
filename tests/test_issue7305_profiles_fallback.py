"""#7305: GET /api/profiles must never 500 when agent.skill_utils is not
importable (two-container Docker without the hermes-agent source mounted)."""

from __future__ import annotations

import builtins
import importlib

import pytest

import api.profiles as profiles


def _profile_dir(tmp_path, skill_name="my-skill"):
    d = tmp_path / "home"
    skills = d / "skills"
    (skills / skill_name).mkdir(parents=True)
    (skills / skill_name / "SKILL.md").write_text(
        "---\nname: " + skill_name + "\ndescription: test skill\n---\nbody\n",
        encoding="utf-8",
    )
    return d


def test_skills_stats_works_without_agent_skill_utils(tmp_path):
    """The fallback local scan must serve the count when the agent module is
    absent — no ImportError escapes into GET /api/profiles."""
    d = _profile_dir(tmp_path)
    real_import = builtins.__import__

    def no_agent(name, *args, **kwargs):
        if name == "agent.skill_utils" or name.startswith("agent."):
            raise ImportError("agent not mounted (simulated Docker)")
        return real_import(name, *args, **kwargs)

    importlib.reload(profiles)  # ensure the module-level agent imports are re-evaluated
    try:
        # Patch the import used by _get_profile_skills_stats' compute path.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(builtins, "__import__", no_agent)
            enabled, compatible = profiles._get_profile_skills_stats(d)
    finally:
        pass
    assert compatible >= 1, "must count at least the local SKILL.md without agent.skill_utils"
    assert enabled >= 1


def test_default_profile_dict_never_raises_without_agent(tmp_path):
    """_default_profile_dict() (the fallback row for GET /api/profiles) must
    return a dict even with no agent module available."""
    d = _profile_dir(tmp_path)
    orig = profiles._DEFAULT_HERMES_HOME
    try:
        profiles._DEFAULT_HERMES_HOME = d
        row = profiles._default_profile_dict()
    finally:
        profiles._DEFAULT_HERMES_HOME = orig
    assert row["name"] == "default"
    assert row["skill_count"] >= 1
