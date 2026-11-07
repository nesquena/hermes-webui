"""Regression coverage for the operator-initiated stale-runtime restart route (#7934).

The route lets an operator recover from ``409 agent_runtime_stale`` without a
shell, while every guard that keeps the restart manual stays fail-closed:

* only a genuinely stale runtime is restartable (never a general button),
* an Agent update that is active/incomplete/unknown blocks the restart,
* active chat work blocks the restart exactly like ``/api/updates/apply``,
* the stale→schedule transition is re-checked under ``_apply_lock`` so a
  state change between admission and scheduling cannot slip through,
* the delayed worker re-reads the REAL update marker across its idle
  wait, so an update that begins after scheduling aborts the
  replacement (the real-boundary regressions below run the shipped
  guard, real markers and the real lock instead of mocked fields).
"""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
import types

import pytest

# The real-boundary regressions below reuse the shared staging fixture that
# already exercises the SHIPPED revision guard — a real Git checkout, a real
# marker directory and a captured (never started) scheduler thread — instead
# of re-faking those collaborators per test module.
from tests.test_agent_runtime_revision_guard import (
    changed_agent_checkout as changed_agent_checkout,
)


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


# ── real-boundary regressions: actual markers against the shipped guard ──
#
# The suites above mock the revision guard, the transaction reader and the
# idle waiter, so they cannot notice a regression that breaks the wiring
# between a REAL marker file and the REAL guard/worker.  The tests below run
# that boundary as shipped: a genuine stale checkout, real markers on disk,
# the real ``_apply_lock``, the real admission scan and under-lock re-check,
# and the real delayed worker (captured, never started).  Only the
# process-replacement calls are inert — and counted.


def _publish_active_marker(checkout):
    """A live-PID update marker: a real Agent update is in flight."""
    marker = checkout.home / ".hermes-update-in-progress"
    marker.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")


def _publish_incomplete_marker(checkout):
    """A recovery marker: the last Agent update did not finish."""
    (checkout.root / ".update-incomplete").touch()


def _publish_unreadable_marker(checkout):
    """An unverifiable marker: the guard must fail closed as unknown."""
    marker = checkout.home / ".hermes-update-in-progress"
    marker.write_text("not-a-pid\nnot-a-time\n", encoding="utf-8")


MARKER_PUBLISHERS = [
    ("active", _publish_active_marker),
    ("incomplete", _publish_incomplete_marker),
    ("unknown", _publish_unreadable_marker),
]


@pytest.fixture
def real_restart_env(changed_agent_checkout, monkeypatch):
    """Run the recovery route and its delayed worker against real markers.

    ``changed_agent_checkout`` stages the genuine stale runtime (real Git
    checkout, real ``ensure_agent_runtime_current``, real marker directory)
    and captures the scheduler's thread.  This env adds only what a test
    must never do for real — the process-replacement calls — plus a seam on
    ``_apply_lock`` to publish a marker between the route's admission scan
    and its under-lock re-check, exactly the window the route must guard.
    """
    from api import agent_runtime, updates

    checkout = changed_agent_checkout
    destructive = types.SimpleNamespace(purge=[], execv=[], spawn=[], exits=[])
    sleeps: list[float] = []
    logs = types.SimpleNamespace(warnings=[], infos=[], exceptions=[])
    hook = types.SimpleNamespace(on_acquire=None)

    def record_purge(repo_dir):
        destructive.purge.append(repo_dir)

    def record_execv(path, argv):
        destructive.execv.append((path, argv))

    def record_spawn(*args, **kwargs):
        destructive.spawn.append(args)

    def record_exit(code):
        destructive.exits.append(code)

    monkeypatch.setattr(updates, "_purge_agent_pycache", record_purge)
    monkeypatch.setattr(os, "execv", record_execv)
    monkeypatch.setattr(updates, "_windows_restart_spawn", record_spawn)
    monkeypatch.setattr(updates, "_windows_restart_exit", record_exit)
    # Keep the worker's idle wait real in shape (it still runs and is
    # recorded) without spending wall-clock seconds per test.
    monkeypatch.setattr(time, "sleep", sleeps.append)

    class RecordingLogger:
        def warning(self, msg, *args):
            logs.warnings.append(msg % args if args else msg)

        def info(self, msg, *args):
            logs.infos.append(msg % args if args else msg)

        def exception(self, msg, *args, **_kwargs):
            logs.exceptions.append(msg)

    monkeypatch.setattr(updates, "logger", RecordingLogger())

    real_lock = updates._apply_lock

    class MarkerPublishingLock:
        """The real ``_apply_lock`` with a publish seam between admission
        and the under-lock re-check."""

        def acquire(self, blocking=True, timeout=-1):
            if hook.on_acquire is not None:
                hook.on_acquire()
            return real_lock.acquire(blocking, timeout)

        def release(self):
            real_lock.release()

        def locked(self):
            return real_lock.locked()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_exc):
            self.release()

    monkeypatch.setattr(updates, "_apply_lock", MarkerPublishingLock())

    def destructive_total():
        return sum(
            len(calls)
            for calls in (
                destructive.purge,
                destructive.execv,
                destructive.spawn,
                destructive.exits,
            )
        )

    return types.SimpleNamespace(
        checkout=checkout,
        updates=updates,
        agent_runtime=agent_runtime,
        hook=hook,
        lock=real_lock,
        destructive=destructive,
        destructive_total=destructive_total,
        logs=logs,
        sleeps=sleeps,
        monkeypatch=monkeypatch,
    )


