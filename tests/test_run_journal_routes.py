from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import io
import json
import queue
import sys
import threading
import types


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SRC = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_stream_status_exposes_replay_summary():
    status_pos = ROUTES_SRC.index('parsed.path == "/api/chat/stream/status"')
    block = ROUTES_SRC[status_pos : status_pos + 900]

    assert "find_run_summary(stream_id)" in block
    assert '"replay_available"' in block
    assert '"journal"' in block
    assert "_run_journal_status_payload" in block


def test_dead_stream_sse_replays_journal_before_404_fallback():
    handler_pos = ROUTES_SRC.index("def _handle_sse_stream")
    block = ROUTES_SRC[handler_pos : handler_pos + 1800]

    assert "find_run_summary(stream_id)" in block
    assert "stream not found" in block
    assert "_replay_run_journal" in block
    assert "_parse_run_journal_after_seq" in block
    assert 'Content-Type", "text/event-stream; charset=utf-8"' in block


def test_active_stream_replay_uses_snapshot_cutoff_and_skips_duplicate_queue_items(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "replayed"}, "run_1:1"))
            self.q.put_nowait(("stream_end", {}, "run_1:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "run_1:1", "offline_buffered_events": 1}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    stream = FakeStream()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {
            "events": [
                {
                    "event": "token",
                    "payload": {"text": "replayed"},
                    "event_id": f"{run_id}:1",
                }
            ]
        },
    )
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_args, **_kwargs: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_1"] = stream
    try:
        routes._handle_sse_stream(handler, urlparse("/api/chat/stream?stream_id=run_1&replay=1&after_seq=0"))
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert body.count("event: token\n") == 1
    assert "id: run_1:1\n" in body
    assert "id: run_1:2\n" in body
    assert stream.unsubscribed is True


def test_active_stream_snapshot_keeps_items_for_new_run_with_same_seq_range(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "fresh"}, "run_new:1"))
            self.q.put_nowait(("stream_end", {}, "run_new:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {
                "last_event_id": "run_old:3",
                "offline_buffered_events": 2,
            }

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    stream = FakeStream()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_2",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {"events": []},
    )
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_args, **_kwargs: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_new"] = stream
    try:
        routes._handle_sse_stream(
            handler,
            urlparse("/api/chat/stream?stream_id=run_new&replay=1&after_seq=0"),
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_new:1\n" in body
    assert "id: run_new:2\n" in body
    assert body.count("id: run_new:1\n") == 1
    assert stream.unsubscribed is True


def test_active_stream_replay_without_journal_keeps_buffered_queue_items(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "buffered"}, "missing_journal_run:1"))
            self.q.put_nowait(("stream_end", {}, "missing_journal_run:2"))

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": "missing_journal_run:1", "offline_buffered_events": 1}

        def unsubscribe(self, _q):
            pass

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: None)
    handler = Handler()
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["missing_journal_run"] = FakeStream()
    try:
        routes._handle_sse_stream(
            handler,
            urlparse("/api/chat/stream?stream_id=missing_journal_run&replay=1&after_seq=0"),
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: missing_journal_run:1\n" in body
    assert "event: token\n" in body
    assert "buffered" in body


def test_live_sse_uses_each_queue_items_own_event_id():
    import api.routes as routes
    from api.config import create_stream_channel

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    stream = create_stream_channel()
    stream.put_nowait(("token", {"text": "A"}, "run_own_id:1"))
    stream.put_nowait(("stream_end", {"ok": True}, "run_own_id:2"))
    handler = Handler()
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_own_id"] = stream
    try:
        routes._handle_sse_stream(handler, urlparse("/api/chat/stream?stream_id=run_own_id"))
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_own_id:1\nevent: token\n" in body
    assert "id: run_own_id:2\nevent: stream_end\n" in body
    assert body.count("id: run_own_id:2\n") == 1


def test_replay_emits_event_ids_and_stale_restart_diagnostic():
    replay_pos = ROUTES_SRC.index("def _replay_run_journal")
    block = ROUTES_SRC[replay_pos : replay_pos + 2400]

    assert "_read_run_journal_until_complete" in block
    assert "_sse_with_id" in block
    assert "stale_interrupted_event" in block


def test_session_payload_exposes_runtime_journal_for_stale_streams():
    assert "original_stream_id = getattr(s, \"active_stream_id\", None)" in ROUTES_SRC
    assert '"runtime_journal"' in ROUTES_SRC
    assert '"runtime_journal_snapshot"' in ROUTES_SRC
    assert "_run_journal_live_snapshot(original_stream_id, handler=handler)" in ROUTES_SRC
    assert 'terminal_state = "lost-worker-bookkeeping"' in ROUTES_SRC
    assert "active=journal_active" in ROUTES_SRC
    assert "journal_active = bool(original_stream_id in active_stream_ids)" in ROUTES_SRC


def test_live_journal_snapshot_reconstructs_visible_progress_and_tool_aliases(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "last_seq": 4,
            "last_event_id": f"{stream_id}:4",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {
            "events": [
                {
                    "seq": 1,
                    "event": "token",
                    "payload": {"text": "First segment."},
                    "event_id": f"{run_id}:1",
                    "created_at": 1000.0,
                },
                {
                    "seq": 2,
                    "event": "tool",
                    "payload": {
                        "name": "terminal",
                        "preview": "running tests",
                        "tool_use_id": "toolu_123",
                        "args": {"command": "pytest -q", "extra": "x" * 200},
                    },
                    "event_id": f"{run_id}:2",
                },
                {
                    "seq": 3,
                    "event": "tool_complete",
                    "payload": {
                        "name": "terminal",
                        "preview": "passed",
                        "tool_use_id": "toolu_123",
                        "duration": 1.25,
                    },
                    "event_id": f"{run_id}:3",
                },
                {
                    "seq": 4,
                    "event": "reasoning",
                    "payload": {"text": "Checked result."},
                    "event_id": f"{run_id}:4",
                },
                {
                    "seq": 5,
                    "event": "token",
                    "payload": {"text": " Second segment."},
                    "event_id": f"{run_id}:5",
                    "created_at": 1001.0,
                },
            ]
        },
    )

    snapshot = routes._run_journal_live_snapshot("run_1")

    assert snapshot["last_seq"] == 5
    assert snapshot["last_event_id"] == "run_1:5"
    assert snapshot["last_assistant_text"] == "First segment. Second segment."
    assert snapshot["last_reasoning_text"] == "Checked result."
    assert snapshot["current_live_segment_seq"] == 2
    assert snapshot["activity_burst_anchors"] == [{"id": 1, "textEnd": len("First segment.")}]
    assert snapshot["messages"] == [
        {
            "role": "assistant",
            "content": "First segment. Second segment.",
            "reasoning": "Checked result.",
            "_live": True,
            "_journal_snapshot": True,
            "_journal_stream_id": "run_1",
            "_ts": 1001.0,
        }
    ]
    tool = snapshot["tool_calls"][0]
    assert tool["name"] == "terminal"
    assert tool["done"] is True
    assert tool["tid"] == "toolu_123"
    assert tool["tool_use_id"] == "toolu_123"
    assert tool["activityBurstId"] == 1
    assert tool["activitySegmentSeq"] == 1
    assert tool["snippet"] == "passed"
    assert tool["duration"] == 1.25
    assert tool["args"]["extra"] == "x" * 200
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    assert [row["role"] for row in rows] == ["prose", "tool", "thinking", "prose"]
    assert rows[2]["local_id"] == "live-reasoning:run_1:2"
    assert rows[2]["group"]["activity_segment_seq"] == 2


def test_live_journal_snapshot_bounds_pathological_tool_args(monkeypatch):
    import api.routes as routes

    long_command = "python -c " + repr("print('x')\n" * 24)
    huge_args = {
        "command": long_command,
        "items": [{"index": i, "payload": "x" * 100} for i in range(50_000)],
    }
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "last_seq": 1,
            "last_event_id": f"{stream_id}:1",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {
            "events": [
                {
                    "seq": 1,
                    "event": "tool",
                    "payload": {
                        "name": "terminal",
                        "tool_use_id": "toolu_huge",
                        "args": huge_args,
                    },
                    "event_id": f"{run_id}:1",
                },
            ]
        },
    )

    snapshot = routes._run_journal_live_snapshot("run_1")
    tool = snapshot["tool_calls"][0]
    assert tool["args"]["command"] == long_command
    assert len(tool["args"]["items"]) <= 64
    assert len(json.dumps(snapshot, sort_keys=True)) < 200_000


