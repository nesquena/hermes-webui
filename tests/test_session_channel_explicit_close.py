"""PR #7302 v2 — explicit SessionChannel close protocol (sentinel ``None``).

These are the BEHAVIOR tests requested in the review of PR #7302
(@nesquena-hermes, 03-Sep-2026, requirement 3). They exercise the real
registry + ``_reaper_loop`` + handler contract rather than asserting on
source strings:

  * a live, draining subscriber keeps the channel alive past the idle TTL
    (the existing pinned invariant — NOT weakened);
  * a saturated subscriber is only collected after the positive stall window
    (``SESSION_CHANNEL_SUBSCRIBER_STALL_SECS``), never by age alone;
  * ``close()`` delivers the ``None`` sentinel to every subscriber, which is
    what makes ``_handle_session_sse_stream()`` break out of its
    ``q.get()`` loop, run ``unsubscribe()`` and close the response so the
    browser's ``EventSource`` reconnects;
  * the reaper closes BEFORE detaching the registry entry, and a fresh channel
    is created on the next subscribe (reconnection onto the replacement);
  * a late subscriber on an already-closed channel is not left hanging.
"""

import queue
import threading
import time

import pytest

from api import background_process as bp
from api import config as cfg


def _drive_reaper_until(predicate, timeout: float = 3.0, interval: float = 0.02):
    """Run the REAL ``_reaper_loop`` in a thread until ``predicate()`` holds.

    Monkeypatching ``_REAPER_INTERVAL_SECS`` keeps the tick tight so the test
    stays fast without faking the loop body (the previous coverage replicated
    the loop by hand, which could never catch a close-before-detach regression).
    """
    original = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = interval
    started = bp.start_session_channel_reaper()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()
    finally:
        bp.stop_session_channel_reaper()
        bp._REAPER_INTERVAL_SECS = original
        del started  # only recorded for symmetry with start/stop pairing


# ---------------------------------------------------------------------------
# close(): the explicit protocol
# ---------------------------------------------------------------------------


def test_close_signals_sentinel_to_every_subscriber():
    """close() must hand every subscriber the ``None`` sentinel."""
    ch = bp.SessionChannel("sess-close-many")
    q1 = ch.subscribe()
    q2 = ch.subscribe()

    signalled = ch.close("test")

    assert signalled == 2
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None
    assert ch.subscriber_count() == 0
    assert ch.closed is True


def test_close_is_idempotent():
    """A second close() is a no-op — it must not double-signal or raise."""
    ch = bp.SessionChannel("sess-close-twice")
    q = ch.subscribe()

    assert ch.close("first") == 1
    assert ch.close("second") == 0
    assert q.get_nowait() is None
    # Only the one sentinel — no duplicate left behind.
    with pytest.raises(queue.Empty):
        q.get_nowait()


def test_close_evicts_stale_entry_when_queue_is_saturated():
    """A full buffer must still receive the sentinel, or the handler hangs."""
    ch = bp.SessionChannel("sess-close-full")
    q = ch.subscribe(maxsize=1)
    q.put_nowait(("bg_task_complete", {"stale": True}))  # saturate

    signalled = ch.close("saturated")

    assert signalled == 1
    # The stale entry was evicted so the sentinel could land.
    assert q.get_nowait() is None


def test_late_subscriber_on_closed_channel_is_not_stranded():
    """Subscribing after close() returns a queue that already holds the sentinel."""
    ch = bp.SessionChannel("sess-close-late")
    ch.close("already-closed")

    q = ch.subscribe()

    assert q.get_nowait() is None


def test_closed_channel_is_collectible_even_with_subscribers():
    """After close(), the reaper may detach the entry on its next tick."""
    ch = bp.SessionChannel("sess-close-collectible")
    ch.subscribe()
    assert ch.reaper_should_collect(time.time()) is False

    ch.close("collectible")

    assert ch.reaper_should_collect(time.time()) is True


# ---------------------------------------------------------------------------
# The live-subscriber invariant (unchanged by this PR)
# ---------------------------------------------------------------------------


def test_live_subscriber_keeps_channel_past_idle_ttl():
    """A draining subscriber must NEVER be collected for being old."""
    ch = bp.SessionChannel("sess-live-past-ttl")
    q = ch.subscribe()
    ch.created_at = time.time() - (cfg.SESSION_CHANNEL_IDLE_TTL_SECS + 100)
    ch.emit("bg_task_complete", {"ok": True})  # the tab drains → healthy
    assert q.get_nowait() is not None

    assert ch.reaper_should_collect(time.time()) is False
    # ...and still not at the shallow end of the stall window.
    assert ch.reaper_should_collect(time.time() + 10) is False


# ---------------------------------------------------------------------------
# Positive dead-subscriber signal
# ---------------------------------------------------------------------------


