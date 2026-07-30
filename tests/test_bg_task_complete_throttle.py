"""T3 throttle tests for bg_task_complete emits.

The backend emits a canonical ``bg_task_complete`` SSE frame and a temporary
``process_complete`` alias for the same payload. T3 adds a per-session 1s
coalesce gate around that dual emit so rapid completion bursts do not flood a
live WebUI tab; the deferred emit must carry the latest payload from the burst.
"""
from __future__ import annotations

# The fake process-registry stub + its installer were duplicated verbatim in
# three bg_task_complete suites; they now live once in tests/_wakeup_helpers.py
# (Greptile review on PR #2979). Import under the legacy local names so the
# rest of this module is unchanged.
from tests._wakeup_helpers import FakeProcessRegistry as _FakeProcessRegistry
from tests._wakeup_helpers import install_fake_registry as _install_fake_registry


def _reset_state(bp):
    from api import config as _cfg

    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        _cfg.PROCESS_SESSION_INDEX.clear()
    _cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
    if hasattr(_cfg, "DEFERRED_PROCESS_WAKEUPS"):
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            _cfg.DEFERRED_PROCESS_WAKEUPS.clear()
    with _cfg.STREAMS_LOCK:
        _cfg.STREAMS.clear()
    if hasattr(_cfg, "ACTIVE_RUNS"):
        with _cfg.ACTIVE_RUNS_LOCK:
            _cfg.ACTIVE_RUNS.clear()
    # T3 module-level throttle state.
    if hasattr(bp, "_LAST_EMIT_TS"):
        bp._LAST_EMIT_TS.clear()
    if hasattr(bp, "_PENDING_EMIT_PAYLOADS"):
        bp._PENDING_EMIT_PAYLOADS.clear()
    if hasattr(bp, "_PENDING_EMIT_TIMERS"):
        bp._PENDING_EMIT_TIMERS.clear()


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ManualTimer:
    def __init__(self, timers: list["_ManualTimer"], delay: float, callback, args=()):
        self.timers = timers
        self.delay = delay
        self.callback = callback
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True
        self.timers.append(self)

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(*self.args)


def _install_emit_harness(monkeypatch, *, session_id: str = "sess-throttle"):
    from api import background_process as bp

    fake = _FakeProcessRegistry()
    _install_fake_registry(monkeypatch, fake)
    _reset_state(bp)
    bp.register_process_session(session_id, session_id)

    emits: list[tuple[str, dict]] = []

    def _capture_emit(sid: str, event: str, data: dict) -> int:
        emits.append((event, dict(data)))
        return 1

    monkeypatch.setattr(bp, "_emit_to_session_streams", _capture_emit)
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", lambda sid, prompt: None)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: False)

    clock = _FakeClock()
    monkeypatch.setattr(bp.time, "time", clock.time)

    timers: list[_ManualTimer] = []

    def _timer_factory(delay, callback, args=(), kwargs=None):
        assert kwargs in (None, {})
        return _ManualTimer(timers, delay, callback, args)

    monkeypatch.setattr(bp.threading, "Timer", _timer_factory)
    return bp, fake, emits, clock, timers


def _process_completion(bp, fake, task_id: str, session_id: str = "sess-throttle") -> None:
    fake.register(task_id, session_id)
    bp._process_one(
        {
            "type": "completion",
            "session_id": task_id,
            "session_key": session_id,
            "command": f"echo {task_id}",
            "exit_code": 0,
            "output": task_id,
        }
    )


def _canonical_payloads(emits: list[tuple[str, dict]]) -> list[dict]:
    return [payload for event, payload in emits if event == "bg_task_complete"]


def test_rapid_bg_task_complete_emits_coalesce_to_immediate_plus_one_deferred(monkeypatch):
    bp, fake, emits, _clock, timers = _install_emit_harness(monkeypatch)

    for idx in range(10):
        _process_completion(bp, fake, f"task-{idx}")

    # First emit is immediate. The rest of the burst is represented by one
    # deferred timer; cancelled timer instances may remain in the manual list.
    assert len(_canonical_payloads(emits)) == 1
    live_timers = [timer for timer in timers if not timer.cancelled]
    assert len(live_timers) == 1

    live_timers[0].fire()

    canonical_payloads = _canonical_payloads(emits)
    assert len(canonical_payloads) <= 2
    assert [payload["task_id"] for payload in canonical_payloads] == ["task-0", "task-9"]


def test_bg_task_complete_emits_two_seconds_apart_all_fire(monkeypatch):
    bp, fake, emits, clock, timers = _install_emit_harness(monkeypatch)

    for idx in range(3):
        _process_completion(bp, fake, f"spaced-{idx}")
        clock.advance(2.0)

    assert [payload["task_id"] for payload in _canonical_payloads(emits)] == [
        "spaced-0",
        "spaced-1",
        "spaced-2",
    ]
    assert not [timer for timer in timers if not timer.cancelled]


def test_coalesced_bg_task_complete_payload_replace_uses_latest_payload(monkeypatch):
    bp, fake, emits, _clock, timers = _install_emit_harness(monkeypatch)

    _process_completion(bp, fake, "first")
    _process_completion(bp, fake, "middle")
    _process_completion(bp, fake, "latest")

    live_timers = [timer for timer in timers if not timer.cancelled]
    assert len(live_timers) == 1
    live_timers[0].fire()

    canonical_payloads = _canonical_payloads(emits)
    assert [payload["task_id"] for payload in canonical_payloads] == ["first", "latest"]
    assert canonical_payloads[-1]["summary"].endswith("latest completed (exit_code=0).")


def test_outside_window_arrival_flushes_immediately_even_with_pending_timer(monkeypatch):
    bp, fake, emits, clock, timers = _install_emit_harness(monkeypatch)

    _process_completion(bp, fake, "first")
    clock.advance(0.2)
    _process_completion(bp, fake, "pending")
    assert [payload["task_id"] for payload in _canonical_payloads(emits)] == ["first"]
    assert len([timer for timer in timers if not timer.cancelled]) == 1

    clock.advance(1.1)
    _process_completion(bp, fake, "outside-window")

    assert [payload["task_id"] for payload in _canonical_payloads(emits)] == [
        "first",
        "outside-window",
    ]
    assert not [timer for timer in timers if not timer.cancelled]
