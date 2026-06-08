"""Integration tests: the merged upstream PR #2279 (next-turn drain, A) +
our-original Option B SSE/server-side drain coexist without duplicating
wakeups for the same background process_id.

These tests verify the shared dedupe contract via the REAL merged upstream
key — process_registry._completion_consumed (checked by
process_registry.is_completion_consumed()):
- If B's drain fires first (proactive case), it marks the registry
  consumed-marker so A's next-turn drain skips the same process_id.
- If A's (real merged #2279) drain fires first (SSE-disconnected case), it
  marks the same registry consumed-marker so B's drain early-returns.

api.config.BG_TASK_COMPLETE_EVENTS_SEEN remains as B's own private
secondary dedupe (duplicate enqueue within this module) but is NOT the
cross-A/B contract — the real merged #2279 never writes it.

The two paths run in *different* hot paths (background thread vs. agent turn
start) but share process_registry._completion_consumed, so a wakeup can only
happen once.
"""
from __future__ import annotations

import threading

import pytest

# The fake process-registry stub + its installer were duplicated verbatim in
# three bg_task_complete suites; they now live once in tests/_wakeup_helpers.py
# (Greptile review on PR #2979). Import under the legacy local names so the
# rest of this module is unchanged. ``threading`` is still imported above for
# the _RenamedRegistry stub further down.
from tests._wakeup_helpers import FakeProcessRegistry as _FakeProcessRegistry
from tests._wakeup_helpers import install_fake_registry as _install_fake_registry


def _reset_cfg_state():
    from api import config as _cfg
    from api import background_process as bp
    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        _cfg.PROCESS_SESSION_INDEX.clear()
    _cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
    with _cfg.STREAMS_LOCK:
        _cfg.STREAMS.clear()
    if hasattr(_cfg, "ACTIVE_RUNS"):
        with _cfg.ACTIVE_RUNS_LOCK:
            _cfg.ACTIVE_RUNS.clear()
    if hasattr(bp, "_LAST_EMIT_TS"):
        bp._LAST_EMIT_TS.clear()
    if hasattr(bp, "_PENDING_EMIT_PAYLOADS"):
        bp._PENDING_EMIT_PAYLOADS.clear()
    if hasattr(bp, "_PENDING_EMIT_TIMERS"):
        bp._PENDING_EMIT_TIMERS.clear()


def test_b_sse_first_then_a_drain_skips_same_process_id(monkeypatch):
    """B emits SSE for process_id=p1, then user types a new turn — A must skip p1."""
    fake = _FakeProcessRegistry()
    fake.register("p1", "sess-1")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()

    from api import background_process as bp
    from api import streaming as st
    from api import config as _cfg

    # Map session_key -> WebUI session_id
    bp.register_process_session("sess-1", "sess-1")

    evt = {
        "type": "completion",
        "session_id": "p1",
        "session_key": "sess-1",
        "command": "sleep 1",
        "exit_code": 0,
        "output": "done",
    }
    # B path: process the event
    bp._process_one(evt)

    # B must have marked the (session, process) seen and registry-consumed
    assert "p1" in _cfg.BG_TASK_COMPLETE_EVENTS_SEEN["sess-1"]
    assert fake.is_completion_consumed("p1")

    # Now simulate A's next-turn drain. Put a *new* event onto the queue for the
    # same process_id (e.g. a kill_process race). A must skip because B already
    # delivered.
    fake.completion_queue.put(evt)
    notifications = st._drain_webui_process_notifications("sess-1")
    assert notifications == [], "A must NOT re-fire when B already woke the agent for p1"


