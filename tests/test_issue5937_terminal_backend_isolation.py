"""Regression tests for #5937 multi-profile terminal backend isolation.

WebUI must invalidate the agent process-global "default" terminal env when the
selected profile's backend identity changes (e.g. SSH → local), without
evicting on same-backend consecutive turns — and must FAIL CLOSED: no turn is
ever admitted against a slot that may belong to a different backend.

The fakes here mirror the REAL hermes-agent cleanup contract
(tools/terminal_tool.cleanup_vm), which the #5988 round-2 gate found the
previous mocks were hiding:

* ``cleanup_vm`` pops the cache slot FIRST, then swallows backend-teardown
  exceptions and returns ``None`` either way — it NEVER signals failure.
* ``DockerEnvironment.cleanup(force_remove=True)`` performs ``docker stop`` /
  ``docker rm -f`` asynchronously; returning proves nothing about the
  container actually being removed.

Success must therefore be established by observed postconditions (slot gone,
container gone), and these tests assert that an incompatible turn is NEVER
admitted on timeout or on unverified cleanup.
"""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path

import pytest

import api.terminal_backend_isolation as isolation


REPO_ROOT = Path(__file__).parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

SSH = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box.example", "TERMINAL_SSH_USER": "deploy"}
LOCAL = {"TERMINAL_ENV": "local"}
DOCKER_A = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:a"}
DOCKER_B = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:b"}


def setup_function(_fn=None):
    isolation.reset_terminal_backend_identity_for_tests()


# ── Fakes faithful to the real agent contract ────────────────────────────────


class FakeDockerEnv:
    """Mirrors DockerEnvironment: container removal happens in cleanup(); a
    silent removal failure leaves the container in ``agent.containers``."""

    def __init__(self, agent, container_id):
        self._agent = agent
        self._container_id = container_id
        self._docker_exe = "docker"
        self._cleanup_thread = None

    def cleanup(self, *, force_remove=False):
        if self._agent.backend_cleanup_error is not None:
            raise self._agent.backend_cleanup_error
        if force_remove and not self._agent.rm_silently_fails:
            self._agent.containers.discard(self._container_id)
        self._container_id = None


class FakeGenericEnv:
    def cleanup(self):
        pass


class FakeAgent:
    """Faithful stand-in for hermes-agent tools/terminal_tool."""

    def __init__(self):
        self.active = {}
        self.containers = set()
        self.cleanup_calls = []
        self.backend_cleanup_error = None  # raised INSIDE env.cleanup (swallowed)
        self.rm_silently_fails = False  # agent's async docker rm loses the race
        self.direct_rm_results = None  # None = always works; else pop-per-call
        self.pop_slot = True  # contract-drift knob

    def seed_docker(self, container_id="cid-aaaa"):
        self.containers.add(container_id)
        self.active["default"] = FakeDockerEnv(self, container_id)
        return container_id

    def seed_generic(self):
        self.active["default"] = FakeGenericEnv()

    def get_active_env(self, task_id):
        return self.active.get(task_id)

    def cleanup_vm(self, task_id, *, force_remove=False):
        """REAL contract: pop first, swallow teardown exceptions, return None."""
        self.cleanup_calls.append((task_id, force_remove))
        if self.pop_slot:
            env = self.active.pop(task_id, None)
        else:
            env = self.active.get(task_id)
        if env is None:
            return None
        try:
            sig = inspect.signature(env.cleanup)
            if "force_remove" in sig.parameters:
                env.cleanup(force_remove=force_remove)
            else:
                env.cleanup()
        except Exception:
            pass  # the real cleanup_vm logs and swallows
        return None

    def direct_rm(self, container_id, docker_exe):
        if self.direct_rm_results is None:
            self.containers.discard(container_id)
            return
        if self.direct_rm_results and self.direct_rm_results.pop(0):
            self.containers.discard(container_id)


def patch_probes(monkeypatch, agent):
    monkeypatch.setattr(
        isolation, "_container_exists", lambda cid, exe: cid in agent.containers
    )
    monkeypatch.setattr(isolation, "_force_remove_container", agent.direct_rm)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_POLL_SECONDS", 0.01)


def turn(agent, env, **kw):
    return isolation.acquire_terminal_backend_turn_lease(
        env, cleanup_vm=agent.cleanup_vm, get_active_env=agent.get_active_env, **kw
    )


def one_shot(agent, env, **kw):
    return isolation.maybe_invalidate_default_terminal_env(
        env, cleanup_vm=agent.cleanup_vm, get_active_env=agent.get_active_env, **kw
    )


