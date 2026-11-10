"""Async-delegation delivery must use the ledger of the profile that owns the row (#7214)."""

from __future__ import annotations

import contextvars
import queue
import sqlite3
import sys
import time
import types

import pytest

from api import delegation_ledger_owner as owner_mod
from api import process_event_utils as peu


def _ledger(home, *ids):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO async_delegations VALUES (?)", [(i,) for i in ids])


def _event(delegation_id="d-1", session="s-1"):
    return {"type": "async_delegation", "delegation_id": delegation_id, "origin_ui_session_id": session}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles

    root = (tmp_path / "root").resolve()
    named = root / "profiles" / "named"
    root.mkdir()
    named.mkdir(parents=True)
    homes = {"default": root, "named": named}
    sessions = {}
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda name: homes.get(name or "default", root))
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "default", "is_default": True}, {"name": "named"}])
    monkeypatch.setattr(models, "get_session_profile_readonly", lambda sid: sessions[sid])  # KeyError like the real reader

    # Agent stand-ins: a context-local home override, and ledger calls that record it.
    current = contextvars.ContextVar("home", default="ambient")
    constants = types.ModuleType("hermes_constants")
    constants.set_hermes_home_override = lambda home: current.set(str(home))
    constants.reset_hermes_home_override = current.reset
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    calls = []
    agent = types.ModuleType("tools.async_delegation")
    agent.claim_event_delivery = lambda evt, consumer: calls.append(("claim", current.get())) or "c-1"
    agent.complete_event_delivery = lambda evt, claim_id: calls.append(("ack", current.get()))
    agent.release_event_delivery = lambda evt, claim_id: calls.append(("release", current.get()))
    agent.get_durable_delegation = lambda did: calls.append(("get", current.get())) or {"delivery_state": "pending"}
    agent.restore_undelivered_completions = lambda q: calls.append(("restore", current.get()))
    pkg = types.ModuleType("tools")
    pkg.async_delegation = agent
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.async_delegation", agent)
    peu._reset_legacy_async_delivery_dedupe_for_tests()
    yield {"root": root, "named": named, "sessions": sessions, "calls": calls, "profiles": profiles}
    peu._reset_legacy_async_delivery_dedupe_for_tests()


def test_resolution_statuses(world):
    assert owner_mod.resolve_ledger_owner(_event()).status == "missing"  # legacy no-row event
    _ledger(world["named"], "d-1")
    assert owner_mod.resolve_ledger_owner(_event()) == owner_mod.LedgerOwner("found", world["named"])
    _ledger(world["root"], "d-1")
    assert owner_mod.resolve_ledger_owner(_event()).status == "unknown"  # ambiguous
    world["sessions"]["s-1"] = "named"
    assert owner_mod.resolve_ledger_owner(_event()).home == world["named"]  # provenance decides


def test_unreadable_or_incomplete_enumeration_is_unknown(world, monkeypatch):
    (world["named"] / "state.db").write_bytes(b"not a database" * 100)
    assert owner_mod.resolve_ledger_owner(_event()).status == "unknown"
    (world["named"] / "state.db").unlink()
    world["named"].chmod(0)  # permission denied is unreadable, not missing
    try:
        assert owner_mod.resolve_ledger_owner(_event()).status == "unknown"
    finally:
        world["named"].chmod(0o700)
    monkeypatch.setattr(world["profiles"], "list_profiles_api", lambda: [{"name": ""}])
    assert owner_mod.resolve_ledger_owner(_event()).status == "unknown"
    with pytest.raises(RuntimeError):
        owner_mod.authorized_ledger_homes()


def test_claim_ack_release_and_retry_use_the_owner_ledger(world):
    _ledger(world["named"], "d-1")
    claim = peu.claim_async_delegation_delivery(_event(), "test")
    peu.release_async_delegation_delivery(_event(), claim)
    peu.schedule_async_delegation_claim_retry(_event(), queue.Queue(), delay=60, claim=claim)
    claim = peu.claim_async_delegation_delivery(_event(), "test")
    peu.complete_async_delegation_delivery(_event(), claim)
    named = str(world["named"])
    assert world["calls"] == [("claim", named), ("release", named), ("get", named), ("claim", named), ("ack", named)]


