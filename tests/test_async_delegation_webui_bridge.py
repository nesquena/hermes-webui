"""Regression coverage for async delegate_task completion routing beyond #4912.

#4912 taught ``format_wakeup_prompt`` how to render ``async_delegation`` events.
These tests cover the remaining WebUI bridge contract: route those events by
``session_key``, dedupe by ``delegation_id``, and coordinate with the optional
agent-side async-delegation consumed marker when available.
"""
from __future__ import annotations

import queue
import sys
import time
import types

from api import background_process as bp
from api import config as cfg
from api import streaming


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeProcessRegistry:
    def __init__(self):
        self.completion_queue = queue.Queue()
        self._completion_consumed: set[str] = set()
        self._lock = _NoopLock()

    def is_completion_consumed(self, process_id: str) -> bool:
        return process_id in self._completion_consumed

    def get(self, process_id: str):
        return None


def _install_fake_process_registry(monkeypatch):
    registry = _FakeProcessRegistry()

    def _format(evt):
        return f"[ASYNC DELEGATION BATCH COMPLETE — {evt['delegation_id']}]\nbackend summary"

    fake_mod = types.ModuleType("tools.process_registry")
    fake_mod.process_registry = registry
    fake_mod.format_process_notification = _format
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.process_registry", fake_mod)
    return registry


def _reset_wakeup_state() -> None:
    cfg.PROCESS_SESSION_INDEX.clear()
    cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
    cfg.DEFERRED_PROCESS_WAKEUPS.clear()


def _async_delegation_event(**overrides):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_test123",
        "session_key": "webui-session-1",
        "status": "completed",
        "is_batch": True,
        "results": [{"task_index": 0, "status": "completed", "summary": "backend summary"}],
        "dispatched_at": time.time() - 2,
        "completed_at": time.time(),
    }
    evt.update(overrides)
    return evt


def test_background_wakeup_routes_async_delegation_by_delegation_id(monkeypatch):
    _reset_wakeup_state()
    _install_fake_process_registry(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    started: list[tuple[str, str, str]] = []
    emitted: list[tuple[str, dict]] = []
    marked: list[str] = []

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda session_id: False)
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda session_id, prompt, *, process_id="": started.append((session_id, prompt, process_id)),
    )
    monkeypatch.setattr(
        bp,
        "_emit_bg_task_complete_events_coalesced",
        lambda session_id, payload: emitted.append((session_id, payload)) or 1,
    )
    monkeypatch.setattr(bp, "mark_async_delegation_record_consumed", lambda deleg_id: marked.append(deleg_id) or True)

    bp._process_one(_async_delegation_event())

    assert started == [("webui-session-1", started[0][1], "deleg_test123")]
    assert "ASYNC DELEGATION BATCH COMPLETE" in started[0][1]
    assert emitted[0][1]["task_id"] == "deleg_test123"
    assert cfg.BG_TASK_COMPLETE_EVENTS_SEEN["webui-session-1"] == {"deleg_test123"}
    assert marked == ["deleg_test123"]


def test_streaming_next_turn_drain_handles_async_delegation_event(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    marked: list[str] = []
    monkeypatch.setattr(streaming, "mark_async_delegation_record_consumed", lambda deleg_id: marked.append(deleg_id) or True)

    registry.completion_queue.put(_async_delegation_event())

    notifications = streaming._drain_webui_process_notifications("webui-session-1")

    assert len(notifications) == 1
    assert "ASYNC DELEGATION BATCH COMPLETE" in notifications[0]
    assert registry.is_completion_consumed("deleg_test123")
    assert marked == ["deleg_test123"]


def test_streaming_next_turn_drain_routes_via_process_session_index_mapping(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["gateway-session-key"] = "webui-session-1"
    registry.completion_queue.put(_async_delegation_event(session_key="gateway-session-key"))

    wrong_session_notifications = streaming._drain_webui_process_notifications("webui-session-2")
    right_session_notifications = streaming._drain_webui_process_notifications("webui-session-1")

    assert wrong_session_notifications == []
    assert len(right_session_notifications) == 1
    assert "ASYNC DELEGATION BATCH COMPLETE" in right_session_notifications[0]