def active_turn_count():
    with isolation._COND:
        return sum(isolation._active_turn_counts.values())


# ── Identity (pure) ──────────────────────────────────────────────────────────


def test_identity_defaults_missing_backend_to_local():
    assert isolation.terminal_backend_identity({})[0] == "local"
    assert isolation.terminal_backend_identity({"TERMINAL_ENV": "  SSH "})[0] == "ssh"


def test_identity_includes_ssh_target_not_cwd():
    a = isolation.terminal_backend_identity({**SSH, "TERMINAL_CWD": "/a"})
    b = isolation.terminal_backend_identity({**SSH, "TERMINAL_CWD": "/b"})
    assert a == b
    different_host = isolation.terminal_backend_identity(
        {**SSH, "TERMINAL_SSH_HOST": "other.example"}
    )
    assert a != different_host


def test_identity_includes_ssh_key_and_modal_mode():
    key_a = isolation.terminal_backend_identity({**SSH, "TERMINAL_SSH_KEY": "/k/a"})
    key_b = isolation.terminal_backend_identity({**SSH, "TERMINAL_SSH_KEY": "/k/b"})
    assert key_a != key_b
    modal_direct = isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal", "TERMINAL_MODAL_MODE": "direct"}
    )
    modal_managed = isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal", "TERMINAL_MODAL_MODE": "managed"}
    )
    assert modal_direct != modal_managed


def test_identity_normalizes_agent_defaults():
    assert isolation.terminal_backend_identity(
        SSH
    ) == isolation.terminal_backend_identity({**SSH, "TERMINAL_SSH_PORT": "22"})
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "docker"}
    ) == isolation.terminal_backend_identity(
        {
            "TERMINAL_ENV": "docker",
            "TERMINAL_DOCKER_IMAGE": "nikolaik/python-nodejs:python3.11-nodejs20",
        }
    )
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal"}
    ) == isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal", "TERMINAL_MODAL_MODE": "AUTO"}
    )


def test_identity_excludes_inactive_backend_settings():
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "local", "TERMINAL_SSH_HOST": "leftover.example"}
    ) == isolation.terminal_backend_identity({"TERMINAL_ENV": "local"})
    assert isolation.terminal_backend_identity(
        {**SSH, "TERMINAL_DOCKER_IMAGE": "img:x"}
    ) == isolation.terminal_backend_identity(SSH)


# ── Nominal admission / invalidation ─────────────────────────────────────────


def test_first_turn_records_identity_without_cleanup():
    agent = FakeAgent()
    assert one_shot(agent, SSH) is False
    assert agent.cleanup_calls == []


def test_same_backend_consecutive_turns_do_not_cleanup():
    agent = FakeAgent()
    assert one_shot(agent, LOCAL) is False
    assert one_shot(agent, LOCAL) is False
    assert agent.cleanup_calls == []


def test_ssh_then_local_invalidates_default_slot():
    agent = FakeAgent()
    agent.seed_generic()
    assert one_shot(agent, SSH) is False
    assert one_shot(agent, LOCAL) is True
    assert agent.cleanup_calls == [("default", False)]
    assert agent.get_active_env("default") is None
    # Committed: a follow-up same-identity turn does not re-invalidate.
    assert one_shot(agent, LOCAL) is False
    assert len(agent.cleanup_calls) == 1


def test_local_then_ssh_invalidates_default_slot():
    agent = FakeAgent()
    agent.seed_generic()
    assert one_shot(agent, LOCAL) is False
    assert one_shot(agent, SSH) is True
    assert agent.cleanup_calls == [("default", False)]


def test_ssh_host_change_invalidates():
    agent = FakeAgent()
    agent.seed_generic()
    one_shot(agent, SSH)
    assert one_shot(agent, {**SSH, "TERMINAL_SSH_HOST": "b.example"}) is True
    assert agent.cleanup_calls == [("default", False)]


# ── Docker force-remove + verified removal ───────────────────────────────────


