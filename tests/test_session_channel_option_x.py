"""Tests for the persistent per-session SSE channel (Option X).

Verifies the SessionChannel registry + reaper + dual-emit + endpoint wiring
that bridges the cross-turn bg_task_complete delivery gap (between agent
turns STREAMS is torn down, so the session-scoped channel is the only live
surface).

Companion to t_98368bd0 implementation plan. Structural (source-grep) checks
plus pure-function tests for the SessionChannel class and reaper logic.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------

def test_background_process_exports_session_channel_api():
    from api import background_process as bp

    for name in (
        "SessionChannel",
        "SESSION_CHANNELS",
        "SESSION_CHANNELS_LOCK",
        "get_or_create_session_channel",
        "subscribe_to_session_channel",
        "get_session_channel",
        "start_session_channel_reaper",
        "stop_session_channel_reaper",
    ):
        assert hasattr(bp, name), f"missing: {name}"


def test_config_exports_session_channel_ttl_constants():
    from api import config as cfg

    assert isinstance(cfg.SESSION_CHANNEL_IDLE_TTL_SECS, int)
    assert cfg.SESSION_CHANNEL_IDLE_TTL_SECS == 14400  # 4 hours per spec
    assert isinstance(cfg.SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS, int)
    assert cfg.SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS == 60


# ---------------------------------------------------------------------------
# SessionChannel: subscribe / emit / unsubscribe lifecycle
# ---------------------------------------------------------------------------

def test_session_channel_subscriber_lifecycle():
    """subscribe → emit → unsubscribe with no leak."""
    from api.background_process import SessionChannel

    ch = SessionChannel("sess-1")
    q1 = ch.subscribe()
    q2 = ch.subscribe()
    assert ch.subscriber_count() == 2

    delivered = ch.emit("bg_task_complete", {"hello": 1})
    assert delivered == 2
    assert q1.get_nowait() == ("bg_task_complete", {"hello": 1})
    assert q2.get_nowait() == ("bg_task_complete", {"hello": 1})

    ch.unsubscribe(q1)
    assert ch.subscriber_count() == 1
    # Re-emit goes to remaining sub only
    ch.emit("bg_task_complete", {"hello": 2})
    assert q2.get_nowait() == ("bg_task_complete", {"hello": 2})

    ch.unsubscribe(q2)
    assert ch.subscriber_count() == 0


def test_session_channel_emit_with_full_buffer_drops_silently():
    """A slow tab whose queue is full doesn't block other subscribers."""
    from api.background_process import SessionChannel

    ch = SessionChannel("sess-full")
    q_slow = ch.subscribe(maxsize=1)
    q_fast = ch.subscribe(maxsize=16)
    # Fill the slow queue
    q_slow.put_nowait(("filler", {}))

    delivered = ch.emit("bg_task_complete", {"x": 1})
    # fast receives, slow dropped
    assert delivered == 1
    assert q_fast.get_nowait() == ("bg_task_complete", {"x": 1})


# ---------------------------------------------------------------------------
# Reaper: subscribers-empty grace + idle TTL cap
# ---------------------------------------------------------------------------

def test_session_channel_reaper_keeps_live_subscriber():
    """A channel with at least one subscriber must NEVER be collected."""
    from api.background_process import SessionChannel

    ch = SessionChannel("sess-live")
    ch.subscribe()
    # Far in the future, with live subscriber → not collected
    assert ch.reaper_should_collect(time.time() + 1_000_000) is False


def test_session_channel_reaper_grace_period():
    """No subscribers + grace expired → collect."""
    from api.background_process import SessionChannel
    from api import config as cfg

    ch = SessionChannel("sess-grace")
    q = ch.subscribe()
    ch.unsubscribe(q)
    # Just dropped — within grace → keep
    assert ch.reaper_should_collect(time.time()) is False
    # Past grace → collect
    later = time.time() + cfg.SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS + 1
    assert ch.reaper_should_collect(later) is True


def test_session_channel_reaper_idle_ttl_cap():
    """Channel older than idle TTL with no subscribers → collect."""
    from api.background_process import SessionChannel
    from api import config as cfg

    ch = SessionChannel("sess-zombie")
    # Force created_at far in the past, no subscriber drop tracked
    ch.created_at = time.time() - (cfg.SESSION_CHANNEL_IDLE_TTL_SECS + 100)
    ch.last_subscriber_drop_at = None
    assert ch.subscriber_count() == 0
    assert ch.reaper_should_collect(time.time()) is True