def test_a_drain_first_marks_seen_so_b_would_skip(monkeypatch):
    """A (the REAL merged upstream #2279 next-turn drain) drains and wakes the
    agent; later B's queue read of the same id is a no-op because the SHARED
    upstream dedupe key (process_registry._completion_consumed) already
    contains it.

    Re-pointed for the rebase: the real merged #2279 drain dedupes ONLY via
    process_registry.is_completion_consumed() — it does NOT populate
    api.config.BG_TASK_COMPLETE_EVENTS_SEEN (that set is ours-original and
    private to api.background_process). So the cross-A/B contract is the
    registry consumed-marker, not BG_TASK_COMPLETE_EVENTS_SEEN.
    """
    fake = _FakeProcessRegistry()
    fake.register("p2", "sess-2")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()

    from api import background_process as bp
    from api import streaming as st
    from api import config as _cfg

    bp.register_process_session("sess-2", "sess-2")

    evt = {
        "type": "completion",
        "session_id": "p2",
        "session_key": "sess-2",
        "command": "echo hi",
        "exit_code": 0,
        "output": "hi",
    }
    # A path: queue carried over from a closed-tab session, drain at next turn
    fake.completion_queue.put(evt)
    notifications = st._drain_webui_process_notifications("sess-2")
    assert len(notifications) == 1
    assert "Background process p2 completed" in notifications[0]

    # The REAL merged #2279 A-drain marks the SHARED upstream dedupe key
    # (registry consumed-marker) — NOT our private BG_TASK_COMPLETE_EVENTS_SEEN.
    assert fake.is_completion_consumed("p2")
    assert "sess-2" not in _cfg.BG_TASK_COMPLETE_EVENTS_SEEN, (
        "real upstream #2279 A-drain must NOT populate our private "
        "BG_TASK_COMPLETE_EVENTS_SEEN set"
    )

    # Now if B's drain thread sees another spurious event for the same id
    # (duplicate enqueue), _process_one must early-return on the SHARED
    # registry consumed-marker that A set — no double wakeup.
    bp._process_one(evt)  # second time
    assert fake.is_completion_consumed("p2")
    # B early-returned on the shared key BEFORE reaching its own seen-set, so
    # BG_TASK_COMPLETE_EVENTS_SEEN stays unpopulated for this session (proves
    # the cross-A/B dedupe used the real upstream key, not ours).
    assert "sess-2" not in _cfg.BG_TASK_COMPLETE_EVENTS_SEEN
    # And no duplicate wakeup marker was queued by the second B pass.
    assert "sess-2" not in _cfg.PENDING_BG_TASK_COMPLETIONS


def test_registry_completion_consumed_contract():
    """Copilot #2242 review #4 — fail CI LOUD if the agent ProcessRegistry
    private cross-A/B dedupe surface is renamed/retyped upstream.

    The WebUI B-drain has no public ``mark_completion_consumed`` to call, so
    it reaches into ``ProcessRegistry._completion_consumed`` (under
    ``._lock``) to set the shared marker that the public
    ``is_completion_consumed`` reads. If a future upstream refactor renames
    any of these, the double-wakeup bug would silently come back. This test
    pins the contract so the rename breaks HERE (visibly) instead.
    """
    pytest.importorskip("tools.process_registry", reason="hermes-agent not installed")
    from tools.process_registry import ProcessRegistry
    from api import background_process as bp

    pr = ProcessRegistry()
    for attr in bp._REGISTRY_CONSUMED_CONTRACT:
        assert hasattr(pr, attr), (
            f"ProcessRegistry.{attr} is gone — the WebUI cross-A/B wakeup "
            f"dedupe coupling (Copilot #2242 #4) is broken. Either restore it "
            f"or add a PUBLIC mark_completion_consumed() upstream and switch "
            f"api/background_process._mark_registry_completion_consumed to it."
        )
    # Shape contract: the write target must be a set-like (supports .add) and
    # the guard must be a usable context manager (supports `with`).
    assert hasattr(pr._completion_consumed, "add"), (
        "ProcessRegistry._completion_consumed is no longer a set-like "
        "(.add gone) — cross-A/B wakeup dedupe write would fail."
    )
    assert hasattr(pr._lock, "__enter__") and hasattr(pr._lock, "__exit__"), (
        "ProcessRegistry._lock is no longer a context manager — the guarded "
        "marker write in _mark_registry_completion_consumed would fail."
    )
    assert callable(pr.is_completion_consumed), (
        "ProcessRegistry.is_completion_consumed must stay a public method "
        "(the cross-A/B dedupe READ side depends on it)."
    )

    # End-to-end: the public read sees what the guarded private write sets
    # (the exact mechanism _mark_registry_completion_consumed relies on).
    pid = "proc_contract_test"
    assert pr.is_completion_consumed(pid) is False
    with pr._lock:
        pr._completion_consumed.add(pid)
    assert pr.is_completion_consumed(pid) is True


