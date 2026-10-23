"""Regression coverage for the operator-initiated stale-runtime restart route (#7934).

The route lets an operator recover from ``409 agent_runtime_stale`` without a
shell, while every guard that keeps the restart manual stays fail-closed:

* only a genuinely stale runtime is restartable (never a general button),
* an Agent update that is active/incomplete/unknown blocks the restart,
* active chat work blocks the restart exactly like ``/api/updates/apply``,
* the stale→schedule transition is re-checked under ``_apply_lock`` so a
  state change between admission and scheduling cannot slip through.
"""
from __future__ import annotations

import types

import pytest


REPO = None  # populated lazily; tests import through the repo conftest


@pytest.fixture
def restart_env(monkeypatch):
    """Wire the restart route against faked guard/lock/scheduler collaborators."""
    from api import agent_runtime, routes, updates

    calls = types.SimpleNamespace(schedule=0, ensure=0, transaction=0)
    state = types.SimpleNamespace(
        stale=True,
        transaction="unverified",
        transaction_after_lock=None,
        under_lock_exc_bare=False,
        blocked=False,
        lock_free=True,
        stale_after_lock=True,
    )

    def fake_ensure():
        calls.ensure += 1
        # First call is the admission check; the second is the post-lock
        # re-check, governed independently so the TOCTOU case can be forced.
        if calls.ensure > 1:
            if not state.stale_after_lock:
                return
            if state.under_lock_exc_bare:
                raise agent_runtime.AgentRuntimeChangedError(
                    agent_runtime._RESTART_REQUIRED_MESSAGE,
                )
            under_lock_state = (
                state.transaction_after_lock
                if state.transaction_after_lock is not None
                else state.transaction
            )
            raise agent_runtime.AgentRuntimeChangedError(
                agent_runtime._RESTART_REQUIRED_MESSAGE,
                agent_update_state=under_lock_state,
            )
        elif not state.stale:
            return
        raise agent_runtime.AgentRuntimeChangedError(
            agent_runtime._RESTART_REQUIRED_MESSAGE,
            agent_update_state=state.transaction,
        )

    def fake_transaction():
        calls.transaction += 1
        if calls.transaction > 1 and state.transaction_after_lock is not None:
            return state.transaction_after_lock
        return state.transaction

    def fake_schedule(delay: float = 2.0, *, transaction_guard: bool = False) -> None:
        calls.schedule += 1
        calls.transaction_guard = transaction_guard

    def fake_blocker() -> dict:
        if state.blocked:
            return {
                "active_streams": 2,
                "active_runs": 0,
                "blocking_stream_ids": ["s1", "s2"],
                "blocking_run_ids": [],
                "restart_blocked": True,
            }
        return {
            "active_streams": 0,
            "active_runs": 0,
            "blocking_stream_ids": [],
            "blocking_run_ids": [],
            "restart_blocked": False,
        }

    class FakeLock:
        def __init__(self) -> None:
            self.held = False
            self.acquires = 0

        def acquire(self, blocking: bool = True) -> bool:
            self.acquires += 1
            if not state.lock_free:
                return False
            self.held = True
            return True

        def release(self) -> None:
            self.held = False

    lock = FakeLock()
    monkeypatch.setattr(agent_runtime, "ensure_agent_runtime_current", fake_ensure)
    monkeypatch.setattr(agent_runtime, "_agent_update_transaction_state", fake_transaction)
    monkeypatch.setattr(updates, "_schedule_restart", fake_schedule)
    monkeypatch.setattr(updates, "_restart_blocker_snapshot", fake_blocker)
    monkeypatch.setattr(updates, "_apply_lock", lock)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {"status": status, "payload": payload},
    )
    return types.SimpleNamespace(
        calls=calls, state=state, lock=lock, updates=updates, routes=routes,
    )


# ── happy path ──


def test_stale_runtime_schedules_restart(restart_env):
    """Stale + no marker + no active work → the restart is scheduled."""
    result = restart_env.updates.restart_stale_runtime()
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert restart_env.calls.schedule == 1
    assert restart_env.calls.transaction_guard is True, (
        "the recovery route must arm the delayed worker's transaction guard"
    )
    assert restart_env.lock.held is False, "the lock must be released after scheduling"


# ── refusal: runtime already current ──


def test_current_runtime_is_refused(restart_env):
    """A current runtime is never restartable — no general restart button."""
    restart_env.state.stale = False
    result = restart_env.updates.restart_stale_runtime()
    assert result["ok"] is False
    assert result["type"] == "agent_runtime_current"
    assert restart_env.calls.schedule == 0


