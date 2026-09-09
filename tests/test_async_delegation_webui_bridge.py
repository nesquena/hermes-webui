"""Regression coverage for async delegate_task completion routing beyond #4912.

#4912 taught ``format_wakeup_prompt`` how to render ``async_delegation`` events.
These tests cover the remaining WebUI bridge contract: route those events by
``session_key``, dedupe by ``delegation_id``, and coordinate with both the
current durable claim API and bounded compatibility fallbacks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import types

import pytest

from api import background_process as bp
from api import config as cfg
from api import models, routes
from api import process_event_utils as peu
from api import streaming
from api.models import Session
from api.session_lineage import (
    build_completion_delivery_context,
    completion_delivery_metadata,
    read_completion_delivery_receipt,
    release_turn_admission,
)
from api.turn_journal import read_turn_journal


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


def _install_fake_durable_delivery_api(monkeypatch):
    calls = {
        "claim": [],
        "complete": [],
        "release": [],
        "mark": [],
        "legacy": [],
        "delivery_state": "pending",
        "delivery_attempts": 0,
        "pending_ids": set(),
        "restore_failures": 0,
    }
    fake_mod = types.ModuleType("tools.async_delegation")

    def _claim(evt, consumer):
        calls["claim"].append((dict(evt), consumer))
        if calls["delivery_state"] != "pending":
            return None
        calls["delivery_attempts"] += 1
        return f"claim:{consumer}"

    def _complete(evt, claim_id):
        calls["complete"].append((dict(evt), claim_id))
        calls["delivery_state"] = "delivered"

    def _release(evt, claim_id):
        calls["release"].append((dict(evt), claim_id))
        calls["delivery_state"] = (
            "dropped" if calls["delivery_attempts"] >= 8 else "pending"
        )

    def _get_durable(delegation_id):
        if calls["delivery_state"] == "pending":
            calls["pending_ids"].add(delegation_id)
        return {"delivery_state": calls["delivery_state"]}

    def _restore(target_queue):
        if calls["restore_failures"]:
            calls["restore_failures"] -= 1
            raise RuntimeError("transient durable-store read failure")
        if calls["delivery_state"] != "pending":
            return 0
        pending = sorted(calls["pending_ids"])
        for delegation_id in pending:
            target_queue.put(_async_delegation_event(delegation_id=delegation_id))
        return len(pending)

    def _mark(delegation_id):
        calls["mark"].append(delegation_id)
        return True

    def _legacy(delegation_id):
        calls["legacy"].append(delegation_id)
        return True

    fake_mod.claim_event_delivery = _claim
    fake_mod.complete_event_delivery = _complete
    fake_mod.release_event_delivery = _release
    fake_mod.get_durable_delegation = _get_durable
    fake_mod.restore_undelivered_completions = _restore
    fake_mod.mark_completion_delivered = _mark
    fake_mod.mark_async_delegation_consumed = _legacy
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)
    return calls


def _install_fake_legacy_delivery_api(monkeypatch):
    """Install a pre-durable-core surface with only compatibility markers."""
    calls = {"mark": [], "legacy": []}
    fake_mod = types.ModuleType("tools.async_delegation")
    fake_mod.mark_completion_delivered = (  # type: ignore[reportAttributeAccessIssue]
        lambda delegation_id: calls["mark"].append(delegation_id) or True
    )
    fake_mod.mark_async_delegation_consumed = (  # type: ignore[reportAttributeAccessIssue]
        lambda delegation_id: calls["legacy"].append(delegation_id)
    )
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)
    return calls


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _reset_wakeup_state() -> None:
    cfg.PROCESS_SESSION_INDEX.clear()
    cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    cfg.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
    cfg.DEFERRED_PROCESS_WAKEUPS.clear()
    reset = getattr(peu, "_reset_legacy_async_delivery_dedupe_for_tests", None)
    if reset is not None:
        reset()


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


def test_background_wakeup_claims_and_completes_without_registry_growth(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    started: list[tuple[str, str, str]] = []
    emitted: list[tuple[str, dict]] = []

    def _accept(
        session_id,
        prompt,
        *,
        delegation_id,
        evt,
        claim,
        process_registry,
    ):
        started.append((session_id, prompt, delegation_id))
        bp._record_async_delegation_accepted(
            evt,
            session_id=session_id,
            claim=claim,
        )

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda session_id: False)
    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", _accept)
    monkeypatch.setattr(
        bp,
        "_emit_bg_task_complete_events_coalesced",
        lambda session_id, payload: emitted.append((session_id, payload)) or 1,
    )

    bp._process_one(_async_delegation_event())

    assert started == [("webui-session-1", started[0][1], "deleg_test123")]
    assert "ASYNC DELEGATION BATCH COMPLETE" in started[0][1]
    assert emitted[0][1]["task_id"] == "deleg_test123"
    assert cfg.BG_TASK_COMPLETE_EVENTS_SEEN == {}
    assert [consumer for _evt, consumer in delivery["claim"]] == ["webui-background"]
    assert len(delivery["complete"]) == 1
    assert delivery["release"] == []
    assert delivery["mark"] == []
    assert delivery["legacy"] == []
    assert registry._completion_consumed == set()


def test_canonical_callback_failure_releases_and_schedules_durable_recovery(monkeypatch):
    _reset_wakeup_state()
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    async_delivery = sys.modules["tools.async_delegation"]

    async_delivery.complete_event_delivery = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("ACK failed")
    )
    async_delivery.mark_completion_delivered = lambda _delegation_id: False
    async_delivery.mark_async_delegation_consumed = lambda _delegation_id: (_ for _ in ()).throw(
        RuntimeError("legacy ACK failed")
    )
    async_delivery.get_durable_delegation = lambda _delegation_id: (_ for _ in ()).throw(
        RuntimeError("durable store temporarily unavailable")
    )

    evt = _async_delegation_event()
    registry.completion_queue.put(evt)
    monkeypatch.setattr(
        streaming,
        "_completion_event_matches_webui_lineage",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: False)
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda session_id, _prompt, *, evt, claim, **_kwargs: bp._record_async_delegation_accepted(
            evt,
            session_id=session_id,
            claim=claim,
        ),
    )

    try:
        assert streaming._drain_webui_process_notifications("webui-session-1") is None
        assert [consumer for _evt, consumer in delivery["claim"]] == [
            "webui-background"
        ]
        assert len(delivery["release"]) == 1
        assert _wait_until(lambda: registry.completion_queue.qsize() == 1)
    finally:
        _reset_wakeup_state()


def test_requeue_put_failure_arms_durable_restore_sweep(monkeypatch):
    _reset_wakeup_state()
    delivery = _install_fake_durable_delivery_api(monkeypatch)

    class _FailingQueue:
        def put(self, _evt):
            raise RuntimeError("queue unavailable")

    registry = _FakeProcessRegistry()
    registry.completion_queue = _FailingQueue()
    evt = _async_delegation_event()
    delivery["pending_ids"].add(evt["delegation_id"])

    try:
        bp._requeue_async_delegation_event(registry, evt)
        assert peu.async_delivery_retry_timer_count() == 1
    finally:
        _reset_wakeup_state()


def test_requeue_stops_without_enqueuing_during_shutdown():
    _reset_wakeup_state()
    registry = _FakeProcessRegistry()
    bp._DRAIN_STOP.set()
    try:
        bp._requeue_async_delegation_event(registry, _async_delegation_event())
        assert registry.completion_queue.empty()
    finally:
        bp._DRAIN_STOP.clear()


def test_background_unmapped_legacy_event_is_requeued_best_effort(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    _install_fake_durable_delivery_api(monkeypatch)
    monkeypatch.setattr(bp, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 0.01)
    evt = _async_delegation_event(
        delegation_id=None,
        session_id="proc_legacy_retry",
        session_key="legacy-unmapped-session",
    )

    bp._process_one(evt)

    assert _wait_until(lambda: registry.completion_queue.qsize() == 1)
    retried = registry.completion_queue.get_nowait()
    assert retried["session_id"] == "proc_legacy_retry"
    assert retried["_webui_routing_retry_attempted"] is True

    bp._process_one(retried)
    assert registry.completion_queue.empty()


def test_background_wakeup_releases_claim_when_dispatch_fails(monkeypatch):
    _reset_wakeup_state()
    _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda session_id: False)
    monkeypatch.setattr(bp, "_emit_bg_task_complete_events_coalesced", lambda *_args: 1)
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("dispatch failed")),
    )

    bp._process_one(_async_delegation_event())

    assert len(delivery["claim"]) == 1
    assert delivery["complete"] == []
    assert len(delivery["release"]) == 1
    assert "deleg_test123" not in cfg.BG_TASK_COMPLETE_EVENTS_SEEN.get("webui-session-1", set())


def test_background_active_turn_does_not_consume_durable_delivery_attempts(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: True)
    monkeypatch.setattr(peu, "ASYNC_DELIVERY_CLAIM_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must stay deferred")),
    )

    try:
        for _ in range(10):
            bp._process_one(_async_delegation_event())

        assert delivery["claim"] == []
        assert delivery["delivery_attempts"] == 0
        assert delivery["complete"] == []
        assert delivery["release"] == []
        assert delivery["delivery_state"] == "pending"
        assert registry.completion_queue.empty()
        assert peu.async_delivery_retry_timer_count() == 1
        assert cfg.DEFERRED_PROCESS_WAKEUPS == {}
        assert cfg.BG_TASK_COMPLETE_EVENTS_SEEN == {}
    finally:
        _reset_wakeup_state()


def test_background_busy_legacy_completion_retries_until_session_is_idle(monkeypatch):
    """A pre-durable core must not discard a completion after one busy retry."""
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_legacy_delivery_api(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    busy = {"active": True}
    accepted: list[tuple[str, str]] = []

    def _accept(session_id, prompt, *, evt, claim, **_kwargs):
        accepted.append((session_id, prompt))
        bp._record_async_delegation_accepted(evt, session_id=session_id, claim=claim)

    monkeypatch.setattr(bp, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: busy["active"])
    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", _accept)
    monkeypatch.setattr(bp, "_emit_bg_task_complete_events_coalesced", lambda *_args: 1)
    evt = _async_delegation_event()

    try:
        bp._process_one(evt)
        first_retry = registry.completion_queue.get(timeout=1)
        bp._process_one(first_retry)
        second_retry = registry.completion_queue.get(timeout=1)

        busy["active"] = False
        bp._process_one(second_retry)

        assert accepted and accepted[0][0] == "webui-session-1"
        assert delivery == {"mark": ["deleg_test123"], "legacy": []}
        assert registry.completion_queue.empty()
    finally:
        _reset_wakeup_state()


def test_legacy_completion_survives_repeated_wakeup_rejection_then_delivers(monkeypatch):
    """A pre-durable completion whose resolved target keeps rejecting the wakeup
    (409/busy) must stay retryable — the transient-failure sites must not consume
    the one-shot legacy marker and silently drop it after the first rejection.

    Regression for the combined #6632 + #6662 gate finding: the busy-check caller
    was fixed but _start_async_delegation_wakeup_turn's own reject/exception/
    dispatch-failure exits still used the bounded one-shot mode, dropping a legacy
    completion after the second transient failure.
    """
def test_legacy_retry_helper_keeps_requeuing_when_keep_legacy_retrying(monkeypatch):
    """`_retry_unclaimed_async_delegation_event(..., keep_legacy_retrying=True)`
    must requeue on EVERY call for a resolved-but-transiently-failing target —
    never consuming the one-shot `_webui_routing_retry_attempted` marker.

    Regression for the combined #6632 + #6662 gate finding: the busy-check caller
    was fixed, but `_start_async_delegation_wakeup_turn`'s own reject/exception/
    dispatch-failure exits (the resolved-target transient-failure sites) also pass
    keep_legacy_retrying=True now, so a legacy completion is not silently dropped
    after the second transient failure. The genuinely-unmapped callers still use
    the bounded one-shot mode (covered by
    test_background_unmapped_legacy_event_is_requeued_best_effort).
    """
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    # No durable claim available → schedule_async_delegation_claim_retry returns
    # False and we fall to the legacy requeue branch (the branch with the marker).
    monkeypatch.setattr(
        bp, "schedule_async_delegation_claim_retry", lambda *_a, **_k: False
    )
    monkeypatch.setattr(bp, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 0.01)

    try:
        evt = _async_delegation_event()
        # Drive the resolved-target transient-failure retry three times in a row.
        for _ in range(3):
            bp._retry_unclaimed_async_delegation_event(
                registry, evt, keep_legacy_retrying=True
            )
            requeued = registry.completion_queue.get(timeout=1)
            # The marker must NOT be set — otherwise the next pass would drop it.
            assert not requeued.get("_webui_routing_retry_attempted"), (
                "keep_legacy_retrying must not consume the one-shot marker"
            )
            evt = requeued
        assert registry.completion_queue.empty()

        # Contrast: the DEFAULT (unmapped) mode is bounded — one requeue, then stop.
        _reset_wakeup_state()
        registry2 = _install_fake_process_registry(monkeypatch)
        monkeypatch.setattr(
            bp, "schedule_async_delegation_claim_retry", lambda *_a, **_k: False
        )
        evt2 = _async_delegation_event()
        bp._retry_unclaimed_async_delegation_event(registry2, evt2)
        once = registry2.completion_queue.get(timeout=1)
        assert once.get("_webui_routing_retry_attempted") is True
        # Second pass on the already-marked event does NOT requeue (bounded).
        bp._retry_unclaimed_async_delegation_event(registry2, once)
        assert registry2.completion_queue.empty()
    finally:
        _reset_wakeup_state()


def test_formatting_failure_stays_bounded_not_keep_legacy_retrying(monkeypatch):
    """A permanently-malformed completion (format_wakeup_prompt raises / empty)
    must use the BOUNDED one-shot retry, never keep_legacy_retrying — otherwise a
    malformed event would loop forever. Only the post-format DISPATCH exception
    against a resolved target keeps retrying. Guards the formatting-vs-dispatch
    split from the combined #6632+#6662 gate.
    """
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    _install_fake_legacy_delivery_api(monkeypatch)
    calls = {"n": 0}

    def _spy(process_registry, evt, *, keep_legacy_retrying=False):
        calls["n"] += 1
        calls["keep_legacy_retrying"] = keep_legacy_retrying

    monkeypatch.setattr(bp, "_retry_unclaimed_async_delegation_event", _spy)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *_a, **_k: None)
    # Force a formatting failure (empty prompt → RuntimeError inside the format try).
    monkeypatch.setattr(bp, "format_wakeup_prompt", lambda _evt: "")

    class _Claim:
        durable = True

    try:
        bp._process_async_delegation_event(
            _async_delegation_event(),
            session_id="webui-session-1",
            delegation_id="deleg_test123",
            process_registry=registry,
        )
    except TypeError:
        # _process_async_delegation_event may claim internally; if the fake claim
        # path differs, fall through — the assertion below is the real check.
        pass

    # The formatting-failure branch MUST have run and used BOUNDED retry.
    assert calls["n"] >= 1, "test did not reach the formatting-failure branch"
    assert calls.get("keep_legacy_retrying") is False, (
        "formatting failure must stay bounded, never keep_legacy_retrying"
    )
    _reset_wakeup_state()


@pytest.mark.parametrize("status", [None, 302, 409, 500])
def test_autonomous_wakeup_rejection_uses_bounded_durable_retry(monkeypatch, status):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    from api import routes

    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *_args, **_kwargs: {"_status": status, "error": "busy"},
    )
    monkeypatch.setattr(bp, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 10.0)
    evt = _async_delegation_event()
    claim = peu.claim_async_delegation_delivery(evt, "webui-background")
    assert claim is not None

    try:
        bp._start_async_delegation_wakeup_turn(
            "webui-session-1",
            "delegation result",
            delegation_id="deleg_test123",
            evt=evt,
            claim=claim,
            process_registry=registry,
        )

        assert _wait_until(lambda: len(delivery["release"]) == 1)
        assert delivery["complete"] == []
        assert registry.completion_queue.empty()
        assert peu.async_delivery_retry_timer_count() == 1
        assert "deleg_test123" not in cfg.BG_TASK_COMPLETE_EVENTS_SEEN.get("webui-session-1", set())
    finally:
        _reset_wakeup_state()


def test_autonomous_wakeup_exception_uses_bounded_durable_retry(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    from api import routes

    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("start failed")),
    )
    monkeypatch.setattr(bp, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 10.0)
    evt = _async_delegation_event()
    claim = peu.claim_async_delegation_delivery(evt, "webui-background")
    assert claim is not None

    try:
        bp._start_async_delegation_wakeup_turn(
            "webui-session-1",
            "delegation result",
            delegation_id="deleg_test123",
            evt=evt,
            claim=claim,
            process_registry=registry,
        )

        assert _wait_until(lambda: len(delivery["release"]) == 1)
        assert delivery["complete"] == []
        assert registry.completion_queue.empty()
        assert peu.async_delivery_retry_timer_count() == 1
    finally:
        _reset_wakeup_state()


def test_autonomous_wakeup_acks_after_successful_turn_acceptance(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    from api import routes

    def accept_turn(*_args, **kwargs):
        kwargs["completion_acceptance"]()
        return {"_status": 200, "stream_id": "stream-1"}

    monkeypatch.setattr(routes, "start_session_turn", accept_turn)
    monkeypatch.setattr(bp, "_emit_bg_task_complete_events_coalesced", lambda *_args: 1)
    evt = _async_delegation_event()
    claim = peu.claim_async_delegation_delivery(evt, "webui-background")
    assert claim is not None

    bp._start_async_delegation_wakeup_turn(
        "webui-session-1",
        "delegation result",
        delegation_id="deleg_test123",
        evt=evt,
        claim=claim,
        process_registry=registry,
    )

    assert _wait_until(lambda: len(delivery["complete"]) == 1)
    assert delivery["release"] == []
    assert registry.completion_queue.qsize() == 0
    assert cfg.BG_TASK_COMPLETE_EVENTS_SEEN == {}


def test_background_and_next_turn_consumers_share_one_atomic_claim(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    evt = _async_delegation_event()
    registry.completion_queue.put(dict(evt))
    starts = []
    barrier = threading.Barrier(2)

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: False)
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda *_args, **_kwargs: starts.append("background"),
    )

    def _background():
        barrier.wait()
        bp._process_one(dict(evt))

    def _next_turn():
        barrier.wait()
        assert streaming._drain_webui_process_notifications("webui-session-1") is None

    workers = [threading.Thread(target=_background), threading.Thread(target=_next_turn)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert len(starts) == 1
    assert len(delivery["claim"]) == 1
    assert registry._completion_consumed == set()


def test_legacy_async_event_id_falls_back_to_session_id(monkeypatch):
    _reset_wakeup_state()
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    evt = _async_delegation_event(
        delegation_id=None,
        session_id="proc_deleg1",
        task_id="task-legacy-1",
    )
    registry.completion_queue.put(evt)
    started = []
    monkeypatch.setattr(
        streaming,
        "_completion_event_matches_webui_lineage",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: False)
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda session_id, _prompt, *, evt, claim, **_kwargs: (
            started.append(session_id),
            bp._record_async_delegation_accepted(
                evt,
                session_id=session_id,
                claim=claim,
            ),
        ),
    )

    assert streaming._drain_webui_process_notifications("webui-session-1") is None

    assert peu.completion_delivery_id(evt) == "proc_deleg1"
    assert started == ["webui-session-1"]
    assert len(delivery["claim"]) == 1
    assert len(delivery["complete"]) == 1


def test_streaming_next_turn_claims_and_completes_without_registry_growth(monkeypatch):
    _reset_wakeup_state()
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    registry.completion_queue.put(_async_delegation_event())
    started = []
    monkeypatch.setattr(
        streaming,
        "_completion_event_matches_webui_lineage",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: False)
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda session_id, _prompt, *, evt, claim, **_kwargs: (
            started.append(session_id),
            bp._record_async_delegation_accepted(
                evt,
                session_id=session_id,
                claim=claim,
            ),
        ),
    )

    assert streaming._drain_webui_process_notifications("webui-session-1") is None

    assert started == ["webui-session-1"]
    assert [consumer for _evt, consumer in delivery["claim"]] == ["webui-background"]
    assert len(delivery["complete"]) == 1
    assert delivery["release"] == []
    assert registry._completion_consumed == set()


def test_streaming_live_turn_defers_ack_until_agent_acceptance_boundary(monkeypatch):
    _reset_wakeup_state()
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    registry.completion_queue.put(_async_delegation_event())
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: True)

    try:
        assert streaming._drain_webui_process_notifications("webui-session-1") is None
        assert delivery["claim"] == []
        assert delivery["complete"] == []
        assert peu.async_delivery_retry_timer_count() == 1
    finally:
        _reset_wakeup_state()


def test_streaming_formatter_failure_releases_and_requeues(monkeypatch):
    _reset_wakeup_state()
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    process_registry_mod = sys.modules["tools.process_registry"]

    def _fail_formatter(_evt):
        raise RuntimeError("formatter failed")

    process_registry_mod.format_process_notification = _fail_formatter
    registry.completion_queue.put(_async_delegation_event())
    monkeypatch.setattr(
        streaming,
        "_completion_event_matches_webui_lineage",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: False)

    assert streaming._drain_webui_process_notifications("webui-session-1") is None
    assert len(delivery["claim"]) == 1
    assert delivery["complete"] == []
    assert len(delivery["release"]) == 1
    assert peu.async_delivery_retry_timer_count() == 1


def test_streaming_next_turn_releases_and_requeues_when_complete_fails(monkeypatch):
    _reset_wakeup_state()
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    async_delivery = sys.modules["tools.async_delegation"]

    def _fail_complete(_evt, _claim_id):
        raise RuntimeError("complete failed")

    async_delivery.complete_event_delivery = _fail_complete
    async_delivery.mark_completion_delivered = lambda _delegation_id: False
    async_delivery.mark_async_delegation_consumed = lambda _delegation_id: (_ for _ in ()).throw(
        RuntimeError("legacy marker failed")
    )
    registry.completion_queue.put(_async_delegation_event())
    monkeypatch.setattr(
        streaming,
        "_completion_event_matches_webui_lineage",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _session_id: False)
    monkeypatch.setattr(
        bp,
        "_start_async_delegation_wakeup_turn",
        lambda session_id, _prompt, *, evt, claim, **_kwargs: bp._record_async_delegation_accepted(
            evt,
            session_id=session_id,
            claim=claim,
        ),
    )

    assert streaming._drain_webui_process_notifications("webui-session-1") is None

    assert len(delivery["claim"]) == 1
    assert len(delivery["release"]) == 1
    assert peu.async_delivery_retry_timer_count() == 1
    assert registry._completion_consumed == set()


def test_streaming_synchronous_ack_and_queue_failure_arms_durable_restore(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    evt = _async_delegation_event()
    registry.completion_queue.put(evt)
    monkeypatch.setattr(
        bp,
        "_process_one",
        lambda _evt: (_ for _ in ()).throw(RuntimeError("handoff failed")),
    )

    assert streaming._drain_webui_process_notifications("webui-session-1") is None
    assert registry.completion_queue.get_nowait() == evt
    assert delivery["claim"] == []
    assert delivery["complete"] == []


def test_streaming_next_turn_drain_routes_via_process_session_index_mapping(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    cfg.PROCESS_SESSION_INDEX["gateway-session-key"] = "webui-session-1"
    evt = _async_delegation_event(session_key="gateway-session-key")
    registry.completion_queue.put(evt)
    handoffs = []
    monkeypatch.setattr(bp, "_process_one", lambda event: handoffs.append(event))

    assert streaming._drain_webui_process_notifications("webui-session-2") is None
    assert handoffs == []
    assert streaming._drain_webui_process_notifications("webui-session-1") is None

    assert handoffs == [evt]
    assert delivery["claim"] == []
    assert delivery["complete"] == []


def test_claim_held_by_crashed_owner_schedules_retry_instead_of_dropping(monkeypatch):
    _reset_wakeup_state()
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "webui-session-1"
    registry = _install_fake_process_registry(monkeypatch)
    _install_fake_durable_delivery_api(monkeypatch)
    async_delivery = sys.modules["tools.async_delegation"]
    async_delivery.claim_event_delivery = lambda _evt, _consumer: None
    registry.completion_queue.put(_async_delegation_event())

    try:
        assert streaming._drain_webui_process_notifications("webui-session-1") is None
        assert registry.completion_queue.empty()
        assert peu.async_delivery_retry_timer_count() == 1
    finally:
        _reset_wakeup_state()


def test_background_unmapped_durable_event_schedules_retry(monkeypatch):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    _install_fake_durable_delivery_api(monkeypatch)
    evt = _async_delegation_event()

    try:
        bp._process_one(evt)
        assert registry.completion_queue.empty()
        assert peu.async_delivery_retry_timer_count() == 1
    finally:
        _reset_wakeup_state()


def test_pending_durable_claim_is_requeued_after_lease_retry(monkeypatch):
    _reset_wakeup_state()
    _install_fake_durable_delivery_api(monkeypatch)
    retry_queue = queue.Queue()
    evt = _async_delegation_event()

    assert peu.schedule_async_delegation_claim_retry(evt, retry_queue, delay=0.01)
    assert peu.schedule_async_delegation_claim_retry(evt, retry_queue, delay=0.01)
    assert peu.async_delivery_retry_timer_count() == 1

    retried = retry_queue.get(timeout=1)
    assert retried["delegation_id"] == "deleg_test123"
    assert _wait_until(lambda: peu.async_delivery_retry_timer_count() == 0)


def test_delivered_or_legacy_event_is_not_scheduled_for_lease_retry(monkeypatch):
    _reset_wakeup_state()
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    delivery["delivery_state"] = "delivered"
    retry_queue = queue.Queue()

    assert not peu.schedule_async_delegation_claim_retry(
        _async_delegation_event(), retry_queue, delay=0.01
    )
    assert not peu.schedule_async_delegation_claim_retry(
        _async_delegation_event(delegation_id=None), retry_queue, delay=0.01
    )
    assert retry_queue.empty()
    assert peu.async_delivery_retry_timer_count() == 0


def test_async_delivery_retry_uses_one_lossless_restore_sweep(monkeypatch):
    _reset_wakeup_state()
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    retry_queue = queue.Queue()
    cap = peu.LEGACY_ASYNC_DELIVERY_DEDUPE_MAX
    try:
        for index in range(cap + 100):
            assert peu.schedule_async_delegation_claim_retry(
                _async_delegation_event(delegation_id=f"deleg_timer_{index}"),
                retry_queue,
                delay=0.2,
            )
        assert peu.async_delivery_retry_timer_count() == 1
        assert _wait_until(lambda: retry_queue.qsize() == cap + 100)
        restored = {
            retry_queue.get_nowait()["delegation_id"] for _ in range(cap + 100)
        }
        assert "deleg_timer_0" in restored
        assert f"deleg_timer_{cap + 99}" in restored
        assert restored == delivery["pending_ids"]
        assert _wait_until(lambda: peu.async_delivery_retry_timer_count() == 0)
    finally:
        _reset_wakeup_state()


def test_async_delivery_restore_sweep_retries_transient_store_failure(monkeypatch):
    _reset_wakeup_state()
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    delivery["restore_failures"] = 1
    retry_queue = queue.Queue()
    monkeypatch.setattr(peu, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 0.01)
    try:
        assert peu.schedule_async_delegation_claim_retry(
            _async_delegation_event(delegation_id="deleg_transient"),
            retry_queue,
            delay=0.01,
        )
        retried = retry_queue.get(timeout=1)
        assert retried["delegation_id"] == "deleg_transient"
        assert delivery["restore_failures"] == 0
        assert _wait_until(lambda: peu.async_delivery_retry_timer_count() == 0)
    finally:
        _reset_wakeup_state()


def test_legacy_completion_prefers_mark_completion_delivered(monkeypatch):
    _reset_wakeup_state()
    calls = {"mark": [], "legacy": []}
    fake_mod = types.ModuleType("tools.async_delegation")
    fake_mod.mark_completion_delivered = lambda delegation_id: calls["mark"].append(delegation_id) or True
    fake_mod.mark_async_delegation_consumed = lambda delegation_id: calls["legacy"].append(delegation_id)
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)
    evt = _async_delegation_event()

    claim = peu.claim_async_delegation_delivery(evt, "legacy-marker-test")
    assert claim is not None
    peu.complete_async_delegation_delivery(evt, claim)

    assert calls == {"mark": ["deleg_test123"], "legacy": []}


def test_legacy_completion_falls_back_to_old_consumed_marker(monkeypatch):
    _reset_wakeup_state()
    calls = {"mark": [], "legacy": []}
    fake_mod = types.ModuleType("tools.async_delegation")
    fake_mod.mark_completion_delivered = lambda delegation_id: calls["mark"].append(delegation_id) or False
    fake_mod.mark_async_delegation_consumed = lambda delegation_id: calls["legacy"].append(delegation_id)
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", fake_mod)
    monkeypatch.setattr(fake_pkg, "async_delegation", fake_mod, raising=False)
    evt = _async_delegation_event()

    claim = peu.claim_async_delegation_delivery(evt, "legacy-marker-test")
    assert claim is not None
    peu.complete_async_delegation_delivery(evt, claim)

    assert calls == {"mark": ["deleg_test123"], "legacy": ["deleg_test123"]}


def test_legacy_async_delivery_dedupe_is_bounded(monkeypatch):
    _reset_wakeup_state()
    fake_pkg = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", types.ModuleType("tools.async_delegation"))

    cap = peu.LEGACY_ASYNC_DELIVERY_DEDUPE_MAX
    for index in range(5000):
        evt = _async_delegation_event(delegation_id=f"deleg_legacy_{index}")
        claim = peu.claim_async_delegation_delivery(evt, "bounded-growth-test")
        assert claim is not None
        peu.complete_async_delegation_delivery(evt, claim)

    assert peu.legacy_async_delivery_dedupe_size() == cap


def test_real_core_restart_delivers_async_completion_exactly_once(tmp_path):
    """A real durable core record must not be restored after WebUI accepts it."""
    try:
        from tools import async_delegation as ad
    except Exception as exc:
        pytest.skip(f"hermes-agent async delegation module unavailable: {exc}")
    if not all(
        hasattr(ad, name)
        for name in ("claim_event_delivery", "complete_event_delivery", "release_event_delivery")
    ):
        pytest.skip("installed hermes-agent predates durable completion claims")

    webui_repo = Path(__file__).resolve().parents[1]
    agent_repo = Path(ad.__file__).resolve().parents[1]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    env["HERMES_BASE_HOME"] = str(tmp_path)
    env["HERMES_CONFIG_PATH"] = str(tmp_path / "config.yaml")
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path)
    env["HERMES_WEBUI_TEST_STATE_DIR"] = str(tmp_path)
    env["HERMES_WEBUI_NO_DOTENV"] = "1"
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_repo)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(webui_repo), str(agent_repo), env.get("PYTHONPATH", "")) if part
    )

    python_executable = os.environ.get("HERMES_WEBUI_PYTHON") or sys.executable
    consumer = """
