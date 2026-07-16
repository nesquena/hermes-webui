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

import hashlib
import json
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
_AGENT_DEFAULT_CONTAINER_CPU = "1"
_AGENT_DEFAULT_CONTAINER_MEMORY = "5120"
_AGENT_DEFAULT_CONTAINER_DISK = "51200"

# Truthy set mirrored from the agent's env parsing.
_AGENT_TRUTHY = frozenset({"true", "1", "yes"})

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


def _env_flag(runtime_env: Mapping[str, str], key: str, default: str) -> str:
    """Normalize a boolean-ish setting the way the agent parses it, so
    ``"true"``, ``"1"`` and ``"yes"`` (and an absent key with a truthy
    default) all produce one identity token."""
    return "1" if _env_value(runtime_env, key, default).lower() in _AGENT_TRUTHY else "0"


def _env_json(runtime_env: Mapping[str, str], key: str, default: str) -> str:
    """Canonicalize a JSON-valued setting (volumes, extra args, env maps) so
    formatting differences don't split identities. Unparseable values keep
    their raw text — two different broken strings must stay different."""
    raw = _env_value(runtime_env, key, default)
    try:
        return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError):
        return raw


def _forwarded_secret_fingerprint(runtime_env: Mapping[str, str]) -> str:
    """SHA-256 (truncated) over the (name, value) pairs the backend forwards
    from the host env into the sandbox (#5988 round 5).

    The backend identity already fingerprints ``TERMINAL_DOCKER_FORWARD_ENV``
    as a LIST of names, but the agent forwards each name's live VALUE into the
    container. Two profiles that forward the same var NAMES with DIFFERENT
    values (e.g. ``TENOR_API_KEY=alpha-secret`` vs ``beta-secret``) therefore
    produced byte-identical identities and could reuse each other's cached
    container carrying the other profile's secret. This folds the forwarded
    values in — hashed, so no secret value ever appears in the identity tuple
    or the logs that print it. Returns "" when nothing is forwarded.

    ``TERMINAL_DOCKER_ENV`` (explicit key→value map) is NOT included here: it is
    already value-complete in the backend identity via ``_env_json``.
    """
    raw = _env_value(runtime_env, "TERMINAL_DOCKER_FORWARD_ENV", "[]")
    seed = ""
    names: list[str] = []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Unparseable list: fail closed — fold the raw text in so two different
        # malformed values still differ, and enumerate nothing.
        seed = "raw:" + raw
    else:
        if isinstance(parsed, list):
            names = [str(n) for n in parsed]
        else:
            seed = "raw:" + raw
    if not names and not seed:
        return ""
    parts = [seed]
    for name in sorted(set(names)):
        value = runtime_env.get(name)
        # Distinguish "forwarded but unset" (None) from "forwarded empty" ("").
        marker = "\x00" if value is None else "\x01"
        parts.append(f"{name}{marker}{'' if value is None else value}")
    material = "\x1e".join(parts)
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _profile_identity_suffix(runtime_env: Mapping[str, str]) -> tuple[str, ...]:
    """Profile-boundary components appended to EVERY backend identity so two
    distinct profiles never share a cached backend (#5988 round 5).

    ``HERMES_HOME`` (the profile) is the coarse, universal boundary — it
    applies to every backend type, including a persistent LOCAL shell that
    inherits the host env. The forwarded-secret fingerprint is the
    finer guard for same-home turns whose forwarded credential VALUES differ.
    Appended (not prepended) so callers that read ``identity[0]`` for the
    backend type — e.g. the ``_FORCE_REMOVE_ENV_TYPES`` check — keep working.
    """
    return (
        "home",
        _env_value(runtime_env, "HERMES_HOME"),
        "fwd",
        _forwarded_secret_fingerprint(runtime_env),
    )