def test_docker_transition_force_removes_and_verifies_container(monkeypatch):
    """Gate #5988 CORE: leaving a Docker identity must force-remove AND the
    commit must be backed by an observed container removal, not by
    cleanup_vm returning."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    cid = agent.seed_docker()
    assert one_shot(agent, DOCKER_A) is False
    assert one_shot(agent, LOCAL) is True
    assert agent.cleanup_calls == [("default", True)]
    assert cid not in agent.containers
    with isolation._COND:
        assert isolation._pending_container_removals == {}


def test_docker_image_change_invalidates_with_force_remove(monkeypatch):
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    agent.seed_docker()
    one_shot(agent, DOCKER_A)
    assert one_shot(agent, DOCKER_B) is True
    assert agent.cleanup_calls == [("default", True)]


def test_non_docker_transition_does_not_force_remove():
    agent = FakeAgent()
    agent.seed_generic()
    one_shot(agent, SSH)
    assert one_shot(agent, {"TERMINAL_ENV": "docker"}) is True
    assert agent.cleanup_calls == [("default", False)]


# ── Fail closed: cleanup failure under the REAL contract ─────────────────────


def test_silent_docker_rm_failure_is_caught_and_turn_rejected(monkeypatch):
    """Gate #5988 CORE: the real cleanup_vm returns None even when the docker
    removal silently fails. The verification layer must catch it: transition
    uncommitted, turn NOT admitted, container ledgered for retry."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    cid = agent.seed_docker()
    agent.rm_silently_fails = True
    agent.direct_rm_results = [False]  # direct fallback also fails this round
    docker_identity = isolation.terminal_backend_identity(DOCKER_A)

    assert one_shot(agent, DOCKER_A) is False
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        one_shot(agent, LOCAL)

    # cleanup_vm WAS called and returned None — but nothing was inferred from it.
    assert agent.cleanup_calls == [("default", True)]
    assert cid in agent.containers  # the leak the old mocks hid
    with isolation._COND:
        assert isolation._last_backend_identity == docker_identity  # uncommitted
        assert isolation._pending_container_removals == {cid: "docker"}
    assert active_turn_count() == 0  # the rejected turn was NOT admitted

    # Next turn retries; the direct fallback now works. The slot was already
    # popped, so only the pending-container ledger can close the leak.
    agent.direct_rm_results = None
    assert one_shot(agent, LOCAL) is True
    assert cid not in agent.containers
    with isolation._COND:
        assert isolation._pending_container_removals == {}
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(LOCAL)


def test_backend_cleanup_exception_swallowed_by_agent_is_still_a_failure(monkeypatch):
    """env.cleanup raising is swallowed by the real cleanup_vm (returns None);
    the container survives and the transition must not commit."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    cid = agent.seed_docker()
    agent.backend_cleanup_error = RuntimeError("docker daemon unreachable")
    one_shot(agent, DOCKER_A)
    agent.direct_rm_results = [False]
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        one_shot(agent, LOCAL)
    assert cid in agent.containers
    assert active_turn_count() == 0
    # Daemon recovers: retry commits via the ledger + direct removal.
    agent.direct_rm_results = None
    assert one_shot(agent, LOCAL) is True
    assert cid not in agent.containers


def test_async_docker_removal_is_awaited_not_assumed(monkeypatch):
    """The agent removes the container on a background thread AFTER cleanup_vm
    returns; the probe loop must wait for it rather than failing instantly."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_WAIT_SECONDS", 5.0)
    cid = agent.seed_docker()
    env = agent.active["default"]

    def _async_cleanup(*, force_remove=False):
        t = threading.Thread(
            target=lambda: (time.sleep(0.15), agent.containers.discard(cid)),
            daemon=True,
        )
        t.start()
        env._cleanup_thread = t

    env.cleanup = _async_cleanup
    one_shot(agent, DOCKER_A)
    assert one_shot(agent, LOCAL) is True
    assert cid not in agent.containers


def test_unremoved_default_slot_fails_the_transition():
    """Contract-drift guard: if cleanup_vm stops popping the slot, committing
    would hand the new identity a live stale env — must fail closed."""
    agent = FakeAgent()
    agent.seed_generic()
    agent.pop_slot = False
    one_shot(agent, SSH)
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        one_shot(agent, LOCAL)
    assert active_turn_count() == 0


def test_import_failure_rejects_and_is_retried_by_next_turn(monkeypatch):
    """cleanup_vm unresolvable → the turn must NOT proceed uninvalidated."""
    agent = FakeAgent()
    agent.seed_generic()
    assert one_shot(agent, SSH) is False
    monkeypatch.setattr(isolation, "_resolve_cleanup_vm", lambda: None)
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        isolation.maybe_invalidate_default_terminal_env(
            LOCAL, get_active_env=agent.get_active_env
        )
    monkeypatch.undo()
    assert one_shot(agent, LOCAL) is True
    assert agent.cleanup_calls == [("default", False)]


# ── Fail closed: admission control ───────────────────────────────────────────