import json
import time
from api import background_process as bp, routes, streaming
from tools import async_delegation as ad
bp.register_process_session("webui-session-1", "webui-session-1")
accepted = []
def accept_turn(*_args, **kwargs):
    kwargs["completion_acceptance"]()
    accepted.append(kwargs.get("completion_delivery_id"))
    return {"_status": 200, "stream_id": "canonical-test-stream"}
routes.start_session_turn = accept_turn
record = ad.dispatch_async_delegation(
    goal="restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="webui-session-1", parent_session_id="webui-session-1",
    runner=lambda: {"status": "completed", "summary": "after restart"},
)
deadline = time.time() + 10
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
assert not ad.active_count()
streaming._drain_webui_process_notifications("webui-session-1")
deadline = time.time() + 3
row = ad.get_durable_delegation(record["delegation_id"])
while row["delivery_state"] != "delivered" and time.time() < deadline:
    time.sleep(.01)
    row = ad.get_durable_delegation(record["delegation_id"])
print(json.dumps({
    "delegation_id": record["delegation_id"],
    "deliveries": len(accepted),
    "delivery_state": row["delivery_state"],
}))
"""
    first = subprocess.run(
        [python_executable, "-c", consumer],
        cwd=webui_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    accepted = json.loads(first.stdout.strip().splitlines()[-1])
    assert accepted["deliveries"] == 1
    assert accepted["delivery_state"] == "delivered"

    restart_probe = """
