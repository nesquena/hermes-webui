"""
Regression tests for #4633 — StreamChannel's offline replay buffer is capped.

While no browser tab is subscribed, ``StreamChannel.put_nowait`` buffers each
SSE event so a first/reconnecting subscriber can replay the stream tail. A
client that disconnects WITHOUT cancelling leaves the turn running with zero
subscribers, and the buffer used to be an unbounded ``list`` — it grew for the
whole turn (a busy turn emits thousands of coalesced token frames), an OOM risk
per abandoned turn.

The buffer is now a ``collections.deque(maxlen=_OFFLINE_BUFFER_MAXLEN)`` that
evicts the OLDEST frame when full (a reconnecting tab needs the tail; older
frames stay recoverable via the run journal by ``last_event_id``). The broadcast
path when a subscriber IS attached is unchanged.
"""
import logging

from api.config import StreamChannel, create_stream_channel


def test_offline_buffer_capped_and_drops_oldest():
    """put without a subscriber past the cap keeps len <= N and evicts oldest."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    total = n + 500
    for i in range(total):
        ch.put_nowait(("token", i, f"id{i}"))

    snap = ch.diagnostic_snapshot()
    assert snap["offline_buffered_events"] == n
    assert snap["offline_dropped_events"] == 500
    # The oldest 500 frames were evicted; the newest tail is retained.
    assert list(ch._offline_buffer)[0] == ("token", 500, "id500")
    assert list(ch._offline_buffer)[-1] == ("token", total - 1, f"id{total - 1}")


def test_reconnecting_subscriber_replays_capped_tail():
    """A subscriber attaching after overflow receives exactly the retained tail."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    for i in range(n + 100):
        ch.put_nowait(("token", i, f"id{i}"))

    q, snapshot = ch.subscribe_with_snapshot()
    assert snapshot["offline_buffered_events"] == n
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert len(drained) == n
    assert drained[0][1] == 100          # oldest 100 dropped
    assert drained[-1][1] == n + 100 - 1  # newest retained


def test_last_event_id_tracks_newest_after_eviction():
    """The last_event_id contract is unaffected by buffer eviction."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    for i in range(n + 10):
        ch.put_nowait(("token", i, f"id{i}"))
    assert ch._last_event_id == f"id{n + 10 - 1}"


def test_under_cap_buffers_all_and_drops_none():
    """Below the cap the buffer behaves exactly as before (no drops)."""
    ch = create_stream_channel()
    for i in range(10):
        ch.put_nowait(("token", i))
    snap = ch.diagnostic_snapshot()
    assert snap["offline_buffered_events"] == 10
    assert snap["offline_dropped_events"] == 0


def test_subscribed_path_unbuffered_and_broadcasts():
    """With a subscriber attached, events broadcast directly and don't buffer."""
    ch = create_stream_channel()
    sub = ch.subscribe()
    ch.put_nowait(("token", "x", "idx"))
    assert ch.diagnostic_snapshot()["offline_buffered_events"] == 0
    assert sub.get_nowait() == ("token", "x", "idx")


def test_reconnect_snapshot_exposes_dropped_count():
    """A reconnecting subscriber can detect a truncated replay tail: the snapshot
    carries offline_dropped_events so it can fall back to the run journal."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    for i in range(n + 7):
        ch.put_nowait(("token", i, f"id{i}"))
    _q, snapshot = ch.subscribe_with_snapshot()
    assert snapshot["offline_dropped_events"] == 7
    assert snapshot["last_event_id"] == f"id{n + 7 - 1}"


def test_snapshot_dropped_count_is_per_cycle_not_carried_over():
    """The snapshot truncation signal must be PER disconnect cycle: a subscriber
    arriving during a later CLEAN cycle must see 0, not a stale cumulative
    carry-over from an earlier overflow (which would be a false 'truncated'
    positive). The cumulative total lives in diagnostic_snapshot for ops."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    # cycle 1: overflow
    for i in range(n + 5):
        ch.put_nowait(("token", i))
    # reconnect drains the buffer and resets the per-cycle count
    q = ch.subscribe()
    ch.put_nowait(("token", "live"))
    ch.unsubscribe(q)
    # cycle 2: CLEAN (well under the cap, no eviction)
    for i in range(10):
        ch.put_nowait(("token", ("c2", i)))
    _q2, snapshot = ch.subscribe_with_snapshot()
    assert snapshot["offline_dropped_events"] == 0, (
        "reconnect snapshot carried over a cumulative drop count (false positive)"
    )
    # cumulative total is still visible to ops
    assert ch.diagnostic_snapshot()["offline_dropped_events"] == 5


