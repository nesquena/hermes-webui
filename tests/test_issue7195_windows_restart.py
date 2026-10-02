"""Behavioral regression coverage for the Windows restart entrypoint."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

import api.updates as updates


FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "issue7195_windows_restart.json"
REPO = pathlib.Path(__file__).resolve().parent.parent


def _run_restart(monkeypatch, *, argv, frozen=False, pythonw=False, spawn=None, exit=None):
    events = []
    exit_event = threading.Event()
    spawn_finished = threading.Event()
    spawn_failed = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(updates, "REPO_ROOT", REPO)
    monkeypatch.setattr(updates, "_AGENT_DIR", None)
    monkeypatch.setattr(updates, "_wait_until_restart_safe", lambda: events.append("wait") or {"restart_blocked": False})
    monkeypatch.setattr(updates, "_purge_agent_pycache", lambda path: events.append(("purge", path)))
    monkeypatch.setattr(updates.subprocess, "DETACHED_PROCESS", 1, raising=False)
    monkeypatch.setattr(updates.subprocess, "CREATE_NEW_PROCESS_GROUP", 2, raising=False)
    monkeypatch.setattr(updates.subprocess, "CREATE_NO_WINDOW", 4, raising=False)
    monkeypatch.setattr(updates.subprocess, "DEVNULL", subprocess.DEVNULL)
    real_isfile = updates.os.path.isfile
    monkeypatch.setattr(
        updates.os.path,
        "isfile",
        lambda path: pythonw if str(path).lower().endswith("pythonw.exe") else real_isfile(path),
    )

    def record_spawn(args, **kwargs):
        events.append(("spawn", list(args), kwargs))
        try:
            if spawn:
                spawn(args, **kwargs)
        except BaseException:
            spawn_failed.append(True)
            raise
        finally:
            spawn_finished.set()

    def record_exit(code):
        events.append(("exit", code))
        exit_event.set()
        if exit:
            exit(code)

    monkeypatch.setattr(updates, "_windows_restart_spawn", record_spawn)
    monkeypatch.setattr(updates, "_windows_restart_exit", record_exit)
    updates._schedule_restart(delay=0)
    deadline = time.monotonic() + 2
    while not any(event[0] == "spawn" for event in events if isinstance(event, tuple)):
        if time.monotonic() >= deadline:
            pytest.fail(f"restart worker did not spawn: {events!r}")
        time.sleep(0.01)
    if not spawn_finished.wait(timeout=2):
        pytest.fail(f"restart worker did not finish spawning: {events!r}")
    if not spawn_failed and not exit_event.wait(timeout=2):
        pytest.fail(f"restart worker did not exit after spawning: {events!r}")
    return events


def test_reported_pytest_argv_restarts_canonical_server(monkeypatch):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    events = _run_restart(monkeypatch, argv=fixture["launch"]["argv_shape"])
    spawn = next(event for event in events if event[0] == "spawn")
    assert spawn[1] == [r"C:\Python\python.exe", str(REPO / "server.py")]
    assert not any("pytest" in token.lower() for token in spawn[1])
    assert events[-1] == ("exit", 0)


@pytest.mark.parametrize("argv", [
    ["server.py"],
    ["python", "-m", "server"],
    ["hermes-webui", "--profile", "default"],
    ["wrapper", "--run", "server.py"],
])
def test_source_restart_always_targets_server(monkeypatch, argv):
    events = _run_restart(monkeypatch, argv=argv)
    assert next(event for event in events if event[0] == "spawn")[1] == [r"C:\Python\python.exe", str(REPO / "server.py")]


def test_frozen_restart_preserves_argv(monkeypatch):
    argv = [r"C:\Apps\Hermes\hermes.exe", "--profile", "default"]
    assert next(event for event in _run_restart(monkeypatch, argv=argv, frozen=True) if event[0] == "spawn")[1] == argv


def test_windows_restart_spawns_once_then_exits_once(monkeypatch):
    events = _run_restart(monkeypatch, argv=["pytest", "tests/test_issue7195_windows_restart.py"], pythonw=True)
    assert events[0] == "wait"
    assert events[1][0] == "purge"
    assert [event[0] for event in events if isinstance(event, tuple) and event[0] in {"spawn", "exit"}] == ["spawn", "exit"]
    spawn = next(event for event in events if event[0] == "spawn")
    assert spawn[1][0].endswith("pythonw.exe")
    assert spawn[2]["cwd"] == os.getcwd()
    assert spawn[2]["creationflags"] == 7
    assert spawn[2]["close_fds"] is True
    assert spawn[2]["stdin"] is subprocess.DEVNULL
    assert spawn[2]["stdout"] is subprocess.DEVNULL
    assert spawn[2]["stderr"] is subprocess.DEVNULL


def test_windows_spawn_failure_keeps_current_process(monkeypatch, caplog):
    def fail(*_args, **_kwargs):
        raise OSError("spawn failure")

    messages = []
    monkeypatch.setattr(updates.logger, "exception", lambda message: messages.append(message))
    events = _run_restart(monkeypatch, argv=["server.py"], spawn=fail)
    assert not any(event[0] == "exit" for event in events if isinstance(event, tuple))
    assert messages == ["Windows WebUI restart spawn failed"]


def test_pytest_session_restart_operations_are_inert():
    assert updates._windows_restart_spawn(None) is None
    assert updates._windows_restart_exit(0) is None


def test_local_restart_recorders_restore_session_guards(monkeypatch):
    monkeypatch.setattr(updates, "_windows_restart_spawn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updates, "_windows_restart_exit", lambda *_args: None)
    monkeypatch.undo()
    assert updates._windows_restart_spawn(None) is None
    assert updates._windows_restart_exit(0) is None


@pytest.mark.parametrize("frozen", [False, True])
def test_posix_restart_keeps_execv_shape(monkeypatch, frozen):
    events = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python")
    monkeypatch.setattr(sys, "argv", ["wrapper", "--profile", "default"])
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(updates, "_AGENT_DIR", None)
    monkeypatch.setattr(updates, "_wait_until_restart_safe", lambda: {})
    monkeypatch.setattr(updates, "_purge_agent_pycache", lambda _path: None)
    monkeypatch.setattr(os, "execv", lambda exe, argv: events.append((exe, argv)))
    updates._schedule_restart(delay=0)
    deadline = time.monotonic() + 2
    while not events and time.monotonic() < deadline:
        time.sleep(0.01)
    expected_argv = ["wrapper", "--profile", "default"]
    assert events == [("/usr/bin/python", expected_argv if frozen else ["/usr/bin/python", *expected_argv])]


def test_fixture_subprocess_is_not_guarded():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait(timeout=5) == 0


def test_restart_callers_remain_complete():
    source = (REPO / "api" / "updates.py").read_text(encoding="utf-8")
    assert source.count("_schedule_restart()") == 3
