"""Regression coverage for #7886 — delete-route static-fallback reachability.

``patch_skill_home_modules()`` rebinds ``SKILLS_DIR`` on the already-imported
Agent skill modules.  The Agent's resolver contract is::

    def _skills_dir():
        configured = Path(SKILLS_DIR)
        return configured if configured != _SKILLS_DIR_AT_IMPORT else get_hermes_home() / "skills"

so a ``SKILLS_DIR`` that no longer matches ``_SKILLS_DIR_AT_IMPORT`` reads as an
*explicit static override*: it stops following the context-local profile home
forever.  ``_skill_modules_support_profile_home()`` rejects that same mismatch, so
``_run_agent_streaming()`` and ``profile_env_for_background_worker()`` both fall
back to the whole-turn ``_SKILL_HOME_MODULE_PATCH_LOCK``.

A permanent, process-wide home change — startup and ``switch_profile(process_wide=True)``,
which ``POST /api/profile/delete`` reaches via ``switch_profile('default')`` — must
NOT leave that stale baseline behind.  Every later turn and model-catalog request
was serialized behind the static lock purely because of that leftover mismatch.

The whole-turn lock itself is left intact on purpose: it is still required
whenever a module is genuinely static, and the profile/credential isolation it
protects is not weakened here.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _live_home(fallback):
    """Resolve the home the way ``hermes_constants.get_hermes_home()`` does.

    The real ``_skills_dir()`` re-reads the *context-local* home on every call
    (override → ``HERMES_HOME`` env → platform default). A stand-in that closed
    over a fixed path would exercise a different function than the one that ships
    and would report dynamic modules as broken.
    """
    try:
        import hermes_constants as hc

        return Path(hc.get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME", "")
        return Path(env) if env else Path(fallback)


def _dynamic_skill_module(name, home):
    """A stand-in with the Agent's real import-baseline resolver contract."""
    module = types.ModuleType(name)
    module.HERMES_HOME = home
    module.SKILLS_DIR = home / "skills"
    module._SKILLS_DIR_AT_IMPORT = home / "skills"

    def _skills_dir():
        configured = Path(module.SKILLS_DIR)
        return (
            configured
            if configured != Path(module._SKILLS_DIR_AT_IMPORT)
            else _live_home(home) / "skills"
        )

    module._skills_dir = _skills_dir
    return module


def test_process_wide_home_change_realigns_skill_import_baseline(monkeypatch, tmp_path):
    """A process-wide home change must not leave a stale import baseline.

    The baseline is what makes the Agent treat ``SKILLS_DIR`` as an explicit
    static override; leaving it behind silently disables dynamic profile-home
    resolution for every subsequent turn.
    """
    from api.profiles import _set_hermes_home, _skill_modules_support_profile_home

    old_home = tmp_path / "base" / "profiles" / "alpha"
    new_home = tmp_path / "base"
    (old_home / "skills").mkdir(parents=True)
    new_home.mkdir(parents=True, exist_ok=True)

    skills_tool = _dynamic_skill_module("tools.skills_tool", old_home)
    skill_manager_tool = _dynamic_skill_module("tools.skill_manager_tool", old_home)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)
    monkeypatch.setenv("HERMES_HOME", str(old_home))

    assert _skill_modules_support_profile_home(old_home) is True, (
        "precondition: modules start out dynamically resolvable"
    )

    _set_hermes_home(new_home)

    for module in (skills_tool, skill_manager_tool):
        assert module.SKILLS_DIR == new_home / "skills"
        assert module._SKILLS_DIR_AT_IMPORT == new_home / "skills", (
            "process-wide home change left a stale import baseline; the Agent "
            "now reads SKILLS_DIR as an explicit static override"
        )

    assert _skill_modules_support_profile_home(new_home) is True, (
        "process-wide home change disabled dynamic skill resolution"
    )


def test_scoped_static_fallback_patch_still_creates_a_static_override(monkeypatch, tmp_path):
    """The legacy static-fallback patch must keep its static-override semantics.

    Guards the isolation contract the whole-turn lock exists for: a *scoped*
    patch is still authoritative, so a module under it reports the patched dir
    rather than following the context-local home.
    """
    from api.profiles import (
        _skill_modules_support_profile_home,
        patch_skill_home_modules,
    )

    process_home = tmp_path / "base"
    scope_home = tmp_path / "base" / "profiles" / "alpha"
    process_home.mkdir(parents=True)
    scope_home.mkdir(parents=True)

    skills_tool = _dynamic_skill_module("tools.skills_tool", process_home)
    skill_manager_tool = _dynamic_skill_module("tools.skill_manager_tool", process_home)
    monkeypatch.setitem(sys.modules, "tools.skills_tool", skills_tool)
    monkeypatch.setitem(sys.modules, "tools.skill_manager_tool", skill_manager_tool)

    patch_skill_home_modules(scope_home)

    for module in (skills_tool, skill_manager_tool):
        assert module.SKILLS_DIR == scope_home / "skills"
        assert module._skills_dir() == scope_home / "skills", (
            "scoped patch must stay authoritative for static-global readers"
        )

    # A static module cannot follow a different profile home, so the caller
    # must still choose the serialized fallback path for it.
    other_home = tmp_path / "base" / "profiles" / "beta"
    other_home.mkdir(parents=True)
    assert _skill_modules_support_profile_home(other_home) is False


