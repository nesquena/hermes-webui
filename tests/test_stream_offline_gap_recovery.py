"""
Regression tests for #4633/#5788 — the SSE handler must ENFORCE run-journal
coverage before draining a truncated offline tail.

``StreamChannel.subscribe_with_snapshot()`` reports ``offline_dropped_events``
when the capped offline buffer evicted frames during a disconnect gap. The
retained tail is only safe to drain when the run journal provably backfills
everything from the client's replay cursor through the snapshot cutoff —
otherwise the browser would render later frames plus a normal ``stream_end``
with a silent transcript hole in the middle (the journal being missing,
incomplete, or unreadable leaves the evicted frames unrecoverable).

Enforcement contract pinned here:

* dropped > 0 and the journal does NOT cover the gap → emit the established
  ``apperror`` + ``recovery_control: true`` event (same client contract as
  ``run_journal.stale_interrupted_event`` — the tab restores the transcript
  from persisted state) and do NOT stream the tail.
* dropped > 0 and the journal DOES cover the gap → replay + tail as before.
* a client cursor already at/past the snapshot cutoff has no gap to cover —
  no recovery even when the journal is missing.
* the retained tail itself covers [first buffered frame → cutoff]: the journal
  only has to bridge (cursor → first buffered frame), a cursor already inside
  the retained range needs no journal at all, and the replay/dedup cutoff stops
  at the bridge so queued frames the journal never emitted are not skipped.
"""
import io
import json
import queue
from urllib.parse import urlparse

import api.routes as routes


class _Handler:
    def __init__(self):
        self.wfile = io.BytesIO()

    def send_response(self, _code):
        pass

    def send_header(self, _name, _value):
        pass

    def end_headers(self):
        pass


class _FakeStream:
    def __init__(self, snapshot, items):
        self.q = queue.Queue()
        for item in items:
            self.q.put_nowait(item)
        self._snapshot = dict(snapshot)
        self.unsubscribed = False

    def subscribe_with_snapshot(self):
        return self.q, dict(self._snapshot)

    def unsubscribe(self, q):
        self.unsubscribed = q is self.q


def _run_handler(monkeypatch, stream, query):
    handler = _Handler()
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_1"] = stream
    try:
        routes._handle_sse_stream(handler, urlparse(f"/api/chat/stream?{query}"))
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)
    return handler.wfile.getvalue().decode("utf-8")


def _sse_payloads(body, event):
    """Extract parsed data payloads for all SSE frames of the given event."""
    payloads = []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line == f"event: {event}" and i + 1 < len(lines):
            data = lines[i + 1]
            assert data.startswith("data: ")
            payloads.append(json.loads(data[len("data: "):]))
    return payloads


def _journal_events(lo, hi, run_id="run_1"):
    return [
        {
            "event": "token",
            "payload": {"text": f"journal-{seq}"},
            "event_id": f"{run_id}:{seq}",
            "seq": seq,
        }
        for seq in range(lo, hi + 1)
    ]


def test_dropped_frames_without_journal_emit_recovery_not_tail(monkeypatch):
    """Missing journal + evicted frames → recovery_control apperror, no tail."""
    stream = _FakeStream(
        {
            "last_event_id": "run_1:9000",
            "offline_buffered_events": 2,
            "offline_dropped_events": 25,
            "offline_first_event_id": "run_1:8999",
        },
        [
            ("token", {"text": "tail-frame"}, "run_1:8999"),
            ("stream_end", {}, "run_1:9000"),
        ],
    )
    monkeypatch.setattr(routes, "find_run_summary", lambda _sid: None)
    monkeypatch.setattr(routes, "stream_owner_session_id", lambda _sid: "session_1")

    body = _run_handler(monkeypatch, stream, "stream_id=run_1&replay=1&after_seq=100")

    payloads = _sse_payloads(body, "apperror")
    assert len(payloads) == 1
    assert payloads[0]["recovery_control"] is True
    assert payloads[0]["type"] == "interrupted"
    assert payloads[0]["session_id"] == "session_1"
    assert payloads[0]["stream_id"] == "run_1"
    assert payloads[0]["offline_dropped_events"] == 25
    # The truncated tail must NOT be drained: no silent hole + stream_end.
    assert "tail-frame" not in body
    assert "event: stream_end" not in body
    # Every exit path returns the queue to the channel.
    assert stream.unsubscribed is True