@pytest.mark.parametrize(("update_state", "publish"), MARKER_PUBLISHERS)
def test_real_marker_under_the_lock_refuses_without_scheduling(
    real_restart_env, update_state, publish,
):
    """A real marker published between admission and the under-lock
    re-check must refuse the restart with its freshest diagnostic — zero
    scheduling, lock released.  Exercises the shipped guard, the real
    marker classification and the real lock, not fake fields."""
    env = real_restart_env
    checkout = env.checkout
    assert env.agent_runtime._agent_update_transaction_state() == "unverified", (
        "admission must start from a clean idle transaction"
    )
    try:
        env.hook.on_acquire = lambda: publish(checkout)
        result = env.updates.restart_stale_runtime()
    finally:
        env.hook.on_acquire = None
    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == update_state
    assert result["restart_scheduled"] is False
    assert result["restart_available"] is True
    assert checkout.workers == [], "a refused restart must not schedule anything"
    assert env.lock.locked() is False, "the lock must be released on refusal"


def test_runtime_became_current_under_the_real_lock_never_schedules(real_restart_env):
    """Control: a runtime that becomes current between admission and the
    under-lock re-check is refused with the typed current response — the
    real guard's own verdict, no fake fields."""
    env = real_restart_env
    checkout = env.checkout

    def become_current():
        env.monkeypatch.setattr(
            env.agent_runtime,
            "_AGENT_REVISION",
            env.agent_runtime._read_agent_revision(
                checkout.root, module_path=checkout.root / "run_agent.py",
            ),
        )

    try:
        env.hook.on_acquire = become_current
        result = env.updates.restart_stale_runtime()
    finally:
        env.hook.on_acquire = None
    assert result["ok"] is False
    assert result["type"] == "agent_runtime_current"
    assert checkout.workers == []
    assert env.lock.locked() is False


@pytest.mark.parametrize(("update_state", "publish"), MARKER_PUBLISHERS)
def test_real_worker_aborts_when_a_marker_appears_across_the_idle_wait(
    real_restart_env, update_state, publish,
):
    """The delayed worker must re-read the REAL marker after its idle wait:
    an update that began after scheduling was accepted aborts the
    replacement before any purge/exec/spawn/exit.  This is the
    marker-to-worker integration the mocked worker env could not reach."""
    env = real_restart_env
    checkout = env.checkout
    result = env.updates.restart_stale_runtime()
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert len(checkout.workers) == 1, "the real delayed worker must be captured"
    # The marker only exists AFTER scheduling was accepted — i.e. across
    # the worker's idle wait, as an external Agent updater would arrive.
    publish(checkout)
    checkout.workers[0]()
    assert env.destructive_total() == 0, (
        "an in-flight update must abort before any destructive call"
    )
    assert env.sleeps, "the worker must observe its idle wait before the guard"
    assert any("aborted" in w for w in env.logs.warnings)


def test_real_worker_replaces_when_the_transaction_stays_allowed(real_restart_env):
    """Positive control on the real boundary: with the transaction still
    idle-stale, the captured worker reaches the (intercepted) replacement —
    proving the abort cases above are the guard's doing, not a broken
    worker."""
    env = real_restart_env
    checkout = env.checkout
    result = env.updates.restart_stale_runtime()
    assert result["ok"] is True
    assert len(checkout.workers) == 1
    checkout.workers[0]()
    assert len(env.destructive.execv) == 1
    assert env.destructive.purge, "the pycache purge must precede the replacement"
    assert env.logs.warnings == []


def test_real_worker_skips_when_the_runtime_became_current(real_restart_env):
    """Control: a runtime that became current again while the restart was
    pending has nothing to recover — the worker skips the replacement."""
    env = real_restart_env
    checkout = env.checkout
    assert env.updates.restart_stale_runtime()["ok"] is True
    assert len(checkout.workers) == 1
    env.monkeypatch.setattr(
        env.agent_runtime,
        "_AGENT_REVISION",
        env.agent_runtime._read_agent_revision(
            checkout.root, module_path=checkout.root / "run_agent.py",
        ),
    )
    checkout.workers[0]()
    assert env.destructive_total() == 0
    assert any("skipped" in i for i in env.logs.infos)


