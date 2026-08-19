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

    assert second.wfile.body.count(b"id: evt-2") == 1
    assert second.wfile.body.count(b'"event_id": "evt-2"') == 1
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
        assert fresh._session_channel_initial_event_id != unknown._session_channel_initial_event_id
    finally:
        channel.unsubscribe(fresh)
        channel.unsubscribe(unknown)


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