def test_session_channel_reaper_idle_ttl_held_by_live_subscriber():
    """Even past idle TTL, a live subscriber keeps the channel alive."""
    from api.background_process import SessionChannel
    from api import config as cfg

    ch = SessionChannel("sess-zombie-live")
    ch.created_at = time.time() - (cfg.SESSION_CHANNEL_IDLE_TTL_SECS + 100)
    ch.subscribe()  # someone IS listening
    assert ch.reaper_should_collect(time.time()) is False


def test_reaper_collects_via_registry():
    """The reaper loop iterates SESSION_CHANNELS and removes collected entries."""
    from api import background_process as bp, config as cfg

    sid = "sess-reaper-integration"
    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    ch.unsubscribe(q)
    # Push the drop time far enough back to trigger grace collection
    ch.last_subscriber_drop_at = time.time() - (cfg.SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS + 5)

    # Drive one iteration of the reaper's body directly
    now = time.time()
    with bp.SESSION_CHANNELS_LOCK:
        for k, channel in list(bp.SESSION_CHANNELS.items()):
            if channel.reaper_should_collect(now):
                bp.SESSION_CHANNELS.pop(k, None)
    assert bp.get_session_channel(sid) is None


def test_subscribe_to_session_channel_is_atomic_get_create_subscribe():
    """The atomic helper returns a registered channel with the subscriber
    already attached, in one SESSION_CHANNELS_LOCK critical section.

    First call creates; second call reuses the same instance.
    """
    from api import background_process as bp

    sid = "sess-atomic-subscribe"
    with bp.SESSION_CHANNELS_LOCK:
        bp.SESSION_CHANNELS.pop(sid, None)
    try:
        ch, q = bp.subscribe_to_session_channel(sid)
        # Channel is registered and the slot is already counted.
        assert bp.get_session_channel(sid) is ch
        assert ch.subscriber_count() == 1
        # An emit reaches our queue immediately — proves we're on the live channel.
        ch.emit("bg_task_complete", {"n": 1})
        assert q.get_nowait() == ("bg_task_complete", {"n": 1})

        # Second call reuses the same instance and adds a second subscriber.
        ch2, q2 = bp.subscribe_to_session_channel(sid)
        assert ch2 is ch
        assert ch.subscriber_count() == 2
        ch.unsubscribe(q2)
    finally:
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)


def test_subscribe_to_session_channel_survives_concurrent_reaper():
    """Regression for PR #2971 Greptile P1 (background_process.py:215).

    Reproduces the reaper TOCTOU: an idle, past-grace channel exists in the
    registry; a subscriber arrives while the reaper sweeps concurrently. With
    the old split ``get_or_create_session_channel()`` + ``ch.subscribe()`` the
    reaper could collect the channel in the gap, orphaning the subscriber so
    later emits never reach its queue. The atomic helper holds
    SESSION_CHANNELS_LOCK across both steps, so the post-subscribe registry
    entry must be the EXACT channel the subscriber is attached to, and a
    subsequent emit must be delivered.
    """
    import threading
    from api import background_process as bp, config as cfg

    sid = "sess-reaper-toctou"

    # Seed an idle channel that is already eligible for collection (no subs,
    # drop time pushed well past the grace window) — the dangerous precondition.
    with bp.SESSION_CHANNELS_LOCK:
        bp.SESSION_CHANNELS.pop(sid, None)
        seed = bp.SessionChannel(sid)
        seed.last_subscriber_drop_at = (
            time.time() - (cfg.SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS + 5)
        )
        bp.SESSION_CHANNELS[sid] = seed

    stop = threading.Event()

    def _reaper_spin():
        # Hammer the exact reaper critical section concurrently.
        while not stop.is_set():
            now = time.time()
            with bp.SESSION_CHANNELS_LOCK:
                for k, channel in list(bp.SESSION_CHANNELS.items()):
                    if channel.reaper_should_collect(now):
                        bp.SESSION_CHANNELS.pop(k, None)

    t = threading.Thread(target=_reaper_spin, daemon=True)
    t.start()
    try:
        for _ in range(200):
            ch, q = bp.subscribe_to_session_channel(sid)
            # Post-condition: the channel we're subscribed to is the one in the
            # registry (atomicity guarantee). The reaper cannot have evicted it
            # between create and subscribe because both ran under the lock and
            # the channel now has a live subscriber.
            assert bp.get_session_channel(sid) is ch
            assert ch.subscriber_count() >= 1
            # And an emit on the registry-resolved channel reaches our queue —
            # i.e. we are NOT orphaned on a collected channel.
            resolved = bp.get_session_channel(sid)
            resolved.emit("bg_task_complete", {"ping": 1})
            assert q.get(timeout=1.0) == ("bg_task_complete", {"ping": 1})
            ch.unsubscribe(q)
    finally:
        stop.set()
        t.join(timeout=2.0)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)


