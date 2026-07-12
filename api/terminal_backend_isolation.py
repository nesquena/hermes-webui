"""Defense-in-depth for multi-profile terminal backend switches (#5937).

Hermes Agent collapses ordinary session task IDs to a shared ``"default"``
terminal/file environment cache slot (see hermes-agent ``tools/terminal_tool``
and upstream agent issue #62720). WebUI hosts many profiles in one process and
applies each profile's terminal settings via process env for the turn. That is
correct for *new* environment creation, but it does not by itself evict a
cached incompatible environment under ``"default"``.

This module tracks the last backend *identity* applied for a turn and, when
the identity changes, calls the agent's ``cleanup_vm("default")`` so the next
terminal/file tool recreates against the now-correct ``TERMINAL_*`` env.

Same-backend consecutive turns do not invalidate, preserving the agent's
intentional cross-session sharing of one long-lived local/container env.
"""

from __future__ import annotations

import logging
import threading
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

# Process-global last backend identity that was applied for a WebUI agent turn.
# Guarded by ``_LOCK``; read/write only via helpers below.
_LOCK = threading.Lock()
_last_backend_identity: Optional[tuple[str, ...]] = None


def terminal_backend_identity(runtime_env: Mapping[str, str]) -> tuple[str, ...]:
    """Return a stable identity for the terminal backend encoded in *runtime_env*.

    Keys match what the agent uses to create environments (TERMINAL_ENV plus
    remote/container targeting). CWD is intentionally excluded so two same-
    backend sessions with different workspaces still share one cache slot.
    """
    env_type = str(runtime_env.get("TERMINAL_ENV") or "local").strip().lower() or "local"
    ssh_host = str(runtime_env.get("TERMINAL_SSH_HOST") or "").strip()
    ssh_user = str(runtime_env.get("TERMINAL_SSH_USER") or "").strip()
    ssh_port = str(runtime_env.get("TERMINAL_SSH_PORT") or "").strip()
    docker_image = str(runtime_env.get("TERMINAL_DOCKER_IMAGE") or "").strip()
    modal_image = str(runtime_env.get("TERMINAL_MODAL_IMAGE") or "").strip()
    singularity_image = str(runtime_env.get("TERMINAL_SINGULARITY_IMAGE") or "").strip()
    daytona_image = str(runtime_env.get("TERMINAL_DAYTONA_IMAGE") or "").strip()
    return (
        env_type,
        ssh_host,
        ssh_user,
        ssh_port,
        docker_image,
        modal_image,
        singularity_image,
        daytona_image,
    )


def reset_terminal_backend_identity_for_tests() -> None:
    """Clear process-global identity tracking (tests only)."""
    global _last_backend_identity
    with _LOCK:
        _last_backend_identity = None


def maybe_invalidate_default_terminal_env(
    runtime_env: Mapping[str, str],
    *,
    cleanup_vm=None,
) -> bool:
    """Invalidate the agent ``"default"`` terminal env when backend identity changes.

    Returns True when ``cleanup_vm("default")`` was invoked. First turn in a
    process only records identity (no cleanup). Same identity as last turn is a
    no-op.

    *cleanup_vm* is injectable for unit tests; production resolves
    ``tools.terminal_tool.cleanup_vm`` lazily so WebUI still boots when the
    agent is not on ``sys.path``.
    """
    global _last_backend_identity

    identity = terminal_backend_identity(runtime_env)
    previous: Optional[tuple[str, ...]]
    with _LOCK:
        previous = _last_backend_identity
        if previous is not None and previous == identity:
            return False
        _last_backend_identity = identity
        if previous is None:
            return False

    if cleanup_vm is None:
        try:
            from tools.terminal_tool import cleanup_vm as _cleanup_vm  # type: ignore
        except Exception:
            logger.warning(
                "Could not import tools.terminal_tool.cleanup_vm to invalidate "
                "stale default terminal env after backend change (#5937); "
                "agent-side #62720 remains the primary correctness fix",
                exc_info=True,
            )
            return False
        cleanup_vm = _cleanup_vm

    try:
        cleanup_vm("default")
    except Exception:
        logger.warning(
            "cleanup_vm('default') failed after terminal backend identity change "
            "(#5937) previous=%s current=%s",
            previous,
            identity,
            exc_info=True,
        )
        return False

    logger.info(
        "Invalidated agent default terminal env after backend identity change "
        "(#5937) previous=%s current=%s",
        previous,
        identity,
    )
    return True