def test_eviction_logs_once_per_disconnect_cycle(caplog):
    """The one-shot eviction debug log must fire again after a reconnect cycle,
    not stay silenced forever by the cumulative counter (which stays cumulative
    for diagnostics)."""
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN

    def overflow(extra):
        for i in range(n + extra):
            ch.put_nowait(("token", i))

    with caplog.at_level(logging.DEBUG, logger="api.config"):
        # cycle 1 (no subscriber) → one log
        overflow(3)
        assert caplog.text.count("offline buffer full") == 1
        # a subscriber drains the buffer → resets the per-cycle log guard
        q = ch.subscribe()
        ch.put_nowait(("token", "live"))
        ch.unsubscribe(q)
        # cycle 2 → logs again (was previously silenced by the cumulative gate)
        overflow(2)
        assert caplog.text.count("offline buffer full") == 2

    # cumulative dropped counter is preserved across cycles (not reset)
    assert ch.diagnostic_snapshot()["offline_dropped_events"] == 5


def test_transient_attach_does_not_silence_truncation_signal():
    """The drop signal is scoped to the BUFFER, not to an attach cycle.

    A subscribe drain is a non-destructive copy: after a transient attach
    (drain + detach with no live frame in between) the buffer is STILL the
    same truncated tail, so the next subscriber MUST still see
    offline_dropped_events > 0 — resetting it at unsubscribe would hand that
    subscriber a silently-holed replay marked clean (the exact regression this
    PR exists to prevent). Whether the reconnect actually NEEDS the evicted
    frames is decided server-side against offline_first_event_id.
    """
    ch = create_stream_channel()
    n = StreamChannel._OFFLINE_BUFFER_MAXLEN
    for i in range(n + 25):
        ch.put_nowait(("token", i, f"id{i}"))

    # Transient attach: drains a copy, detaches before any live frame.
    q, snapshot = ch.subscribe_with_snapshot()
    assert snapshot["offline_dropped_events"] == 25
    ch.unsubscribe(q)

    # The buffer content did not change, so neither may the signal.
    q2, snapshot2 = ch.subscribe_with_snapshot()
    assert snapshot2["offline_dropped_events"] == 25
    assert snapshot2["offline_first_event_id"] == "id25"
    head = q2.get_nowait()
    assert head[2] == "id25"  # replay head really is the post-eviction frame
    ch.unsubscribe(q2)

    # A live broadcast clears buffer AND signal together (the only reset
    # point): the next disconnect cycle starts clean.
    q3 = ch.subscribe()
    ch.put_nowait(("token", "live", f"id{n + 25}"))
    ch.unsubscribe(q3)
    _q4, snapshot4 = ch.subscribe_with_snapshot()
    assert snapshot4["offline_dropped_events"] == 0
    ch.unsubscribe(_q4)
    # Cumulative ops counter is never reset.
    assert ch.diagnostic_snapshot()["offline_dropped_events"] == 25


def test_snapshot_exposes_first_buffered_event_id():
    """The SSE handler bounds its journal-coverage window at the first retained
    frame — the snapshot must expose that frame's event id (None when the
    buffer is empty or the head frame carries no id)."""
    ch = create_stream_channel()
    _q, empty_snapshot = ch.subscribe_with_snapshot()
    assert empty_snapshot["offline_first_event_id"] is None
    ch.unsubscribe(_q)

    ch2 = create_stream_channel()
    ch2.put_nowait(("token", "a", "run:41"))
    ch2.put_nowait(("token", "b", "run:42"))
    _q2, snapshot = ch2.subscribe_with_snapshot()
    assert snapshot["offline_first_event_id"] == "run:41"
    ch2.unsubscribe(_q2)

    # 2-tuple (id-less) head frames degrade to None, not an IndexError.
    ch3 = create_stream_channel()
    ch3.put_nowait(("token", "no-id"))
    _q3, snapshot3 = ch3.subscribe_with_snapshot()
    assert snapshot3["offline_first_event_id"] is None