# ---------------------------------------------------------------------------
# Dual emit: STREAMS empty + SESSION_CHANNELS has subscriber → delivered
# ---------------------------------------------------------------------------

def test_emit_when_idle_between_turns_session_channel_delivers():
    """No STREAMS but a SESSION_CHANNELS subscriber → event reaches the channel."""
    from api import background_process as bp, config as cfg

    sid = "sess-between-turns"
    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    try:
        # Make sure STREAMS has no relevant entry
        with cfg.STREAMS_LOCK:
            assert all(
                (cfg.ACTIVE_RUNS.get(stream_id) or {}).get("session_id") != sid
                for stream_id in cfg.STREAMS
            )

        evt = {
            "type": "completion",
            "session_id": "proc-99",
            "session_key": sid,
            "command": "sleep 1",
            "exit_code": 0,
            "output": "ok",
        }
        bp.register_process_session(sid, sid)
        try:
            bp._process_one(evt)
            event_name, data = q.get(timeout=2.0)
            assert event_name == "bg_task_complete"
            assert data["session_id"] == sid
            assert data["task_id"] == "proc-99"
            assert data.get("event_id"), "emitter must stamp event_id (Q4 contract)"
        finally:
            bp.unregister_process_session(sid)
            cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
            cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)


def test_emit_during_busy_turn_dual_emits_to_both():
    """STREAMS + SESSION_CHANNELS both subscribed → both receive."""
    from api import background_process as bp, config as cfg

    sid = "sess-busy-dual"
    stream_id = "stream-busy-dual"

    streams_received: list = []

    class _FakeStreamChannel:
        def put_nowait(self, item):
            streams_received.append(item)

    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = _FakeStreamChannel()
    cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}

    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    bp.register_process_session(sid, sid)

    try:
        evt = {
            "type": "completion",
            "session_id": "proc-busy-1",
            "session_key": sid,
            "command": "sleep 1",
            "exit_code": 0,
            "output": "ok",
        }
        bp._process_one(evt)

        # Both surfaces received the event (frontend will dedupe by process_id)
        assert streams_received, "STREAMS subscriber must receive in-turn delivery"
        event_name, data = streams_received[0]
        assert event_name == "bg_task_complete"
        assert data["task_id"] == "proc-busy-1"
        assert data.get("event_id"), "emitter must stamp event_id (Q4 contract)"

        ev2, data2 = q.get(timeout=2.0)
        assert ev2 == "bg_task_complete"
        assert data2["task_id"] == "proc-busy-1"
        assert data2.get("event_id"), "emitter must stamp event_id (Q4 contract)"
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.pop(stream_id, None)
        cfg.ACTIVE_RUNS.pop(stream_id, None)
        bp.unregister_process_session(sid)
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


# ---------------------------------------------------------------------------
# Route + frontend wiring (source-grep)
# ---------------------------------------------------------------------------

def test_routes_registers_session_stream_endpoint():
    src = (REPO_ROOT / "api" / "routes.py").read_text()
    assert "/api/session/stream" in src
    assert "_handle_session_sse_stream" in src


def test_routes_session_sse_uses_session_channel_subscribe():
    src = (REPO_ROOT / "api" / "routes.py").read_text()
    # The handler must use the atomic get-or-create+subscribe helper (closes
    # the PR #2971 reaper TOCTOU race) and release the slot on every exit path.
    assert "subscribe_to_session_channel" in src
    assert "ch.unsubscribe(q)" in src
    # The split get-then-subscribe call pair must NOT come back — it reopens
    # the race the atomic helper exists to close.
    assert "ch = get_or_create_session_channel(sid)" not in src


def test_server_starts_session_channel_reaper():
    src = (REPO_ROOT / "server.py").read_text()
    assert "start_session_channel_reaper" in src
    assert "stop_session_channel_reaper" in src


def test_frontend_opens_session_stream():
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    assert "api/session/stream?session_id=" in js
    assert "startSessionStream" in js
    assert "stopSessionStream" in js


