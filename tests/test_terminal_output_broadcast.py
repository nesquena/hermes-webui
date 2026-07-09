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

    assert any(e == "terminal_closed" for _seq, e, _p in _drain(a))
    assert any(e == "terminal_closed" for _seq, e, _p in _drain(b)), (
        "second tab never received terminal_closed"
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
    reconnect = term.subscribe(after_seq=cursor)

    assert [payload["text"] for _seq, _event, payload in _drain(reconnect)] == ["B"]


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

    # Overflow the slow subscriber's queue (maxsize 2000) while draining fast.
    total = 2000 + 50
    for i in range(total):
        term.put_output("output", {"text": f"c{i}"})
        # Keep 'fast' drained so it never overflows.
        try:
            fast.get_nowait()
        except queue.Empty:
            pass

    # Slow subscriber is capped and kept the newest chunks (drop-oldest).
    slow_items = _drain(slow)
    assert len(slow_items) <= 2000
    assert slow_items[-1][2]["text"] == f"c{total - 1}", "slow queue didn't keep newest"


def test_unsubscribe_unknown_queue_is_safe():
    term = _make_term()
    stray: queue.Queue = queue.Queue()
    term.unsubscribe(stray)  # must not raise
