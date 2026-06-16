"""Behavioral tests for issue #4029 stale-completion age gate.

These exercise the real `_drain_webui_process_notifications` against a fake
process_registry, proving that:
  - a completion older than the cap is dropped (consumed, not requeued),
  - a fresh completion is still delivered,
  - events without `completed_at` are never dropped (backward compat),
  - the env override (incl. disable via 0) is honored.
"""
import importlib
import queue
import sys
import threading
import time
import types

import pytest


def _install_fake_registry(monkeypatch, events):
    """Build a fake `tools.process_registry` module whose `process_registry`
    exposes the surface `_drain_webui_process_notifications` touches."""
    q = queue.Queue()
    for e in events:
        q.put(e)

    class _FakeProc:
        def __init__(self, session_key):
            self.session_key = session_key

    class _FakeRegistry:
        def __init__(self):
            self.completion_queue = q
            self._lock = threading.RLock()
            self._completion_consumed = set()
            # map session_id -> session_key so ownership check passes
            self._owner = {}

        def is_completion_consumed(self, sid):
            return sid in self._completion_consumed

        def get(self, sid):
            key = self._owner.get(sid)
            return _FakeProc(key) if key is not None else None

    reg = _FakeRegistry()
    mod = types.ModuleType("tools.process_registry")
    mod.process_registry = reg
    # ensure parent package exists
    if "tools" not in sys.modules:
        pkg = types.ModuleType("tools")
        pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.process_registry", mod)
    return reg


def _make_event(sid, completed_at, session_key="websess-1"):
    return {
        "type": "completion",
        "session_id": sid,
        "session_key": session_key,
        "command": f"cmd-{sid}",
        "exit_code": 0,
        "output": f"out-{sid}",
        **({"completed_at": completed_at} if completed_at is not None else {}),
    }


@pytest.fixture
def streaming():
    return importlib.import_module("api.streaming")


def test_stale_completion_is_dropped_and_fresh_is_delivered(streaming, monkeypatch):
    now = time.time()
    fresh = _make_event("fresh", now - 60)          # 1 min old -> keep
    stale = _make_event("stale", now - 7 * 3600)    # 7 h old -> drop (cap 6h)
    reg = _install_fake_registry(monkeypatch, [stale, fresh])
    reg._owner["fresh"] = "websess-1"
    reg._owner["stale"] = "websess-1"
    # default cap = 6h
    monkeypatch.delenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", raising=False)

    out = streaming._drain_webui_process_notifications("websess-1")

    joined = "\n".join(out)
    assert "fresh" in joined, "fresh completion should be delivered"
    assert "stale" not in joined, "stale completion must be dropped"
    # stale was consumed (won't come back), fresh consumed after delivery
    assert "stale" in reg._completion_consumed
    assert "fresh" in reg._completion_consumed
    # nothing belonging to this session is left requeued
    assert reg.completion_queue.empty()


def test_event_without_completed_at_is_never_dropped(streaming, monkeypatch):
    legacy = _make_event("legacy", None)  # no completed_at -> backward compat keep
    reg = _install_fake_registry(monkeypatch, [legacy])
    reg._owner["legacy"] = "websess-1"
    monkeypatch.delenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", raising=False)

    out = streaming._drain_webui_process_notifications("websess-1")

    assert any("legacy" in n for n in out), "legacy event (no timestamp) must be delivered"


def test_env_zero_disables_age_gate(streaming, monkeypatch):
    now = time.time()
    ancient = _make_event("ancient", now - 10 * 24 * 3600)  # 10 days old
    reg = _install_fake_registry(monkeypatch, [ancient])
    reg._owner["ancient"] = "websess-1"
    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "0")

    out = streaming._drain_webui_process_notifications("websess-1")

    assert any("ancient" in n for n in out), "age gate disabled -> even ancient delivered"


def test_helper_reads_env_override(streaming, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "120")
    assert streaming._stale_completion_max_age_seconds() == 120.0
    monkeypatch.setenv("HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS", "not-a-number")
    # invalid -> falls back to default 6h
    assert streaming._stale_completion_max_age_seconds() == 6 * 60 * 60