def terminal_backend_identity(runtime_env: Mapping[str, str]) -> tuple[str, ...]:
    """Return a stable identity for the terminal backend encoded in *runtime_env*.

    Every setting the agent's ``get_config()`` feeds into creating the
    *active* backend's environment participates (#5988 round 4: two turns are
    same-identity only when environment creation would consume identical
    inputs), AND the profile boundary is part of the identity (#5988 round 5:
    ``HERMES_HOME`` + a fingerprint of the forwarded-secret values, so two
    profiles can never share a cached backend carrying the other's secret).
    Inactive backends' settings are excluded — an SSH host left over in env
    must not change a local backend's identity. Absent keys are normalized to
    the agent's defaults so ``{}`` and ``{"TERMINAL_SSH_PORT": "22"}`` cannot
    disagree. Deliberately excluded: CWD (two same-backend sessions with
    different workspaces share one cache slot by design) and per-command knobs
    that don't shape the environment object (TERMINAL_TIMEOUT,
    TERMINAL_MAX_FOREGROUND_TIMEOUT, TERMINAL_DISK_WARNING_GB,
    TERMINAL_LIFETIME_SECONDS).

    *runtime_env* must be an identity-complete, profile-safe snapshot — the
    effective turn env with ``HERMES_HOME`` stamped to the RESOLVED profile
    home, not raw live ``os.environ`` (whose ``HERMES_HOME`` may still be a
    previous turn's). The streaming and ``/api/chat`` turn paths build it.
    """
    return _backend_identity_tuple(runtime_env) + _profile_identity_suffix(runtime_env)