def test_status_payload_marks_non_terminal_dead_journal_as_stale():
    import api.routes as routes

    payload = routes._run_journal_status_payload(
        {
            "session_id": "session_1",
            "run_id": "run_1",
            "last_seq": 3,
            "last_event_id": "run_1:3",
            "last_event": "token",
            "terminal": False,
            "terminal_state": "running",
        },
        active=False,
    )

    assert payload["terminal"] is False
    assert payload["terminal_state"] == "lost-worker-bookkeeping"
    assert payload["last_event_id"] == "run_1:3"


def test_status_payload_preserves_terminal_error_state():
    import api.routes as routes

    payload = routes._run_journal_status_payload(
        {
            "session_id": "session_1",
            "run_id": "run_1",
            "terminal": True,
            "terminal_state": "interrupted-by-crash",
            "last_event": "apperror",
        },
        active=False,
    )

    assert payload["terminal"] is True
    assert payload["terminal_state"] == "interrupted-by-crash"


def test_replay_run_journal_writes_replayed_events_and_synthetic_terminal(monkeypatch):
    import api.routes as routes

    handler = SimpleNamespace(wfile=io.BytesIO())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {
            "events": [
                {
                    "event": "token",
                    "payload": {"text": "hello"},
                    "event_id": f"{run_id}:1",
                }
            ]
        },
    )
    monkeypatch.setattr(
        routes,
        "stale_interrupted_event",
        lambda session_id, run_id, after_seq=None, max_seq=None: {
            "event": "apperror",
            "payload": {"type": "interrupted"},
            "event_id": f"{run_id}:2",
        },
    )

    assert routes._replay_run_journal(handler, "run_1", 0) is True
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:1\n" in body
    assert "event: token\n" in body
    assert "id: run_1:2\n" in body
    assert "event: apperror\n" in body


