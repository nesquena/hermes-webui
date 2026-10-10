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