from tools.process_registry import process_registry
print(process_registry.completion_queue.qsize())
"""
    second = subprocess.run(
        [python_executable, "-c", restart_probe],
        cwd=webui_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip().splitlines()[-1] == "0"

def test_real_core_active_claim_survives_restart_and_retries_after_lease(tmp_path):
    """A restored event with a live claim is retried after lease expiry, not lost."""
    try:
        from tools import async_delegation as ad
    except Exception as exc:
        pytest.skip(f"hermes-agent async delegation module unavailable: {exc}")
    if not all(
        hasattr(ad, name)
        for name in (
            "claim_event_delivery",
            "complete_event_delivery",
            "release_event_delivery",
            "get_durable_delegation",
        )
    ):
        pytest.skip("installed hermes-agent predates durable completion claims")

    webui_repo = Path(__file__).resolve().parents[1]
    agent_repo = Path(ad.__file__).resolve().parents[1]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    env["HERMES_BASE_HOME"] = str(tmp_path)
    env["HERMES_CONFIG_PATH"] = str(tmp_path / "config.yaml")
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path)
    env["HERMES_WEBUI_TEST_STATE_DIR"] = str(tmp_path)
    env["HERMES_WEBUI_NO_DOTENV"] = "1"
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_repo)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(webui_repo), str(agent_repo), env.get("PYTHONPATH", "")) if part
    )
    python_executable = os.environ.get("HERMES_WEBUI_PYTHON") or sys.executable

    producer = """
