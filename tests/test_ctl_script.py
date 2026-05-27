import argparse
import builtins
import subprocess
import sys
from pathlib import Path

import pytest


def test_daemon_start_writes_pid_state_and_uses_packaged_bootstrap(monkeypatch, tmp_path):
    from hermes_webui import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_HOST", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_PORT", raising=False)

    captured = {}

    class FakePopen:
        pid = 43210

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_resolve_server_python", lambda: (sys.executable, None))

    args = argparse.Namespace(host="0.0.0.0", port=18991, bootstrap_args=[])
    assert cli._daemon_start(args) == 0

    hermes_home = tmp_path / ".hermes"
    assert (hermes_home / "webui.pid").read_text(encoding="utf-8").strip() == "43210"
    state = (hermes_home / "webui.ctl.env").read_text(encoding="utf-8")
    assert "HOST=0.0.0.0" in state
    assert "PORT=18991" in state
    assert captured["cmd"][:3] == [sys.executable, "-m", "hermes_webui.server"]
    assert captured["kwargs"]["env"]["HERMES_WEBUI_STATE_DIR"] == str(hermes_home / "webui")


def test_daemon_start_uses_discovered_agent_python(monkeypatch, tmp_path):
    from hermes_webui import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    agent_dir = tmp_path / "agent-site"
    agent_dir.mkdir()
    agent_python = tmp_path / "agent-python"
    agent_python.write_text("", encoding="utf-8")
    captured = {}

    class FakePopen:
        pid = 43211

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cli, "_is_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_resolve_server_python", lambda: (str(agent_python), agent_dir))

    args = argparse.Namespace(host="127.0.0.1", port=18992, bootstrap_args=[])
    assert cli._daemon_start(args) == 0

    assert captured["cmd"][:3] == [str(agent_python), "-m", "hermes_webui.server"]
    env = captured["kwargs"]["env"]
    assert env["HERMES_WEBUI_AGENT_DIR"] == str(agent_dir)
    assert env["HERMES_WEBUI_PYTHON"] == str(agent_python)
    assert str(cli.PACKAGE_ROOT.parent) in env["PYTHONPATH"].split(":")


def test_serve_reexecs_to_discovered_agent_python(monkeypatch, tmp_path):
    from hermes_webui import cli

    agent_dir = tmp_path / "agent-site"
    agent_dir.mkdir()
    agent_python = tmp_path / "agent-python"
    agent_python.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("HERMES_WEBUI_SERVER_REEXECED", raising=False)
    monkeypatch.setattr(cli.sys, "executable", str(tmp_path / "uv-python"))
    monkeypatch.setattr(cli, "_resolve_server_python", lambda: (str(agent_python), agent_dir))

    captured = {}

    def fake_execvpe(path, argv, env):
        captured["path"] = path
        captured["argv"] = argv
        captured["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execvpe", fake_execvpe)

    with pytest.raises(SystemExit):
        cli._serve(argparse.Namespace(host="127.0.0.1", port=18993))

    assert captured["path"] == str(agent_python)
    assert captured["argv"][:3] == [str(agent_python), "-m", "hermes_webui.cli"]
    assert captured["argv"][-4:] == ["--host", "127.0.0.1", "--port", "18993"]
    env = captured["env"]
    assert env["HERMES_WEBUI_SERVER_REEXECED"] == "1"
    assert env["HERMES_WEBUI_AGENT_DIR"] == str(agent_dir)
    assert env["HERMES_WEBUI_PYTHON"] == str(agent_python)
    assert str(cli.PACKAGE_ROOT.parent) in env["PYTHONPATH"].split(":")


def test_daemon_stop_removes_stale_pid_without_killing(monkeypatch, tmp_path):
    from hermes_webui import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "webui.pid").write_text("999999\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_is_alive", lambda pid: False)

    assert cli._daemon_stop(argparse.Namespace()) == 0
    assert not (hermes_home / "webui.pid").exists()


def test_logs_supports_non_following_line_count(monkeypatch, tmp_path):
    from hermes_webui import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    log_file = hermes_home / "webui.log"
    log_file.write_text("one\ntwo\nthree\n", encoding="utf-8")

    calls = {}

    def fake_call(cmd):
        calls["cmd"] = cmd
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    assert cli._daemon_logs(argparse.Namespace(lines=2, follow=False)) == 0
    assert calls["cmd"] == ["tail", "-n", "2", str(log_file)]


def test_logs_reports_unwritable_log_path(monkeypatch, tmp_path, capsys):
    from hermes_webui import cli

    monkeypatch.setenv("HERMES_WEBUI_LOG_FILE", str(tmp_path / "webui.log"))
    monkeypatch.setattr(Path, "touch", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    assert cli._daemon_logs(argparse.Namespace(lines=2, follow=False)) == 1
    assert "cannot open log file" in capsys.readouterr().err


def test_ctl_sh_delegates_to_packaged_cli():
    ctl = Path(__file__).resolve().parents[1] / "ctl.sh"
    result = subprocess.run(
        ["bash", str(ctl), "--help"],
        cwd=ctl.parent,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "hermes-webui" in result.stdout


def test_version_flag_does_not_import_runtime_config(monkeypatch, capsys):
    from hermes_webui import cli

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"api.config", "api.updates"}:
            raise AssertionError(f"--version should not import {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(cli, "_cli_version", lambda: "test-version")

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "test-version"


def test_legacy_api_import_aliases_packaged_api_tree():
    import hermes_webui  # noqa: F401 - package import installs the api alias.
    import api.routes as legacy_routes

    assert Path(legacy_routes.__file__).parts[-3:] == (
        "hermes_webui",
        "api",
        "routes.py",
    )


def test_mcp_missing_extra_reports_actionable_error(monkeypatch, capsys):
    from hermes_webui import cli

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "hermes_webui":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert cli._mcp(argparse.Namespace(profile=None)) == 1
    assert "MCP support is not installed" in capsys.readouterr().err


def test_server_reuses_address_for_fast_daemon_restart():
    server = Path(__file__).resolve().parents[1] / "server.py"
    source = server.read_text(encoding="utf-8")
    assert "allow_reuse_address = True" in source
