"""Regression: apperror must close SSE relay drain loops.

Backend emits `apperror` as a terminal event with no trailing `stream_end`
(streaming.py returns immediately after put('apperror', ...)). Relay loops that
only treat ("stream_end", "error", "cancel") as close signals hang forever —
the phantom name `error` is never emitted. The authoritative close set is
api.run_journal.SSE_RELAY_CLOSE_EVENTS.
"""
from __future__ import annotations

from pathlib import Path

import api.config as config
from api.run_journal import SSE_RELAY_CLOSE_EVENTS, TERMINAL_SSE_EVENTS


def test_relay_close_events_include_apperror_not_only_phantom_error():
    assert "apperror" in SSE_RELAY_CLOSE_EVENTS
    assert "stream_end" in SSE_RELAY_CLOSE_EVENTS
    assert "cancel" in SSE_RELAY_CLOSE_EVENTS
    # `error` may remain for legacy/adapter frames, but must not be the sole
    # stand-in for apperror.
    assert "apperror" in SSE_RELAY_CLOSE_EVENTS
    # `done` must NOT close the live chat relay — title + stream_end follow it.
    assert "done" not in SSE_RELAY_CLOSE_EVENTS
    assert "done" in TERMINAL_SSE_EVENTS


def test_routes_relay_loops_import_sse_relay_close_events():
    src = Path("api/routes.py").read_text(encoding="utf-8")
    assert "SSE_RELAY_CLOSE_EVENTS" in src
    # The three historic hardcoded tuples must be gone.
    assert 'event in ("stream_end", "error", "cancel")' not in src
    assert 'event in ("stream_end", "error", "cancel")' not in src
    assert '_is_terminal = event in ("stream_end", "error", "cancel")' not in src


def test_cancel_drop_list_allows_apperror_not_phantom_error():
    session_id = "unowned-cancel-apperror-oracle-session"
    stream_id = "unowned-cancel-apperror-oracle-stream"
    key = (session_id, stream_id)
    assert key not in config.STREAM_CANCEL_GENERATIONS
    assert session_id not in config.SESSION_WRITEBACK_OWNERS

    assert config.admit_stream_publication(
        session_id,
        stream_id,
        "apperror",
        cancelled=True,
    ) is None
    assert config.admit_stream_publication(
        session_id,
        stream_id,
        "error",
        cancelled=True,
    ) is False
    assert config.admit_stream_publication(
        session_id,
        stream_id,
        "token",
        cancelled=True,
    ) is False

    assert key not in config.STREAM_CANCEL_GENERATIONS
    assert session_id not in config.SESSION_WRITEBACK_OWNERS


def test_apperror_terminates_relay_close_predicate():
    """Observable behavior of the shared predicate used by relay loops."""
    assert "apperror" in SSE_RELAY_CLOSE_EVENTS
    # Simulate the drain decision: apperror must stop the loop.
    events = ["token", "tool", "apperror", "token"]
    closed_at = None
    for i, event in enumerate(events):
        if event in SSE_RELAY_CLOSE_EVENTS:
            closed_at = i
            break
    assert closed_at == 2
    # done alone must not stop the relay (title / stream_end still pending).
    assert "done" not in SSE_RELAY_CLOSE_EVENTS


def test_chat_sse_stream_unsubscribes_on_apperror(monkeypatch):
    """Live /api/chat/stream must exit the drain loop when apperror arrives."""
    import io
    import queue
    from urllib.parse import urlparse

    import api.routes as routes

    class _Handler:
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

    class _FakeStream:
        def __init__(self):
            self.q = queue.Queue()
            self.q.put_nowait(("token", {"text": "hi"}, "run1:1"))
            self.q.put_nowait(("apperror", {"message": "provider failed"}, "run1:2"))
            self.unsubscribed = False

        def subscribe_with_snapshot(self):
            return self.q, {"last_event_id": None, "offline_buffered_events": 0}

        def unsubscribe(self, q):
            self.unsubscribed = q is self.q

    stream = _FakeStream()
    monkeypatch.setattr(routes, "_stream_id_visible_to_request_profile", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "STREAMS", {"run1": stream})
    monkeypatch.setattr(
        routes,
        "_sse_replay_run_journal_gap_checked",
        lambda *_a, **_k: (False, None),
    )

    def _sleep(_seconds):
        raise BrokenPipeError("hung waiting after apperror")

    monkeypatch.setattr(routes.time, "sleep", _sleep)

    handler = _Handler()
    routes._handle_sse_stream(handler, urlparse("/api/chat/stream?stream_id=run1"))

    assert stream.unsubscribed is True, "handler must unsubscribe after apperror"
    body = handler.wfile.getvalue().decode("utf-8", errors="replace")
    assert "event: apperror" in body