import time
from tools import async_delegation as ad
from tools.process_registry import process_registry
record = ad.dispatch_async_delegation(
    goal="lease-restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="webui-session-1", parent_session_id="webui-session-1",
    runner=lambda: {"status": "completed", "summary": "after lease"},
)
deadline = time.time() + 10
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
assert not ad.active_count()
evt = process_registry.completion_queue.get_nowait()
claim = ad.claim_event_delivery(evt, "crashed-owner")
assert claim
print(record["delegation_id"])
"""
    first = subprocess.run(
        [python_executable, "-c", producer],
        cwd=webui_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    delegation_id = next(
        line.strip()
        for line in reversed(first.stdout.splitlines())
        if line.strip().startswith("deleg_")
    )

    consumer = f"""
import json
import time
from tools import async_delegation as ad
from api import background_process as bp, process_event_utils as peu, routes, streaming
from tools.process_registry import process_registry
bp.register_process_session("webui-session-1", "webui-session-1")
accepted = []
def accept_turn(*_args, **kwargs):
    kwargs["completion_acceptance"]()
    accepted.append(kwargs.get("completion_delivery_id"))
    return {{"_status": 200, "stream_id": "canonical-lease-stream"}}
routes.start_session_turn = accept_turn
peu.ASYNC_DELIVERY_CLAIM_RETRY_SECONDS = 0.05
streaming._drain_webui_process_notifications("webui-session-1")
first_timers = peu.async_delivery_retry_timer_count()
with ad._DB_LOCK, ad._connect() as conn:
    conn.execute(
        "UPDATE async_delegations SET delivery_claimed_at=? WHERE delegation_id=?",
        (time.time() - 301, {delegation_id!r}),
    )
