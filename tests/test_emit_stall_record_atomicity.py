"""Deterministic barrier test for PR #7302 finding 4 (review 2026-10-02 06:54):

``emit()`` accepted an event into a subscriber queue and cleared that
subscriber's stall evidence as TWO separate lock acquisitions. The reaper
revalidates eligibility and claims a collection under the SAME ``self._lock``,
so in the gap a subscriber that had just drained, and was refilled by this very
put, looked full again WITH a stale stall record -- and the reaper collected the
channel, evicting the completion it had just accepted to make room for the close
sentinel.

The fix keeps the broadcast and its stall bookkeeping inside one lock
transition: ``put_nowait`` never blocks, so the reaper can only ever observe the
pre-put state (stalled) or the state where the record was cleared by the put
that revived the subscriber.

This test holds the producer between its ``put_nowait`` and its stall-record
update with a gated ``put_nowait`` plus ``threading.Event``s -- the same barrier
shape the review used -- and pins the observable consequence: the accepted
completion is still in the queue and the channel was not collected.
"""

import queue
import threading
import time

import api.background_process as bp
import api.config as cfg


def _open_stall_run(ch, q):
    """Fill ``q`` and land the ``Full`` that opens a stall run (dead-tab shape)."""
    while not q.full():
        ch.emit("bg_task_complete", {"filler": q.qsize()})
    ch.emit("bg_task_complete", {"dropped": True})
    assert q in ch._stalled_since, "the probe never opened a stall run"


def _drain(q):
    drained = 0
    while True:
        try:
            q.get_nowait()
            drained += 1
        except queue.Empty:
            return drained


def test_accepted_completion_survives_a_reaper_tick_inside_the_emit_window():
    """A put that revives a stalled subscriber must not be reaped away."""
    ch = bp.SessionChannel("sess-emit-stall-atomic")
    q = ch.subscribe(maxsize=1)
    _open_stall_run(ch, q)
    assert _drain(q) > 0, "the tab never drained, so no revival is being tested"

    put_done = threading.Event()
    release = threading.Event()
    real_put = q.put_nowait

    def gated_put(item):
        real_put(item)          # the emit path's put lands: the slot is taken again
        put_done.set()
        release.wait(5)         # hold the producer before it can clear the record

    q.put_nowait = gated_put

    emitted = {}

    def producer():
        emitted["delivered"] = ch.emit("bg_task_complete", {"completion": True})

    producer_thread = threading.Thread(target=producer)
    producer_thread.start()
    assert put_done.wait(5), "the emit path never reached its put"

    reaped = {}
    started = threading.Event()

    def reaper():
        started.set()
        reaped["collected"] = ch.collect_if_eligible(
            time.time() + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS + 1
        )

    reaper_thread = threading.Thread(target=reaper)
    reaper_thread.start()
    assert started.wait(5), "the reaper thread never started"
    # Buggy code finishes the collection here; the fixed code is parked on the
    # channel lock the producer holds, which is exactly the serialization asked for.
    reaper_thread.join(1.0)

    release.set()
    producer_thread.join(5)
    reaper_thread.join(5)

    assert not producer_thread.is_alive() and not reaper_thread.is_alive()
    assert emitted.get("delivered") == 1, "the emit path did not accept the completion"

    remaining = []
    while True:
        try:
            remaining.append(q.get_nowait())
        except queue.Empty:
            break

    assert ("bg_task_complete", {"completion": True}) in remaining, (
        "the just-accepted completion was evicted to make room for the close "
        f"sentinel: remaining_queue={remaining}"
    )
    assert reaped.get("collected") is False, (
        "the reaper collected a channel whose subscriber had just accepted the "
        "event: the emit path and the stall-record update are not one transition"
    )


def test_stall_record_is_cleared_under_the_same_lock_as_the_put():
    """The record is gone by the time emit() returns, with no second acquisition."""
    ch = bp.SessionChannel("sess-emit-record-atomic")
    q = ch.subscribe(maxsize=1)
    _open_stall_run(ch, q)
    _drain(q)

    delivered = ch.emit("bg_task_complete", {"completion": True})

    assert delivered == 1
    assert q not in ch._stalled_since, (
        "the stall record outlived the successful put, leaving the revival window open"
    )