def test_delete_active_profile_keeps_dynamic_skill_resolution():
    """Execute the real delete route with real Agent skill modules.

    This is the executed reproduction #7886 was waiting on: delete the
    process-active named profile in non-isolated mode with no active streams,
    then prove dynamic profile-home skill resolution survived.
    """
    agent_dir = os.environ.get("HERMES_WEBUI_AGENT_DIR")
    if not agent_dir:
        import importlib.util

        spec = importlib.util.find_spec("hermes_constants")
        if spec is None:
            import pytest

            pytest.skip("hermes-agent not installed")
        agent_dir = str(Path(spec.origin).parent)
    if not (Path(agent_dir) / "tools" / "skills_tool.py").is_file():
        import pytest

        pytest.skip("requires the hermes-agent source tree")

    python = os.environ.get("HERMES_WEBUI_PYTHON", sys.executable)
    # Build the disposable fixture in the parent so HERMES_HOME can point at it.
    tmp = Path(tempfile.mkdtemp(prefix="t7886-"))
    base = tmp / "base"
    alpha = base / "profiles" / "alpha"
    (alpha / "skills" / "alpha-only").mkdir(parents=True)
    (alpha / "skills" / "alpha-only" / "SKILL.md").write_text(
        "---\nname: alpha-only\ndescription: probe\n---\nprobe\n", encoding="utf-8"
    )
    (alpha / "config.yaml").write_text("{}\n", encoding="utf-8")
    (base / "profiles" / "default").mkdir(parents=True, exist_ok=True)
    (base / "active_profile").write_text("alpha", encoding="utf-8")

    script = textwrap.dedent('''
        import json, os, sys
        from pathlib import Path

        base = Path(os.environ["HERMES_BASE_HOME"])
        alpha = base / "profiles" / "alpha"

        import hermes_constants as hc
        from api import profiles
        import tools.skills_tool as skills
        import tools.skill_manager_tool as manager

        report = {
            "pre_dynamic": profiles._skill_modules_support_profile_home(alpha),
        }

        # The route under test: delete the process-active named profile.
        profiles._active_profile = "alpha"
        profiles.delete_profile_api("alpha")

        report["active_after"] = profiles._active_profile
        report["post_dynamic"] = profiles._skill_modules_support_profile_home(base)
        report["baseline_matches"] = (
            str(skills.SKILLS_DIR) == str(skills._SKILLS_DIR_AT_IMPORT)
            and str(manager.SKILLS_DIR) == str(manager._SKILLS_DIR_AT_IMPORT)
        )
        report["resolves_to_default"] = str(skills._skills_dir()) == str(base / "skills")

        # A later turn for another profile must stay dynamically resolvable,
        # i.e. it must not be pushed onto the whole-turn static lock. The real
        # caller (profile_env_for_background_worker) installs the context-local
        # override *before* asking about capability, so mirror that order.
        beta = base / "profiles" / "beta"
        (beta / "skills").mkdir(parents=True)
        (beta / "config.yaml").write_text("{}\\n", encoding="utf-8")
        token = hc.set_hermes_home_override(beta)
        try:
            report["post_dynamic_beta"] = profiles._skill_modules_support_profile_home(beta)
        finally:
            hc.reset_hermes_home_override(token)

        print("REPORT " + json.dumps(report))
    ''')
    result = subprocess.run(
        [python, "-c", script],
        capture_output=True,
        text=True,
        timeout=300,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp),
            # The Agent resolves named profiles under HERMES_HOME, so the
            # fixture must be reachable from there.
            "HERMES_HOME": str(alpha),
            "HERMES_BASE_HOME": str(base),
            "HERMES_WEBUI_STATE_DIR": str(tmp / "webui"),
            "HERMES_RUNTIME_DIR": str(tmp / "runtime"),
            "HERMES_WEBUI_AGENT_DIR": agent_dir,
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "PYTEST_CURRENT_TEST": "issue 7886 delete route",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join([str(ROOT), agent_dir]),
        },
    )
    stdout = result.stdout
    marker = "REPORT "
    assert marker in stdout, (
        f"probe did not report state\nrc={result.returncode}\n"
        f"stdout={stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
    )
    payload = stdout.split(marker)[-1].splitlines()[0]
    report = json.loads(payload)

    assert report["pre_dynamic"] is True, f"precondition failed: {report}"
    assert report["active_after"] == "default", f"delete did not switch home: {report}"
    assert report["baseline_matches"] is True, (
        "delete route left a stale skill import baseline, so every later turn "
        f"is forced onto the whole-turn static lock: {report}"
    )
    assert report["resolves_to_default"] is True, f"skills did not follow new home: {report}"
    assert report["post_dynamic"] is True, (
        "delete route disabled dynamic skill resolution: " f"{report}"
    )
    assert report["post_dynamic_beta"] is True, (
        "a later profile's turn would be serialized behind the static lock: "
        f"{report}"
    )