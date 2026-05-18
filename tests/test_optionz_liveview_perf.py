"""Option Z live-view + SSE backpressure regression tests.

Two defects fixed on top of 481ddb9 (feat/process-complete-event-isla):

Defect B — server-initiated wakeup turn is not shown live (needs refresh).
  Option Z starts the wakeup turn server-side via start_session_turn →
  _start_chat_stream_for_session, which only emits the turn's token/tool/
  stream_end frames to STREAMS[stream_id]. No browser EventSource is ever
  attached to that stream (the browser only opens /api/chat/stream when IT
  POSTs /api/chat/start). The per-session SSE channel only carried
  process_complete, never a signal to attach. Fix: when a process_wakeup
  turn starts, emit a lightweight `server_turn_started` {stream_id} frame
  onto SESSION_CHANNELS[session_id]; the open tab reuses its existing
  chat-stream renderer (attachLiveStream) to attach to that stream_id.

Defect A — SSE thread exhaustion with multiple tabs.
  server.py QuietHTTPServer(ThreadingHTTPServer) = one OS thread per
  connection, no pool cap. A slow/backgrounded tab whose TCP recv window
  is full makes handler.wfile.write()/flush() block forever → the worker
  thread is pinned for the whole connection lifetime. Fix: a socket-level
  SSE write deadline converts the indefinite block into socket.timeout
  (== TimeoutError on py3.10+, already in routes._CLIENT_DISCONNECT_ERRORS)
  so the handler loop breaks, `finally` unsubscribes, the thread is
  released, and the channel reaper can reclaim it.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Defect B — server-initiated turn fans `server_turn_started` to SessionChannel
# ---------------------------------------------------------------------------

def test_server_turn_streams_to_session_channel(monkeypatch):
    """A process_wakeup turn that successfully starts must emit a
    `server_turn_started` {stream_id} frame onto a subscribed SessionChannel
    so an open tab can attach its existing renderer to the server-created
    stream. Closed-tab path is unaffected (no subscriber → no-op)."""
    from api import background_process as bp, config as cfg
    import api.routes as routes

    sid = "sess-optz-liveview-fanout"
    fake_stream_id = "stream-optz-fanout-1"

    # Patch the heavy turn-start core so the test stays unit-fast: pretend a
    # turn started and return the same dict shape the real function returns.
    def _fake_start_chat_stream_for_session(s, **kwargs):
        return {"stream_id": fake_stream_id, "session_id": s.session_id, "_status": 200}

    class _FakeSession:
        session_id = sid
        model = "test-model"
        model_provider = None

    monkeypatch.setattr(
        routes, "_start_chat_stream_for_session", _fake_start_chat_stream_for_session, raising=True
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: _FakeSession(), raising=True)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: "/tmp/ws", raising=True
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda m, p, **_kw: ("test-model", None, False),
        raising=True,
    )

    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    try:
        resp = routes.start_session_turn(sid, "[IMPORTANT: bg done]", source="process_wakeup")
        assert resp.get("stream_id") == fake_stream_id

        event_name, data = q.get(timeout=2.0)
        assert event_name == "server_turn_started", (
            "server-initiated turn must fan a server_turn_started frame onto "
            "the per-session live-view channel"
        )
        assert data["stream_id"] == fake_stream_id
        assert data["session_id"] == sid
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)


def test_server_turn_no_session_channel_is_noop(monkeypatch):
    """Closed-tab path: no SessionChannel exists → start_session_turn must
    NOT create one and must still return the started stream (server-side
    wakeup, the Option Z headline, is unaffected)."""
    from api import background_process as bp
    import api.routes as routes

    sid = "sess-optz-liveview-notab"
    fake_stream_id = "stream-optz-notab-1"

    def _fake_start_chat_stream_for_session(s, **kwargs):
        return {"stream_id": fake_stream_id, "session_id": s.session_id, "_status": 200}

    class _FakeSession:
        session_id = sid
        model = "test-model"
        model_provider = None

    monkeypatch.setattr(
        routes, "_start_chat_stream_for_session", _fake_start_chat_stream_for_session, raising=True
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: _FakeSession(), raising=True)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: "/tmp/ws", raising=True
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda m, p, **_kw: ("test-model", None, False),
        raising=True,
    )

    assert bp.get_session_channel(sid) is None
    resp = routes.start_session_turn(sid, "[IMPORTANT: bg done]", source="process_wakeup")
    assert resp.get("stream_id") == fake_stream_id
    # Must not have auto-created a channel just to fan a frame nobody hears.
    assert bp.get_session_channel(sid) is None


# ---------------------------------------------------------------------------
# Defect A — SSE write deadline drops a stuck writer / releases the thread
# ---------------------------------------------------------------------------

def test_sse_write_deadline_helper_sets_socket_timeout():
    from api.streaming import _sse_set_write_deadline, SSE_WRITE_DEADLINE_SECONDS

    recorded = {}

    class _FakeConn:
        def settimeout(self, v):
            recorded["timeout"] = v

    class _FakeHandler:
        connection = _FakeConn()

    h = _FakeHandler()
    _sse_set_write_deadline(h)
    assert recorded["timeout"] == SSE_WRITE_DEADLINE_SECONDS

    _sse_set_write_deadline(h, 7.5)
    assert recorded["timeout"] == 7.5


def test_sse_write_deadline_helper_never_raises():
    """A handler without a usable connection must not blow up the SSE setup."""
    from api.streaming import _sse_set_write_deadline

    class _NoConn:
        connection = None

    class _Broken:
        @property
        def connection(self):
            raise RuntimeError("boom")

    _sse_set_write_deadline(_NoConn())   # no exception
    _sse_set_write_deadline(_Broken())   # no exception
    _sse_set_write_deadline(object())    # no exception


def test_sse_write_timeout_drops_slow_subscriber():
    """Behavioural: a SessionChannel subscriber whose SSE writer raises
    socket.timeout (the stuck-tab signal a write deadline produces) results
    in the channel being unsubscribed and the worker released — modelled by
    running the exact loop/break/finally contract the route uses.

    socket.timeout is TimeoutError on py3.10+, which is in
    routes._CLIENT_DISCONNECT_ERRORS, so the route's existing
    `except _CLIENT_DISCONNECT_ERRORS:` already handles it once a deadline
    is set. This test pins that contract.
    """
    from api import background_process as bp
    from api.routes import _CLIENT_DISCONNECT_ERRORS

    assert socket.timeout in (_CLIENT_DISCONNECT_ERRORS) or issubclass(
        socket.timeout, _CLIENT_DISCONNECT_ERRORS
    ), "socket.timeout must be catchable by the SSE route's disconnect handler"

    sid = "sess-optz-stuck-writer"
    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    assert ch.subscriber_count() == 1

    released = threading.Event()

    def _route_like_loop():
        # Mirror _handle_session_sse_stream's loop+finally exactly.
        try:
            while True:
                ch.emit("server_turn_started", {"stream_id": "x"})
                _evt = q.get(timeout=1.0)
                # Simulate handler.wfile.write hitting the write deadline:
                raise socket.timeout("timed out")
        except _CLIENT_DISCONNECT_ERRORS:
            pass
        finally:
            ch.unsubscribe(q)
            released.set()

    t = threading.Thread(target=_route_like_loop, daemon=True)
    t.start()
    assert released.wait(timeout=3.0), "stuck-writer handler did not release"
    t.join(timeout=2.0)
    assert ch.subscriber_count() == 0, "stuck subscriber was not dropped"
    with bp.SESSION_CHANNELS_LOCK:
        bp.SESSION_CHANNELS.pop(sid, None)


# ---------------------------------------------------------------------------
# Source-grep wiring guards
# ---------------------------------------------------------------------------

def test_all_sse_endpoints_set_write_deadline():
    src = (REPO_ROOT / "api" / "routes.py").read_text()
    assert "_sse_set_write_deadline" in src
    # Every SSE handler must arm the deadline. Count call sites — there are
    # 6 long-lived SSE endpoints (chat-stream, terminal, gateway, approval,
    # clarify, session).
    assert src.count("_sse_set_write_deadline(handler") >= 6, (
        "all 6 SSE endpoints must arm the write deadline"
    )


def test_streaming_exports_write_deadline_api():
    from api import streaming
    assert hasattr(streaming, "_sse_set_write_deadline")
    assert hasattr(streaming, "SSE_WRITE_DEADLINE_SECONDS")
    assert isinstance(streaming.SSE_WRITE_DEADLINE_SECONDS, (int, float))


def test_start_session_turn_emits_server_turn_started():
    src = (REPO_ROOT / "api" / "routes.py").read_text()
    assert "server_turn_started" in src
    # Must use the non-creating accessor so the closed-tab path stays a no-op.
    assert "get_session_channel" in src


def test_frontend_attaches_renderer_on_server_turn_started():
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    assert "server_turn_started" in js
    # Must reuse the existing chat-stream render path, not hand-roll a 2nd one.
    assert "attachLiveStream" in js
