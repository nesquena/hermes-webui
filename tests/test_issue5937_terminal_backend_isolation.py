"""Regression tests for #5937 multi-profile terminal backend isolation.

WebUI must invalidate the agent process-global "default" terminal env when the
selected profile's backend identity changes (e.g. SSH → local), without
evicting on same-backend consecutive turns — and must FAIL CLOSED: no turn is
ever admitted against a slot that may belong to a different backend.

Two layers of coverage:

* Fakes faithful to the REAL hermes-agent cleanup contract
  (tools/terminal_tool.cleanup_vm), which the #5988 round-2 gate found the
  previous mocks were hiding:

  - ``cleanup_vm`` pops the cache slot FIRST, then swallows backend-teardown
    exceptions and returns ``None`` either way — it NEVER signals failure.
  - ``DockerEnvironment.cleanup(force_remove=True)`` performs ``docker stop``
    / ``docker rm -f`` asynchronously; returning proves nothing about the
    container actually being removed.

* The INSTALLED ``tools.terminal_tool`` module itself (#5988 round 4): the
  real ``cleanup_vm`` seeded through the real module cache, with the module's
  own helper resolution, driven against scripted ``docker`` executables so
  the actual subprocess probe path is exercised — including the reproduced
  false-commit where a Docker daemon failure used to read as "container
  removed". These tests skip when hermes-agent is not installed (upstream
  webui CI); the fake-based suite keeps the logic covered there, and the
  agent-installed lane (run before every push) exercises the real contract.

Success must be established by observed postconditions (slot gone, container
gone), "cannot verify" must never count as "verified", and these tests assert
that an incompatible turn is NEVER admitted on timeout, on unverified
cleanup, on daemon-failure probes, or on unexpected isolation-module errors.
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
from pathlib import Path

import pytest

import api.terminal_backend_isolation as isolation


REPO_ROOT = Path(__file__).parent.parent
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")

SSH = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "box.example", "TERMINAL_SSH_USER": "deploy"}
LOCAL = {"TERMINAL_ENV": "local"}
DOCKER_A = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:a"}
DOCKER_B = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img:b"}


def setup_function(_fn=None):
    isolation.reset_terminal_backend_identity_for_tests()


def teardown_function(_fn=None):
    # Shard hygiene: these tests mutate process-global identity state that
    # other streaming tests in the same shard would otherwise inherit.
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


def test_identity_fingerprints_all_docker_creation_inputs():
    """Gate #5988 round 4: every setting the agent's get_config() feeds into
    Docker environment creation must split the identity — volumes, env maps,
    network, extra args, resources, user mode, forwarded env, persistence."""
    base = isolation.terminal_backend_identity(DOCKER_A)
    for key, val in [
        ("TERMINAL_DOCKER_VOLUMES", '["/data:/data"]'),
        ("TERMINAL_DOCKER_ENV", '{"X": "1"}'),
        ("TERMINAL_DOCKER_EXTRA_ARGS", '["--privileged"]'),
        ("TERMINAL_DOCKER_FORWARD_ENV", '["PATH"]'),
        ("TERMINAL_DOCKER_NETWORK", "false"),
        ("TERMINAL_DOCKER_RUN_AS_HOST_USER", "true"),
        ("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true"),
        ("TERMINAL_CONTAINER_CPU", "4"),
        ("TERMINAL_CONTAINER_MEMORY", "8192"),
        ("TERMINAL_CONTAINER_DISK", "10240"),
        ("TERMINAL_CONTAINER_PERSISTENT", "false"),
        ("TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES", "false"),
    ]:
        changed = isolation.terminal_backend_identity({**DOCKER_A, key: val})
        assert changed != base, f"{key} must participate in the docker identity"


def test_identity_fingerprints_ssh_local_and_modal_creation_inputs():
    assert isolation.terminal_backend_identity(
        {**SSH, "TERMINAL_SSH_PERSISTENT": "false"}
    ) != isolation.terminal_backend_identity(SSH)
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "local", "TERMINAL_LOCAL_PERSISTENT": "true"}
    ) != isolation.terminal_backend_identity(LOCAL)
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "modal", "TERMINAL_CONTAINER_MEMORY": "8192"}
    ) != isolation.terminal_backend_identity({"TERMINAL_ENV": "modal"})


def test_identity_normalizes_json_and_flag_formatting():
    """Formatting-only differences in JSON/boolean settings must not split
    identities (and thereby force spurious verified transitions)."""
    a = isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_ENV": '{"A": "1", "B": "2"}'}
    )
    b = isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_ENV": '{"B":"2","A":"1"}'}
    )
    assert a == b
    assert isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_NETWORK": "yes"}
    ) == isolation.terminal_backend_identity(DOCKER_A)  # default is true
    assert isolation.terminal_backend_identity(
        {**SSH, "TERMINAL_SSH_PERSISTENT": "1"}
    ) == isolation.terminal_backend_identity(SSH)  # default is true
    assert isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "local", "TERMINAL_LOCAL_PERSISTENT": "false"}
    ) == isolation.terminal_backend_identity(LOCAL)
    # Unparseable JSON stays raw: two different broken values differ.
    broken_a = isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_VOLUMES": "[not json"}
    )
    broken_b = isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_VOLUMES": "[also not json"}
    )
    assert broken_a != broken_b


# ── Nominal admission / invalidation ─────────────────────────────────────────


def test_first_turn_records_identity_without_cleanup():
    """First use with the slot OBSERVED absent: commit, nothing to clean."""
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
    assert one_shot(agent, SSH) is False
    agent.seed_generic()  # env created during the SSH era
    assert one_shot(agent, LOCAL) is True
    assert agent.cleanup_calls == [("default", False)]
    assert agent.get_active_env("default") is None
    # Committed: a follow-up same-identity turn does not re-invalidate.
    assert one_shot(agent, LOCAL) is False
    assert len(agent.cleanup_calls) == 1


def test_local_then_ssh_invalidates_default_slot():
    agent = FakeAgent()
    assert one_shot(agent, LOCAL) is False
    agent.seed_generic()
    assert one_shot(agent, SSH) is True
    assert agent.cleanup_calls == [("default", False)]


def test_ssh_host_change_invalidates():
    agent = FakeAgent()
    one_shot(agent, SSH)
    agent.seed_generic()
    assert one_shot(agent, {**SSH, "TERMINAL_SSH_HOST": "b.example"}) is True
    assert agent.cleanup_calls == [("default", False)]


# ── First-use slot verification (#5988 round 4) ──────────────────────────────


def test_first_use_with_populated_slot_performs_verified_cleanup():
    """The first lease in the process must not blindly record its identity:
    tool-capable code outside the lease discipline may already have created
    the "default" env. A populated slot gets the full verified cleanup."""
    agent = FakeAgent()
    agent.seed_generic()
    assert one_shot(agent, SSH) is True  # invalidated, not merely recorded
    assert agent.cleanup_calls == [("default", False)]
    assert agent.get_active_env("default") is None
    with isolation._COND:
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(SSH)


def test_first_use_with_populated_container_slot_fails_closed(monkeypatch):
    """First use finding a container-backed env whose removal cannot be
    verified must REJECT the turn — committing would let the very first turn
    run against a container nobody proved gone."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    cid = agent.seed_docker()
    agent.rm_silently_fails = True
    agent.direct_rm_results = [False]
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        one_shot(agent, DOCKER_A)
    # Force-removal was requested off the env object's own evidence, even
    # though there is no previous identity to consult.
    assert agent.cleanup_calls == [("default", True)]
    with isolation._COND:
        assert isolation._last_backend_identity is None  # never committed
        assert isolation._pending_container_removals == {cid: "docker"}
    assert active_turn_count() == 0

    # The slot was already popped by the failed attempt (real pop-first
    # contract) — the retry goes through the observed-absent path and must
    # STILL refuse to commit while the container stays ledgered.
    assert agent.get_active_env("default") is None
    agent.direct_rm_results = [False]
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        one_shot(agent, DOCKER_A)
    with isolation._COND:
        assert isolation._last_backend_identity is None

    # Removal finally verifies: first use commits.
    agent.direct_rm_results = None
    assert one_shot(agent, DOCKER_A) is False  # nothing left to clean
    assert cid not in agent.containers
    with isolation._COND:
        assert isolation._pending_container_removals == {}
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(DOCKER_A)


