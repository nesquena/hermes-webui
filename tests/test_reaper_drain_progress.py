"""A subscriber that drained its queue is alive: a quiet session is not a stall.

Re-gate regression for PR #7302 (review 2026-10-01, finding 1 -- "silent reaper"):

``SessionChannel.emit()`` records ``_stalled_since[q]`` on ``queue.Full`` and only
clears it on a later SUCCESSFUL put. ``_handle_session_sse_stream`` drains with
``q.get()``, which never touches that record, so a subscriber that drained its whole
backlog after a burst -- and then received no further event, which is the normal
shape of a quiet session -- kept its stall run. Once that run aged past
``SESSION_CHANNEL_SUBSCRIBER_STALL_SECS`` the reaper counted it as positive
dead-subscriber evidence and closed a channel whose only subscriber was alive and
caught up.

The fix treats a dequeue as progress: the reaper clears the stall entry of every
subscriber whose queue has a free slot, sampled under the channel lock -- the same
lock the emit path records the run with. Only the consumer removes items, so free
space is proof that the subscriber drained after the ``Full``.

These tests pin the invariant, its observable consequence, and its burden of proof:
a subscriber that really never drains must still be reaped.
"""

import queue
import time

import api.background_process as bp
import api.config as cfg


def _open_stall_run(ch, q):
    """Fill ``q`` (alive consumer absent) and land the ``Full`` that opens a stall run."""
    while not q.full():
        ch.emit("bg_task_complete", {"filler": q.qsize()})
    ch.emit("bg_task_complete", {"dropped": True})  # queue.Full -> stall run opens
    assert q in ch._stalled_since, "the probe never opened a stall run"


def _drain(q):
    """Consume the whole backlog, the way the SSE handler's ``q.get()`` loop does."""
    drained = 0
    while True:
        try:
            q.get_nowait()
            drained += 1
        except queue.Empty:
            return drained


def test_burst_then_drain_with_no_further_emit_is_not_reaped():
    """burst -> drain -> silence is a healthy quiet session, not a dead subscriber."""
    ch = bp.SessionChannel("sess-burst-drain-quiet")
    q = ch.subscribe(maxsize=2)
    _open_stall_run(ch, q)

    assert _drain(q) > 0, "the subscriber never drained its backlog"
    # No further emit: nothing else happens on the session.
    future = time.time() + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS + 1

    assert ch._dead_subscriber_signal(future) is False, (
        "a subscriber that drained its queue was still counted as dead: the reaper "
        "would close a live channel on a quiet session"
    )
    assert ch.reaper_should_collect(future) is False, (
        "the reaper collected a channel whose only subscriber is alive and caught up"
    )


def test_drain_clears_the_recorded_stall_run():
    """The run is cleared (not merely ignored), so it cannot age into evidence later."""
    ch = bp.SessionChannel("sess-drain-clears-run")
    q = ch.subscribe(maxsize=1)
    _open_stall_run(ch, q)
    assert q in ch._stalled_since

    _drain(q)
    ch._dead_subscriber_signal(time.time())

    assert q not in ch._stalled_since, (
        "the drained subscriber kept its stall run: a later quiet window would reap it"
    )


def test_saturated_subscriber_that_never_drains_is_still_reaped():
    """Burden of proof unchanged: a queue that stays full is still death evidence."""
    ch = bp.SessionChannel("sess-truly-stuck")
    q = ch.subscribe(maxsize=1)
    q.put_nowait(("bg_task_complete", {"filler": True}))
    ch.emit("bg_task_complete", {"dropped": True})  # stall run opens; nothing drains
    future = time.time() + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS + 1

    assert q.full(), "the probe let the queue drain, so it is not testing a stuck tab"
    assert ch._dead_subscriber_signal(future) is True, (
        "a subscriber that never drained lost its dead-subscriber signal"
    )
    assert ch.reaper_should_collect(future) is True, (
        "the reaper stopped collecting a genuinely stuck subscriber"
    )


def test_drained_subscriber_protects_the_channel_beside_a_stuck_one():
    """One drained subscriber is still enough to protect the channel (Option X)."""
    ch = bp.SessionChannel("sess-mixed-drained-and-stuck")
    stuck = ch.subscribe(maxsize=1)
    drained = ch.subscribe(maxsize=1)
    stuck.put_nowait(("bg_task_complete", {"filler": True}))
    ch.emit("bg_task_complete", {"dropped-for-both": True})
    _drain(drained)  # the healthy tab catches up
    future = time.time() + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS + 1

    assert ch.reaper_should_collect(future) is False, (
        "a stalled tab evicted a channel that a live, caught-up subscriber still held"
    )
