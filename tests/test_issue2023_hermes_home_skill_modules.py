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


def test_skill_modules_support_profile_home_returns_true_for_dynamic_modules(monkeypatch, tmp_path):
    from api.profiles import _skill_modules_support_profile_home

    profile_home = tmp_path / "profile"
    expected = profile_home / "skills"
    baseline = tmp_path / "base" / "skills"

    skills_tool = types.ModuleType("tools.skills_tool")
    skills_tool.HERMES_HOME = profile_home.parent
    skills_tool.SKILLS_DIR = baseline
    skills_tool._SKILLS_DIR_AT_IMPORT = baseline
    skills_tool._skills_dir = lambda: expected
    skill_manager_tool = types.ModuleType("tools.skill_manager_tool")
    skill_manager_tool.HERMES_HOME = profile_home.parent
    skill_manager_tool.SKILLS_DIR = baseline
    skill_manager_tool._SKILLS_DIR_AT_IMPORT = baseline
    skill_manager_tool._skills_dir = lambda: expected

    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)

    assert _skill_modules_support_profile_home(profile_home) is True


def test_skill_modules_support_profile_home_returns_false_when_already_globally_patched(monkeypatch, tmp_path):
    from api.profiles import _skill_modules_support_profile_home

    profile_home = tmp_path / "profile"
    expected = profile_home / "skills"
    baseline = tmp_path / "base" / "skills"
    patched = tmp_path / "alpha" / "skills"

    skills_tool = types.ModuleType("tools.skills_tool")
    skills_tool.HERMES_HOME = profile_home.parent
    skills_tool.SKILLS_DIR = patched
    skills_tool._SKILLS_DIR_AT_IMPORT = baseline
    skills_tool._skills_dir = lambda: expected

    skill_manager_tool = types.ModuleType("tools.skill_manager_tool")
    skill_manager_tool.HERMES_HOME = profile_home.parent
    skill_manager_tool.SKILLS_DIR = baseline
    skill_manager_tool._SKILLS_DIR_AT_IMPORT = baseline
    skill_manager_tool._skills_dir = lambda: expected

    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)

    assert _skill_modules_support_profile_home(profile_home) is False


def test_skill_modules_support_profile_home_returns_false_when_module_is_static(monkeypatch, tmp_path):
    from api.profiles import _skill_modules_support_profile_home

    profile_home = tmp_path / "profile"
    expected = profile_home / "skills"
    baseline = tmp_path / "base" / "skills"

    skills_tool = types.ModuleType("tools.skills_tool")
    skills_tool.SKILLS_DIR = baseline
    skills_tool._SKILLS_DIR_AT_IMPORT = baseline
    skills_tool._skills_dir = expected
    skill_manager_tool = types.ModuleType("tools.skill_manager_tool")
    skill_manager_tool.SKILLS_DIR = baseline
    skill_manager_tool._SKILLS_DIR_AT_IMPORT = baseline
    skill_manager_tool._skills_dir = lambda: expected

    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)

    assert _skill_modules_support_profile_home(profile_home) is False


def test_skill_modules_support_profile_home_returns_false_when_resolver_raises(monkeypatch, tmp_path):
    from api.profiles import _skill_modules_support_profile_home

    profile_home = tmp_path / "profile"
    expected = profile_home / "skills"
    baseline = tmp_path / "base" / "skills"

    def _raise():
        raise RuntimeError("not callable")

    skills_tool = types.ModuleType("tools.skills_tool")
    skills_tool.SKILLS_DIR = baseline
    skills_tool._SKILLS_DIR_AT_IMPORT = baseline
    skills_tool._skills_dir = _raise
    skill_manager_tool = types.ModuleType("tools.skill_manager_tool")
    skill_manager_tool.SKILLS_DIR = baseline
    skill_manager_tool._SKILLS_DIR_AT_IMPORT = baseline
    skill_manager_tool._skills_dir = lambda: expected

    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)

    assert _skill_modules_support_profile_home(profile_home) is False


def test_skill_modules_support_profile_home_returns_false_when_module_missing(monkeypatch, tmp_path):
    from api.profiles import _skill_modules_support_profile_home

    profile_home = tmp_path / "profile"
    expected = profile_home / "skills"
    baseline = tmp_path / "base" / "skills"

    skill_manager_tool = types.ModuleType("tools.skill_manager_tool")
    skill_manager_tool.SKILLS_DIR = baseline
    skill_manager_tool._SKILLS_DIR_AT_IMPORT = baseline
    skill_manager_tool._skills_dir = lambda: expected

    monkeypatch.delitem(sys.modules, "tools.skills_tool", raising=False)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)

    assert _skill_modules_support_profile_home(profile_home) is False
