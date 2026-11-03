"""Shared helper for the updater restart-worker isolation fix (#8687).

``api.updates._schedule_restart()`` does two things:

1. ``run_drain.mark_restart_pending()`` — closes the run-admission fence
   until the process image is replaced;
2. spawns a daemon worker that sleeps for ``delay`` and then takes
   ``api.updates._apply_lock`` to sweep ``__pycache__`` before ``os.execv``.

Step 2 is what makes the function untestable in isolation: the worker is a
daemon thread that outlives the calling test. While it is alive it holds
``_apply_lock`` while it walks the *real* repository's ``__pycache__`` tree,
so every later ``apply_update()`` / ``apply_force_update()`` in the same
pytest session is refused with ``Update already in progress``.

A bare stub of ``_schedule_restart`` is not enough for the tests that assert
on the admission fence, because the fence is closed by step 1 — the very step
a naive stub removes. :func:`isolated_restart_scheduler` therefore keeps
step 1 (the admission gate the tests assert on) and drops only step 2 (the
worker thread), so the fence semantics under test stay intact and nothing
escapes the test.

Usage::

    from tests._updater_restart_isolation import isolated_restart_scheduler

    def test_something(monkeypatch):
        with isolated_restart_scheduler(monkeypatch) as scheduled:
            ...
            assert scheduled.calls == [60]
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager


@contextmanager
def isolated_restart_scheduler(monkeypatch):
    """Stub ``_schedule_restart`` without letting a restart worker escape.

    Yields a recorder with a ``calls`` list holding the ``delay`` value of
    every scheduled restart. The stub closes the run-admission fence exactly
    like production does (via ``run_drain.mark_restart_pending()``) but does
    not spawn the worker thread, so no test leaves a live thread holding
    ``api.updates._apply_lock`` behind it.

    The stub is installed through the caller's ``monkeypatch`` fixture, so
    pytest restores the real function at test teardown — the context manager
    deliberately does not undo the fixture itself, because that would also
    revert the caller's own unrelated monkeypatches.
    """
    from api import run_drain
    from api import updates

    recorder = _RestartRecorder()

    def _stub_schedule_restart(delay: float = 2.0) -> None:
        recorder.calls.append(delay)
        # Production closes the admission fence here, before the worker
        # starts. Keep that: the fence is what the calling test asserts on.
        run_drain.mark_restart_pending()

    monkeypatch.setattr(updates, "_schedule_restart", _stub_schedule_restart)
    yield recorder


class _RestartRecorder:
    """Records the restart delays a stubbed ``_schedule_restart`` received."""

    def __init__(self) -> None:
        self.calls: list[float] = []


def live_restart_workers() -> list[str]:
    """Return the names of the restart workers that are still alive.

    ``_schedule_restart`` names its worker ``_do``, so a live worker shows up
    as ``Thread-N (_do)``.
    """
    return [t.name for t in threading.enumerate() if t.name.endswith("(_do)")]


def apply_lock_is_free() -> bool:
    """Return True when ``api.updates._apply_lock`` can be acquired right now."""
    from api import updates

    acquired = updates._apply_lock.acquire(blocking=False)
    if acquired:
        updates._apply_lock.release()
    return acquired


def assert_no_restart_worker_escaped(timeout: float = 5.0) -> None:
    """Fail if a restart worker still holds ``_apply_lock``.

    A leaked worker is a daemon thread that sleeps for its ``delay`` and only
    then takes ``_apply_lock``, so the lock is not held immediately after the
    leaking test returns. Wait for the delay window to elapse before
    asserting, otherwise the check passes for the wrong reason.
    """
    deadline = timeout
    step = 0.25
    waited = 0.0
    while waited < deadline:
        if not apply_lock_is_free():
            raise AssertionError(
                "a restart worker still holds api.updates._apply_lock "
                f"after {waited:.2f}s — it will refuse later apply_update() calls"
            )
        if not live_restart_workers():
            return
        waited += step
        time.sleep(step)

    raise AssertionError(
        f"a restart worker thread is still alive after {deadline:.2f}s: "
        f"{live_restart_workers()}"
    )
