"""Defense-in-depth for multi-profile terminal backend switches (#5937).

Hermes Agent collapses ordinary session task IDs to a shared ``"default"``
terminal/file environment cache slot (see hermes-agent ``tools/terminal_tool``
and upstream agent issue #62720). WebUI hosts many profiles in one process and
applies each profile's terminal settings via process env for the turn. That is
correct for *new* environment creation, but it does not by itself evict a
cached incompatible environment under ``"default"``.

This module tracks the backend *identity* in effect for each agent turn and,
when a turn starts under a different identity, invalidates the agent's
``"default"`` slot so the next terminal/file tool recreates against the
now-correct ``TERMINAL_*`` env.

Because a wrong reuse runs tools on another profile's backend, this is a
security boundary and every path fails CLOSED:

* Turns hold a full-turn *lease* on their identity. A differing-backend turn
  waits (bounded) for in-flight turns to finish; if they don't, the incoming
  turn is REJECTED with :class:`TerminalBackendTransitionTimeout` — it never
  runs against a mismatched slot, and no lease is registered for it.
* While a transition's cleanup is in flight (explicit ``_transition_identity``
  state) NO turn is admitted — not even same-new-identity turns. They wait for
  the transition to complete and then re-evaluate; if the transition failed
  they retry it themselves rather than assuming the leader succeeded.
* Success is never inferred from ``cleanup_vm`` returning. The real agent
  contract (hermes-agent ``tools/terminal_tool.cleanup_vm``) pops the cache
  slot FIRST, then swallows backend-teardown exceptions and returns ``None``
  either way — and ``DockerEnvironment.cleanup(force_remove=True)`` runs
  ``docker stop``/``docker rm -f`` on a background daemon thread. Committing a
  transition therefore requires *observed postconditions*: the ``"default"``
  slot is verifiably gone (``get_active_env``), and for container-identity
  transitions the outgoing container is verifiably removed (``docker
  inspect`` probe, with a bounded wait for the agent's async removal and one
  direct ``docker rm -f`` fallback). Unverifiable == failed: the transition
  stays uncommitted, the incoming turn is rejected with
  :class:`TerminalBackendInvalidationFailed`, and a later turn retries.
  Outstanding container removals are remembered in a pending ledger until a
  probe confirms them gone, so a failed removal cannot be forgotten by the
  retry (the slot pop already happened, so the retry's ``cleanup_vm`` alone
  would be a silent no-op).

One deliberate carve-out: when the hermes-agent runtime itself is not
importable (``tools.terminal_tool`` is where the ``"default"`` cache lives),
no cached environment can exist in this process, so a transition commits
vacuously — that is correctness, not fail-open (exercised by webui CI, which
runs without hermes-agent installed).

Same-backend turns share the identity freely and never invalidate, preserving
the agent's intentional cross-session sharing of one long-lived
local/container env. A synchronous, failure-reporting forced-cleanup contract
on the agent side (proposed alongside hermes-agent #63361) would let the
probe layer here shrink to a single verified call.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# Mirror of hermes-agent tools/terminal_tool.py defaults, so an explicitly
# configured default value and an absent key resolve to the same identity.
_AGENT_DEFAULT_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"
_AGENT_DEFAULT_SSH_PORT = "22"
_AGENT_DEFAULT_MODAL_MODE = "auto"

# Backends whose cached environment is a container the agent can reattach by
# labels without comparing image (persistent Docker). Cleanup on a transition
# away from these must force-remove AND verify removal, or
# Docker-A -> local -> Docker-B silently reuses image A's container.
# (Daytona shares the reattach shape but its cleanup() does not accept
# force_remove yet — agent-side hermes-agent #63361 tracks that contract.)
_FORCE_REMOVE_ENV_TYPES = frozenset({"docker"})

# How long a turn waits for in-flight turns on another identity (or for an
# in-progress transition) before being rejected. Fail-closed: on timeout the
# incoming turn does NOT run and no lease is registered for it.
_LEASE_WAIT_SECONDS = 30.0

# How long a transition leader waits for the agent's asynchronous
# ``docker stop``/``docker rm -f`` daemon thread to actually remove the
# outgoing container before attempting one direct ``docker rm -f`` fallback.
# DockerEnvironment.cleanup uses stop -t 10, so the normal case is seconds.
_CONTAINER_REMOVAL_WAIT_SECONDS = 45.0
_CONTAINER_REMOVAL_POLL_SECONDS = 0.25

_PROBE_TIMEOUT_SECONDS = 15
_DIRECT_REMOVE_TIMEOUT_SECONDS = 30

_COND = threading.Condition(threading.Lock())
_last_backend_identity: Optional[tuple[str, ...]] = None
_active_turn_counts: dict[tuple[str, ...], int] = {}
# Identity currently being transitioned TO while its leader runs cleanup +
# verification outside the lock. While set, no turn is admitted.
_transition_identity: Optional[tuple[str, ...]] = None
# container_id -> docker executable. Recorded BEFORE force-remove cleanup and
# popped only once a probe confirms the container is gone, so a failed or
# still-async removal survives into the next transition attempt.
_pending_container_removals: dict[str, str] = {}


class TerminalBackendIsolationError(RuntimeError):
    """Base class for fail-closed terminal backend isolation rejections."""


class TerminalBackendTransitionTimeout(TerminalBackendIsolationError):
    """Raised when a turn cannot be admitted within the bounded wait."""


class TerminalBackendInvalidationFailed(TerminalBackendIsolationError):
    """Raised when the previous backend env could not be verifiably removed."""


def _env_value(runtime_env: Mapping[str, str], key: str, default: str = "") -> str:
    value = runtime_env.get(key)
    text = str(value).strip() if value is not None else ""
    return text or default


def terminal_backend_identity(runtime_env: Mapping[str, str]) -> tuple[str, ...]:
    """Return a stable identity for the terminal backend encoded in *runtime_env*.

    Only the settings the agent actually uses to create the *active* backend
    participate — an SSH host left over in env must not change a local
    backend's identity, and vice versa. Absent keys are normalized to the
    agent's defaults so ``{}`` and ``{"TERMINAL_SSH_PORT": "22"}`` cannot
    disagree. CWD is intentionally excluded so two same-backend sessions with
    different workspaces still share one cache slot.
    """
    env_type = _env_value(runtime_env, "TERMINAL_ENV", "local").lower()
    if env_type == "ssh":
        return (
            "ssh",
            _env_value(runtime_env, "TERMINAL_SSH_HOST"),
            _env_value(runtime_env, "TERMINAL_SSH_USER"),
            _env_value(runtime_env, "TERMINAL_SSH_PORT", _AGENT_DEFAULT_SSH_PORT),
            _env_value(runtime_env, "TERMINAL_SSH_KEY"),
        )
    if env_type == "docker":
        return (
            "docker",
            _env_value(runtime_env, "TERMINAL_DOCKER_IMAGE", _AGENT_DEFAULT_IMAGE),
        )
    if env_type == "modal":
        return (
            "modal",
            _env_value(runtime_env, "TERMINAL_MODAL_IMAGE", _AGENT_DEFAULT_IMAGE),
            _env_value(
                runtime_env, "TERMINAL_MODAL_MODE", _AGENT_DEFAULT_MODAL_MODE
            ).lower(),
        )
    if env_type == "singularity":
        return (
            "singularity",
            _env_value(
                runtime_env,
                "TERMINAL_SINGULARITY_IMAGE",
                f"docker://{_AGENT_DEFAULT_IMAGE}",
            ),
        )
    if env_type == "daytona":
        return (
            "daytona",
            _env_value(runtime_env, "TERMINAL_DAYTONA_IMAGE", _AGENT_DEFAULT_IMAGE),
        )
    return (env_type,)


def reset_terminal_backend_identity_for_tests() -> None:
    """Clear process-global identity/lease/transition tracking (tests only)."""
    global _last_backend_identity, _transition_identity
    with _COND:
        _last_backend_identity = None
        _transition_identity = None
        _active_turn_counts.clear()
        _pending_container_removals.clear()
        _COND.notify_all()


class TerminalBackendTurnLease:
    """Held for the whole agent turn; release() decrements the identity count."""

    __slots__ = ("identity", "_released")

    def __init__(self, identity: tuple[str, ...]):
        self.identity = identity
        self._released = False

    def release(self) -> None:
        with _COND:
            if self._released:
                return
            self._released = True
            count = _active_turn_counts.get(self.identity, 0)
            if count <= 1:
                _active_turn_counts.pop(self.identity, None)
            else:
                _active_turn_counts[self.identity] = count - 1
            _COND.notify_all()


_AGENT_RUNTIME_ABSENT = object()


def _resolve_terminal_tool():
    """Return the agent terminal_tool module, or the ``_AGENT_RUNTIME_ABSENT``
    sentinel when hermes-agent is not installed in this process.

    Absence is meaningful, not a failure: the process-global ``"default"`` env
    cache LIVES in ``tools.terminal_tool``. If that module cannot be imported,
    no cached environment can exist in this process, so a backend transition
    is vacuously safe (there is nothing to invalidate). Import errors other
    than ImportError propagate to the caller, which fails closed.
    """
    try:
        import tools.terminal_tool as _terminal_tool  # type: ignore

        return _terminal_tool
    except ImportError:
        return _AGENT_RUNTIME_ABSENT


def _container_exists(container_id: str, docker_exe: str) -> bool:
    """Probe whether *container_id* still exists (running or stopped).

    Raises on probe failure (missing docker binary, timeout) so the caller
    treats "cannot verify" as "not verified" — fail closed.
    """
    proc = subprocess.run(
        [docker_exe or "docker", "inspect", "--format", "{{.Id}}", container_id],
        capture_output=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _force_remove_container(container_id: str, docker_exe: str) -> None:
    """Directly ``docker rm -f`` a container the agent's async cleanup missed."""
    subprocess.run(
        [docker_exe or "docker", "rm", "-f", container_id],
        capture_output=True,
        timeout=_DIRECT_REMOVE_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


def _verify_pending_container_removals(wait_seconds: Optional[float] = None) -> bool:
    """Confirm every ledgered outgoing container is actually gone.

    Waits (bounded) for the agent's asynchronous removal thread, then attempts
    one direct ``docker rm -f`` per surviving container and re-probes. Returns
    True only when the ledger is empty; unverified entries stay ledgered for
    the next transition attempt.
    """
    with _COND:
        pending = dict(_pending_container_removals)
    if not pending:
        return True
    timeout = (
        _CONTAINER_REMOVAL_WAIT_SECONDS if wait_seconds is None else wait_seconds
    )
    deadline = time.monotonic() + timeout
    all_verified = True
    for container_id, docker_exe in pending.items():
        verified = False
        try:
            while True:
                if not _container_exists(container_id, docker_exe):
                    verified = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(_CONTAINER_REMOVAL_POLL_SECONDS)
            if not verified:
                logger.warning(
                    "Outgoing container %s still present after %.0fs; attempting "
                    "direct removal (#5937)",
                    container_id[:12],
                    timeout,
                )
                _force_remove_container(container_id, docker_exe)
                verified = not _container_exists(container_id, docker_exe)
        except Exception:
            logger.warning(
                "Could not verify removal of outgoing container %s (#5937); "
                "treating as not removed",
                container_id[:12],
                exc_info=True,
            )
            verified = False
        if verified:
            with _COND:
                _pending_container_removals.pop(container_id, None)
        else:
            all_verified = False
    return all_verified


def _invalidate_default_env(previous, identity, cleanup_vm, get_active_env) -> bool:
    """Verifiably invalidate the agent ``"default"`` slot for previous->identity.

    Returns True only when the postconditions are OBSERVED (slot gone; any
    outgoing containers gone). ``cleanup_vm`` returning is never treated as
    success by itself: the real agent contract swallows backend-teardown
    exceptions and returns None, and Docker force-removal runs asynchronously.
    """
    if cleanup_vm is None or get_active_env is None:
        terminal_tool = _resolve_terminal_tool()
        if terminal_tool is _AGENT_RUNTIME_ABSENT:
            if cleanup_vm is None and get_active_env is None:
                # No agent runtime -> the "default" env cache (which lives in
                # tools.terminal_tool) cannot exist in this process. There is
                # nothing to invalidate; the transition is vacuously safe.
                logger.info(
                    "hermes-agent terminal runtime not importable; no default "
                    "terminal env cache can exist in this process — backend "
                    "identity transition %s -> %s is vacuously safe (#5937)",
                    previous,
                    identity,
                )
                return True
            # Partial injection with an absent runtime is ambiguous — the
            # caller believes an env layer exists that we cannot verify.
            return False
        if cleanup_vm is None:
            cleanup_vm = getattr(terminal_tool, "cleanup_vm", None)
        if get_active_env is None:
            get_active_env = getattr(terminal_tool, "get_active_env", None)
            if get_active_env is None:
                # Older agent builds predate get_active_env; the underlying
                # cache dict has existed since the slot mechanism was added.
                _envs = getattr(terminal_tool, "_active_environments", None)
                if isinstance(_envs, dict):
                    get_active_env = _envs.get
        if cleanup_vm is None or get_active_env is None:
            logger.warning(
                "hermes-agent terminal runtime is present but cleanup_vm/"
                "get_active_env could not be resolved (#5937); failing closed",
            )
            return False

    force_remove = previous[0] in _FORCE_REMOVE_ENV_TYPES

    # Ledger the outgoing container BEFORE cleanup: cleanup_vm pops the slot
    # unconditionally, so this is the last moment its container id is
    # reachable. If anything below fails, the ledger survives into the retry.
    outgoing_env = None
    try:
        outgoing_env = get_active_env("default")
    except Exception:
        logger.warning(
            "get_active_env('default') failed pre-cleanup (#5937)", exc_info=True
        )
        return False
    if force_remove and outgoing_env is not None:
        container_id = getattr(outgoing_env, "_container_id", None)
        if container_id:
            docker_exe = getattr(outgoing_env, "_docker_exe", None) or "docker"
            with _COND:
                _pending_container_removals.setdefault(str(container_id), docker_exe)

    try:
        if force_remove:
            cleanup_vm("default", force_remove=True)
        else:
            cleanup_vm("default")
    except Exception:
        logger.warning(
            "cleanup_vm('default') raised after terminal backend identity change "
            "(#5937) previous=%s current=%s; transition left uncommitted so a "
            "later turn retries",
            previous,
            identity,
            exc_info=True,
        )
        return False

    # Postcondition 1: the "default" slot is actually gone. The current agent
    # contract pops it before teardown, so this also guards contract drift.
    try:
        if get_active_env("default") is not None:
            logger.warning(
                "cleanup_vm('default') returned but the default env slot is "
                "still populated (#5937) previous=%s current=%s; transition "
                "left uncommitted",
                previous,
                identity,
            )
            return False
    except Exception:
        logger.warning(
            "get_active_env('default') failed post-cleanup (#5937)", exc_info=True
        )
        return False

    # Postcondition 2: every ledgered outgoing container is verifiably gone.
    # DockerEnvironment.cleanup(force_remove=True) stops/removes on a daemon
    # thread; give it a bounded head start via the thread handle when we have
    # one, then probe (the probe loop tolerates the async lag either way).
    cleanup_thread = getattr(outgoing_env, "_cleanup_thread", None)
    if cleanup_thread is not None:
        try:
            cleanup_thread.join(timeout=_CONTAINER_REMOVAL_WAIT_SECONDS)
        except Exception:
            logger.debug(
                "joining outgoing env cleanup thread failed (#5937)", exc_info=True
            )
    if not _verify_pending_container_removals():
        logger.warning(
            "Outgoing container removal could not be verified after terminal "
            "backend identity change (#5937) previous=%s current=%s; transition "
            "left uncommitted",
            previous,
            identity,
        )
        return False

    logger.info(
        "Invalidated agent default terminal env after backend identity change "
        "(#5937) previous=%s current=%s force_remove=%s (slot + container "
        "removal verified)",
        previous,
        identity,
        force_remove,
    )
    return True


def acquire_terminal_backend_turn_lease(
    runtime_env: Mapping[str, str],
    *,
    cleanup_vm=None,
    get_active_env=None,
    wait_seconds: Optional[float] = None,
) -> tuple[TerminalBackendTurnLease, bool]:
    """Acquire a full-turn lease on this turn's backend identity, or reject.

    Returns ``(lease, invalidated)``. The caller MUST call ``lease.release()``
    when the turn finishes (success or failure).

    Admission rules (fail closed — a turn is never admitted against a slot
    that may belong to a different backend):

    - First turn in the process records the identity without cleanup.
    - Same identity as the last committed turn, no transition in progress:
      admitted immediately, runs concurrently with other same-identity turns.
    - While a transition's cleanup/verification is in flight, EVERY arrival
      waits — same-new-identity turns do not piggyback on an unproven
      cleanup; they re-evaluate once the transition commits or fails (and
      retry the invalidation themselves on failure).
    - Differing identity: waits (bounded) for in-flight turns to release,
      then becomes the transition leader: invalidates the agent ``"default"``
      slot and commits only on VERIFIED success.
    - On wait timeout raises :class:`TerminalBackendTransitionTimeout`; on
      unverifiable cleanup raises :class:`TerminalBackendInvalidationFailed`.
      In both cases no lease is registered and the transition stays
      uncommitted, so a later turn retries.

    *runtime_env* should be the effective post-merge environment for the turn
    (process env overlaid with the profile's runtime env), not the profile env
    alone, so identity reflects what environment creation will actually see.
    """
    global _last_backend_identity, _transition_identity

    identity = terminal_backend_identity(runtime_env)
    timeout = _LEASE_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + timeout

    with _COND:
        while True:
            if _transition_identity is None:
                previous = _last_backend_identity
                if previous is None or previous == identity:
                    if previous is None:
                        _last_backend_identity = identity
                    _active_turn_counts[identity] = (
                        _active_turn_counts.get(identity, 0) + 1
                    )
                    return TerminalBackendTurnLease(identity), False
                # Differing identity: all active turns necessarily run on the
                # committed identity, so becoming leader requires full drain.
                if not any(count > 0 for count in _active_turn_counts.values()):
                    _transition_identity = identity
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TerminalBackendTransitionTimeout(
                    "Terminal backend switch is waiting on turns still running "
                    "against the previous backend; this turn was not started to "
                    "avoid running tools on the wrong backend. Please retry in "
                    "a moment. (#5937)"
                )
            _COND.wait(remaining)
        previous = _last_backend_identity

    invalidated = False
    try:
        try:
            invalidated = _invalidate_default_env(
                previous, identity, cleanup_vm, get_active_env
            )
        except Exception:
            # _invalidate_default_env handles its own expected failures; an
            # escape here is a bug, but the boundary still fails CLOSED.
            logger.warning(
                "unexpected error during terminal backend invalidation (#5937)",
                exc_info=True,
            )
            invalidated = False
    finally:
        with _COND:
            _transition_identity = None
            if invalidated:
                _last_backend_identity = identity
                _active_turn_counts[identity] = (
                    _active_turn_counts.get(identity, 0) + 1
                )
            _COND.notify_all()

    if not invalidated:
        raise TerminalBackendInvalidationFailed(
            "The previous terminal backend environment could not be verifiably "
            "removed; this turn was not started to avoid running tools against "
            "a stale backend. It will be retried on the next turn — see server "
            "logs for the cleanup failure. (#5937)"
        )
    return TerminalBackendTurnLease(identity), True


def maybe_invalidate_default_terminal_env(
    runtime_env: Mapping[str, str],
    *,
    cleanup_vm=None,
    get_active_env=None,
) -> bool:
    """Point-in-time invalidation check (no full-turn lease held afterwards).

    Kept for tests and non-turn callers; agent turns should use
    ``acquire_terminal_backend_turn_lease`` so the identity stays leased for
    the duration of the turn. Propagates the same fail-closed
    :class:`TerminalBackendIsolationError` rejections.
    """
    lease, invalidated = acquire_terminal_backend_turn_lease(
        runtime_env, cleanup_vm=cleanup_vm, get_active_env=get_active_env
    )
    lease.release()
    return invalidated