# ── marker location: profile launches read the profile-tree root ──
#
# The installed Agent writes the update marker at the profile-tree ROOT
# (marker v2, hermes_cli/update_lock.py ``update_marker_path``), while a
# WebUI launched on a named profile has ``HERMES_HOME`` re-homed to
# ``<root>/profiles/<p>``.  Reading only the captured home then sees
# ``absent`` while an update is live — and an ``absent`` marker is what
# authorizes the destructive restart.


def _publish_over_age_live_marker(checkout):
    """A live owner older than the v1 ceiling: the update is still running."""
    marker = checkout.home / ".hermes-update-in-progress"
    marker.write_text(
        f"{os.getpid()}\n{time.time() - 86400}\n", encoding="utf-8"
    )


def _publish_live_delegate_marker(checkout):
    """A dead owner with a live delegate: the update outlived its parent."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    marker = checkout.home / ".hermes-update-in-progress"
    marker.write_text(
        f"{process.pid}\n{time.time()}\n"
        f"delegate:{os.getpid()} ct:{time.time():.3f}\n",
        encoding="utf-8",
    )


def _publish_dead_owner_marker(checkout):
    """A dead owner and no delegate: a crashed update's leftover marker."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    marker = checkout.home / ".hermes-update-in-progress"
    marker.write_text(f"{process.pid}\n{time.time()}\n", encoding="utf-8")


def _relaunch_under_profile(env, name="webui"):
    """Re-point the captured ``HERMES_HOME`` at ``<root>/profiles/<name>``."""
    root = env.checkout.home
    profile_home = root / "profiles" / name
    profile_home.mkdir(parents=True, exist_ok=True)
    env.monkeypatch.setattr(env.agent_runtime, "_HERMES_HOME", profile_home)
    return root, profile_home


def test_real_marker_at_profile_tree_root_refuses_profile_launch(
    real_restart_env,
):
    """Under a profile launch the marker lives at the profile-tree root.

    The route must fold that root marker into its verdict: state ``active``,
    restart refused, zero destructive calls.  Reading only the re-homed
    profile home sees ``absent`` and would purge + execv mid-update.
    """
    env = real_restart_env
    root, _profile_home = _relaunch_under_profile(env)
    (root / ".hermes-update-in-progress").write_text(
        f"{os.getpid()}\n{time.time()}\n", encoding="utf-8"
    )

    result = env.updates.restart_stale_runtime()

    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == "active"
    assert result["restart_scheduled"] is False
    assert env.destructive_total() == 0, (
        "a live marker at the profile-tree root must abort the restart"
    )


def test_real_marker_at_root_refuses_root_launch_control(real_restart_env):
    """Control: the same live marker with ``HERMES_HOME=<root>`` (no
    profile) is refused with zero destructive calls on every head."""
    env = real_restart_env
    (env.checkout.home / ".hermes-update-in-progress").write_text(
        f"{os.getpid()}\n{time.time()}\n", encoding="utf-8"
    )

    result = env.updates.restart_stale_runtime()

    assert result.get("ok") is not True
    assert result["agent_update_state"] == "active"
    assert env.destructive_total() == 0


def test_real_marker_at_profile_home_still_refuses_legacy_location(
    real_restart_env,
):
    """Legacy second candidate: a marker written at the profile home (the
    pre-v2 Agent location) must still be detected under a profile launch."""
    env = real_restart_env
    _root, profile_home = _relaunch_under_profile(env)
    (profile_home / ".hermes-update-in-progress").write_text(
        f"{os.getpid()}\n{time.time()}\n", encoding="utf-8"
    )

    result = env.updates.restart_stale_runtime()

    assert result.get("ok") is not True
    assert result["agent_update_state"] == "active"
    assert env.destructive_total() == 0


# ── live long-running updates must not read as a safe ``stale`` ──


def test_real_over_age_live_owner_refuses_restart(real_restart_env):
    """A live owner older than 20 minutes is still an update in flight.

    v2 markers support long-lived owners; the 20-minute ceiling only ages
    out v1 markers.  A process that still answers is never proof the
    update finished, so the restart is refused instead of purging
    mid-update.
    """
    env = real_restart_env
    _publish_over_age_live_marker(env.checkout)

    result = env.updates.restart_stale_runtime()

    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == "stale_live"
    assert result["restart_scheduled"] is False
    assert env.destructive_total() == 0


def test_real_live_delegate_with_dead_owner_refuses_restart(real_restart_env):
    """A surviving delegate keeps the update in flight after its parent
    died; the marker must not read as a safe ``stale`` leftover."""
    env = real_restart_env
    _publish_live_delegate_marker(env.checkout)

    result = env.updates.restart_stale_runtime()

    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == "active"
    assert result["restart_scheduled"] is False
    assert env.destructive_total() == 0


