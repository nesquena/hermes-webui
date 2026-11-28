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
import subprocess
import sys

import pytest


LOGGER_NAME = "api.agent_runtime"


def _completed(*, returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess shaped like the ones subprocess.run returns."""
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
    """Return the messages this module's logger emitted.

    Scoped to ``LOGGER_NAME`` on purpose: ``caplog`` captures every logger in
    the session, so an unrelated warning could otherwise satisfy a loose
    assertion and let the branch under test be deleted without turning the
    test red. That is exactly how a timeout assertion came to accept the
    unrelated "run_agent has no __file__" warning.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


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


def test_budget_exhaustion_is_logged(monkeypatch, tmp_path, caplog):
    """A zero budget cannot even start the read; the timeout reason is recorded.

    The ``module_path`` argument is load-bearing. Without it the reader falls
    back to the loaded ``run_agent`` module, which is absent in a unit test
    session: it then logs "the loaded run_agent module has no __file__" and
    returns *before* the timeout is ever reached. A loose assertion on the
    shared "agent revision unreadable" prefix would still pass on that
    unrelated warning, so the timeout branch could be deleted without turning
    this test red. Passing an explicit module path forces the real timeout
    path, and the assertion below pins that one exact message.
    """
    from api import agent_runtime

    monkeypatch.setattr(agent_runtime, "_GIT_REVISION_TIMEOUT_SECONDS", 0)

    agent_dir = tmp_path / "agent"
    module_path = agent_dir / "run_agent.py"

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(
            agent_dir, module_path=module_path
        ) is None

    # Exactly one warning, and it is the timeout one — not the missing-__file__
    # warning that an omitted module_path would have produced.
    expected = (
        "agent revision unreadable: a Git read failed in "
        f"{agent_dir}: {subprocess.TimeoutExpired('git', 0)}"
    )
    assert _messages(caplog) == [expected], _messages(caplog)


def test_missing_run_agent_module_is_logged(monkeypatch, caplog):
    """The no-``__file__`` fallback names itself, so it cannot stand in for another reason."""
    from api import agent_runtime

    monkeypatch.setitem(sys.modules, "run_agent", None)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(Path("/nonexistent-agent")) is None

    messages = _messages(caplog)
    assert messages == [
        "agent revision unreadable: the loaded run_agent module has no __file__"
    ], messages


def test_expired_budget_after_successful_reads_is_logged(
    monkeypatch, tmp_path, caplog
):
    """The post-read budget check names itself too.

    This is the *other* timeout warning: every Git call succeeded, but the
    total budget had run out by the time HEAD came back. It lives on a
    different branch from the exception handler above, so it needs its own
    pin — with the same exact-match rule, so no unrelated warning can stand in
    for it.
    """
    import time as real_time
    import types

    from api import agent_runtime

    agent_dir, module_path = _agent_tree(tmp_path)

    # Every Git call succeeds, but the clock advances past the deadline during
    # the last one, so the post-read budget check is what fires — not the
    # TimeoutExpired handler, which would fire if the budget ran out *before*
    # a call started.
    clock = {"now": 100.0}
    monkeypatch.setattr(
        agent_runtime,
        "time",
        types.SimpleNamespace(monotonic=lambda: clock["now"], time=real_time.time),
    )
    monkeypatch.setattr(agent_runtime, "_GIT_REVISION_TIMEOUT_SECONDS", 10)

    # Budget is 10s: the first two calls fit inside it, the third one starts
    # with 2s left and finishes 5s later — so the read succeeded but the
    # total budget is gone.
    delays = [4.0, 4.0, 5.0]
    calls = {"n": 0}

    def _run(argv, **_kwargs):
        delay = delays[calls["n"]]
        calls["n"] += 1
        clock["now"] += delay
        joined = " ".join(str(part) for part in argv)
        if "rev-parse --show-toplevel" in joined:
            return _completed(stdout=f"{agent_dir}\n")
        if "ls-files" in joined:
            return _completed(stdout="run_agent.py\n")
        return _completed(stdout="d" * 40 + "\n")

    monkeypatch.setattr(agent_runtime.subprocess, "run", _run)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert agent_runtime._read_agent_revision(
            agent_dir, module_path=module_path
        ) is None

    assert calls["n"] == 3, calls["n"]
    assert _messages(caplog) == [
        "agent revision unreadable: the 10s Git read budget expired before the "
        f"HEAD read completed in {agent_dir}"
    ], _messages(caplog)


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
