"""Backend regressions for WebUI write-approval profile binding."""

import json
import os
import threading
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def real_agent_modules():
    """Exercise disk-backed Agent modules when installed; CI need not install Agent."""
    # Import WebUI first so its normal Agent discovery has run.
    from api import commands  # noqa: F401

    for name in (
        "hermes_constants", "tools.write_approval",
        "hermes_cli.write_approval_commands", "tools.memory_tool",
    ):
        module = pytest.importorskip(name)
        assert getattr(module, "__file__", None), f"{name} must not be a test stub"
        assert Path(module.__file__).is_file()


@pytest.mark.parametrize("subsystem", ["skills", "memory"])
def test_root_approval_command_ignores_mirrored_named_worker_home(
    monkeypatch, tmp_path, subsystem
):
    """A root request must not read or reject a named worker's pending record."""
    from api import commands, profiles
    import sys
    import types

    # The production server has api.streaming loaded before background workers
    # run. Importing it lazily in this focused test boots the installed Agent;
    # provide only its shared env lock so this test isolates profile routing.
    if "api.streaming" not in sys.modules:
        streaming_stub = types.ModuleType("api.streaming")
        streaming_stub.__dict__["_ENV_LOCK"] = threading.Lock()
        monkeypatch.setitem(sys.modules, "api.streaming", streaming_stub)

    root_home = tmp_path / "root"
    named_home = root_home / "profiles" / "worker"
    root_pending = root_home / "pending" / subsystem / "root-record.json"
    named_pending = named_home / "pending" / subsystem / "named-record.json"
    record = {
        "id": "record",
        "subsystem": subsystem,
        "action": "add",
        "summary": "disposable test record",
        "origin": "foreground",
        "created_at": 1.0,
        "payload": {"action": "add", "target": "memory", "content": "test"},
    }
    for path, pending_id in ((root_pending, "root-record"), (named_pending, "named-record")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**record, "id": pending_id}), encoding="utf-8")

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setenv("HERMES_HOME", str(root_home))
    entered = threading.Event()
    release = threading.Event()
    worker_errors = []

    def named_worker():
        from hermes_constants import get_hermes_home

        try:
            with profiles.profile_env_for_background_worker(
                "worker", "issue-7629-worker", scope_skill_modules=False,
            ):
                assert get_hermes_home() == named_home
                assert os.environ["HERMES_HOME"] == str(named_home)
                entered.set()
                assert release.wait(10), "root request did not finish"
                assert get_hermes_home() == named_home
        except BaseException as exc:
            worker_errors.append(exc)
            entered.set()

    worker = threading.Thread(target=named_worker, daemon=True)
    worker.start()
    profiles.set_request_profile("default")
    try:
        assert entered.wait(10), "named worker did not enter its profile scope"
        assert not worker_errors
        assert worker.is_alive()
        pending_output = commands.execute_agent_command(f"/{subsystem} pending")
        assert "root-record" in pending_output
        assert "named-record" not in pending_output

        reject_output = commands.execute_agent_command(f"/{subsystem} reject all")
        assert "Rejected" in reject_output
        # Root request binding must not change the worker's process mirror.
        assert os.environ["HERMES_HOME"] == str(named_home)
    finally:
        profiles.clear_request_profile()
        release.set()
        worker.join(10)

    assert not worker.is_alive()
    assert not worker_errors
    assert os.environ["HERMES_HOME"] == str(root_home)
    assert not root_pending.exists()
    assert named_pending.exists(), "the named worker's pending record must be untouched"



