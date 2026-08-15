"""Regression tests: ``StreamChannel`` per-subscriber queues are bounded.

Each connected browser tab gets its own ``queue.Queue`` to receive broadcast
SSE frames. That queue used to be ``queue.Queue()`` (no maxsize), so a slow /
backpressured / backgrounded tab accumulated every coalesced token frame for the
WHOLE turn (the producer is the agent token stream) — an OOM risk with many tabs
× long agentic turns. The sibling offline buffer was already capped (#4633), but
once a subscriber attached, events flowed into its unbounded per-subscriber
queue.

The subscriber queue is now ``queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)``
and both the broadcast path (``put_nowait``) and the offline-buffer replay path
(``subscribe_with_snapshot``) drop the OLDEST frame on ``queue.Full`` so a slow
tab keeps the most recent tail (older frames stay recoverable via the run
journal by ``last_event_id``).

The cap equals ``_OFFLINE_BUFFER_MAXLEN`` so a reconnecting tab can still replay
the full retained offline tail (the #4633 reconnect contract); the point of the
bound is to cap the previously-unbounded LIVE-broadcast-to-slow-tab growth — a
50K-frame turn can no longer pin all 50000 frames per slow tab.
"""
from __future__ import annotations

import queue as _queue

from api.config import StreamChannel, create_stream_channel