def test_unknown_owner_is_deferred_not_claimed_or_dropped(world):
    _ledger(world["root"], "d-1")
    _ledger(world["named"], "d-1")
    assert peu.claim_async_delegation_delivery(_event(), "test") is None
    assert peu.legacy_async_delivery_dedupe_size() == 0
    q = queue.Queue()
    assert peu.schedule_async_delegation_claim_retry(_event(), q, delay=0)
    assert q.get(timeout=2) == _event()
    assert world["calls"] == []


def test_failed_home_binding_never_runs_against_the_ambient_ledger(world, monkeypatch):
    _ledger(world["named"], "d-1")

    def broken(_home):
        raise RuntimeError("binding failed")

    monkeypatch.setattr(sys.modules["hermes_constants"], "set_hermes_home_override", broken)
    with pytest.raises(RuntimeError):
        peu.claim_async_delegation_delivery(_event(), "test")
    assert world["calls"] == []
    assert peu.legacy_async_delivery_dedupe_size() == 0


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_drain_startup_recovers_from_one_enumeration_failure_without_another_restore(world, monkeypatch):
    from api import background_process as bp

    monkeypatch.setattr(peu, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 0.05)
    registry = types.SimpleNamespace(restore_completions=lambda: None, completion_queue=queue.Queue())
    registry_mod = types.ModuleType("tools.process_registry")
    registry_mod.process_registry = registry
    monkeypatch.setitem(sys.modules, "tools.process_registry", registry_mod)
    monkeypatch.setattr(bp, "recover_processes_for_webui", lambda: 0)
    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    restores = []
    real_restore = peu.restore_durable_process_completions

    def counted_restore(reg):
        restores.append(reg)
        real_restore(reg)

    monkeypatch.setattr(bp, "restore_durable_process_completions", counted_restore)
    listed = world["profiles"].list_profiles_api
    failures = iter([True])

    def flaky():
        if next(failures, False):
            raise RuntimeError("transient enumeration failure")
        return listed()

    monkeypatch.setattr(world["profiles"], "list_profiles_api", flaky)
    try:
        assert bp.start_drain_thread()
        replayed = sorted([("restore", str(world["root"])), ("restore", str(world["named"]))])
        assert _wait_for(lambda: sorted(world["calls"]) == replayed)
    finally:
        bp.stop_drain_thread()
    assert restores == [registry]  # the drain entered startup restore exactly once
    assert peu._NON_LAUNCH_REPLAY_DONE


def test_failed_startup_enumeration_keeps_a_single_retry_timer(world, monkeypatch):
    monkeypatch.setattr(peu, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 0.2)
    registry = types.SimpleNamespace(restore_completions=lambda: None, completion_queue=queue.Queue())
    attempts = []
    armed = []
    real_timer = peu.threading.Timer

    def recording_timer(*args, **kwargs):
        timer = real_timer(*args, **kwargs)
        armed.append(timer)
        return timer

    def failing():
        attempts.append(time.monotonic())
        raise RuntimeError("enumeration still failing")

    monkeypatch.setattr(peu.threading, "Timer", recording_timer)
    monkeypatch.setattr(world["profiles"], "list_profiles_api", failing)
    for _ in range(10):
        peu.restore_durable_process_completions(registry)
    assert len(attempts) == 10
    assert len(armed) == 1  # repeated failures share one pending retry
    assert _wait_for(lambda: len(attempts) >= 11)  # the timer itself retried
    assert _wait_for(lambda: len(armed) == 2)  # and re-armed exactly one successor
    assert sum(t.is_alive() for t in armed) <= 1
    assert world["calls"] == []


def test_startup_replay_retries_by_itself_after_enumeration_failure(world, monkeypatch):
    monkeypatch.setattr(peu, "ASYNC_DELIVERY_ROUTING_RETRY_SECONDS", 0.05)
    registry = types.SimpleNamespace(restore_completions=lambda: None, completion_queue=queue.Queue())
    listed = world["profiles"].list_profiles_api
    failures = iter([True])

    def flaky():
        if next(failures, False):
            raise RuntimeError("enumeration failed")
        return listed()

    monkeypatch.setattr(world["profiles"], "list_profiles_api", flaky)
    # The background drain calls this once; no later turn may be needed to recover.
    peu.restore_durable_process_completions(registry)
    deadline = time.monotonic() + 2
    while len(world["calls"]) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    replayed = sorted([("restore", str(world["root"])), ("restore", str(world["named"]))])
    assert sorted(world["calls"]) == replayed
    peu.restore_durable_process_completions(registry)
    time.sleep(0.2)
    assert sorted(world["calls"]) == replayed  # once only
