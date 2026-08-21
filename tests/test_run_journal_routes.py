from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
import io
import json
import queue


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SRC = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_gateway_terminal_error_save_failure_is_marked_unsaved(monkeypatch, tmp_path):
    import api.gateway_chat as gateway_chat
    import api.models as models
    import api.streaming as streaming

    session = models.Session(
        session_id="gateway_terminal_error_save_failed",
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        messages=[{"role": "user", "content": "prompt"}],
        context_messages=[],
    )
    session.active_stream_id = "gateway_terminal_error_stream"

    def fail_save(*_args, **_kwargs):
        raise OSError("forced gateway terminal error save failure")

    session.save = fail_save
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(gateway_chat, "_stream_writeback_is_current", lambda *_args: True)
    monkeypatch.setattr(streaming, "_snapshot_and_append_partial_on_error", lambda *_args: None)

    payload = gateway_chat._settle_gateway_terminal_error(
        session.session_id,
        session.active_stream_id,
        str(tmp_path),
        "gpt-4o",
        "openai",
        "gateway exploded",
    )

    assert payload["terminal_session_persisted"] is False
    assert "terminal_session_persisted_session_id" not in payload


def test_gateway_terminal_error_successful_save_is_marked_persisted(monkeypatch, tmp_path):
    import api.gateway_chat as gateway_chat
    import api.models as models
    import api.streaming as streaming

    session = models.Session(
        session_id="gateway_terminal_error_save_succeeds",
        workspace=str(tmp_path),
        model="gpt-4o",
        model_provider="openai",
        messages=[{"role": "user", "content": "prompt"}],
        context_messages=[],
    )
    session.active_stream_id = "gateway_terminal_error_stream"
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(gateway_chat, "_stream_writeback_is_current", lambda *_args: True)
    monkeypatch.setattr(streaming, "_snapshot_and_append_partial_on_error", lambda *_args: None)

    payload = gateway_chat._settle_gateway_terminal_error(
        session.session_id,
        session.active_stream_id,
        str(tmp_path),
        "gpt-4o",
        "openai",
        "gateway exploded",
    )

    assert payload["terminal_session_persisted"] is True
    assert payload["terminal_session_persisted_session_id"] == session.session_id


def test_stream_status_exposes_replay_summary():
    status_pos = ROUTES_SRC.index('parsed.path == "/api/chat/stream/status"')
    block = ROUTES_SRC[status_pos : status_pos + 900]

    assert "find_run_summary(stream_id)" in block
    assert '"replay_available"' in block
    assert '"journal"' in block
    assert "_run_journal_status_payload" in block


def test_dead_stream_sse_replays_journal_before_404_fallback():
    handler_pos = ROUTES_SRC.index("def _handle_sse_stream")
    block = ROUTES_SRC[handler_pos : handler_pos + 5400]

    assert "find_run_summary(stream_id)" in block
    assert "stream not found" in block
    assert "_replay_run_journal" in block
    assert "_chat_stream_resume_cursor" in block
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
    block = ROUTES_SRC[replay_pos : replay_pos + 1200]

    assert "read_run_events" in block
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


