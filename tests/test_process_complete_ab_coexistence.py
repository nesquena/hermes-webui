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

api.config.PROCESS_COMPLETE_EVENTS_SEEN remains as B's own private
secondary dedupe (duplicate enqueue within this module) but is NOT the
cross-A/B contract — the real merged #2279 never writes it.

The two paths run in *different* hot paths (background thread vs. agent turn
start) but share process_registry._completion_consumed, so a wakeup can only
happen once.
"""
from __future__ import annotations

import queue
import threading
import types


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
    """Inject the fake registry into tools.process_registry for the test.

    IMPORTANT (rebase isolation fix): use ONLY monkeypatch.setitem so both
    sys.modules entries are restored to their real/absent state on teardown.
    The prior implementation did sys.modules.setdefault("tools", ...) which
    is an UNTRACKED mutation — when real `tools` was not yet imported it
    permanently leaked a non-package fake `tools` into sys.modules, breaking
    any later test that does `from tools.process_registry import ...` (e.g.
    test_session_channel_option_x). With the merged upstream #2279 those
    real-`tools`-importing tests now share the same pytest session, so the
    leak became a hard cross-file failure.
    """
    import sys
    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = fake
    tools_mod = types.ModuleType("tools")
    tools_mod.process_registry = mod  # type: ignore[attr-defined]
    # Both setitem calls are monkeypatch-tracked: on teardown each key is
    # restored to its prior value, or deleted if it was absent — no leak.
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.process_registry", mod)


def _reset_cfg_state():
    from api import config as _cfg
    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        _cfg.PROCESS_SESSION_INDEX.clear()
    _cfg.PENDING_PROCESS_COMPLETIONS.clear()
    _cfg.PROCESS_COMPLETE_EVENTS_SEEN.clear()
    with _cfg.STREAMS_LOCK:
        _cfg.STREAMS.clear()
    if hasattr(_cfg, "ACTIVE_RUNS"):
        _cfg.ACTIVE_RUNS.clear()


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
    assert "p1" in _cfg.PROCESS_COMPLETE_EVENTS_SEEN["sess-1"]
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
    api.config.PROCESS_COMPLETE_EVENTS_SEEN (that set is ours-original and
    private to api.background_process). So the cross-A/B contract is the
    registry consumed-marker, not PROCESS_COMPLETE_EVENTS_SEEN.
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
    # (registry consumed-marker) — NOT our private PROCESS_COMPLETE_EVENTS_SEEN.
    assert fake.is_completion_consumed("p2")
    assert "sess-2" not in _cfg.PROCESS_COMPLETE_EVENTS_SEEN, (
        "real upstream #2279 A-drain must NOT populate our private "
        "PROCESS_COMPLETE_EVENTS_SEEN set"
    )

    # Now if B's drain thread sees another spurious event for the same id
    # (duplicate enqueue), _process_one must early-return on the SHARED
    # registry consumed-marker that A set — no double wakeup.
    bp._process_one(evt)  # second time
    assert fake.is_completion_consumed("p2")
    # B early-returned on the shared key BEFORE reaching its own seen-set, so
    # PROCESS_COMPLETE_EVENTS_SEEN stays unpopulated for this session (proves
    # the cross-A/B dedupe used the real upstream key, not ours).
    assert "sess-2" not in _cfg.PROCESS_COMPLETE_EVENTS_SEEN
    # And no duplicate wakeup marker was queued by the second B pass.
    assert "sess-2" not in _cfg.PENDING_PROCESS_COMPLETIONS


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
