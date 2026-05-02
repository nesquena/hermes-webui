"""Regression tests for supervisor-friendly bootstrap foreground mode."""

from __future__ import annotations

import os
import sys

import pytest


def test_parse_args_accepts_foreground_flag():
    import bootstrap as bs

    args = bs.parse_args(["--foreground", "--no-browser", "18888"])

    assert args.foreground is True
    assert args.no_browser is True
    assert args.port == 18888


def test_supervisor_env_present_detects_launchd_and_systemd():
    import bootstrap as bs

    assert bs.supervisor_env_present({}) is False
    assert bs.supervisor_env_present({"LAUNCHD_SOCKET": "/tmp/launchd.sock"}) is True
    assert bs.supervisor_env_present({"INVOCATION_ID": "systemd-unit"}) is True
    assert bs.supervisor_env_present({"NOTIFY_SOCKET": "/run/notify"}) is True


def test_foreground_replaces_bootstrap_without_popen(monkeypatch, tmp_path):
    import bootstrap as bs

    calls = {}

    def fake_execve(executable, argv, env):
        calls["execve"] = (executable, argv, env)
        raise SystemExit(0)

    def fail_popen(*_args, **_kwargs):
        raise AssertionError("foreground mode must not spawn a detached child")

    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--foreground", "--no-browser"])
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
    monkeypatch.setattr(bs, "discover_agent_dir", lambda: None)
    monkeypatch.setattr(bs, "hermes_command_exists", lambda: True)
    monkeypatch.setattr(bs, "discover_launcher_python", lambda _agent_dir: "python3")
    monkeypatch.setattr(bs, "ensure_python_has_webui_deps", lambda _python: "/bin/python3")
    monkeypatch.setattr(bs.subprocess, "Popen", fail_popen)
    monkeypatch.setattr(bs, "wait_for_health", lambda _url: False)
    monkeypatch.setattr(bs.os, "chdir", lambda path: calls.setdefault("chdir", path))
    monkeypatch.setattr(bs.os, "execve", fake_execve)

    with pytest.raises(SystemExit) as exc:
        bs.main()

    assert exc.value.code == 0
    assert calls["chdir"] == str(bs.REPO_ROOT)
    executable, argv, env = calls["execve"]
    assert executable == "/bin/python3"
    assert argv == ["/bin/python3", str(bs.REPO_ROOT / "server.py")]
    assert env["HERMES_WEBUI_HOST"] == bs.DEFAULT_HOST
    assert env["HERMES_WEBUI_PORT"] == str(bs.DEFAULT_PORT)
    assert env["HERMES_WEBUI_STATE_DIR"] == str(tmp_path)


def test_supervisor_env_triggers_foreground_without_flag(monkeypatch, tmp_path):
    import bootstrap as bs

    calls = {}

    def fake_execve(executable, argv, env):
        calls["execve"] = (executable, argv, env)
        raise SystemExit(0)

    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--no-browser"])
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("INVOCATION_ID", "unit-id")
    monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
    monkeypatch.setattr(bs, "discover_agent_dir", lambda: None)
    monkeypatch.setattr(bs, "hermes_command_exists", lambda: True)
    monkeypatch.setattr(bs, "discover_launcher_python", lambda _agent_dir: "python3")
    monkeypatch.setattr(bs, "ensure_python_has_webui_deps", lambda _python: "/bin/python3")
    monkeypatch.setattr(bs.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Popen called")))
    monkeypatch.setattr(bs.os, "chdir", lambda _path: None)
    monkeypatch.setattr(bs.os, "execve", fake_execve)

    with pytest.raises(SystemExit) as exc:
        bs.main()

    assert exc.value.code == 0
    assert calls["execve"][2]["INVOCATION_ID"] == "unit-id"
