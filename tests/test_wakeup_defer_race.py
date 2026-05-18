"""Defer-path wakeup race: a fast background task that completes WHILE a turn
is tearing down must still wake an autonomous agent.

Root cause (proven by source, not speculation):
  - api/background_process.py:_process_one defer branch — when a completion
    arrives and _session_has_active_turn(session_id) is True (ACTIVE_RUNS has
    a row), Option Z CANNOT start a turn (start_session_turn would 409). Before
    this fix it only logged + left a bare PENDING_PROCESS_COMPLETIONS session
    flag; the wakeup_prompt was DISCARDED.
  - The only consumer of that bare flag was the PR #2279 next-turn drain
    (api/streaming._drain_webui_process_notifications, called at
    streaming.py:3445 inside the turn pipeline). It reads completion_queue —
    which the Option Z drain thread already emptied — and is gated by
    PROCESS_COMPLETE_EVENTS_SEEN / registry _completion_consumed (both set in
    _process_one BEFORE the defer). So even a user turn could not recover it.
  - For an AUTONOMOUS agent there is NO next user turn, so the deferred wakeup
    was lost forever. A SLOW task (5s) completes AFTER teardown finished →
    idle path → fires (Test A passed). A FAST task (2s) completes INSIDE the
    teardown window (between "agent finished output" and ACTIVE_RUNS cleared)
    → defer → lost (Test B failed). Exactly matches A-success / B-fail.

The fix persists the prompt at defer time (DEFERRED_PROCESS_WAKEUPS) and a
turn-teardown idle-hook (drain_deferred_wakeups_for_session, invoked from
streaming.py right after unregister_active_run) redelivers it once the session
goes idle — symmetric with the idle branch. claim_deferred_wakeups pops
atomically, so delivery is exactly-once (no double-fire, no wakeup loop).

These tests simulate the drain-thread + teardown sequence directly (no live
server needed — precedent t_9f0184cf), monkeypatching start_session_turn the
same way tests/test_session_channel_option_x.py does.
"""
from __future__ import annotations

import queue
import threading
import types


# --------------------------------------------------------------------------
# Fakes / fixtures (mirrors test_process_complete_ab_coexistence +
# test_session_channel_option_x patterns)
# --------------------------------------------------------------------------


class _FakeProcessRegistry:
    """Minimal stand-in for tools.process_registry.process_registry."""

    def __init__(self):
        self._lock = threading.Lock()
        self._completion_consumed: set[str] = set()
        self.completion_queue: queue.Queue = queue.Queue()
        self._procs: dict[str, types.SimpleNamespace] = {}

    def register(self, process_id: str, session_key: str) -> None:
        self._procs[process_id] = types.SimpleNamespace(session_key=session_key)

    def get(self, process_id: str):
        return self._procs.get(process_id)

    def is_completion_consumed(self, process_id: str) -> bool:
        with self._lock:
            return process_id in self._completion_consumed


def _install_fake_registry(monkeypatch, fake):
    import sys

    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = fake
    tools_mod = sys.modules.setdefault("tools", types.ModuleType("tools"))
    tools_mod.process_registry = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.process_registry", mod)


def _install_fake_start_session_turn(monkeypatch, *, status=200):
    """Patch api.routes.start_session_turn to record calls instead of running
    a real agent turn. The drain helper does ``from api.routes import
    start_session_turn`` inside a daemon thread, so patching the attribute on
    the api.routes module is what the thread resolves at call time.
    """
    import api.routes as _routes

    holder = {"calls": [], "event": threading.Event()}

    def _fake(session_id, message, *, source="process_wakeup"):
        holder["calls"].append(
            {"session_id": session_id, "message": message, "source": source}
        )
        holder["event"].set()
        return {"stream_id": "fake-stream", "session_id": session_id, "_status": status}

    monkeypatch.setattr(_routes, "start_session_turn", _fake, raising=True)
    return holder