def test_frontend_busy_race_gate_obsoleted_by_option_z_pivot():
    """Per the Option Z PIVOT note baked into the handler body, the browser
    is no longer in the wakeup path at all — the server-side drain owns
    starting the next turn. The original ``if (S.busy)`` busy-race gate
    inside the handler was paired with the now-removed re-POST of
    ``/api/chat/stream``; once the re-POST went away (Option Z), the gate
    became moot. We assert the pivot documentation is in place so a future
    refactor doesn't silently re-introduce the gate without re-introducing
    the re-POST as well."""
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    fn_ix = js.index("function _handleBgTaskCompleteEvent")
    fn_src = js[fn_ix:fn_ix + 2400]
    assert "Option Z PIVOT" in fn_src
    assert "drain thread" in fn_src
    # The legacy re-POST and busy-race gate must NOT be inside the handler.
    assert "if (S.busy)" not in fn_src
    assert "/api/chat/stream" not in fn_src


def test_frontend_shared_handler_dedupes_across_paths():
    """Module-scope dedupe ring buffer (Map+TTL keyed (sid, event_id)) is what makes dual-emit safe."""
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    # Module-scope Map+TTL declaration outside any `function () { ... }` body
    assert "const _bgTaskCompleteSeenIds = new Map();" in js
    assert "const _BG_TASK_COMPLETE_TTL_MS = 60000;" in js
    assert "const _BG_TASK_COMPLETE_CAP = 256;" in js
    assert "_bgTaskCompleteRingBufferAdd" in js
    assert "_handleBgTaskCompleteEvent" in js
    # The handler must require event_id (server contract surface per Q4).
    assert "if (!evt_id) return;" in js or "if(!evt_id) return;" in js


def test_sessions_js_starts_and_stops_session_stream_on_mount_unmount():
    js = (REPO_ROOT / "static" / "sessions.js").read_text()
    assert "startSessionStream(S.session.session_id)" in js
    assert "stopSessionStream" in js


# ---------------------------------------------------------------------------
# Regression: notify_on_complete event shape carries NO session_key
# ---------------------------------------------------------------------------
#
# Root cause of "zero ack POSTs ever" (t_0f447014):
#   tools.process_registry.ProcessRegistry._move_to_finished() enqueues the
#   completion event for a notify_on_complete process WITHOUT a "session_key"
#   field (only the watch_match enqueue includes one). _process_one then does
#   `session_key = evt.get("session_key") or process_id`, so it falls back to
#   the process id ("proc_xxxx"), which is NEVER a key in
#   PROCESS_SESSION_INDEX (only webui_session_id -> webui_session_id is
#   registered). The lookup misses → silent debug drop → no SSE emit ever.
#
# The existing happy-path test (test_emit_when_idle_between_turns_...) masks
# this because it hand-builds evt WITH "session_key": sid — a shape the real
# completion enqueue never produces. This test drives the event through the
# REAL process_registry.completion_queue using the EXACT dict shape
# _move_to_finished() produces, so it fails on the unfixed code and passes
# after the fix.