# ── refusal: an Agent update is in flight ──


@pytest.mark.parametrize("update_state", ["active", "incomplete", "unknown"])
def test_agent_update_in_flight_refuses_with_stale_payload(restart_env, update_state):
    """active/incomplete refuse per spec; unknown fails closed the same way."""
    restart_env.state.transaction = update_state
    result = restart_env.updates.restart_stale_runtime()
    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == update_state
    assert result["restart_scheduled"] is False
    assert result["restart_available"] is True
    assert restart_env.calls.schedule == 0


# ── refusal: an update starts after admission, under the lock ──


@pytest.mark.parametrize("update_state", ["active", "incomplete", "unknown"])
def test_update_started_under_lock_refuses_without_scheduling(restart_env, update_state):
    """An update that begins between the admission scan and lock admission
    must be refused with its freshest diagnostic — the under-lock exception
    is inspected, not discarded, and no restart is scheduled."""
    restart_env.state.transaction = "unverified"  # admission sees a clean idle
    restart_env.state.transaction_after_lock = update_state
    result = restart_env.updates.restart_stale_runtime()
    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == update_state
    assert result["restart_scheduled"] is False
    assert result["restart_available"] is True
    assert restart_env.calls.schedule == 0
    assert restart_env.lock.held is False, "the lock must be released on refusal"


def test_bare_under_lock_exception_resamples_and_refuses(restart_env):
    """A diagnostic-less under-lock exception still fails closed: the
    transaction is resampled at the lock and the refusal uses that state."""
    restart_env.state.transaction = "unverified"
    restart_env.state.transaction_after_lock = "incomplete"
    restart_env.state.under_lock_exc_bare = True
    result = restart_env.updates.restart_stale_runtime()
    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == "incomplete"
    assert result["restart_scheduled"] is False
    assert restart_env.calls.schedule == 0
    assert restart_env.lock.held is False


# ── refusal: active chat work ──


def test_active_streams_block_the_restart(restart_env):
    """Active work returns the existing restart-blocked response, no restart."""
    restart_env.state.blocked = True
    result = restart_env.updates.restart_stale_runtime()
    assert result["ok"] is False
    assert result["restart_blocked"] is True
    assert result["active_streams"] == 2
    assert restart_env.calls.schedule == 0


# ── refusal: an update holds _apply_lock ──


def test_apply_lock_held_refuses(restart_env):
    """A concurrent update holding _apply_lock blocks the restart."""
    restart_env.state.lock_free = False
    result = restart_env.updates.restart_stale_runtime()
    assert result["ok"] is False
    assert result.get("restart_scheduled") is not True
    assert restart_env.calls.schedule == 0


# ── atomic admission: stale flips to current under the lock ──


def test_stale_to_current_under_lock_never_schedules(restart_env):
    """The post-lock re-check catches a runtime that became current mid-flight."""
    restart_env.state.stale_after_lock = False  # second ensure() passes: current
    result = restart_env.updates.restart_stale_runtime()
    assert result["ok"] is False
    assert result["type"] == "agent_runtime_current"
    assert restart_env.calls.schedule == 0
    assert restart_env.lock.held is False


# ── discoverability payload ──


def test_stale_payload_advertises_restart_support():
    """Clients only offer Restart when the payload says the server supports it."""
    from api import agent_runtime

    exc = agent_runtime.AgentRuntimeChangedError("stale", agent_update_state="unverified")
    payload = agent_runtime.agent_runtime_stale_payload(exc)
    assert payload["restart_available"] is True
    assert payload["type"] == "agent_runtime_stale"
    assert payload["retryable"] is True


# ── HTTP route surface ──


def test_route_maps_current_refusal_to_409(restart_env, monkeypatch):
    monkeypatch.setattr(
        restart_env.updates,
        "restart_stale_runtime",
        lambda: {"ok": False, "type": "agent_runtime_current", "message": "current"},
    )
    response = restart_env.routes._handle_runtime_restart(object(), {})
    assert response["status"] == 409


def test_route_maps_stale_refusal_to_409(restart_env, monkeypatch):
    monkeypatch.setattr(
        restart_env.updates,
        "restart_stale_runtime",
        lambda: {"ok": False, "type": "agent_runtime_stale", "restart_available": True},
    )
    response = restart_env.routes._handle_runtime_restart(object(), {})
    assert response["status"] == 409


def test_route_maps_restart_blocked_to_200(restart_env, monkeypatch):
    """The restart-blocked body keeps the /api/updates/apply 200 convention."""
    monkeypatch.setattr(
        restart_env.updates,
        "restart_stale_runtime",
        lambda: {"ok": False, "restart_blocked": True, "active_streams": 1},
    )
    response = restart_env.routes._handle_runtime_restart(object(), {})
    assert response["status"] == 200
    assert response["payload"]["restart_blocked"] is True


