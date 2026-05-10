import io
import threading
from types import SimpleNamespace

from api.config import STREAMS, STREAMS_LOCK, create_stream_channel
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
