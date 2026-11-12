"""A wake-up refused for a transient reason the WebUI did not cause must not
consume the durable delivery budget.

Scenario: while the Agent runtime is being replaced, the still-running WebUI
answers every server-side wake-up with ``409 agent_runtime_stale
retryable=True``. Releasing the claim as a plain failure each time exhausts the
bounded budget within seconds and terminally drops a completion whose origin
session is alive and waiting.

Contract under test (``api/background_process._start_async_delegation_wakeup_turn``):

* a refusal whose payload says ``retryable: True`` (stale runtime), a paused
  wake-up or a session busy with another stream releases the claim with
  ``retryable=True`` so the core refunds the attempt;
* every other rejection keeps the historical bounded behaviour;
* an older core whose ``release_event_delivery`` has no ``retryable`` keyword
  still gets a plain release (compatibility, never an exception).
"""
from __future__ import annotations

import sys
import time
import types

import pytest

from api import background_process as bp
from api import config as cfg
from api import process_event_utils as peu


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _reset() -> None:
    cfg.PROCESS_SESSION_INDEX.clear()
    cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
    cfg.DEFERRED_PROCESS_WAKEUPS.clear()
    reset = getattr(peu, "_reset_legacy_async_delivery_dedupe_for_tests", None)
    if reset is not None:
        reset()


def _event():
    return {
        "type": "async_delegation",
        "delegation_id": "deleg_retryable",
        "session_key": "webui-session-1",
        "status": "completed",
        "is_batch": True,
        "results": [{"task_index": 0, "status": "completed", "summary": "verdict"}],
        "dispatched_at": time.time() - 2,
        "completed_at": time.time(),
    }


class _Registry:
    def __init__(self):
        import queue

        self.completion_queue = queue.Queue()


def _install_core(monkeypatch, *, supports_retryable: bool):
    calls = {"release": [], "complete": []}
    mod = types.ModuleType("tools.async_delegation")
    mod.claim_event_delivery = lambda evt, consumer: f"claim:{consumer}"
    mod.complete_event_delivery = lambda evt, claim_id: calls["complete"].append(claim_id)
    mod.get_durable_delegation = lambda delegation_id: {"delivery_state": "pending", "delivery_attempts": 1}
    mod.restore_undelivered_completions = lambda queue_: 0
    if supports_retryable:
        def _release(evt, claim_id, *, retryable=False):
            calls["release"].append((claim_id, retryable))
    else:
        def _release(evt, claim_id):  # pre-refund core: no keyword at all
            calls["release"].append((claim_id, None))
    mod.release_event_delivery = _release
    pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", mod)
    monkeypatch.setattr(pkg, "async_delegation", mod, raising=False)
    return calls


def _run(monkeypatch, response, *, supports_retryable=True):
    _reset()
    calls = _install_core(monkeypatch, supports_retryable=supports_retryable)
    from api import routes

    monkeypatch.setattr(routes, "start_session_turn", lambda *a, **k: response)
    monkeypatch.setattr(bp, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 10.0)
    registry = _Registry()
    evt = _event()
    claim = peu.claim_async_delegation_delivery(evt, "webui-background")
    assert claim is not None
    try:
        bp._start_async_delegation_wakeup_turn(
            "webui-session-1", "verdict", delegation_id="deleg_retryable",
            evt=evt, claim=claim, process_registry=registry,
        )
        assert _wait_until(lambda: len(calls["release"]) == 1)
        assert calls["complete"] == []
        return calls["release"][0]
    finally:
        _reset()


@pytest.mark.parametrize("response", [
    {"_status": 409, "error": "runtime changed", "type": "agent_runtime_stale", "retryable": True},
    {"_status": 503, "error": "temporarily unavailable", "retryable": True},
    {"_status": 409, "error": "process_wakeup_paused"},
    {"_status": 409, "error": "session already has an active stream", "active_stream_id": "s1"},
])
def test_transient_refusal_releases_with_refund(monkeypatch, response):
    _claim, retryable = _run(monkeypatch, response)
    assert retryable is True


@pytest.mark.parametrize("response", [
    {"_status": 500, "error": "boom"},
    {"_status": 404, "error": "Session not found"},
    {"_status": 400, "error": "message is required"},
    {"_status": 409, "error": "conflict", "retryable": False},
    None,
])
def test_hard_rejection_keeps_bounded_budget(monkeypatch, response):
    _claim, retryable = _run(monkeypatch, response)
    assert retryable is False


def test_old_core_without_retryable_keyword_still_releases(monkeypatch):
    _claim, retryable = _run(
        monkeypatch,
        {"_status": 409, "error": "x", "type": "agent_runtime_stale", "retryable": True},
        supports_retryable=False,
    )
    assert retryable is None  # plain release happened, no TypeError escaped


def test_real_core_signature_is_detected():
    def new_core(evt, claim_id, *, retryable=False):
        return None

    def kwargs_core(evt, claim_id, **kwargs):
        return None

    def old_core(evt, claim_id):
        return None

    assert peu._release_accepts_retryable(new_core) is True
    assert peu._release_accepts_retryable(kwargs_core) is True
    assert peu._release_accepts_retryable(old_core) is False