def test_real_completion_event_shape_routes_to_session_channel():
    """A completion event in the real _move_to_finished() shape (no
    session_key) must still route to the registered WebUI session.

    Faithfully reproduces production: the terminal tool spawns a background
    process with session_key == webui_session_id (captured synchronously at
    spawn while the turn env is live). On exit, ProcessRegistry._move_to_
    finished() (1) moves the ProcessSession into _finished — which retains
    session_key — then (2) enqueues a completion event that DOES NOT carry a
    "session_key" field. The drain (_process_one) must recover the session_key
    from the still-tracked ProcessSession in the registry.
    """
    import time as _t

    from api import background_process as bp, config as cfg
    pytest.importorskip("tools.process_registry", reason="hermes-agent not installed")
    from tools.process_registry import process_registry, ProcessSession

    webui_sid = "sess-real-completion-shape"
    proc_id = "proc_realshape0001"

    ch = bp.get_or_create_session_channel(webui_sid)
    q = ch.subscribe()
    # streaming.py binds key == webui_session_id (register_process_session
    # called with (session_id, session_id)). HERMES_SESSION_KEY for the
    # spawned child therefore equals webui_sid, and the terminal tool stamps
    # that onto ProcessSession.session_key at spawn time.
    bp.register_process_session(webui_sid, webui_sid)

    # Simulate the finished process the registry retains in _finished after
    # _move_to_finished(): it carries the spawn-time session_key.
    finished = ProcessSession(
        id=proc_id,
        command="pytest -q",
        session_key=webui_sid,
        started_at=_t.time(),
        exited=True,
        exit_code=0,
        notify_on_complete=True,
    )
    with process_registry._lock:
        process_registry._finished[proc_id] = finished

    # Build the event EXACTLY as ProcessRegistry._move_to_finished() enqueues
    # it for notify_on_complete: type/session_id/command/exit_code/output.
    # Crucially: NO "session_key" key. This is the real wire shape that the
    # existing happy-path test (test_emit_when_idle_between_turns_...) never
    # exercises because it hand-injects session_key.
    evt = {
        "type": "completion",
        "session_id": proc_id,
        "command": "pytest -q",
        "exit_code": 0,
        "output": "1197 passed",
    }
    process_registry.completion_queue.put(evt)

    drained = process_registry.completion_queue.get(timeout=2.0)
    try:
        bp._process_one(drained)
        event_name, data = q.get(timeout=2.0)
        assert event_name == "bg_task_complete"
        assert data["session_id"] == webui_sid
        assert data["task_id"] == proc_id
        assert data.get("event_id"), "emitter must stamp event_id (Q4 contract)"
        # Per the Q1 minimal-payload trim settled in #2242, the SSE payload
        # no longer carries ``wakeup_prompt`` (or ``command`` / ``exit_code``).
        # The optional ``summary`` field is now the only human-readable
        # surface; when the synthetic wakeup body is available the emitter
        # derives a short first-line summary from it.
        assert "wakeup_prompt" not in data
        assert "command" not in data
        assert "exit_code" not in data
        summary = data.get("summary")
        if summary is not None:
            assert isinstance(summary, str)
            assert "IMPORTANT" in summary or "Background process" in summary
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(webui_sid, None)
        bp.unregister_process_session(webui_sid)
        with process_registry._lock:
            process_registry._finished.pop(proc_id, None)
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(webui_sid)
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(webui_sid, None)


# ===========================================================================
# Option Z (PIVOT): server-side wakeup is the PRIMARY mechanism.
#
# The drain thread starts the agent turn directly server-side
# (api/background_process._start_server_side_wakeup_turn →
# api.routes.start_session_turn) with NO browser round-trip. The per-session
# SSE channel is demoted to a pure live-view layer. These tests prove:
#   1. closed-tab (no SSE subscriber at all) STILL starts a server-side turn
#   2. active-turn defers (no double-start; PR #2279 next-turn drain handles it)
#   3. one wakeup per process_id (dedupe)
#   4. open tab still sees the live SSE frame (live-view unchanged)
# ===========================================================================


def _wait_for_wakeup(holder, timeout=3.0):
    """Thin wrapper preserving the legacy local name; the body lives in
    ``tests/_wakeup_helpers.py`` and is shared with
    ``test_wakeup_defer_race.py`` (Copilot PR #2971 r3305700944).
    """
    from tests._wakeup_helpers import wait_for_wakeup as _impl
    return _impl(holder, timeout=timeout)


def _install_fake_start_session_turn(monkeypatch, *, status=200):
    """Thin wrapper preserving the legacy local name; the body lives in
    ``tests/_wakeup_helpers.py`` and is shared with
    ``test_wakeup_defer_race.py`` (Copilot PR #2971 r3305700944).
    """
    from tests._wakeup_helpers import install_fake_start_session_turn as _impl
    return _impl(monkeypatch, status=status)


def test_server_side_wakeup_when_idle_no_tab(monkeypatch):
    """THE headline test: no SSE subscriber at all (closed tab / never opened).
    Pushing a completion must still start a server-side turn for the session.
    This is the closed-tab case browser-mediated wakeup could never serve.
    """
    from api import background_process as bp, config as cfg

    sid = "sess-optz-idle-notab"
    proc_id = "proc-optz-idle-1"

    holder = _install_fake_start_session_turn(monkeypatch)
    bp.register_process_session(sid, sid)
    try:
        # Deliberately NO ch.subscribe() and NO STREAMS entry: nobody is
        # listening. ACTIVE_RUNS has no row for sid → session is idle.
        assert bp.get_session_channel(sid) is None
        assert bp._session_has_active_turn(sid) is False

        evt = {
            "type": "completion",
            "session_id": proc_id,
            "session_key": sid,
            "command": "sleep 8",
            "exit_code": 0,
            "output": "done",
        }
        bp._process_one(evt)

        assert _wait_for_wakeup(holder), (
            "server-side wakeup turn was NOT started for an idle session with "
            "no tab — closed-tab case is broken"
        )
        assert len(holder["calls"]) == 1
        call = holder["calls"][0]
        assert call["session_id"] == sid
        assert call["source"] == "process_wakeup"
        assert call["message"].startswith("[IMPORTANT: Background process")
    finally:
        bp.unregister_process_session(sid)
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