def test_concurrent_first_use_arrivals_serialize_through_the_transition():
    """Two racing first arrivals: one verifies, the other waits and is then
    admitted on the committed identity — never both probing concurrently."""
    agent = FakeAgent()
    in_probe = threading.Event()
    release_probe = threading.Event()

    def held_get_active_env(task_id):
        in_probe.set()
        release_probe.wait(timeout=10)
        return agent.get_active_env(task_id)

    leader_result = {}

    def _leader():
        lease, did = isolation.acquire_terminal_backend_turn_lease(
            SSH, cleanup_vm=agent.cleanup_vm, get_active_env=held_get_active_env
        )
        leader_result["invalidated"] = did
        lease.release()

    leader = threading.Thread(target=_leader, daemon=True)
    leader.start()
    assert in_probe.wait(timeout=5)

    follower_result = {}

    def _follower():
        lease, did = turn(agent, SSH, wait_seconds=10)
        follower_result["invalidated"] = did
        lease.release()

    follower = threading.Thread(target=_follower, daemon=True)
    follower.start()
    follower.join(timeout=0.3)
    assert follower.is_alive(), "second first-use arrival must wait for the leader"

    release_probe.set()
    leader.join(timeout=10)
    follower.join(timeout=10)
    assert leader_result["invalidated"] is False
    assert follower_result["invalidated"] is False
    assert agent.cleanup_calls == []