def test_differing_backend_turn_waits_for_active_lease():
    """A differing-backend turn must not evict an in-use env."""
    agent = FakeAgent()
    agent.seed_generic()
    first, invalidated = turn(agent, SSH)
    assert invalidated is False

    entered = threading.Event()
    result = {}

    def _differing_turn():
        entered.set()
        lease, did = turn(agent, LOCAL, wait_seconds=10)
        result["invalidated"] = did
        lease.release()

    thread = threading.Thread(target=_differing_turn, daemon=True)
    thread.start()
    entered.wait(timeout=5)
    thread.join(timeout=0.3)
    assert thread.is_alive(), "differing-backend turn should wait on the lease"
    assert agent.cleanup_calls == []

    first.release()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["invalidated"] is True
    assert agent.cleanup_calls == [("default", False)]


def test_lease_wait_timeout_rejects_turn_without_admitting_it():
    """Gate #5988 CORE: on timeout the incoming turn must be REJECTED — never
    admitted alongside the still-active previous identity."""
    agent = FakeAgent()
    agent.seed_generic()
    ssh_identity = isolation.terminal_backend_identity(SSH)
    first, _ = turn(agent, SSH)
    with pytest.raises(isolation.TerminalBackendTransitionTimeout):
        turn(agent, LOCAL, wait_seconds=0.05)
    assert agent.cleanup_calls == []
    with isolation._COND:
        # Only the SSH turn is registered; the rejected turn left no lease.
        assert dict(isolation._active_turn_counts) == {ssh_identity: 1}
        assert isolation._last_backend_identity == ssh_identity
    first.release()
    # Transition was not committed — a later unobstructed turn succeeds.
    lease2, invalidated2 = turn(agent, LOCAL)
    assert invalidated2 is True
    lease2.release()


def test_no_turn_is_admitted_while_transition_cleanup_is_in_flight():
    """Same-new-identity arrivals must WAIT on the transition — no piggyback
    on an unproven cleanup."""
    agent = FakeAgent()
    agent.seed_generic()
    seed, _ = turn(agent, SSH)
    seed.release()

    in_cleanup = threading.Event()
    release_cleanup = threading.Event()

    def held_cleanup(task_id, **kw):
        in_cleanup.set()
        release_cleanup.wait(timeout=10)
        return agent.cleanup_vm(task_id, **kw)

    leader_result = {}

    def _leader():
        lease, did = isolation.acquire_terminal_backend_turn_lease(
            LOCAL, cleanup_vm=held_cleanup, get_active_env=agent.get_active_env
        )
        leader_result["invalidated"] = did
        lease.release()

    leader = threading.Thread(target=_leader, daemon=True)
    leader.start()
    assert in_cleanup.wait(timeout=5), "leader never reached cleanup_vm"

    piggy_result = {}

    def _piggy():
        lease, did = turn(agent, LOCAL, wait_seconds=10)
        piggy_result["invalidated"] = did
        lease.release()

    piggy = threading.Thread(target=_piggy, daemon=True)
    piggy.start()
    piggy.join(timeout=0.3)
    assert piggy.is_alive(), "same-new-identity turn must wait for the transition"
    # The leader is still blocked inside held_cleanup (which records the call
    # only when it delegates) — the piggybacker must not have invalidated.
    assert agent.cleanup_calls == []

    release_cleanup.set()
    leader.join(timeout=10)
    piggy.join(timeout=10)
    assert leader_result["invalidated"] is True
    assert piggy_result["invalidated"] is False  # committed by the leader
    assert len(agent.cleanup_calls) == 1  # exactly one invalidation total


