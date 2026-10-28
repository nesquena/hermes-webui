from __future__ import annotations

import io
from types import SimpleNamespace
from urllib.parse import urlparse


def _handler():
    return SimpleNamespace(wfile=io.BytesIO())


def _row(seq, event, payload=None, run_id="run_1", terminal=False):
    row = {
        "event": event,
        "type": event,
        "seq": seq,
        "run_id": run_id,
        "event_id": f"{run_id}:{seq}",
        "payload": payload or {},
    }
    if terminal:
        row["terminal"] = True
    return row


def _approval_payload(approval_id="gwlocal:aaa", pending_count=1):
    return {
        "command": "rm -rf /tmp/probe",
        "pattern_key": "SQL DROP",
        "description": "SQL DROP",
        "approval_id": approval_id,
        "request_id": approval_id.replace("gwlocal:", ""),
        "pending_count": pending_count,
        "allow_permanent": True,
        "allow_session": True,
    }


def _clarify_payload(clarify_id="ee67c18ce636"):
    return {
        "question": "接下来怎么走？",
        "choices_offered": ["A", "B"],
        "clarify_id": clarify_id,
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
# Unit: terminal-settle evidence prescan
# ---------------------------------------------------------------------------


def test_terminal_settled_events_reports_min_terminal_seq_per_run():
    from api.run_journal import terminal_settled_prompt_events

    events = [
        _row(1, "token", {"text": "hi"}),
        _row(2, "approval", _approval_payload()),
        _row(3, "done", {"status": "completed"}, terminal=True),
        _row(4, "approval", _approval_payload("gwlocal:bbb")),  # post-terminal
    ]
    settled = terminal_settled_prompt_events(events)
    # only the pre-terminal approval seq is a key; the map holds min terminal seq
    assert settled == {("run_1", 2): 3}


def test_terminal_settled_events_covers_all_terminal_kinds():
    from api.run_journal import terminal_settled_prompt_events as f

    for kind, state in [
        ("done", "completed"),
        ("cancel", "interrupted-by-user"),
        ("apperror", "errored"),
        ("error", "error"),
        ("stream_end", "completed"),
    ]:
        events = [
            _row(2, "approval", _approval_payload()),
            _row(3, kind, {}, terminal=True),
        ]
        assert f(events) == {("run_1", 2): 3}, kind


def test_terminal_settled_events_ignores_promptless_terminal_rows():
    from api.run_journal import terminal_settled_prompt_events as f

    events = [
        _row(1, "token", {"text": "hi"}),
        _row(2, "done", {"status": "completed"}, terminal=True),
        _row(3, "approval", _approval_payload()),  # after terminal: NOT settled
    ]
    assert f(events) == {}


def test_terminal_settled_events_keeps_pending_when_no_terminal():
    from api.run_journal import terminal_settled_prompt_events as f

    events = [
        _row(1, "token", {"text": "hi"}),
        _row(2, "approval", _approval_payload()),
        _row(3, "token", {"text": "still running"}),
    ]
    assert f(events) == {}


def test_terminal_settled_events_ignores_non_flagged_terminal_rows():
    # A done row without terminal=True (defensive: some legacy rows?) must not
    # settle anything — evidence fails closed to "keep visible".
    from api.run_journal import terminal_settled_prompt_events as f

    events = [
        _row(2, "approval", _approval_payload()),
        _row(3, "done", {"status": "completed"}),  # no terminal flag
    ]
    assert f(events) == {}


def test_terminal_settled_events_parallel_queue_all_dead_at_done():
    from api.run_journal import terminal_settled_prompt_events as f

    # Parallel tool approvals (#527): two queued entries, run then finishes.
    events = [
        _row(2, "approval", _approval_payload("gwlocal:a", pending_count=2)),
        _row(3, "approval", _approval_payload("gwlocal:b", pending_count=2)),
        _row(4, "token", {"text": "tail"}),
        _row(5, "done", {"status": "completed"}, terminal=True),
    ]
    assert f(events) == {("run_1", 2): 5, ("run_1", 3): 5}


def test_terminal_settled_events_tolerates_malformed_rows():
    from api.run_journal import terminal_settled_prompt_events as f

    events = [
        "not-a-dict",
        _row(2, "approval", _approval_payload()),
        {"event": "done", "terminal": True},  # no seq
        _row(4, "done", {"status": "completed"}, terminal=True),
    ]
    assert f(events) == {("run_1", 2): 4}


def test_terminal_settled_events_scoped_per_run():
    from api.run_journal import terminal_settled_prompt_events as f

    events = [
        _row(2, "approval", _approval_payload(), run_id="run_1"),
        _row(3, "done", {"status": "completed"}, run_id="run_2", terminal=True),
    ]
    assert f(events) == {}


def test_terminal_settled_events_includes_clarify():
    from api.run_journal import terminal_settled_prompt_events as f

    events = [
        _row(2, "clarify", _clarify_payload()),
        _row(3, "done", {"status": "completed"}, terminal=True),
    ]
    assert f(events) == {("run_1", 2): 3}


# ---------------------------------------------------------------------------
# Unit: combined visibility predicate
# ---------------------------------------------------------------------------


def test_journal_replay_prompt_visible_with_terminal_settle():
    from api.run_journal import journal_replay_prompt_visible

    terminal_settled = {("run_1", 2): 3}
    no_tool_settle = set()
    # Pre-terminal approval in same run: hidden by the terminal proof.
    assert (
        journal_replay_prompt_visible(
            _row(2, "approval", _approval_payload()),
            no_tool_settle,
            terminal_settled=terminal_settled,
        )
        is False
    )
    # Same run, seq after the terminal row: visible.
    assert (
        journal_replay_prompt_visible(
            _row(9, "approval", _approval_payload()),
            no_tool_settle,
            terminal_settled=terminal_settled,
        )
        is True
    )
    # Other run: visible.
    assert (
        journal_replay_prompt_visible(
            _row(2, "approval", _approval_payload(), run_id="run_2"),
            no_tool_settle,
            terminal_settled=terminal_settled,
        )
        is True
    )
    # Clarify settled by tool_complete still hidden with empty terminal map.
    assert (
        journal_replay_prompt_visible(
            _row(2, "clarify", _clarify_payload()), {("run_1", 3)}
        )
        is False
    )
    # Non-prompt rows always visible.
    assert journal_replay_prompt_visible(_row(1, "token"), no_tool_settle, terminal_settled=terminal_settled) is True
    # Malformed fails open.
    assert journal_replay_prompt_visible("not-a-dict", no_tool_settle, terminal_settled=terminal_settled) is True


# ---------------------------------------------------------------------------
# Dead-stream replay: _replay_run_journal honours terminal settle
# ---------------------------------------------------------------------------


def _drive_replay(monkeypatch, events, after_seq=0):
    import api.routes as routes

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
    assert routes._replay_run_journal(handler, "run_1", after_seq) is True
    return handler.wfile.getvalue().decode("utf-8")


def test_dead_stream_replay_skips_pre_terminal_approval(monkeypatch):
    body = _drive_replay(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}),
            _row(2, "approval", _approval_payload()),
            _row(3, "done", {"status": "completed"}, terminal=True),
        ],
    )
    assert "event: approval\n" not in body
    assert "gwlocal:aaa" not in body
    assert "event: token\n" in body
    assert "event: done\n" in body