def test_replay_run_journal_honors_after_seq_cursor(monkeypatch):
    import api.routes as routes

    captured = {}
    handler = SimpleNamespace(wfile=io.BytesIO())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": True,
        },
    )

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        captured["after_seq"] = after_seq
        captured["max_seq"] = max_seq
        return {
            "events": [
                {
                    "event": "done",
                    "payload": {"session": {"session_id": session_id}},
                    "event_id": f"{run_id}:4",
                }
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)

    assert routes._replay_run_journal(handler, "run_1", 3) is True
    assert captured["after_seq"] == 3
    assert captured["max_seq"] is None
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:4\n" in body
    assert "event: done\n" in body


def test_replay_run_journal_hydrates_compacted_terminal_session(tmp_path, monkeypatch):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session_id = "session_replay_huge_terminal"
    run_id = "run_replay_huge_terminal"
    huge_context = "x" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000)
    session = Session(
        session_id=session_id,
        title="Replay hydrate",
        messages=[
            {"role": "user", "content": huge_context, "timestamp": 1.0},
            {"role": "assistant", "content": "final answer", "_ts": 2.0},
        ],
    )
    session.save(skip_index=True)
    run_journal.append_run_event(
        session_id,
        run_id,
        "done",
        {
            "terminal_session_persisted": True,
            "terminal_session_persisted_session_id": session_id,
            "session": session.compact()
            | {"messages": list(session.messages), "message_count": len(session.messages)}
        },
        session_dir=session_dir,
    )
    compact = run_journal.read_run_events(session_id, run_id, session_dir=session_dir)
    assert "messages" not in compact["events"][0]["payload"]["session"]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is True

    data_line = next(
        line for line in handler.wfile.getvalue().decode("utf-8").splitlines() if line.startswith("data: ")
    )
    data = json.loads(data_line.removeprefix("data: "))
    assert data["session"]["session_id"] == session_id
    assert data["session"]["messages"][0]["content"] == huge_context
    assert data["session"]["messages"][1]["content"] == "final answer"


def test_gateway_terminal_replay_hydrates_full_persisted_transcripts(tmp_path, monkeypatch):
    import api.gateway_chat as gateway_chat
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes
    from api.models import Session
    from api.streaming import _session_payload_with_full_messages

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    def base_messages(case_name):
        messages = []
        for idx in range(6):
            messages.append(
                {
                    "role": "user",
                    "content": f"{case_name} prompt {idx}",
                    "timestamp": float(idx * 2 + 1),
                }
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": f"{case_name} answer {idx}",
                    "_ts": float(idx * 2 + 2),
                }
            )
        return messages

    cases = [
        ("done", {"usage": {"output_tokens": 3}}),
        ("cancel", {"message": "Cancelled by user"}),
        (
            "apperror",
            {
                "label": "Gateway request failed",
                "type": "gateway_error",
                "message": "Gateway failed",
            },
        ),
    ]
    for event_name, raw_payload in cases:
        session_id = f"session_gateway_persisted_{event_name}"
        stream_id = f"run_gateway_persisted_{event_name}"
        session = Session(
            session_id=session_id,
            title=f"Gateway {event_name}",
            messages=base_messages(event_name),
            active_stream_id=stream_id,
        )
        session.save(skip_index=True)
        if event_name == "done":
            payload = {
                **raw_payload,
                "session": _session_payload_with_full_messages(session, tool_calls=[]),
            }
        else:
            payload = raw_payload

        settled = gateway_chat._settle_gateway_terminal_event_payload(
            session_id,
            stream_id,
            tmp_path,
            "model-x",
            "gateway",
            event_name,
            payload,
        )
        assert settled["terminal_session_persisted"] is True
        assert settled["terminal_session_persisted_session_id"] == session_id
        assert len(settled["session"]["messages"]) >= 12

        run_journal.append_run_event(
            session_id,
            stream_id,
            event_name,
            settled,
            session_dir=session_dir,
        )
        compact = run_journal.read_run_events(session_id, stream_id, session_dir=session_dir)
        compact_payload = compact["events"][0]["payload"]
        assert "messages" not in compact_payload["session"]
        assert compact_payload["session"]["messages_omitted"]["reason"] == "terminal_session_transcript_persisted"

        monkeypatch.setattr(
            routes,
            "find_run_summary",
            lambda candidate_stream_id, _stream_id=stream_id: (
                run_journal.find_run_summary(_stream_id, session_dir=session_dir)
                if candidate_stream_id == _stream_id
                else None
            ),
        )
        monkeypatch.setattr(
            routes,
            "read_run_events",
            lambda candidate_session_id, candidate_run_id, after_seq=None, max_seq=None: (
                run_journal.read_run_events(
                    candidate_session_id,
                    candidate_run_id,
                    after_seq=after_seq,
                    max_seq=max_seq,
                    session_dir=session_dir,
                )
            ),
        )
        handler = SimpleNamespace(wfile=io.BytesIO())

        assert routes._replay_run_journal(handler, stream_id, 0, include_stale=False) is True

        data_line = next(
            line
            for line in handler.wfile.getvalue().decode("utf-8").splitlines()
            if line.startswith("data: ")
        )
        data = json.loads(data_line.removeprefix("data: "))
        persisted = models.get_session(session_id)
        assert data["session"]["session_id"] == session_id
        assert data["session"]["messages"] == persisted.messages
        assert len(data["session"]["messages"]) == len(persisted.messages)
        assert data["session"]["messages"][0]["content"] == f"{event_name} prompt 0"


