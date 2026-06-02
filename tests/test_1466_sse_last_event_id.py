import io
from types import SimpleNamespace

from api.config import STREAMS, STREAMS_LOCK, STREAM_LAST_EVENT_ID, attach_sse_event_id, create_stream_channel
from api.routes import _handle_sse_stream, _parse_run_journal_after_seq, _runner_stream_cursor_from_query, _sse
from api.streaming import _sse as _streaming_sse


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.sent_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        return None


def test_last_event_id_is_used_as_replay_cursor_when_query_is_absent():
    assert _parse_run_journal_after_seq({}, stream_id="run-1", last_event_id="run-1:41") == 41


def test_mismatched_last_event_id_is_ignored_for_run_journal_replay():
    assert _parse_run_journal_after_seq({}, stream_id="run-1", last_event_id="run-2:41") is None


def test_query_after_event_id_takes_precedence_over_last_event_id():
    qs = {"after_event_id": ["run-1:7"]}
    assert _parse_run_journal_after_seq(qs, stream_id="run-1", last_event_id="run-1:41") == 7


def test_runner_cursor_uses_last_event_id_fallback():
    assert _runner_stream_cursor_from_query({}, last_event_id="run-9:12") == "12"


def test_routes_sse_emits_native_id_field():
    handler = _FakeHandler()
    _sse(handler, "token", {"text": "hi"}, event_id="run-1:1")
    payload = handler.wfile.getvalue().decode("utf-8")
    assert payload.startswith("id: run-1:1\nevent: token\n")
    assert 'data: {"text": "hi"}' in payload


def test_streaming_sse_emits_native_id_field():
    handler = _FakeHandler()
    _streaming_sse(handler, "token", {"text": "hi"}, event_id="run-1:1")
    payload = handler.wfile.getvalue().decode("utf-8")
    assert payload.startswith("id: run-1:1\nevent: token\n")


def test_live_chat_stream_uses_per_event_payload_id_not_stale_side_channel():
    stream_id = "run-live-id-test"
    stream = create_stream_channel()
    stream.put_nowait(("token", attach_sse_event_id({"text": "A"}, "run-live-id-test:1")))
    stream.put_nowait(("token", attach_sse_event_id({"text": "B"}, "run-live-id-test:2")))
    stream.put_nowait(("stream_end", attach_sse_event_id({"status": "done"}, "run-live-id-test:3")))

    handler = _FakeHandler()
    with STREAMS_LOCK:
        previous = dict(STREAMS)
        previous_ids = dict(STREAM_LAST_EVENT_ID)
        STREAMS.clear()
        STREAM_LAST_EVENT_ID.clear()
        STREAMS[stream_id] = stream
        STREAM_LAST_EVENT_ID[stream_id] = "run-live-id-test:99"
    try:
        assert _handle_sse_stream(handler, SimpleNamespace(query=f"stream_id={stream_id}")) is True
        payload = handler.wfile.getvalue().decode("utf-8")
        assert "id: run-live-id-test:1\nevent: token" in payload
        assert "id: run-live-id-test:2\nevent: token" in payload
        assert "id: run-live-id-test:3\nevent: stream_end" in payload
        assert "id: run-live-id-test:99" not in payload
    finally:
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(previous)
            STREAM_LAST_EVENT_ID.clear()
            STREAM_LAST_EVENT_ID.update(previous_ids)
