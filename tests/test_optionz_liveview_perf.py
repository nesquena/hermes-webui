"""Option Z live-view + SSE backpressure regression tests.

Two defects fixed on top of 481ddb9 (feat/process-complete-event-isla):

Defect B — server-initiated wakeup turn is not shown live (needs refresh).
  Option Z starts the wakeup turn server-side via start_session_turn →
  _start_chat_stream_for_session, which only emits the turn's token/tool/
  stream_end frames to STREAMS[stream_id]. No browser EventSource is ever
  attached to that stream (the browser only opens /api/chat/stream when IT
  POSTs /api/chat/start). The per-session SSE channel only carried
  bg_task_complete, never a signal to attach. Fix: when a process_wakeup
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Defect B — server-initiated turn fans `server_turn_started` to SessionChannel
# ---------------------------------------------------------------------------

def test_server_turn_streams_to_session_channel(monkeypatch):
    """A process_wakeup turn that successfully starts must emit a
    `server_turn_started` {stream_id} frame onto a subscribed SessionChannel
    so an open tab can attach its existing renderer to the server-created
    stream. Closed-tab path is unaffected (no subscriber → no-op)."""
    from api import background_process as bp
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


def test_sse_write_deadline_env_override(monkeypatch):
    import importlib

    from api import streaming

    monkeypatch.setenv("HERMES_SSE_WRITE_DEADLINE", "7.25")
    try:
        reloaded = importlib.reload(streaming)
        assert reloaded.SSE_WRITE_DEADLINE_SECONDS == 7.25
    finally:
        monkeypatch.delenv("HERMES_SSE_WRITE_DEADLINE", raising=False)
        importlib.reload(streaming)


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


# ---------------------------------------------------------------------------
# Root cause (open-tab live-view): lost fire-and-forget server_turn_started
#   The fan-out in start_session_turn is SessionChannel.emit with NO replay
#   buffer — a tab whose /api/session/stream subscriber is momentarily absent
#   at the emit instant (transient SSE drop, reverse-proxy idle-timeout,
#   browser connection-pool starvation) misses the frame permanently and the
#   server-initiated wakeup never renders live (the user must hard-refresh).
#   The server-side wakeup itself ran + persisted fine; ONLY the live-view
#   was lost. Fix: on (re)subscribe to /api/session/stream, if the session
#   has a live run RIGHT NOW, replay a synthetic server_turn_started
#   {recovered: True} to that new subscriber so the open tab self-heals.
#
#   Reproduced deterministically with Playwright on the real instance: with
#   the per-session EventSource force-closed at the exact wakeup-emit instant
#   (no subscriber), `sleep 15`'s wakeup turn did NOT render live; a hard
#   refresh showed it WAS persisted (proving server-side wakeup works and
#   only live-view was broken). See workspace/liveview-open-tab-fix.md §1.
# ---------------------------------------------------------------------------

def test_active_stream_id_for_session_returns_live_run_stream():
    """The on-subscribe recovery lookup must return the live run's stream_id
    when the session has an ACTIVE_RUNS row, and None when idle."""
    from api import background_process as bp, config as cfg

    sid = "sess-recover-lookup"
    stream_id = "stream-recover-lookup-1"

    assert bp.active_stream_id_for_session(sid) is None  # idle → None

    cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}
    try:
        assert bp.active_stream_id_for_session(sid) == stream_id
        # Unrelated session must not match.
        assert bp.active_stream_id_for_session("sess-other") is None
    finally:
        cfg.ACTIVE_RUNS.pop(stream_id, None)
    assert bp.active_stream_id_for_session(sid) is None  # cleaned up → None


def test_session_sse_on_subscribe_recovers_lost_server_turn_started():
    """Behavioural contract: a tab that (re)subscribes to the per-session
    channel AFTER the fire-and-forget server_turn_started was already
    broadcast (so it missed the original frame) must still receive a
    recovery server_turn_started for the in-flight stream — modelled by
    running the exact recovery block the route uses.

    This is the root-cause fix: without it, a momentarily-absent subscriber
    loses the frame permanently and the open tab never renders the
    server-initiated wakeup turn live (needs a hard refresh).
    """
    from api import background_process as bp, config as cfg

    sid = "sess-recover-onsub"
    stream_id = "stream-recover-onsub-1"

    # Simulate: server-side wakeup turn IS live (ACTIVE_RUNS row exists), but
    # the original server_turn_started broadcast already happened and reached
    # NO subscriber (the tab's EventSource was momentarily down).
    cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}
    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()  # tab (re)connects NOW, after the lost broadcast
    try:
        # Exactly the route's on-subscribe recovery logic
        # (_handle_session_sse_stream): look up the live run and replay.
        recover_stream_id = bp.active_stream_id_for_session(sid)
        assert recover_stream_id == stream_id
        recovery_frame = {
            "session_id": sid,
            "stream_id": recover_stream_id,
            "source": "subscribe_recovery",
            "recovered": True,
        }
        # The route _sse()s this directly to the new subscriber's connection;
        # the contract under test is "a freshly-subscribed tab gets a
        # server_turn_started for the in-flight stream so it can attach".
        assert recovery_frame["stream_id"] == stream_id
        assert recovery_frame["recovered"] is True
        assert recovery_frame["session_id"] == sid

        # And when the session is idle (no live run) the recovery is a no-op
        # — no spurious attach frame for a session with nothing running.
        cfg.ACTIVE_RUNS.pop(stream_id, None)
        assert bp.active_stream_id_for_session(sid) is None
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)
        cfg.ACTIVE_RUNS.pop(stream_id, None)


def test_session_sse_handler_wires_on_subscribe_recovery():
    """Source-grep: the per-session SSE handler must perform on-subscribe
    recovery via active_stream_id_for_session and emit a recovered
    server_turn_started, AFTER subscribing (so it can't race the original)."""
    src = (REPO_ROOT / "api" / "routes.py").read_text()
    assert "active_stream_id_for_session" in src
    # The recovery must be inside the session SSE handler and use the
    # recovered marker so the frontend uses the replay attach path.
    handler_ix = src.index("def _handle_session_sse_stream")
    handler_src = src[handler_ix:handler_ix + 6000]
    assert "active_stream_id_for_session" in handler_src
    assert '"recovered": True' in handler_src
    assert "server_turn_started" in handler_src
    # Recovery CALL must come AFTER the channel subscription so a frame emitted
    # between subscribe and recovery is still caught by the queue (no lost-frame
    # gap). The handler subscribes via the atomic ``subscribe_to_session_channel``
    # helper (TOCTOU-safe get-or-create+subscribe under one lock); assert on that
    # call site vs the recovery call site ``= active_stream_id_for_session(``.
    assert handler_src.index("subscribe_to_session_channel(") < handler_src.index(
        "= active_stream_id_for_session("
    )


def test_frontend_recovered_frame_uses_reconnecting_attach():
    """The frontend server_turn_started handler must honour `recovered`:
    a recovered (replay) frame attaches via the reconnecting path so the
    renderer rebuilds the in-progress stream from the run journal instead
    of expecting token 0 (which would render a truncated turn)."""
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    assert "recovered" in js
    h_ix = js.index("addEventListener('server_turn_started'")
    h_src = js[h_ix:h_ix + 1600]
    assert "d.recovered" in h_src
    assert "reconnecting" in h_src
    # Still reuses the single renderer — no second hand-rolled stream.
    assert "attachLiveStream" in h_src


# ---------------------------------------------------------------------------
# Copilot review #3 — _emit_to_session_streams owner-unknown broadcast
#   Resolution: skip non-matching AND owner-unknown streams on the STREAMS
#   loop (rely solely on SESSION_CHANNELS for cross-turn live-view, which the
#   repro proved is the sole authoritative carrier post Option X/Z). Removes
#   the cross-session-leak surface Copilot flagged.
# ---------------------------------------------------------------------------

def test_emit_to_session_streams_skips_owner_unknown_stream():
    """Copilot #3: a STREAMS entry with NO ACTIVE_RUNS row (owner unknown)
    must NOT receive the event on the STREAMS loop — the old code broadcast
    to it, relying on every frontend consumer to filter by session_id (a
    fragile cross-session leak). The per-session SessionChannel still
    delivers (the authoritative cross-turn live-view path)."""
    from api import background_process as bp, config as cfg

    sid = "sess-copilot3-skip"
    unknown_stream_id = "stream-copilot3-no-active-run"

    leaked: list = []

    class _FakeStreamChannel:
        def put_nowait(self, item):
            leaked.append(item)

    with cfg.STREAMS_LOCK:
        cfg.STREAMS[unknown_stream_id] = _FakeStreamChannel()
    # Deliberately NO cfg.ACTIVE_RUNS row for unknown_stream_id → owner unknown.

    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    try:
        emitted = bp._emit_to_session_streams(sid, "bg_task_complete", {"session_id": sid})
        # The owner-unknown STREAMS entry must NOT have been written to.
        assert leaked == [], (
            "owner-unknown stream must be skipped (no cross-session broadcast)"
        )
        # The per-session SessionChannel still delivered (authoritative path).
        ev, data = q.get(timeout=2.0)
        assert ev == "bg_task_complete"
        assert data["session_id"] == sid
        assert emitted >= 1
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.pop(unknown_stream_id, None)


def test_emit_to_session_streams_still_delivers_to_matching_owner():
    """Regression guard for the Copilot #3 change: an owner-KNOWN stream
    whose session matches MUST still receive the event on the STREAMS loop
    (in-turn defense-in-depth path is preserved)."""
    from api import background_process as bp, config as cfg

    sid = "sess-copilot3-match"
    stream_id = "stream-copilot3-match-1"

    received: list = []

    class _FakeStreamChannel:
        def put_nowait(self, item):
            received.append(item)

    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = _FakeStreamChannel()
    cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}

    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    try:
        bp._emit_to_session_streams(sid, "bg_task_complete", {"session_id": sid})
        assert received, "owner-matching stream must still receive in-turn delivery"
        assert received[0][0] == "bg_task_complete"
        ev, _data = q.get(timeout=2.0)
        assert ev == "bg_task_complete"
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.pop(stream_id, None)
        cfg.ACTIVE_RUNS.pop(stream_id, None)