# ── Docker force-remove + verified removal ───────────────────────────────────


def test_docker_transition_force_removes_and_verifies_container(monkeypatch):
    """Gate #5988 CORE: leaving a Docker identity must force-remove AND the
    commit must be backed by an observed container removal, not by
    cleanup_vm returning."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    assert one_shot(agent, DOCKER_A) is False
    cid = agent.seed_docker()
    assert one_shot(agent, LOCAL) is True
    assert agent.cleanup_calls == [("default", True)]
    assert cid not in agent.containers
    with isolation._COND:
        assert isolation._pending_container_removals == {}


def test_docker_image_change_invalidates_with_force_remove(monkeypatch):
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    one_shot(agent, DOCKER_A)
    agent.seed_docker()
    assert one_shot(agent, DOCKER_B) is True
    assert agent.cleanup_calls == [("default", True)]


def test_non_docker_transition_does_not_force_remove():
    agent = FakeAgent()
    one_shot(agent, SSH)
    agent.seed_generic()
    assert one_shot(agent, {"TERMINAL_ENV": "docker"}) is True
    assert agent.cleanup_calls == [("default", False)]


# ── Fail closed: cleanup failure under the REAL contract ─────────────────────


def test_silent_docker_rm_failure_is_caught_and_turn_rejected(monkeypatch):
    """Gate #5988 CORE: the real cleanup_vm returns None even when the docker
    removal silently fails. The verification layer must catch it: transition
    uncommitted, turn NOT admitted, container ledgered for retry."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    docker_identity = isolation.terminal_backend_identity(DOCKER_A)

    assert one_shot(agent, DOCKER_A) is False
    cid = agent.seed_docker()
    agent.rm_silently_fails = True
    agent.direct_rm_results = [False]  # direct fallback also fails this round
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
    one_shot(agent, DOCKER_A)
    cid = agent.seed_docker()
    agent.backend_cleanup_error = RuntimeError("docker daemon unreachable")
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
    one_shot(agent, DOCKER_A)
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
    assert one_shot(agent, LOCAL) is True
    assert cid not in agent.containers


def test_unremoved_default_slot_fails_the_transition():
    """Contract-drift guard: if cleanup_vm stops popping the slot, committing
    would hand the new identity a live stale env — must fail closed."""
    agent = FakeAgent()
    one_shot(agent, SSH)
    agent.seed_generic()
    agent.pop_slot = False
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        one_shot(agent, LOCAL)
    assert active_turn_count() == 0


def test_absent_agent_runtime_commits_vacuously(monkeypatch):
    """When tools.terminal_tool is not importable (e.g. webui CI runs without
    hermes-agent), no env cache can exist in this process — a transition must
    commit vacuously instead of rejecting every turn forever."""
    agent = FakeAgent()
    assert one_shot(agent, SSH) is False
    monkeypatch.setattr(
        isolation, "_resolve_terminal_tool", lambda: isolation._AGENT_RUNTIME_ABSENT
    )
    assert isolation.maybe_invalidate_default_terminal_env(LOCAL) is True
    with isolation._COND:
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(LOCAL)


def test_present_runtime_with_unresolvable_helpers_fails_closed(monkeypatch):
    """A present-but-broken runtime is NOT the vacuous case: the cache may
    exist and we cannot verify it — reject the turn."""
    agent = FakeAgent()
    assert one_shot(agent, SSH) is False
    agent.seed_generic()

    class _BrokenModule:
        pass

    monkeypatch.setattr(isolation, "_resolve_terminal_tool", lambda: _BrokenModule())
    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        isolation.maybe_invalidate_default_terminal_env(LOCAL)
    assert active_turn_count() == 0
    monkeypatch.undo()
    # Runtime resolvable again: retry succeeds.
    assert one_shot(agent, LOCAL) is True
    assert agent.cleanup_calls == [("default", False)]


# ── Honest Docker probe (#5988 round 4: daemon failure ≠ removed) ────────────


def _write_fake_docker(tmp_path, name, stderr_text, exit_code, stdout_text=""):
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        + (f"printf '%s\\n' \"{stdout_text}\"\n" if stdout_text else "")
        + (f"printf '%s\\n' \"{stderr_text}\" >&2\n" if stderr_text else "")
        + f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


needs_sh = pytest.mark.skipif(
    sys.platform == "win32", reason="scripted docker executables need a POSIX sh"
)


@needs_sh
def test_probe_exit_zero_means_container_exists(tmp_path):
    exe = _write_fake_docker(tmp_path, "docker-up", "", 0, stdout_text="sha256:abc")
    assert isolation._container_exists("cid-1234", exe) is True