def test_dead_stream_replay_keeps_approval_when_run_still_active(monkeypatch):
    body = _drive_replay(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}),
            _row(2, "approval", _approval_payload()),
            _row(3, "token", {"text": "waiting"}),
        ],
    )
    assert "event: approval\n" in body


def test_dead_stream_replay_skips_cancelled_run_clarify(monkeypatch):
    # A clarify in a cancelled run has no tool_complete(clarify) settle row,
    # but the terminal cancel row proves nothing awaits an answer anymore.
    body = _drive_replay(
        monkeypatch,
        [
            _row(2, "clarify", _clarify_payload()),
            _row(3, "cancel", {}, terminal=True),
        ],
    )
    assert "event: clarify\n" not in body


def test_dead_stream_replay_malformed_prescan_fails_open(monkeypatch):
    import api.routes as routes

    events = [
        _row(2, "approval", _approval_payload()),
        _row(3, "done", {"status": "completed"}, terminal=True),
    ]

    def boom(_events):
        raise TypeError("forced prescan failure")

    handler = _handler()
    monkeypatch.setattr(routes, "find_run_summary", lambda stream_id: {
        "session_id": "session_1", "run_id": stream_id, "terminal": True,
    })
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {"events": events},
    )
    monkeypatch.setattr(routes, "settled_prompt_events", boom)
    monkeypatch.setattr(routes, "terminal_settled_prompt_events", boom)
    assert routes._replay_run_journal(handler, "run_1", 0) is True
    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: approval\n" in body
    assert "event: done\n" in body


# ---------------------------------------------------------------------------
# Session events stream: emit_replay honours terminal settle
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
        self.headers[key] = key

    def end_headers(self):
        pass