def test_mark_registry_completion_consumed_fails_loud_on_rename(monkeypatch, caplog):
    """A renamed private attr must log ERROR (contract violation), NOT be
    swallowed silently at DEBUG (the pre-Copilot-#4 behavior)."""
    import logging

    class _RenamedRegistry:
        # Simulates an upstream rename: _completion_consumed -> _consumed_v2.
        def __init__(self):
            self._lock = threading.Lock()
            self._consumed_v2: set[str] = set()

        def is_completion_consumed(self, pid: str) -> bool:
            return pid in self._consumed_v2

    fake = _RenamedRegistry()
    _install_fake_registry(monkeypatch, fake)

    from api import background_process as bp

    with caplog.at_level(logging.ERROR, logger="api.background_process"):
        bp._mark_registry_completion_consumed("p-renamed")

    assert any(
        "coupling contract VIOLATED" in r.message and r.levelno >= logging.ERROR
        for r in caplog.records
    ), "a renamed registry private attr must surface as an ERROR, not a silent DEBUG"
    # The marker was NOT set (the bug it guards against), but it failed LOUD so
    # CI / monitoring catches it instead of double-firing wakeups silently.
    assert not fake.is_completion_consumed("p-renamed")


def test_emit_uses_new_event_name_with_trimmed_payload_and_event_id(monkeypatch):
    """T1 + T2 contract: emit is named ``bg_task_complete`` (canonical) AND
    ``process_complete`` (dual-emit shim until PR (b)); payload matches the
    minimal shape ``{session_id, task_id, completed_at, summary?, event_id}``;
    both emits carry the same payload + the same ``event_id``.
    """
    fake = _FakeProcessRegistry()
    fake.register("task-evt-1", "sess-evt-1")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()

    from api import background_process as bp

    bp.register_process_session("sess-evt-1", "sess-evt-1")

    # Capture every (event, data) tuple the emitter pushes to streams.
    emits: list[tuple[str, dict]] = []

    def _capture(session_id: str, event: str, data: dict) -> int:
        emits.append((event, data))
        return 1

    monkeypatch.setattr(bp, "_emit_to_session_streams", _capture)

    evt = {
        "type": "completion",
        "session_id": "task-evt-1",
        "session_key": "sess-evt-1",
        "command": "sleep 1",
        "exit_code": 0,
        "output": "done",
    }
    bp._process_one(evt)

    # Dual-emit shim: both names fire, same payload, same event_id.
    names = [e[0] for e in emits]
    assert "bg_task_complete" in names, f"canonical event missing: {names}"
    assert "process_complete" in names, f"dual-emit shim missing: {names}"

    payloads = [e[1] for e in emits if e[0] in ("bg_task_complete", "process_complete")]
    assert len({p["event_id"] for p in payloads}) == 1, (
        "dual-emit must share a single event_id so consumers can dedupe"
    )

    payload = payloads[0]
    # Minimal shape per maintainer (R2 §Q1).
    expected_required = {"session_id", "task_id", "completed_at", "event_id"}
    allowed = expected_required | {"summary"}
    assert expected_required <= set(payload), f"missing required keys: {payload}"
    assert set(payload) <= allowed, f"unexpected keys in trimmed payload: {payload}"

    # Dropped keys must NOT be present.
    for dropped in ("command", "exit_code", "type", "stdout_preview", "wakeup_prompt", "emitted_at", "process_id"):
        assert dropped not in payload, f"{dropped!r} should be dropped by T1 trim"

    # Field-rename invariants:
    assert payload["session_id"] == "sess-evt-1"
    assert payload["task_id"] == "task-evt-1"          # was process_id
    assert isinstance(payload["completed_at"], float)  # was emitted_at
    assert isinstance(payload["event_id"], str) and len(payload["event_id"]) >= 8


def test_event_id_is_unique_per_emit(monkeypatch):
    """T2: every emit gets a fresh event_id; two completions for two distinct
    processes produce two distinct ids.
    """
    fake = _FakeProcessRegistry()
    fake.register("task-a", "sess-evt-2")
    fake.register("task-b", "sess-evt-2")
    _install_fake_registry(monkeypatch, fake)
    _reset_cfg_state()

    from api import background_process as bp

    bp.register_process_session("sess-evt-2", "sess-evt-2")
    monkeypatch.setattr(bp, "_EMIT_COALESCE_WINDOW_SECS", 0.0)

    emits: list[tuple[str, dict]] = []

    def _capture(session_id: str, event: str, data: dict) -> int:
        emits.append((event, data))
        return 1

    monkeypatch.setattr(bp, "_emit_to_session_streams", _capture)

    bp._process_one({"type": "completion", "session_id": "task-a", "session_key": "sess-evt-2", "exit_code": 0})
    bp._process_one({"type": "completion", "session_id": "task-b", "session_key": "sess-evt-2", "exit_code": 0})

    canonical_payloads = [d for ev, d in emits if ev == "bg_task_complete"]
    assert len(canonical_payloads) == 2
    assert canonical_payloads[0]["event_id"] != canonical_payloads[1]["event_id"]