def test_server_side_wakeup_deferred_when_turn_active(monkeypatch):
    """A foreground turn is active (ACTIVE_RUNS has a row for the session) →
    the drain must NOT start a second turn. The PENDING_BG_TASK_COMPLETIONS
    marker is left for PR #2279's next-turn drain.
    """
    from api import background_process as bp, config as cfg

    sid = "sess-optz-active-defer"
    proc_id = "proc-optz-active-1"
    stream_id = "stream-optz-active-1"

    holder = _install_fake_start_session_turn(monkeypatch)
    bp.register_process_session(sid, sid)
    cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}
    try:
        assert bp._session_has_active_turn(sid) is True

        evt = {
            "type": "completion",
            "session_id": proc_id,
            "session_key": sid,
            "command": "sleep 8",
            "exit_code": 0,
            "output": "done",
        }
        bp._process_one(evt)

        # Give any (incorrectly spawned) runner thread a chance to fire.
        fired = holder["event"].wait(timeout=1.0)
        assert fired is False, (
            "server-side wakeup must DEFER when a turn is active — it "
            "double-started a turn"
        )
        assert holder["calls"] == []
        # Marker must remain so the next-turn drain delivers it.
        assert sid in cfg.PENDING_BG_TASK_COMPLETIONS
    finally:
        cfg.ACTIVE_RUNS.pop(stream_id, None)
        bp.unregister_process_session(sid)
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


def test_wakeup_dedupe_once_per_process(monkeypatch):
    """The same process_id delivered twice (kill_process racing the reader
    thread) must wake the agent at most once.
    """
    from api import background_process as bp, config as cfg

    sid = "sess-optz-dedupe"
    proc_id = "proc-optz-dedupe-1"

    holder = _install_fake_start_session_turn(monkeypatch)
    bp.register_process_session(sid, sid)
    try:
        evt = {
            "type": "completion",
            "session_id": proc_id,
            "session_key": sid,
            "command": "sleep 8",
            "exit_code": 0,
            "output": "done",
        }
        bp._process_one(evt)
        assert _wait_for_wakeup(holder)
        # Second delivery of the SAME process_id — must be deduped before the
        # server-side wakeup branch.
        bp._process_one(dict(evt))
        time.sleep(0.5)
        assert len(holder["calls"]) == 1, (
            "duplicate completion for the same process_id woke the agent twice"
        )
    finally:
        bp.unregister_process_session(sid)
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


def test_open_tab_sees_live_stream(monkeypatch):
    """Live-view still works: with a subscribed per-session SSE channel, the
    bg_task_complete frame is still delivered to the tab (so the open tab can
    render the server-initiated turn live). Server-side wakeup is additive —
    it does not remove the SSE emit.
    """
    from api import background_process as bp, config as cfg

    sid = "sess-optz-liveview"
    proc_id = "proc-optz-liveview-1"

    holder = _install_fake_start_session_turn(monkeypatch)
    ch = bp.get_or_create_session_channel(sid)
    q = ch.subscribe()
    bp.register_process_session(sid, sid)
    try:
        evt = {
            "type": "completion",
            "session_id": proc_id,
            "session_key": sid,
            "command": "sleep 8",
            "exit_code": 0,
            "output": "done",
        }
        bp._process_one(evt)

        # Live-view: the open tab still receives the SSE frame.
        event_name, data = q.get(timeout=2.0)
        assert event_name == "bg_task_complete"
        assert data["session_id"] == sid
        assert data["task_id"] == proc_id
        assert data.get("event_id"), "emitter must stamp event_id (Q4 contract)"

        # AND the server-side wakeup still started (no active turn here).
        assert _wait_for_wakeup(holder)
        assert holder["calls"][0]["session_id"] == sid
    finally:
        ch.unsubscribe(q)
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(sid, None)
        bp.unregister_process_session(sid)
        cfg.PENDING_BG_TASK_COMPLETIONS.discard(sid)
        cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)


# ---------------------------------------------------------------------------
# event_id contract surface — backend emitter must stamp every event
# ---------------------------------------------------------------------------