def _wait_for_wakeup(holder, timeout=3.0):
    return holder["event"].wait(timeout=timeout)


def _reset_cfg_state():
    from api import config as _cfg

    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        _cfg.PROCESS_SESSION_INDEX.clear()
    _cfg.PENDING_PROCESS_COMPLETIONS.clear()
    _cfg.PROCESS_COMPLETE_EVENTS_SEEN.clear()
    with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
        _cfg.DEFERRED_PROCESS_WAKEUPS.clear()
    with _cfg.STREAMS_LOCK:
        _cfg.STREAMS.clear()
    if hasattr(_cfg, "ACTIVE_RUNS"):
        with _cfg.ACTIVE_RUNS_LOCK:
            _cfg.ACTIVE_RUNS.clear()


def _completion_evt(process_id: str, session_key: str) -> dict:
    return {
        "type": "completion",
        "session_id": process_id,
        "session_key": session_key,
        "command": "sleep 2",
        "exit_code": 0,
        "output": "done",
    }


# --------------------------------------------------------------------------
# Test 1 — THE headline acceptance: completion during teardown still wakes
# (the autonomous-agent, no-next-user-turn case == the Test B scenario)
# --------------------------------------------------------------------------


def test_completion_during_turn_teardown_still_wakes(monkeypatch):
    """Session has an ACTIVE_RUN → a process completes → _process_one defers
    (marker persisted, NO immediate turn). Then the turn tears down
    (unregister_active_run) and the teardown idle-hook fires the deferred
    wakeup exactly once. This is the Test B (sleep 2) scenario.
    """
    from api import background_process as bp, config as cfg

    fake = _FakeProcessRegistry()
    fake.register("proc-fast-1", "sess-teardown")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()
    holder = _install_fake_start_session_turn(monkeypatch)

    sid = "sess-teardown"
    stream_id = "stream-teardown-1"
    bp.register_process_session(sid, sid)
    try:
        # A turn is active (mid-teardown window: agent finished output but
        # ACTIVE_RUNS not yet cleared).
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}
        assert bp._session_has_active_turn(sid) is True

        # Fast bg task completes INSIDE the teardown window → defer.
        bp._process_one(_completion_evt("proc-fast-1", sid))

        # Deferred, NOT fired: no turn started, prompt persisted.
        assert holder["event"].wait(timeout=0.8) is False
        assert holder["calls"] == []
        assert sid in cfg.DEFERRED_PROCESS_WAKEUPS
        assert cfg.DEFERRED_PROCESS_WAKEUPS[sid][0]["process_id"] == "proc-fast-1"

        # Turn teardown: unregister_active_run clears the ACTIVE_RUNS row
        # (this is exactly what streaming.py does under ACTIVE_RUNS_LOCK),
        # then the teardown idle-hook runs.
        cfg.unregister_active_run(stream_id)
        assert bp._session_has_active_turn(sid) is False
        started = bp.drain_deferred_wakeups_for_session(sid)
        assert started == 1

        assert _wait_for_wakeup(holder), (
            "deferred wakeup was NOT redelivered at turn teardown — the "
            "autonomous-agent fast-bg-task case is still broken"
        )
        assert len(holder["calls"]) == 1
        call = holder["calls"][0]
        assert call["session_id"] == sid
        assert call["source"] == "process_wakeup"
        assert call["message"].startswith("[IMPORTANT: Background process")
        # Claimed → nothing left to re-deliver.
        assert sid not in cfg.DEFERRED_PROCESS_WAKEUPS
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.pop(stream_id, None)
        bp.unregister_process_session(sid)
        _reset_cfg_state()


# --------------------------------------------------------------------------
# Test 2 — idle path unchanged: fires once, no regression, the new teardown
# hook does not double-fire it (the Test A / sleep 5 path)
# --------------------------------------------------------------------------