def test_dropped_frames_with_full_journal_coverage_stream_normally(monkeypatch):
    """Journal covers (cursor → cutoff] → replay + tail drain, no recovery."""
    stream = _FakeStream(
        {
            "last_event_id": "run_1:105",
            "offline_buffered_events": 2,
            "offline_dropped_events": 25,
            "offline_first_event_id": "run_1:106",
        },
        [
            ("token", {"text": "tail-frame"}, "run_1:106"),
            ("stream_end", {}, "run_1:107"),
        ],
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {"session_id": "session_1", "run_id": sid, "terminal": False},
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, run_id, after_seq=None, max_seq=None: {
            "events": _journal_events(101, 105, run_id=run_id)
        },
    )

    body = _run_handler(monkeypatch, stream, "stream_id=run_1&replay=1&after_seq=100")

    assert _sse_payloads(body, "apperror") == []
    assert "journal-101" in body and "journal-105" in body
    assert "tail-frame" in body
    assert "event: stream_end" in body
    assert stream.unsubscribed is True


def test_incomplete_journal_coverage_emits_recovery(monkeypatch):
    """A journal that stops short of the snapshot cutoff is NOT coverage."""
    stream = _FakeStream(
        {
            "last_event_id": "run_1:105",
            "offline_buffered_events": 2,
            "offline_dropped_events": 25,
            "offline_first_event_id": "run_1:106",
        },
        [
            ("token", {"text": "tail-frame"}, "run_1:106"),
            ("stream_end", {}, "run_1:107"),
        ],
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {"session_id": "session_1", "run_id": sid, "terminal": False},
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        # Journal lost its newest entries: stops at 103 < cutoff 105.
        lambda _session_id, run_id, after_seq=None, max_seq=None: {
            "events": _journal_events(101, 103, run_id=run_id)
        },
    )
    monkeypatch.setattr(routes, "stream_owner_session_id", lambda _sid: "session_1")

    body = _run_handler(monkeypatch, stream, "stream_id=run_1&replay=1&after_seq=100")

    payloads = _sse_payloads(body, "apperror")
    assert len(payloads) == 1
    assert payloads[0]["recovery_control"] is True
    assert "tail-frame" not in body
    assert stream.unsubscribed is True


def test_cursor_at_cutoff_has_no_gap_and_streams_tail(monkeypatch):
    """A client already at/past the snapshot cutoff missed nothing the buffer
    ever held — no recovery even when the journal is missing entirely."""
    stream = _FakeStream(
        {
            "last_event_id": "run_1:105",
            "offline_buffered_events": 2,
            "offline_dropped_events": 25,
            "offline_first_event_id": "run_1:106",
        },
        [
            ("token", {"text": "tail-frame"}, "run_1:106"),
            ("stream_end", {}, "run_1:107"),
        ],
    )
    monkeypatch.setattr(routes, "find_run_summary", lambda _sid: None)

    body = _run_handler(monkeypatch, stream, "stream_id=run_1&replay=1&after_seq=105")

    assert _sse_payloads(body, "apperror") == []
    assert "tail-frame" in body
    assert "event: stream_end" in body
    assert stream.unsubscribed is True


def test_coverage_helper_rejects_mid_window_holes(monkeypatch):
    """Endpoint checks are not enough: a malformed/dropped line INSIDE the
    window (journal seqs are contiguous by construction) must fail coverage."""
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {"session_id": "session_1", "run_id": sid, "terminal": False},
    )
    events = [e for e in _journal_events(101, 105) if e["seq"] != 103]
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id, after_seq=None, max_seq=None: {"events": events},
    )
    assert routes._run_journal_covers_offline_gap("run_1", 100, 105) is False


def test_coverage_helper_requires_known_cutoff():
    """Without a same-run journaled cutoff nothing can be proven → not covered."""
    assert routes._run_journal_covers_offline_gap("run_1", 100, None) is False


def test_cursor_inside_retained_tail_needs_no_journal(monkeypatch):
    """A reconnect whose cursor already sits inside the retained buffer range
    is contiguous from the queue alone — no journal required, no recovery,
    even though frames were evicted before the retained head (they are all
    ≤ the client's cursor)."""
    stream = _FakeStream(
        {
            "last_event_id": "run_1:9000",
            "offline_buffered_events": 2,
            "offline_dropped_events": 25,
            "offline_first_event_id": "run_1:8999",
        },
        [
            ("token", {"text": "tail-frame"}, "run_1:8999"),
            ("stream_end", {}, "run_1:9000"),
        ],
    )
    monkeypatch.setattr(routes, "find_run_summary", lambda _sid: None)

    body = _run_handler(monkeypatch, stream, "stream_id=run_1&replay=1&after_seq=8998")

    assert _sse_payloads(body, "apperror") == []
    assert "tail-frame" in body
    assert "event: stream_end" in body
    assert stream.unsubscribed is True


