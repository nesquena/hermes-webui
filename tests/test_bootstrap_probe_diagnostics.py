"""The bootstrap probe's failure diagnostic (#7848).

`_python_can_run_webui_and_agent` used to return `check.returncode == 0` and throw the
probe's stderr away. Every candidate that failed then produced the same one-line
"cannot import both WebUI dependencies and Hermes Agent" message, which names neither
the interpreter that failed nor the dependency that was missing. On PM-managed installs
the WebUI then restart-loops with no actionable cause in the log.

These tests assert the diagnostic describes the environment that actually failed,
including the case where the agent relaunches the probe into a different interpreter.
"""

import os
import pathlib
import subprocess
import sys

import pytest

import bootstrap

MARKER = "HERMES_WEBUI_PROBE_FAIL"


def _pip_isolated_run(real_run):
    """Pass probe subprocesses through to the real runner; no-op the pip installs."""

    def runner(cmd, *a, **k):
        if "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *a, **k)

    return runner


def _repo_venv_python(repo_root: pathlib.Path) -> pathlib.Path:
    rel = pathlib.Path(
        "Scripts/python.exe" if bootstrap.platform.system() == "Windows" else "bin/python"
    )
    return repo_root / ".venv" / rel


def _broken_agent(agent_dir: pathlib.Path, missing: str) -> pathlib.Path:
    """An agent tree whose run_agent.py raises the way a broken managed env does."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "run_agent.py").write_text(
        f"raise ModuleNotFoundError(\"No module named '{missing}'\")\n",
        encoding="utf-8",
    )
    return agent_dir


def test_failure_names_the_interpreter_and_the_missing_dependency(monkeypatch, tmp_path):
    """The whole point: say which interpreter failed and which import it died on."""
    agent_dir = _broken_agent(tmp_path / "agent", "ruamel")
    repo = tmp_path / "repo"
    venv_python = _repo_venv_python(repo)
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    # A real interpreter, so the probe runs for real on the final candidate too.
    os.symlink(sys.executable, venv_python)

    monkeypatch.setattr(bootstrap, "REPO_ROOT", repo)
    monkeypatch.setattr(
        bootstrap.subprocess, "run", _pip_isolated_run(subprocess.run)
    )

    with pytest.raises(RuntimeError) as excinfo:
        bootstrap.ensure_python_has_webui_deps(sys.executable, agent_dir)

    message = str(excinfo.value)
    # The interpreter that was actually probed...
    assert str(venv_python) in message
    # ...and the dependency that was actually missing. Without both, the message is
    # unactionable: "install PyYAML into the agent venv" is the wrong advice when the
    # agent tree itself is broken.
    assert "ruamel" in message


def test_failure_reports_the_relaunched_interpreter_not_the_one_requested(monkeypatch, tmp_path):
    """Importing the agent can relaunch the probe into the managed runtime.

    The reporter's tell was that the traceback came from the *relaunched* interpreter,
    not the one the probe started with. When that happens the diagnostic has to name
    both, or the operator debugs the wrong site-packages tree.
    """
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    requested = str(tmp_path / "agent" / "venv" / "bin" / "python")
    relaunched = "/pm/store/python3.14"

    def fake_run(cmd, *a, **k):
        if "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr=f"{MARKER}\t{relaunched}\tModuleNotFoundError: No module named 'yaml'\n",
        )

    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.delenv("HERMES_WEBUI_DISABLE_LOCAL_VENV", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        bootstrap.ensure_python_has_webui_deps(requested, agent_dir)

    message = str(excinfo.value)
    assert relaunched in message
    assert requested in message
    # Explicit that the two differ, so the reader knows which tree is authoritative.
    assert "relaunch" in message.lower()


def test_failure_falls_back_to_the_probe_traceback_tail(monkeypatch, tmp_path):
    """A probe that dies before printing the marker still reports its real error."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    requested = str(agent_dir / "venv" / "bin" / "python")

    def fake_run(cmd, *a, **k):
        if "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr=(
                '  File "<string>", line 2, in <module>\n'
                "ModuleNotFoundError: No module named 'jiter.jiter'\n"
            ),
        )

    monkeypatch.setattr(bootstrap, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.delenv("HERMES_WEBUI_DISABLE_LOCAL_VENV", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        bootstrap.ensure_python_has_webui_deps(requested, agent_dir)

    message = str(excinfo.value)
    assert requested in message
    assert "jiter.jiter" in message