def test_runtime_snapshot_transport_projection_dedupes_live_tool_payloads_without_mutation():
    import api.routes as routes

    repeated = "x" * 4000
    snapshot = {
        "messages": [{"role": "assistant", "content": "progress", "_live": True, "_ts": 1234.5}],
        "last_assistant_text": "progress",
        "last_reasoning_text": "",
        "tool_calls": [{
            "name": "terminal",
            "tid": "call-1",
            "args": {"command": "pytest"},
            "preview": repeated,
            "snippet": repeated,
            "done": True,
        }],
        "anchor_activity_scene": {
            "version": "activity_scene_v1",
            "identity": {"session_id": "session-1", "stream_id": "stream-1", "run_id": "run-1"},
            "activity_rows": [{
                "row_id": "tool:call-1:0",
                "local_id": "call-1",
                "order_index": 0,
                "kind": "tool_completed",
                "role": "tool",
                "display_hint": "tool_row",
                "display_hints": {"compact_worklog": "tool_row"},
                "source_event_type": "tool_complete",
                "event_id": None,
                "run_id": "run-1",
                "stream_id": "stream-1",
                "seq": None,
                "status": "completed",
                "created_at": 1.0,
                "identity": {"local_id": "call-1", "run_id": "run-1", "stream_id": "stream-1"},
                "group": {"group_key": "activity:0"},
                "text": repeated,
                "thinking": None,
                "tool_call_id": "call-1",
                "tool": {
                    "id": "call-1", "tid": "call-1", "name": "terminal",
                    "args": {"command": "pytest"},
                    "preview": repeated, "snippet": repeated,
                    "done": True, "is_error": False,
                },
                "payload": {
                    "name": "terminal", "args": {"command": "pytest"},
                    "preview": repeated, "snippet": repeated,
                    "tid": "call-1", "id": "call-1",
                },
            }],
        },
    }
    original = json.loads(json.dumps(snapshot))

    projected = routes._runtime_journal_snapshot_for_session_payload(snapshot)
    row = projected["anchor_activity_scene"]["activity_rows"][0]

    assert snapshot == original
    assert projected["messages"] == []
    assert projected["last_assistant_text"] == "progress"
    assert projected["last_message_ts"] == 1234.5
    assert projected["tool_calls"] == [{
        "name": "terminal",
        "tid": "call-1",
        "args": {"command": "pytest"},
        "snippet": repeated,
        "done": True,
    }]
    assert row["tool"]["args"] == {"command": "pytest"}
    assert row["tool"]["snippet"] == repeated
    assert "preview" not in row["tool"]
    assert "payload" not in row
    assert "text" not in row
    assert row["tool_call_id"] == "call-1"
    assert len(json.dumps(projected)) < len(json.dumps(snapshot)) * 0.5


def test_runtime_snapshot_transport_projection_keeps_tool_fallback_without_scene():
    import api.routes as routes

    snapshot = {
        "messages": [],
        "last_assistant_text": "",
        "last_reasoning_text": "",
        "tool_calls": [{
            "name": "terminal",
            "tid": "call-1",
            "preview": "same result",
            "snippet": "same result",
            "args": {"command": "pytest"},
        }],
    }

    projected = routes._runtime_journal_snapshot_for_session_payload(snapshot)

    assert projected["tool_calls"] == [{
        "name": "terminal",
        "tid": "call-1",
        "snippet": "same result",
        "args": {"command": "pytest"},
    }]
    assert snapshot["tool_calls"][0]["preview"] == "same result"


def test_paginated_session_followup_does_not_repeat_runtime_snapshot():
    from tests.test_session_tail_payload import _FakeSession, _invoke

    stream_id = "stream-paginated-snapshot"
    session = _FakeSession([
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ])
    session.active_stream_id = stream_id
    snapshot = {
        "stream_id": stream_id,
        "last_seq": 2,
        "last_event_id": f"{stream_id}:2",
        "messages": [{"role": "assistant", "content": "live progress", "_live": True}],
        "last_assistant_text": "live progress",
        "last_reasoning_text": "",
        "tool_calls": [],
        "anchor_activity_scene": {
            "version": "activity_scene_v1",
            "identity": {"session_id": session.session_id, "stream_id": stream_id, "run_id": stream_id},
            "activity_rows": [{
                "row_id": "prose-1", "local_id": "prose-1",
                "kind": "process_prose", "role": "prose",
                "source_event_type": "token", "status": "running", "text": "live progress",
            }],
        },
    }
    projected = {
        **snapshot,
        "messages": [],
    }

    with patch("api.routes._active_stream_ids", return_value={stream_id}), \
         patch("api.routes.find_run_summary", return_value={
             "session_id": session.session_id,
             "run_id": stream_id,
             "last_seq": 2,
             "last_event_id": f"{stream_id}:2",
             "terminal": False,
         }), \
         patch("api.routes._run_journal_live_snapshot", return_value=snapshot):
        full = _invoke(
            session,
            query=f"session_id={session.session_id}&messages=1&resolve_model=0",
        )
        paginated = _invoke(
            session,
            query=f"session_id={session.session_id}&messages=1&resolve_model=0&msg_limit=1",
        )

    assert full["runtime_journal_snapshot"] == projected
    assert "runtime_journal_snapshot" not in paginated
    assert paginated["runtime_journal"]["last_seq"] == 2
    assert paginated["runtime_journal"]["terminal"] is False


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