@needs_sh
def test_probe_no_such_object_means_container_gone(tmp_path):
    exe = _write_fake_docker(
        tmp_path, "docker-gone", "Error: No such object: cid-1234", 1
    )
    assert isolation._container_exists("cid-1234", exe) is False


@needs_sh
def test_probe_daemon_failure_raises_instead_of_reporting_gone(tmp_path):
    """The reproduced round-4 false-commit: a dead daemon exits nonzero, which
    the old probe read as "container removed". It must raise (unverifiable)."""
    exe = _write_fake_docker(
        tmp_path,
        "docker-down",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?",
        1,
    )
    with pytest.raises(RuntimeError):
        isolation._container_exists("cid-1234", exe)


@needs_sh
def test_daemon_failure_keeps_container_ledgered_and_blocks_commit(tmp_path, monkeypatch):
    """Through the verification layer (real probe subprocess, no probe mocks):
    a daemon failure keeps the removal unverified — the entry stays ledgered
    and the transition that depends on it is rejected."""
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_POLL_SECONDS", 0.05)
    daemon_down = _write_fake_docker(
        tmp_path, "docker-down", "Cannot connect to the Docker daemon", 1
    )
    with isolation._COND:
        isolation._pending_container_removals["cid-9999"] = daemon_down
    assert isolation._verify_pending_container_removals() is False
    with isolation._COND:
        assert isolation._pending_container_removals == {"cid-9999": daemon_down}

    # Daemon comes back and reports the object definitively absent: verified.
    Path(daemon_down).write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Error: No such object: cid-9999' >&2\nexit 1\n",
        encoding="utf-8",
    )
    assert isolation._verify_pending_container_removals() is True
    with isolation._COND:
        assert isolation._pending_container_removals == {}


# ── REAL installed agent contract (#5988 round 4) ────────────────────────────
#
# These drive the module's OWN helper resolution against the installed
# tools.terminal_tool: the real cleanup_vm (pop-first, swallow, return None)
# seeded through the real module cache, with scripted docker executables so
# the actual subprocess probe path decides the outcome. Skipped when
# hermes-agent is not installed (upstream webui CI); run on the
# agent-installed verification lane.


def _real_terminal_tool_or_skip():
    terminal_tool = pytest.importorskip(
        "tools.terminal_tool",
        reason="hermes-agent not installed; real-contract lane runs agent-installed",
    )
    if "force_remove" not in inspect.signature(terminal_tool.cleanup_vm).parameters:
        pytest.skip("installed hermes-agent predates cleanup_vm(force_remove=...)")
    return terminal_tool


class _SeededContainerEnv:
    """Container-shaped env seeded into the REAL module cache. cleanup()
    raising mirrors a daemon-unreachable teardown — which the real cleanup_vm
    swallows."""

    def __init__(self, container_id, docker_exe, cleanup_error=None):
        self._container_id = container_id
        self._docker_exe = docker_exe
        self._cleanup_thread = None
        self._cleanup_error = cleanup_error
        self.cleanup_calls = []

    def cleanup(self, force_remove=False):
        self.cleanup_calls.append(force_remove)
        if self._cleanup_error is not None:
            raise self._cleanup_error


@needs_sh
def test_real_cleanup_vm_daemon_failure_is_rejected_not_admitted(tmp_path, monkeypatch):
    """THE reproduced round-4 scenario, against the real contract end to end:
    committed local identity, a container-backed env appears in the REAL
    module cache, an incompatible turn arrives while the Docker daemon is
    down. The real cleanup_vm pops the slot, swallows the teardown error and
    returns None; the probe cannot verify removal — the turn must be
    REJECTED and the identity left uncommitted (it used to be ADMITTED with
    committed=('local',))."""
    terminal_tool = _real_terminal_tool_or_skip()
    monkeypatch.setattr(terminal_tool, "_active_environments", {}, raising=True)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_POLL_SECONDS", 0.05)

    # Commit an initial local identity through the module's own resolution
    # (empty real cache → observed-absent first use).
    lease, invalidated = isolation.acquire_terminal_backend_turn_lease(LOCAL)
    assert invalidated is False
    lease.release()
    local_identity = isolation.terminal_backend_identity(LOCAL)

    daemon_down = _write_fake_docker(
        tmp_path, "docker-down", "Cannot connect to the Docker daemon", 1
    )
    env = _SeededContainerEnv(
        "cid-real-1", daemon_down, cleanup_error=RuntimeError("daemon unreachable")
    )
    terminal_tool._active_environments["default"] = env

    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        isolation.acquire_terminal_backend_turn_lease(DOCKER_A)

    # Real pop-first contract happened...
    assert env.cleanup_calls == [True]  # force-removal requested
    assert terminal_tool._active_environments.get("default") is None
    # ...and none of it was inferred as success.
    with isolation._COND:
        assert isolation._last_backend_identity == local_identity  # uncommitted
        assert isolation._pending_container_removals == {"cid-real-1": daemon_down}
    assert active_turn_count() == 0

    # Daemon recovers and reports the container definitively gone: the retry
    # verifies through the ledger and commits.
    Path(daemon_down).write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Error: No such object: cid-real-1' >&2\nexit 1\n",
        encoding="utf-8",
    )
    lease, invalidated = isolation.acquire_terminal_backend_turn_lease(DOCKER_A)
    assert invalidated is True
    lease.release()
    with isolation._COND:
        assert isolation._pending_container_removals == {}
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(DOCKER_A)


