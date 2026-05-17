"""Regression coverage for issue #2023.

Process-wide profile switches must keep both skill tool modules pointed at the
active profile home.  The modules live in hermes-agent and may not be importable
in this test environment, so the test injects lightweight stand-ins into
``sys.modules``.
"""
import sys
import types


def _skill_module(name, home):
    module = types.ModuleType(name)
    module.HERMES_HOME = home
    module.SKILLS_DIR = home / "skills"
    return module


def test_set_hermes_home_patches_both_skill_tool_module_caches(monkeypatch, tmp_path):
    from api.profiles import _set_hermes_home

    old_home = tmp_path / "old-home"
    new_home = tmp_path / "new-home"
    skills_tool = _skill_module("tools.skills_tool", old_home)
    skill_manager_tool = _skill_module("tools.skill_manager_tool", old_home)

    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)

    _set_hermes_home(new_home)

    assert skills_tool.HERMES_HOME == new_home
    assert skills_tool.SKILLS_DIR == new_home / "skills"
    assert skill_manager_tool.HERMES_HOME == new_home
    assert skill_manager_tool.SKILLS_DIR == new_home / "skills"