# ── Last-Event-ID fallback resume cursor on /api/chat/stream ────────────────
# The stream emits `id: stream_id:seq` on every journaled event, so any
# spec-compliant SSE client (browser EventSource auto-reconnect, Android/CLI
# clients) sends `Last-Event-ID` automatically on reconnect. The handler must
# honor it when no explicit query cursor is present instead of replaying from
# seq 0 and double-rendering the transcript.


class _HeaderHandler:
    """Minimal request handler double carrying headers + a writable wfile."""

    def __init__(self, last_event_id=None):
        self.wfile = io.BytesIO()
        self.headers = (
            {"Last-Event-ID": last_event_id} if last_event_id is not None else {}
        )

    def send_response(self, _code):
        pass

    def send_header(self, _name, _value):
        pass

    def end_headers(self):
        pass


def test_chat_stream_resume_cursor_prefers_query_params_over_header():
    """Explicit after_event_id/after_seq always win over Last-Event-ID."""
    import api.routes as routes

    handler = _HeaderHandler(last_event_id="run_1:9")
    qs = {"after_event_id": ["run_1:4"]}
    # (after_seq, requested, raw_cursor, runner_cursor)
    assert routes._chat_stream_resume_cursor(handler, qs, "run_1") == (4, True, "run_1:4", "run_1:4")

    qs = {"after_seq": ["7"]}
    assert routes._chat_stream_resume_cursor(handler, qs, "run_1") == (7, True, None, "7")


def test_chat_stream_resume_cursor_explicit_unparseable_query_blocks_header():
    """A supplied-but-unparseable explicit query cursor must NOT fall through to
    the Last-Event-ID header — precedence is by query-param PRESENCE, not
    successful parsing (Codex CORE #1). The header can never override an
    explicit cursor and silently skip events."""
    import api.routes as routes

    # Foreign-run after_event_id parses to None for this stream, but its
    # presence must still block the (parseable) header.
    handler = _HeaderHandler(last_event_id="run_1:9")
    qs = {"after_event_id": ["run_other:2"]}
    after_seq, requested, raw, runner = routes._chat_stream_resume_cursor(handler, qs, "run_1")
    assert after_seq is None
    assert requested is True  # asked to resume → replay-from-start downstream
    assert raw == "run_other:2"  # explicit query cursor surfaced, header ignored
    # No valid paired seq and a foreign journal run → no runner cursor (full replay).
    assert runner is None


def test_chat_stream_resume_cursor_reads_last_event_id_header():
    """With no query cursor, Last-Event-ID is the resume position."""
    import api.routes as routes

    handler = _HeaderHandler(last_event_id="run_1:3")
    assert routes._chat_stream_resume_cursor(handler, {}, "run_1") == (3, True, "run_1:3", "run_1:3")


def test_chat_stream_resume_cursor_foreign_run_header_is_requested_but_unusable():
    """A Last-Event-ID for a different run id parses out as unusable, but the
    resume was still REQUESTED — presence survives so the caller replays from
    start instead of skipping the journal (Codex CORE #2)."""
    import api.routes as routes

    handler = _HeaderHandler(last_event_id="run_other:5")
    after_seq, requested, raw, runner = routes._chat_stream_resume_cursor(handler, {}, "run_1")
    assert after_seq is None
    assert requested is True
    assert raw == "run_other:5"
    # The runner keys cursors per run independently, so the opaque header value
    # is preserved for the runner even though the journal path rejects it.
    assert runner == "run_other:5"


