"""Regression tests — terminal output is broadcast to every attached viewer.

Previously `TerminalSession.output` was a single `queue.Queue` read
destructively by the SSE handler. Two tabs/windows viewing the SAME session
each opened their own EventSource -> two handlers competed on that one queue, so
every PTY chunk went to exactly one of them: each tab saw a disjoint half of the
stream and only one saw `terminal_closed`.

Output now fans out: each consumer `subscribe()`s its own queue (seeded with a
bounded backlog so a late/first attach still catches up), and `put_output`
broadcasts to all of them. These pin that two subscribers each receive the full
stream, that a late subscriber replays the backlog, and that a slow subscriber
can't starve another.
"""
import io
import os
import queue
import threading
from types import SimpleNamespace

import pytest

if os.name != "posix":
    pytest.skip("terminal tests require POSIX terminal support", allow_module_level=True)

import api.terminal as terminal
from api import routes


def _make_term(sid="bcast"):
    class _Proc:
        pid = 4242

        def poll(self):
            return None

    return terminal.TerminalSession(
        session_id=sid, workspace="/tmp", proc=_Proc(), master_fd=-1
    )


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_two_subscribers_each_get_the_full_stream():
    term = _make_term()
    a = term.subscribe()
    b = term.subscribe()

    for i in range(5):
        term.put_output("output", {"text": f"chunk-{i}"})

    got_a = [p["text"] for _seq, _e, p in _drain(a)]
    got_b = [p["text"] for _seq, _e, p in _drain(b)]
    expected = [f"chunk-{i}" for i in range(5)]
    assert got_a == expected, "subscriber A missed chunks (stream was split)"
    assert got_b == expected, "subscriber B missed chunks (stream was split)"


def test_terminal_closed_reaches_all_subscribers():
    term = _make_term()
    a = term.subscribe()
    b = term.subscribe()

    term.put_output("terminal_closed", {"exit_code": 0})

    assert [e for _seq, e, _p in _drain(a)] == ["terminal_closed"]
    assert [e for _seq, e, _p in _drain(b)] == ["terminal_closed"], (
        "second tab did not receive terminal_closed exactly once"
    )


def test_late_subscriber_replays_backlog():
    term = _make_term()
    # Output produced before any viewer attaches (e.g. the initial shell prompt).
    for i in range(3):
        term.put_output("output", {"text": f"pre-{i}"})

    late = term.subscribe()
    term.put_output("output", {"text": "live"})

    got = [p["text"] for _seq, _e, p in _drain(late)]
    assert got == ["pre-0", "pre-1", "pre-2", "live"], (
        "late subscriber did not replay the backlog then receive live output"
    )


def test_reconnecting_subscriber_replays_only_events_after_cursor():
    term = _make_term()
    first = term.subscribe()
    term.put_output("output", {"text": "A"})
    first_items = _drain(first)
    assert [payload["text"] for _seq, _event, payload in first_items] == ["A"]
    cursor = first_items[-1][0]
    term.unsubscribe(first)

    term.put_output("output", {"text": "B"})
    term.put_output("output", {"text": "C"})
    reconnect = term.subscribe(after_seq=cursor)

    replayed = _drain(reconnect)
    assert [seq for seq, _event, _payload in replayed] == [cursor + 1, cursor + 2]
    assert [payload["text"] for _seq, _event, payload in replayed] == ["B", "C"]

    new_viewer = term.subscribe()
    assert [payload["text"] for _seq, _event, payload in _drain(new_viewer)] == [
        "A",
        "B",
        "C",
    ]


def test_sse_reconnect_honors_last_event_id_and_emits_ids(monkeypatch):
    term = _make_term("sse-cursor")
    term.put_output("output", {"text": "A"})
    term.put_output("output", {"text": "B"})
    term.put_output("terminal_closed", {"exit_code": 0})

    class _Handler:
        headers = {"Last-Event-ID": "1"}

        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    monkeypatch.setitem(terminal._TERMINALS, term.session_id, term)
    handler = _Handler()

    routes._handle_terminal_output(
        handler,
        SimpleNamespace(query=f"session_id={term.session_id}"),
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert handler.status == 200
    assert '"text": "A"' not in body
    assert '"text": "B"' in body
    assert "id: 2\n" in body
    assert "id: 3\n" in body
    assert term._subscribers == []


def test_sse_heartbeat_is_a_valid_comment_and_cleans_up(monkeypatch):
    term = _make_term("sse-heartbeat")
    term.closed.set()

    class _Handler:
        headers = {}

        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)
    monkeypatch.setattr(routes, "_SSE_HEARTBEAT_INTERVAL_SECONDS", 0)
    monkeypatch.setitem(terminal._TERMINALS, term.session_id, term)
    handler = _Handler()

    routes._handle_terminal_output(
        handler,
        SimpleNamespace(query=f"session_id={term.session_id}"),
    )

    body = handler.wfile.getvalue().decode("utf-8")
    assert handler.status == 200
    assert body.startswith(": terminal heartbeat\n\n")
    assert "event: terminal_closed\n" in body
    assert "id: None" not in body
    assert term._subscribers == []


def test_unsubscribe_stops_delivery_and_shrinks_list():
    term = _make_term()
    a = term.subscribe()
    b = term.subscribe()
    assert len(term._subscribers) == 2

    term.unsubscribe(a)
    assert len(term._subscribers) == 1

    term.put_output("output", {"text": "after-unsub"})
    assert _drain(a) == []  # a got nothing after unsubscribing
    assert [p["text"] for _seq, _e, p in _drain(b)] == ["after-unsub"]