def test_route_maps_scheduled_restart_to_200(restart_env, monkeypatch):
    monkeypatch.setattr(
        restart_env.updates,
        "restart_stale_runtime",
        lambda: {"ok": True, "restart_scheduled": True},
    )
    response = restart_env.routes._handle_runtime_restart(object(), {})
    assert response["status"] == 200
    assert response["payload"]["restart_scheduled"] is True


# ── shared refusal helper ──


def test_refusal_state_helper_reads_exception_diagnostic():
    """The helper trusts the exception's diagnostic and treats idle-stale as safe."""
    from api import agent_runtime, updates

    unsafe = agent_runtime.AgentRuntimeChangedError(
        "stale", agent_update_state="active",
    )
    assert updates._agent_update_refusal_state(unsafe) == "active"
    idle = agent_runtime.AgentRuntimeChangedError(
        "stale", agent_update_state="unverified",
    )
    assert updates._agent_update_refusal_state(idle) is None
    assert updates._agent_update_refusal_state(None) in {None, "unverified"}


# ── delayed worker: recovery-specific transaction guard ──


@pytest.fixture
def worker_env(monkeypatch):
    """Wire the delayed restart worker against faked guard/lock/scheduler."""
    import os
    import threading

    from api import agent_runtime, updates

    events = types.SimpleNamespace(
        execv=0, warnings=[], infos=[], done=threading.Event(),
    )
    state = types.SimpleNamespace(transaction="unverified", runtime_current=False)

    def fake_ensure():
        if state.runtime_current:
            return
        raise agent_runtime.AgentRuntimeChangedError(
            agent_runtime._RESTART_REQUIRED_MESSAGE,
            agent_update_state=state.transaction,
        )

    class FakeLogger:
        def warning(self, msg, *args):
            events.warnings.append(msg % args if args else msg)
            events.done.set()

        def info(self, msg, *args):
            events.infos.append(msg % args if args else msg)
            events.done.set()

        def exception(self, *args, **kwargs):
            pass

    def fake_execv(path, argv):
        events.execv += 1
        events.done.set()

    monkeypatch.setattr(agent_runtime, "ensure_agent_runtime_current", fake_ensure)
    monkeypatch.setattr(updates, "_wait_until_restart_safe", lambda: None)
    monkeypatch.setattr(updates, "_purge_agent_pycache", lambda _path: None)
    monkeypatch.setattr(updates, "logger", FakeLogger())
    monkeypatch.setattr(os, "execv", fake_execv)
    return types.SimpleNamespace(events=events, state=state, updates=updates)


def _wait_for_worker(env, timeout=5.0):
    assert env.events.done.wait(timeout), "the restart worker never finished"


@pytest.mark.parametrize("update_state", ["active", "incomplete", "unknown"])
def test_worker_aborts_replacement_when_update_starts_mid_delay(worker_env, update_state):
    """An unsafe marker published after admission but before the delayed
    replacement aborts the execv — the recovery route's safety contract."""
    worker_env.state.transaction = update_state
    worker_env.updates._schedule_restart(delay=0.0, transaction_guard=True)
    _wait_for_worker(worker_env)
    assert worker_env.events.execv == 0
    assert any("aborted" in w for w in worker_env.events.warnings)


def test_worker_replaces_when_transaction_stays_allowed(worker_env):
    """Positive control: idle-stale (unverified) still replaces the process."""
    worker_env.state.transaction = "unverified"
    worker_env.updates._schedule_restart(delay=0.0, transaction_guard=True)
    _wait_for_worker(worker_env)
    assert worker_env.events.execv == 1
    assert worker_env.events.warnings == []


def test_worker_skips_replacement_when_runtime_became_current(worker_env):
    """A runtime that became current again has nothing left to recover."""
    worker_env.state.runtime_current = True
    worker_env.updates._schedule_restart(delay=0.0, transaction_guard=True)
    _wait_for_worker(worker_env)
    assert worker_env.events.execv == 0
    assert any("skipped" in i for i in worker_env.events.infos)


def test_successful_update_restart_keeps_unguarded_semantics(worker_env):
    """Other callers (successful updates) must not gain the guard: even an
    active transaction does not abort their replacement."""
    worker_env.state.transaction = "active"
    worker_env.updates._schedule_restart(delay=0.0)
    _wait_for_worker(worker_env)
    assert worker_env.events.execv == 1
