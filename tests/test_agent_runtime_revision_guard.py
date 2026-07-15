"""Regression coverage for Hermes Agent source changes during a WebUI process lifetime."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_loaded_agent_runtime_fails_closed_after_source_revision_changes(tmp_path: Path):
    agent_dir = tmp_path / "hermes-agent"
    agent_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        "class AIAgent:\n    revision = 'before'\n",
        encoding="utf-8",
    )
    _git(agent_dir, "init", "-q")
    _git(agent_dir, "add", "run_agent.py")
    _git(agent_dir, "commit", "-qm", "before")

    probe = tmp_path / "probe.py"
    probe.write_text(
        """
from pathlib import Path
import subprocess

import api.streaming as streaming
from api import agent_runtime

agent_dir = Path(__file__).parent / "hermes-agent"
assert agent_runtime._AGENT_DIR == agent_dir.resolve()
assert streaming._get_ai_agent().revision == "before"

(agent_dir / "run_agent.py").write_text(
    "class AIAgent:\\n    revision = 'after'\\n",
    encoding="utf-8",
)
subprocess.run(["git", "add", "run_agent.py"], cwd=agent_dir, check=True)
subprocess.run(
    [
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "after",
    ],
    cwd=agent_dir,
    check=True,
)

try:
    streaming._get_ai_agent()
except RuntimeError as exc:
    message = str(exc)
    assert "Hermes Agent was updated" in message
    assert "Restart Hermes WebUI" in message
else:
    raise AssertionError("stale in-process AIAgent was reused after its source revision changed")

try:
    agent_runtime.require_ai_agent_class()
except agent_runtime.AgentRuntimeChangedError as exc:
    assert "Restart Hermes WebUI" in str(exc)
else:
    raise AssertionError("unguarded AIAgent import was allowed after its source revision changed")
""".strip()
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
            "HERMES_HOME": str(tmp_path / "hermes-home"),
            "HERMES_WEBUI_STATE_DIR": str(tmp_path / "webui-state"),
            "PYTHONPATH": os.pathsep.join((str(REPO), str(agent_dir))),
        }
    )
    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