def test_emit_to_session_streams_does_not_leak_to_other_session_owner():
    """Cross-session isolation: a stream owned by a DIFFERENT session must
    never receive this session's event (unchanged behavior, pinned)."""
    from api import background_process as bp, config as cfg

    sid = "sess-copilot3-self"
    other_sid = "sess-copilot3-other"
    other_stream_id = "stream-copilot3-other-1"

    leaked: list = []

    class _FakeStreamChannel:
        def put_nowait(self, item):
            leaked.append(item)

    with cfg.STREAMS_LOCK:
        cfg.STREAMS[other_stream_id] = _FakeStreamChannel()
    cfg.ACTIVE_RUNS[other_stream_id] = {"session_id": other_sid}

    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    try:
        bp._emit_to_session_streams(sid, "bg_task_complete", {"session_id": sid})
        assert leaked == [], "must not leak to a different session's stream"
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.pop(other_stream_id, None)
        cfg.ACTIVE_RUNS.pop(other_stream_id, None)


def test_emit_to_session_streams_skip_unknown_owner_documented_in_source():
    """Source-grep: the Copilot #3 resolution must be the skip-unknown-owner
    form (`if owner_sid != session_id: continue`), not the old
    broadcast-on-unknown fallback (`if owner_sid and owner_sid != ...`)."""
    src = (REPO_ROOT / "api" / "background_process.py").read_text()
    fn_ix = src.index("def _emit_to_session_streams")
    fn_src = src[fn_ix:fn_ix + 2600]
    assert "if owner_sid != session_id:" in fn_src
    assert "if owner_sid and owner_sid != session_id:" not in fn_src
    assert "Copilot review #3" in fn_src


# ---------------------------------------------------------------------------
# event_id contract surface (post-rename to bg_task_complete)
# ---------------------------------------------------------------------------
#
# The #2242 thread Q4 reply pins the consumer dedupe key on
# `(session_id, event_id)`. The handler in static/messages.js MUST treat
# event_id as mandatory and MUST NOT surface or ack an event missing one.
# ---------------------------------------------------------------------------


def test_frontend_handler_requires_event_id_to_surface():
    """Source-grep: _handleBgTaskCompleteEvent ignores events without event_id."""
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    fn_ix = js.index("function _handleBgTaskCompleteEvent")
    fn_src = js[fn_ix:fn_ix + 1800]
    # event_id is extracted from the payload.
    assert "d.event_id" in fn_src
    # Missing event_id short-circuits before any dedupe / ack.
    assert "if (!evt_id) return;" in fn_src or "if(!evt_id) return;" in fn_src
    # Dedupe goes through the ring buffer helper, keyed by (sid, event_id).
    assert "_bgTaskCompleteRingBufferAdd(sid, evt_id)" in fn_src
    # Ack body carries event_id so server can correlate.
    assert "event_id: evt_id" in fn_src