def _drain(q: _queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except _queue.Empty:
            break
    return out


def test_subscriber_queue_has_a_maxsize_bound():
    """The regression: a subscriber queue MUST be bounded (it used to be 0 ==
    unbounded). Assert the cap is set and finite, and matches the documented
    reconnect-replay bound."""
    ch = create_stream_channel()
    q = ch.subscribe()
    assert q.maxsize > 0
    assert q.maxsize == StreamChannel._SUBSCRIBER_QUEUE_MAXSIZE
    assert q.maxsize == StreamChannel._OFFLINE_BUFFER_MAXLEN


def test_broadcast_drops_oldest_when_subscriber_never_drains():
    """A subscriber that never drains stays bounded by the queue cap and keeps
    the most recent tail (drop-oldest), not the oldest frames — even when the
    producer emits far more frames than the cap."""
    ch = create_stream_channel()
    cap = StreamChannel._SUBSCRIBER_QUEUE_MAXSIZE
    q = ch.subscribe()

    # Subscribe a second, draining subscriber so put_nowait takes the live
    # broadcast path (subscribers present) rather than the offline buffer.
    drainer = ch.subscribe()
    total = cap + 500  # produce more than the cap; the slow tab must drop
    for i in range(total):
        ch.put_nowait(("token", i, f"id{i}"))
        # Keep the drainer empty so broadcast always has a live audience.
        _drain(drainer)

    # The never-drained subscriber never exceeded the cap.
    assert q.qsize() <= cap
    kept = _drain(q)
    # The most recent `cap` frames are retained; the 500 older ones were dropped.
    assert len(kept) == cap
    assert kept[0] == ("token", total - cap, f"id{total - cap}")
    assert kept[-1] == ("token", total - 1, f"id{total - 1}")

    # Ops counter surfaced the cumulative drops.
    snap = ch.diagnostic_snapshot()
    assert snap["subscriber_dropped_events"] >= 500


def test_replay_drops_oldest_when_offline_buffer_exceeds_queue_cap():
    """subscribe_with_snapshot replays buffered frames into a bounded queue. The
    default cap equals the offline buffer, so replay just fits and never drops.
    To exercise the replay drop-on-full path, monkeypatch the cap SMALLER than
    the buffer, then call the REAL subscribe_with_snapshot (not a hand-mirrored
    loop) so the test stays coupled to the production code path under test."""
    ch = create_stream_channel()
    buffer_len = StreamChannel._OFFLINE_BUFFER_MAXLEN
    # Drive the offline buffer with no subscriber attached (fills buffer past
    # overflow, so the retained tail is exactly `buffer_len` frames).
    for i in range(buffer_len + 100):
        ch.put_nowait(("token", i, f"id{i}"))

    small_cap = 8
    original_cap = StreamChannel._SUBSCRIBER_QUEUE_MAXSIZE
    StreamChannel._SUBSCRIBER_QUEUE_MAXSIZE = small_cap
    try:
        # The default cap equals the buffer, so a resubscribe normally replays
        # the full retained tail. With the cap forced small, replay must drop
        # oldest and keep the newest `small_cap` frames — exercising the real
        # drop-on-full branch inside subscribe_with_snapshot.
        q, snapshot = ch.subscribe_with_snapshot()
    finally:
        StreamChannel._SUBSCRIBER_QUEUE_MAXSIZE = original_cap

    drained = _drain(q)
    assert len(drained) == small_cap
    # Newest tail retained.
    assert drained[-1] == ("token", buffer_len + 100 - 1, f"id{buffer_len + 100 - 1}")
    # Oldest retained frame is `small_cap` back from the end.
    assert drained[0] == ("token", buffer_len + 100 - small_cap, f"id{buffer_len + 100 - small_cap}")
    # The cumulative drop counter was incremented by the replay path.
    assert ch._subscriber_dropped_total >= 1


def test_broadcast_to_many_subscribers_stays_bounded():
    """Multiple subscribers, one of which never drains, never exceeds any
    queue's cap and the draining subscribers receive every frame."""
    ch = create_stream_channel()
    cap = StreamChannel._SUBSCRIBER_QUEUE_MAXSIZE
    slow = ch.subscribe()
    fast = ch.subscribe()

    total = cap + 10  # a little over the cap is enough to force one drop
    fast_seen = []
    for i in range(total):
        ch.put_nowait(("token", i, f"id{i}"))
        fast_seen.extend(_drain(fast))

    # The fast (draining) subscriber got every frame in order.
    assert fast_seen == [("token", i, f"id{i}") for i in range(total)]
    # The slow subscriber stayed bounded and kept the newest tail.
    assert slow.qsize() <= cap
    slow_kept = _drain(slow)
    assert len(slow_kept) == cap
    assert slow_kept[-1] == ("token", total - 1, f"id{total - 1}")


def test_reconnect_replay_still_delivers_full_offline_tail():
    """The cap must NOT break the #4633 reconnect-replay contract: a tab that
    resubscribes after the offline buffer filled still receives the FULL
    retained offline tail. This holds because the cap equals the offline buffer
    cap — guarding against the regression where the cap was set too small."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    for i in range(n + 100):
        ch.put_nowait(("token", i, f"id{i}"))

    q, snapshot = ch.subscribe_with_snapshot()
    assert snapshot["offline_buffered_events"] == n
    drained = _drain(q)
    # Full retained tail delivered — the cap matches the offline buffer cap.
    assert len(drained) == n
    assert drained[0] == ("token", 100, "id100")  # oldest 100 dropped from buffer
    assert drained[-1] == ("token", n + 100 - 1, f"id{n + 100 - 1}")


def test_unsubscribe_removes_subscriber():
    """Sanity: after unsubscribe, the queue no longer receives broadcasts."""
    ch = create_stream_channel()
    q = ch.subscribe()
    other = ch.subscribe()  # keep a live audience for the broadcast path
    ch.unsubscribe(q)
    ch.put_nowait(("token", 0, "id0"))
    assert q.empty()
    assert _drain(other) == [("token", 0, "id0")]


def test_terminal_frame_survives_concurrent_drain_during_drop_oldest():
    """Regression: on queue.Full the drop-oldest loop used to `break` if a
    concurrent consumer drained the queue between the Full and get_nowait
    (raising queue.Empty). That silently discarded the `item` being enqueued —
    and when `item` was a TERMINAL frame (stream_end/error/cancel) the
    subscriber never received it, leaving the client attached indefinitely
    (spinner-forever). The fix is `continue` (retry the put into the now-empty
    queue), so the terminal frame is always delivered.

    This deterministically simulates the exact interleaving (no threads, which
    would be flaky): a custom queue whose get_nowait raises Empty on the first
    post-Full call (as if a concurrent consumer had drained it), then behaves
    normally. The OLD `break` logic drops the terminal item; the NEW `continue`
    logic delivers it. We run both and assert only `continue` delivers."""
    terminal_item = ("stream_end", None, "terminal-id")

    class _RaceyQueue:
        """Simulates a bounded queue that a concurrent consumer drained between
        the producer's Full and get_nowait: the first put raises Full (queue at
        cap), then by the time the producer calls get_nowait the consumer has
        drained the ENTIRE queue, so get_nowait raises Empty. Subsequent puts
        succeed (the queue has space) — which is exactly the condition where the
        fix's `continue` must retry instead of the bug's `break`."""
        def __init__(self):
            self._real = _queue.Queue(maxsize=8)
            self._drained = False
            # Pre-fill to cap so the first put_nowait raises Full.
            for i in range(8):
                self._real.put_nowait(("token", i, f"id{i}"))

        def put_nowait(self, item):
            return self._real.put_nowait(item)

        def get_nowait(self):
            # First eviction attempt: simulate the concurrent consumer having
            # drained the whole queue — raise Empty (no oldest to drop).
            if not self._drained:
                self._drained = True
                # Empty the real queue to model the drain, then raise Empty so
                # the producer sees the race.
                while True:
                    try:
                        self._real.get_nowait()
                    except _queue.Empty:
                        break
                raise _queue.Empty
            return self._real.get_nowait()

    def run_drop_oldest_loop(on_empty):
        """Mirror the StreamChannel broadcast drop-oldest loop with a chosen
        on-Empty action (break = pre-fix bug, continue = fix)."""
        rq = _RaceyQueue()
        delivered = []
        while True:
            try:
                rq.put_nowait(terminal_item)
                delivered.append(terminal_item)
                break
            except _queue.Full:
                try:
                    rq.get_nowait()
                except _queue.Empty:
                    if on_empty == "break":
                        break  # the bug: silently discard terminal_item
                    else:
                        continue  # the fix: retry the put
        return delivered

    # Pre-fix (break): the terminal frame is silently dropped.
    delivered_break = run_drop_oldest_loop("break")
    assert delivered_break == [], (
        "pre-fix `break` should have silently dropped the terminal frame"
    )
    # Post-fix (continue): the terminal frame is delivered.
    delivered_continue = run_drop_oldest_loop("continue")
    assert delivered_continue == [terminal_item], (
        "post-fix `continue` must deliver the terminal frame after the race"
    )

