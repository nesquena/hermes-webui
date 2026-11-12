"""Regression coverage for proxy-ACKed half-open SSE subscribers (#7105).

A reverse proxy can keep acknowledging the server's five-second heartbeat after
the downstream browser has disappeared. Socket keepalive/write deadlines then
see a healthy TCP peer while each handler thread and subscriber queue stay
pinned forever. The server must bound each subscription generation and let a
live EventSource renew it by reconnecting.
"""

from __future__ import annotations

import io
import queue
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import api.background_process as background_process
import api.config as config
import api.gateway_watcher as gateway_watcher
import api.models as models
import api.routes as routes


class _AcceptingWriter:
    """Proxy-shaped writer: every heartbeat is accepted immediately."""

    def __init__(self) -> None:
        self.body = bytearray()
        self.first_write = threading.Event()

    def write(self, data: bytes) -> int:
        self.body.extend(data)
        self.first_write.set()
        return len(data)

    def flush(self) -> None:
        pass


class _FakeHandler:
    def __init__(self) -> None:
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.headers = {}
        self.wfile = _AcceptingWriter()
        self.rfile = io.BytesIO()
        self.close_connection = False

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.sent_headers.append((name, value))

    def end_headers(self) -> None:
        pass


def _run_bounded(target, *, timeout: float = 0.75) -> None:
    finished = threading.Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            target()
        except BaseException as exc:  # surfaced on the test thread below
            errors.append(exc)
        finally:
            finished.set()

    threading.Thread(target=worker, daemon=True).start()
    assert finished.wait(timeout), "proxy-ACKed SSE handler outlived its subscriber lease"
    assert not errors, errors