def test_real_dead_owner_without_delegate_still_allows_restart(
    real_restart_env,
):
    """Recovery control: a crashed update's leftover marker (dead owner,
    no live delegate) must not block the operator's restart forever."""
    env = real_restart_env
    _publish_dead_owner_marker(env.checkout)

    result = env.updates.restart_stale_runtime()

    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert len(env.checkout.workers) == 1
    env.checkout.workers[0]()
    assert len(env.destructive.execv) == 1


# ── checkout custody: a dead marker owner is not sufficient ──
#
# The installed Agent (marker v2, hermes_cli/update_lock.py) guards the
# checkout with a second authority besides the marker PID: a KERNEL lock
# on ``hermes-update.lock`` inside the checkout's git dir (or
# ``.hermes-update.lock`` at the checkout root for non-git installs). An
# updater's completion/build/git child inherits the lock FD (pass_fds), so
# the checkout can stay locked after the marker owner died — with no
# ``delegate:`` marker line. The recovery route must refuse while the
# actual loaded checkout is held or unverifiable, even though every marker
# identity is dead, and still recover when the same checkout is provably
# free (the control above).


def _checkout_lock_path(checkout):
    """The installed Agent's checkout kernel lock path for this checkout
    (mirrors hermes_cli/update_lock.py ``checkout_lock_path``: a linked
    worktree locks in its common git dir; a plain checkout locks
    ``.hermes-update.lock`` at its root)."""
    root = checkout.root
    dot = root / ".git"
    if dot.is_dir():
        gitdir = dot
    elif dot.is_file():
        text = dot.read_text(encoding="utf-8-sig").strip()
        if not text.startswith("gitdir:"):
            return root / ".hermes-update.lock"
        gitdir = root / text[len("gitdir:"):].strip()
    else:
        return root / ".hermes-update.lock"
    common = gitdir / "commondir"
    if common.is_file():
        gitdir = gitdir / common.read_text(encoding="utf-8-sig").strip()
    return gitdir / "hermes-update.lock"


def _hold_checkout_lock(checkout, hold_seconds=60):
    """Spawn a surviving updater child that holds the REAL checkout kernel
    lock.  Models the installed Agent's custody lifecycle: the updater's
    child keeps the flock after the marker owner died.  Terminate the
    returned process to release custody."""
    lock_path = _checkout_lock_path(checkout)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    script = (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(lock_path)!r}, os.O_RDONLY)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "sys.stdout.write('locked\\n')\n"
        "sys.stdout.flush()\n"
        f"time.sleep({hold_seconds})\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line.strip() == "locked", (
        "the custody child must report its flock before the test proceeds"
    )
    return child


@pytest.fixture
def held_checkout_lock():
    """Spawn updater-custody children; every one is terminated on teardown."""
    children = []

    def hold(checkout):
        child = _hold_checkout_lock(checkout)
        children.append(child)
        return child

    yield hold
    for child in children:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize(
    "publish_marker",
    [None, _publish_dead_owner_marker],
    ids=["absent", "dead-owner"],
)
def test_real_held_checkout_refuses_admission_despite_idle_marker(
    real_restart_env, held_checkout_lock, publish_marker,
):
    """A dead marker owner is not sufficient: an updater child can still
    hold the CHECKOUT kernel lock (no ``delegate:`` line exists for it), so
    recovery admission must refuse with the ``held`` diagnostic while the
    loaded checkout is locked — for an absent marker and a dead-owner
    leftover alike."""
    env = real_restart_env
    if publish_marker is not None:
        publish_marker(env.checkout)
    held_checkout_lock(env.checkout)

    result = env.updates.restart_stale_runtime()

    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == "held"
    assert result["restart_scheduled"] is False
    assert env.checkout.workers == [], (
        "a held checkout must not schedule any recovery worker"
    )
    assert env.destructive_total() == 0


def test_real_checkout_held_between_admission_and_lock_refuses(
    real_restart_env, held_checkout_lock,
):
    """The under-lock re-check must refuse when custody appears after
    admission: the marker reads idle-stale at admission, then an updater
    child takes the checkout lock before the under-lock re-read."""
    env = real_restart_env
    checkout = env.checkout
    _publish_dead_owner_marker(checkout)

    def hold_under_lock():
        held_checkout_lock(checkout)

    try:
        env.hook.on_acquire = hold_under_lock
        result = env.updates.restart_stale_runtime()
    finally:
        env.hook.on_acquire = None

    assert result.get("ok") is not True
    assert result["type"] == "agent_runtime_stale"
    assert result["agent_update_state"] == "held"
    assert result["restart_scheduled"] is False
    assert env.checkout.workers == [], (
        "a held checkout must not schedule any recovery worker"
    )
    assert env.lock.locked() is False
    assert env.destructive_total() == 0