deadline = time.time() + 3
while process_registry.completion_queue.empty() and time.time() < deadline:
    time.sleep(.01)
streaming._drain_webui_process_notifications("webui-session-1")
deadline = time.time() + 3
row = ad.get_durable_delegation({delegation_id!r})
while row["delivery_state"] != "delivered" and time.time() < deadline:
    time.sleep(.01)
    row = ad.get_durable_delegation({delegation_id!r})
print(json.dumps({{
    "first_deliveries": 0,
    "scheduled_retries": first_timers,
    "second_deliveries": len(accepted),
    "delivery_state": row["delivery_state"] if row else None,
}}))
"""
    second = subprocess.run(
        [python_executable, "-c", consumer],
        cwd=webui_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout.strip().splitlines()[-1])
    assert result == {
        "first_deliveries": 0,
        "scheduled_retries": 1,
        "second_deliveries": 1,
        "delivery_state": "delivered",
    }

    probe = subprocess.run(
        [
            python_executable,
            "-c",
            "from tools.process_registry import process_registry; print(process_registry.completion_queue.qsize())",
        ],
        cwd=webui_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip().splitlines()[-1] == "0"


# ── Combined-path coverage: #6185 origin-routing ⊕ #6159 durable-claims ──────
# Neither PR alone exercises the interaction where the origin return address
# overrides the mutable session-key index AND the routed delivery still flows
# through the durable claim/complete ack. These guard the combine seam.


def test_origin_ui_session_id_overrides_index_and_still_acks(monkeypatch):
    """An async completion whose session-key index resolves to session B but
    whose origin_ui_session_id is A must start the wakeup turn in A (origin
    wins), and still take exactly one durable claim + one ack."""
    _reset_wakeup_state()
    _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    # The session-key index would route to session "B" ...
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "session-B"
    started: list[tuple[str, str, str]] = []

    def _accept(session_id, prompt, *, delegation_id, evt, claim, process_registry):
        started.append((session_id, prompt, delegation_id))
        bp._record_async_delegation_accepted(evt, session_id=session_id, claim=claim)

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda session_id: False)
    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", _accept)
    monkeypatch.setattr(
        bp, "_emit_bg_task_complete_events_coalesced", lambda session_id, payload: 1
    )

    # ... but the event carries the exact origin return address "session-A".
    bp._process_one(_async_delegation_event(origin_ui_session_id="session-A"))

    assert started, "wakeup turn was never started"
    assert started[0][0] == "session-A", (
        f"origin_ui_session_id must win over the index; started in {started[0][0]!r}"
    )
    assert [consumer for _evt, consumer in delivery["claim"]] == ["webui-background"]
    assert len(delivery["complete"]) == 1  # exactly one ack, in the origin session
    assert delivery["release"] == []


def test_origin_only_completion_without_session_key_routes_and_acks(monkeypatch):
    """A completion with no session_key at all but a valid origin_ui_session_id
    survives the drop-paths (origin is a sufficient route) and delivers+acks."""
    _reset_wakeup_state()
    _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    started: list[str] = []

    def _accept(session_id, prompt, *, delegation_id, evt, claim, process_registry):
        started.append(session_id)
        bp._record_async_delegation_accepted(evt, session_id=session_id, claim=claim)

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda session_id: False)
    monkeypatch.setattr(bp, "_start_async_delegation_wakeup_turn", _accept)
    monkeypatch.setattr(
        bp, "_emit_bg_task_complete_events_coalesced", lambda session_id, payload: 1
    )

    evt = _async_delegation_event(origin_ui_session_id="session-A")
    evt.pop("session_key", None)
    bp._process_one(evt)

    assert started == ["session-A"]
    assert len(delivery["complete"]) == 1


def test_async_completion_with_unresolvable_target_retries_not_silent_drop(monkeypatch):
    """The combine caveat: an async event that resolves to an empty target must
    route through the bounded retry (so the durable row stays retryable), NOT a
    bare return that would leave it pending forever with no ack and no retry."""
    _reset_wakeup_state()
    _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    retried: list[dict] = []
    monkeypatch.setattr(
        bp,
        "_retry_unclaimed_async_delegation_event",
        lambda process_registry, evt: retried.append(dict(evt)),
    )

    # No session_key index entry and no origin_ui_session_id → empty target.
    evt = _async_delegation_event()
    evt.pop("session_key", None)
    bp._process_one(evt)

    assert len(retried) == 1, "unrouteable async event must be handed to the retry"
    # No claim was taken and no ACK fired — the durable row stays pending, and
    # NOTHING was completed (which would have been a spurious ack).
    assert delivery["claim"] == []
    assert delivery["complete"] == []


def test_next_turn_drain_respects_origin_over_session_key_index(monkeypatch):
    """Immutable origin beats the mutable index in the physical prefilter.

    A mismatched indexed session cannot steal, claim, or settle the event. The
    true origin receives one unchanged handoff to canonical ``_process_one``;
    the streaming source performs no fresh settlement itself.
    """
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    # The session-key index would route this event to session "B" ...
    cfg.PROCESS_SESSION_INDEX["webui-session-1"] = "session-B"
    evt = _async_delegation_event(origin_ui_session_id="session-A")
    registry.completion_queue.put(evt)
    handoffs = []
    monkeypatch.setattr(bp, "_process_one", lambda event: handoffs.append(event))

    # Session B must skip/requeue unchanged, taking no claim or ACK.
    assert streaming._drain_webui_process_notifications("session-B") is None
    assert handoffs == []
    assert delivery["claim"] == []
    assert delivery["complete"] == []

    # Session A hands off once; direct source settlement remains absent.
    assert streaming._drain_webui_process_notifications("session-A") is None
    assert handoffs == [evt]
    assert delivery["claim"] == []
    assert delivery["complete"] == []
    assert delivery["release"] == []


def test_async_ack_follows_incorporated_receipt_and_restart_does_not_replay(
    monkeypatch,
    tmp_path,
):
    _reset_wakeup_state()
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SESSION_DIR", tmp_path, raising=False)
    with cfg.LOCK:
        cfg.SESSIONS.clear()
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    session = Session(session_id="webui-session-1", title="async", profile="default")
    session.save()
    with cfg.LOCK:
        cfg.SESSIONS[session.session_id] = session

    event = _async_delegation_event(origin_ui_session_id=session.session_id)
    context = build_completion_delivery_context(event, session.session_id, session_dir=tmp_path)
    claim = peu.claim_async_delegation_delivery(event, "webui-background")
    assert claim is not None
    admission_box = {}

    class ParkedThread:
        def __init__(self, *args, **kwargs):
            admission_box["admission"] = kwargs["kwargs"]["admission"]

        def start(self):
            admission_box["admission"].admitted.set()

    class ImmediateThread:
        def __init__(self, *args, target=None, **kwargs):
            self.target = target
            self.args = kwargs.pop("args", ())
            self.kwargs = kwargs.pop("kwargs", {})

        def start(self):
            self.target(*self.args, **self.kwargs)

    bp_threading = types.SimpleNamespace(**vars(bp.threading))
    bp_threading.Thread = ImmediateThread
    routes_threading = types.SimpleNamespace(**vars(routes.threading))
    routes_threading.Thread = ParkedThread
    monkeypatch.setattr(bp, "threading", bp_threading)
    monkeypatch.setattr(routes, "threading", routes_threading)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda _session, _requested: str(tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_a, **_k: ("test-model", None, False),
    )
    monkeypatch.setattr(routes, "create_stream_channel", queue.Queue)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)

    durable_api = sys.modules["tools.async_delegation"]
    original_complete = durable_api.complete_event_delivery

    def complete_after_incorporation(evt, claim_id):
        receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
        assert receipt is not None and receipt["state"] == "incorporated"
        original_complete(evt, claim_id)

    durable_api.complete_event_delivery = complete_after_incorporation
    bp._start_async_delegation_wakeup_turn(
        session.session_id,
        "delegation result",
        delegation_id=event["delegation_id"],
        evt=event,
        claim=claim,
        process_registry=registry,
    )

    assert len(delivery["complete"]) == 1
    assert delivery["release"] == []
    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None and receipt["state"] == "incorporated"
    assert routes.recover_accepted_completion_delivery(context) is False
    routes._abort_prepared_chat_turn(
        session,
        admission_box["admission"].stream_id,
        admission_box["admission"],
    )


def test_current_turn_async_drain_crash_restart_executes_once_via_receipt(
    monkeypatch,
    tmp_path,
):
    """R1: a current-turn pop is not an ACK or a substitute for a receipt."""
    from tests.test_background_process_restart_recovery import (
        _configure_completion_recovery,
        _install_counting_completion_core,
    )

    _reset_wakeup_state()
    _configure_completion_recovery(monkeypatch, tmp_path)
    registry = _install_fake_process_registry(monkeypatch)
    delivery = _install_fake_durable_delivery_api(monkeypatch)
    session = Session(
        session_id="current-turn-async",
        title="current-turn async",
        profile="default",
    )
    session.save()
    with cfg.LOCK:
        cfg.SESSIONS[session.session_id] = session

    event = _async_delegation_event(
        delegation_id="deleg_current_turn_restart",
        session_key=session.session_id,
        origin_ui_session_id=session.session_id,
        origin_profile="default",
    )
    registry.completion_queue.put(event)
    context = build_completion_delivery_context(
        event,
        session.session_id,
        session_dir=tmp_path,
    )
    admissions = []
    real_routes_threading = routes.threading

    class ImmediateThread:
        def __init__(self, *args, target=None, **kwargs):
            self.target = target
            self.args = kwargs.pop("args", ())
            self.kwargs = kwargs.pop("kwargs", {})

        def start(self):
            assert self.target is not None
            self.target(*self.args, **self.kwargs)

    class ParkedThread:
        def __init__(self, *_args, **kwargs):
            admissions.append(kwargs["kwargs"]["admission"])

        def start(self):
            admissions[-1].admitted.set()

    bp_threading = types.SimpleNamespace(**vars(bp.threading))
    bp_threading.Thread = ImmediateThread
    routes_threading = types.SimpleNamespace(**vars(routes.threading))
    routes_threading.Thread = ParkedThread
    monkeypatch.setattr(bp, "threading", bp_threading)
    monkeypatch.setattr(routes, "threading", routes_threading)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(bp, "_emit_bg_task_complete_events_coalesced", lambda *_a: 0)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda _session, _requested: str(tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_a, **_k: ("test-model", None, False),
    )
    monkeypatch.setattr(routes, "create_stream_channel", queue.Queue)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)

    streaming._drain_webui_process_notifications(session.session_id)
    print(
        "CURRENT_TURN_ASYNC_DRAIN_REACHED="
        + json.dumps(
            {
                "completion_key": context.completion_key,
                "delivery_state": delivery["delivery_state"],
                "queue_size": registry.completion_queue.qsize(),
            },
            sort_keys=True,
        )
    )

    # Simulate the crash: only receipt/sidecar/journal bytes survive.
    assert len(admissions) == 1
    release_turn_admission(admissions[0])
    with cfg.LOCK:
        cfg.SESSIONS.clear()
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr(routes, "threading", real_routes_threading)
    executions = []
    finished = threading.Event()
    _install_counting_completion_core(monkeypatch, executions, finished)

    class RecoveryRegistry(_FakeProcessRegistry):
        def recover_from_checkpoint(self):
            return 0

        def list_sessions(self):
            return []

    recovery_registry = RecoveryRegistry()
    _install_fake_process_registry(monkeypatch)
    fake_process_mod = sys.modules["tools.process_registry"]
    fake_process_mod.process_registry = recovery_registry
    assert bp.recover_processes_for_webui(
        recovery_registry,
        lambda *_args, **_kwargs: None,
    ) == 0

    # Baseline reaches the named drain sentinel but has no receipt to resume,
    # so this is a behavioral RED (provider count 0), never a harness error.
    assert finished.wait(timeout=2)
    assert executions == [
        (
            session.session_id,
            bp.format_wakeup_prompt(event),
            context.correlation_id[:32],
        )
    ]
    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None
    assert _wait_until(
        lambda: (
            read_completion_delivery_receipt(context, session_dir=tmp_path) or {}
        ).get("execution_state")
        == "delivered"
    )
    assert delivery["delivery_state"] == "delivered"
    assert len(delivery["complete"]) == 1

    metadata = completion_delivery_metadata(context)
    persisted = json.loads(
        (tmp_path / f"{session.session_id}.json").read_text(encoding="utf-8")
    )
    assert len(
        [row for row in persisted["messages"] if row.get("_completion_delivery") == metadata]
    ) == 1
    assert len(
        [
            row
            for row in persisted["context_messages"]
            if row.get("_completion_delivery") == metadata
        ]
    ) == 1
    assert len(
        [
            row
            for row in read_turn_journal(session.session_id, session_dir=tmp_path)["events"]
            if row.get("_completion_delivery") == metadata
        ]
    ) == 1

    with cfg.LOCK:
        cfg.SESSIONS.clear()
    with cfg.ACTIVE_RUNS_LOCK:
        cfg.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    assert bp.recover_processes_for_webui(
        recovery_registry,
        lambda *_args, **_kwargs: None,
    ) == 0
    assert len(executions) == 1


