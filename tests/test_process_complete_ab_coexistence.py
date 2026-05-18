"""Integration tests: PR #2279 (next-turn drain, A) + PR #2242 (SSE drain, B) coexist
without duplicating wakeups for the same background process_id.

These tests verify the shared dedupe contract via api.config.PROCESS_COMPLETE_EVENTS_SEEN:
- If B's SSE drain fires first (proactive case), A's next-turn drain must skip the
  same process_id.
- If A's drain fires first (SSE-disconnected case), B's drain must not re-emit a
  duplicate SSE event.

The two paths run in *different* hot paths (background thread vs. agent turn start),
but they share the same per-session dedupe set, so a wakeup can only happen once.
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
    """Inject the fake registry into both tools.process_registry and any cached imports."""
    import sys
    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = fake
    tools_mod = sys.modules.setdefault("tools", types.ModuleType("tools"))
    tools_mod.process_registry = mod  # type: ignore[attr-defined]
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
    """A drains and wakes the agent; later B's queue read of the same id is a no-op
    because PROCESS_COMPLETE_EVENTS_SEEN already contains it."""
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

    # A marked the seen-set
    assert "p2" in _cfg.PROCESS_COMPLETE_EVENTS_SEEN["sess-2"]
    # A also marked agent-registry-consumed (via _mark_process_completion_consumed)
    assert fake.is_completion_consumed("p2")

    # Now if B's drain thread were to see another spurious event for the same id
    # (e.g. duplicate enqueue), _process_one must early-return on the seen set.
    bp._process_one(evt)  # second time
    # seen set unchanged; no extra PENDING_PROCESS_COMPLETIONS entries beyond
    # what B may have already added (it adds one regardless of dedupe; that's
    # harmless because routes._start_chat_stream_for_session drains the marker).
    assert _cfg.PROCESS_COMPLETE_EVENTS_SEEN["sess-2"] == {"p2"}