def test_idle_completion_still_fires_once(monkeypatch):
    """No ACTIVE_RUN → _process_one fires the server-side wakeup immediately
    (Option Z idle branch, the Test A path). Nothing is deferred, so the new
    turn-teardown hook is a pure no-op — total deliveries stays exactly 1.
    """
    from api import background_process as bp, config as cfg

    fake = _FakeProcessRegistry()
    fake.register("proc-idle-1", "sess-idle")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()
    holder = _install_fake_start_session_turn(monkeypatch)

    sid = "sess-idle"
    bp.register_process_session(sid, sid)
    try:
        assert bp._session_has_active_turn(sid) is False

        bp._process_one(_completion_evt("proc-idle-1", sid))
        assert _wait_for_wakeup(holder), "idle path regressed — wakeup not fired"
        assert len(holder["calls"]) == 1
        # Idle branch did NOT persist anything.
        assert sid not in cfg.DEFERRED_PROCESS_WAKEUPS

        # The wakeup turn itself ends and tears down → its teardown re-runs
        # the idle-hook. It must find nothing and NOT double-fire.
        started = bp.drain_deferred_wakeups_for_session(sid)
        assert started == 0
        assert len(holder["calls"]) == 1, (
            "the teardown hook double-fired an idle-path wakeup"
        )
    finally:
        bp.unregister_process_session(sid)
        _reset_cfg_state()


# --------------------------------------------------------------------------
# Test 3 — idempotent with the PR #2279 next-turn drain: a user turn that
# DOES come must not also deliver (shared SEEN / _completion_consumed gate)
# --------------------------------------------------------------------------


def test_next_user_turn_drain_and_teardown_hook_dont_double_fire(monkeypatch):
    """If a user turn DOES come, the next-turn drain
    (_drain_webui_process_notifications) must NOT also deliver the deferred
    completion: _process_one set PROCESS_COMPLETE_EVENTS_SEEN AND the registry
    _completion_consumed marker BEFORE the defer, and it consumed the
    completion_queue event, so the next-turn drain has nothing to fire. The
    teardown idle-hook then delivers it exactly once. Total deliveries == 1.
    """
    from api import background_process as bp, config as cfg
    from api import streaming as st

    fake = _FakeProcessRegistry()
    fake.register("proc-shared-1", "sess-shared")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()
    holder = _install_fake_start_session_turn(monkeypatch)

    sid = "sess-shared"
    stream_id = "stream-shared-1"
    bp.register_process_session(sid, sid)
    try:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}

        bp._process_one(_completion_evt("proc-shared-1", sid))
        # Shared dedupe contract: _process_one marked it seen + registry-
        # consumed before deferring.
        assert "proc-shared-1" in cfg.PROCESS_COMPLETE_EVENTS_SEEN[sid]
        assert fake.is_completion_consumed("proc-shared-1")
        assert sid in cfg.DEFERRED_PROCESS_WAKEUPS

        # A user turn comes: the next-turn drain runs. Even if a duplicate
        # event were re-queued (kill_process race), the SEEN + consumed gate
        # makes it a no-op — it must NOT deliver the deferred wakeup.
        fake.completion_queue.put(_completion_evt("proc-shared-1", sid))
        notifications = st._drain_webui_process_notifications(sid)
        assert notifications == [], (
            "next-turn drain double-delivered a completion the defer path owns"
        )

        # That user turn ends → its teardown fires the deferred wakeup ONCE.
        cfg.unregister_active_run(stream_id)
        started = bp.drain_deferred_wakeups_for_session(sid)
        assert started == 1
        assert _wait_for_wakeup(holder)
        assert len(holder["calls"]) == 1, (
            "deferred wakeup delivered more than once across next-turn drain "
            "+ teardown hook"
        )
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.pop(stream_id, None)
        bp.unregister_process_session(sid)
        _reset_cfg_state()


# --------------------------------------------------------------------------
# Test 4 — no wakeup loop: the wakeup turn's own teardown does not re-trigger
# a wakeup for the same process_id
# --------------------------------------------------------------------------


