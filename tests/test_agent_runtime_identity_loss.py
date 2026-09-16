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


def test_raising_revision_reader_fails_closed(monkeypatch, tmp_path: Path):
    """A revision read that RAISES must fail closed, not escape as a 500.

    Every other identity-loss shape (deleted, permission-denied, empty,
    whitespace-only, corrupt, removed directory) already raises
    AgentRuntimeChangedError.  A reader that throws is the same situation --
    the revision is unreadable, which is indistinguishable from changed --
    so it must produce the same typed error rather than propagating a raw
    OSError that the request layer would surface as an HTTP 500 instead of
    the intended stale-runtime response.
    """
    from api import agent_runtime

    source_dir = tmp_path / "raising-agent"
    source_dir.mkdir()
    module_file = source_dir / "run_agent.py"
    module_file.write_bytes(b"class AIAgent: pass\n")

    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", source_dir.resolve())
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", module_file.resolve())
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", "0" * 40)

    def _boom(*_args, **_kwargs):
        raise PermissionError("revision unreadable")

    monkeypatch.setattr(agent_runtime, "_read_agent_revision", _boom)

    with pytest.raises(agent_runtime.AgentRuntimeChangedError):
        agent_runtime.ensure_agent_runtime_current()
