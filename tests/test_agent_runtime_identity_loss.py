"""Git identity loss must not fall back to forgeable source-file metadata."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_git_identity_loss_rejects_changed_source_with_preserved_metadata(
    monkeypatch, tmp_path: Path
):
    """A known Git identity cannot fall back to forgeable file metadata."""
    from api import agent_runtime

    source_dir = tmp_path / "loaded-agent"
    source_dir.mkdir()
    module_file = source_dir / "run_agent.py"
    module_file.write_bytes(b"class AIAgent: pass\n")
    _git(source_dir, "init", "-q")
    _git(source_dir, "add", "run_agent.py")
    _git(source_dir, "commit", "-qm", "loaded agent")

    loaded_module = types.ModuleType("run_agent")
    loaded_module.__file__ = str(module_file)
    monkeypatch.setitem(sys.modules, "run_agent", loaded_module)
    revision = _git(source_dir, "rev-parse", "HEAD")
    original_stat = module_file.stat()
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", source_dir.resolve())
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", module_file.resolve())
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", revision)
    monkeypatch.setattr(
        agent_runtime, "_AGENT_MODULE_MTIME_NS", original_stat.st_mtime_ns,
        raising=False,
    )

    (source_dir / ".git").rename(tmp_path / "hidden-agent-git")
    module_file.write_bytes(b"class AIAgent: gasp\n")
    os.utime(module_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    fresh_stat = module_file.stat()
    assert fresh_stat.st_size == original_stat.st_size
    assert fresh_stat.st_mtime_ns == original_stat.st_mtime_ns

    with pytest.raises(agent_runtime.AgentRuntimeChangedError):
        agent_runtime.ensure_agent_runtime_current()
