"""Defense-in-depth for multi-profile terminal backend switches (#5937).

Hermes Agent collapses ordinary session task IDs to a shared ``"default"``
terminal/file environment cache slot (see hermes-agent ``tools/terminal_tool``
and upstream agent issue #62720). WebUI hosts many profiles in one process and
applies each profile's terminal settings via process env for the turn. That is
correct for *new* environment creation, but it does not by itself evict a
cached incompatible environment under ``"default"``.

This module tracks the backend *identity* in effect for each agent turn and,
when a turn starts under a different identity, calls the agent's
``cleanup_vm("default")`` so the next terminal/file tool recreates against the
now-correct ``TERMINAL_*`` env.

Turns hold a full-turn *lease* on their identity: a differing-backend turn
waits for in-flight turns on the previous identity to finish before
invalidating, so it can never evict an environment that is still in use by a
concurrent turn. Same-backend turns share the identity freely and never
invalidate, preserving the agent's intentional cross-session sharing of one
long-lived local/container env.

Persistent Docker containers are reattached by labels without comparing
image, so a plain (non-force) cleanup can leave a stale container that a
later Docker turn silently reuses; transitions *away from* Docker therefore
clean up with ``force_remove=True``.
"""

from __future__ import annotations

import logging
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
# away from these must force-remove, or Docker-A -> local -> Docker-B silently
# reuses image A's container.
_FORCE_REMOVE_ENV_TYPES = frozenset({"docker"})

# How long a differing-backend turn waits for in-flight turns on another
# identity before proceeding WITHOUT invalidation. Fail-safe direction: never
# evict an environment that may still be in use; the identity transition stays
# uncommitted, so a later turn retries the invalidation.
_LEASE_WAIT_SECONDS = 30.0

_COND = threading.Condition(threading.Lock())
_last_backend_identity: Optional[tuple[str, ...]] = None
_active_turn_counts: dict[tuple[str, ...], int] = {}


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
    """Clear process-global identity/lease tracking (tests only)."""
    global _last_backend_identity
    with _COND:
        _last_backend_identity = None
        _active_turn_counts.clear()
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


def _resolve_cleanup_vm():
    try:
        from tools.terminal_tool import cleanup_vm as _cleanup_vm  # type: ignore
    except Exception:
        logger.warning(
            "Could not import tools.terminal_tool.cleanup_vm to invalidate "
            "stale default terminal env after backend change (#5937); "
            "agent-side #62720 remains the primary correctness fix",
            exc_info=True,
        )
        return None
    return _cleanup_vm


def _invalidate_default_env(previous, identity, cleanup_vm) -> bool:
    """Run cleanup_vm('default') for a previous->identity transition.

    Returns True only on success; the caller commits the identity transition
    only then, so a transient import/cleanup failure is retried by a later
    turn instead of being permanently skipped.
    """
    if cleanup_vm is None:
        cleanup_vm = _resolve_cleanup_vm()
        if cleanup_vm is None:
            return False
    force_remove = previous[0] in _FORCE_REMOVE_ENV_TYPES
    try:
        if force_remove:
            cleanup_vm("default", force_remove=True)
        else:
            cleanup_vm("default")
    except Exception:
        logger.warning(
            "cleanup_vm('default') failed after terminal backend identity change "
            "(#5937) previous=%s current=%s; transition left uncommitted so a "
            "later turn retries",
            previous,
            identity,
            exc_info=True,
        )
        return False
    logger.info(
        "Invalidated agent default terminal env after backend identity change "
        "(#5937) previous=%s current=%s force_remove=%s",
        previous,
        identity,
        force_remove,
    )
    return True


def acquire_terminal_backend_turn_lease(
    runtime_env: Mapping[str, str],
    *,
    cleanup_vm=None,
    wait_seconds: Optional[float] = None,
) -> tuple[TerminalBackendTurnLease, bool]:
    """Acquire a full-turn lease on this turn's backend identity.

    Returns ``(lease, invalidated)``. The caller MUST call ``lease.release()``
    when the turn finishes (success or failure).

    - First turn in the process records the identity without cleanup.
    - Same identity as the last committed turn: no cleanup, runs concurrently
      with other same-identity turns.
    - Differing identity: waits (bounded) for in-flight turns on other
      identities to release, then invalidates the agent ``"default"`` slot.
      Only the first turn of the new identity performs the invalidation;
      concurrent same-identity turns piggyback on it.
    - On wait timeout or cleanup failure the transition is NOT committed, so a
      later turn retries; an in-use environment is never force-evicted.

    *runtime_env* should be the effective post-merge environment for the turn
    (process env overlaid with the profile's runtime env), not the profile env
    alone, so identity reflects what environment creation will actually see.
    """
    global _last_backend_identity

    identity = terminal_backend_identity(runtime_env)
    timeout = _LEASE_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + timeout
    timed_out = False
    with _COND:
        while any(
            key != identity and count > 0
            for key, count in _active_turn_counts.items()
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            _COND.wait(remaining)
        previous = _last_backend_identity
        _active_turn_counts[identity] = _active_turn_counts.get(identity, 0) + 1
        lease = TerminalBackendTurnLease(identity)
        if previous is None:
            _last_backend_identity = identity
            return lease, False
        if previous == identity:
            return lease, False
        if timed_out:
            logger.warning(
                "Terminal backend identity changed (previous=%s current=%s) but "
                "turns on the previous identity were still active after %.0fs; "
                "proceeding WITHOUT invalidation (#5937). The transition stays "
                "uncommitted and will be retried by a later turn.",
                previous,
                identity,
                timeout,
            )
            return lease, False
        if _active_turn_counts.get(identity, 0) > 1:
            # Another turn on this same new identity is already in flight and
            # owns (or already performed) the invalidation; don't evict the
            # environment it may have just created.
            return lease, False

    invalidated = _invalidate_default_env(previous, identity, cleanup_vm)
    if invalidated:
        with _COND:
            _last_backend_identity = identity
    return lease, invalidated


def maybe_invalidate_default_terminal_env(
    runtime_env: Mapping[str, str],
    *,
    cleanup_vm=None,
) -> bool:
    """Point-in-time invalidation check (no full-turn lease held afterwards).

    Kept for tests and non-turn callers; agent turns should use
    ``acquire_terminal_backend_turn_lease`` so the identity stays leased for
    the duration of the turn.
    """
    lease, invalidated = acquire_terminal_backend_turn_lease(
        runtime_env, cleanup_vm=cleanup_vm
    )
    lease.release()
    return invalidated