@needs_sh
def test_real_cleanup_vm_swallowed_teardown_with_verified_removal_commits(
    tmp_path, monkeypatch
):
    """Counterpart: the real cleanup_vm swallows a teardown error, but the
    probe POSITIVELY verifies the container absent — the transition commits.
    Proves the fail-closed layer keys on observed state, not on exceptions."""
    terminal_tool = _real_terminal_tool_or_skip()
    monkeypatch.setattr(terminal_tool, "_active_environments", {}, raising=True)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_POLL_SECONDS", 0.05)

    lease, _ = isolation.acquire_terminal_backend_turn_lease(LOCAL)
    lease.release()

    gone = _write_fake_docker(
        tmp_path, "docker-gone", "Error: No such object: cid-real-2", 1
    )
    env = _SeededContainerEnv(
        "cid-real-2", gone, cleanup_error=RuntimeError("teardown hiccup")
    )
    terminal_tool._active_environments["default"] = env

    lease, invalidated = isolation.acquire_terminal_backend_turn_lease(DOCKER_A)
    assert invalidated is True
    lease.release()
    assert env.cleanup_calls == [True]
    with isolation._COND:
        assert isolation._pending_container_removals == {}
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(DOCKER_A)


@needs_sh
def test_real_first_use_with_populated_cache_verifies_before_commit(tmp_path, monkeypatch):
    """First lease in the process finds a container env already in the REAL
    cache (created by code outside the lease discipline): it must be
    verifiably cleaned before ANY identity commits."""
    terminal_tool = _real_terminal_tool_or_skip()
    monkeypatch.setattr(terminal_tool, "_active_environments", {}, raising=True)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(isolation, "_CONTAINER_REMOVAL_POLL_SECONDS", 0.05)

    daemon_down = _write_fake_docker(
        tmp_path, "docker-down", "Cannot connect to the Docker daemon", 1
    )
    env = _SeededContainerEnv("cid-real-3", daemon_down)
    terminal_tool._active_environments["default"] = env

    with pytest.raises(isolation.TerminalBackendInvalidationFailed):
        isolation.acquire_terminal_backend_turn_lease(LOCAL)
    with isolation._COND:
        assert isolation._last_backend_identity is None  # nothing committed

    Path(daemon_down).write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Error: No such object: cid-real-3' >&2\nexit 1\n",
        encoding="utf-8",
    )
    lease, _ = isolation.acquire_terminal_backend_turn_lease(LOCAL)
    lease.release()
    with isolation._COND:
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(LOCAL)


# ── Fail closed: admission control ───────────────────────────────────────────


def test_differing_backend_turn_waits_for_active_lease():
    """A differing-backend turn must not evict an in-use env."""
    agent = FakeAgent()
    first, invalidated = turn(agent, SSH)
    assert invalidated is False
    agent.seed_generic()

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
    ssh_identity = isolation.terminal_backend_identity(SSH)
    first, _ = turn(agent, SSH)
    agent.seed_generic()
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
    seed, _ = turn(agent, SSH)
    seed.release()
    agent.seed_generic()

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
    seed, _ = turn(agent, DOCKER_A)
    seed.release()
    cid = agent.seed_docker()
    agent.rm_silently_fails = True
    agent.direct_rm_results = [False, True]  # leader's fallback fails; waiter's works

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
    seed, _ = turn(agent, SSH)
    seed.release()
    agent.seed_generic()

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


def test_lease_release_is_idempotent():
    """The streaming path releases the lease twice on the normal path (inner
    ordered release + outer backstop); the second must be a no-op and never
    underflow another turn's count."""
    agent = FakeAgent()
    first, _ = turn(agent, LOCAL)
    second, _ = turn(agent, LOCAL)
    assert active_turn_count() == 2
    first.release()
    first.release()  # backstop double-release
    assert active_turn_count() == 1  # second turn's count untouched
    second.release()
    assert active_turn_count() == 0


# ── Fail closed: unexpected isolation-module errors (#5988 round 4) ──────────


