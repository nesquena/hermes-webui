"""Regression test for the thread-start check-then-act race in background_process.

``start_drain_thread`` and ``start_session_channel_reaper`` checked
``is_alive()`` and then created + started the daemon thread WITHOUT a lock, so
two concurrent callers could both observe "not alive" and each spawn a thread.
The loser's thread was never stored in the module global and ran forever,
un-joinable. Both check-then-start sequences are now serialized under
``_THREAD_LIFECYCLE_LOCK``, so exactly one thread is ever created.

The test drives N callers through a barrier so they hit the check-and-start
simultaneously, and asserts exactly one caller reports "started" (returns True)
and exactly one live thread exists.
"""

from __future__ import annotations

import threading

import pytest


# (start_fn, stop_fn, loop_attr, thread_attr, thread_name_fragment)
_CASES = [
    (
        "start_drain_thread",
        "stop_drain_thread",
        "_drain_loop",
        "_DRAIN_THREAD",
        "bg-task-complete-drain",
    ),
    (
        "start_session_channel_reaper",
        "stop_session_channel_reaper",
        "_reaper_loop",
        "_REAPER_THREAD",
        "session-channel-reaper",
    ),
]


@pytest.mark.parametrize(
    "start_name,stop_name,loop_attr,thread_attr,name_frag", _CASES
)
def test_concurrent_start_creates_exactly_one_thread(
    monkeypatch, start_name, stop_name, loop_attr, thread_attr, name_frag
):
    from api import background_process as bp

    release = threading.Event()

    def _blocking_loop() -> None:
        # Keep the started thread alive during the assertions so a genuinely
        # started thread reads as is_alive()==True deterministically.
        release.wait(5.0)

    monkeypatch.setattr(bp, loop_attr, _blocking_loop, raising=True)
    # Fresh slate: no pre-existing thread reference.
    monkeypatch.setattr(bp, thread_attr, None, raising=True)

    start_fn = getattr(bp, start_name)
    n = 12
    barrier = threading.Barrier(n)
    results: list[bool] = []
    results_lock = threading.Lock()

    def _caller() -> None:
        barrier.wait()
        started = start_fn()
        with results_lock:
            results.append(started)

    callers = [threading.Thread(target=_caller) for _ in range(n)]
    try:
        for c in callers:
            c.start()
        for c in callers:
            c.join(timeout=5.0)

        # Exactly one caller won the create; the rest saw a live thread.
        assert sum(1 for r in results if r) == 1, (
            f"expected exactly one start to win, got {results}"
        )
        # Exactly one live daemon thread carrying this loop's name.
        live = [
            t
            for t in threading.enumerate()
            if name_frag in t.name and t.is_alive()
        ]
        assert len(live) == 1, f"expected 1 live {name_frag} thread, got {len(live)}"
        # The module global points at that one live thread.
        assert getattr(bp, thread_attr) is live[0]
    finally:
        release.set()
        getattr(bp, stop_name)()


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(pytest.main([__file__, "-v"]))