def test_no_wakeup_loop(monkeypatch):
    """The wakeup turn started by the teardown hook itself ends and tears
    down → its teardown re-runs drain_deferred_wakeups_for_session. The atomic
    claim (DEFERRED_PROCESS_WAKEUPS.pop) already removed the entry, so the
    second drain finds nothing → no infinite wakeup loop.
    """
    from api import background_process as bp, config as cfg

    fake = _FakeProcessRegistry()
    fake.register("proc-loop-1", "sess-loop")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()
    holder = _install_fake_start_session_turn(monkeypatch)

    sid = "sess-loop"
    stream_id = "stream-loop-1"
    bp.register_process_session(sid, sid)
    try:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS[stream_id] = {"session_id": sid}
        bp._process_one(_completion_evt("proc-loop-1", sid))
        assert sid in cfg.DEFERRED_PROCESS_WAKEUPS

        # First teardown: claims + fires once.
        cfg.unregister_active_run(stream_id)
        assert bp.drain_deferred_wakeups_for_session(sid) == 1
        assert _wait_for_wakeup(holder)
        assert len(holder["calls"]) == 1
        assert sid not in cfg.DEFERRED_PROCESS_WAKEUPS

        # The wakeup turn itself runs and tears down → second drain. It must
        # find NOTHING (already claimed) and start NO further turn.
        for _ in range(3):
            assert bp.drain_deferred_wakeups_for_session(sid) == 0
        assert len(holder["calls"]) == 1, (
            "wakeup loop: the wakeup turn's own teardown re-fired the same "
            "process_id"
        )
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.pop(stream_id, None)
        bp.unregister_process_session(sid)
        _reset_cfg_state()


# --------------------------------------------------------------------------
# Test 5 — multi-stream / cancel-reconnect guard: only fire when the session
# is TRULY idle (the just-ended stream was the last ACTIVE_RUN for the sid)
# --------------------------------------------------------------------------


def test_multistream_guard_only_fires_when_truly_idle(monkeypatch):
    """A cancel/reconnect leaves a SECOND active stream for the same session.
    When the first stream tears down the session is NOT idle → the deferred
    marker must be left intact (no fire). Only when the last active stream
    tears down does the hook claim + fire, exactly once.
    """
    from api import background_process as bp, config as cfg

    fake = _FakeProcessRegistry()
    fake.register("proc-multi-1", "sess-multi")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()
    holder = _install_fake_start_session_turn(monkeypatch)

    sid = "sess-multi"
    stream_a = "stream-multi-a"
    stream_b = "stream-multi-b"
    bp.register_process_session(sid, sid)
    try:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS[stream_a] = {"session_id": sid}
            cfg.ACTIVE_RUNS[stream_b] = {"session_id": sid}
        bp._process_one(_completion_evt("proc-multi-1", sid))
        assert sid in cfg.DEFERRED_PROCESS_WAKEUPS

        # First stream tears down — second is still active → NOT idle.
        cfg.unregister_active_run(stream_a)
        assert bp._session_has_active_turn(sid) is True
        assert bp.drain_deferred_wakeups_for_session(sid) == 0
        assert holder["calls"] == []
        # Marker retained for the later teardown.
        assert sid in cfg.DEFERRED_PROCESS_WAKEUPS

        # Last stream tears down — now truly idle → fire exactly once.
        cfg.unregister_active_run(stream_b)
        assert bp._session_has_active_turn(sid) is False
        assert bp.drain_deferred_wakeups_for_session(sid) == 1
        assert _wait_for_wakeup(holder)
        assert len(holder["calls"]) == 1
        assert sid not in cfg.DEFERRED_PROCESS_WAKEUPS
    finally:
        with cfg.ACTIVE_RUNS_LOCK:
            cfg.ACTIVE_RUNS.pop(stream_a, None)
            cfg.ACTIVE_RUNS.pop(stream_b, None)
        bp.unregister_process_session(sid)
        _reset_cfg_state()