def test_backend_emitter_stamps_event_id_on_every_bg_task_complete():
    """Per the #2242 Q4 reply: every bg_task_complete emit carries an
    event_id; the consumer's ring-buffer dedupe is keyed on it. Source-grep
    the payload builder to confirm event_id is stamped."""
    src = (REPO_ROOT / "api" / "background_process.py").read_text()
    # Locate the canonical payload builder and confirm event_id is in the dict.
    fn_ix = src.index("def _build_payload")
    fn_src = src[fn_ix:fn_ix + 4000]
    assert '"event_id"' in fn_src or "'event_id'" in fn_src, (
        "payload builder must stamp event_id on every bg_task_complete payload"
    )
    assert "uuid.uuid4().hex" in fn_src, (
        "event_id should be a per-emit uuid hex (R2 §Q1)"
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Regression (Greptile P1 r3371970195): a CLOSED (readyState===2) session
# EventSource must trigger a FRESH reconnect, not silently die.
# ---------------------------------------------------------------------------
#
# Bug: startSessionStream's top guard is
#   `if (_sessionStreamSessionId === sid && _sessionEventSource) return;`
# When onerror fired with readyState === 2 (permanent close: server 4xx/204,
# browser retry-exhaustion), the old code scheduled the reconnect timer WITHOUT
# clearing _sessionEventSource. The still-non-null (but CLOSED) object made the
# guard short-circuit, so stopSessionStream() was never reached and no new
# EventSource was ever created — the session stream stayed dead until the user
# navigated away and back, dropping all bg_task_complete notifications meanwhile.
#
# Fix: in onerror, when es.readyState === 2 and es is still the active source,
# close it and null _sessionEventSource BEFORE arming the reconnect timer, so
# the deferred startSessionStream() passes its guard, runs stopSessionStream(),
# and builds a fresh EventSource. The `_sessionEventSource === es` identity
# check prevents a stale onerror from a superseded stream stomping a newer live
# connection.

def test_session_stream_onerror_clears_closed_source_so_reconnect_proceeds():
    js = (REPO_ROOT / "static" / "messages.js").read_text()
    # Isolate the onerror handler body within startSessionStream.
    fn_ix = js.index("function startSessionStream")
    err_ix = js.index("es.onerror", fn_ix)
    onerror_src = js[err_ix:err_ix + 1600]

    # Must only act on the permanently-CLOSED state.
    assert "es.readyState === 2" in onerror_src
    # Identity-guard so a stale onerror can't stomp a newer live connection.
    assert "_sessionEventSource === es" in onerror_src
    # Must drop the dead reference (and close it) BEFORE arming the timer so
    # startSessionStream's "already connected" guard no longer short-circuits.
    assert "_sessionEventSource = null;" in onerror_src
    assert "es.close()" in onerror_src

    # Ordering: the null-out must precede the setTimeout that re-opens.
    null_pos = onerror_src.index("_sessionEventSource = null;")
    timer_pos = onerror_src.index("setTimeout(")
    assert null_pos < timer_pos, (
        "must null the closed EventSource BEFORE arming the reconnect timer, "
        "else startSessionStream's guard short-circuits and the stream stays dead"
    )


# ---------------------------------------------------------------------------
# Regression (Greptile P1 r3377162160): session stream must NOT stay dead
# after a failed / early-returned loadSession (sessions.js:754 re-arm).
# ---------------------------------------------------------------------------
#
# Bug: loadSession() tears down the live per-session SSE unconditionally at the
# top — `if (typeof stopSessionStream==='function') stopSessionStream();` — but
# only RE-arms it on the success path (`startSessionStream(S.session.session_id)`
# near the end). Every early-return exit leaves the session the user is still
# viewing with _sessionEventSource === null and no path back to a live stream:
#   - fetch error (network / non-404)          → returns after stopSessionStream
#   - api() returned undefined (401 redirect)  → returns
#   - stale-load race (_loadingSessionId !== sid after data) → returns
#   - same-session no-op guard (currentSid===sid && !forceReload) → returns
#     BEFORE the teardown, but a *prior* failed load already nulled the source,
#     and re-selecting the same session would otherwise no-op forever.
# Net effect: bg_task_complete delivery silently dies until a full page reload.
#
# Fix: an idempotent helper `_rearmActiveSessionStream()` calls
# startSessionStream(S.session.session_id) for whatever session is actually on
# screen, invoked on the early-return paths AND the same-session no-op guard.
# startSessionStream() is idempotent (its top guard
# `_sessionStreamSessionId === sid && _sessionEventSource` no-ops when already
# live) so the success path is never double-armed. The fetch-error path keeps
# its own pre-existing guarded restart (`_selfHealedCurrent` check) instead of
# the helper, because only there can the current session have just self-healed
# away — re-arming a 404'd/deleted session_id would spin the SSE reconnect loop
# against a dead session. Mirrors the #2979 messages.js reconnect fix.

def test_load_session_rearms_stream_on_every_early_return():
    js = (REPO_ROOT / "static" / "sessions.js").read_text()

    # The idempotent re-arm helper must exist and arm the on-screen session.
    assert "function _rearmActiveSessionStream(" in js, (
        "expected a dedicated idempotent re-arm helper"
    )
    helper_ix = js.index("function _rearmActiveSessionStream(")
    helper_src = js[helper_ix:helper_ix + 400]
    assert "S.session" in helper_src and "startSessionStream(" in helper_src, (
        "helper must (re)arm startSessionStream for the currently-shown S.session"
    )

    # Isolate the loadSession body.
    fn_ix = js.index("async function loadSession(")
    body = js[fn_ix:fn_ix + 12000]

    # The unconditional teardown must still be there (this is what creates the
    # dead-stream window the re-arm closes).
    assert "stopSessionStream()" in body

    # Post-teardown early-return paths must re-arm. The helper covers the
    # same-session guard, the undefined-data (401) exit, and the stale-response
    # exit — 3 helper call sites is the floor. (The fetch-error path uses its
    # own `_selfHealedCurrent`-guarded restart, asserted separately below; the
    # rapid-switch post-draft handoff is owned by the newer load's own arming.)
    assert js.count("_rearmActiveSessionStream()") >= 3, (
        "each failed/early-return loadSession exit after stopSessionStream() "
        "must re-arm the on-screen session's stream, else bg_task_complete "
        "delivery dies until a page reload (Greptile P1 r3377162160)"
    )

    # Specifically: the same-session no-op guard must be PRECEDED by a re-arm
    # so re-selecting a session whose stream a prior failed load killed revives
    # it. The re-arm sits before the guard (not inside a wrapping block) so the
    # guard stays the exact one-liner other tests assert; it's idempotent so
    # the real-switch path is unaffected.
    guard_ix = body.index("currentSid===sid && !forceReload && !_loadingSessionId")
    pre_guard = body[max(0, guard_ix - 600):guard_ix]
    assert "_rearmActiveSessionStream()" in pre_guard, (
        "a re-arm must run before the same-session no-op guard so a "
        "previously-killed stream is revived on re-selecting the session"
    )

    # The fetch-error catch must restart the stream for the on-screen session,
    # but guarded against the self-healed-current (404'd) case so it never
    # spins the reconnect loop against a dead session_id.
    catch_ix = body.index("const _selfHealedCurrent")
    catch_src = body[catch_ix:catch_ix + 1400]
    assert "!_selfHealedCurrent" in catch_src and "startSessionStream(currentSid)" in catch_src, (
        "fetch-error path must restart the on-screen stream, guarded against "
        "the self-healed-current (deleted/404) session"
    )


def test_session_sse_stream_unsubscribes_on_header_write_failure():
    """Deep-review fix (Codex): in _handle_session_sse_stream the subscriber
    slot is acquired by subscribe_to_session_channel BEFORE the SSE headers are
    written. The header writes (send_response/send_header/end_headers) touch the
    socket and can raise a client-disconnect error. If that happened OUTSIDE the
    try/finally, ch.unsubscribe(q) would be skipped and — because
    reaper_should_collect refuses to collect a channel with sub_count>0 — the
    channel would zombie forever. Pin that the subscribe and the header setup
    both sit inside the single try whose finally unsubscribes.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
    i = src.find("def _handle_session_sse_stream(")
    assert i != -1, "handler not found"
    j = src.find("\ndef ", i + 1)
    body = src[i:j]

    sub_ix = body.find("subscribe_to_session_channel(")
    assert sub_ix != -1, "subscribe call not found"
    try_ix = body.find("try:", sub_ix)
    end_headers_ix = body.find("end_headers()", sub_ix)
    finally_ix = body.find("finally:", sub_ix)
    unsub_ix = body.find("ch.unsubscribe(q)", finally_ix if finally_ix != -1 else sub_ix)

    # Order must be: subscribe → try → end_headers (inside try) → finally → unsubscribe.
    assert try_ix != -1 and finally_ix != -1 and unsub_ix != -1
    assert sub_ix < try_ix < end_headers_ix < finally_ix < unsub_ix, (
        "header setup must run INSIDE the try/finally that unsubscribes — "
        "a header-write disconnect must not leak a SessionChannel subscriber"
    )