def test_replay_run_journal_hydrates_compacted_continuation_terminal_session(tmp_path, monkeypatch):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    origin_session_id = "compression_origin"
    continuation_session_id = "compression_continuation"
    run_id = "run_replay_compression"
    origin = Session(
        session_id=origin_session_id,
        title="Pre compression",
        messages=[
            {"role": "user", "content": "old prompt", "timestamp": 1.0},
            {"role": "assistant", "content": "old snapshot", "_ts": 2.0},
        ],
        pre_compression_snapshot=True,
    )
    continuation = Session(
        session_id=continuation_session_id,
        title="Continuation",
        messages=[
            {"role": "user", "content": "continue", "timestamp": 3.0},
            {"role": "assistant", "content": "continuation final", "_ts": 4.0},
        ],
        parent_session_id=origin_session_id,
        terminal_replay_origin_session_id=origin_session_id,
        terminal_replay_run_id=run_id,
        terminal_replay_stream_id=run_id,
    )
    origin.save(skip_index=True)
    continuation.save(skip_index=True)
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "done",
        {
            "terminal_session_persisted": True,
            "terminal_session_persisted_session_id": continuation_session_id,
            "session_id": continuation_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "session": continuation.compact()
            | {
                "messages": list(continuation.messages),
                "message_count": len(continuation.messages),
            },
        },
        session_dir=session_dir,
    )
    compact = run_journal.read_run_events(origin_session_id, run_id, session_dir=session_dir)
    compact_payload = compact["events"][0]["payload"]
    assert "messages" not in compact_payload["session"]
    assert compact_payload["session"]["session_id"] == continuation_session_id

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is True

    data_line = next(
        line for line in handler.wfile.getvalue().decode("utf-8").splitlines() if line.startswith("data: ")
    )
    data = json.loads(data_line.removeprefix("data: "))
    assert data["session"]["session_id"] == continuation_session_id
    assert data["session"]["parent_session_id"] == origin_session_id
    assert data["session"]["terminal_replay_origin_session_id"] == origin_session_id
    assert data["session"]["terminal_replay_run_id"] == run_id
    assert data["session"]["terminal_replay_stream_id"] == run_id
    assert data["session"]["messages"][0]["content"] == "continue"
    assert data["session"]["messages"][1]["content"] == "continuation final"


def test_replay_run_journal_fails_closed_for_same_parent_sibling_continuation(tmp_path, monkeypatch):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    origin_session_id = "compression_origin_same_parent"
    target_run_id = "run_owned_continuation"
    sibling_run_id = "run_sibling_continuation"
    sibling_session_id = "compression_sibling_continuation"
    assistant = {"role": "assistant", "content": "sibling final", "_ts": 6.0}
    sibling = Session(
        session_id=sibling_session_id,
        messages=[
            {"role": "user", "content": "sibling prompt", "timestamp": 5.0},
            assistant,
        ],
        parent_session_id=origin_session_id,
        terminal_replay_origin_session_id=origin_session_id,
        terminal_replay_run_id=sibling_run_id,
        terminal_replay_stream_id=sibling_run_id,
    )
    sibling.save(skip_index=True)
    run_journal.append_run_event(
        origin_session_id,
        target_run_id,
        "token",
        {"text": "must not leak before bad terminal"},
        session_dir=session_dir,
    )
    run_journal.append_run_event(
        origin_session_id,
        target_run_id,
        "done",
        {
            "terminal_session_persisted": True,
            "terminal_session_persisted_session_id": sibling_session_id,
            "session_id": sibling_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": sibling_session_id,
            "continuation_session_id": sibling_session_id,
            "session": sibling.compact()
            | {"messages": list(sibling.messages), "message_count": len(sibling.messages)},
        },
        session_dir=session_dir,
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, target_run_id, 0, include_stale=False) is False
    assert handler.wfile.getvalue() == b""


def test_replay_run_journal_fails_closed_for_wrong_terminal_target_run(tmp_path, monkeypatch):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    origin_session_id = "compression_origin_wrong_run"
    continuation_session_id = "compression_continuation_wrong_run"
    run_id = "run_expected_terminal"
    assistant = {"role": "assistant", "content": "continuation final", "_ts": 4.0}
    continuation = Session(
        session_id=continuation_session_id,
        messages=[
            {"role": "user", "content": "continue", "timestamp": 3.0},
            assistant,
        ],
        parent_session_id=origin_session_id,
        terminal_replay_origin_session_id=origin_session_id,
        terminal_replay_run_id=run_id,
        terminal_replay_stream_id=run_id,
    )
    continuation.save(skip_index=True)
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "token",
        {"text": "must not leak before bad terminal"},
        session_dir=session_dir,
    )
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "done",
        {
            "terminal_session_persisted": True,
            "terminal_session_persisted_session_id": continuation_session_id,
            "session_id": continuation_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": continuation_session_id,
                "run_id": "run_attacker_rewrite",
                "stream_id": run_id,
                "message_index": 1,
                "message_ref": routes._assistant_anchor_scene_message_ref(assistant),
            },
            "session": continuation.compact()
            | {"messages": list(continuation.messages), "message_count": len(continuation.messages)},
        },
        session_dir=session_dir,
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is False
    assert handler.wfile.getvalue() == b""


