"""Regression coverage for issue #2023.

Process-wide profile switches must keep both skill tool modules pointed at the
active profile home.  The modules live in hermes-agent and may not be importable
in this test environment, so the test injects lightweight stand-ins into
``sys.modules``.
"""
import sys
import types

import pytest


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


def _shared_resolver_modules(import_home, live_home):
    """skills_tool / skill_manager_tool laid out as on Agents with upstream 17b953d8b7cd.

    skills_tool keeps HERMES_HOME, SKILLS_DIR, _SKILLS_DIR_AT_IMPORT and a ``_skills_dir()``
    that follows the live home unless SKILLS_DIR was patched; the manager has none of
    those globals and does ``from tools.skills_tool import _skills_dir``.
    """
    skills_tool = _skill_module("tools.skills_tool", import_home)
    skills_tool._SKILLS_DIR_AT_IMPORT = skills_tool.SKILLS_DIR

    def _skills_dir():
        configured = skills_tool.SKILLS_DIR
        return configured if configured != skills_tool._SKILLS_DIR_AT_IMPORT else live_home / "skills"

    skills_tool._skills_dir = _skills_dir
    skill_manager_tool = types.ModuleType("tools.skill_manager_tool")
    skill_manager_tool._skills_dir = skills_tool._skills_dir
    return skills_tool, skill_manager_tool


def _own_skills_dir_manager(import_home, live_home):
    """skill_manager_tool as on Agents before 17b953d8b7cd: its own globals and resolver."""
    manager = _skill_module("tools.skill_manager_tool", import_home)
    manager._SKILLS_DIR_AT_IMPORT = manager.SKILLS_DIR
    manager._skills_dir = lambda: (
        manager.SKILLS_DIR if manager.SKILLS_DIR != manager._SKILLS_DIR_AT_IMPORT else live_home / "skills"
    )
    return manager


def _unrecognised(name):
    def _shape(skills_tool, manager, live_home):
        if name == "manager_has_different_resolver":
            # Same answer as skills_tool, but not the same function: not the known shape.
            manager._skills_dir = lambda: live_home / "skills"
        elif name == "manager_has_skills_dir_only":
            manager.SKILLS_DIR = skills_tool.SKILLS_DIR
        elif name == "manager_has_hermes_home":
            manager.HERMES_HOME = skills_tool.HERMES_HOME
        elif name == "manager_has_import_baseline_only":
            manager._SKILLS_DIR_AT_IMPORT = skills_tool.SKILLS_DIR
        elif name == "manager_has_no_resolver":
            del manager._skills_dir
        elif name == "skills_tool_patched":
            skills_tool.SKILLS_DIR = live_home / "elsewhere"
        elif name == "skills_tool_missing_import_baseline":
            del skills_tool._SKILLS_DIR_AT_IMPORT
        else:  # pragma: no cover - parametrization typo
            raise AssertionError(name)
    return _shape


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("shared_resolver", True),
        ("own_skills_dir", True),
        ("manager_has_different_resolver", False),
        ("manager_has_skills_dir_only", False),
        ("manager_has_hermes_home", False),
        ("manager_has_import_baseline_only", False),
        ("manager_has_no_resolver", False),
        ("skills_tool_patched", False),
        ("skills_tool_missing_import_baseline", False),
    ],
)
def test_skill_modules_support_profile_home_by_agent_module_shape(monkeypatch, tmp_path, shape, expected):
    """Only the two known Agent layouts count as profile-home capable; anything else keeps the lock."""
    from api.profiles import _skill_modules_support_profile_home

    import_home = tmp_path / "import-home"
    profile_home = tmp_path / "profile"
    skills_tool, manager = _shared_resolver_modules(import_home, profile_home)
    if shape == "own_skills_dir":
        manager = _own_skills_dir_manager(import_home, profile_home)
    elif shape != "shared_resolver":
        _unrecognised(shape)(skills_tool, manager, profile_home)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", manager)

    assert _skill_modules_support_profile_home(profile_home) is expected
    # Another profile's home never qualifies: the resolvers must answer for this one.
    assert _skill_modules_support_profile_home(tmp_path / "other") is False


def test_skill_home_patch_and_restore_leave_shared_resolver_manager_without_globals(monkeypatch, tmp_path):
    """The legacy fallback (and _set_hermes_home, which calls the same patch) rebinds
    skills_tool only; the manager follows it and gains no globals."""
    from api.profiles import (
        _skill_modules_support_profile_home,
        patch_skill_home_modules,
        restore_skill_home_modules,
        snapshot_skill_home_modules,
    )

    import_home = tmp_path / "import-home"
    profile_home = tmp_path / "profile"
    skills_tool, manager = _shared_resolver_modules(import_home, import_home)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", manager)
    manager_globals = set(vars(manager))

    snapshot = snapshot_skill_home_modules()
    patch_skill_home_modules(profile_home)

    assert skills_tool.HERMES_HOME == profile_home
    assert skills_tool.SKILLS_DIR == profile_home / "skills"
    assert manager._skills_dir() == profile_home / "skills"
    assert set(vars(manager)) == manager_globals

    restore_skill_home_modules(snapshot)

    assert skills_tool.HERMES_HOME == import_home
    assert skills_tool.SKILLS_DIR == skills_tool._SKILLS_DIR_AT_IMPORT == import_home / "skills"
    assert set(vars(manager)) == manager_globals
    assert _skill_modules_support_profile_home(import_home) is True