def test_reaper_collects_saturated_subscriber_after_stall_window():
    """Only a full stall window — never age — may evict a subscribed channel."""
    ch = bp.SessionChannel("sess-stalled")
    ch.subscribe(maxsize=1)
    ch.emit("bg_task_complete", {"filler": True})  # fills the buffer
    ch.emit("bg_task_complete", {"dropped": True})  # queue.Full → stall starts
    now = time.time()

    assert ch.reaper_should_collect(now) is False
    assert (
        ch.reaper_should_collect(now + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS - 1)
        is False
    )
    assert (
        ch.reaper_should_collect(now + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS + 1)
        is True
    )


def test_reaper_keeps_channel_when_any_subscriber_still_drains():
    """One healthy tab protects the channel from another tab's stall."""
    ch = bp.SessionChannel("sess-mixed")
    stalled = ch.subscribe(maxsize=1)
    stalled.put_nowait(("bg_task_complete", {"filler": True}))
    ch.subscribe()  # healthy, roomy subscriber
    ch.emit("bg_task_complete", {"dropped-for-stalled": True})
    future = time.time() + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS + 1

    assert ch.reaper_should_collect(future) is False


def test_stall_run_is_cleared_once_the_subscriber_drains():
    """A resumed tab must not carry an old stall run into the next window."""
    ch = bp.SessionChannel("sess-stall-cleared")
    q = ch.subscribe(maxsize=1)
    q.put_nowait(("bg_task_complete", {"filler": True}))
    ch.emit("bg_task_complete", {"dropped": True})  # stall recorded
    q.get_nowait()  # tab drains again
    ch.emit("bg_task_complete", {"delivered": True})  # success clears the run
    future = time.time() + cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS + 1

    assert ch.reaper_should_collect(future) is False


# ---------------------------------------------------------------------------
# Registry integration: close BEFORE detach, then reconnect
# ---------------------------------------------------------------------------


def test_reaper_loop_closes_subscribers_before_detaching():
    """The real reaper loop signals the handler, then drops the entry."""
    sid = "sess-reaper-close-then-detach"
    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe(maxsize=1)
    q.put_nowait(("bg_task_complete", {"filler": True}))
    ch.emit("bg_task_complete", {"dropped": True})  # start the stall run
    # Rewind the stall run so the very next tick is already past the window.
    with ch._lock:
        for subscriber in list(ch._stalled_since):
            ch._stalled_since[subscriber] = (
                time.time() - cfg.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS - 1
            )

    collected = _drive_reaper_until(lambda: bp.get_session_channel(sid) is None)

    assert collected, "reaper never collected the stalled channel"
    # The handler was unblocked BEFORE the detach: the sentinel is already there.
    assert q.get_nowait() is None
    assert ch.closed is True


def test_reconnect_lands_on_a_fresh_channel_after_close():
    """After collection, a reconnecting tab gets a NEW, working channel."""
    sid = "sess-reconnect-fresh"
    first = bp.get_or_create_session_channel(sid)
    q_old = first.subscribe()
    first.close("simulated reconnect")

    assert q_old.get_nowait() is None  # handler breaks → EventSource reconnects
    bp.SESSION_CHANNELS.pop(sid, None)  # reaper tick

    second = bp.get_or_create_session_channel(sid)

    assert second is not first
    assert second.closed is False
    q_new = second.subscribe()
    assert second.emit("bg_task_complete", {"after": "reconnect"}) == 1
    assert q_new.get_nowait() == ("bg_task_complete", {"after": "reconnect"})


def test_handler_loop_contract_sentinel_exits_and_unsubscribes():
    """Mirror of ``_handle_session_sse_stream``'s queue loop.

    The handler breaks on ``None`` and its ``finally`` runs ``unsubscribe``.
    This pins the contract the explicit close protocol depends on: without the
    sentinel the loop would keep writing keepalives forever (the exact defect
    the review called out), so a regression here must fail loudly.
    """
    sid = "sess-handler-contract"
    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    delivered = []
    exited = threading.Event()
    handler_interval = 0.02

    def handler():
        try:
            while True:
                try:
                    payload = q.get(timeout=handler_interval)
                except queue.Empty:
                    continue
                if payload is None:
                    break
                delivered.append(payload)
        finally:
            ch.unsubscribe(q)
            exited.set()

    thread = threading.Thread(target=handler, daemon=True)
    thread.start()
    ch.emit("bg_task_complete", {"before": "close"})
    deadline = time.time() + 2
    while not delivered and time.time() < deadline:
        time.sleep(0.01)
    assert delivered == [("bg_task_complete", {"before": "close"})]
    assert not exited.is_set()

    ch.close("handler-contract")

    assert exited.wait(2.0), "handler never exited after the sentinel"
    assert ch.subscriber_count() == 0  # finally ran unsubscribe
    thread.join(timeout=2)
    bp.SESSION_CHANNELS.pop(sid, None)