def test_replay_run_journal_fails_closed_for_bad_continuation_lineage(tmp_path, monkeypatch):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    origin_session_id = "compression_origin_bad_lineage"
    continuation_session_id = "compression_continuation_bad_lineage"
    run_id = "run_bad_lineage"
    assistant = {"role": "assistant", "content": "continuation final", "_ts": 4.0}
    continuation = Session(
        session_id=continuation_session_id,
        messages=[
            {"role": "user", "content": "continue", "timestamp": 3.0},
            assistant,
        ],
        parent_session_id="different_origin",
    )
    continuation.save(skip_index=True)
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "token",
        {"text": "must not leak before bad terminal"},
        session_dir=session_dir,
    )
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "done",
        {
            "terminal_session_persisted": True,
            "terminal_session_persisted_session_id": continuation_session_id,
            "session_id": continuation_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": continuation_session_id,
                "run_id": run_id,
                "stream_id": run_id,
                "message_index": 1,
                "message_ref": routes._assistant_anchor_scene_message_ref(assistant),
            },
            "session": continuation.compact()
            | {"messages": list(continuation.messages), "message_count": len(continuation.messages)},
        },
        session_dir=session_dir,
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is False
    assert handler.wfile.getvalue() == b""


def test_replay_run_journal_fails_closed_for_terminal_message_ref_mismatch(tmp_path, monkeypatch):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    origin_session_id = "compression_origin_bad_ref"
    continuation_session_id = "compression_continuation_bad_ref"
    run_id = "run_bad_ref"
    continuation = Session(
        session_id=continuation_session_id,
        messages=[
            {"role": "user", "content": "continue", "timestamp": 3.0},
            {"role": "assistant", "content": "continuation final", "_ts": 4.0},
        ],
        parent_session_id=origin_session_id,
    )
    continuation.save(skip_index=True)
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "token",
        {"text": "must not leak before bad terminal"},
        session_dir=session_dir,
    )
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "done",
        {
            "terminal_session_persisted": True,
            "terminal_session_persisted_session_id": continuation_session_id,
            "session_id": continuation_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": continuation_session_id,
                "run_id": run_id,
                "stream_id": run_id,
                "message_index": 1,
                "message_ref": "0" * 64,
            },
            "session": continuation.compact()
            | {"messages": list(continuation.messages), "message_count": len(continuation.messages)},
        },
        session_dir=session_dir,
    )
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is False
    assert handler.wfile.getvalue() == b""


def test_replay_run_journal_keeps_unpersisted_terminal_payload_after_save_failure(
    tmp_path,
    monkeypatch,
):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session_id = "save_failure_replay"
    run_id = "run_save_failure_replay"
    assistant = {"role": "assistant", "content": "journal-only terminal", "_ts": 8.0}
    run_journal.append_run_event(
        session_id,
        run_id,
        "apperror",
        {
            "terminal_session_persisted": False,
            "session": {
                "session_id": session_id,
                "messages": [
                    {"role": "user", "content": "please recover", "timestamp": 7.0},
                    assistant,
                ],
                "message_count": 2,
            },
        },
        session_dir=session_dir,
    )
    compact = run_journal.read_run_events(session_id, run_id, session_dir=session_dir)
    assert compact["events"][0]["payload"]["session"]["messages"][-1] == assistant

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is True

    data_line = next(
        line for line in handler.wfile.getvalue().decode("utf-8").splitlines() if line.startswith("data: ")
    )
    data = json.loads(data_line.removeprefix("data: "))
    assert data["session"]["session_id"] == session_id
    assert data["session"]["messages"][1] == assistant


def test_replay_run_journal_emits_overcap_unpersisted_continuation_recovery_control(
    tmp_path,
    monkeypatch,
):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    origin_session_id = "save_failure_origin_replay"
    continuation_session_id = "save_failure_continuation_replay"
    run_id = "run_save_failure_huge_replay"
    huge_answer = "x" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000)
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "token",
        {"text": "partial answer"},
        session_dir=session_dir,
    )
    run_journal.append_run_event(
        origin_session_id,
        run_id,
        "apperror",
        {
            "terminal_session_persisted": False,
            "session_id": continuation_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "session": {
                "session_id": continuation_session_id,
                "parent_session_id": origin_session_id,
                "messages": [
                    {"role": "user", "content": "please recover", "timestamp": 7.0},
                    {"role": "assistant", "content": huge_answer, "_ts": 8.0},
                ],
                "message_count": 2,
            },
        },
        session_dir=session_dir,
    )
    compact = run_journal.read_run_events(origin_session_id, run_id, session_dir=session_dir)
    terminal_payload = compact["events"][-1]["payload"]
    assert "messages" not in terminal_payload["session"]
    assert terminal_payload["session"]["session_id"] == continuation_session_id

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is True

    frames = [
        json.loads(line.removeprefix("data: "))
        for line in handler.wfile.getvalue().decode("utf-8").splitlines()
        if line.startswith("data: ")
    ]
    data = frames[-1]
    assert data["session"]["session_id"] == continuation_session_id
    assert data["session"]["parent_session_id"] == origin_session_id
    assert "messages" not in data["session"]
    assert data["terminal_recovery_control"]["session_id"] == origin_session_id
    assert data["terminal_recovery_control"]["target_session_id"] == continuation_session_id
    assert data["terminal_recovery_control"]["continuation_session_id"] == continuation_session_id
    assert data["terminal_recovery_control"]["run_id"] == run_id
    assert data["terminal_recovery_control"]["stream_id"] == run_id
    assert data["terminal_disposition"]["session_id"] == origin_session_id
    assert data["terminal_disposition"]["target_session_id"] == continuation_session_id
    assert data["terminal_disposition"]["continuation_session_id"] == continuation_session_id