def test_failclosed_helper_converts_unexpected_errors_to_typed_rejection(monkeypatch):
    """Gate #5988 CORE: an UNEXPECTED failure during acquisition must not let
    the turn proceed without a lease. The turn entry point converts it to the
    same typed rejection that blocks AIAgent construction + tool execution."""

    def _boom(*args, **kwargs):
        raise RuntimeError("isolation module bug")

    monkeypatch.setattr(isolation, "acquire_terminal_backend_turn_lease", _boom)
    with pytest.raises(isolation.TerminalBackendIsolationError) as excinfo:
        isolation.acquire_turn_lease_failclosed(LOCAL)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    # No lease was registered for the rejected turn.
    assert active_turn_count() == 0


def test_failclosed_helper_passes_deliberate_rejections_through(monkeypatch):
    """Typed rejections keep their subtype (callers/tests distinguish timeout
    from invalidation failure) — the helper only converts UNEXPECTED errors."""
    agent = FakeAgent()
    first, _ = isolation.acquire_turn_lease_failclosed(
        SSH, cleanup_vm=agent.cleanup_vm, get_active_env=agent.get_active_env
    )
    with pytest.raises(isolation.TerminalBackendTransitionTimeout):
        isolation.acquire_turn_lease_failclosed(
            LOCAL,
            cleanup_vm=agent.cleanup_vm,
            get_active_env=agent.get_active_env,
            wait_seconds=0.05,
        )
    first.release()


def test_failclosed_helper_returns_lease_on_success():
    agent = FakeAgent()
    lease, invalidated = isolation.acquire_turn_lease_failclosed(
        LOCAL, cleanup_vm=agent.cleanup_vm, get_active_env=agent.get_active_env
    )
    assert invalidated is False
    assert active_turn_count() == 1
    lease.release()
    assert active_turn_count() == 0


# ── Turn-path integration source guards ──────────────────────────────────────


def test_streaming_acquires_full_turn_lease_on_effective_env():
    """Source guard: the streaming turn path leases the identity for the whole
    turn, fail-closed on EVERY acquisition outcome.

    Identity must come from the effective post-merge environment (process env
    overlaid with the profile runtime env), the lease must be acquired BEFORE
    _ENV_LOCK (waiters must not block other turns' env restore) via the
    fail-closed entry point (#5988 round 4: unexpected acquisition errors must
    abort the turn, not run it open), and it must be released in the turn's
    finally after the env restore.
    """
    assert "acquire_turn_lease_failclosed" in STREAMING_PY
    # Effective env = os.environ overlaid with the profile runtime env.
    merged_idx = STREAMING_PY.find("_effective_turn_env = dict(os.environ)")
    assert merged_idx >= 0
    overlay_idx = STREAMING_PY.find(
        "_effective_turn_env.update(_safe_profile_runtime_env)", merged_idx
    )
    assert overlay_idx > merged_idx
    acquire_idx = STREAMING_PY.find("acquire_turn_lease_failclosed(", overlay_idx)
    assert acquire_idx > overlay_idx
    # Lease acquisition happens before the env-mutation critical section.
    update_idx = STREAMING_PY.find("os.environ.update(_safe_profile_runtime_env)")
    assert update_idx >= 0
    assert acquire_idx < update_idx
    # No fail-open remnant: the turn path must not swallow acquisition errors
    # (the pre-round-4 "log loudly and proceed" posture) and must not bypass
    # the fail-closed wrapper by calling the raw acquisition function.
    assert "log loudly and proceed" not in STREAMING_PY
    assert "acquire_terminal_backend_turn_lease(" not in STREAMING_PY
    # Released in the finally, after the profile env restore loop.
    restore_idx = STREAMING_PY.find("for _key, _old_value in old_profile_env.items()")
    release_idx = STREAMING_PY.find("_terminal_backend_lease.release()")
    assert restore_idx >= 0
    assert release_idx > restore_idx
    # AND a backstop release in the outermost guaranteed-teardown finally:
    # the lease is acquired well before the inner try begins, so an exception
    # in that window must not leak the lease (a leaked lease under fail-closed
    # admission is a standing denial for differing-backend turns).
    backstop_idx = STREAMING_PY.find("_terminal_backend_lease.release()", release_idx + 1)
    metering_teardown_idx = STREAMING_PY.find("meter().end_session(stream_id, 0)")
    assert metering_teardown_idx > 0
    assert backstop_idx > metering_teardown_idx


