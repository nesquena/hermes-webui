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
    assert "_run_journal_live_snapshot(" in ROUTES_SRC
    assert "session_id=sid" in ROUTES_SRC
    assert "workspace_id=canonical_workspace_identity" in ROUTES_SRC
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


def test_live_journal_snapshot_uses_session_scoped_bounded_read(monkeypatch):
    import api.routes as routes

    captured = {}

    def bounded_read(session_id, run_id, **kwargs):
        captured["session_id"] = session_id
        captured["run_id"] = run_id
        captured["kwargs"] = kwargs
        return {
            "events": [
                {
                    "seq": 1,
                    "event": "token",
                    "event_id": f"{run_id}:1",
                    "payload": {"text": "bounded"},
                }
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", bounded_read)

    snapshot = routes._run_journal_live_snapshot("run_scoped", session_id="session_scoped")

    assert snapshot["session_id"] == "session_scoped"
    assert captured["session_id"] == "session_scoped"
    assert captured["run_id"] == "run_scoped"
    assert captured["kwargs"]["max_bytes"] == routes._ANCHOR_JOURNAL_SNAPSHOT_MAX_BYTES
    assert captured["kwargs"]["max_rows"] == routes._ANCHOR_JOURNAL_SNAPSHOT_MAX_ROWS


def test_live_journal_snapshot_fails_closed_on_truncated_evidence(monkeypatch):
    import api.routes as routes

    workspace_id = "a" * 64
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, **kwargs: {
            "truncated": True,
            "events": [
                {
                    "seq": 1,
                    "event": "artifact_reference",
                    "event_id": f"{run_id}:1",
                    "payload": {
                        "kind": "workspace_file",
                        "path": "reports/partial.md",
                        "source_tool": "write_file",
                        "workspace_id": workspace_id,
                    },
                }
            ],
        },
    )

    snapshot = routes._run_journal_live_snapshot(
        "run_partial",
        session_id="session_partial",
        workspace_id=workspace_id,
    )

    assert snapshot["journal_truncated"] is True
    assert snapshot["anchor_activity_scene"]["artifacts"] == []


def test_live_journal_snapshot_drops_foreign_workspace_artifact_but_keeps_prose(
    monkeypatch,
    tmp_path,
):
    import api.routes as routes
    from api.artifact_references import canonical_workspace_identity

    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    workspace_a_id = canonical_workspace_identity(workspace_a)
    workspace_b_id = canonical_workspace_identity(workspace_b)
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, **kwargs: {
            "events": [
                {
                    "seq": 1,
                    "event": "token",
                    "event_id": f"{run_id}:1",
                    "payload": {"text": "prose from session B"},
                },
                {
                    "seq": 2,
                    "event": "artifact_reference",
                    "event_id": f"{run_id}:2",
                    "payload": {
                        "kind": "workspace_file",
                        "path": "reports/from-a.md",
                        "source_tool": "write_file",
                        "workspace_id": workspace_a_id,
                    },
                },
            ]
        },
    )

    snapshot = routes._run_journal_live_snapshot(
        "run-session-b",
        session_id="session-b",
        workspace_id=workspace_b_id,
    )

    assert snapshot["last_assistant_text"] == "prose from session B"
    assert snapshot["anchor_activity_scene"]["artifacts"] == []
    assert any(
        row.get("role") == "prose" and row.get("text") == "prose from session B"
        for row in snapshot["anchor_activity_scene"]["activity_rows"]
    )


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