def test_replay_run_journal_completes_writer_overcap_unpersisted_terminal_events(
    tmp_path,
    monkeypatch,
):
    import api.models as models
    import api.run_journal as run_journal
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    origin_session_id = "save_failure_origin_writer_replay"
    continuation_session_id = "save_failure_continuation_writer_replay"
    cases = [
        ("done", {}, "completed"),
        ("apperror", {"type": "tool_limit_reached"}, "tool_limit_reached"),
        ("cancel", {"message": "Cancelled by user"}, "interrupted-by-user"),
    ]
    huge_answer = "终" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000)
    run_ids = []
    for event_name, extra_payload, _expected_terminal_state in cases:
        run_id = f"run_save_failure_writer_replay_{event_name}"
        run_ids.append(run_id)
        writer = run_journal.RunJournalWriter(origin_session_id, run_id, session_dir=session_dir)
        writer.append_sse_event("token", {"text": "partial answer"})
        payload = {
            "terminal_session_persisted": False,
            "session_id": continuation_session_id,
            "old_session_id": origin_session_id,
            "new_session_id": continuation_session_id,
            "continuation_session_id": continuation_session_id,
            "session": {
                "session_id": continuation_session_id,
                "parent_session_id": origin_session_id,
                "messages": [
                    {"role": "user", "content": "please recover", "timestamp": 7.0},
                    {"role": "assistant", "content": huge_answer, "_ts": 8.0},
                ],
                "message_count": 2,
            },
        }
        payload.update(extra_payload)
        writer.append_sse_event(event_name, payload)
        path = session_dir / "_run_journal" / origin_session_id / f"{run_id}.jsonl"
        assert path.stat().st_size < run_journal._RUN_EVENTS_MAX_BYTES

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )

    for (event_name, _extra_payload, expected_terminal_state), run_id in zip(cases, run_ids, strict=True):
        handler = SimpleNamespace(wfile=io.BytesIO())

        assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is True

        body = handler.wfile.getvalue().decode("utf-8")
        assert f"event: {event_name}\n" in body
        frames = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        data = frames[-1]
        assert data["session"]["session_id"] == continuation_session_id
        assert data["session"]["parent_session_id"] == origin_session_id
        assert "messages" not in data["session"]
        assert data["terminal_recovery_control"]["session_id"] == origin_session_id
        assert data["terminal_recovery_control"]["target_session_id"] == continuation_session_id
        assert data["terminal_recovery_control"]["terminal_state"] == expected_terminal_state
        assert data["terminal_disposition"]["session_id"] == origin_session_id
        assert data["terminal_disposition"]["target_session_id"] == continuation_session_id


def test_standard_streaming_save_failure_producers_emit_bounded_recovery_rows(
    tmp_path,
    monkeypatch,
):
    import api.models as models
    import api.routes as routes
    import api.run_journal as run_journal
    import api.streaming as streaming

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(streaming, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
    monkeypatch.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )

    huge_answer = "终" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000)
    cases = [
        ("done_save_failure", "apperror", "errored"),
        ("apperror_save_failure", "apperror", "tool_limit_reached"),
        ("cancel_save_failure", "cancel", "interrupted-by-user"),
    ]
    original_session_save = models.Session.save
    save_failure_plans = {}

    def _controlled_session_save(self, *args, **kwargs):
        plan = save_failure_plans.get(getattr(self, "session_id", None))
        if plan is not None:
            plan["count"] += 1
            if plan["count"] > plan["allowed_successes"]:
                raise OSError(f"forced terminal save failure for {plan['case_name']}")
        return original_session_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", _controlled_session_save)

    def _make_fake_agent(bound_case_name, bound_session_id, bound_stream_id):
        class FakeAgent:
            def __init__(
                self,
                *args,
                stream_delta_callback=None,
                **kwargs,
            ):
                self.session_id = bound_session_id
                self.stream_delta_callback = stream_delta_callback
                self.context_compressor = None
                self.session_prompt_tokens = 0
                self.session_completion_tokens = 0
                self.session_estimated_cost_usd = None
                self.session_cache_read_tokens = 0
                self.session_cache_write_tokens = 0
                self.reasoning_config = None
                self.ephemeral_system_prompt = None
                self._last_error = (
                    "maximum number of tool-calling iterations reached"
                    if bound_case_name == "apperror_save_failure"
                    else None
                )

            def run_conversation(self, **kwargs):
                if self.stream_delta_callback:
                    self.stream_delta_callback("终")
                if bound_case_name == "cancel_save_failure":
                    streaming.CANCEL_FLAGS[bound_stream_id].set()
                messages = [
                    {"role": "user", "content": kwargs.get("persist_user_message", ""), "timestamp": 1.0},
                    {"role": "assistant", "content": huge_answer, "timestamp": 2.0},
                ]
                if bound_case_name == "apperror_save_failure":
                    return {
                        "failed": True,
                        "error": "maximum number of tool-calling iterations reached",
                        "messages": messages,
                    }
                return {"messages": messages}

            def interrupt(self, _message):
                return None

        return FakeAgent

    for case_name, expected_event, expected_terminal_state in cases:
        session_id = f"standard_origin_{case_name}"
        stream_id = f"standard_stream_{case_name}"
        session = models.Session(
            session_id=session_id,
            workspace=str(tmp_path),
            model="gpt-4o",
            model_provider="openai",
            messages=[],
            context_messages=[],
        )
        session.active_stream_id = stream_id
        session.pending_user_message = "Do the long task."
        session.pending_started_at = 1.0
        session.save()
        models.SESSIONS[session_id] = session

        save_failure_plans[session_id] = {
            "allowed_successes": 1,
            "case_name": case_name,
            "count": 0,
        }
        event_queue = queue.Queue()
        streaming.STREAMS[stream_id] = event_queue

        FakeAgent = _make_fake_agent(case_name, session_id, stream_id)
        monkeypatch.setattr(streaming, "get_session", lambda _sid, _session=session: _session)
        monkeypatch.setattr(streaming, "_get_ai_agent", lambda _FakeAgent=FakeAgent: _FakeAgent)

        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="Do the long task.",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
            model_provider="openai",
        )

        queued = list(event_queue.queue)
        assert any(item[0] == expected_event for item in queued), queued
        path = session_dir / "_run_journal" / session_id / f"{stream_id}.jsonl"
        assert path.stat().st_size < run_journal._RUN_EVENTS_MAX_BYTES
        journal = run_journal.read_run_events(session_id, stream_id, session_dir=session_dir)
        assert journal["complete"] is True
        terminal_event = journal["events"][-1]
        assert terminal_event["event"] == expected_event
        assert terminal_event["terminal_state"] == expected_terminal_state
        payload = terminal_event["payload"]
        assert payload["terminal_session_persisted"] is False
        assert payload["session"]["session_id"] == session_id
        if "messages" in payload["session"]:
            assert payload["session"]["terminal_recovery_delta"]["reason"] == "terminal_session_save_failed"
            assert payload["session"]["terminal_recovery_delta"]["message_count"] >= 1
        else:
            assert (
                payload["session"]["messages_omitted"]["reason"]
                == "terminal_session_save_failed_payload_too_large"
            )
            assert payload["terminal_recovery_control"] == {
                "version": "terminal_recovery_control_v1",
                "reason": "terminal_session_save_failed_payload_too_large",
                "session_id": session_id,
                "run_id": stream_id,
                "stream_id": stream_id,
                "terminal_state": expected_terminal_state,
            }
            assert payload["terminal_disposition"] == {
                "version": "terminal_disposition_v1",
                "kind": "consumed_non_materializable",
                "reason": "terminal_session_save_failed_payload_too_large",
                "session_id": session_id,
                "run_id": stream_id,
                "stream_id": stream_id,
            }

        handler = SimpleNamespace(wfile=io.BytesIO())
        assert routes._replay_run_journal(handler, stream_id, 0, include_stale=False) is True
        body = handler.wfile.getvalue().decode("utf-8")
        assert f"event: {expected_event}\n" in body
        frames = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]
        replay_payload = frames[-1]
        if "terminal_recovery_control" in replay_payload:
            assert replay_payload["terminal_recovery_control"]["terminal_state"] == expected_terminal_state
        else:
            assert (
                replay_payload["session"]["terminal_recovery_delta"]["reason"]
                == "terminal_session_save_failed"
            )

        streaming.STREAMS.pop(stream_id, None)
        streaming.CANCEL_FLAGS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)