def test_real_worker_aborts_when_checkout_becomes_held_across_the_delay(
    real_restart_env, held_checkout_lock,
):
    """The delayed pre-replacement guard must re-probe checkout custody: a
    dead marker keeps reading ``stale`` while an updater child locks the
    checkout after scheduling was accepted — the replacement must abort
    before any purge/exec/spawn/exit."""
    env = real_restart_env
    checkout = env.checkout
    _publish_dead_owner_marker(checkout)

    result = env.updates.restart_stale_runtime()
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert len(checkout.workers) == 1

    held_checkout_lock(checkout)
    checkout.workers[0]()

    assert env.destructive_total() == 0, (
        "a held checkout must abort the replacement before any destructive call"
    )
    assert any("aborted" in w for w in env.logs.warnings)


def test_real_unverifiable_checkout_lock_refuses(real_restart_env):
    """A checkout whose lock cannot be probed fails closed: a non-regular
    file (here: a directory) at the Agent's lock path reads ``unknown`` and
    refuses recovery even with a dead marker owner."""
    env = real_restart_env
    _publish_dead_owner_marker(env.checkout)
    lock_path = env.checkout.root / ".git" / "hermes-update.lock"
    lock_path.mkdir(parents=True)

    result = env.updates.restart_stale_runtime()

    assert result.get("ok") is not True
    assert result["agent_update_state"] == "unknown"
    assert result["restart_scheduled"] is False
    assert env.destructive_total() == 0


# ── Windows checkout custody: the probe must speak the native protocol ──
#
# The installed Agent guards the checkout lock on Windows with ``msvcrt``
# BYTE-RANGE locks (``hermes_cli/update_lock.py``): the owner holds byte
# 1<<20, and every descendant that joins the lock — it cannot inherit one
# on Windows — holds one of the 16 lease bytes at 1048577-1048592, so a
# completion child the kill-on-close job refused outlives a killed owner
# and keeps the checkout busy with no marker line naming it.  A probe
# that locks a different range (byte 0, as an earlier revision did) never
# contends with either native range: it reports ``free`` while the Agent
# still holds custody and admits a recovery restart that purges and
# replaces a live update.
#
# The tests below model that protocol on Linux — Windows range locks
# contend across every handle to the same file, handles in one process
# included — and drive the three recovery boundaries (admission, the
# under-lock re-check, the delayed worker) with the real guard, real
# markers and the real captured worker.

_WINDOWS_OWNER_LOCK_OFFSET = 1 << 20
_WINDOWS_LEASE_SLOTS = 16
_WINDOWS_FIRST_LEASE_OFFSET = _WINDOWS_OWNER_LOCK_OFFSET + 1
_WINDOWS_LAST_LEASE_OFFSET = _WINDOWS_OWNER_LOCK_OFFSET + _WINDOWS_LEASE_SLOTS


class _ModeledMsvcrt:
    """Model of the Windows byte-range lock protocol on the checkout lock.

    ``msvcrt.locking`` locks ``count`` bytes at the fd's current
    position, and Windows range locks contend across every handle to the
    same file — handles in the same process included — so an LK_NBLCK
    fails when any OTHER open file description already holds an
    overlapping range.  That is the contention the production probe must
    observe.  A successful lock is held until its LK_UNLCK, which is what
    lets the release regression prove the probe gives back every range it
    acquired.
    """

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self) -> None:
        self._ranges: dict[tuple[int, int], list[list[int]]] = {}
        self.calls: list[tuple[int, int, int, int]] = []

    def locking(self, fd: int, mode: int, count: int) -> None:
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        info = os.fstat(fd)
        held = self._ranges.setdefault((info.st_dev, info.st_ino), [])
        if mode == self.LK_NBLCK:
            contended = any(
                other != fd and start < offset + count and offset < start + size
                for start, size, other in held
            )
            self.calls.append((fd, mode, offset, count))
            if contended:
                raise OSError(errno.EDEADLOCK, "resource deadlock avoided")
            held.append([offset, count, fd])
            return
        if mode == self.LK_UNLCK:
            for entry in held:
                if entry == [offset, count, fd]:
                    held.remove(entry)
                    break
            self.calls.append((fd, mode, offset, count))
            return
        raise OSError(errno.EINVAL, "unsupported locking mode")  # pragma: no cover

    def lock_attempts(self) -> set[tuple[int, int]]:
        """The ``(offset, count)`` ranges an LK_NBLCK was issued for."""
        return {
            (offset, count)
            for _fd, mode, offset, count in self.calls
            if mode == self.LK_NBLCK
        }

    def outstanding(self) -> list[tuple[int, int, int]]:
        """Every acquired-but-unreleased ``(fd, offset, count)`` range."""
        return sorted(
            (fd, start, size)
            for entries in self._ranges.values()
            for start, size, fd in entries
        )