def _backend_identity_tuple(runtime_env: Mapping[str, str]) -> tuple[str, ...]:
    """The backend-specific identity (without the profile suffix). ``[0]`` is
    the backend type, which ``_invalidate_default_env`` reads."""
    env_type = _env_value(runtime_env, "TERMINAL_ENV", "local").lower()
    if env_type == "ssh":
        return (
            "ssh",
            _env_value(runtime_env, "TERMINAL_SSH_HOST"),
            _env_value(runtime_env, "TERMINAL_SSH_USER"),
            _env_value(runtime_env, "TERMINAL_SSH_PORT", _AGENT_DEFAULT_SSH_PORT),
            _env_value(runtime_env, "TERMINAL_SSH_KEY"),
            _env_flag(
                runtime_env,
                "TERMINAL_SSH_PERSISTENT",
                _env_value(runtime_env, "TERMINAL_PERSISTENT_SHELL", "true"),
            ),
        )
    if env_type == "docker":
        return (
            "docker",
            _env_value(runtime_env, "TERMINAL_DOCKER_IMAGE", _AGENT_DEFAULT_IMAGE),
            _env_value(
                runtime_env, "TERMINAL_CONTAINER_CPU", _AGENT_DEFAULT_CONTAINER_CPU
            ),
            _env_value(
                runtime_env,
                "TERMINAL_CONTAINER_MEMORY",
                _AGENT_DEFAULT_CONTAINER_MEMORY,
            ),
            _env_value(
                runtime_env, "TERMINAL_CONTAINER_DISK", _AGENT_DEFAULT_CONTAINER_DISK
            ),
            # TERMINAL_DOCKER_FORWARD_ENV is represented by the profile suffix's
            # forwarded-secret fingerprint (#5988 round 5) — order-independent
            # over the name SET and value-complete — so it is intentionally not
            # a separate backend-tuple field (which would over-split on mere
            # list-order differences).
            _env_json(runtime_env, "TERMINAL_DOCKER_VOLUMES", "[]"),
            _env_json(runtime_env, "TERMINAL_DOCKER_ENV", "{}"),
            _env_json(runtime_env, "TERMINAL_DOCKER_EXTRA_ARGS", "[]"),
            _env_flag(runtime_env, "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false"),
            _env_flag(runtime_env, "TERMINAL_DOCKER_RUN_AS_HOST_USER", "false"),
            _env_flag(runtime_env, "TERMINAL_DOCKER_NETWORK", "true"),
            _env_flag(runtime_env, "TERMINAL_CONTAINER_PERSISTENT", "true"),
            _env_flag(
                runtime_env, "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES", "true"
            ),
        )
    if env_type == "modal":
        return (
            "modal",
            _env_value(runtime_env, "TERMINAL_MODAL_IMAGE", _AGENT_DEFAULT_IMAGE),
            _env_value(
                runtime_env, "TERMINAL_MODAL_MODE", _AGENT_DEFAULT_MODAL_MODE
            ).lower(),
            _env_value(
                runtime_env, "TERMINAL_CONTAINER_CPU", _AGENT_DEFAULT_CONTAINER_CPU
            ),
            _env_value(
                runtime_env,
                "TERMINAL_CONTAINER_MEMORY",
                _AGENT_DEFAULT_CONTAINER_MEMORY,
            ),
            _env_value(
                runtime_env, "TERMINAL_CONTAINER_DISK", _AGENT_DEFAULT_CONTAINER_DISK
            ),
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
    if env_type == "local":
        return (
            "local",
            _env_flag(runtime_env, "TERMINAL_LOCAL_PERSISTENT", "false"),
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


# docker/podman `inspect` prints this (modulo case) when — and only when —
# the daemon answered and the object is definitively absent. Any other
# nonzero outcome (daemon unreachable, permission denied, TLS error, ...)
# proves nothing about the container.
_PROBE_ABSENT_MARKER = "no such object"


def _container_exists(container_id: str, docker_exe: str) -> bool:
    """Probe whether *container_id* still exists (running or stopped).

    Tri-state, fail closed (#5988 round 4): exit 0 means the container
    exists; a nonzero exit is treated as ABSENT only when the daemon
    positively reported "no such object". Every other failure (daemon
    unreachable, missing binary, timeout, permission error) RAISES so the
    caller treats "cannot verify" as "not verified" — a dead Docker daemon
    must never read as "container removed".
    """
    proc = subprocess.run(
        [docker_exe or "docker", "inspect", "--format", "{{.Id}}", container_id],
        capture_output=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return True
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    if _PROBE_ABSENT_MARKER in stderr.lower():
        return False
    raise RuntimeError(
        "docker inspect probe for container %s failed without a definitive "
        "absence report (exit %s): %s"
        % (container_id[:12], proc.returncode, stderr.strip()[:300] or "<no stderr>")
    )


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


def _invalidate_default_env(
    previous, identity, cleanup_vm, get_active_env
) -> tuple[bool, bool]:
    """Verifiably invalidate the agent ``"default"`` slot for previous->identity.

    Returns ``(committed, invalidated)``. ``committed`` is True only when the
    postconditions are OBSERVED (slot gone; any outgoing containers gone) —
    ``cleanup_vm`` returning is never treated as success by itself: the real
    agent contract swallows backend-teardown exceptions and returns None, and
    Docker force-removal runs asynchronously. ``invalidated`` reports whether
    an actual invalidation happened (False for a first-use commit that PROVED
    the slot empty and had nothing to clean).

    ``previous is None`` is the first-use case (#5988 round 4): the slot's
    state is unproven — tool-capable code outside the lease discipline (or a
    prior failed first-use attempt) may already have populated it. First use
    therefore commits only after OBSERVING the slot absent, or after a full
    verified cleanup when it is populated.
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
                return True, previous is not None
            # Partial injection with an absent runtime is ambiguous — the
            # caller believes an env layer exists that we cannot verify.
            return False, False
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
            return False, False

    # Observe the outgoing env BEFORE cleanup: cleanup_vm pops the slot
    # unconditionally, so this is the last moment its container id is
    # reachable. If anything below fails, the ledger survives into the retry.
    try:
        outgoing_env = get_active_env("default")
    except Exception:
        logger.warning(
            "get_active_env('default') failed pre-cleanup (#5937)", exc_info=True
        )
        return False, False

    if previous is None and outgoing_env is None:
        # First use with the slot OBSERVED absent. Still require the pending
        # ledger empty: a prior failed first-use attempt may have popped the
        # slot but left its container unverified — committing here would
        # forget that leak.
        if not _verify_pending_container_removals():
            logger.warning(
                "first-use commit blocked: ledgered container removals remain "
                "unverified (#5937) identity=%s",
                identity,
            )
            return False, False
        logger.info(
            "first terminal backend identity %s committed after observing the "
            "default env slot absent (#5937)",
            identity,
        )
        return True, False

    # Force-removal is required when the OUTGOING env is a reattachable
    # container. The previous identity type says so for known transitions;
    # for first-use (previous unknown) the env object itself is the evidence.
    container_id = (
        getattr(outgoing_env, "_container_id", None)
        if outgoing_env is not None
        else None
    )
    force_remove = bool(container_id) or (
        previous is not None and previous[0] in _FORCE_REMOVE_ENV_TYPES
    )
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
        return False, False

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
            return False, False
    except Exception:
        logger.warning(
            "get_active_env('default') failed post-cleanup (#5937)", exc_info=True
        )
        return False, False

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
        return False, False

    logger.info(
        "Invalidated agent default terminal env after backend identity change "
        "(#5937) previous=%s current=%s force_remove=%s (slot + container "
        "removal verified)",
        previous,
        identity,
        force_remove,
    )
    return True, True


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

    - First turn in the process VERIFIES the slot state before committing its
      identity (#5988 round 4): commit-without-cleanup only after observing
      the agent ``"default"`` slot absent; a populated slot (created by code
      outside the lease discipline, or surviving a prior failed attempt) gets
      the full verified cleanup first.
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
                if previous is not None and previous == identity:
                    _active_turn_counts[identity] = (
                        _active_turn_counts.get(identity, 0) + 1
                    )
                    return TerminalBackendTurnLease(identity), False
                # First use (previous None: slot state unproven, verify it
                # outside the lock) or differing identity: all active turns
                # necessarily run on the committed identity, so becoming
                # leader requires full drain.
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

    committed = False
    invalidated = False
    try:
        try:
            committed, invalidated = _invalidate_default_env(
                previous, identity, cleanup_vm, get_active_env
            )
        except Exception:
            # _invalidate_default_env handles its own expected failures; an
            # escape here is a bug, but the boundary still fails CLOSED.
            logger.warning(
                "unexpected error during terminal backend invalidation (#5937)",
                exc_info=True,
            )
            committed = False
            invalidated = False
    finally:
        with _COND:
            _transition_identity = None
            if committed:
                _last_backend_identity = identity
                _active_turn_counts[identity] = (
                    _active_turn_counts.get(identity, 0) + 1
                )
            _COND.notify_all()

    if not committed:
        raise TerminalBackendInvalidationFailed(
            "The previous terminal backend environment could not be verifiably "
            "removed; this turn was not started to avoid running tools against "
            "a stale backend. It will be retried on the next turn — see server "
            "logs for the cleanup failure. (#5937)"
        )
    return TerminalBackendTurnLease(identity), invalidated


def acquire_turn_lease_failclosed(
    runtime_env: Mapping[str, str],
    *,
    cleanup_vm=None,
    get_active_env=None,
    wait_seconds: Optional[float] = None,
) -> tuple[TerminalBackendTurnLease, bool]:
    """Acquire the turn lease, converting ANY unexpected internal error into a
    typed fail-closed rejection (#5988 round 4).

    This is the entry point every in-process tool-capable turn must use. A
    cross-profile security boundary must fail closed even against bugs in the
    boundary itself: if acquisition breaks in an unforeseen way, the turn is
    REJECTED (``TerminalBackendIsolationError`` propagates and blocks agent
    construction + tool execution) rather than proceeding without a lease
    against an unverified slot. The deliberate trade: an isolation-module bug
    now aborts backend-switching turns loudly instead of silently running
    them open.
    """
    try:
        return acquire_terminal_backend_turn_lease(
            runtime_env,
            cleanup_vm=cleanup_vm,
            get_active_env=get_active_env,
            wait_seconds=wait_seconds,
        )
    except TerminalBackendIsolationError:
        raise
    except Exception as exc:
        logger.error(
            "terminal backend isolation failed unexpectedly (#5937); failing "
            "closed — this turn will NOT run without a verified backend lease",
            exc_info=True,
        )
        raise TerminalBackendIsolationError(
            "Terminal backend isolation failed unexpectedly; this turn was not "
            "started to avoid running tools against an unverified backend. "
            "Please retry; see server logs for the underlying error. (#5937)"
        ) from exc


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