def test_real_readonly_profile_binding_restores_agent_home_after_scope(monkeypatch, tmp_path):
    """The installed Agent home override is active only for the command scope."""
    from api import profiles
    from hermes_constants import get_hermes_home

    root_home = tmp_path / "root"
    named_home = tmp_path / "profiles" / "worker"
    root_home.mkdir(parents=True)
    named_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setenv("HERMES_HOME", str(named_home))
    profiles.set_request_profile("default")
    try:
        assert get_hermes_home() == named_home
        with profiles.profile_env_for_active_request_readonly(
            "issue-7629-test", include_root=True
        ) as bound:
            assert bound is True
            assert get_hermes_home() == root_home
        assert get_hermes_home() == named_home
    finally:
        profiles.clear_request_profile()


@pytest.mark.requires_agent_modules
def test_skill_approval_after_deleting_startup_profile_writes_to_request_profile(
    monkeypatch, request, tmp_path
):
    """A stale Agent SKILLS_DIR after profile deletion must not redirect approval writes."""
    import hermes_constants
    from api import config, commands, profiles
    from hermes_constants import get_hermes_home
    from hermes_cli import profiles as agent_profiles
    from tools import write_approval
    import tools.skills_tool as skills_tool
    import tools.skill_manager_tool as skill_manager

    # delete_profile_api() switches the active profile, and _pin_process_profile_home
    # pins the Agent's process home to this test's tmp root. Restore that pin too,
    # or every later test resolves get_hermes_home() to the deleted tmp tree.
    monkeypatch.setattr(
        hermes_constants,
        "_PINNED_PROCESS_HERMES_HOME",
        getattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None),
        raising=False,
    )

    skill_module_snapshot = profiles.snapshot_skill_home_modules()
    request.addfinalizer(lambda: profiles.restore_skill_home_modules(skill_module_snapshot))
    root_home = tmp_path / "root"
    startup_home = root_home / "profiles" / "startup"
    request_home = root_home / "profiles" / "profile-a"
    for home in (root_home, startup_home, request_home):
        home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(root_home))
    monkeypatch.setattr(profiles, "_active_profile", "startup")
    monkeypatch.setattr(config, "_get_config_path", lambda: root_home / "config.yaml")
    monkeypatch.setenv("HERMES_HOME", str(startup_home))
    # Recreate the installed-Agent state: the skill manager was imported under
    # `startup`, then profile deletion switched the process cache to `root`.
    monkeypatch.setattr(skill_manager, "_SKILLS_DIR_AT_IMPORT", startup_home / "skills")
    monkeypatch.setattr(skill_manager, "SKILLS_DIR", startup_home / "skills")
    applied = []
    apply_skill_pending = skill_manager.apply_skill_pending

    def capture_apply_skill_pending(payload):
        result = apply_skill_pending(payload)
        applied.append({
            "home": str(get_hermes_home()),
            "manager_skills_dir": str(skill_manager._skills_dir()),
            "skills_tool_dir": str(skills_tool._skills_dir()),
            "result": json.loads(result),
        })
        return result

    monkeypatch.setattr(skill_manager, "apply_skill_pending", capture_apply_skill_pending)

    def remove_only_disposable_startup_profile(name, *, yes=False):
        assert name == "startup"
        startup_home.rmdir()
        return startup_home

    monkeypatch.setattr(agent_profiles, "delete_profile", remove_only_disposable_startup_profile)
    profiles.delete_profile_api("startup")
    assert profiles.get_active_profile_name() == "default"
    assert not startup_home.exists()

    profiles.set_request_profile("profile-a")
    try:
        with profiles.profile_env_for_active_request_readonly(
            "issue-7629-stage-skill", include_root=True
        ) as bound:
            assert bound is True
            assert get_hermes_home() == request_home
            staged = write_approval.stage_write(
                "skills",
                {
                    "action": "create",
                    "name": "approval-profile-proof",
                    "content": (
                        "---\nname: approval-profile-proof\n"
                        "description: Verify profile-bound approval writes.\n---\n\n"
                        "Disposable test skill.\n"
                    ),
                },
                summary="disposable profile write",
                origin="foreground",
            )

        output = commands.execute_agent_command(f"/skills approve {staged['id']}")
    finally:
        profiles.clear_request_profile()

    expected_skill = request_home / "skills" / "approval-profile-proof" / "SKILL.md"
    wrong_profile_skill = root_home / "skills" / "approval-profile-proof" / "SKILL.md"
    assert "Approved 1 skills write(s)." in output
    assert applied
    assert applied[0]["home"] == str(request_home)
    assert applied[0]["manager_skills_dir"] == str(request_home / "skills")
    assert applied[0]["skills_tool_dir"] == str(request_home / "skills")
    assert applied[0]["result"]["skill_md"] == str(expected_skill)
    assert expected_skill.is_file()
    assert not wrong_profile_skill.exists()
    assert not (request_home / "pending" / "skills" / f"{staged['id']}.json").exists()


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize("module_name", ["tools.skills_tool", "tools.skill_manager_tool"])
def test_skill_approval_scope_rejects_resolver_outside_request_profile(
    monkeypatch, tmp_path, module_name
):
    """An installed Agent resolver mismatch must fail closed before approval."""
    from importlib import import_module

    from api import commands, profiles

    root_home = tmp_path / "root"
    request_home = root_home / "profiles" / "profile-a"
    request_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(root_home))
    monkeypatch.setenv("HERMES_HOME", str(root_home))

    modules = {name: import_module(name) for name in profiles._SKILL_HOME_MODULES}
    snapshot = profiles.snapshot_skill_home_modules()
    # Simulate a process-wide home switch (startup/_set_hermes_home).
    profiles.patch_skill_home_modules(root_home, align_import_baseline=True)
    bad_module = modules[module_name]
    monkeypatch.setattr(bad_module, "_skills_dir", lambda: tmp_path / "outside" / "skills")

    profiles.set_request_profile("profile-a")
    try:
        with pytest.raises(RuntimeError, match="Skill write-approval paths"):
            with commands._write_approval_profile_context(
                "issue-7629-invalid-skill-path", require_skill_paths=True
            ):
                pytest.fail("approval scope yielded with an out-of-profile resolver")
    finally:
        profiles.clear_request_profile()
        profiles.restore_skill_home_modules(snapshot)