def test_chat_stream_resume_cursor_absent_without_any_cursor():
    """No query cursor and no header → (None, False): fresh attach, no replay."""
    import api.routes as routes

    handler = _HeaderHandler()
    assert routes._chat_stream_resume_cursor(handler, {}, "run_1") == (None, False, None, None)
    # Malformed header values are unusable but still a resume request. A
    # colon-less opaque value is preserved for the runner (runner ids need not
    # be journal-shaped); a colon value that fails int() parsing is not.
    handler = _HeaderHandler(last_event_id="not-a-cursor")
    assert routes._chat_stream_resume_cursor(handler, {}, "run_1") == (None, True, "not-a-cursor", "not-a-cursor")


def test_dead_stream_replay_uses_last_event_id_header(monkeypatch):
    """Dead-stream path: reconnect with only Last-Event-ID resumes mid-journal."""
    import api.routes as routes

    captured = {}
    handler = _HeaderHandler(last_event_id="run_1:2")
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": True,
            "last_seq": 4,
        },
    )

    journal = [
        {"event": "token", "payload": {"text": "j1"}, "event_id": "run_1:1", "seq": 1},
        {"event": "token", "payload": {"text": "j2"}, "event_id": "run_1:2", "seq": 2},
        {"event": "token", "payload": {"text": "j3"}, "event_id": "run_1:3", "seq": 3},
        {"event": "done", "payload": {"session": {"session_id": "session_1"}}, "event_id": "run_1:4", "seq": 4},
    ]

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        captured["after_seq"] = after_seq
        return {
            "events": [
                e for e in journal
                if after_seq is None or int(e["seq"]) > int(after_seq)
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_a, **_k: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    try:
        routes._handle_sse_stream(
            handler, urlparse("/api/chat/stream?stream_id=run_1")
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    assert captured["after_seq"] == 2
    body = handler.wfile.getvalue().decode("utf-8")
    # Resume after seq 2: only seq 3 and 4 are emitted, not 1-2.
    assert "id: run_1:1\n" not in body
    assert "id: run_1:2\n" not in body
    assert "id: run_1:3\n" in body
    assert "id: run_1:4\n" in body
    assert "event: done\n" in body


def test_live_stream_replay_uses_last_event_id_header(monkeypatch):
    """Live path: header cursor drives the replay gap-check and dedup cutoff."""
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "tail"}, "run_1:4"))
            self.q.put_nowait(("stream_end", {}, "run_1:5"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {
                "last_event_id": "run_1:4",
                "offline_buffered_events": 2,
                "offline_first_event_id": "run_1:4",
            }

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    captured = {}
    handler = _HeaderHandler(last_event_id="run_1:2")
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

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        captured["after_seq"] = after_seq
        return {
            "events": [
                {
                    "event": "token",
                    "payload": {"text": "bridged"},
                    "event_id": f"{run_id}:3",
                    "seq": 3,
                }
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_a, **_k: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_1"] = stream
    try:
        routes._handle_sse_stream(
            handler, urlparse("/api/chat/stream?stream_id=run_1")
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    # Journal replay bridged (cursor → buffered tail) using the header cursor.
    assert captured["after_seq"] == 2
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:3\n" in body
    assert "id: run_1:4\n" in body
    assert "id: run_1:5\n" in body
    assert stream.unsubscribed is True


def test_live_stream_invalid_cursor_replays_from_start_not_skips(monkeypatch):
    """A foreign/malformed reconnect cursor (asked to resume, can't honor it)
    must replay the journal from START and still drain the tail — never
    silently skip the whole journal (Codex CORE #2)."""
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "tail"}, "run_1:3"))
            self.q.put_nowait(("stream_end", {}, "run_1:4"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {
                "last_event_id": "run_1:3",
                "offline_buffered_events": 2,
                "offline_first_event_id": "run_1:3",
            }

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    captured = {}
    # Foreign-run header cursor: parses unusable for run_1, but requested=True.
    handler = _HeaderHandler(last_event_id="run_other:9")
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

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        captured["after_seq"] = after_seq
        captured["max_seq"] = max_seq
        return {
            "events": [
                {"event": "token", "payload": {"text": "j1"}, "event_id": f"{run_id}:1", "seq": 1},
                {"event": "token", "payload": {"text": "j2"}, "event_id": f"{run_id}:2", "seq": 2},
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_a, **_k: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_1"] = stream
    try:
        routes._handle_sse_stream(
            handler, urlparse("/api/chat/stream?stream_id=run_1")
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    # Normalized to replay-from-start: the journal bridge covers 1..first-1.
    assert captured["after_seq"] == 0
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:1\n" in body
    assert "id: run_1:2\n" in body
    # Buffered tail still drained, terminal survives.
    assert "id: run_1:3\n" in body
    assert "id: run_1:4\n" in body
    assert stream.unsubscribed is True


def test_live_stream_cursor_equal_to_cutoff_does_not_double_send(monkeypatch):
    """A valid cursor EQUAL to the snapshot cutoff must enter the drain dedup
    bound (equality is in-range, NOT ahead). The buffered copy of the event at
    the cursor was already delivered to this client, so it must be filtered —
    only the not-yet-seen frames after it are drained (Codex r2 #1 off-by-one).
    """
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            # Retained tail: client already holds through seq 3; seq 4 is new.
            self.q.put_nowait(("token", {"text": "f2"}, "run_1:2"))
            self.q.put_nowait(("token", {"text": "f3"}, "run_1:3"))
            self.q.put_nowait(("stream_end", {}, "run_1:4"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {
                "last_event_id": "run_1:3",
                "offline_buffered_events": 3,
                "offline_first_event_id": "run_1:2",
            }

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    captured = {}
    handler = _HeaderHandler(last_event_id="run_1:3")  # cursor == snapshot cutoff
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

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        captured["after_seq"] = after_seq
        captured["max_seq"] = max_seq
        return {"events": []}

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_a, **_k: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_1"] = stream
    try:
        routes._handle_sse_stream(
            handler, urlparse("/api/chat/stream?stream_id=run_1")
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    # The events at/below the cursor (seqs 2 and 3) were already delivered —
    # they must be filtered out, NOT re-sent. Only the new terminal frame (4)
    # is drained.
    assert "id: run_1:2\n" not in body
    assert "id: run_1:3\n" not in body
    assert "id: run_1:4\n" in body
    assert stream.unsubscribed is True


def test_live_stream_ahead_cursor_unknown_cutoff_still_delivers(monkeypatch):
    """An ahead-of-stream Last-Event-ID with an UNKNOWN snapshot cutoff
    (no parseable last_event_id) must not become the live dedup bound.

    Before the fix, header run_1:999 + snapshot_cutoff_seq=None installed 999
    as the drain filter bound, so every queued frame — including the terminal
    stream_end fence — was discarded and the reconnect stalled on heartbeats
    with an empty SSE body (Codex r4 data-loss). The unknown cutoff is treated
    as fence 0, normalizing the cursor to replay-from-start so both queued
    events are delivered exactly once and the subscriber is released.
    """
    import api.routes as routes

    class FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "t1"}, "run_1:1"))
            self.q.put_nowait(("stream_end", {}, "run_1:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            # No last_event_id / first_event_id: cutoff is UNKNOWN (None).
            return self.q, {"offline_buffered_events": 2}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    handler = _HeaderHandler(last_event_id="run_1:999")
    stream = FakeStream()
    monkeypatch.setattr(routes, "find_run_summary", lambda _sid: None)
    monkeypatch.setattr(
        routes, "read_run_events", lambda *a, **k: {"events": []}
    )
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_a, **_k: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    routes.STREAMS["run_1"] = stream
    try:
        routes._handle_sse_stream(
            handler, urlparse("/api/chat/stream?stream_id=run_1")
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    body = handler.wfile.getvalue().decode("utf-8")
    # Both events emit exactly once — including the terminal fence.
    assert body.count("id: run_1:1\n") == 1
    assert body.count("id: run_1:2\n") == 1
    assert body.count("event: stream_end\n") == 1
    assert stream.unsubscribed is True


def test_dead_stream_ahead_of_stream_cursor_returns_full_replay(monkeypatch):
    """A cursor AHEAD of the dead stream's authoritative last_seq is normalized
    to replay-from-start, so the journal's real events are emitted instead of
    an empty SSE body (Codex r2 #2 dead-stream half)."""
    import api.routes as routes

    captured = {}
    handler = _HeaderHandler(last_event_id="run_1:999")
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": True,
            "last_seq": 2,
        },
    )

    journal = [
        {"event": "token", "payload": {"text": "hello"}, "event_id": "run_1:1", "seq": 1},
        {"event": "done", "payload": {"session": {"session_id": "session_1"}}, "event_id": "run_1:2", "seq": 2},
    ]

    def fake_read_run_events(session_id, run_id, after_seq=None, max_seq=None):
        captured["after_seq"] = after_seq
        return {
            "events": [
                e for e in journal
                if after_seq is None or int(e["seq"]) > int(after_seq)
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)
    monkeypatch.setattr(routes, "stale_interrupted_event", lambda *_a, **_k: None)
    previous_streams = dict(routes.STREAMS)
    routes.STREAMS.clear()
    try:
        routes._handle_sse_stream(
            handler, urlparse("/api/chat/stream?stream_id=run_1")
        )
    finally:
        routes.STREAMS.clear()
        routes.STREAMS.update(previous_streams)

    # 999 > last_seq (2) → normalized to replay-from-start, and the replay body
    # actually carries the journal's events (not an empty response).
    assert captured["after_seq"] == 0
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:1\n" in body
    assert "id: run_1:2\n" in body
    assert "event: done\n" in body


def test_runner_observe_reconnect_uses_last_event_id_header(monkeypatch):
    """Runner-local path: header-only reconnect carries Last-Event-ID through
    to observe_run so the runner resumes instead of duplicating from the start
    (Codex CORE #3)."""
    import api.routes as routes

    calls = []

    class FakeRunnerClient:
        def observe_run(self, run_id, *, cursor=None):
            calls.append((run_id, cursor))
            return {
                "run_id": run_id,
                "cursor": "7",
                "events": [
                    {"event": "message", "payload": {"content": "hi"}, "event_id": "run-1:6"},
                    {"event": "stream_end", "payload": {"ok": True}, "event_id": "run-1:7"},
                ],
            }

    class Handler:
        def __init__(self, last_event_id=None):
            self.wfile = io.BytesIO()
            self.headers = (
                {"Last-Event-ID": last_event_id} if last_event_id is not None else {}
            )

        def send_response(self, _code):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: FakeRunnerClient())
    handler = Handler(last_event_id="run-1:5")
    try:
        assert routes._handle_sse_stream(
            handler, urlparse("/api/chat/stream?stream_id=run-1")
        ) is True
    finally:
        monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)

    # The header cursor is carried through to the runner, not dropped.
    assert calls == [("run-1", "run-1:5")]
    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: stream_end" in body


class _RunnerProbeHandler:
    def __init__(self, last_event_id=None):
        self.wfile = io.BytesIO()
        self.headers = (
            {"Last-Event-ID": last_event_id} if last_event_id is not None else {}
        )

    def send_response(self, _code):
        pass

    def send_header(self, _name, _value):
        pass

    def end_headers(self):
        pass


def _run_runner_probe(monkeypatch, url, last_event_id=None):
    """Drive _handle_sse_stream down the runner-observe path, returning the
    (run_id, cursor) the runner's observe_run was called with."""
    import api.routes as routes

    calls = []

    class FakeRunnerClient:
        def observe_run(self, run_id, *, cursor=None):
            calls.append((run_id, cursor))
            return {
                "run_id": run_id,
                "cursor": "7",
                "events": [
                    {"event": "stream_end", "payload": {"ok": True}, "event_id": f"{run_id}:7"},
                ],
            }

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: FakeRunnerClient())
    handler = _RunnerProbeHandler(last_event_id=last_event_id)
    try:
        routes._handle_sse_stream(handler, urlparse(url))
    finally:
        monkeypatch.delenv("HERMES_WEBUI_RUNTIME_ADAPTER", raising=False)
    return calls


def test_runner_malformed_explicit_cursor_blocks_header(monkeypatch):
    """Runner path: an explicit but malformed after_event_id must NOT let the
    Last-Event-ID header override it — the runner gets NO cursor (full replay),
    not the header value (Codex r2 #3 probe)."""
    calls = _run_runner_probe(
        monkeypatch,
        "/api/chat/stream?stream_id=run_1&after_event_id=malformed",
        last_event_id="run_1:9",
    )
    assert calls == [("run_1", None)]


def test_runner_foreign_cursor_passes_no_cursor_for_full_replay(monkeypatch):
    """Runner path: a foreign after_event_id (foreign:2) is unusable for this
    run — the runner must get NO cursor (full replay), never the leaked seq
    '2' that would skip runner events (Codex r2 #3 probe)."""
    calls = _run_runner_probe(
        monkeypatch,
        "/api/chat/stream?stream_id=run_1&after_event_id=foreign:2",
    )
    assert calls == [("run_1", None)]


def test_runner_explicit_opaque_cursor_wins(monkeypatch):
    """Runner path: an explicit opaque ``cursor=`` query param keeps precedence
    over both after_* params and the Last-Event-ID header (the runner-local
    contract)."""
    calls = _run_runner_probe(
        monkeypatch,
        "/api/chat/stream?stream_id=run_1&cursor=opaque-xyz",
        last_event_id="run_1:5",
    )
    assert calls == [("run_1", "opaque-xyz")]


def test_runner_paired_opaque_event_id_resumes(monkeypatch):
    """Runner path: an opaque runner event id (event:2) PAIRED with a valid
    after_seq resumes at that seq — never None (full replay), which would
    duplicate tokens/tool events (Codex r3 probe 1)."""
    calls = _run_runner_probe(
        monkeypatch,
        "/api/chat/stream?stream_id=run_1&after_event_id=event:2&after_seq=2",
    )
    assert calls == [("run_1", "2")]


def test_runner_header_only_opaque_event_id_resumes(monkeypatch):
    """Runner path: a header-only opaque runner event id (Last-Event-ID:
    event:2) resumes from it — never None (full replay / duplicates)
    (Codex r3 probe 2)."""
    calls = _run_runner_probe(
        monkeypatch,
        "/api/chat/stream?stream_id=run_1",
        last_event_id="event:2",
    )
    assert calls == [("run_1", "event:2")]


def test_runner_malformed_explicit_with_valid_seq_uses_seq(monkeypatch):
    """Runner path: a malformed after_event_id PAIRED with a valid after_seq
    resumes at the seq, not the malformed raw value (Codex r3 probe 2
    counterpart)."""
    calls = _run_runner_probe(
        monkeypatch,
        "/api/chat/stream?stream_id=run_1&after_event_id=malformed&after_seq=5",
    )
    assert calls == [("run_1", "5")]
