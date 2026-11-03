from __future__ import annotations

import io
from types import SimpleNamespace
from urllib.parse import urlparse


def _handler():
    return SimpleNamespace(wfile=io.BytesIO())


def _row(seq, event, payload=None, run_id="run_1"):
    return {
        "event": event,
        "type": event,
        "seq": seq,
        "run_id": run_id,
        "event_id": f"{run_id}:{seq}",
        "payload": payload or {},
    }


def _clarify_payload(clarify_id="ee67c18ce636"):
    return {
        "question": "接下来怎么走？",
        "choices_offered": ["A", "B"],
        "clarify_id": clarify_id,
        "session_id": "session_1",
        "kind": "clarify",
    }


def _settle_row(seq, run_id="run_1", name="clarify", preview='{"outcome": "submitted"}'):
    return _row(
        seq,
        "tool_complete",
        {
            "event_type": "tool.completed",
            "name": name,
            "preview": preview,
            "tid": f"call_{seq}",
        },
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Unit: the settle-evidence prescan predicate
# ---------------------------------------------------------------------------


def test_settled_clarify_events_reports_max_settle_seq_per_run():
    from api.run_journal import settled_prompt_events

    events = [
        _row(1, "token", {"text": "hi"}),
        _row(2, "tool", {"event_type": "tool.started", "name": "clarify", "tid": "call_a"}),
        _row(3, "clarify", _clarify_payload()),
        _settle_row(4),
        _row(5, "done", {"status": "completed"}),
    ]
    settled = settled_prompt_events(events)
    assert settled == {("run_1", 4)}


def test_settled_prompt_events_ignores_other_tool_completions():
    from api.run_journal import settled_prompt_events

    events = [
        _row(2, "clarify", _clarify_payload()),
        _settle_row(3, name="terminal"),
        _settle_row(4, name="read_file"),
    ]
    assert settled_prompt_events(events) == set()


def test_settled_prompt_events_tolerates_malformed_rows():
    from api.run_journal import settled_prompt_events

    events = [
        "not-a-dict",
        _row(2, "clarify", _clarify_payload()),
        {"event": "tool_complete", "payload": {"name": "clarify"}},  # no seq
        _settle_row(4),
    ]
    assert settled_prompt_events(events) == {("run_1", 4)}


def test_settled_prompt_events_ignores_settle_rows_from_other_runs():
    from api.run_journal import settled_prompt_events

    events = [
        _row(2, "clarify", _clarify_payload()),
        _settle_row(9, run_id="run_2"),  # different run, later seq
    ]
    assert settled_prompt_events(events) == {("run_2", 9)}


def test_journal_replay_prompt_visible_predicate():
    from api.run_journal import journal_replay_prompt_visible

    settled = {("run_1", 4)}
    # Clarify before the settle row in the same run: hidden.
    assert (
        journal_replay_prompt_visible(
            _row(3, "clarify", _clarify_payload()), settled
        )
        is False
    )
    # Non-prompt rows are always visible.
    assert journal_replay_prompt_visible(_row(1, "token"), settled) is True
    # Clarify in a run with no settle evidence: visible (pending, still answerable).
    assert (
        journal_replay_prompt_visible(
            _row(3, "clarify", _clarify_payload()), {("run_2", 4)}
        )
        is True
    )
    # Malformed rows pass through.
    assert journal_replay_prompt_visible("not-a-dict", settled) is True
    assert journal_replay_prompt_visible({"event": "clarify"}, settled) is True


# ---------------------------------------------------------------------------
# Dead-stream / gap-checked journal replay: _replay_run_journal
# ---------------------------------------------------------------------------


def _drive_replay(monkeypatch, events, after_seq=0, terminal=True):
    import api.routes as routes

    handler = _handler()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": terminal,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {
            "events": events
        },
    )
    assert routes._replay_run_journal(handler, "run_1", after_seq) is True
    return handler.wfile.getvalue().decode("utf-8")


def test_dead_stream_replay_skips_settled_clarify(monkeypatch):
    body = _drive_replay(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}),
            _row(2, "clarify", _clarify_payload()),
            _settle_row(3),
            _row(4, "done", {"status": "completed"}),
        ],
    )
    assert "event: clarify\n" not in body
    assert "id: run_1:2\n" not in body
    assert "event: token\n" in body
    assert "event: tool_complete\n" in body
    assert "event: done\n" in body


def test_dead_stream_replay_keeps_pending_clarify(monkeypatch):
    body = _drive_replay(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}),
            _row(2, "clarify", _clarify_payload()),
            _row(3, "token", {"text": "still waiting"}),
        ],
    )
    assert "event: clarify\n" in body
    assert "id: run_1:2\n" in body