@pytest.mark.requires_agent_modules
def test_skill_approval_holds_resolver_lock_until_handler_finishes(
    monkeypatch, request, tmp_path
):
    """A concurrent profile-module patch cannot race the approved skill action."""
    from importlib import import_module

    from api import commands, profiles

    root_home = tmp_path / "root"
    request_home = root_home / "profiles" / "profile-a"
    request_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(root_home))
    monkeypatch.setenv("HERMES_HOME", str(root_home))

    for module_name in profiles._SKILL_HOME_MODULES:
        import_module(module_name)
    snapshot = profiles.snapshot_skill_home_modules()
    request.addfinalizer(lambda: profiles.restore_skill_home_modules(snapshot))
    # Simulate a process-wide home switch (startup/_set_hermes_home).
    profiles.patch_skill_home_modules(request_home, align_import_baseline=True)

    profiles.set_request_profile("profile-a")

    def competing_patch_can_acquire() -> bool:
        acquired = profiles._SKILL_HOME_MODULE_PATCH_LOCK.acquire(blocking=False)
        if acquired:
            profiles._SKILL_HOME_MODULE_PATCH_LOCK.release()
        return acquired

    try:
        with commands._write_approval_profile_context(
            "issue-7629-serialized-skill-approval", require_skill_paths=True
        ):
            results = []
            worker = threading.Thread(
                target=lambda: results.append(competing_patch_can_acquire())
            )
            worker.start()
            worker.join(5)
            assert not worker.is_alive()
            assert results == [False], "approval released the path lock before handler completion"

        results = []
        worker = threading.Thread(
            target=lambda: results.append(competing_patch_can_acquire())
        )
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        assert results == [True], "approval did not release the path lock after completion"
    finally:
        profiles.clear_request_profile()