def _drive_session_stream(monkeypatch, events):
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
    monkeypatch.setattr(routes, "session_journal_fingerprint", lambda session_id: "fp-1")

    handler = _FakeSessionStreamHandler()
    routes._handle_session_sse_stream_for_session(
        handler,
        urlparse("/api/sessions/session_1/events?after_event_id=run_1:0"),
        "session_1",
    )
    return handler.wfile.getvalue().decode("utf-8")


def test_session_stream_replay_skips_pre_terminal_approval(monkeypatch):
    body = _drive_session_stream(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}, run_id="run_1"),
            _row(2, "approval", _approval_payload(), run_id="run_1"),
            _row(3, "done", {"status": "completed"}, run_id="run_1", terminal=True),
        ],
    )
    assert "event: approval\n" not in body
    assert "event: token\n" in body
    assert "event: done\n" in body


def test_session_stream_replay_keeps_active_run_approval(monkeypatch):
    body = _drive_session_stream(
        monkeypatch,
        [
            _row(2, "approval", _approval_payload(), run_id="run_1"),
            _row(3, "token", {"text": "hi"}, run_id="run_1"),
        ],
    )
    assert "event: approval\n" in body


# ---------------------------------------------------------------------------
# Cross-kind: clarify tool-completion evidence must never settle an approval
# ---------------------------------------------------------------------------


def test_journal_replay_prompt_visible_clarify_settle_never_hides_approval():
    from api.run_journal import journal_replay_prompt_visible

    settled = {("run_1", 4)}
    # Pending approval BEFORE the clarify completion in the same run:
    # still visible — the completion is clarify-kind evidence only.
    assert (
        journal_replay_prompt_visible(_row(2, "approval", _approval_payload()), settled)
        is True
    )
    # Approval AFTER the clarify completion in the same run: visible too.
    assert (
        journal_replay_prompt_visible(_row(6, "approval", _approval_payload()), settled)
        is True
    )
    # Control: the completed clarify itself stays settled (hidden).
    assert (
        journal_replay_prompt_visible(_row(3, "clarify", _clarify_payload()), settled)
        is False
    )
    # Control: the same-run terminal proof still hides the approval.
    assert (
        journal_replay_prompt_visible(
            _row(2, "approval", _approval_payload()),
            settled,
            terminal_settled={("run_1", 2): 5},
        )
        is False
    )


def test_dead_stream_replay_cross_kind_clarify_settle_keeps_pending_approval(monkeypatch):
    # Reviewer reproduction: a live (nonterminal) run whose journal mixes a
    # still-pending approval with an already-completed clarify. The approval
    # must survive the clarify's tool_complete settle evidence.
    body = _drive_replay(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}),
            _row(2, "approval", _approval_payload()),
            _row(3, "clarify", _clarify_payload()),
            _settle_row(4),
            _row(5, "token", {"text": "still waiting"}),
        ],
    )
    assert "event: approval\n" in body
    assert "id: run_1:2\n" in body
    assert "gwlocal:aaa" in body
    # Controls: the completed clarify stays settled; other rows untouched.
    assert "event: clarify\n" not in body
    assert "event: token\n" in body
    assert "event: tool_complete\n" in body


def test_dead_stream_replay_approval_after_clarify_settle_still_visible(monkeypatch):
    # Same-run ordering: even an approval emitted AFTER the clarify's
    # tool_complete stays visible while the run is still live — the settle
    # seq comparison is kind-scoped, not a blanket run cursor.
    body = _drive_replay(
        monkeypatch,
        [
            _row(2, "clarify", _clarify_payload()),
            _settle_row(3),
            _row(4, "approval", _approval_payload()),
            _row(5, "token", {"text": "waiting"}),
        ],
    )
    assert "event: approval\n" in body
    assert "id: run_1:4\n" in body
    assert "event: clarify\n" not in body


def test_session_stream_replay_cross_kind_clarify_settle_keeps_pending_approval(monkeypatch):
    # Same mixed journal through the session events stream's emit_replay.
    body = _drive_session_stream(
        monkeypatch,
        [
            _row(1, "token", {"text": "hi"}, run_id="run_1"),
            _row(2, "approval", _approval_payload(), run_id="run_1"),
            _row(3, "clarify", _clarify_payload(), run_id="run_1"),
            _settle_row(4, run_id="run_1"),
            _row(5, "token", {"text": "still waiting"}, run_id="run_1"),
        ],
    )
    assert "event: approval\n" in body
    assert "id: run_1:2\n" in body
    # Controls: completed clarify still filtered, run rows untouched.
    assert "event: clarify\n" not in body
    assert "event: token\n" in body
    assert "event: tool_complete\n" in body
