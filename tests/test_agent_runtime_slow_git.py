"""Revision checks tolerate bounded Git latency without weakening admission."""
import subprocess
import sys

import pytest


@pytest.fixture
def known_revision(monkeypatch, tmp_path):
    from api import agent_runtime as runtime

    repo = tmp_path / "repo"
    repo.mkdir()
    module = repo / "run_agent.py"
    module.write_text("class AIAgent: pass\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", "run_agent.py")
    git("commit", "-qm", "fixture")
    monkeypatch.setattr(runtime, "_AGENT_SOURCE_DIR", repo)
    monkeypatch.setattr(runtime, "_AGENT_MODULE_PATH", module)
    monkeypatch.setattr(runtime, "_AGENT_REVISION", git("rev-parse", "HEAD"))
    return runtime


@pytest.mark.parametrize("slow_call", [None, 1, 2, 3])
def test_unchanged_revision_with_slow_git_is_current(monkeypatch, known_revision, slow_call):
    runtime = known_revision
    real_run = subprocess.run
    calls = []

    def delayed_run(command, **kwargs):
        calls.append(command)
        if len(calls) == slow_call:
            # Delay and Git execute inside the SAME bounded subprocess. Do not
            # sleep outside subprocess.run(), which would evade its timeout.
            command = [sys.executable, "-c",
                       "import os,sys,time; time.sleep(2.2); "
                       "os.execvp(sys.argv[1], sys.argv[1:])", *command]
        return real_run(command, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", delayed_run)
    runtime.ensure_agent_runtime_current()
    assert len(calls) == 3


@pytest.mark.parametrize("failed_call", [1, 2, 3])
@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_git_read_failure_still_fails_closed(monkeypatch, known_revision, failed_call, failure):
    runtime = known_revision
    real_run = subprocess.run
    calls = []

    def failing_run(command, **kwargs):
        calls.append(command)
        if len(calls) == failed_call:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 1, "", "unavailable")
        return real_run(command, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", failing_run)
    with pytest.raises(runtime.AgentRuntimeChangedError):
        runtime.ensure_agent_runtime_current()
    assert len(calls) == failed_call
