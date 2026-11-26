"""Unreadable Agent revisions must leave a diagnosable trace in the log.

The stale-runtime guard fails closed on purpose (#7407): a revision it cannot
read is indistinguishable from one that changed, and both end in the same 409.
That behaviour is correct and deliberately untouched here. The gap these tests
pin shut is the *observability* half — `_read_agent_revision` used to return
``None`` without a word, so an operator facing the restart demand had no way to
tell "the Agent checkout moved" from "Git could not be read for a moment".

Two contracts are asserted:

1. every unreadable path names the failing Git step, its return code and a
   bounded excerpt of Git's own message;
2. the admission guard distinguishes "read failed" from "revision genuinely
   moved" in its own warning, without changing either outcome.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


LOGGER_NAME = "api.agent_runtime"


def _completed(*, returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess shaped like the ones subprocess.run returns."""
    import subprocess

    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(monkeypatch, mapping, *, default=None):
    """Replace the module's subprocess.run with per-command canned results.

    ``mapping`` keys are substrings of the joined argv, so each test can fail
    exactly one Git step while the others keep succeeding.
    """
    from api import agent_runtime

    seen: list[str] = []

    def _run(argv, **_kwargs):
        joined = " ".join(str(part) for part in argv)
        seen.append(joined)
        for needle, result in mapping.items():
            if needle in joined:
                return result
        if default is not None:
            return default
        return _completed(stdout="")

    monkeypatch.setattr(agent_runtime.subprocess, "run", _run)
    return seen


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _agent_tree(tmp_path):
    """Return (agent_dir, module_path) for a fake checkout on disk."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    module_path = agent_dir / "run_agent.py"
    module_path.write_text("class AIAgent: pass\n", encoding="utf-8")
    return agent_dir, module_path


def test_worktree_read_failure_names_the_step_and_git_output(
    monkeypatch, tmp_path, caplog
):
    """A failing `rev-parse --show-toplevel` must say so, with Git's own words."""
    from api import agent_runtime

    agent_dir, module_path = _agent_tree(tmp_path)
    _patch_run(
        monkeypatch,
        {
            "rev-parse --show-toplevel": _completed(
                returncode=128, stderr="fatal: not a git repository (or any parent)"
            )
        },
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(
            agent_dir, module_path=module_path
        ) is None

    messages = _messages(caplog)
    assert any("agent revision unreadable" in m for m in messages), messages
    assert any("rev-parse --show-toplevel" in m for m in messages), messages
    assert any("rc=128" in m for m in messages), messages
    assert any("not a git repository" in m for m in messages), messages


def test_tracked_module_failure_names_the_step(monkeypatch, tmp_path, caplog):
    """A failing `ls-files --error-unmatch` is distinguishable from step one."""
    from api import agent_runtime

    agent_dir, module_path = _agent_tree(tmp_path)
    _patch_run(
        monkeypatch,
        {
            "rev-parse --show-toplevel": _completed(stdout=f"{agent_dir}\n"),
            "ls-files": _completed(
                returncode=1, stderr="error: pathspec 'agent/run_agent.py' did not match"
            ),
        },
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(
            agent_dir, module_path=module_path
        ) is None

    messages = _messages(caplog)
    assert any("ls-files --error-unmatch" in m for m in messages), messages
    assert any("rc=1" in m for m in messages), messages
    # The earlier step succeeded, so its complaint must not be the one recorded.
    assert not any("rev-parse --show-toplevel" in m for m in messages), messages


def test_head_read_failure_names_the_step(monkeypatch, tmp_path, caplog):
    """A failing `rev-parse --verify HEAD` reports the HEAD step, not an earlier one."""
    from api import agent_runtime

    agent_dir, module_path = _agent_tree(tmp_path)
    _patch_run(
        monkeypatch,
        {
            "rev-parse --show-toplevel": _completed(stdout=f"{agent_dir}\n"),
            "ls-files": _completed(stdout="run_agent.py\n"),
            "rev-parse --verify HEAD": _completed(
                returncode=128, stderr="fatal: Needed a single revision"
            ),
        },
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(
            agent_dir, module_path=module_path
        ) is None

    messages = _messages(caplog)
    assert any("rev-parse --verify HEAD" in m for m in messages), messages
    assert any("Needed a single revision" in m for m in messages), messages


def test_empty_head_output_is_logged(monkeypatch, tmp_path, caplog):
    """rc=0 with blank stdout is still unreadable, and says so."""
    from api import agent_runtime

    agent_dir, module_path = _agent_tree(tmp_path)
    _patch_run(
        monkeypatch,
        {
            "rev-parse --show-toplevel": _completed(stdout=f"{agent_dir}\n"),
            "ls-files": _completed(stdout="run_agent.py\n"),
            "rev-parse --verify HEAD": _completed(returncode=0, stdout="   \n"),
        },
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(
            agent_dir, module_path=module_path
        ) is None

    assert any("rev-parse --verify HEAD" in m for m in _messages(caplog))


def test_missing_agent_dir_is_logged(caplog):
    """No resolved Agent directory is itself an unreadable reason worth naming."""
    from api import agent_runtime

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(None) is None

    messages = _messages(caplog)
    assert any("no Agent source directory" in m for m in messages), messages


def test_budget_exhaustion_is_logged(monkeypatch, caplog):
    """A zero budget cannot even start the read; the timeout reason is recorded."""
    from api import agent_runtime

    monkeypatch.setattr(agent_runtime, "_GIT_REVISION_TIMEOUT_SECONDS", 0)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(Path("/nonexistent-agent")) is None

    messages = _messages(caplog)
    assert any("agent revision unreadable" in m for m in messages), messages


def test_admission_separates_unreadable_from_changed(monkeypatch, tmp_path, caplog):
    """The 409 warning must say WHICH of the two causes fired."""
    from api import agent_runtime

    agent_dir, module_path = _agent_tree(tmp_path)
    monkeypatch.setattr(agent_runtime, "_AGENT_SOURCE_DIR", agent_dir)
    monkeypatch.setattr(agent_runtime, "_AGENT_MODULE_PATH", module_path)
    monkeypatch.setattr(agent_runtime, "_AGENT_REVISION", "a" * 40)

    # Cause 1: the read itself fails.
    monkeypatch.setattr(
        agent_runtime, "_read_agent_revision", lambda *a, **k: None
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with pytest.raises(agent_runtime.AgentRuntimeChangedError):
            agent_runtime.ensure_agent_runtime_current()
    unreadable = [m for m in _messages(caplog) if "admission failed" in m]
    assert unreadable, "no admission warning recorded"
    assert any("unreadable" in m for m in unreadable), unreadable

    caplog.clear()

    # Cause 2: the read succeeds but the revision moved.
    monkeypatch.setattr(
        agent_runtime, "_read_agent_revision", lambda *a, **k: "b" * 40
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with pytest.raises(agent_runtime.AgentRuntimeChangedError):
            agent_runtime.ensure_agent_runtime_current()
    changed = [m for m in _messages(caplog) if "admission failed" in m]
    assert changed, "no admission warning recorded"
    assert any("no longer matches" in m for m in changed), changed
    assert not any("unreadable" in m for m in changed), changed


def test_read_failure_detail_is_bounded_and_single_line():
    """Git can emit multi-line, arbitrarily long stderr; the log line must not."""
    from api import agent_runtime

    result = _completed(
        returncode=1,
        stderr="line one\nline two\n" + ("x" * 1000),
    )
    detail = agent_runtime._git_failure_detail(result)

    assert "\n" not in detail
    assert len(detail) <= agent_runtime._GIT_FAILURE_DETAIL_MAX + len("...")
    assert detail.startswith("line one line two")


def test_read_failure_detail_empty_output():
    """Silent failures still produce a usable placeholder."""
    from api import agent_runtime

    assert agent_runtime._git_failure_detail(_completed(returncode=1)) == "(no output)"


def test_successful_read_logs_nothing(monkeypatch, tmp_path, caplog):
    """The happy path must stay silent — this guard runs on every request."""
    from api import agent_runtime

    agent_dir, module_path = _agent_tree(tmp_path)
    _patch_run(
        monkeypatch,
        {
            "rev-parse --show-toplevel": _completed(stdout=f"{agent_dir}\n"),
            "ls-files": _completed(stdout="run_agent.py\n"),
            "rev-parse --verify HEAD": _completed(stdout="c" * 40 + "\n"),
        },
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        revision = agent_runtime._read_agent_revision(
            agent_dir, module_path=module_path
        )

    assert revision == "c" * 40
    assert not [m for m in _messages(caplog) if "unreadable" in m]
