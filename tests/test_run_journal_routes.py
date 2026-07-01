from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import io
import queue


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SRC = (ROOT / "api" / "routes.py").read_text()


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
    assert len(tool["args"]["extra"]) <= 123


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