def test_gateway_save_failure_producers_emit_bounded_recovery_rows(
    tmp_path,
    monkeypatch,
):
    import api.gateway_chat as gateway_chat
    import api.models as models
    import api.routes as routes
    import api.run_journal as run_journal

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=session_dir),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=session_dir,
        ),
    )

    huge_answer = "终" * (run_journal._RUN_EVENTS_MAX_BYTES + 10_000)
    cases = [
        ("apperror", {"type": "tool_limit_reached", "message": "tool limit"}, "tool_limit_reached"),
        ("cancel", {"message": "Cancelled by user"}, "interrupted-by-user"),
    ]
    for event_name, event_payload, expected_terminal_state in cases:
        session_id = f"gateway_origin_{event_name}_save_failed"
        stream_id = f"gateway_stream_{event_name}_save_failed"
        session = models.Session(
            session_id=session_id,
            workspace=str(tmp_path),
            model="gpt-4o",
            model_provider="openai",
            messages=[
                {"role": "user", "content": "Do the long task.", "timestamp": 1.0},
                {"role": "assistant", "content": huge_answer, "timestamp": 2.0},
            ],
            context_messages=[],
        )
        session.active_stream_id = stream_id
        session.save()
        models.SESSIONS[session_id] = session

        def _fail_save(*_args, _event_name=event_name, **_kwargs):
            raise OSError(f"forced gateway save failure for {_event_name}")

        session.save = _fail_save
        monkeypatch.setattr(gateway_chat, "get_session", lambda _sid, _session=session: _session)
        payload = gateway_chat._settle_gateway_terminal_event_payload(
            session_id,
            stream_id,
            str(tmp_path),
            "gpt-4o",
            "openai",
            event_name,
            event_payload,
        )
        writer = run_journal.RunJournalWriter(session_id, stream_id, session_dir=session_dir)
        writer.append_sse_event(event_name, payload)

        path = session_dir / "_run_journal" / session_id / f"{stream_id}.jsonl"
        assert path.stat().st_size < run_journal._RUN_EVENTS_MAX_BYTES
        journal = run_journal.read_run_events(session_id, stream_id, session_dir=session_dir)
        assert journal["complete"] is True
        payload = journal["events"][-1]["payload"]
        assert payload["terminal_session_persisted"] is False
        assert payload["session"]["session_id"] == session_id
        assert journal["events"][-1]["terminal_state"] == expected_terminal_state
        if "messages" in payload["session"]:
            assert payload["session"]["terminal_recovery_delta"]["reason"] == "terminal_session_save_failed"
            assert payload["session"]["terminal_recovery_delta"]["message_count"] >= 1
        else:
            assert (
                payload["session"]["messages_omitted"]["reason"]
                == "terminal_session_save_failed_payload_too_large"
            )
            assert payload["terminal_recovery_control"]["session_id"] == session_id
            assert payload["terminal_recovery_control"]["terminal_state"] == expected_terminal_state
            assert payload["terminal_disposition"]["session_id"] == session_id

        handler = SimpleNamespace(wfile=io.BytesIO())
        assert routes._replay_run_journal(handler, stream_id, 0, include_stale=False) is True
        body = handler.wfile.getvalue().decode("utf-8")
        assert f"event: {event_name}\n" in body


