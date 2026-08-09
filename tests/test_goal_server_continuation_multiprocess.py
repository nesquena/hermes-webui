"""Cross-process safety contracts for the durable /goal registry."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _configure(path: str):
    from api import goal_continuations as gc

    gc.REGISTRY_PATH = Path(path)
    gc._REGISTRY = None
    return gc


def _hold_registry_transaction(path: str, acquired, release) -> None:
    gc = _configure(path)
    with gc._registry_transaction():
        acquired.set()
        release.wait(10)


def _schedule_one(path: str, session_id: str, ready, done) -> None:
    gc = _configure(path)
    ready.wait(10)
    gc.schedule_goal_continuation(
        session_id,
        f"prompt-{session_id}",
        source_stream_id=f"source-{session_id}",
        profile_home=None,
        goal_turns_used=1,
        now=time.time(),
    )
    done.set()


def _owner_after_fork(path: str, output) -> None:
    gc = _configure(path)
    output.put((os.getpid(), gc._current_owner_id()))


def test_registry_transaction_blocks_cross_process_lost_update(tmp_path):
    """A second writer cannot load stale JSON while another transaction is open."""
    path = tmp_path / "goal-continuations.json"
    ctx = multiprocessing.get_context("fork")
    acquired = ctx.Event()
    release = ctx.Event()
    writer_ready = ctx.Event()
    writer_done = ctx.Event()

    holder = ctx.Process(target=_hold_registry_transaction, args=(str(path), acquired, release))
    writer = ctx.Process(
        target=_schedule_one,
        args=(str(path), "session-b", writer_ready, writer_done),
    )
    holder.start()
    assert acquired.wait(5)
    writer.start()
    writer_ready.set()
    assert not writer_done.wait(0.3), "second process bypassed the registry transaction lock"

    release.set()
    assert writer_done.wait(5)
    holder.join(5)
    writer.join(5)
    assert holder.exitcode == 0
    assert writer.exitcode == 0

    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data["intents"]) == ["session-b"]
    assert (tmp_path / ".goal-continuations.json.lock").stat().st_mode & 0o777 == 0o600


def test_owner_identity_is_regenerated_after_fork(tmp_path):
    path = tmp_path / "goal-continuations.json"
    ctx = multiprocessing.get_context("fork")
    output = ctx.Queue()
    from api import goal_continuations as gc

    parent_owner = gc._current_owner_id()
    child = ctx.Process(target=_owner_after_fork, args=(str(path), output))
    child.start()
    child_pid, child_owner = output.get(timeout=5)
    child.join(5)

    assert child.exitcode == 0
    assert child_pid != os.getpid()
    assert child_owner != parent_owner
    assert child_owner.startswith(f"webui-{child_pid}-")
