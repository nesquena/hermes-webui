import io
import threading
from types import SimpleNamespace

import api.models as models
from api.config import STREAMS, STREAMS_LOCK, create_stream_channel
from api.run_journal import append_run_event
from api.routes import _handle_sse_stream


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        return None


def test_stream_channel_broadcasts_each_event_to_every_subscriber():
    stream = create_stream_channel()
    q1 = stream.subscribe()
    q2 = stream.subscribe()

    try:
        stream.put_nowait(("token", {"text": "H"}))
        stream.put_nowait(("token", {"text": "allo"}))
        stream.put_nowait(("stream_end", {"status": "done"}))

        assert q1.get(timeout=1) == ("token", {"text": "H"})
        assert q1.get(timeout=1) == ("token", {"text": "allo"})
        assert q1.get(timeout=1) == ("stream_end", {"status": "done"})

        assert q2.get(timeout=1) == ("token", {"text": "H"})
        assert q2.get(timeout=1) == ("token", {"text": "allo"})
        assert q2.get(timeout=1) == ("stream_end", {"status": "done"})
    finally:
        stream.unsubscribe(q1)
        stream.unsubscribe(q2)


def test_same_stream_in_two_tabs_receives_identical_token_sequence():
    stream_id = "multitab-stream"
    stream = create_stream_channel()
    with STREAMS_LOCK:
        STREAMS[stream_id] = stream

    handlers = [_FakeHandler(), _FakeHandler()]
    threads = [
        threading.Thread(
            target=_handle_sse_stream,
            args=(handler, SimpleNamespace(query=f"stream_id={stream_id}")),
            daemon=True,
        )
        for handler in handlers
    ]

    try:
        for thread in threads:
            thread.start()

        stream.put_nowait(("token", {"text": "H"}))
        stream.put_nowait(("token", {"text": "allo"}))
        stream.put_nowait(("stream_end", {"status": "done"}))

        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive(), "every tab should finish the same SSE stream"

        for handler in handlers:
            payload = handler.wfile.getvalue().decode("utf-8")
            assert handler.status == 200
            assert '"text": "H"' in payload
            assert '"text": "allo"' in payload
            assert "event: stream_end" in payload
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)


def test_active_stream_replays_journaled_activity_for_late_subscribers(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    session_id = "late-subscriber-session"
    stream_id = "late-subscriber-stream"
    append_run_event(
        session_id,
        stream_id,
        "tool",
        {
            "name": "terminal",
            "preview": "terminal: pytest",
            "args": {},
            "is_error": False,
            "tid": "call-1",
        },
    )

    stream = create_stream_channel()
    with STREAMS_LOCK:
        STREAMS[stream_id] = stream
    handler = _FakeHandler()
    thread = threading.Thread(
        target=_handle_sse_stream,
        args=(handler, SimpleNamespace(query=f"stream_id={stream_id}&after_seq=0")),
        daemon=True,
    )

    try:
        thread.start()
        stream.put_nowait(("token", {"text": "later"}))
        stream.put_nowait(("stream_end", {"status": "done"}))
        thread.join(timeout=1)
        assert not thread.is_alive(), "active stream should finish after live stream_end"

        payload = handler.wfile.getvalue().decode("utf-8")
        assert handler.status == 200
        assert f"id: {stream_id}:1" in payload
        assert "event: tool" in payload
        assert '"name": "terminal"' in payload
        assert '"preview": "terminal: pytest"' in payload
        assert '"text": "later"' in payload
        assert "interrupted" not in payload
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)


def test_active_stream_late_subscriber_deduplicates_journal_and_offline_buffer(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    session_id = "late-subscriber-dedup-session"
    stream_id = "late-subscriber-dedup-stream"
    tool_payload = {
        "name": "terminal",
        "preview": "terminal: pytest",
        "args": {},
        "is_error": False,
        "tid": "call-1",
    }
    append_run_event(session_id, stream_id, "tool", tool_payload)

    stream = create_stream_channel()
    stream.put_nowait(("tool", tool_payload))
    with STREAMS_LOCK:
        STREAMS[stream_id] = stream
    handler = _FakeHandler()
    thread = threading.Thread(
        target=_handle_sse_stream,
        args=(handler, SimpleNamespace(query=f"stream_id={stream_id}&after_seq=0")),
        daemon=True,
    )

    try:
        thread.start()
        stream.put_nowait(("stream_end", {"status": "done"}))
        thread.join(timeout=1)
        assert not thread.is_alive(), "active stream should finish after live stream_end"

        payload = handler.wfile.getvalue().decode("utf-8")
        assert handler.status == 200
        assert payload.count("event: tool") == 1
        assert payload.count('"tid": "call-1"') == 1
        assert "event: stream_end" in payload
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