def _posix_pid_is_alive(pid: int) -> bool | None:
    """The module's own POSIX liveness answer, for a modeled-Windows run.

    ``ctypes`` cannot load ``kernel32`` on Linux, so the production
    Windows pid probe would answer ``unknown`` for every marker identity
    and the marker — not the checkout lock — would decide each verdict.
    Keeping the POSIX answer preserves the real marker classification so
    the byte-range custody stays the authority under test.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return None
    return True


def _windows_holder_record(pid: int) -> str:
    """The installed Agent's Windows holder record: pid, start, identity."""
    return f"{pid}\n{int(time.time())}\nwindows-identity\n"


def _dead_owner_record() -> str:
    """A holder record naming a pid that is already gone."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    return _windows_holder_record(process.pid)


class _WindowsCustody:
    """A modeled Windows custody hold on the checkout lock.

    Mirrors the installed Agent: the owner — or a descendant that joined
    it — opens the checkout lock file and takes its byte range with
    ``msvcrt.locking`` (the owner byte at 1<<20, or one lease byte at
    1048577-1048592 for a joiner).  ``holder_record`` is the native
    holder record written into the lock file; ``None`` models a survivor
    that never wrote its name anywhere, leaving the lease byte as the
    only authority that exists.
    """

    def __init__(self, msvcrt, checkout, *, offset, holder_record=None):
        self._msvcrt = msvcrt
        self.offset = offset
        lock_path = _checkout_lock_path(checkout)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
        if holder_record is not None:
            lock_path.write_text(holder_record, encoding="utf-8")
        self.fd: int | None = os.open(lock_path, os.O_RDONLY)
        os.lseek(self.fd, offset, os.SEEK_SET)
        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, self.offset, os.SEEK_SET)
            self._msvcrt.locking(fd, self._msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


@pytest.fixture
def windows_restart_env(real_restart_env):
    """``real_restart_env`` on a modeled Windows checkout lock.

    The production probe branches on ``sys.platform``, so this fixture
    runs that branch against :class:`_ModeledMsvcrt` and keeps the
    module's own POSIX pid-liveness answer (see
    :func:`_posix_pid_is_alive`).  Everything else stays as shipped: the
    real guard, the real markers, the real lock and the real worker.
    """
    from api import agent_runtime

    env = real_restart_env
    msvcrt = _ModeledMsvcrt()
    env.monkeypatch.setattr(sys, "platform", "win32")
    env.monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    env.monkeypatch.setattr(agent_runtime, "_pid_is_alive", _posix_pid_is_alive)
    # The intercepted Windows spawn builds its creationflags from these
    # subprocess constants, which are Windows-only.
    for name, value in (
        ("DETACHED_PROCESS", 1),
        ("CREATE_NEW_PROCESS_GROUP", 2),
        ("CREATE_NO_WINDOW", 4),
    ):
        env.monkeypatch.setattr(subprocess, name, value, raising=False)
    env.msvcrt = msvcrt

    def hold_custody(checkout, *, offset, holder_record=None):
        return _WindowsCustody(
            msvcrt, checkout, offset=offset, holder_record=holder_record,
        )

    env.hold_custody = hold_custody
    return env


def _probe_checkout_state(env) -> str:
    return env.agent_runtime._agent_checkout_lock_state()


@pytest.mark.parametrize("lock_file", ["missing", "present-unlocked"])
def test_windows_probe_reports_free_without_native_custody(
    windows_restart_env, lock_file,
):
    """A checkout nobody holds reads ``free`` — with no lock file at all
    and with an unlocked one — so the probe never over-refuses."""
    env = windows_restart_env
    lock_path = _checkout_lock_path(env.checkout)
    if lock_file == "present-unlocked":
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
    assert _probe_checkout_state(env) == "free"


@pytest.mark.parametrize(
    "offset",
    [
        _WINDOWS_OWNER_LOCK_OFFSET,
        _WINDOWS_FIRST_LEASE_OFFSET,
        _WINDOWS_LAST_LEASE_OFFSET,
    ],
    ids=["owner-byte", "first-lease-slot", "last-lease-slot"],
)
def test_windows_probe_reports_held_for_every_native_range(
    windows_restart_env, offset,
):
    """The owner byte and BOTH ends of the lease range are custody: an
    Agent old enough to predate leases holds only the owner byte, and a
    joined descendant holds any one lease slot — including the last, which
    a probe that stopped one byte early would miss."""
    env = windows_restart_env
    custody = env.hold_custody(env.checkout, offset=offset)
    try:
        assert _probe_checkout_state(env) == "held"
    finally:
        custody.release()


def test_windows_probe_reports_unknown_when_the_lock_cannot_be_probed(
    windows_restart_env,
):
    """A lock path that exists but cannot be probed fails closed."""
    env = windows_restart_env
    _checkout_lock_path(env.checkout).mkdir(parents=True)
    assert _probe_checkout_state(env) == "unknown"


def test_windows_probe_acquires_and_releases_the_native_ranges(
    windows_restart_env,
):
    """A free probe must take the owner byte AND the complete lease range
    — never byte 0, which contends with neither native range — and give
    every acquired range back, so the diagnostic itself never shuts out
    the updater waiting behind it."""
    env = windows_restart_env
    lock_path = _checkout_lock_path(env.checkout)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()

    assert _probe_checkout_state(env) == "free"

    attempts = env.msvcrt.lock_attempts()
    assert (_WINDOWS_OWNER_LOCK_OFFSET, 1) in attempts
    assert (_WINDOWS_FIRST_LEASE_OFFSET, _WINDOWS_LEASE_SLOTS) in attempts
    assert (0, 1) not in attempts, (
        "byte 0 does not contend with either native range"
    )
    assert env.msvcrt.outstanding() == [], (
        "every acquired range must be released on every exit"
    )
    # The checkout is acquirable again behind the diagnostic: a waiting
    # updater can still take the owner byte and a lease byte of its own.
    owner = env.hold_custody(env.checkout, offset=_WINDOWS_OWNER_LOCK_OFFSET)
    lease = env.hold_custody(env.checkout, offset=_WINDOWS_LAST_LEASE_OFFSET)
    owner.release()
    lease.release()


def test_windows_probe_never_writes_or_reclaims_the_lock_file(
    windows_restart_env,
):
    """The probe is a diagnostic, not a custody claim: it must not write,
    truncate, unlink or recreate the lock file — not while contending
    with live custody and not on the free pass either."""
    env = windows_restart_env
    lock_path = _checkout_lock_path(env.checkout)
    custody = env.hold_custody(
        env.checkout,
        offset=_WINDOWS_OWNER_LOCK_OFFSET,
        holder_record=_dead_owner_record(),
    )
    before = (lock_path.read_bytes(), os.stat(lock_path).st_ino)
    try:
        assert _probe_checkout_state(env) == "held"
    finally:
        custody.release()
    assert _probe_checkout_state(env) == "free"

    assert (lock_path.read_bytes(), os.stat(lock_path).st_ino) == before
    assert lock_path.is_file(), "the probe must not unlink or reclaim the lock"


def test_windows_probe_answers_bounded_while_custody_is_held(
    windows_restart_env,
):
    """A non-blocking range probe: contention answers ``held`` at once and
    never parks the recovery request on the Agent's lock."""
    env = windows_restart_env
    custody = env.hold_custody(
        env.checkout, offset=_WINDOWS_FIRST_LEASE_OFFSET,
        holder_record=_dead_owner_record(),
    )
    try:
        started = time.monotonic()
        assert _probe_checkout_state(env) == "held"
        assert time.monotonic() - started < 1.0
    finally:
        custody.release()