def test_api_chat_sync_handler_is_under_the_same_lease():
    """Source guard (#5988 round 4): the fallback POST /api/chat handler
    constructs a tool-capable AIAgent and must hold the same full-turn
    backend-identity lease — acquired fail-closed BEFORE its TERMINAL_* env
    mutation and AIAgent construction, rejected turns surfaced as a
    retryable error instead of running, and released in its finally after
    the env restore."""
    start = ROUTES_PY.find("def _handle_chat_sync(")
    assert start >= 0
    end = ROUTES_PY.find("\ndef ", start + 1)
    handler_src = ROUTES_PY[start:end]
    acquire_idx = handler_src.find("acquire_turn_lease_failclosed(")
    assert acquire_idx > 0, "/api/chat must acquire the backend-identity lease"
    # Fail closed: deliberate rejections return an error response...
    reject_idx = handler_src.find("except TerminalBackendIsolationError", acquire_idx)
    assert reject_idx > acquire_idx
    assert "status=503" in handler_src[reject_idx : reject_idx + 300]
    # ...and the raw (non-fail-closed) acquisition function is never used.
    assert "acquire_terminal_backend_turn_lease(" not in handler_src
    # Ordering: lease before the env mutation, env mutation before the agent.
    env_mutation_idx = handler_src.find('os.environ["TERMINAL_CWD"]')
    agent_idx = handler_src.find("AIAgent(")
    assert 0 < acquire_idx < env_mutation_idx < agent_idx
    # Released in the finally after the env restore (idempotent release).
    restore_idx = handler_src.find('os.environ["HERMES_SESSION_KEY"] = old_session_key')
    release_idx = handler_src.find("_terminal_backend_lease.release()")
    assert 0 < restore_idx < release_idx


# ── Round 5: profile-complete, profile-safe identity ─────────────────────────
#
# Gate #5988 round 5 [CORE]: the identity that gates backend reuse omitted the
# profile (HERMES_HOME) and the forwarded-secret VALUES, so two profiles with
# identical backend config produced byte-identical identities and one could
# reuse the other's cached container carrying the other profile's secret.

ALPHA = {
    **DOCKER_A,
    "HERMES_HOME": "/profiles/alpha",
    "TERMINAL_DOCKER_FORWARD_ENV": '["TENOR_API_KEY"]',
    "TENOR_API_KEY": "alpha-secret",
}
BETA = {
    **DOCKER_A,
    "HERMES_HOME": "/profiles/beta",
    "TERMINAL_DOCKER_FORWARD_ENV": '["TENOR_API_KEY"]',
    "TENOR_API_KEY": "beta-secret",
}


def test_backend_type_still_readable_at_index_0():
    """The profile components are APPENDED, so callers that read identity[0]
    for the backend type (e.g. the _FORCE_REMOVE_ENV_TYPES check) keep working."""
    assert isolation.terminal_backend_identity({})[0] == "local"
    assert isolation.terminal_backend_identity(SSH)[0] == "ssh"
    assert isolation.terminal_backend_identity(DOCKER_A)[0] == "docker"


def test_identity_carries_profile_suffix():
    ident = isolation.terminal_backend_identity(
        {"TERMINAL_ENV": "local", "HERMES_HOME": "/h"}
    )
    assert "home" in ident and "/h" in ident and "fwd" in ident


def test_distinct_profile_homes_split_identity_even_with_identical_backend():
    """The reproduced cross-profile leak: identical backend config, different
    profile — identities MUST differ so no reuse is possible."""
    assert isolation.terminal_backend_identity(ALPHA) != isolation.terminal_backend_identity(BETA)


def test_hermes_home_splits_even_a_local_backend():
    """Profile boundary is universal — a persistent LOCAL shell inherits the
    host env, so two profiles must not share one local slot either."""
    a = isolation.terminal_backend_identity({"TERMINAL_ENV": "local", "HERMES_HOME": "/a"})
    b = isolation.terminal_backend_identity({"TERMINAL_ENV": "local", "HERMES_HOME": "/b"})
    assert a != b
    assert a[0] == "local"


def test_same_home_different_forwarded_secret_value_splits_identity():
    base = {**DOCKER_A, "HERMES_HOME": "/p/x", "TERMINAL_DOCKER_FORWARD_ENV": '["TENOR_API_KEY"]'}
    a = isolation.terminal_backend_identity({**base, "TENOR_API_KEY": "aaa"})
    b = isolation.terminal_backend_identity({**base, "TENOR_API_KEY": "bbb"})
    assert a != b


def test_forwarded_secret_value_never_appears_in_identity_tuple():
    """The fingerprint is a hash — the raw secret must not leak into the
    identity tuple (which gets logged)."""
    ident = isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_FORWARD_ENV": '["TENOR_API_KEY"]', "TENOR_API_KEY": "top-secret-value"}
    )
    assert "top-secret-value" not in "\x1e".join(map(str, ident))


def test_forwarded_env_unset_vs_empty_value_differ():
    fwd = '["K"]'
    unset = isolation.terminal_backend_identity({**DOCKER_A, "TERMINAL_DOCKER_FORWARD_ENV": fwd})
    empty = isolation.terminal_backend_identity({**DOCKER_A, "TERMINAL_DOCKER_FORWARD_ENV": fwd, "K": ""})
    assert unset != empty