def test_waiter_retries_invalidation_itself_when_leader_cleanup_fails(monkeypatch):
    """Gate #5988 CORE: a waiter must not assume the leader invalidated. When
    the leader's cleanup fails verification, the waiter performs its OWN
    verified invalidation instead of running against the stale slot."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    cid = agent.seed_docker()
    agent.rm_silently_fails = True
    agent.direct_rm_results = [False, True]  # leader's fallback fails; waiter's works
    seed, _ = turn(agent, DOCKER_A)
    seed.release()

    in_cleanup = threading.Event()
    release_cleanup = threading.Event()

    def held_cleanup(task_id, **kw):
        in_cleanup.set()
        release_cleanup.wait(timeout=10)
        return agent.cleanup_vm(task_id, **kw)

    leader_result = {}

    def _leader():
        try:
            lease, did = isolation.acquire_terminal_backend_turn_lease(
                LOCAL, cleanup_vm=held_cleanup, get_active_env=agent.get_active_env
            )
            lease.release()
            leader_result["outcome"] = ("admitted", did)
        except isolation.TerminalBackendInvalidationFailed:
            leader_result["outcome"] = ("rejected", None)

    leader = threading.Thread(target=_leader, daemon=True)
    leader.start()
    assert in_cleanup.wait(timeout=5)

    waiter_result = {}

    def _waiter():
        lease, did = turn(agent, LOCAL, wait_seconds=10)
        waiter_result["invalidated"] = did
        lease.release()

    waiter = threading.Thread(target=_waiter, daemon=True)
    waiter.start()
    waiter.join(timeout=0.3)
    assert waiter.is_alive(), "waiter admitted during an in-flight transition"

    release_cleanup.set()
    leader.join(timeout=10)
    waiter.join(timeout=10)
    assert leader_result["outcome"] == ("rejected", None)
    # The waiter did NOT trust the failed leader: it ran its own verified
    # invalidation (ledgered container removed via the direct fallback).
    assert waiter_result["invalidated"] is True
    assert cid not in agent.containers
    with isolation._COND:
        assert isolation._pending_container_removals == {}
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(LOCAL)


def test_previous_identity_arrival_also_waits_during_transition():
    """During a transition even previous-identity turns wait: the slot is
    mid-destruction and admitting them would recreate under a moving target."""
    agent = FakeAgent()
    agent.seed_generic()
    seed, _ = turn(agent, SSH)
    seed.release()

    in_cleanup = threading.Event()
    release_cleanup = threading.Event()

    def held_cleanup(task_id, **kw):
        in_cleanup.set()
        release_cleanup.wait(timeout=10)
        return agent.cleanup_vm(task_id, **kw)

    leader = threading.Thread(
        target=lambda: isolation.acquire_terminal_backend_turn_lease(
            LOCAL, cleanup_vm=held_cleanup, get_active_env=agent.get_active_env
        )[0].release(),
        daemon=True,
    )
    leader.start()
    assert in_cleanup.wait(timeout=5)

    old_arrival = threading.Thread(
        target=lambda: turn(agent, SSH, wait_seconds=10)[0].release(), daemon=True
    )
    old_arrival.start()
    old_arrival.join(timeout=0.3)
    assert old_arrival.is_alive(), "previous-identity turn admitted mid-transition"

    release_cleanup.set()
    leader.join(timeout=10)
    old_arrival.join(timeout=10)
    assert not old_arrival.is_alive()


# ── Streaming integration source guard ───────────────────────────────────────


def test_streaming_acquires_full_turn_lease_on_effective_env():
    """Source guard: the turn path leases the identity for the whole turn.

    Identity must come from the effective post-merge environment (process env
    overlaid with the profile runtime env), the lease must be acquired BEFORE
    _ENV_LOCK (waiters must not block other turns' env restore), it must be
    released in the turn's finally after the env restore, and deliberate
    isolation rejections must ABORT the turn (fail closed) rather than being
    swallowed.
    """
    assert "acquire_terminal_backend_turn_lease" in STREAMING_PY
    # Effective env = os.environ overlaid with the profile runtime env.
    merged_idx = STREAMING_PY.find("_effective_turn_env = dict(os.environ)")
    assert merged_idx >= 0
    overlay_idx = STREAMING_PY.find(
        "_effective_turn_env.update(_safe_profile_runtime_env)", merged_idx
    )
    assert overlay_idx > merged_idx
    acquire_idx = STREAMING_PY.find(
        "acquire_terminal_backend_turn_lease(", overlay_idx
    )
    assert acquire_idx > overlay_idx
    # Lease acquisition happens before the env-mutation critical section.
    update_idx = STREAMING_PY.find("os.environ.update(_safe_profile_runtime_env)")
    assert update_idx >= 0
    assert acquire_idx < update_idx
    # Deliberate rejections fail closed: the typed error is imported and
    # re-raised (turn aborts); only unexpected module bugs fall through open.
    assert "TerminalBackendIsolationError" in STREAMING_PY
    reject_idx = STREAMING_PY.find("except TerminalBackendIsolationError:")
    assert reject_idx > 0
    raise_idx = STREAMING_PY.find("raise", reject_idx)
    generic_idx = STREAMING_PY.find("except Exception:", reject_idx)
    assert 0 < raise_idx < generic_idx
    # Released in the finally, after the profile env restore loop.
    restore_idx = STREAMING_PY.find("for _key, _old_value in old_profile_env.items()")
    release_idx = STREAMING_PY.find("_terminal_backend_lease.release()")
    assert restore_idx >= 0
    assert release_idx > restore_idx
    # Point-in-time invalidation is no longer called from the turn path.
    assert "maybe_invalidate_default_terminal_env(_safe_profile_runtime_env)" not in STREAMING_PY