def test_replay_run_journal_reads_suffix_after_default_row_cap(tmp_path, monkeypatch):
    import api.run_journal as run_journal
    import api.routes as routes

    session_id = "session_1"
    run_id = "run_long"
    for seq in range(1, 2049):
        run_journal.append_run_event(session_id, run_id, "token", {"text": str(seq)}, session_dir=tmp_path, seq=seq)
    run_journal.append_run_event(session_id, run_id, "token", {"text": "suffix"}, session_dir=tmp_path, seq=2049)
    run_journal.append_run_event(session_id, run_id, "done", {"session": {}}, session_dir=tmp_path, seq=2050)

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=tmp_path,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 2048, include_stale=False) is True

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_long:2048\n" not in body
    assert "id: run_long:2049\n" in body
    assert "event: token\n" in body
    assert "id: run_long:2050\n" in body
    assert "event: done\n" in body


def test_replay_run_journal_returns_complete_after_paged_boundary(monkeypatch):
    import api.routes as routes

    calls = []
    handler = SimpleNamespace(wfile=io.BytesIO())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": True,
        },
    )

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        calls.append(after_seq)
        if after_seq == 0:
            return {
                "events": [
                    {"event": "token", "payload": {"text": "a"}, "event_id": f"{run_id}:1", "seq": 1},
                    {"event": "token", "payload": {"text": "b"}, "event_id": f"{run_id}:2", "seq": 2},
                ],
                "malformed": [{"line": 3, "reason": "replay_limit_rows"}],
                "complete": False,
                "limit_reason": "replay_limit_rows",
                "next_after_seq": 2,
            }
        return {
            "events": [
                {"event": "done", "payload": {"session": {"session_id": session_id}}, "event_id": f"{run_id}:3", "seq": 3}
            ],
            "malformed": [],
            "complete": True,
            "limit_reason": None,
            "next_after_seq": 3,
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)

    assert routes._replay_run_journal(handler, "run_1", 0, include_stale=False) is True
    assert calls == [0, 2]


def test_replay_run_journal_keeps_writer_boundary_reorder_exact_once(tmp_path, monkeypatch):
    import api.run_journal as run_journal
    import api.routes as routes

    session_id = "session_boundary"
    run_id = "run_boundary"
    writer = run_journal.RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    for seq in range(1, 2048):
        writer.append_sse_event("token", {"text": str(seq)})

    real_append = run_journal.append_run_event
    first_append_ready = threading.Event()
    release_first_append = threading.Event()
    first_append_blocked = {"value": False}

    def interleaved_append(session_id, run_id, event_name, payload=None, **kwargs):
        if payload == {"text": "delayed-2048"} and not first_append_blocked["value"]:
            first_append_blocked["value"] = True
            first_append_ready.set()
            assert release_first_append.wait(timeout=5)
        return real_append(session_id, run_id, event_name, payload, **kwargs)

    monkeypatch.setattr(run_journal, "append_run_event", interleaved_append)
    errors = []

    def append_delayed():
        try:
            writer.append_sse_event("token", {"text": "delayed-2048"})
        except Exception as exc:  # pragma: no cover - failure reported below.
            errors.append(exc)

    delayed_thread = threading.Thread(target=append_delayed)
    delayed_thread.start()
    assert first_append_ready.wait(timeout=5)
    writer.append_sse_event("token", {"text": "fast-2049"})
    release_first_append.set()
    delayed_thread.join(timeout=5)

    assert not delayed_thread.is_alive()
    assert errors == []
    assert first_append_blocked["value"] is True

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: run_journal.find_run_summary(stream_id, session_dir=tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: run_journal.read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            max_seq=max_seq,
            session_dir=tmp_path,
        ),
    )
    handler = SimpleNamespace(wfile=io.BytesIO())

    assert routes._replay_run_journal(handler, run_id, 0, include_stale=False) is True

    replayed_ids = [
        line.removeprefix("id: ")
        for line in handler.wfile.getvalue().decode("utf-8").splitlines()
        if line.startswith("id: ")
    ]
    assert replayed_ids == [f"{run_id}:{seq}" for seq in range(1, 2050)]


def test_active_stream_replay_keeps_items_for_new_run_with_same_seq_range(monkeypatch):
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "fresh"}, "run_new:1"))
            self.q.put_nowait(("stream_end", {}, "run_new:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {
                "last_event_id": "run_old:3",
                "offline_buffered_events": 2,
            }

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    stream = FakeStream()
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_2",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None: {"events": []},
    )
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_args, **_kwargs: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_new"] = stream
    try:
        routes._handle_sse_stream(
            handler,
            urlparse("/api/chat/stream?stream_id=run_new&replay=1&after_seq=0"),
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_new:1\n" in body
    assert "id: run_new:2\n" in body
    assert body.count("id: run_new:1\n") == 1
    assert stream.unsubscribed is True