def test_forwarded_fingerprint_is_forward_list_order_stable():
    a = isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_FORWARD_ENV": '["A","B"]', "A": "1", "B": "2"}
    )
    b = isolation.terminal_backend_identity(
        {**DOCKER_A, "TERMINAL_DOCKER_FORWARD_ENV": '["B","A"]', "A": "1", "B": "2"}
    )
    assert a == b


def test_no_forwarded_env_yields_empty_fingerprint():
    assert isolation._forwarded_secret_fingerprint({}) == ""
    assert isolation._forwarded_secret_fingerprint({"TERMINAL_DOCKER_FORWARD_ENV": "[]"}) == ""


def test_distinct_profiles_do_not_reuse_a_cached_backend(monkeypatch):
    """End-to-end: alpha commits a docker identity and seeds its container;
    a beta turn (different home + secret) must NOT reuse it — it triggers a
    verified transition (force-remove alpha's container), not a silent share."""
    agent = FakeAgent()
    patch_probes(monkeypatch, agent)
    # alpha first use: slot observed absent, commits, no cleanup
    assert one_shot(agent, ALPHA) is False
    cid = agent.seed_docker()  # alpha's container now cached under "default"
    # beta arrives: different identity → transition, force-remove alpha's cid
    assert one_shot(agent, BETA) is True
    assert agent.cleanup_calls == [("default", True)]
    assert cid not in agent.containers
    with isolation._COND:
        assert isolation._last_backend_identity == isolation.terminal_backend_identity(BETA)


# ── Round 5: lease-leak on synchronous setup / restore failure ───────────────


def test_streaming_stamps_profile_home_and_nests_the_release():
    """Source guard: the streaming identity snapshot stamps the RESOLVED
    profile home (not stale os.environ), and the primary release is nested in
    a finally so a restore failure can't skip it."""
    assert "_effective_turn_env['HERMES_HOME'] = _profile_home" in STREAMING_PY
    restore = STREAMING_PY.find("for _key, _old_value in old_profile_env.items()")
    assert restore > 0
    release = STREAMING_PY.find("_terminal_backend_lease.release()", restore)
    # a `finally:` sits between the restore loop and the primary release
    nested_finally = STREAMING_PY.rfind("finally:", restore, release)
    assert 0 < restore < nested_finally < release


def test_api_chat_sync_lease_survives_setup_and_restore_failure():
    """Source guard (#5988 round 5 lease-leak): in _handle_chat_sync the env
    MUTATION is inside the guarded try (round-4 left it between the acquire and
    the try → a throw leaked the lease with no backstop), and the release is
    NESTED in a finally so a restore failure can't skip it."""
    start = ROUTES_PY.find("def _handle_chat_sync(")
    end = ROUTES_PY.find("\ndef ", start + 1)
    src = ROUTES_PY[start:end]
    acquire = src.find("acquire_turn_lease_failclosed(")
    assert acquire > 0
    guarded_try = src.find("\n    try:", acquire)
    mutation = src.find('os.environ["TERMINAL_CWD"] = str(workspace)', acquire)
    # mutation is AFTER the guarding try opens (i.e. inside it), not before it
    assert 0 < acquire < guarded_try < mutation
    # release is nested under a finally that follows the restore
    restore = src.find('os.environ.pop("TERMINAL_CWD", None)', mutation)
    release = src.find("_terminal_backend_lease.release()", restore)
    nested_finally = src.rfind("finally:", restore, release)
    assert 0 < restore < nested_finally < release


def test_api_chat_sync_identity_comes_from_resolved_profile_not_raw_environ():
    """Source guard (#5988 P1 / greptile): /api/chat must build the lease
    identity from THIS request's RESOLVED profile overlay + stamped
    HERMES_HOME, not raw process-global os.environ (which a concurrent
    streaming turn owns for its whole run). Building it from live os.environ
    would let a sync turn reuse the streaming profile's cached backend."""
    start = ROUTES_PY.find("def _handle_chat_sync(")
    end = ROUTES_PY.find("\ndef ", start + 1)
    src = ROUTES_PY[start:end]
    # The raw-os.environ acquire is gone.
    assert "acquire_turn_lease_failclosed(dict(os.environ))" not in src
    # The profile is resolved and overlaid, HERMES_HOME stamped, and THAT
    # snapshot is what the lease is acquired from.
    resolve = src.find("get_hermes_home_for_profile(")
    overlay = src.find("_effective_turn_env.update(_chat_safe_profile_env)")
    stamp = src.find('_effective_turn_env["HERMES_HOME"] = _chat_profile_home')
    acquire = src.find("acquire_turn_lease_failclosed(_effective_turn_env)")
    assert 0 < resolve < overlay < acquire
    assert 0 < stamp < acquire
    # And the resolved profile env is actually APPLIED for the turn (so the
    # agent creates the backend the identity names), then restored.
    assert "os.environ.update(_chat_safe_profile_env)" in src
    assert "for _pk, _pv in _old_profile_env.items():" in src