def _short_lease(monkeypatch) -> None:
    monkeypatch.setattr(routes, "_SSE_SUBSCRIBER_LEASE_SECONDS", 0.04, raising=False)
    monkeypatch.setattr(routes, "_SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda handler: None)


def test_session_channel_lease_releases_only_the_expired_generation(monkeypatch):
    _short_lease(monkeypatch)
    old_q: queue.Queue = queue.Queue()
    new_q: queue.Queue = queue.Queue()

    class Channel:
        def __init__(self) -> None:
            self.subscribers = [old_q]
            self.unsubscribed: list[queue.Queue] = []

        def unsubscribe(self, q: queue.Queue) -> None:
            self.subscribers.remove(q)
            self.unsubscribed.append(q)

        def note_subscriber_write_ok(self, _q: queue.Queue) -> None:
            pass  # writer-liveness mark (#7302); irrelevant to the lease

    channel = Channel()
    monkeypatch.setattr(
        background_process,
        "subscribe_to_session_channel",
        lambda sid, maxsize=64, after_event_id=None: (channel, old_q),
    )
    monkeypatch.setattr(background_process, "active_stream_id_for_session", lambda sid: None)
    monkeypatch.setattr(background_process, "persisted_message_count_for_session", lambda sid: None)
    monkeypatch.setattr(background_process, "should_emit_session_updated", lambda known, current: False)

    handler = _FakeHandler()

    def run() -> None:
        routes._handle_session_sse_stream(
            handler,
            urlparse("http://example.test/api/session/stream?session_id=s-1"),
        )

    finished = threading.Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            run()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    threading.Thread(target=worker, daemon=True).start()
    assert handler.wfile.first_write.wait(0.5), "session stream never sent its initial frame"
    channel.subscribers.append(new_q)  # a reconnect/new tab owns a newer generation
    assert finished.wait(0.75), "proxy-ACKed session stream outlived its lease"
    assert not errors, errors
    assert channel.unsubscribed == [old_q]
    assert channel.subscribers == [new_q]
    assert handler.close_connection is True
    assert ("Connection", "close") not in handler.sent_headers


def test_session_channel_reconnect_recovers_turn_completed_during_lease_gap(monkeypatch):
    """The forced rotation must reuse the existing completed-gap self-heal."""
    _short_lease(monkeypatch)
    queues = [queue.Queue(), queue.Queue()]
    persisted_count = {"value": 0}

    class Channel:
        def __init__(self) -> None:
            self.unsubscribed = []

        def unsubscribe(self, q: queue.Queue) -> None:
            self.unsubscribed.append(q)

        def note_subscriber_write_ok(self, _q: queue.Queue) -> None:
            pass  # writer-liveness mark (#7302); irrelevant to the lease

    channel = Channel()
    monkeypatch.setattr(
        background_process,
        "subscribe_to_session_channel",
        lambda sid, maxsize=64, after_event_id=None: (channel, queues.pop(0)),
    )
    monkeypatch.setattr(background_process, "active_stream_id_for_session", lambda sid: None)
    monkeypatch.setattr(
        background_process,
        "persisted_message_count_for_session",
        lambda sid: persisted_count["value"],
    )

    first = _FakeHandler()
    _run_bounded(
        lambda: routes._handle_session_sse_stream(
            first,
            urlparse("http://example.test/api/session/stream?session_id=s-gap&known_count=0"),
        )
    )

    # The browser is between EventSource generations while a server-owned turn
    # finishes. Its durable transcript count advances even though the ephemeral
    # bg_task_complete/server_turn_started frames had no connected subscriber.
    persisted_count["value"] = 2

    second = _FakeHandler()
    _run_bounded(
        lambda: routes._handle_session_sse_stream(
            second,
            urlparse("http://example.test/api/session/stream?session_id=s-gap&known_count=0"),
        )
    )

    assert len(channel.unsubscribed) == 2
    assert b"event: session-updated" in second.wfile.body
    assert b'"message_count": 2' in second.wfile.body


def test_session_channel_replays_event_emitted_between_lease_generations(monkeypatch):
    """Forward-only completion frames need cursor replay across forced EOF."""
    _short_lease(monkeypatch)
    sid = "s-replay"
    channel = background_process.SessionChannel(sid)
    with background_process.SESSION_CHANNELS_LOCK:
        background_process.SESSION_CHANNELS[sid] = channel
    monkeypatch.setattr(background_process, "active_stream_id_for_session", lambda _sid: None)
    monkeypatch.setattr(background_process, "persisted_message_count_for_session", lambda _sid: None)

    first = _FakeHandler()
    first_done = threading.Event()

    def first_worker() -> None:
        try:
            routes._handle_session_sse_stream(
                first,
                urlparse(f"http://example.test/api/session/stream?session_id={sid}"),
            )
        finally:
            first_done.set()

    threading.Thread(target=first_worker, daemon=True).start()
    assert first.wfile.first_write.wait(0.5)
    assert first_done.wait(0.75)
    initial_ids = [
        line.split(b": ", 1)[1]
        for line in first.wfile.body.splitlines()
        if line.startswith(b"id: ")
    ]
    assert len(initial_ids) == 1, first.wfile.body
    initial_cursor = initial_ids[0].decode("utf-8")

    # Exact production race: the durable completion lands after the old queue
    # is gone but before EventSource creates the replacement HTTP request. No
    # prior completion event exists, so the initial frame's cursor is the only
    # way native EventSource can request this gap event.
    assert channel.emit(
        "bg_task_complete",
        {"event_id": "evt-2", "session_id": sid},
    ) == 0

    second = _FakeHandler()
    second.headers["Last-Event-ID"] = initial_cursor
    _run_bounded(
        lambda: routes._handle_session_sse_stream(
            second,
            urlparse(f"http://example.test/api/session/stream?session_id={sid}"),
        )
    )

    # The payload keeps its event_id (browser dedupe) …
    assert second.wfile.body.count(b'"event_id": "evt-2"') == 1
    # … but the wire ``id:`` is the frame's own replay cursor, never the
    # payload event_id (which the dual-name emit can repeat across frames).
    second_ids = [
        line.split(b": ", 1)[1].decode("utf-8")
        for line in second.wfile.body.splitlines()
        if line.startswith(b"id: ")
    ]
    assert second_ids[0] == initial_cursor  # replay queued: incoming cursor echoed
    assert second_ids[1:] == [channel._frame_cursor(1)]
    assert b"id: evt-2" not in second.wfile.body
    with background_process.SESSION_CHANNELS_LOCK:
        background_process.SESSION_CHANNELS.pop(sid, None)


def test_session_channel_does_not_replay_history_without_a_known_cursor():
    channel = background_process.SessionChannel("s-fresh")
    channel.emit(
        "bg_task_complete",
        {"event_id": "old-event", "session_id": "s-fresh"},
    )

    fresh = channel.subscribe(maxsize=64)
    unknown = channel.subscribe(maxsize=64, after_event_id="not-in-history")
    try:
        assert fresh.empty()
        assert unknown.empty()
        assert fresh._session_channel_initial_event_id.startswith("session-channel:")
        assert unknown._session_channel_initial_event_id.startswith("session-channel:")
        # Both cursors encode the CURRENT watermark (1) and are bound to this
        # channel incarnation's token, so a later reconnect replays only what
        # lands after this point and only on this channel.
        assert fresh._session_channel_initial_event_id == channel._frame_cursor(1)
        assert unknown._session_channel_initial_event_id == channel._frame_cursor(1)
    finally:
        channel.unsubscribe(fresh)
        channel.unsubscribe(unknown)


def test_cursor_from_another_channel_incarnation_fails_closed():
    """A reaped-and-recreated (or pre-restart) channel restarts ``seq`` at 0.

    A cursor minted by the OLD incarnation must not be interpreted in the new
    channel's sequence space: ``@2`` from the old channel would pass the
    contiguity check against new history ``1..4`` and replay only ``3, 4`` even
    though the client never saw ``1, 2``. Reject the foreign token and fail
    closed: no replay, and a fresh cursor bound to the new incarnation.
    """
    sid = "s-reincarnated"
    old = background_process.SessionChannel(sid)
    old_sub = old.subscribe(maxsize=64)
    old.emit("bg_task_complete", {"event_id": "old-1", "session_id": sid})
    old.emit("bg_task_complete", {"event_id": "old-2", "session_id": sid})
    stale_cursor = old_sub.get_nowait().cursor  # frame cursor for seq 1
    old_sub.get_nowait()
    old.unsubscribe(old_sub)

    new = background_process.SessionChannel(sid)  # reaper collected ``old``
    for n in range(1, 5):
        new.emit("bg_task_complete", {"event_id": f"new-{n}", "session_id": sid})

    # Same grammar and a seq that would be "contiguous" here (1 >= 1 - 1)...
    assert background_process._parse_channel_cursor_watermark(stale_cursor) == 1
    reconnect = new.subscribe(maxsize=64, after_event_id=stale_cursor)
    try:
        # ...but the token names another incarnation: fail closed.
        assert reconnect.empty()
        assert reconnect._session_channel_replay_count == 0
        assert reconnect._session_channel_initial_event_id == new._frame_cursor(4)
        assert reconnect._session_channel_initial_event_id != stale_cursor
    finally:
        new.unsubscribe(reconnect)

    # The new incarnation's own cursor still replays normally.
    own = new.subscribe(maxsize=64, after_event_id=new._frame_cursor(2))
    try:
        assert own._session_channel_replay_count == 2
        assert [d["event_id"] for _e, d in (own.get_nowait(), own.get_nowait())] == ["new-3", "new-4"]
    finally:
        new.unsubscribe(own)


def test_aged_channel_keeps_reconnect_grace_and_gap_history(monkeypatch):
    monkeypatch.setattr(config, "SESSION_CHANNEL_IDLE_TTL_SECS", 10)
    monkeypatch.setattr(config, "SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS", 60)
    channel = background_process.SessionChannel("s-aged")
    channel.created_at -= 11

    first = channel.subscribe(maxsize=64)
    cursor = first._session_channel_initial_event_id
    channel.unsubscribe(first)
    channel.emit(
        "bg_task_complete",
        {"event_id": "aged-gap-event", "session_id": "s-aged"},
    )

    # A five-minute lease can expire after the channel has existed for four
    # hours. The ordinary 60s reconnect grace must win over the age TTL, or the
    # reaper deletes the channel/history before EventSource's ~3s reconnect.
    assert channel.reaper_should_collect(time.time()) is False

    replacement = channel.subscribe(maxsize=64, after_event_id=cursor)
    try:
        event, payload = replacement.get_nowait()
        assert event == "bg_task_complete"
        assert payload["event_id"] == "aged-gap-event"
    finally:
        channel.unsubscribe(replacement)


def test_replay_precedes_live_turn_recovery_that_suspends_session_stream(monkeypatch):
    """Replay the completion toast before server_turn_started hands off the UI."""
    _short_lease(monkeypatch)
    sid = "s-order"
    channel = background_process.SessionChannel(sid)
    baseline = channel.subscribe(maxsize=64)
    cursor = baseline._session_channel_initial_event_id
    channel.unsubscribe(baseline)
    channel.emit(
        "bg_task_complete",
        {"event_id": "gap-completion", "session_id": sid},
    )
    with background_process.SESSION_CHANNELS_LOCK:
        background_process.SESSION_CHANNELS[sid] = channel

    monkeypatch.setattr(
        background_process,
        "active_stream_id_for_session",
        lambda _sid: "recovered-run",
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=True: SimpleNamespace(pending_started_at=1.0),
    )

    handler = _FakeHandler()
    handler.headers["Last-Event-ID"] = cursor
    _run_bounded(
        lambda: routes._handle_session_sse_stream(
            handler,
            urlparse(f"http://example.test/api/session/stream?session_id={sid}"),
        )
    )

    body = bytes(handler.wfile.body)
    assert body.index(b"event: bg_task_complete") < body.index(
        b"event: server_turn_started"
    )
    with background_process.SESSION_CHANNELS_LOCK:
        background_process.SESSION_CHANNELS.pop(sid, None)


def test_only_preloaded_replay_prefix_runs_before_live_turn_recovery(monkeypatch):
    _short_lease(monkeypatch)
    sid = "s-prefix"
    q: queue.Queue = queue.Queue()
    q.put_nowait(
        ("bg_task_complete", {"event_id": "replay-event", "session_id": sid})
    )
    q.put_nowait(
        ("bg_task_complete", {"event_id": "live-event", "session_id": sid})
    )
    q._session_channel_replay_count = 1

    class Channel:
        def unsubscribe(self, _q):
            pass

        def note_subscriber_write_ok(self, _q):
            pass  # writer-liveness mark (#7302)

    monkeypatch.setattr(
        background_process,
        "subscribe_to_session_channel",
        lambda _sid, maxsize=64, after_event_id=None: (Channel(), q),
    )
    monkeypatch.setattr(
        background_process,
        "active_stream_id_for_session",
        lambda _sid: "recovered-run",
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=True: SimpleNamespace(pending_started_at=1.0),
    )

    handler = _FakeHandler()
    _run_bounded(
        lambda: routes._handle_session_sse_stream(
            handler,
            urlparse(f"http://example.test/api/session/stream?session_id={sid}"),
        )
    )

    body = bytes(handler.wfile.body)
    assert body.index(b"replay-event") < body.index(b"event: server_turn_started")
    assert body.index(b"event: server_turn_started") < body.index(b"live-event")


def test_session_list_stream_lease_unsubscribes_proxy_acked_client(monkeypatch):
    _short_lease(monkeypatch)
    q: queue.Queue = queue.Queue()
    unsubscribed: list[queue.Queue] = []
    monkeypatch.setattr(routes, "subscribe_session_events", lambda: q)
    monkeypatch.setattr(routes, "unsubscribe_session_events", unsubscribed.append)
    handler = _FakeHandler()

    _run_bounded(lambda: routes._handle_session_events_stream(handler))

    assert unsubscribed == [q]
    assert handler.close_connection is True
    assert ("Connection", "close") not in handler.sent_headers
    assert b"keepalive" in handler.wfile.body


def test_session_list_stream_lease_expires_under_continuous_events(monkeypatch):
    """An active producer must not postpone the connection-age bound."""
    _short_lease(monkeypatch)

    class BusyQueue:
        def get(self, timeout=None):
            return {"type": "sessions_changed", "reason": "busy"}

    q = BusyQueue()
    unsubscribed = []
    monkeypatch.setattr(routes, "subscribe_session_events", lambda: q)
    monkeypatch.setattr(routes, "unsubscribe_session_events", unsubscribed.append)
    handler = _FakeHandler()

    _run_bounded(lambda: routes._handle_session_events_stream(handler))

    assert unsubscribed == [q]
    assert handler.close_connection is True
    assert b"sessions_changed" in handler.wfile.body


def test_gateway_stream_lease_unsubscribes_proxy_acked_client(monkeypatch):
    _short_lease(monkeypatch)
    q: queue.Queue = queue.Queue()

    class Watcher:
        def __init__(self) -> None:
            self.unsubscribed: list[queue.Queue] = []

        def is_alive(self) -> bool:
            return True

        def subscribe(self) -> queue.Queue:
            return q

        def unsubscribe(self, subscriber: queue.Queue) -> None:
            self.unsubscribed.append(subscriber)

    watcher = Watcher()
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": True})
    monkeypatch.setattr(gateway_watcher, "get_watcher", lambda: watcher)
    monkeypatch.setattr(models, "get_cli_sessions", lambda: [])
    handler = _FakeHandler()

    _run_bounded(
        lambda: routes._handle_gateway_sse_stream(
            handler,
            urlparse("http://example.test/api/sessions/gateway/stream"),
        )
    )

    assert watcher.unsubscribed == [q]
    assert handler.close_connection is True
    assert ("Connection", "close") not in handler.sent_headers
    assert b"sessions_changed" in handler.wfile.body
    assert b"keepalive" in handler.wfile.body


def test_fresh_subscription_churn_cannot_evict_a_valid_subscribers_gap_event():
    """>32 fresh subscriptions must not consume another tab's replay capacity.

    Regression for the gate-reproduced SILENT defect: synthetic subscribe
    cursors used to be appended into the same bounded ``_history`` deque that
    holds replayable events, so one tab's fresh-connection churn evicted
    another tab's still-replayable completion. The fix keeps ``_history`` for
    genuine events only and encodes the subscribe watermark in the cursor.
    """
    channel = background_process.SessionChannel("s-churn")

    # Tab A connects fresh, records its cursor, then drops (EOF/reconnect gap).
    tab_a = channel.subscribe(maxsize=64)
    cursor_a = tab_a._session_channel_initial_event_id
    channel.unsubscribe(tab_a)

    # A completion is emitted during Tab A's reconnect gap.
    channel.emit(
        "bg_task_complete",
        {"event_id": "gap-completion", "session_id": "s-churn"},
    )

    # Far more than the 32-slot replay limit of *fresh* subscriptions churn
    # through the channel while Tab A is away. Under the old code each of these
    # appended a synthetic marker and evicted the gap completion.
    churn = background_process._SESSION_CHANNEL_REPLAY_LIMIT * 3
    for _ in range(churn):
        other = channel.subscribe(maxsize=64)
        channel.unsubscribe(other)

    # The single genuine event is still the only thing in replay history and
    # still replayable — the churn consumed zero capacity.
    with channel._lock:
        assert len(channel._history) == 1
        assert channel._history[0][2] == "gap-completion"

    # Tab A reconnects with its original cursor and STILL receives its
    # reconnect-gap completion.
    replacement = channel.subscribe(maxsize=64, after_event_id=cursor_a)
    try:
        event, payload = replacement.get_nowait()
        assert event == "bg_task_complete"
        assert payload["event_id"] == "gap-completion"
        assert replacement.empty()
    finally:
        channel.unsubscribe(replacement)


def test_synthetic_cursor_fails_closed_when_watermarked_event_was_evicted():
    """A watermark older than retained history must NOT replay a wrong prefix.

    When genuine events overflow the bounded deque past a subscriber's
    watermark, the oldest replayable event is gone. Rather than replay a
    partial/incorrect suffix (or crash), the reconnect must fail closed:
    fresh marker, replay nothing, no stale/incorrect data.
    """
    channel = background_process.SessionChannel("s-evict")

    tab = channel.subscribe(maxsize=64)
    cursor = tab._session_channel_initial_event_id
    channel.unsubscribe(tab)

    # Overflow the replay history with genuine events well past the watermark.
    overflow = background_process._SESSION_CHANNEL_REPLAY_LIMIT + 5
    for i in range(overflow):
        channel.emit(
            "bg_task_complete",
            {"event_id": f"evt-{i}", "session_id": "s-evict"},
        )

    replacement = channel.subscribe(maxsize=64, after_event_id=cursor)
    try:
        # Fail closed: no replay (the watermarked position was evicted), and a
        # brand-new synthetic cursor is issued rather than a stale continuation.
        assert replacement.empty()
        assert replacement._session_channel_replay_count == 0
        assert replacement._session_channel_initial_event_id.startswith(
            "session-channel:"
        )
        assert replacement._session_channel_initial_event_id != cursor
    finally:
        channel.unsubscribe(replacement)


def test_synthetic_cursor_replays_only_gap_events_after_its_watermark():
    """A synthetic reconnect replays events emitted AFTER connect, not before."""
    channel = background_process.SessionChannel("s-watermark")

    # An event exists before Tab A ever connects — Tab A must NOT receive it.
    channel.emit(
        "bg_task_complete",
        {"event_id": "pre-connect", "session_id": "s-watermark"},
    )

    tab = channel.subscribe(maxsize=64)
    cursor = tab._session_channel_initial_event_id
    channel.unsubscribe(tab)

    # Two events land during the reconnect gap — both must replay, in order.
    channel.emit(
        "bg_task_complete",
        {"event_id": "gap-1", "session_id": "s-watermark"},
    )
    channel.emit(
        "bg_task_complete",
        {"event_id": "gap-2", "session_id": "s-watermark"},
    )

    replacement = channel.subscribe(maxsize=64, after_event_id=cursor)
    try:
        first = replacement.get_nowait()
        second = replacement.get_nowait()
        assert first[1]["event_id"] == "gap-1"
        assert second[1]["event_id"] == "gap-2"
        assert replacement.empty()  # pre-connect event was never replayed
    finally:
        channel.unsubscribe(replacement)


def test_synthetic_reconnect_with_queued_replay_keeps_incoming_cursor():
    """Drop between the initial frame and replay delivery must not lose events.

    A synthetic reconnect that has queued replay delivers those events AFTER its
    `initial` frame. If it advertised the post-replay watermark and the client
    dropped between the initial frame and the replay loop, the next reconnect
    would resume past the undelivered events and lose them. The initial frame
    must therefore keep advertising the client's INCOMING cursor whenever replay
    is queued, so a drop-before-replay still re-replays.
    """
    channel = background_process.SessionChannel("s-preserve")

    tab = channel.subscribe(maxsize=64)
    cursor = tab._session_channel_initial_event_id
    channel.unsubscribe(tab)

    channel.emit(
        "bg_task_complete",
        {"event_id": "queued-gap", "session_id": "s-preserve"},
    )

    # Reconnect: replay is queued, so the advertised cursor must equal the
    # incoming cursor (not an advanced watermark).
    reconnect = channel.subscribe(maxsize=64, after_event_id=cursor)
    assert reconnect._session_channel_replay_count == 1
    assert reconnect._session_channel_initial_event_id == cursor
    # Simulate a drop AFTER the initial frame but BEFORE the replay loop ran:
    # the client's Last-Event-ID is still the advertised (== incoming) cursor.
    channel.unsubscribe(reconnect)

    recovery = channel.subscribe(
        maxsize=64,
        after_event_id=reconnect._session_channel_initial_event_id,
    )
    try:
        event, payload = recovery.get_nowait()
        assert event == "bg_task_complete"
        assert payload["event_id"] == "queued-gap"  # NOT lost across the drop
    finally:
        channel.unsubscribe(recovery)


def test_emit_serializes_history_order_with_subscriber_delivery():
    """Concurrent emits must record and deliver in the SAME order.

    ``emit`` appends to ``_history`` and enqueues to live subscribers under one
    lock. If delivery happened outside the lock, concurrent emits could record
    ``A,B`` in history but deliver ``B,A``; a drop after B would resume real-ID
    replay past B and permanently lose A. This drives many concurrent emits and
    asserts every subscriber's received order matches history order exactly.
    """
    import threading

    channel = background_process.SessionChannel("s-order-race")
    sub = channel.subscribe(maxsize=10000)

    n = 200
    barrier = threading.Barrier(n)

    def do_emit(i):
        barrier.wait()
        channel.emit("e", {"event_id": f"evt-{i}", "session_id": "s-order-race"})

    threads = [threading.Thread(target=do_emit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # History order (seq-ordered) is the authoritative order.
    with channel._lock:
        history_order = [item[2] for item in channel._history if item[0]]

    delivered_order = []
    while True:
        try:
            _event, data = sub.get_nowait()
        except queue.Empty:
            break
        delivered_order.append(data["event_id"])
    channel.unsubscribe(sub)

    # Every delivered event that is still in the (bounded) history must appear in
    # the exact same relative order it was recorded — never transposed.
    retained = set(history_order)
    delivered_retained = [e for e in delivered_order if e in retained]
    # history is capped; compare the tail of delivered against history order
    assert delivered_retained[-len(history_order):] == history_order


def _sse_id_lines(body: bytes) -> list[str]:
    return [
        line.split(b": ", 1)[1].decode("utf-8")
        for line in bytes(body).splitlines()
        if line.startswith(b"id: ")
    ]


def test_dual_name_completion_alias_does_not_hide_later_completion_on_reconnect(monkeypatch):
    """A, B, B-alias, A-alias: a tab that saw A must still get B on reconnect.

    ``_emit_bg_task_complete_events_now`` emits every completion twice with the
    SAME payload ``event_id`` (canonical ``bg_task_complete`` plus the legacy
    ``process_complete`` alias), and the coalescer can interleave two
    completions as ``A, B, B-alias, A-alias``. If the SSE ``id:`` line is the
    payload ``event_id``, the browser's ``Last-Event-ID: A`` is ambiguous: it
    also names ``A-alias`` (the newest retained frame), so matching it replays
    nothing and B is lost in exactly the lease-reconnect gap this PR bridges.
    The wire ``id:`` must therefore be a per-frame replay cursor, derived here
    the way native EventSource derives it: the last ``id:`` line received.
    """
    monkeypatch.setattr(routes, "_SSE_SUBSCRIBER_LEASE_SECONDS", 0.25, raising=False)
    monkeypatch.setattr(routes, "_SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda handler: None)
    sid = "s-alias"
    channel = background_process.SessionChannel(sid)
    with background_process.SESSION_CHANNELS_LOCK:
        background_process.SESSION_CHANNELS[sid] = channel
    monkeypatch.setattr(background_process, "active_stream_id_for_session", lambda _sid: None)
    monkeypatch.setattr(background_process, "persisted_message_count_for_session", lambda _sid: None)

    try:
        first = _FakeHandler()
        first_done = threading.Event()

        def first_worker() -> None:
            try:
                routes._handle_session_sse_stream(
                    first,
                    urlparse(f"http://example.test/api/session/stream?session_id={sid}"),
                )
            finally:
                first_done.set()

        threading.Thread(target=first_worker, daemon=True).start()
        assert first.wfile.first_write.wait(0.5), "no initial frame"
        # Completion A reaches the live generation before its lease expires.
        assert channel.emit(
            "bg_task_complete", {"event_id": "A", "session_id": sid}
        ) == 1
        assert first_done.wait(1.0), "generation one outlived its lease"
        assert b'"event_id": "A"' in first.wfile.body
        # Native EventSource remembers the LAST ``id:`` it received.
        browser_last_event_id = _sse_id_lines(first.wfile.body)[-1]

        # Reconnect gap: B lands, then the coalescer flushes B's alias and A's
        # alias (same payload event_id as their originals).
        assert channel.emit("bg_task_complete", {"event_id": "B", "session_id": sid}) == 0
        assert channel.emit("process_complete", {"event_id": "B", "session_id": sid}) == 0
        assert channel.emit("process_complete", {"event_id": "A", "session_id": sid}) == 0

        second = _FakeHandler()
        second.headers["Last-Event-ID"] = browser_last_event_id
        _run_bounded(
            lambda: routes._handle_session_sse_stream(
                second,
                urlparse(f"http://example.test/api/session/stream?session_id={sid}"),
            )
        )

        body = bytes(second.wfile.body)
        assert b'"event_id": "B"' in body, body  # B must NOT be lost
        assert b"event: bg_task_complete\n" in body
        # Replay keeps history order: B's canonical frame precedes A's alias.
        assert body.index(b'"event_id": "B"') < body.index(b'"event_id": "A"')
    finally:
        with background_process.SESSION_CHANNELS_LOCK:
            background_process.SESSION_CHANNELS.pop(sid, None)


def test_channel_frames_carry_per_frame_replay_cursor():
    """Every retained frame owns an ordered wire cursor distinct from event_id.

    Two frames may legitimately share a payload ``event_id`` (the dual-name
    completion emit). Each delivered frame must therefore expose its own
    monotonic replay cursor, in the same ``session-channel:…@<seq>`` grammar
    as the synthetic subscribe cursor, so a reconnect with that cursor replays
    exactly the frames recorded after it. The payload stays untouched so the
    browser's ``(session_id, event_id)`` dedupe keeps working.
    """
    sid = "s-frame-cursor"
    channel = background_process.SessionChannel(sid)
    sub = channel.subscribe(maxsize=64)
    channel.emit("bg_task_complete", {"event_id": "A", "session_id": sid})
    frame_a = sub.get_nowait()
    channel.unsubscribe(sub)

    # Still a plain (event, data) pair for every existing consumer…
    assert frame_a == ("bg_task_complete", {"event_id": "A", "session_id": sid})
    # …that also knows its own wire cursor.
    cursor_a = frame_a.cursor
    assert cursor_a != "A"
    assert background_process._parse_channel_cursor_watermark(cursor_a) == 1

    channel.emit("bg_task_complete", {"event_id": "B", "session_id": sid})
    channel.emit("process_complete", {"event_id": "B", "session_id": sid})
    channel.emit("process_complete", {"event_id": "A", "session_id": sid})

    reconnect = channel.subscribe(maxsize=64, after_event_id=cursor_a)
    try:
        assert reconnect._session_channel_replay_count == 3
        replayed = [reconnect.get_nowait() for _ in range(3)]
        assert [(e, d["event_id"]) for e, d in replayed] == [
            ("bg_task_complete", "B"),
            ("process_complete", "B"),
            ("process_complete", "A"),
        ]
        seqs = [
            background_process._parse_channel_cursor_watermark(f.cursor)
            for f in replayed
        ]
        assert seqs == [2, 3, 4]
        assert reconnect.empty()
        # Resuming from the LAST replayed frame's cursor has nothing left.
        channel.unsubscribe(reconnect)
        resumed = channel.subscribe(maxsize=64, after_event_id=replayed[-1].cursor)
        assert resumed.empty()
        assert resumed._session_channel_replay_count == 0
        channel.unsubscribe(resumed)
    except BaseException:
        channel.unsubscribe(reconnect)
        raise


def test_closed_channel_late_subscriber_gets_close_sentinel_not_replay():
    """#7302 x #7105: a late subscriber on a closed channel gets the close
    sentinel, never a replay prefix that could crowd the sentinel out of its
    queue and leave the handler hanging on keepalives."""
    sid = "s-closed-late"
    channel = background_process.SessionChannel(sid)
    baseline = channel.subscribe(maxsize=64)
    cursor = baseline._session_channel_initial_event_id
    channel.unsubscribe(baseline)
    channel.emit("bg_task_complete", {"event_id": "gap-a", "session_id": sid})
    channel.emit("bg_task_complete", {"event_id": "gap-b", "session_id": sid})
    channel.close("test")

    # The queue is sized to the pending replay exactly: if a closed channel
    # replayed, the two frames would fill it and the sentinel put would fail.
    late = channel.subscribe(maxsize=2, after_event_id=cursor)
    try:
        assert late._session_channel_replay_count == 0
        assert late.get_nowait() is None
        assert late.empty()
    finally:
        channel.unsubscribe(late)


def test_close_sentinel_inside_replay_prefix_ends_the_response(monkeypatch):
    """A close that lands inside the replay prefix (``close()`` evicts one
    queued frame to fit its sentinel) must end the HTTP response exactly like
    the live loop's end-of-stream branch (#7302), and a replayed frame's
    completed write still counts as writer-side liveness."""
    _short_lease(monkeypatch)
    # Long lease: only the sentinel may end this generation.
    monkeypatch.setattr(routes, "_SSE_SUBSCRIBER_LEASE_SECONDS", 30.0, raising=False)
    sid = "s-replay-close"
    q: queue.Queue = queue.Queue()
    q.put_nowait(
        background_process._SessionChannelFrame(
            "bg_task_complete",
            {"event_id": "replayed", "session_id": sid},
            "session-channel:tok@1",
        )
    )
    q.put_nowait(None)
    q._session_channel_replay_count = 2

    class Channel:
        def __init__(self) -> None:
            self.unsubscribed: list[queue.Queue] = []
            self.writes_ok = 0

        def unsubscribe(self, sub: queue.Queue) -> None:
            self.unsubscribed.append(sub)

        def note_subscriber_write_ok(self, _q: queue.Queue) -> None:
            self.writes_ok += 1

    channel = Channel()
    monkeypatch.setattr(
        background_process,
        "subscribe_to_session_channel",
        lambda _sid, maxsize=64, after_event_id=None: (channel, q),
    )
    monkeypatch.setattr(background_process, "active_stream_id_for_session", lambda _sid: None)
    monkeypatch.setattr(background_process, "persisted_message_count_for_session", lambda _sid: None)
    monkeypatch.setattr(background_process, "should_emit_session_updated", lambda known, current: False)

    handler = _FakeHandler()
    _run_bounded(
        lambda: routes._handle_session_sse_stream(
            handler,
            urlparse(f"http://example.test/api/session/stream?session_id={sid}"),
        )
    )

    assert handler.close_connection is True
    assert ("Connection", "close") not in handler.sent_headers
    assert channel.unsubscribed == [q]
    assert b"id: session-channel:tok@1" in bytes(handler.wfile.body)
    assert channel.writes_ok == 1