# Each native custody shape, driven through every recovery boundary.  The
# marker column is what a crashed update leaves behind: a dead owner with
# no delegate, or nothing at all when the survivor never wrote a name.
_WINDOWS_CUSTODY_CASES = [
    # An owner (an Agent old enough to predate leases holds ONLY the owner
    # byte) whose marker owner is dead: the byte range is the only proof.
    ("owner-byte", _WINDOWS_OWNER_LOCK_OFFSET, _publish_dead_owner_marker, True),
    # A dead owner's descendant joined the lock and holds a lease byte —
    # the first slot ...
    ("first-lease-slot", _WINDOWS_FIRST_LEASE_OFFSET, _publish_dead_owner_marker, True),
    # ... and the last slot, one byte short of where a partial probe stops.
    ("last-lease-slot", _WINDOWS_LAST_LEASE_OFFSET, _publish_dead_owner_marker, True),
    # An unnamed survivor: no marker anywhere and an empty lock file, so
    # the lease byte is the sole authority that exists.
    ("unnamed-descendant", _WINDOWS_FIRST_LEASE_OFFSET, None, False),
]


@pytest.mark.parametrize("boundary", ["admission", "under-lock", "delayed-worker"])
@pytest.mark.parametrize(
    ("custody_id", "offset", "publish_marker", "named"),
    _WINDOWS_CUSTODY_CASES,
    ids=[case[0] for case in _WINDOWS_CUSTODY_CASES],
)
def test_windows_custody_refuses_every_recovery_boundary(
    windows_restart_env, custody_id, offset, publish_marker, named, boundary,
):
    """Every native custody shape must refuse the recovery restart at all
    three boundaries — admission, the under-lock re-check and the delayed
    worker's last-moment barrier — with the ``held`` diagnostic and before
    any purge/exec/spawn/exit.  This is the regression the byte-0 probe
    failed: it reported ``free`` for all four shapes and let the
    replacement through."""
    env = windows_restart_env
    checkout = env.checkout
    if publish_marker is not None:
        publish_marker(checkout)
    record = _dead_owner_record() if named else None

    if boundary == "admission":
        custody = env.hold_custody(
            checkout, offset=offset, holder_record=record,
        )
        try:
            result = env.updates.restart_stale_runtime()
        finally:
            custody.release()
        assert result.get("ok") is not True
        assert result["type"] == "agent_runtime_stale"
        assert result["agent_update_state"] == "held"
        assert result["restart_scheduled"] is False
        assert checkout.workers == [], (
            "Windows custody must not schedule any recovery worker"
        )
        assert env.destructive_total() == 0
        return

    if boundary == "under-lock":
        taken = []

        def take_custody():
            taken.append(
                env.hold_custody(checkout, offset=offset, holder_record=record)
            )

        try:
            env.hook.on_acquire = take_custody
            result = env.updates.restart_stale_runtime()
        finally:
            env.hook.on_acquire = None
            for custody in taken:
                custody.release()
        assert result.get("ok") is not True
        assert result["type"] == "agent_runtime_stale"
        assert result["agent_update_state"] == "held"
        assert result["restart_scheduled"] is False
        assert checkout.workers == []
        assert env.lock.locked() is False
        assert env.destructive_total() == 0
        return

    result = env.updates.restart_stale_runtime()
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert len(checkout.workers) == 1
    custody = env.hold_custody(checkout, offset=offset, holder_record=record)
    try:
        checkout.workers[0]()
    finally:
        custody.release()
    assert env.destructive_total() == 0, (
        "Windows custody must abort the replacement before any destructive call"
    )
    assert any("aborted" in w for w in env.logs.warnings)