def test_slow_subscriber_drops_oldest_without_starving_others():
    term = _make_term()
    slow = term.subscribe()
    fast = term.subscribe()

    # Overflow the slow subscriber's queue while draining fast.
    total = terminal._OUTPUT_BUFFER_MAXLEN + 50
    fast_items = []
    for i in range(total):
        term.put_output("output", {"text": f"c{i}"})
        # Keep 'fast' drained so it never overflows.
        fast_items.append(fast.get_nowait())

    # Slow subscriber is capped and kept the newest chunks (drop-oldest).
    slow_items = _drain(slow)
    assert len(slow_items) == terminal._OUTPUT_BUFFER_MAXLEN
    assert slow_items[-1][2]["text"] == f"c{total - 1}", "slow queue didn't keep newest"
    assert [seq for seq, _event, _payload in fast_items] == list(range(1, total + 1))
    assert [payload["text"] for _seq, _event, payload in fast_items] == [
        f"c{i}" for i in range(total)
    ]


def test_unsubscribe_unknown_queue_is_safe():
    term = _make_term()
    known = term.subscribe()
    term.unsubscribe(known)
    term.unsubscribe(known)  # double-unsubscribe must be idempotent
    stray: queue.Queue = queue.Queue()
    term.unsubscribe(stray)  # must not raise
    assert term._subscribers == []


def test_concurrent_producers_keep_monotonic_sequences_through_backlog_rollover():
    term = _make_term()
    live = term.subscribe()
    producer_count = 4
    events_per_producer = 600
    barrier = threading.Barrier(producer_count)

    def produce(producer_id):
        barrier.wait(timeout=1.0)
        for index in range(events_per_producer):
            term.put_output(
                "output",
                {"text": f"producer-{producer_id}-{index}"},
            )

    threads = [
        threading.Thread(target=produce, args=(producer_id,))
        for producer_id in range(producer_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.0)
        assert not thread.is_alive()

    total = producer_count * events_per_producer
    backlog = list(term._backlog)
    assert len(backlog) == terminal._OUTPUT_BUFFER_MAXLEN
    assert [seq for seq, _event, _payload in backlog] == list(
        range(total - terminal._OUTPUT_BUFFER_MAXLEN + 1, total + 1)
    )
    assert [seq for seq, _event, _payload in _drain(live)] == list(
        range(total - terminal._OUTPUT_BUFFER_MAXLEN + 1, total + 1)
    )
    assert term._next_output_seq == total + 1

    cursor = backlog[0][0]
    replay = _drain(term.subscribe(after_seq=cursor))
    assert [seq for seq, _event, _payload in replay] == list(
        range(cursor + 1, total + 1)
    )


def test_concurrent_producers_keep_live_subscriber_delivery_order():
    term = _make_term()
    first_delivery_started = threading.Event()
    second_delivery_completed = threading.Event()
    release_first_delivery = threading.Event()

    class _PausingQueue(queue.Queue):
        def put_nowait(self, item):
            if item[0] == 1:
                first_delivery_started.set()
                assert release_first_delivery.wait(timeout=1.0)
            result = super().put_nowait(item)
            if item[0] == 2:
                second_delivery_completed.set()
            return result

    subscriber = _PausingQueue(maxsize=terminal._OUTPUT_BUFFER_MAXLEN)
    with term._sub_lock:
        term._subscribers.append(subscriber)

    first = threading.Thread(
        target=term.put_output,
        args=("output", {"text": "first"}),
    )
    second = threading.Thread(
        target=term.put_output,
        args=("output", {"text": "second"}),
    )
    first.start()
    assert first_delivery_started.wait(timeout=1.0)
    second.start()
    second_delivered_before_release = second_delivery_completed.wait(timeout=0.1)
    release_first_delivery.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not second_delivered_before_release
    assert [seq for seq, _event, _payload in _drain(subscriber)] == [1, 2]


def test_unsubscribe_waits_for_inflight_delivery_then_stops_future_output():
    term = _make_term()
    delivery_started = threading.Event()
    release_delivery = threading.Event()
    unsubscribe_started = threading.Event()
    unsubscribe_completed = threading.Event()

    class _PausingQueue(queue.Queue):
        def put_nowait(self, item):
            if item[0] == 1:
                delivery_started.set()
                assert release_delivery.wait(timeout=1.0)
            return super().put_nowait(item)

    subscriber = _PausingQueue(maxsize=terminal._OUTPUT_BUFFER_MAXLEN)
    with term._sub_lock:
        term._subscribers.append(subscriber)

    publisher = threading.Thread(
        target=term.put_output,
        args=("output", {"text": "in-flight"}),
    )

    def unsubscribe():
        unsubscribe_started.set()
        term.unsubscribe(subscriber)
        unsubscribe_completed.set()

    remover = threading.Thread(target=unsubscribe)
    publisher.start()
    assert delivery_started.wait(timeout=1.0)
    remover.start()
    assert unsubscribe_started.wait(timeout=1.0)
    assert not unsubscribe_completed.wait(timeout=0.1)

    release_delivery.set()
    publisher.join(timeout=1.0)
    remover.join(timeout=1.0)

    assert not publisher.is_alive()
    assert not remover.is_alive()
    assert unsubscribe_completed.is_set()
    assert term._subscribers == []

    term.put_output("output", {"text": "after-unsubscribe"})
    assert [
        (seq, payload["text"])
        for seq, _event, payload in _drain(subscriber)
    ] == [(1, "in-flight")]