def test_dead_stream_replay_settled_clarify_covers_timeout_completions(monkeypatch):
    # A clarify that timed out (no user answer) still terminates the prompt's
    # lifecycle; replaying the dead card has no recovery value either.
    body = _drive_replay(
        monkeypatch,
        [
            _row(2, "clarify", _clarify_payload()),
            _settle_row(
                3,
                preview='{"responses": [], "outcome": "timeout"}',
            ),
            _row(4, "done", {"status": "completed"}),
        ],
    )
    assert "event: clarify\n" not in body


def test_dead_stream_replay_does_not_apply_settle_to_newer_clarify(monkeypatch):
    # Two clarifies in one run: the first settled, the second still pending.
    # Only the row BEFORE the settle evidence may be dropped.
    body = _drive_replay(
        monkeypatch,
        [
            _row(2, "clarify", _clarify_payload("c_first")),
            _settle_row(3),
            _row(4, "clarify", _clarify_payload("c_second")),
            _row(5, "token", {"text": "waiting"}),
        ],
    )
    assert "c_first" not in body
    assert "c_second" in body


def test_dead_stream_replay_malformed_journal_fails_open(monkeypatch):
    # If the prescan somehow sees garbage, the clarify row must still replay.
    import api.routes as routes
    from api import run_journal

    events = [
        _row(2, "clarify", _clarify_payload()),
        _settle_row(3),
    ]

    def boom(_events):
        raise TypeError("forced prescan failure")

    handler = _handler()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": True,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {"events": events},
    )
    monkeypatch.setattr(run_journal, "settled_prompt_events", boom)
    monkeypatch.setattr(
        routes, "settled_prompt_events", boom
    )  # routes imports it by name
    assert routes._replay_run_journal(handler, "run_1", 0) is True
    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: clarify\n" in body
    assert "event: tool_complete\n" in body


# ---------------------------------------------------------------------------
# Session-scoped SSE stream: emit_replay inside
# _handle_session_run_journal_stream_for_session
# ---------------------------------------------------------------------------


class _FakeSessionStreamHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
        self.command = "GET"
        self.path = "/"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _drive_session_stream(monkeypatch, events):
    """Drive the session events stream with an active (fake) stream, mirroring
    test_session_route_replay_skips_metering_rows: replay runs pre-attach and
    again in reconciliation; the queue carries a terminal event so the drain
    loop exits cleanly."""
    import queue as queue_mod

    import api.routes as routes

    class _FakeStream:
        def __init__(self):
            self.q = queue_mod.Queue()
            self.q.put_nowait(("stream_end", {"status": "done"}, "run_1:999"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_1:999", "offline_buffered_events": 1}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    stream = _FakeStream()
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(
            session_id=sid,
            compact=lambda **_kwargs: {"session_id": sid, "title": "Session"},
        ),
    )
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_a, **_k: "run_1")
    monkeypatch.setattr(routes, "peek_stream", lambda stream_id: stream)
    monkeypatch.setattr(routes, "STREAMS", {"run_1": stream})
    import api.config as config

    monkeypatch.setattr(config, "STREAMS", {"run_1": stream})
    monkeypatch.setattr(
        routes,
        "read_session_run_events",
        lambda *_args, **_kwargs: {"status": "ok", "events": events},
    )
    monkeypatch.setattr(
        routes, "session_journal_fingerprint", lambda session_id: "fp-1"
    )

    handler = _FakeSessionStreamHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_1:0"),
        "session_1",
    )
    return handler.wfile.getvalue().decode("utf-8")


def test_session_stream_replay_skips_settled_clarify(monkeypatch):
    body = _drive_session_stream(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}, run_id="run_1"),
            _row(2, "clarify", _clarify_payload(), run_id="run_1"),
            _settle_row(3, run_id="run_1"),
            _row(4, "done", {"status": "completed"}, run_id="run_1"),
        ],
    )
    assert "event: clarify\n" not in body
    assert "id: run_1:2\n" not in body
    assert "event: token\n" in body
    assert "event: done\n" in body


def test_session_stream_replay_keeps_pending_clarify(monkeypatch):
    body = _drive_session_stream(
        monkeypatch,
        [
            _row(2, "clarify", _clarify_payload(), run_id="run_1"),
            _row(3, "token", {"text": "hi"}, run_id="run_1"),
        ],
    )
    assert "event: clarify\n" in body
    assert "id: run_1:2\n" in body
