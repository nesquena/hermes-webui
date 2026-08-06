"""Regression test for the bg_task_complete drain-loop tight-spin (#2476, #4633).

``_drain_loop`` read ``process_registry.completion_queue`` directly and wrapped
it in a bare ``except Exception: continue``. A registry missing that attribute
raised ``AttributeError`` that was swallowed and retried with no backoff — a
100%-CPU tight loop with no log line. The loop now:

  * reads the queue via ``getattr(process_registry, 'completion_queue', None)``
    and backs off on the stop event when it is absent (mirrors streaming.py),
  * catches ``queue.Empty`` explicitly (the normal idle path → plain continue),
  * logs + backs off on any other queue error instead of tight-looping.

Uses the shared ``install_fake_registry`` stub helper (see ``_wakeup_helpers``).
"""

from __future__ import annotations

import queue
import threading

from tests._wakeup_helpers import install_fake_registry


class _CountingStop:
    """Stand-in for ``_DRAIN_STOP`` that records ``wait()`` and self-terminates.

    In the OLD (buggy) code the missing-queue path hit ``continue`` and never
    touched the stop event's ``wait`` — it just re-spun on ``is_set()``. So a
    recorded ``wait(1.0)`` call is exactly the proof the backoff path was taken.
    ``wait`` sets the flag after ``exit_after`` calls so the loop exits promptly
    without any real sleep, keeping the test deterministic and fast.
    """

    def __init__(self, exit_after: int = 1):
        self._flag = threading.Event()
        self.wait_calls: list[float | None] = []
        self._exit_after = exit_after

    def is_set(self) -> bool:
        return self._flag.is_set()

    def set(self) -> None:
        self._flag.set()

    def clear(self) -> None:
        self._flag.clear()

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if len(self.wait_calls) >= self._exit_after:
            self._flag.set()
        return self._flag.is_set()


def _run_drain_with_stop(monkeypatch, fake_registry, stop) -> threading.Thread:
    from api import background_process as bp

    install_fake_registry(monkeypatch, fake_registry)
    monkeypatch.setattr(bp, "_DRAIN_STOP", stop, raising=True)
    t = threading.Thread(target=bp._drain_loop, name="test-drain", daemon=True)
    t.start()
    return t


def test_missing_completion_queue_backs_off_instead_of_spinning(monkeypatch):
    """A registry with no completion_queue must back off, not tight-loop."""

    class _NoQueueRegistry:
        pass  # deliberately no completion_queue attribute

    stop = _CountingStop(exit_after=1)
    t = _run_drain_with_stop(monkeypatch, _NoQueueRegistry(), stop)
    t.join(timeout=3.0)

    assert not t.is_alive(), "drain loop did not terminate on stop"
    # The backoff path ran: wait() was called with the 1.0s backoff timeout.
    assert stop.wait_calls, "missing-queue path did not back off (tight loop)"
    assert stop.wait_calls[0] == 1.0


def test_non_empty_queue_error_logs_and_backs_off(monkeypatch):
    """A non-Empty queue error is logged and backed off, not swallowed silently."""

    class _BoomQueue:
        def get(self, timeout=None):
            raise RuntimeError("queue exploded")

    class _BoomRegistry:
        completion_queue = _BoomQueue()

    stop = _CountingStop(exit_after=1)
    t = _run_drain_with_stop(monkeypatch, _BoomRegistry(), stop)
    t.join(timeout=3.0)

    assert not t.is_alive()
    assert stop.wait_calls and stop.wait_calls[0] == 1.0


def test_empty_queue_is_the_plain_continue_path(monkeypatch):
    """queue.Empty is the normal idle path: continue, no backoff wait()."""

    class _EmptyQueue:
        def __init__(self, stop):
            self._stop = stop

        def get(self, timeout=None):
            # Stop the loop after being polled once so the test terminates,
            # then raise Empty like a real idle queue.
            self._stop.set()
            raise queue.Empty

    class _EmptyRegistry:
        pass

    stop = _CountingStop(exit_after=99)  # effectively never self-terminates
    reg = _EmptyRegistry()
    reg.completion_queue = _EmptyQueue(stop)
    t = _run_drain_with_stop(monkeypatch, reg, stop)
    t.join(timeout=3.0)

    assert not t.is_alive()
    # Empty took the explicit `continue` path — no backoff wait() was recorded.
    assert stop.wait_calls == []


if __name__ == "__main__":  # pragma: no cover - manual invocation
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