def test_replay_cutoff_stops_at_buffer_head_so_dedup_keeps_queued_frames(monkeypatch):
    """The journal only bridges (cursor → first buffered frame); the dedup
    cutoff must stop there too. With the cutoff at the snapshot's last_event_id
    the drain loop's `seq <= replay_cutoff_seq` filter would silently skip the
    queued frames the journal never emitted — a hole certified as covered."""
    stream = _FakeStream(
        {
            "last_event_id": "run_1:105",
            "offline_buffered_events": 3,
            "offline_dropped_events": 25,
            "offline_first_event_id": "run_1:103",
        },
        [
            ("token", {"text": "buffered-103"}, "run_1:103"),
            ("token", {"text": "buffered-104"}, "run_1:104"),
            ("token", {"text": "buffered-105"}, "run_1:105"),
            ("stream_end", {}, "run_1:106"),
        ],
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {"session_id": "session_1", "run_id": sid, "terminal": False},
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        # The journal only holds the bridge (100 → 102]; 103.. live in the buffer.
        lambda _session_id, run_id, after_seq=None, max_seq=None: {
            "events": _journal_events(101, 102, run_id=run_id)
        },
    )

    body = _run_handler(monkeypatch, stream, "stream_id=run_1&replay=1&after_seq=100")

    assert _sse_payloads(body, "apperror") == []
    assert "journal-101" in body and "journal-102" in body
    # The queued frames past the bridge must all survive the dedup filter.
    assert "buffered-103" in body
    assert "buffered-104" in body
    assert "buffered-105" in body
    assert "event: stream_end" in body
    assert stream.unsubscribed is True


def test_cursor_past_buffer_head_does_not_double_render_queued_frames(monkeypatch):
    """A cursor AT/INSIDE the retained tail (after_seq >= first buffered frame)
    must not receive the queued copy of frames it already rendered.

    The journal-emission bound (buffer head) and the client-cursor bound are
    two DIFFERENT dedup cutoffs: collapsing them onto the buffer head would
    re-stream [head, cursor] from the queue — a visible double-render, since
    the drain loop's `seq <=` filter is the only dedup for replayed streams
    (the client does not de-duplicate token frames itself)."""
    stream = _FakeStream(
        {
            "last_event_id": "run_1:105",
            "offline_buffered_events": 3,
            "offline_dropped_events": 0,
            "offline_first_event_id": "run_1:103",
        },
        [
            ("token", {"text": "buffered-103"}, "run_1:103"),
            ("token", {"text": "buffered-104"}, "run_1:104"),
            ("token", {"text": "buffered-105"}, "run_1:105"),
            ("stream_end", {}, "run_1:106"),
        ],
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {"session_id": "session_1", "run_id": sid, "terminal": False},
    )
    all_events = _journal_events(101, 105)

    def _filtering_read(_session_id, _run_id, after_seq=None, max_seq=None):
        # Mirror the real read_run_events window filters.
        events = [
            e
            for e in all_events
            if (after_seq is None or e["seq"] > after_seq)
            and (max_seq is None or e["seq"] <= max_seq)
        ]
        return {"events": events}

    monkeypatch.setattr(routes, "read_run_events", _filtering_read)

    # Client already rendered up to 104 (e.g. tab flap after a partial drain).
    body = _run_handler(monkeypatch, stream, "stream_id=run_1&replay=1&after_seq=104")

    assert _sse_payloads(body, "apperror") == []
    assert "buffered-103" not in body
    assert "buffered-104" not in body
    assert "buffered-105" in body
    assert "event: stream_end" in body
    assert stream.unsubscribed is True


def test_bogus_cursor_is_clamped_so_terminal_frame_survives(monkeypatch):
    """The client-cursor dedup bound is clamped at the snapshot cutoff.

    A legitimate cursor can never exceed the channel's last known frame; an
    out-of-range one (corrupt client state) must not raise the dedup cutoff
    past the queued terminal frame — the drain loop's `seq <=` skip runs
    BEFORE its terminal break, so a filtered stream_end would pin the loop on
    heartbeats until the write deadline."""
    from urllib.parse import parse_qs

    monkeypatch.setattr(routes, "find_run_summary", lambda _sid: None)
    handler = _Handler()
    handled, cutoff = routes._sse_replay_run_journal_gap_checked(
        handler,
        parse_qs("stream_id=run_1&replay=1&after_seq=999"),
        "run_1",
        {
            "last_event_id": "run_1:105",
            "offline_buffered_events": 3,
            "offline_dropped_events": 0,
            "offline_first_event_id": "run_1:103",
        },
    )
    assert handled is False
    # Clamped to the cutoff (105): the queued stream_end (seq 106) passes the
    # drain loop's dedup filter and terminates the connection.
    assert cutoff == 105