def test_windows_free_checkout_recovers_at_admission(windows_restart_env):
    """Control: with no native range held the probe must not over-refuse —
    a crashed update's leftover marker stays recoverable, which is the
    whole point of the custody guards."""
    env = windows_restart_env
    _publish_dead_owner_marker(env.checkout)
    result = env.updates.restart_stale_runtime()
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert len(env.checkout.workers) == 1
    assert env.destructive_total() == 0


def test_windows_free_checkout_recovers_under_the_lock(windows_restart_env):
    """Control: the under-lock re-check re-probes the same free checkout
    and still admits the recovery restart."""
    env = windows_restart_env
    _publish_dead_owner_marker(env.checkout)
    try:
        env.hook.on_acquire = lambda: None
        result = env.updates.restart_stale_runtime()
    finally:
        env.hook.on_acquire = None
    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert len(env.checkout.workers) == 1
    assert env.destructive_total() == 0


@pytest.fixture(autouse=True)
def _reopen_run_admission_after_a_scheduled_restart():
    """Reopen run admission after a restart this module scheduled.

    ``_schedule_restart`` closes run admission **synchronously** and reopens
    it from its daemon thread once the restart delay elapses. Two things
    make that unreliable here:

    * the thread is a daemon and may not have run yet when the test returns;
    * ``abandon_restart_pending`` only clears the state for the **newest**
      epoch, so when several restarts are scheduled the earlier threads'
      abandon calls are refused and the last one may still be pending.

    Either way admission stays closed, and every later test in the shard is
    then refused with ``server_draining`` — a cross-test leak, not a product
    behaviour. The workflow requires the suite to be shard-safe ("no
    cross-test state leakage"), so the state is restored deterministically
    here instead of relying on the scheduler's timing.
    """
    yield
    from api import run_drain

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if run_drain.admission_status().get("open"):
                return
            # Reopen the state the scheduler is holding: abandon the epoch
            # that is current NOW, so the call cannot be refused as stale.
            epoch = getattr(run_drain, "_RESTART_PENDING_EPOCH", None)
            if epoch is not None:
                run_drain.abandon_restart_pending(epoch)
        except Exception:  # pragma: no cover - the probe must never fail a test
            return
        # ``time.sleep`` may be stubbed by the restart env, so wait on a
        # primitive this module does not replace.
        threading.Event().wait(0.01)


def test_windows_free_checkout_worker_completes_the_replacement(
    windows_restart_env,
):
    """Control: across the worker's idle wait the checkout stays free, so
    the delayed barrier lets the replacement run to the (intercepted)
    Windows spawn and exit — proving the abort cases above are the
    custody guard's doing, not a broken worker."""
    env = windows_restart_env
    _publish_dead_owner_marker(env.checkout)
    result = env.updates.restart_stale_runtime()
    assert result["ok"] is True
    assert len(env.checkout.workers) == 1
    env.checkout.workers[0]()
    assert len(env.destructive.spawn) == 1
    assert env.destructive.exits == [0]
    assert env.destructive.purge, "the pycache purge must precede the replacement"
    assert env.logs.warnings == []


def test_a_scheduled_restart_leaves_run_admission_open(
    windows_restart_env,
):
    """Sentinel: a restart scheduled by the test above must not leave run
    admission closed for the next test in the shard.

    Without the waiting fixture this fails with the next chat start
    refused as ``server_draining`` — the exact cross-test leak that made
    this PR's CI shards red.
    """
    from api import run_drain

    assert run_drain.admission_status().get("open") is True, (
        "a scheduled restart left run admission closed: the next test in "
        "this shard would be refused with server_draining"
    )
