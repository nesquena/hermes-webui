"""Profile-scoped construction of streaming agents (#8517).

The Agent binds its external memory provider to whatever
``hermes_constants.get_hermes_home()`` answers while ``AIAgent.__init__`` runs
(``agent/agent_init._memory_provider_init_kwargs``). Without a context-local
override that answer is the process-global ``HERMES_HOME`` env mirror, which
other WebUI workers rewrite for their own profile
(``api.profiles.profile_env_for_background_worker``, the cron profile contexts,
a process-wide profile switch). A turn built inside that window binds another
profile's memory bank, and the agent is then cached for every later turn.

``construct_profile_scoped_agent`` closes that window for the streaming path:

* it sets the context-local home override for the session's own profile home
  around the constructor, explicitly, and checks that ``get_hermes_home()``
  then answers that home;
* it fails closed: when the scope cannot be set, the agent is built with memory
  off for that turn (``skip_memory=True``), a WARNING is logged and the caller
  does not cache it, so the next turn tries again with memory. An Agent with no
  context-local override at all (< v0.18.0) never gets memory in this process, so
  that memoryless agent is cached as before;
* after construction it reads the home the memory manager says it bound
  (``_memory_manager._bound_scope``); on a mismatch it rebuilds the agent
  without memory, warns, and the caller does not cache it.

Every Agent attribute is read defensively. Agents from before the per-profile
memory scope (Agent commit 419e6627, 2026-10-17) have no ``_bound_scope``; their
binding cannot be read back and is trusted to the override (logged once).
Agents older than v0.18.0 have no context-local override at all; see
``_enter_profile_home``.

``install_shared_provider_instance_guard`` (#8517 P4) covers what a correct
binding cannot: a provider that hands every agent ONE process-wide instance
(mnemosyne-hermes 0.7.1). Each construction re-initialises that instance for its
own profile, so another profile's cached agent then reads and writes this
profile's bank. The guard makes the first profile home that initialises such an
instance its only owner for the life of the process; every other profile is
refused BEFORE ``initialize`` runs and builds without that provider.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import importlib
import inspect
import logging
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_logged_once: set[str] = set()
_logged_once_lock = threading.Lock()


def _log_once(key: str, message: str, *args: Any) -> None:
    with _logged_once_lock:
        if key in _logged_once:
            return
        _logged_once.add(key)
    logger.warning(message, *args)


# #8613: every provider instance the #8517 P4 guard refused to the construction
# in progress because a teardown of that instance is running. The Agent's own
# initialiser drops a refused provider, so once ``agent_cls(...)`` has returned
# there is no other trace of the refusal — the agent simply has no provider —
# and the build would be cached as a memory-bearing agent that has none.
# ``construct_profile_scoped_agent`` scopes this to one construction.
_lifetime_refusals: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "webui_8613_lifetime_refusals", default=()
)


def _hermes_constants():
    """The Agent's ``hermes_constants``, resolved on every call: a negative answer
    cached from an earlier failed import must never switch the scope off."""
    mod = sys.modules.get("hermes_constants")
    if mod is not None:
        return mod
    try:
        import hermes_constants as mod  # noqa: PLC0415
    except Exception:
        return None
    return mod


def profile_home_key(home: Any) -> Optional[str]:
    """Comparable key for a Hermes home: the Agent's ``hermes_home_key`` (the function
    that produced ``_bound_scope["home_key"]``) when available, else the resolved path."""
    raw = str(home or "").strip()
    if not raw:
        return None
    mod = _hermes_constants()
    key_fn = getattr(mod, "hermes_home_key", None) if mod is not None else None
    if callable(key_fn):
        try:
            return str(key_fn(raw))
        except Exception:
            logger.debug("hermes_home_key(%r) failed; using the resolved path", raw, exc_info=True)
    try:
        return os.path.normcase(str(Path(raw).expanduser().resolve(strict=False)))
    except Exception:
        return None


def session_profile_home_or_empty(session: Any) -> str:
    """The session's own profile home, never the shared ``HERMES_HOME`` env mirror.
    ``""`` when it cannot be resolved; memory then fails closed for the turn."""
    try:
        from api.profiles import get_hermes_home_for_profile  # noqa: PLC0415

        home = get_hermes_home_for_profile(getattr(session, "profile", None))
        return str(home) if home else ""
    except Exception:
        logger.warning(
            "[webui] #8517: could not resolve the profile home for session %s; "
            "its memory provider is disabled for this turn",
            getattr(session, "session_id", "?"),
            exc_info=True,
        )
        return ""


# Every profile home an old-Agent construction was asked for in this process
# (see ``serves_single_profile``); never shrinks.
_profile_homes_seen: set[str] = set()
_profile_homes_seen_lock = threading.Lock()


def serves_single_profile(target_key: Optional[str], *, record: bool = True) -> bool:
    """True only when this WebUI process provably serves ONE profile home.

    ``record=False`` only asks (the memory status a new build would get): the home is
    not counted as served.

    Used where per-profile memory isolation cannot be enforced (an Agent without
    ``claim_instance``): external memory stays on only when no other profile can
    run in this process. (Agents without the context-local home override get no
    memory at all, see ``_legacy_agent_reason``.) Conservative by design; any doubt
    answers False (memory off), never True:

    * ``target_key`` must be the home WebUI's own profile machinery gives the root
      profile (``api.profiles.get_hermes_home_for_profile(None)``; in isolated
      profile mode that is the pinned profile, every lookup clamps to it);
    * that home has no named profile directory (``<root>/profiles/<dir>``); a root
      session counts as multi-profile as soon as one exists, even an unused one;
    * no other home was ever requested here (a session whose profile directory
      does not exist still counts once it was served).
    """
    if not target_key:
        return False
    with _profile_homes_seen_lock:
        if record:
            _profile_homes_seen.add(target_key)
        if len(_profile_homes_seen | {target_key}) > 1:
            return False
    try:
        from api.profiles import get_hermes_home_for_profile  # noqa: PLC0415

        root = get_hermes_home_for_profile(None)
    except Exception:
        logger.debug("[webui] #8517: root profile home unavailable", exc_info=True)
        return False
    if not root or profile_home_key(root) != target_key:
        return False
    try:
        for child in (Path(str(root)) / "profiles").iterdir():
            if child.is_dir():
                return False
    except (FileNotFoundError, NotADirectoryError):
        return True
    except Exception:
        logger.debug("[webui] #8517: cannot list named profiles", exc_info=True)
        return False
    return True


def _noop() -> None:
    return None


_LEGACY_AGENT_REASON = "this Hermes Agent has no context-local home override (older than v0.18.0)"


def _legacy_agent_reason() -> str:
    """Memory is off on a Hermes Agent < v0.18.0 (#8517 all-r2 finding 1).

    Such an Agent has no context-local home: its external provider binds, and its
    built-in MEMORY.md / USER.md store reads and writes, whatever the process-wide
    ``HERMES_HOME`` env mirror names at that instant, for the whole life of the
    agent. Every WebUI worker for another profile rewrites that mirror. A "only one
    profile exists" check at construction cannot be fenced against a profile that is
    created and starts running later (between the check and the bind, during the
    turn, or after the agent was cached), so it is not an admission either."""
    _log_once(
        "legacy-agent",
        "[webui] #8517: this Hermes Agent has no context-local home override (older than "
        "v0.18.0), so an agent cannot be held to its own profile's memory. Memory "
        "(external providers and the built-in MEMORY.md/USER.md) is turned off for every "
        "agent. Upgrade the Agent to restore it.",
    )
    return _LEGACY_AGENT_REASON


def turn_memory_reason(profile_home: str, *, override_installed: bool) -> str:
    """``""`` when the caller's whole turn verifiably runs in ``profile_home``, else
    why memory must be off for this turn (#8517 all-r2 finding 2).

    The caller has installed (``override_installed``) the context-local home override
    for the turn and holds it until the turn ends; this checks that the override
    took effect: ``get_hermes_home()`` answers ``profile_home`` now, in the turn's
    own context. A construction-time scope alone is not enough: the built-in store
    resolves the home on every write and the Agent's providers check the caller's
    home on every hook, all during ``run_conversation``.
    """
    target_key = profile_home_key(profile_home)
    if not target_key:
        return "the session's profile home could not be resolved"
    mod = _hermes_constants()
    setter = getattr(mod, "set_hermes_home_override", None) if mod is not None else None
    resetter = getattr(mod, "reset_hermes_home_override", None) if mod is not None else None
    if not (callable(setter) and callable(resetter)):
        return _legacy_agent_reason()
    if not override_installed:
        return "the turn-level profile home override could not be set"
    getter = getattr(mod, "get_hermes_home", None)
    if not callable(getter):
        return "the turn-level profile home cannot be verified"
    try:
        effective = profile_home_key(getter())
    except Exception:
        effective = None
    if effective != target_key:
        return "the turn-level profile home override did not take effect"
    return ""


def agent_has_memory(agent: Any) -> bool:
    """True when ``agent`` holds an external memory manager or the built-in store."""
    return (
        getattr(agent, "_memory_manager", None) is not None
        or getattr(agent, "_memory_store", None) is not None
    )


# ── what the user is told (#8517 follow-up) ──────────────────────────────────
#
# The reasons above are logged for the operator; the user only sees that memory does
# not work. The status is server-authoritative and describes the agent actually
# serving a session: ``construct_profile_scoped_agent`` records on every agent it
# builds for the life of a session (cacheable) either a lasting reason or healthy;
# a one-turn fail-closed build records nothing (it is never cached, so it never
# describes the session). ``session_memory_status`` reports the session's cached
# agent's record, else what a new build would provably get, else nothing (unknown).
# The UI renders the notice from the session payload's ``memory_status`` and from the
# per-turn stream event (``memory_status_event``). Codes only, never paths:
#
# * ``agent_outdated``: Agent < v0.18.0, every memory store off for every profile;
# * ``agent_no_instance_claim``: Agent without ``claim_instance`` serving several
#   profiles, external memory providers off (built-in memory still on);
# * ``provider_shared_instance``: the provider handed out one shared instance that
#   another profile already owns, so it is off for this profile until the WebUI
#   restarts (built-in memory still on).

MEMORY_OFF_AGENT_OUTDATED = "agent_outdated"
MEMORY_OFF_AGENT_NO_INSTANCE_CLAIM = "agent_no_instance_claim"
MEMORY_OFF_PROVIDER_SHARED_INSTANCE = "provider_shared_instance"
_MEMORY_OFF_SCOPES = {
    MEMORY_OFF_AGENT_OUTDATED: "all",
    MEMORY_OFF_AGENT_NO_INSTANCE_CLAIM: "external",
    MEMORY_OFF_PROVIDER_SHARED_INSTANCE: "external",
}
# English text for clients that predate the notice: their ``warning`` listener shows
# ``message`` as a composer status. The UI proper renders its own i18n copy by code.
_MEMORY_OFF_MESSAGES = {
    MEMORY_OFF_AGENT_OUTDATED: (
        "Memory is off: this version of Hermes Agent cannot keep memory separate per "
        "profile. Update Hermes Agent to turn it back on."
    ),
    MEMORY_OFF_AGENT_NO_INSTANCE_CLAIM: (
        "Memory providers are off: this version of Hermes Agent cannot keep them separate "
        "across profiles. Built-in memory still works. Update Hermes Agent to turn them back on."
    ),
    MEMORY_OFF_PROVIDER_SHARED_INSTANCE: (
        "Your memory provider is off for this profile because another profile is already "
        "using it. Built-in memory still works. Restart WebUI, or update the provider "
        "(for example mnemosyne-hermes 0.7.2 or later)."
    ),
}
_MEMORY_OK = "ok"
_MEMORY_STATUS_ATTR = "_webui_memory_status"
# Shared-instance refusals made by the guard during one construction (same thread:
# the Agent initialises its memory providers inside ``AIAgent.__init__``).
_construction_refusals: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "webui_issue8517_construction_refusals", default=None,
)


def _set_memory_status(agent: Any, code: str) -> None:
    try:
        setattr(agent, _MEMORY_STATUS_ATTR, code)
    except Exception:
        logger.debug("[webui] #8517: could not record the memory status on the agent", exc_info=True)


def _status_for_code(code: Any) -> Optional[dict]:
    if code == _MEMORY_OK:
        return {"state": "ok"}
    if isinstance(code, str) and code in _MEMORY_OFF_SCOPES:
        return {"state": "off", "reason": code, "scope": _MEMORY_OFF_SCOPES[code]}
    return None


def memory_status_for_agent(agent: Any) -> Optional[dict]:
    """The status ``construct_profile_scoped_agent`` recorded on ``agent``:
    ``{"state": "off", "reason", "scope"}``, ``{"state": "ok"}``, or None (one-turn
    fail-closed build, or any object this module did not build)."""
    try:
        code = vars(agent).get(_MEMORY_STATUS_ATTR) if agent is not None else None
    except TypeError:  # no instance __dict__ (slots): never built here
        return None
    return _status_for_code(code)


def _claim_seam_available() -> Optional[bool]:
    """Whether the Agent has ``agent.memory_manager.claim_instance`` (None: unknown).

    Never imports the Agent runtime from a request thread before it is bound: the first
    cache-miss GET /api/session after boot would otherwise pull in ~84 modules (including
    the tools registry singleton). Until then the answer is unknown; the first turn reports
    the real status."""
    mm = sys.modules.get("agent.memory_manager")
    if mm is None:
        if "run_agent" not in sys.modules:
            return None
        try:
            mm = importlib.import_module("agent.memory_manager")
        except Exception:
            return None
    return callable(getattr(mm, "claim_instance", None))


def predicted_memory_status(profile_home: str) -> Optional[dict]:
    """What a new agent for ``profile_home`` would provably get, without building it
    and without counting the home as served: ``agent_outdated``, or
    ``agent_no_instance_claim`` when several profiles exist. Anything else (healthy, a
    shared-instance refusal, a one-turn failure) is only known by building: None."""
    target_key = profile_home_key(profile_home)
    if not target_key:
        return None
    mod = _hermes_constants()
    if mod is None:
        return None
    setter = getattr(mod, "set_hermes_home_override", None)
    resetter = getattr(mod, "reset_hermes_home_override", None)
    if not (callable(setter) and callable(resetter)):
        return _status_for_code(MEMORY_OFF_AGENT_OUTDATED)
    if _claim_seam_available() is False and not serves_single_profile(target_key, record=False):
        return _status_for_code(MEMORY_OFF_AGENT_NO_INSTANCE_CLAIM)
    return None


def session_memory_status(session_id: str, profile_home: Optional[str] = None, *, session: Any = None) -> Optional[dict]:
    """The memory status of the agent serving ``session_id``: its cached agent's
    record when one is cached (a root agent cached before a second profile appeared
    keeps external memory on, and says so), else ``predicted_memory_status`` for the
    session's profile home (``profile_home``, or resolved from ``session`` only when
    needed), else None. Turn-level failures never change it: their agents are never
    cached and never recorded."""
    sid = str(session_id or "")
    if not sid:
        return None
    try:
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK  # noqa: PLC0415

        with SESSION_AGENT_CACHE_LOCK:
            entry = SESSION_AGENT_CACHE.get(sid)
    except Exception:
        logger.debug("[webui] #8517: agent cache unavailable for the memory status", exc_info=True)
        return None
    if entry:
        return memory_status_for_agent(entry[0] if isinstance(entry, tuple) else entry)
    if profile_home is None:
        profile_home = session_profile_home_or_empty(session) if session is not None else ""
    return predicted_memory_status(profile_home)


def memory_status_event(status: Optional[dict], *, session_id: str, stream_id: str = "") -> Optional[tuple[str, dict]]:
    """``(event, payload)`` telling the UI a session's memory status during a turn, or
    None when unknown. Memory off is a ``warning`` of ``type: "memory_off"`` (older
    clients show its ``message``); healthy is its own ``memory_status`` event (older
    clients would show any warning as "Warning"). Codes and fixed text only."""
    if not isinstance(status, dict):
        return None
    sid = str(session_id or "")
    stream = str(stream_id or "")
    if status.get("state") == "ok":
        return "memory_status", {"type": "memory_status", "state": "ok", "session_id": sid, "stream_id": stream}
    reason = status.get("reason")
    if status.get("state") != "off" or not isinstance(reason, str) or reason not in _MEMORY_OFF_SCOPES:
        return None
    return "warning", {
        "type": "memory_off",
        "session_id": sid,
        "stream_id": stream,
        "reason": reason,
        "scope": _MEMORY_OFF_SCOPES[reason],
        "message": _MEMORY_OFF_MESSAGES[reason],
    }


def _enter_profile_home(profile_home: str, target_key: Optional[str]) -> tuple[Callable[[], None], Optional[str]]:
    """Set the context-local Hermes home to ``profile_home``.

    Returns ``(restore, reason)``: ``reason`` is None when construction may keep
    memory, else why it must not.
    """
    if not target_key:
        return _noop, "the session's profile home could not be resolved"
    mod = _hermes_constants()
    setter = getattr(mod, "set_hermes_home_override", None) if mod is not None else None
    resetter = getattr(mod, "reset_hermes_home_override", None) if mod is not None else None
    if not (callable(setter) and callable(resetter)):
        return _noop, _legacy_agent_reason()
    try:
        token = setter(str(profile_home))
    except Exception as exc:
        return _noop, f"setting the profile home override failed ({type(exc).__name__})"

    def _restore() -> None:
        try:
            resetter(token)
        except Exception:
            logger.warning("[webui] #8517: resetting the profile home override failed", exc_info=True)

    getter = getattr(mod, "get_hermes_home", None)
    if callable(getter):
        try:
            effective = profile_home_key(getter())
        except Exception:
            effective = None
        if effective != target_key:
            _restore()
            return _noop, "the profile home override did not take effect"
    return _restore, None


def _bound_memory_home_key(agent: Any) -> tuple[str, Optional[str]]:
    """``("none", None)`` without an external memory manager, ``("bound", key)`` when
    the manager reports its home, ``("unverifiable", None)`` when it cannot say."""
    manager = getattr(agent, "_memory_manager", None)
    if manager is None:
        return "none", None
    scope = getattr(manager, "_bound_scope", None)
    if isinstance(scope, dict):
        key = scope.get("home_key")
        if isinstance(key, str) and key:
            return "bound", key
        key = profile_home_key(scope.get("hermes_home"))
        if key:
            return "bound", key
    # MemoryManager's own build-time home key (Agent 2026-10-12+), when there is no scope.
    key = getattr(manager, "_home_key", None)
    if isinstance(key, str) and key:
        return "bound", key
    return "unverifiable", None


def _detach_memory(agent: Any) -> None:
    """Drop the agent's reference to its memory manager without shutting the provider
    down: a shared provider instance may still serve another profile's live agent."""
    if getattr(agent, "_memory_manager", None) is None:
        return
    # #8613: a detached agent no longer holds the instance, so it must not keep
    # another agent from being proved its last holder.
    unregister_provider_holders(agent)
    try:
        agent._memory_manager = None
    except Exception as exc:
        raise RuntimeError(
            "could not detach a memory provider bound to another profile from the agent"
        ) from exc


def _constructor_params(agent_cls: Any) -> set:
    try:
        return set(inspect.signature(agent_cls.__init__).parameters)
    except (TypeError, ValueError):
        return set()


_BUILTIN_MEMORY_TOOLSET = "memory"


def _without_builtin_memory_toolset(kwargs: dict, params: set) -> None:
    """Make the Agent build no built-in MEMORY.md / USER.md store (#8517 all-r1
    finding 1). ``skip_memory=True`` only skips EXTERNAL providers: the Agent still
    loads the built-in store when the ``memory`` toolset is requested
    (agent/agent_init._init_memory, #65429), and that store reads and writes
    ``get_hermes_home()/memories``, i.e. whatever home is ambient when it runs.

    * ``memory`` is removed from ``enabled_toolsets`` (Agents 2026-07-22..09-01 only
      check that list);
    * ``memory`` is added to ``disabled_toolsets`` when the constructor names it
      (current Agents: a disabled toolset is not a request, and the tool leaves the
      model's tool surface).
    """
    enabled = kwargs.get("enabled_toolsets")
    if isinstance(enabled, (list, tuple, set, frozenset)):
        kwargs["enabled_toolsets"] = [t for t in enabled if t != _BUILTIN_MEMORY_TOOLSET]
    if "disabled_toolsets" in params:
        disabled = kwargs.get("disabled_toolsets")
        disabled = list(disabled) if isinstance(disabled, (list, tuple, set, frozenset)) else []
        if _BUILTIN_MEMORY_TOOLSET not in disabled:
            disabled.append(_BUILTIN_MEMORY_TOOLSET)
        kwargs["disabled_toolsets"] = disabled


def _detach_builtin_memory(agent: Any) -> None:
    """Backstop for an Agent that loaded the built-in store anyway: drop it before
    any turn reads it into the system prompt or the memory tool writes through it
    (both look the store up per call and treat ``None`` as memory off)."""
    if getattr(agent, "_memory_store", None) is None:
        return
    try:
        agent._memory_store = None
        agent._memory_enabled = False
        agent._user_profile_enabled = False
    except Exception as exc:
        raise RuntimeError("could not drop the built-in memory store from the agent") from exc


def _build_without_memory(agent_cls: Any, agent_kwargs: dict, params: set, *, keep_builtin: bool = False) -> Any:
    """Build with external memory providers off; unless ``keep_builtin``, the
    built-in MEMORY.md / USER.md store is off too. ``keep_builtin`` is only for
    callers whose profile home is verifiably held for construction AND the turn."""
    kwargs = dict(agent_kwargs)
    if "skip_memory" in params:
        kwargs["skip_memory"] = True
    if not keep_builtin:
        _without_builtin_memory_toolset(kwargs, params)
    agent = agent_cls(**kwargs)
    _detach_memory(agent)
    if not keep_builtin:
        _detach_builtin_memory(agent)
    return agent


def construct_memoryless_helper_agent(agent_cls: Any, agent_kwargs: dict, *, purpose: str = "") -> Any:
    """Build a one-shot helper agent (manual compression, summaries, commit
    messages) that never touches memory (#8517 P2).

    With memory on, ``AIAgent.__init__`` initialises the memory provider for
    whatever home the request thread's ambient state names. A provider that hands
    every agent one shared instance is then re-pointed at that profile, and the
    owning session's next cached turn writes into the other profile's bank. These
    helpers only need the model, so memory is skipped outright.

    ``skip_memory`` is passed only when the constructor names it (Agent
    2026-02-19+; external memory providers arrived later, 2026-04-02, so an
    Agent without the parameter has none). Any manager is still detached. The
    caller's ``agent_kwargs`` is never mutated. The built-in MEMORY.md / USER.md
    store is off as well (the request thread's ambient home is not the owner's).
    """
    agent = _build_without_memory(agent_cls, agent_kwargs, _constructor_params(agent_cls))
    logger.debug("[webui] built memoryless helper agent for %s", purpose or "helper")
    return agent


def construct_profile_scoped_agent(
    agent_cls: Any,
    agent_kwargs: dict,
    *,
    profile_home: str,
    agent_params: Optional[set] = None,
    session_id: str = "",
    fail_closed_reason: str = "",
) -> tuple[Any, bool]:
    """Build ``agent_cls(**agent_kwargs)`` for the session whose profile lives at
    ``profile_home`` so its memory provider can only bind that profile.

    Returns ``(agent, cacheable)``. ``cacheable`` is False when memory was turned
    off or dropped for this turn only; the caller must not put that agent in
    SESSION_AGENT_CACHE, so the next turn builds again with memory. Two reasons hold
    for the life of the process instead, so their agents are cacheable, as every
    agent was before #8517: an Agent without the context-local override
    (< v0.18.0; all memory off), and an Agent without ``claim_instance`` serving
    several profiles (external memory off; the Agent cannot gain the seam and served
    homes only accumulate, so no later turn could build it with external memory).
    ``agent_kwargs`` is never mutated. A non-empty ``fail_closed_reason`` (the
    caller could not hold the profile home for the turn) builds without memory.

    Fail-closed builds turn off the external providers AND the built-in
    MEMORY.md / USER.md store, except when only the shared-instance guard is
    missing (old Agent, several profiles): then the profile home is verifiably
    held, so the built-in store keeps the Agent's own ``skip_memory`` semantics.

    A memory-bearing build runs under an acquisition reservation (#8613): it is
    taken before ``agent_cls(...)`` initialises the providers and held until the
    holder is registered or the build aborts, so a session boundary cannot prove
    this turn's agent absent while it is acquiring the shared instance. If the
    build still ends up on an instance a teardown is closing, it fails closed
    (memory off, not cached) instead of handing out an agent whose next save would
    go through a closed client. The same holds when the #8517 P4 guard refused the
    instance to it mid-teardown: the Agent then drops the provider itself and
    keeps built-in memory, which leaves no other trace, so the refusal is noted
    construction-scoped (``_lifetime_refusals``) and the build is not cached
    either — a cached memoryless agent would never be given the provider again.
    """
    guarded = install_shared_provider_instance_guard()
    params = _constructor_params(agent_cls) if agent_params is None else set(agent_params)
    target_key = profile_home_key(profile_home)
    restore, reason = _enter_profile_home(profile_home, target_key)
    # Reasons that hold for the life of the process (cacheable) versus reasons about
    # this turn only (not cacheable, so the next turn retries with memory).
    stable_for_process = reason == _LEGACY_AGENT_REASON
    try:
        if reason is None and fail_closed_reason:
            reason = fail_closed_reason
        keep_builtin = False
        if reason is None and not guarded and not serves_single_profile(target_key):
            # #8517 all-r1 finding 2: an Agent without ``claim_instance`` initialises a
            # shared provider instance for every profile that builds an agent, so the
            # first profile's live agents would follow the last one's bank. Without the
            # guard, no external provider is initialised in multi-profile operation.
            _log_once(
                "no-claim-multi",
                "[webui] #8517: this Hermes Agent cannot keep a memory provider instance with "
                "one profile (no claim_instance; older than 2026-10-17) and this WebUI serves "
                "several profiles, so external memory providers are turned off for every "
                "agent (built-in memory still works). Upgrade the Agent to restore them.",
            )
            reason = "external memory providers are off: Agent without claim_instance and several profiles"
            keep_builtin = True
            stable_for_process = True
        if reason is not None:
            logger.warning(
                "[webui] #8517: memory provider disabled for session %s this turn: %s "
                "(profile home %r). The agent is built without memory%s.",
                session_id, reason, profile_home,
                "" if stable_for_process else " and not cached",
            )
            agent = _build_without_memory(agent_cls, agent_kwargs, params, keep_builtin=keep_builtin)
            if stable_for_process:
                _set_memory_status(
                    agent,
                    MEMORY_OFF_AGENT_NO_INSTANCE_CLAIM if keep_builtin else MEMORY_OFF_AGENT_OUTDATED,
                )
            return agent, stable_for_process
        # #8613: reserve the acquisition BEFORE the provider is initialised. The
        # Agent initialises its native manager (and claims the shared instance)
        # inside ``agent_cls(...)``; only once it has returned can the holder be
        # recorded. The reservation is held across that whole window — through the
        # registration below and through an aborted build alike — so the
        # last-holder proof can never prove the old agent alone while this
        # construction is acquiring the same instance.
        if not _reserve_acquisition(target_key or ""):
            logger.warning(
                "[webui] #8804: a teardown of the memory provider of profile home %r is still "
                "running, and this Hermes Agent cannot keep a new agent off the instance it "
                "closes (no claim_instance; older than 2026-10-17). The agent built for "
                "session %s runs without memory this turn and is not cached; the next turn "
                "constructs it again.",
                profile_home, session_id,
            )
            return _build_without_memory(agent_cls, agent_kwargs, params), False
        try:
            # #8517 (master): the shared-instance guard refuses a provider another
            # profile already owns; the Agent drops it and keeps the rest. Recorded
            # construction-scoped so the memory status can name that reason.
            refusals: list = []
            refusals_token = _construction_refusals.set(refusals)
            # #8613: construction-scoped note of every provider instance the #8517 P4
            # guard refused because a teardown of it is running (must-fix 2). The
            # Agent's own initialiser drops a refused provider, so without this the
            # refusal is unreadable once the constructor has returned: the agent
            # simply has no provider, and the build would answer cacheable.
            _refusal_token = _lifetime_refusals.set(())
            try:
                agent = agent_cls(**agent_kwargs)
                refused = _lifetime_refusals.get()
            finally:
                _lifetime_refusals.reset(_refusal_token)
                _construction_refusals.reset(refusals_token)
            state, bound_key = _bound_memory_home_key(agent)
            if state == "bound" and bound_key != target_key:
                logger.warning(
                    "[webui] #8517: memory provider for session %s bound %r instead of its "
                    "profile home %r; it is dropped for this turn and the agent is not cached.",
                    session_id, bound_key, target_key,
                )
                _detach_memory(agent)
                # The Agent bound a home other than the one held for it, so its built-in
                # MEMORY.md / USER.md store cannot be trusted either (all-r1 finding 1).
                _detach_builtin_memory(agent)
                if "skip_memory" in params:
                    agent = _build_without_memory(agent_cls, agent_kwargs, params)
                return agent, False
            if state == "unverifiable":
                _log_once(
                    "unverifiable",
                    "[webui] #8517: this Hermes Agent does not report which profile home its "
                    "memory provider bound; relying on the profile home override set for "
                    "construction. Upgrade the Agent to verify it.",
                )
            # #8613 must-fix 2: a provider the guard refused mid-teardown leaves the
            # Agent on built-in memory with no other trace, so the construction must
            # answer NOT cacheable — a cache hit would reuse that memoryless agent and
            # the profile would never initialise the provider again. Whatever the agent
            # DID acquire is still recorded, so another agent's boundary cannot release
            # it under this one.
            if refused:
                logger.warning(
                    "[webui] #8613: memory provider(s) %s were refused to session %s because "
                    "a teardown of the instance is running, so the agent built for it runs "
                    "without that provider and is not cached; the next turn constructs it "
                    "again.",
                    list(refused), session_id,
                )
                _record_build_home(agent, profile_home)
                register_provider_holders(agent, home_key=target_key or "")
                return agent, False
            # #8613: fail closed if the build ended up holding an instance a teardown
            # is closing (an Agent without the ``claim_instance`` seam keeps it, since
            # the reservation only fences the table). Such an agent's next save would
            # go through a closed client, so it is rebuilt without memory and not
            # cached: the next turn constructs again, once the teardown has settled.
            dying, dying_name = agent_holds_dying_instance(agent)
            if dying:
                logger.warning(
                    "[webui] #8613: memory provider %r for session %s is being torn down, "
                    "so the agent built for it is dropped for this turn and not cached; "
                    "the next turn constructs it again.",
                    dying_name, session_id,
                )
                _detach_memory(agent)
                _detach_builtin_memory(agent)
                if "skip_memory" in params:
                    agent = _build_without_memory(agent_cls, agent_kwargs, params)
                return agent, False
            # #8613: this agent now holds every provider instance its manager serves, so a
            # later session boundary can prove whether it is the last holder of them.
            _record_build_home(agent, profile_home)
            register_provider_holders(agent, home_key=target_key or "")
            # #8517 (master): the guard refused a shared provider instance another
            # profile owns; the Agent dropped it and kept the rest. Holds until the
            # WebUI restarts.
            _set_memory_status(agent, MEMORY_OFF_PROVIDER_SHARED_INSTANCE if refusals else _MEMORY_OK)
            return agent, True
        finally:
            # Registration happened (or the build aborted): either way this
            # construction no longer acquires anything.
            _end_acquisition(target_key or "")
    finally:
        restore()


@contextlib.contextmanager
def profile_home_turn_scope(profile_home: str):
    """Hold the context-local Hermes home on ``profile_home`` for a WHOLE turn:
    construction, ``run_conversation`` and every memory hook it runs (#8517 all-r1
    finding 4). The Agent's providers check the caller's home on every call, and the
    built-in MEMORY.md / USER.md store resolves ``get_hermes_home()`` on every write,
    so a turn that runs after the construction scope is restored meets whatever home
    the shared env mirror names (another profile after a UI switch).

    Yields ``""`` when the home is held (or, on an Agent < v0.18.0, when memory may
    run on it at all), else the reason it is not; the caller then builds the agent
    without memory (``construct_profile_scoped_agent(fail_closed_reason=...)``).
    Restored on every exit.
    """
    restore, reason = _enter_profile_home(profile_home, profile_home_key(profile_home))
    try:
        yield reason or ""
    finally:
        restore()


# ── #8517 P3: session-boundary commits run as the session's owner ─────────────

# The Agent's caller-scope module and the logger it refuses on (its ``_refuse``).
_CALLER_SCOPE_MODULE = "agent.memory_caller_scope"
_CALLER_SCOPE_LOGGER = "agent.memory_caller_scope"


def session_owner_profile_home(session_id: str) -> str:
    """The profile home of the session ``session_id`` belongs to, looked up from the
    session store; ``""`` when the session or its profile cannot be resolved."""
    if not session_id:
        return ""
    try:
        from api.models import get_session_profile_readonly  # noqa: PLC0415
        from api.profiles import get_hermes_home_for_profile  # noqa: PLC0415

        # Read-only ownership lookup: no sidecar repair, no cache publication.
        home = get_hermes_home_for_profile(get_session_profile_readonly(session_id))
        return str(home) if home else ""
    except Exception:
        logger.debug("[webui] #8517: no owner profile for session %s", session_id, exc_info=True)
        return ""


class _HookRefusalCapture(logging.Handler):
    """Notices the Agent refusing ``hook`` during one ``call_as_session_owner``. The
    Agent's lifecycle hooks return nothing when its caller-scope check refuses them;
    the only signal is its refusal, ``agent/memory_caller_scope._refuse``.

    With that function present the capture is told by ``_install_refusal_hook``'s
    wrapper (``note_refusal``), whatever the deployer's logging configuration
    (#8613). Without it (an Agent with no caller-scope check, which never refuses)
    the capture is the pre-#8613 handler on the refusal logger, for this thread."""

    def __init__(self, hook: str):
        super().__init__(logging.WARNING)
        self._needle = f"hook {hook}"
        self._thread = threading.get_ident()
        self.refused = False

    def note_refusal(self, label: Any) -> None:
        if isinstance(label, str) and self._needle in label:
            self.refused = True

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread:
            return
        try:
            message = record.getMessage()
        except Exception:
            return
        if "refused" in message and self._needle in message:
            self.refused = True


# The captures open in THIS context (thread / task), innermost last. A tuple, replaced
# and reset, never mutated: a concurrent call on another thread has its own value.
_refusal_captures: contextvars.ContextVar[tuple[_HookRefusalCapture, ...]] = contextvars.ContextVar(
    "webui_memory_hook_refusal_captures", default=())
_refuse_wrap_lock = threading.Lock()
# Set on the wrapper to the registry it notes into: a reload of this module (a new
# registry) wraps again; the previous wrapper then notes into nothing and delegates.
_REFUSE_WRAPPER_MARK = "_webui_8613_refusal_captures"


def _wrap_refuse(original: Callable[..., Any], captures: contextvars.ContextVar) -> Callable[..., Any]:
    def _refuse(*args: Any, **kwargs: Any) -> Any:
        try:
            active = captures.get()
            if active:
                label = args[0] if args else kwargs.get("label")
                for capture in active:
                    capture.note_refusal(label)
        except Exception:  # noting must never change what the Agent's refusal does
            pass
        return original(*args, **kwargs)

    functools.update_wrapper(_refuse, original, updated=())
    setattr(_refuse, _REFUSE_WRAPPER_MARK, captures)
    return _refuse


def _install_refusal_hook() -> bool:
    """Wrap the Agent's ``agent.memory_caller_scope._refuse`` once (#8613), so a
    refused hook is noted before the Agent logs it: no logger level, ``disabled``
    flag, filter or handler is touched, and what the deployer's handlers receive is
    unchanged. The wrapper delegates every call unchanged (arguments, return value,
    exceptions). The Agent's ``caller_scope`` resolves ``_refuse`` from its module
    globals at each call, so replacing the module attribute reaches every refusal.

    Idempotent and thread-safe; a reload of the Agent module (a fresh ``_refuse``)
    is wrapped again on the next call. False when the Agent has no ``_refuse``: the
    caller then uses the pre-#8613 log capture."""
    mod = sys.modules.get(_CALLER_SCOPE_MODULE)
    if mod is None:
        try:
            mod = importlib.import_module(_CALLER_SCOPE_MODULE)
        except Exception:
            return False
    captures = _refusal_captures
    current = getattr(mod, "_refuse", None)
    if getattr(current, _REFUSE_WRAPPER_MARK, None) is captures:
        return True
    with _refuse_wrap_lock:
        current = getattr(mod, "_refuse", None)
        if not callable(current):
            return False
        if getattr(current, _REFUSE_WRAPPER_MARK, None) is not captures:
            try:
                setattr(mod, "_refuse", _wrap_refuse(current, captures))  # noqa: B010 (a module attribute set at runtime)
            except Exception:
                logger.debug("[webui] #8613: cannot wrap the Agent's memory refusal", exc_info=True)
                return False
    return True


def _enter_session_owner_scope(agent: Any, session_id: str, owner_home: str) -> tuple[Callable[[], None], Optional[str]]:
    """Set the context-local home to the session owner's profile for a lifecycle
    hook on ``agent``. ``(restore, reason)``: ``reason`` set means do not run it."""
    if getattr(agent, "_memory_manager", None) is None:
        return _noop, None  # no external memory: nothing to bind, nothing to refuse
    # Trusted ownership only: the owner recorded beside the turn mark, else the
    # session store. Never the agent's own binding (a misbound agent would vouch
    # for itself) and never unscoped (#8517 all-r2 finding 3).
    owner = str(owner_home or "").strip() or session_owner_profile_home(session_id)
    owner_key = profile_home_key(owner) if owner else None
    if not owner_key:
        return _noop, (
            "the profile that owns the session could not be resolved (no recorded owner "
            "and no session record)"
        )
    state, bound_key = _bound_memory_home_key(agent)
    if state == "bound" and bound_key != owner_key:
        return _noop, (
            f"its memory provider is bound to {bound_key!r}, not the session's profile home {owner_key!r}"
        )
    # On an Agent < v0.18.0 this refuses too (no context-local home to hold).
    return _enter_profile_home(owner, owner_key)


def call_as_session_owner(
    agent: Any,
    fn: Callable[[], Any],
    *,
    session_id: str,
    owner_home: str = "",
    hook: str = "on_session_end",
    what: str = "memory commit",
) -> bool:
    """Run ``fn`` (a lifecycle call on ``agent``) under the profile of the session
    that owns it, never the caller's (#8517 P3).

    Session boundaries fire from other profiles' turns (LRU eviction), from fresh
    threads (``/api/session/new`` previous-session commit, shutdown drain) whose
    ambient home is the shared env mirror. The Agent then refuses
    ``on_session_end`` silently. Returns True only when ``fn`` ran and the Agent
    did not refuse ``hook``; on False a WARNING is logged and the caller must keep
    the work pending. Exceptions from ``fn`` propagate.
    """
    restore, reason = _enter_session_owner_scope(agent, session_id, owner_home)
    if reason is not None:
        logger.warning(
            "[webui] #8517: %s for session %s not run: %s. The work stays pending "
            "and is retried at the next session boundary.",
            what, session_id, reason,
        )
        return False
    capture = _HookRefusalCapture(hook)
    log_capture: Optional[logging.Logger] = None
    token: Optional[contextvars.Token] = None
    try:
        if not _install_refusal_hook():
            # No ``_refuse`` to wrap: the pre-#8613 capture, which only sees the
            # refusal when the deployer's logging creates the record.
            log_capture = logging.getLogger(_CALLER_SCOPE_LOGGER)
            log_capture.addHandler(capture)
        token = _refusal_captures.set(_refusal_captures.get() + (capture,))
        fn()
    finally:
        if token is not None:
            _refusal_captures.reset(token)
        if log_capture is not None:
            log_capture.removeHandler(capture)
        restore()
    if capture.refused:
        logger.warning(
            "[webui] #8517: the Agent refused the %s for session %s (%s). It is NOT "
            "counted as committed; the work stays pending and is retried at the "
            "next session boundary.",
            what, session_id, hook,
        )
        return False
    return True


# ── one shared provider instance, one profile (#8517 P4) ────────────────────
#
# The Agent's ``MemoryManager.initialize_all`` calls
# ``claim_instance(provider, hermes_home)`` (agent/memory_caller_scope.py, looked up
# through the ``agent.memory_manager`` module global) for every external provider,
# drops the provider when the claim is refused, and only then calls
# ``provider.initialize(hermes_home=...)``. The Agent refuses a second profile only on
# a multiplexed gateway; WebUI serves several profiles in one process without that
# flag (turning it on breaks the second profile's memory and WebUI credential reads),
# so an instance shared by the plugin would be re-pointed by every construction.
# The guard wraps that one call: an instance already initialised for another profile
# home is refused before ``initialize`` runs. Refusal happens inside the Agent's own
# path, so the Agent drops the provider, keeps built-in memory and tells the user
# through its startup-warning sink. Fresh instances (the documented provider
# contract) and single-profile installs never see a second home: no change.

_instance_owners: dict[int, tuple[Callable[[], Any], str]] = {}
_instance_owners_lock = threading.Lock()
_install_lock = threading.Lock()
_GUARD_MARK = "_webui_issue8517_shared_instance_guard"
_PRUNE_AT = 64


def _instance_ref(provider: Any) -> Callable[[], Any]:
    """Weak reference to ``provider``; a strong one when it cannot be weakly referenced,
    so its ``id()`` can never be reused by another instance while it is tracked."""
    try:
        return weakref.ref(provider)
    except TypeError:
        return lambda: provider


def _prune_dead_owners() -> None:
    if len(_instance_owners) < _PRUNE_AT:
        return
    for pid, (ref, _key) in list(_instance_owners.items()):
        if ref() is None:
            del _instance_owners[pid]


def _claim_for_profile(original: Callable[[Any, Any], bool], provider: Any, home: Any) -> bool:
    """``claim_instance`` that also refuses a cross-profile rebind without multiplexing."""
    key = profile_home_key(home) or str(home or "")
    pid = id(provider)
    # #8613: the acquisition half of the instance-lifetime protocol. An instance a
    # teardown is closing is never handed out again: the Agent drops a refused
    # provider and keeps built-in memory, which is the only safe answer for a
    # client that is going away. Without this the claim below would happily
    # re-bind it and the agent's next save would fail. Only the IN-FLIGHT window
    # is fenced: once the teardown has settled the entry is gone, and the next
    # construction re-claims and re-initialises the instance, exactly as the
    # owned path (``shutdown_memory_provider``) already does — a refusal that
    # outlived the teardown would leave a singleton provider off until restart.
    lifetime = _instance_lifetime_state(provider)
    if lifetime is not None:
        # The construction that hits this must not be cached (must-fix 2): the
        # Agent drops the provider and keeps built-in memory, which leaves the
        # refusal unreadable once the initialiser has returned.
        _note_lifetime_refusal(provider, lifetime)
        _log_once(
            f"acquire-dying:{pid}:{key}",
            "[webui] #8613: memory provider %r is not acquired for profile home %r: a "
            "teardown of that instance is running. This agent runs without that provider "
            "(built-in memory still works); the next turn constructs it again once the "
            "teardown has settled.",
            getattr(provider, "name", "?"), str(home),
        )
        return False
    with _instance_owners_lock:
        entry = _instance_owners.get(pid)
        owner = entry[1] if entry is not None and entry[0]() is provider else None
        if owner is None or owner == key:
            # The Agent's own claim keeps its bookkeeping and multiplex refusal; only an
            # accepted claim makes this home the owner.
            if not original(provider, home):
                return False
            if owner is None:
                _prune_dead_owners()
                _instance_owners[pid] = (_instance_ref(provider), key)
            return True
    name = getattr(provider, "name", "?")
    refusals = _construction_refusals.get()
    if refusals is not None:
        refusals.append(name)
    _log_once(
        f"shared:{name}:{owner}:{key}",
        "[webui] #8517: memory provider %r is disabled for profile home %r: the plugin "
        "returned the same shared provider instance that already serves profile home %r "
        "in this WebUI process, and initialising it again would point that profile's "
        "live agents at this profile's memory. This profile runs without that provider "
        "(built-in memory still works) until the WebUI restarts; the first profile to use "
        "it keeps it. The provider must return a new instance per agent.",
        name, str(home), owner,
    )
    return False


def install_shared_provider_instance_guard() -> bool:
    """Wrap the Agent's ``agent.memory_manager.claim_instance`` once per loaded module.
    True when the guard is active. Agents without that seam (before 2026-10-17) are
    left as they are and logged once; ``construct_profile_scoped_agent`` then turns
    external providers off unless this process serves a single profile."""
    try:
        mm = sys.modules.get("agent.memory_manager") or importlib.import_module("agent.memory_manager")
    except Exception:
        _log_once(
            "shared-guard-import",
            "[webui] #8517: could not import agent.memory_manager; a memory provider that "
            "shares one instance across profiles is not guarded in this process.",
        )
        return False
    current = getattr(mm, "claim_instance", None)
    if getattr(current, _GUARD_MARK, False):
        return True
    if not callable(current):
        _log_once(
            "shared-guard-seam",
            "[webui] #8517: this Hermes Agent has no memory provider instance claim (older "
            "than 2026-10-17); a provider that hands every agent one shared instance could "
            "mix profiles' memory, so external memory providers stay off while this WebUI "
            "serves several profiles. Upgrade the Agent.",
        )
        return False
    with _install_lock:
        current = getattr(mm, "claim_instance", None)
        if getattr(current, _GUARD_MARK, False):
            return True

        @functools.wraps(current)
        def claim_instance(provider: Any, home: Any) -> bool:
            return _claim_for_profile(current, provider, home)

        setattr(claim_instance, _GUARD_MARK, True)
        mm.claim_instance = claim_instance
    return True


# ── who holds a provider instance (#8613) ───────────────────────────────────
#
# A provider that hands every agent ONE process-wide instance (mnemosyne-hermes
# 0.7.1) ends up shared by every agent of the profile that claimed it: the #8517
# P4 guard above stops a SECOND PROFILE from rebinding such an instance, not a
# second agent of the same one. Each of those agents owns its own MemoryManager,
# and every manager-level teardown (``claim_teardown``, ``release_soft``,
# ``shutdown_all``) shuts the shared INSTANCE down — the profile's other live
# agents then lose the client their next memory save goes through. That is why
# the unowned-eviction release was reverted (#8636): it proved a manager, not an
# instance.
#
# WebUI therefore records, per provider instance, which agents currently hold it,
# and tears an instance down only once the agent reaching the boundary is its LAST
# holder. Keyed by ``id(provider)`` exactly like the P4 guard (an unhashable or
# value-equal provider is still tracked), validated against a weak reference so a
# collected instance's id can never be mistaken for a live one, and emptied by
# weak-reference callbacks so an agent or an instance that dies without a boundary
# does not pin the other one forever.
#
# The table is PROCESS-LOCAL by nature: a provider instance is a process-local
# object, and the P4 guard above already keys per-process ownership by ``id()``.
# Nothing here is shared across processes, so ``_provider_holders_lock`` is the
# whole concurrency story — the flock-and-re-read-the-disk discipline for
# cross-process state (state.db) would prove nothing about object identity and is
# deliberately not used.


class _AgentHolder:
    """One agent's hold on a provider instance (#8613).

    ``agent`` is the weak reference the table is keyed by; ``manager`` is a weak
    reference to the memory manager the agent holds the instance through. The
    manager is only needed to tell, once the agent itself is gone (evicted or
    collected), whether it still has queued memory work — see
    ``_InstanceHolders.work``.
    """

    __slots__ = ("agent", "manager")

    def __init__(self, agent: Callable[[], Any], manager: Callable[[], Any]):
        self.agent = agent
        self.manager = manager


class _InstanceHolders:
    """Who holds one provider instance right now, whether a teardown of it is
    running, and which departed agents still have queued memory work on it."""

    __slots__ = ("ref", "home_key", "holders", "teardown", "work", "claiming", "intent")

    def __init__(self, ref: Callable[[], Any], home_key: str = ""):
        self.ref = ref                                    # weak reference: identity, never id() alone
        self.home_key = home_key                          # profile home key the instance serves
        self.holders: dict[int, _AgentHolder] = {}        # id(agent) -> its hold
        self.teardown = False                             # a teardown of this instance is running
        # id(manager) -> weak reference to that manager, for an agent that LEFT
        # the holder set while its manager still had queued memory work. That
        # work still writes through this instance, so it holds the instance
        # exactly like a live holder does — until the work drains, at which
        # point the weak reference dies with the manager and the hold
        # evaporates.
        #
        # The hold belongs to the MANAGER, never to the agent that happened to
        # hold the instance (#8613 round 2, finding A): CPython reuses the
        # id() of a collected agent, so a hold filed under the agent's id was
        # picked up by a later, unrelated agent that landed on the same id —
        # and the release then closed the client under the DEPARTED agent's
        # queued save. Every read and removal below compares the RESOLVED
        # manager (``manager_ref() is manager``), never a bare id.
        self.work: dict[int, Callable[[], Any]] = {}
        # How many releases are currently claiming this entry (#8613 round 2,
        # finding B). A claim removes its agent from the holder set BEFORE it
        # may wait for the construction fence, so while it waits the entry has
        # no holders, no teardown flag and no work — and every prune site
        # deletes an entry in that shape, process-wide. A claiming entry is
        # kept until its claim settles.
        self.claiming = 0
        # A last-holder release that was refused ONLY because queued memory
        # work still writes through this instance (#8613, review 5722370551):
        # the cleanup intent is kept here and runs once that work has drained,
        # without another agent reaching a session boundary. A new holder
        # takes the instance over (``register_provider_holders`` drops it), and
        # an entry carrying one is never pruned. See ``_ReleaseIntent``.
        self.intent: "Optional[_ReleaseIntent]" = None


_provider_holders: dict[int, _InstanceHolders] = {}
# Reentrant: a weak-reference callback can fire from a GC that runs inside the lock.
_provider_holders_lock = threading.RLock()
# The SAME lock as a Condition, so a release can WAIT for the constructions it
# fences instead of refusing and dropping the instance out of the table (#8613
# should-fix 5). ``wait()`` releases the lock, so no thread is blocked by it.
_provider_holders_cond = threading.Condition(_provider_holders_lock)

# ── the GC weak-reference callbacks never WAIT for the holder lock ─────────
#
# A weak-reference callback fires on whatever thread collected the object —
# including from inside a logging handler's ``write()``, while that thread
# holds the handler's own lock. The holder lock can be held at the same time
# by a thread that is LOGGING (the release logs under it), so a callback that
# WAITS for the holder lock deadlocks the two threads against each other: the
# holder lock is then never released, and every later construction that needs
# the holder table blocks until the WebUI restarts (#8613 round 3, finding 1).
#
# So a callback that finds the lock busy QUEUES its work and returns; the next
# locked table operation applies the queue under the lock. A callback that
# fires on the thread that already holds the lock — a GC inside a locked
# section — still runs immediately, because the lock is reentrant.
_pending_forgets: list[tuple[str, int, Callable[[], Any]]] = []
# Re-entrancy guard: applying a queued forget can itself allocate, and that
# allocation can run a GC whose callback fires on THIS thread — which holds the
# lock, so the callback runs its forget inline and would drain the queue
# again. The guard keeps the drain single-level; anything queued meanwhile is
# applied by the next locked table operation.
_draining_pending_forgets = False


def _drain_pending_forgets_locked() -> None:
    """Apply the forget operations queued while the holder lock was busy.

    Called with ``_provider_holders_lock`` held, before the operation that
    reads or writes the table: a forgotten agent must not be counted as a
    holder, and a departed agent's queued work must be registered as a hold
    before a release may prove that nothing else writes through the instance.
    """
    global _draining_pending_forgets
    if _draining_pending_forgets:
        return
    _draining_pending_forgets = True
    try:
        while _pending_forgets:
            kind, key, ref = _pending_forgets.pop()
            if kind == "agent":
                _forget_agent_holder_locked(key, ref)
            else:
                _forget_provider_instance_locked(key, ref)
    finally:
        _draining_pending_forgets = False


# ── one instance-lifetime protocol: acquisition reservations (#8613) ─────────
#
# ``construct_profile_scoped_agent`` initialises the native memory manager INSIDE
# ``agent_cls(**agent_kwargs)`` (the Agent's ``MemoryManager.initialize_for_agent``
# -> ``claim_instance`` -> ``provider.initialize``) and can only record the holder
# once that call has RETURNED. The whole provider initialisation sits between the
# two, and a session boundary landing there proves the OLD agent the last
# RECORDED holder and tears the shared instance down under the agent being built
# — which is then handed out cacheable, with a client the release just closed.
#
# So acquisition is reserved BEFORE the provider is initialised and held until the
# holder is registered (or the build aborts), and the last-holder proof counts the
# reservation: a teardown cannot be claimed for an instance a construction is about
# to acquire. The reservation is keyed by PROFILE HOME, not by instance, because
# the instance is not known yet; the #8517 P4 guard above already proves that one
# instance serves exactly one profile home, so the home names it unambiguously.
#
# ``_inflight_homes`` is guarded by ``_provider_holders_lock`` — the SAME lock as
# the holder table — so the proof and a reservation can never disagree, and it is
# only ever touched for a dict update, so the lock is never held across provider
# I/O (``release()`` runs a 10 s drain outside it, exactly as before).
#
# The third hold is queued memory work (#8613 must-fix 1). An agent that reaches a
# boundary — or is collected — while its manager still has a background save
# queued keeps holding every instance that save goes through: the work runs in the
# provider's own profile on the manager's writer thread, so releasing the instance
# under it would close the client the save needs. That hold is weak (the queued
# work keeps the manager alive) and evaporates as soon as the work has drained.
#
# The other half of the protocol is the acquisition chokepoint: the #8517 P4
# ``claim_instance`` guard refuses an instance whose entry is in ``teardown`` (a
# release is running), so an agent constructed DURING a release is never handed
# the client that release is about to close — the Agent drops the provider and
# keeps built-in memory. A refusal is noted construction-scoped
# (``_lifetime_refusals``) so the build answers ``cacheable=False`` even though
# the Agent's own initialiser has already dropped the refused provider.
# ``register_provider_holders`` refuses the same entry as a backstop, and
# ``construct_profile_scoped_agent`` fails closed (rebuild without memory, do not
# cache) for an Agent without that seam, whose manager keeps the instance anyway.
# Once the release has settled the entry is gone and the instance is claimed
# again, so the fence lasts exactly as long as the teardown it protects.

_inflight_homes: dict[str, int] = {}

# A release that must NOT block on the construction fence. The
# compression-migration close runs inside the per-session agent lock, so
# waiting there for a same-profile construction to finish would stall the
# user's next turn (#8613 round 2, the maintainer's note). The flag makes the
# claim REFUSE the release instead of waiting — the provider keeps its worker,
# exactly as it does when the fence wait times out.
_skip_fence_wait: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "webui_8613_skip_fence_wait", default=False,
)


@contextlib.contextmanager
def release_without_fence_wait():
    """Release the memory provider WITHOUT waiting for the construction fence.

    A release normally waits, bounded, for a same-profile construction that
    may be about to acquire the very instance the release would close
    (``_claim_last_held_teardown``). A caller that must not block — the
    compression-migration close holds the per-session agent lock while it
    runs — uses this instead: the release is refused right away, and the
    provider keeps its worker until the next session boundary.

    The flag is construction-scoped (a ContextVar, like the refusal notes
    beside it), so it applies to the release it wraps and to nothing else.
    """
    token = _skip_fence_wait.set(True)
    try:
        yield
    finally:
        _skip_fence_wait.reset(token)


# #8804: how long a construction on an Agent without ``claim_instance`` waits for a
# teardown of its profile's provider instance before it builds without memory. Well
# above the Agent's own bounded waits inside ``shutdown_all``.
_UNFENCED_TEARDOWN_WAIT_S = 30.0


def _teardown_running_locked(home_key: str) -> bool:
    """A teardown is closing a provider instance ``home_key``'s agents may share
    (#8804). Holder lock held. An entry without a home key could serve any profile,
    so it counts for every one."""
    return any(
        entry.teardown and (not entry.home_key or entry.home_key == home_key)
        for entry in _provider_holders.values()
    )


def _reserve_acquisition(home_key: str) -> bool:
    """Reserve the acquisition of ``home_key``'s provider instances BEFORE they
    are initialised (#8613). Paired with ``_end_acquisition`` in a ``finally``.

    #8804: on an Agent without ``claim_instance`` (before 2026-10-17) the instance a
    teardown is closing cannot be refused to a new agent: the Agent initialises the
    shared instance inside its constructor, so a build that ran during another
    agent's ``shutdown_all`` re-opened the instance and the teardown then closed it
    under the new agent. Such a construction therefore WAITS here, before it
    reserves and in the same critical section as the release's claim, until no
    teardown of an instance its profile may share is running; the release's
    settlement wakes it. It never waits while holding a reservation, so it cannot
    hold up the claim's own wait for constructions in flight. False (nothing
    reserved) when the wait runs out: the caller builds without memory and does not
    cache the agent. The holder lock is not held across the teardown itself.

    An Agent with the seam refuses the instance itself (``_claim_for_profile``), so
    its constructions never wait here and reserve exactly as before."""
    if not home_key:
        return True
    webui_fences = _claim_seam_available() is not True
    with _provider_holders_lock:
        if webui_fences and _teardown_running_locked(home_key):
            deadline = time.monotonic() + _UNFENCED_TEARDOWN_WAIT_S
            while _teardown_running_locked(home_key):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _provider_holders_cond.wait(remaining)
        _inflight_homes[home_key] = _inflight_homes.get(home_key, 0) + 1
    return True


def _end_acquisition(home_key: str) -> None:
    """Release a reservation taken by ``_reserve_acquisition``: the holder is now
    registered, or the build aborted and acquires nothing (#8613)."""
    if not home_key:
        return
    with _provider_holders_lock:
        left = _inflight_homes.get(home_key, 0) - 1
        if left > 0:
            _inflight_homes[home_key] = left
        else:
            _inflight_homes.pop(home_key, None)
        # A release waiting on the fence this construction held may now re-run its
        # proof: either a holder appeared for the instance (the release is then
        # correctly refused) or nothing was acquired (the release runs).
        _provider_holders_cond.notify_all()
        # #8817: a release that could not wait for the construction kept its
        # intent; it is retried now that no construction is in flight for its
        # homes. A new holder of the same instance has already taken the intent
        # over (``register_provider_holders``), so it is not among these.
        ready = _fence_intents_ready_locked()
    # Started outside the holder lock: the retry takes it itself.
    for intent in ready:
        _start_intent_retry(intent)


def _weak_manager_ref(manager: Any) -> Callable[[], Any]:
    """Weak reference to a memory manager; an inert one when it cannot be weakly
    referenced, so a hold is never pinned by the manager it describes (#8613)."""
    try:
        return weakref.ref(manager)
    except TypeError:
        return lambda: None


def _agent_manager(agent: Any) -> Any:
    """The agent's memory manager, read defensively (None without one)."""
    return getattr(agent, "_memory_manager", None)


def _future_is_done(future: Any) -> bool:
    """Whether ``future`` has finished, WITHOUT taking the future's condition lock.

    ``concurrent.futures.Future.done()`` acquires the future's own condition
    lock, and the writer thread holds that same condition inside
    ``Future.set_result`` while it allocates (``for waiter in self._waiters``),
    so a gen-0 collection can run inside it. Reading the future's ``_state``
    attribute answers the same question without any lock (#8613 round 2,
    finding C): the state is a plain attribute that only ever moves from
    PENDING/RUNNING to FINISHED/CANCELLED, so reading it without the lock can
    only ever be a stale-but-conservative answer — a future that is not done
    yet reads as not done, which is the safe direction (the instance is kept
    alive for the pending save).

    A future that does not expose ``_state`` at all (a stand-in, or a future
    from another library) falls back to ``done()``: the answer is then taken
    under the future's lock, which is the very edge this fix removes — but a
    foreign future has no writer allocating under a condition this table
    knows about, and an unreadable answer counts as work pending.
    """
    state = getattr(future, "_state", None)
    if state is None:
        try:
            return bool(future.done())
        except Exception:
            return False
    name = getattr(state, "name", state)
    return name in ("FINISHED", "CANCELLED", "CANCELLED_AND_NOTIFIED")


def _manager_has_outstanding_work(manager: Any) -> bool:
    """True when ``manager`` still has queued or running memory work (#8613
    must-fix 1): a background save that has not drained yet. This is the Agent's
    own executor bookkeeping, read defensively; an unreadable answer counts as
    work pending, because a provider that keeps running is the status quo while
    a save that goes through a client another agent's teardown closed is not.

    The future's state is read WITHOUT taking the future's condition lock
    (#8613 round 2, finding C): this function runs with
    ``_provider_holders_lock`` held, and taking the future's lock there
    inverts the lock order against a writer inside ``Future.set_result`` —
    a deadlock whose consequence is that every later memory-bearing
    construction blocks until restart.
    """
    if manager is None:
        return False
    futures = getattr(manager, "_background_futures", None)
    if not isinstance(futures, dict) or not futures:
        return False
    try:
        return any(not _future_is_done(future) for future in list(futures))
    except Exception:
        return True


def _inflight_acquisitions(home_key: str) -> int:
    """How many constructions are acquiring for ``home_key``. An empty key cannot
    be correlated with any instance, so ANY construction in flight counts — an
    unprovable answer is treated as an in-flight acquisition, never as none."""
    with _provider_holders_lock:
        if home_key:
            return _inflight_homes.get(home_key, 0)
        return sum(_inflight_homes.values())


def _note_lifetime_refusal(provider: Any, state: str) -> None:
    """Note, for the construction in progress, that ``provider`` was refused
    because a teardown of its instance is ``state`` (#8613 must-fix 2). Read back
    by ``construct_profile_scoped_agent`` once the constructor has returned."""
    try:
        seen = _lifetime_refusals.get()
    except LookupError:  # pragma: no cover - the ContextVar carries a default
        seen = ()
    _lifetime_refusals.set((*seen, f"{getattr(provider, 'name', '?')}:{state}"))


def _instance_lifetime_state(provider: Any) -> Optional[str]:
    """``"teardown"`` while a teardown of ``provider`` is running, else None
    (#8613). The acquisition chokepoint (the #8517 P4 ``claim_instance`` guard)
    refuses an instance in that state: the Agent then drops the provider and
    keeps built-in memory, which is the only safe answer for an instance whose
    client is being closed.

    A teardown that has COMPLETED fences nothing: the entry leaves the table with
    its last holder, and the next construction claims and re-initialises the
    instance again — the same recovery the owned path
    (``shutdown_memory_provider`` -> ``release_soft``) has always had. Refusing
    for longer than the teardown itself would leave a singleton provider off
    until the WebUI restarts.

    Takes ``_provider_holders_lock``. Nothing takes the holder lock while holding
    ``_instance_owners_lock`` (the P4 table's lock), so the two never nest.
    """
    with _provider_holders_lock:
        entry = _live_holders_entry(provider)
        if entry is None:
            return None
        if entry.teardown:
            return "teardown"
        return None


def agent_holds_dying_instance(agent: Any) -> tuple[bool, str]:
    """``(True, name)`` when ``agent`` serves a provider instance a teardown is
    closing right now (#8613). Such an agent's next memory save would go through
    a client another agent's teardown closed, so it must not be handed out as a
    cacheable agent — ``construct_profile_scoped_agent`` fails closed and
    rebuilds it without memory instead.
    """
    if agent is None:
        return False, ""
    for provider in _provider_instances(agent):
        if _instance_lifetime_state(provider) is not None:
            return True, str(getattr(provider, "name", "?"))
    return False, ""


def _provider_instances(agent: Any) -> list:
    """The provider instances ``agent``'s memory manager serves, read defensively:
    every Agent attribute WebUI touches here has changed shape at least once
    (``providers`` as a property, as a method, or as a plain list)."""
    manager = getattr(agent, "_memory_manager", None)
    if manager is None:
        return []
    providers: Any = getattr(manager, "providers", None)
    if callable(providers):
        try:
            providers = providers()
        except Exception:
            logger.debug("[webui] #8613: reading the agent's memory providers failed", exc_info=True)
            return []
    if not providers:
        return []
    try:
        return [p for p in list(providers) if p is not None]
    except TypeError:
        return []


def _weak_agent_ref(agent: Any) -> Callable[[], Any]:
    """Weak reference to ``agent`` whose death drops it from every holder set
    (#8613); a strong one when it cannot be weakly referenced, so a holder is never
    silently dropped from an instance it still uses."""
    agent_pid = id(agent)
    try:
        return weakref.ref(agent, lambda dead, pid=agent_pid: _forget_agent_holder(pid, dead))
    except TypeError:
        return lambda: agent


def _forget_provider_instance(inst_pid: int, ref: Callable[[], Any]) -> None:
    """Weak-reference callback: the instance itself is gone, so its holder set is.

    NEVER waits for the holder lock (#8613 round 3, finding 1): this callback
    fires from inside a GC, possibly on a thread that holds a logging
    handler's lock, while the holder lock is held by a thread that is
    logging. The two waits would deadlock the pair, and the holder lock would
    then never be released. A busy lock means the work is queued and applied
    by the next locked table operation.
    """
    if not _provider_holders_lock.acquire(blocking=False):
        _pending_forgets.append(("instance", inst_pid, ref))
        return
    try:
        _drain_pending_forgets_locked()
        _forget_provider_instance_locked(inst_pid, ref)
    finally:
        _provider_holders_lock.release()


def _forget_provider_instance_locked(inst_pid: int, ref: Callable[[], Any]) -> None:
    """The instance is gone, so its holder set is. The holder lock is held."""
    entry = _provider_holders.get(inst_pid)
    if entry is not None and entry.ref is ref:
        del _provider_holders[inst_pid]


def _forget_agent_holder(agent_pid: int, ref: Callable[[], Any]) -> None:
    """Weak-reference callback: a collected agent stops holding every instance.

    NEVER waits for the holder lock (#8613 round 3, finding 1) — see
    ``_forget_provider_instance``. A busy lock means the work is queued and
    applied by the next locked table operation.
    """
    if not _provider_holders_lock.acquire(blocking=False):
        _pending_forgets.append(("agent", agent_pid, ref))
        return
    try:
        _drain_pending_forgets_locked()
        _forget_agent_holder_locked(agent_pid, ref)
    finally:
        _provider_holders_lock.release()


def _forget_agent_holder_locked(agent_pid: int, ref: Callable[[], Any]) -> None:
    """A collected agent stops holding every instance. The holder lock is held."""
    for key, entry in list(_provider_holders.items()):
        holder = entry.holders.get(agent_pid)
        if holder is not None and holder.agent is ref:
            del entry.holders[agent_pid]
            # #8613 must-fix 1: the agent is gone, but a save its manager
            # already queued is not. That manager is still alive — the queued
            # work holds it — so the hold survives as queued work, weakly, and
            # evaporates once the work has drained.
            #
            # #8613 round 2 (finding A): the hold is filed under the
            # MANAGER's identity, not the agent's id(). CPython reuses the
            # id() of a collected object, so a hold filed under the agent's
            # id was inherited by a later agent that landed on the same id —
            # and the release then closed the client under this departed
            # agent's queued save. The manager is a different object with a
            # different identity, so the two never collide.
            #
            # The queued-work check allocates; a nested callback's prune must
            # not take the entry before the hold is filed (see
            # ``_forget_holder_locked``), so it is marked as claimed meanwhile.
            entry.claiming += 1
            try:
                manager = holder.manager()
                if manager is not None and _manager_has_outstanding_work(manager):
                    entry.work[id(manager)] = holder.manager
            finally:
                entry.claiming -= 1
        # An entry nobody holds any more, with no teardown and no queued work
        # to protect, leaves the table — unless a release is claiming it
        # right now (#8613 round 2, finding B). A nested prune may already have
        # taken it, so the removal tolerates a missing key.
        if not entry.holders and not entry.teardown and not entry.work and not entry.claiming and entry.intent is None:
            _provider_holders.pop(key, None)


def _live_holders_entry(provider: Any) -> Optional[_InstanceHolders]:
    """The entry for ``provider``, or None. A stale ``id()`` — the instance it was
    recorded for has been collected — is dropped, never trusted to describe this one."""
    inst_pid = id(provider)
    entry = _provider_holders.get(inst_pid)
    if entry is None:
        return None
    if entry.ref() is not provider:
        del _provider_holders[inst_pid]
        return None
    return entry


_BUILD_HOME_ATTR = "_webui_memory_build_home"


def _record_build_home(agent: Any, profile_home: str) -> None:
    """Remember the profile home ``agent`` was built under (#8804).

    An Agent from before 2026-10-17 does not report the home its memory provider
    bound (no ``_bound_scope``). Its provider binds exactly the home the WebUI held
    for the build: on such an Agent, an external provider only runs while the WebUI
    serves a single profile (no ``claim_instance`` seam, #8585). The unowned-eviction
    release needs that home to run the teardown in it."""
    try:
        setattr(agent, _BUILD_HOME_ATTR, str(profile_home or ""))
    except Exception:
        logger.debug("[webui] #8804: could not record the build home on the agent", exc_info=True)


def _is_legacy_manager(manager: Any) -> bool:
    """A MemoryManager from before 2026-10-19 (#8804): no ``release_soft`` and no
    ``claim_teardown``, but the ``shutdown_all`` every Agent since 2026-04 has."""
    return (
        manager is not None
        and not callable(getattr(manager, "release_soft", None))
        and not callable(getattr(manager, "claim_teardown", None))
        and callable(getattr(manager, "shutdown_all", None))
    )


def _mark_legacy_shut_down(holder: Any) -> None:
    """After an old Agent's provider was released, make the agent's own
    ``shutdown_memory_provider`` a no-op (#8804). On those Agents it runs
    ``on_session_end`` unconditionally, so a later ``close()`` would commit under an
    owner nobody could resolve, then shut the providers down a second time (closing
    the client under any successor that shares the instance). Current Agents get the
    same effect from the soft-release teardown claim.

    A deferred release runs for a ``_ReleaseIntent``: the agent it stands in for is
    marked through the intent's weak reference, when that agent is still alive."""
    target = holder
    if isinstance(holder, _ReleaseIntent):
        ref = holder.agent_ref
        target = ref() if ref is not None else None
        if target is None:
            return
    try:
        target._memory_provider_shutdown = True
    except Exception:
        logger.debug("[webui] #8804: could not mark the released agent shut down", exc_info=True)


# The reason the Agent itself passes to ``PreparedCompaction.discard`` when it releases an
# agent (``agent.prepared_compaction.release_for_agent``: cache eviction, rewind, close).
_AGENT_RELEASED_DISCARD = ("agent released", "agent_released")


# The methods of the Agent's ``PreparedCompaction`` that ``discard`` runs on the instance
# itself. Shadowing any of them on the instance would run someone else's code there.
_AGENT_DISCARD_PATH = frozenset({"discard", "_count_unspliced"})


def _agent_context_classes() -> tuple:
    """The Agent's built-in ``ContextCompressor`` and ``PreparedCompaction`` classes
    (either is None when this Agent does not have it; ``PreparedCompaction`` is v0.22.6+)."""
    classes = []
    for module_name, class_name in (
        ("agent.context_compressor", "ContextCompressor"),
        ("agent.prepared_compaction", "PreparedCompaction"),
    ):
        try:
            module = sys.modules.get(module_name) or importlib.import_module(module_name)
            classes.append(getattr(module, class_name, None))
        except Exception:
            classes.append(None)
    return tuple(classes)


def release_unowned_context_engine(agent: Any, *, session_id: str) -> bool:
    """Cancel the background summary work of an evicted agent whose
    ``shutdown_memory_provider`` the WebUI refused (#8804 follow-up). True when this
    function called the Agent's own ``PreparedCompaction.discard``, whether or not that call
    raised (an exception is caught and logged at DEBUG); the caller does not use the value.

    ``call_as_session_owner`` refuses the whole shutdown when the session's owner cannot
    be resolved, because a session end may write under an owner nobody can confirm
    (#8517; fail closed, AGENTS.md). That includes the context engine's
    ``on_session_end``: ``agent/context_engine.py`` makes it a plugin extension point that
    may flush session data, so it is NEVER called here, and stays behind the owner check.

    What the built-in ``ContextCompressor``'s end does beyond resetting the in-memory
    state of an engine that is being discarded is ``prepared_compaction.discard`` (Agent
    v0.22.6+). It cancels a background summary pass that would otherwise run to
    completion, a model call nobody will use. That is close-only: no session data, only an
    aggregate opt-in metric count. The Agent makes the same call itself when it releases an
    agent (``release_for_agent``: cache eviction, rewind, close), with the same reason. So
    that one call runs here, in the profile home the WebUI built the agent under (never the
    ambient env mirror). An older Agent has no prepared compaction, and nothing to cancel.

    Only the Agent's own code may run on this path, because the session's owner is unknown.
    So nothing on the engine or its class is read before the identity check
    ``type(engine) is ContextCompressor``: not ``isinstance`` (it can consult a plugin's
    ``__class__``), not a method or ``__name__`` (a plugin's descriptor or metaclass would
    run). Any other engine is the plugin's and is left alone, with a once-only line that
    names no class. Then the engine must hold exactly the Agent's ``PreparedCompaction``,
    with no instance attribute shadowing ``discard`` or ``_count_unspliced``.

    The caller runs this only when the Agent's own shutdown did not run (a refusal at hook
    time happens INSIDE that shutdown), decided before the memory release, which marks an
    old Agent's agent shut down (#8806). Either way the agent is then marked shut down,
    the state v0.52.544 left an evicted agent in, so a later ``shutdown_memory_provider``
    cannot run a session end under the unresolved owner."""
    if agent is None:
        return False
    try:
        engine = getattr(agent, "context_compressor", None)
        if engine is None:
            return False
        compressor_class, prepared_class = _agent_context_classes()
        # The first touch of the engine: an identity compare of its exact type.
        if compressor_class is None or type(engine) is not compressor_class:
            _log_once(
                "engine-end-owner",
                "[webui] #8804: an evicted agent's context engine is not the Agent's built-in "
                "compressor, so it is left alone: its session end may write session data and the "
                "session's owner could not be confirmed, so it stays behind the owner check (fail "
                "closed).",
            )
            return False
        prepared = getattr(engine, "prepared_compaction", None)  # the Agent's own engine
        if prepared is None:  # the opt-in prepare_ahead is off, or an Agent before v0.22.6
            return False
        if (
            prepared_class is None
            or type(prepared) is not prepared_class
            or _AGENT_DISCARD_PATH.intersection(vars(prepared))
        ):
            logger.debug(
                "[webui] #8804: the built-in context engine of evicted session %s holds "
                "prepared-summary state that is not the Agent's own; it is left alone.",
                session_id,
            )
            return False
        build_home = str(getattr(agent, _BUILD_HOME_ATTR, "") or "")
        restore, reason = _enter_profile_home(build_home, profile_home_key(build_home) if build_home else None)
        if reason is not None:
            _log_once(
                "engine-release-scope",
                "[webui] #8804: the background summary pass of an evicted agent whose memory "
                "shutdown was refused was not cancelled (%s); it runs to completion unused.",
                reason,
            )
            return False
        try:
            prepared.discard(*_AGENT_RELEASED_DISCARD)
        except Exception:
            logger.debug("[webui] #8804: cancelling the prepared summary failed for %s", session_id, exc_info=True)
        finally:
            restore()
        return True
    finally:
        try:
            agent._memory_provider_shutdown = True
        except Exception:
            logger.debug("[webui] #8804: could not mark the evicted agent shut down", exc_info=True)


def register_provider_holders(agent: Any, *, home_key: str = "") -> int:
    """Record ``agent`` as a holder of every provider instance its memory manager
    serves (#8613). Idempotent per (agent, instance) — a turn that reuses a cached
    agent registers nothing new — and returns how many instances it now holds.

    ``construct_profile_scoped_agent`` calls this right after a memory-bearing
    build, so an agent is in the table before any session boundary can ask whether
    it is the last holder of an instance.

    An instance that is being torn down admits NO new holder: recording one would
    claim protection for a client that is being closed, and the holder's next save
    would fail. The acquisition itself is refused earlier, at the #8517 P4
    ``claim_instance`` guard; this is the backstop for an Agent without that seam,
    whose manager keeps the instance anyway.
    """
    if agent is None:
        return 0
    instances = _provider_instances(agent)
    if not instances:
        return 0
    agent_pid = id(agent)
    manager_ref = _weak_manager_ref(_agent_manager(agent))
    added = 0
    with _provider_holders_lock:
        _drain_pending_forgets_locked()
        for provider in instances:
            inst_pid = id(provider)
            entry = _provider_holders.get(inst_pid)
            if entry is None or entry.ref() is not provider:
                try:
                    ref = weakref.ref(
                        provider,
                        lambda dead, pid=inst_pid: _forget_provider_instance(pid, dead),
                    )
                except TypeError:  # no weak reference: hold it strongly, never by id() alone
                    ref = lambda provider=provider: provider
                entry = _InstanceHolders(ref, home_key)
                # #8613 round 3 (finding 2): the entry is published only with
                # its first holder ALREADY inside it. The weak reference below
                # allocates, and that allocation can run a GC — whose callbacks
                # prune this table PROCESS-WIDE, deleting every entry nobody
                # holds. An entry published while still EMPTY is exactly what
                # such a prune removes, and the registration would then fill a
                # DETACHED object: the live agent is in no table, so the next
                # boundary sees no holder and closes the provider under it.
                entry.holders[agent_pid] = _AgentHolder(_weak_agent_ref(agent), manager_ref)
                _provider_holders[inst_pid] = entry
                added += 1
                continue
            if agent_pid in entry.holders:
                continue
            if entry.teardown:
                _log_once(
                    f"register-dying:{inst_pid}",
                    "[webui] #8613: provider %r was handed to a new agent while it was "
                    "being torn down, so that agent is not recorded as a holder; it runs "
                    "on built-in memory only and the provider is not released for it.",
                    getattr(provider, "name", "?"),
                )
                continue
            # #8613 round 3 (finding 2): the entry is MUTATED here, and the
            # allocation inside ``_weak_agent_ref`` can run a GC whose
            # callbacks prune this table process-wide. The entry is therefore
            # claimed for the mutation, so a prune cannot detach it while it
            # is being filled.
            entry.claiming += 1
            try:
                entry.holders[agent_pid] = _AgentHolder(_weak_agent_ref(agent), manager_ref)
            finally:
                entry.claiming -= 1
            # #8613 (review 5722370551): a live holder now owns the instance, so
            # a cleanup intent retained for an earlier departure is handed over
            # to it — this agent's own session boundary releases the instance.
            # Running the intent later would close the client under this agent.
            entry.intent = None
            added += 1
    return added


def unregister_provider_holders(agent: Any) -> None:
    """Drop ``agent`` from every holder set: it was detached, evicted or discarded
    and will never touch those instances again (#8613). Never touches the instances
    themselves — whether one may be torn down is the caller's decision, taken with
    ``agent_is_last_provider_holder`` BEFORE this runs.

    A manager that still has queued memory work keeps a hold on every instance it
    served (``_InstanceHolders.work``): that work runs in the provider's own
    profile on the manager's writer and still goes through the instance, so
    dropping the agent without a trace would let another agent's boundary prove
    itself the last holder and close the client that save needs (must-fix 1). The
    hold is weak — the queued work keeps the manager alive — and the next proof
    drops it once the work has drained.
    """
    if agent is None:
        return
    agent_pid = id(agent)
    manager = _agent_manager(agent)
    with _provider_holders_lock:
        _drain_pending_forgets_locked()
        for key, entry in list(_provider_holders.items()):
            _forget_holder_locked(entry, agent_pid, manager)
            # An entry nobody holds any more, with no teardown and no queued work
            # to protect, leaves the table — unless a release is claiming it
            # right now (#8613 round 2, finding B).
            if not entry.holders and not entry.teardown and not entry.work and not entry.claiming and entry.intent is None:
                _provider_holders.pop(key, None)


def _forget_holder_locked(entry: _InstanceHolders, agent_pid: int, manager: Any) -> None:
    """Drop ``agent_pid`` from ``entry``'s holders, with ``_provider_holders_lock``
    held, keeping a queued-work hold when its ``manager`` still has outstanding
    memory work (#8613 must-fix 1).

    The hold is filed under the MANAGER's identity (#8613 round 2, finding A):
    a hold keyed by the agent's id() was inherited by a later agent that landed
    on the same id(), and the release then closed the client under the departed
    agent's queued save.
    """
    holder = entry.holders.pop(agent_pid, None)
    if holder is None:
        return
    # The queued-work check allocates, an allocation can run a GC on this thread,
    # and a collected agent's callback prunes inline (the lock is reentrant): the
    # entry has just lost a holder and not yet filed the work hold, so it is
    # marked as claimed for the duration (#8613, same window as the claim's).
    entry.claiming += 1
    try:
        if manager is not None and _manager_has_outstanding_work(manager):
            entry.work[id(manager)] = holder.manager
    finally:
        entry.claiming -= 1


def _last_provider_holder_locked(agent: Any, *, self_dropped: bool = False) -> tuple[bool, str, str]:
    """``agent_is_last_provider_holder`` with ``_provider_holders_lock`` already
    held. Returns ``(ok, why, wait_for)``; ``wait_for`` is empty when no bounded
    wait can clear the refusal, ``"fence"`` when the ONLY reason for it is a
    construction in flight for the instance's profile home, and ``"own_work"``
    when it is the RELEASING agent's own queued memory write (see
    ``_claim_last_held_teardown``).

    ``self_dropped`` is for the claim path, which has already removed ``agent``
    from the holder sets inside the same critical section: the agent is then no
    longer expected to be recorded, and its OWN queued write is counted against
    it (#8613 round 3, finding 3) — the release closes the providers after
    bounded waits, so a write still running past them would lose its client.
    """
    if agent is None:
        return True, "", ""
    instances = _provider_instances(agent)
    if not instances:
        return True, "", ""  # nothing held: no other agent can be broken by a release
    agent_pid = id(agent)
    for provider in instances:
        name = getattr(provider, "name", "?")
        entry = _live_holders_entry(provider)
        if entry is None:
            return False, f"provider {name!r} is not tracked as held by any agent", ""
        if not self_dropped:
            mine = entry.holders.get(agent_pid)
            if mine is None or mine.agent() is not agent:
                # A claim that already dropped this agent (a release in flight for
                # it) must answer as the teardown it is, not as an untracked hold.
                if entry.teardown:
                    return False, f"a teardown of provider {name!r} is already running", ""
                return False, f"provider {name!r} is not tracked as held by this agent", ""
        # A holder collected WHILE a critical section holds this lock has its
        # forget QUEUED (the GC callback never waits for the lock), so its
        # manager's queued work is not in ``entry.work`` yet. Skipping it below
        # as "collected" would let the release close the client under that save
        # (#8613 salvage, Codex r1): file its work hold now, exactly as the
        # queued forget will.
        #
        # Each holder's weak reference is read ONCE (Codex r2): with two passes,
        # a holder alive in the first and collected (on another thread) before
        # the second was neither counted as live nor had its work filed.
        #
        # A collected holder does not count: its weak-reference callback removes it,
        # and a stale reference must not pin an instance nobody uses any more.
        others = []
        for key, h in list(entry.holders.items()):
            if key == agent_pid:
                continue
            if h.agent() is not None:
                others.append(h)
                continue
            held = h.manager()
            if held is not None and _manager_has_outstanding_work(held):
                entry.work[id(held)] = h.manager
        if others:
            return False, (
                f"provider {name!r} of profile home {entry.home_key!r} is still held by "
                f"{len(others)} other agent(s)"
            ), ""
        if entry.teardown:
            return False, f"a teardown of provider {name!r} is already running", ""
        # #8613 must-fix 1: an agent that already left the holder set — evicted or
        # collected — while its manager still had queued memory work holds this
        # instance exactly like a live holder: that work still writes through it.
        #
        # #8613 round 2 (finding A): the hold is compared by the MANAGER's
        # identity, not by the agent's id(). The agent being released is
        # identified by its own manager, so a hold filed under a RECYCLED agent
        # id is still counted as another manager's work — which is what keeps
        # the shared client open for the departed agent's queued save.
        #
        # #8613 round 3 (finding 3): the RELEASING agent's own work counts too.
        # It used to be exempt ("the release drains that itself"), but the
        # Agent's ``release_soft`` closes the providers after BOUNDED waits
        # (a 10 s flush and a 5 s drain): a write still running past them
        # loses its client — "Cannot send a request, as the client has been
        # closed" — and the write is lost with it. The claim waits for that
        # write to drain ("own_work") and refuses the teardown when it does
        # not: the provider keeps its worker, and the write lands.
        own_manager = _agent_manager(agent)
        for pid, manager_ref in list(entry.work.items()):
            held_manager = manager_ref()
            if _manager_has_outstanding_work(held_manager):
                if held_manager is not None and own_manager is not None and held_manager is own_manager:
                    return False, (
                        f"provider {name!r} of profile home {entry.home_key!r} still has queued "
                        f"memory work from the agent being released"
                    ), "own_work"
                return False, (
                    f"provider {name!r} of profile home {entry.home_key!r} still has queued "
                    f"memory work from a departed agent"
                ), "work"
            del entry.work[pid]  # the work drained (or its manager is gone): the hold is spent
        # #8613: a construction in flight for this instance's profile home is a
        # holder the table cannot show yet — the Agent initialises the provider
        # inside its constructor and only registers once it has returned. Counting
        # the reservation is what makes the proof cover that window.
        pending = _inflight_acquisitions(entry.home_key)
        if pending:
            return False, (
                f"provider {name!r} of profile home {entry.home_key!r} is being acquired "
                f"by {pending} construction(s) in flight"
            ), "fence"
    return True, "", ""


def agent_is_last_provider_holder(agent: Any) -> tuple[bool, str]:
    """``(True, "")`` when every provider instance ``agent`` serves has this agent as
    its ONLY live holder, so a teardown cannot break another agent (#8613);
    ``(False, reason)`` when one is still held elsewhere, when a teardown of it is
    already running, when a construction is acquiring it right now, when a
    departed agent still has queued memory work on it, or when that cannot be
    proven. Every unknown answers False: a provider that keeps running is the
    status quo, a provider shut down under a live agent is not.
    """
    with _provider_holders_lock:
        # #8613 round 3 (finding 1): the queued forgets are applied FIRST, so a
        # collected agent is not counted as a live holder and its queued work is
        # registered as a hold — the proof below decides whether a release may
        # close the client that work goes through.
        _drain_pending_forgets_locked()
        ok, why, _wait_for = _last_provider_holder_locked(agent)
    return ok, why


# How long a release waits for a construction of the same profile home to finish
# before it gives up and refuses (#8613 should-fix 5). The Agent initialises the
# provider inside its constructor, so a build that has not returned yet may still
# be about to acquire the very instance this release would close; refusing there
# instead of waiting drops the instance out of the holder table with its writer
# alive, which is the leak #8613 exists to close. ``Condition.wait`` releases the
# lock while it waits, so no other thread is blocked by it.
_ACQUISITION_FENCE_WAIT_S = 15.0


# How long a release waits for the RELEASING agent's own queued memory work to
# drain before it gives up and keeps the provider alive (#8613 round 3, finding
# 3). The Agent's ``release_soft`` closes the providers after BOUNDED waits (a
# 10 s flush and a 5 s drain), so a save still running past them is closed
# under — "Cannot send a request, as the client has been closed" — and the save
# is lost with it. The release drains that work FIRST, here, and refuses the
# teardown when it does not drain: the provider keeps its worker and the save
# lands.
#
# The bound matches the Agent's own flush budget on purpose: a save that drains
# within it is flushed by the release either way, so the release keeps the
# coverage it had; a save that does NOT drain is the case that loses the write,
# and there the provider stays alive instead.
_RELEASE_WORK_DRAIN_WAIT_S = 10.0


def _drain_releasing_work(manager: Any) -> bool:
    """Whether the releasing manager's own queued memory work has drained.

    ``release_soft`` closes the providers after bounded waits, so a save still
    running when it returns loses its client (#8613 round 3, finding 3). The
    release therefore drains that work BEFORE the teardown — with the manager's
    own ``flush_pending`` when it has one, so the save really RAN (a sentinel
    task completing proves every earlier task ran) — and refuses the teardown
    when the work does not drain.

    The manager's own bookkeeping is re-read after the flush: the future's done
    callback drops it from ``_background_futures`` on the writer thread, so a
    drain that just happened can still be visible for an instant. That window
    must not turn into a refusal, so it is given a moment to clear.

    A caller that must not block (the compression-migration close holds the
    per-session agent lock) does not wait at all: it refuses, and the provider
    keeps its worker until the next session boundary.
    """
    if not _manager_has_outstanding_work(manager):
        return True
    if _skip_fence_wait.get():
        return False
    flush = getattr(manager, "flush_pending", None)
    if callable(flush):
        try:
            flush(timeout=_RELEASE_WORK_DRAIN_WAIT_S)
        except Exception:
            return False
    else:
        # No ``flush_pending`` (an older Agent): wait for the manager's own
        # bookkeeping to clear.
        deadline = time.monotonic() + _RELEASE_WORK_DRAIN_WAIT_S
        while _manager_has_outstanding_work(manager):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    # The flush proves the work RAN; the bookkeeping catches up a moment later.
    deadline = time.monotonic() + 1.0
    while _manager_has_outstanding_work(manager):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _agent_holds_instances_locked(agent: Any) -> tuple[bool, str, list]:
    """``(True, "", instances)`` when ``agent`` is a recorded holder of every
    provider instance its manager serves, else ``(False, why, [])`` — with
    ``_provider_holders_lock`` already held. The claim path runs this BEFORE it
    drops the agent from the holder sets, so a release is only ever attempted for
    an agent the table really shows as a holder."""
    if agent is None:
        return True, "", []
    instances = _provider_instances(agent)
    if not instances:
        return True, "", []  # nothing held: no other agent can be broken by a release
    agent_pid = id(agent)
    for provider in instances:
        name = getattr(provider, "name", "?")
        entry = _live_holders_entry(provider)
        if entry is None:
            return False, f"provider {name!r} is not tracked as held by any agent", []
        mine = entry.holders.get(agent_pid)
        if mine is None or mine.agent() is not agent:
            # A claim that already dropped this agent (its release is in flight)
            # must answer as the teardown it is, not as an untracked hold.
            if entry.teardown:
                return False, f"a teardown of provider {name!r} is already running", []
            return False, f"provider {name!r} is not tracked as held by this agent", []
    return True, "", instances


def _retain_for_fence_locked(retain: "Optional[_ReleaseIntent]", entries: list) -> bool:
    """Keep ``retain`` on ``entries`` while a construction in flight refuses the
    release, and record the profile homes it waits for (#8817). Holder lock held,
    in the critical section that refused, so a construction ending right after
    the refusal cannot be missed: ``_end_acquisition`` takes the same lock and
    starts the retry once those homes have no construction in flight. False when
    there is nothing to keep (no intent, or no tracked entry)."""
    if retain is None or not entries:
        return False
    for entry in entries:
        entry.intent = retain
    retain.fence_homes = frozenset(entry.home_key for entry in entries)
    retain.retained = True
    retain.deferred_for = "construction"
    return True


def _fence_refusal_outcome(kept: bool) -> str:
    """The log tail for a release a construction in flight refused (#8817)."""
    if kept:
        return ("The release is kept and runs once that construction has finished, "
                "unless the new agent holds the same instance.")
    return "The provider keeps its worker until the next session boundary."


def _fence_intents_ready_locked() -> list:
    """Intents waiting on constructions whose profile homes have none in flight
    any more (#8817), each taken off the wait so it is started exactly once.
    Holder lock held. Read through the holder table, so an intent no entry
    carries any more (a new holder took every instance over, or it was dropped)
    is never started. One a new holder took over on only some of its instances
    is started, and its claim then drops it (``_claim_release_intent``)."""
    ready: dict = {}
    for entry in list(_provider_holders.values()):
        intent = entry.intent
        if intent is None or not intent.fence_homes or id(intent) in ready:
            continue
        if all(_inflight_acquisitions(home) == 0 for home in intent.fence_homes):
            intent.fence_homes = None
            ready[id(intent)] = intent
    return list(ready.values())


def _claim_last_held_teardown(agent: Any, *, retain: "Optional[_ReleaseIntent]" = None) -> tuple[bool, str, list]:
    """``agent_is_last_provider_holder`` AND the teardown claim in ONE critical
    section, so no holder can be registered, dropped or reserved between the proof
    and the release (#8613). The claimed instances are returned by identity:
    holding them keeps their ``id()`` — and the claim — valid for the whole release.

    The agent leaves the holder sets FIRST, inside that same critical section, and
    only then are the others counted: two agents reaching their boundary at the
    same moment would otherwise each prove the other alive, release nothing, and
    leave the instance outside the table with its writer running (must-fix 6). An
    agent's own queued memory work does not count against it — the release drains
    that itself — but another agent's does (must-fix 1).

    The only refusal a wait can clear is a construction in flight for the
    instance's profile home, so the claim waits, bounded, on
    ``_provider_holders_cond`` (``_end_acquisition`` notifies it) and then re-runs
    the proof: the construction has either registered a holder — the release is
    then correctly refused — or acquired nothing, and the release runs. The lock
    is never held across provider I/O: ``wait()`` releases it, and ``release()``
    still runs a 10 s drain outside it entirely.
    """
    deadline = time.monotonic() + _ACQUISITION_FENCE_WAIT_S
    with _provider_holders_lock:
        # #8613 round 3 (finding 1): the queued forgets are applied FIRST, so a
        # collected agent is not counted as a live holder and its queued work is
        # registered as a hold before the proof decides whether a release may
        # close the client that work goes through.
        _drain_pending_forgets_locked()
        ok, why, claimed = _agent_holds_instances_locked(agent)
        if not ok:
            return False, why, []
        # #8613 round 2 (finding B): the claim removes this agent from the
        # holder sets BEFORE it may wait, so while it waits the entry has no
        # holders, no teardown flag and no queued work — and every prune site
        # deletes an entry in that shape, process-wide, not just their own. The
        # waiting agent's entry would then be gone when the wait ends, and the
        # proof would answer "not tracked as held by any agent": the client
        # stays open and untracked, which is the #8613 leak again, now confined
        # to the wait window.
        #
        # The claim is therefore MARKED on the entries it is waiting for: a
        # claiming entry is never pruned, and the mark is dropped when the claim
        # settles (success, refusal or timeout). The mark goes on BEFORE this
        # agent leaves the holder sets: leaving allocates (the queued-work check
        # lists the manager's futures), an allocation can run a GC on this
        # thread, and a collected agent's callback runs inline here because the
        # lock is reentrant — its prune would take an entry that has just lost
        # its last holder and is not yet marked, and the proof below would
        # answer "not tracked", releasing and retaining nothing.
        claiming_entries: list[_InstanceHolders] = []
        for provider in claimed:
            entry = _live_holders_entry(provider)
            if entry is not None and entry not in claiming_entries:
                entry.claiming += 1
                claiming_entries.append(entry)
        try:
            # This agent is leaving: it must not count as its own co-holder, and a
            # queued save of its own must keep holding what it writes through.
            manager = _agent_manager(agent)
            for entry in claiming_entries:
                _forget_holder_locked(entry, id(agent), manager)
            while True:
                ok, why, wait_for = _last_provider_holder_locked(agent, self_dropped=True)
                if ok:
                    for provider in claimed:
                        entry = _live_holders_entry(provider)
                        if entry is not None:
                            entry.teardown = True
                    return True, "", claimed
                if retain is not None and wait_for in ("own_work", "work"):
                    # #8613 (review 5722370551): the ONLY thing holding the
                    # release back is queued memory work (this agent's own or a
                    # departed agent's). The cleanup intent is kept on the
                    # entries, in this same critical section, so it cannot be
                    # lost between the refusal and the moment that work drains;
                    # the caller arms it (``_arm_release_intent``).
                    for entry in claiming_entries:
                        entry.intent = retain
                    retain.retained = True
                    retain.deferred_for = "work"
                    return False, why, []
                if wait_for != "fence":
                    # #8613 round 3 (finding 3): the releasing agent's own queued
                    # write is outstanding. The release path drains it BEFORE the
                    # claim (see ``_drain_releasing_work``); work that is still
                    # running here was queued after that drain, and the teardown
                    # is refused: the provider keeps its worker and the write
                    # lands. Waiting here would hold the holder lock — the hot
                    # path every construction needs — so it does not.
                    return False, why, []
                if _skip_fence_wait.get():
                    # #8613 round 2 (the maintainer's note): this caller must
                    # not block — it holds the per-session agent lock, and the
                    # fence wait would stall the user's next turn. Refuse the
                    # release now, exactly as the wait's timeout does, and no
                    # client is closed under a live agent.
                    #
                    # #8817: the cleanup intent is KEPT, in this same critical
                    # section, and the release runs once the construction has
                    # finished (``_end_acquisition``). Dropping it here left the
                    # instance out of the holder table with its writer alive
                    # until restart whenever the construction got a different
                    # instance, since no later boundary reaches it.
                    kept = _retain_for_fence_locked(retain, claiming_entries)
                    logger.info(
                        "[webui] #8613: not waiting for a construction of the same "
                        "profile before releasing a memory provider (%s). %s",
                        why, _fence_refusal_outcome(kept),
                    )
                    return False, why, []
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    kept = _retain_for_fence_locked(retain, claiming_entries)  # #8817
                    logger.warning(
                        "[webui] #8613: waited %.0fs for a construction of the same profile to "
                        "finish before releasing a memory provider and gave up (%s). %s",
                        _ACQUISITION_FENCE_WAIT_S, why, _fence_refusal_outcome(kept),
                    )
                    return False, why, []
                _provider_holders_cond.wait(remaining)
        finally:
            for entry in claiming_entries:
                if entry.claiming > 0:
                    entry.claiming -= 1


def _settle_last_held_teardown(agent: Any, claimed: list, *, released: bool = False) -> list[str]:
    """Clear the teardown claim, drop ``agent`` from the holder sets, and name the
    instances that gained a holder while the release ran — the one race this design
    cannot close (an agent constructed for the same profile binds the instance
    mid-release, or leaves queued work on it). Reported so it is visible, not
    silently swallowed.

    ``released`` marks the instances the caller DID tear down: the release drained
    this agent's own queued memory work, so its work hold is spent. It is False
    when the release was abandoned before it ran (an unscopable profile home, a
    manager whose teardown was already claimed), so the next boundary may retry
    with the instance still live.
    """
    raced: list[str] = []
    agent_pid = id(agent)
    manager = _agent_manager(agent)
    webui_fences = _claim_seam_available() is not True
    with _provider_holders_lock:
        # #8613 round 3 (finding 1): apply the queued forgets before the table is
        # read, so a collected agent is not counted as a live holder here.
        _drain_pending_forgets_locked()
        for provider in claimed:
            entry = _live_holders_entry(provider)
            if entry is None:
                continue
            entry.teardown = False
            entry.holders.pop(agent_pid, None)
            if released:
                # #8613 round 2 (finding A): the spent work hold belongs to the
                # released agent's MANAGER. Removing it by the agent's id()
                # removed a DIFFERENT agent's hold when the id had been
                # recycled, which let that agent's queued save lose the
                # instance.
                for pid, manager_ref in list(entry.work.items()):
                    held_manager = manager_ref()
                    if manager is not None and held_manager is manager:
                        del entry.work[pid]
            if any(h.agent() is not None for h in entry.holders.values()) or entry.work:
                raced.append(str(getattr(provider, "name", "?")))
        # An instance nobody holds any more, with no teardown and no queued work to
        # protect, leaves the table — unless a release is claiming it right now
        # (#8613 round 2, finding B).
        for key, entry in list(_provider_holders.items()):
            if not entry.holders and not entry.teardown and not entry.work and not entry.claiming and entry.intent is None:
                _provider_holders.pop(key, None)
        if webui_fences:
            # #8804: a construction on an Agent without ``claim_instance`` may be
            # waiting in ``_reserve_acquisition`` for this teardown to settle.
            _provider_holders_cond.notify_all()
    return raced


def release_unowned_memory_provider(agent: Any, *, session_id: str, owner_home: str = "") -> bool:
    """Stop the memory provider of an evicted ``agent`` whose session owner cannot be
    resolved, WITHOUT a session end (#8613). True when the release ran.

    ``call_as_session_owner`` refuses ``shutdown_memory_provider`` for such an agent
    (session end is a commit, and an unknown owner never commits), which also skipped
    the teardown inside it, so the provider's ``mem-sync`` writer and its clients
    lingered until the WebUI process exited. This runs only the teardown, and only
    when ALL of the following hold:

    * the owner is still unresolvable; a refusal for any other reason (provider bound
      to another profile, no home override) leaves the provider alone;
    * the manager has ``release_soft`` AND ``claim_teardown`` (Agent 2026-10-19+), or
      neither (an older Agent, #8804). ``release_soft`` is not a pure cancellation: it
      claims teardown once (a later session-end shutdown of this manager then runs
      neither ``on_session_end`` nor a second ``shutdown_all``), waits up to 10 s for
      writes the agent ALREADY queued (they run in the provider's bound profile,
      ``in_owner_home``), then runs ``shutdown_all`` (bounded drain + each provider's
      ``shutdown()``). An older manager has nothing softer than ``shutdown_all``: it
      runs after this agent's own queued work was drained, and the agent (or the one
      a deferred ``_ReleaseIntent`` stands in for) is then marked shut down so its own
      ``shutdown_memory_provider`` cannot run a session end or a second teardown
      (``_mark_legacy_shut_down``). Neither path calls ``on_session_end`` or adds a
      write of its own. A manager with only part of the soft-release seam is left
      alone;
    * the profile home the provider is bound to is known: the manager reports it
      (``_bound_scope``, Agent 2026-10-17+), or, on an older Agent that reports
      nothing, it is the home the WebUI built the agent under (``_record_build_home``;
      such an Agent keeps an external provider only while one profile is served,
      #8585). The release runs with that home held as the context-local home, so
      anything resolved during the drain or a provider's ``shutdown()`` names the
      provider's own profile, never the ambient env mirror (another profile's);
    * this agent is the LAST holder of every provider instance it serves
      (``agent_is_last_provider_holder``, claimed atomically with
      ``_claim_last_held_teardown``). A provider that hands one instance to every
      agent of a profile is shared by them all, and a manager-level teardown shuts
      that instance down under the agents still using it — which is why an earlier
      version of this fix was reverted (#8636). While another agent of the same
      profile holds the instance, while a construction is acquiring it right now
      (``_inflight_homes``: the Agent initialises the provider inside its
      constructor, before the holder can be recorded), or while an agent that
      already left still has queued memory work on it, nothing runs and the
      provider keeps its worker; that agent's own session boundary releases it.
      A construction in flight is waited for, bounded, rather than refused: the
      build either registers a holder or acquires nothing. An instance this
      release DID tear down is claimed again — and re-initialised — by the next
      construction, exactly as after an owned session-end teardown. On an Agent
      without ``claim_instance`` a construction that starts while the teardown runs
      waits for it to settle (``_reserve_acquisition``, #8804), because such an Agent
      cannot refuse the instance itself.
    * the manager's teardown has not been claimed already: ``release_soft`` is a
      no-op when it has, and a no-op must not be reported as a release (the
      provider's worker is still alive).

    Otherwise nothing runs and it is logged once per session: that provider
    lingers until the WebUI restarts, as before — except when the ONLY reason is
    queued memory work (this agent's own write, or a departed agent's): then the
    release is retained (``_ReleaseIntent``) and runs on its own once that work
    has drained, without another agent's session boundary (review 5722370551).
    """
    manager = getattr(agent, "_memory_manager", None)
    if manager is None:
        return False
    owner = str(owner_home or "").strip() or session_owner_profile_home(session_id)
    if owner and profile_home_key(owner):
        return False
    release = getattr(manager, "release_soft", None)
    legacy = _is_legacy_manager(manager)
    if not legacy and not (callable(release) and callable(getattr(manager, "claim_teardown", None))):
        _log_once(
            "release-soft-missing",
            "[webui] #8613: this Hermes Agent's memory manager has only part of the soft "
            "release (MemoryManager.release_soft/claim_teardown), so an evicted agent "
            "whose session owner cannot be resolved keeps its provider worker until the "
            "WebUI restarts.",
        )
        return False
    state, bound_key = _bound_memory_home_key(agent)
    bound = getattr(manager, "_bound_scope", None)
    bound_home = str(bound.get("hermes_home") or "") if isinstance(bound, dict) else ""
    if legacy and not bound_home:
        # #8804: an Agent from before 2026-10-17 reports no bound home. Its provider
        # is bound to the home the WebUI built it under (one profile only, #8585).
        build_home = str(getattr(agent, _BUILD_HOME_ATTR, "") or "")
        build_key = profile_home_key(build_home) if build_home else None
        if build_key and (state != "bound" or bound_key == build_key):
            state, bound_home, bound_key = "bound", build_home, build_key
    if state != "bound" or not bound_home or profile_home_key(bound_home) != bound_key:
        _log_once(
            "release-unbound",
            "[webui] #8613: an evicted agent whose session owner cannot be resolved does "
            "not report the profile home its memory provider is bound to, so the "
            "provider is not released; it keeps its worker until the WebUI restarts.",
        )
        return False
    # #8613: never tear down an instance another live agent still holds, one a
    # construction is acquiring, or one a departed agent still has queued memory
    # work on. The claim waits for a construction in flight, bounded, so the
    # instance is not dropped out of the table with its writer alive.
    #
    # #8613 round 3 (finding 3): the RELEASING agent's own queued work is
    # drained FIRST. The Agent's ``release_soft`` closes the providers after
    # bounded waits (a 10 s flush and a 5 s drain), so a save still running
    # past them is closed under — "Cannot send a request, as the client has
    # been closed" — and the save is lost. When the work does not drain in
    # time the teardown is refused: the provider keeps its worker, the save
    # lands, and the next session boundary releases it.
    #
    # The proof runs BEFORE the drain, so the wait only happens when this
    # release would really tear the provider down. A shared instance — another
    # agent of the profile still holds it — is refused by the proof anyway, and
    # waiting for a drain nobody needs would stall the eviction for nothing.
    ok, why = agent_is_last_provider_holder(agent)
    if ok and not _drain_releasing_work(manager):
        _log_once(
            f"release-own-work:{session_id}",
            "[webui] #8613: the memory provider of evicted session %s was NOT released: "
            "its own queued memory write has not drained, and releasing now would close "
            "the client under that write. The provider keeps its worker until the write "
            "has landed; the release then runs on its own.",
            session_id,
        )
        # Not returned here (#8613, review 5722370551): the claim below refuses on
        # that same outstanding write and RETAINS the cleanup intent, so the
        # release runs once the write drains instead of waiting for a session
        # boundary of some later agent that may never come.
    try:
        departing = weakref.ref(agent)
    except TypeError:
        departing = None
    intent = _ReleaseIntent(manager, bound_home, bound_key, session_id, agent_ref=departing)
    ok, why, claimed = _claim_last_held_teardown(agent, retain=intent)
    if not ok:
        armed = _arm_release_intent(intent)
        if armed == "unhookable":
            _start_intent_retry(intent, poll=True)
        if armed != "unretained":
            _log_once(
                f"release-deferred:{session_id}",
                "[webui] #8613: the memory provider of evicted session %s was NOT released "
                "yet: %s. The release is kept and runs on its own once that memory work "
                "has drained or that construction has finished.",
                session_id, why,
            )
            return False
        if intent.retained:
            # #8817 (senior r1, nit 1): the refusal kept the intent, and before
            # this arm ran a retry started by ``_end_acquisition`` claimed it (or
            # a new holder of the instance took it over). Nothing was lost, so
            # "until the WebUI restarts" would be false.
            _log_once(
                f"release-deferred:{session_id}",
                "[webui] #8613: the memory provider of evicted session %s was not released "
                "at eviction: %s. The release was kept and has since been taken over, by its "
                "retry or by a new holder of the instance.",
                session_id, why,
            )
            return False
        # Keyed per session: a static key logged only the FIRST refusal in the
        # process, so every later evicted session was silent about its provider.
        _log_once(
            f"release-shared:{session_id}",
            "[webui] #8613: the memory provider of evicted session %s was NOT released: "
            "%s. The provider keeps its worker until the last agent holding it reaches a "
            "session boundary, or until the WebUI restarts.",
            session_id, why,
        )
        return False
    return _run_claimed_release(
        agent, claimed, manager=manager, bound_home=bound_home, bound_key=bound_key,
        session_id=session_id,
    )


# The tail of a deferred release's log line, by what it was deferred for.
_DEFERRED_RELEASE_SUFFIX = {
    "work": " once its queued memory work had drained",
    "construction": " once a construction of the same profile had finished",
}


def _run_claimed_release(holder: Any, claimed: list, *, manager: Any, bound_home: str,
                         bound_key: Optional[str], session_id: str, deferred: str = "") -> bool:
    """Run a release whose teardown ``claimed`` (``_claim_last_held_teardown`` or
    ``_claim_release_intent``) already holds: ``release_soft`` in the provider's
    bound profile home, never a session end, then settle the claim. True when the
    release ran. ``holder`` is the departing agent, or the ``_ReleaseIntent``
    standing in for it."""
    restore, reason = _enter_profile_home(bound_home, bound_key)
    if reason is not None:
        _log_once(
            "release-scope",
            "[webui] #8613: an evicted agent's memory provider was not released (%s); it "
            "keeps its worker until the WebUI restarts.",
            reason,
        )
        _settle_last_held_teardown(holder, claimed)
        return False
    released = False
    try:
        # #8613 (low): ``release_soft`` claims the teardown itself and does nothing
        # when another teardown of this manager already claimed it. Reporting that
        # as a release would retire the instance with its worker still alive.
        legacy = _is_legacy_manager(manager)
        if getattr(manager, "_teardown_claim", None) or (legacy and getattr(manager, "_shutting_down", False)):
            _log_once(
                f"release-claimed:{session_id}",
                "[webui] #8613: the memory provider of evicted session %s was NOT released: "
                "a teardown of its memory manager was already claimed, so the release "
                "would have done nothing. The provider keeps its worker until the WebUI "
                "restarts.",
                session_id,
            )
            return False
        if legacy:
            # #8804: an Agent from before 2026-10-19 has no soft release. Its
            # ``shutdown_all`` is the teardown ``release_soft`` ends with (bounded
            # drain, then each provider's ``shutdown()``). The releasing manager's
            # queued work was drained before the claim, and nothing here runs
            # ``on_session_end``.
            manager.shutdown_all()
            _mark_legacy_shut_down(holder)
        else:
            manager.release_soft()
        released = True
    finally:
        restore()
        raced = _settle_last_held_teardown(holder, claimed, released=released)
    if raced:
        logger.warning(
            "[webui] #8613: provider(s) %s gained a holder while the release of evicted "
            "session %s was running; that holder may have been shut down with them.",
            raced, session_id,
        )
    logger.info(
        "[webui] #8613: released the memory provider of evicted session %s without "
        "session end (its owner cannot be resolved)%s",
        session_id, _DEFERRED_RELEASE_SUFFIX.get(deferred, ""),
    )
    return True


# ── a release refused only on queued work runs once that work drains ─────────
#
# Review 5722370551: "retain the outstanding-work release intent until a
# last-holder save drains; requiring a third agent's later build/evict to
# trigger cleanup leaves the original leak unresolved." The last holder's
# release refused ONLY because queued memory work still writes through the
# instance (its own, or a departed agent's work hold) keeps a ``_ReleaseIntent``
# on the entries; a done-callback on that work starts the release once the work
# has drained. The intent runs through the same proof, claim and settlement as
# the eviction itself, so it inherits every fence: a live holder (which takes
# the instance over), a construction in flight (waited for, bounded), another
# teardown, and any other queued work.

# An intent whose queued work cannot be hooked (unreadable futures) is re-checked
# this often, and dropped after the limit: the provider then keeps its worker
# until the WebUI restarts, as it did before.
_INTENT_POLL_INTERVAL_S = 0.5
_INTENT_POLL_LIMIT_S = 300.0


class _ReleaseIntent:
    """A last-holder release kept until the queued memory work that refused it
    has drained (#8613, review 5722370551).

    It stands in for the agent that departed: ``_memory_manager`` is that agent's
    manager, held STRONGLY because the agent is gone and the release runs through
    the manager. The last-holder proof (``_last_provider_holder_locked``) and the
    settlement (``_settle_last_held_teardown``) therefore run for it unchanged;
    ``id(intent)`` is never a holder key, so every live agent counts as another
    holder.

    Retained by ``_claim_last_held_teardown`` in the critical section that
    refused; armed by ``_arm_release_intent`` (queued work) or started by
    ``_end_acquisition`` (a construction in flight, #8817); run by
    ``_run_release_intent``. Dropped when a new holder registers
    (``register_provider_holders``), when a newer intent replaces it, when the
    proof refuses for any reason but queued work or a construction in flight, or
    once the release has run.
    """

    __slots__ = ("_memory_manager", "bound_home", "bound_key", "session_id", "armed", "agent_ref",
                 "fence_homes", "retained", "deferred_for")

    def __init__(self, manager: Any, bound_home: str, bound_key: Optional[str], session_id: str,
                 agent_ref: Optional[Callable[[], Any]] = None):
        self._memory_manager = manager
        self.bound_home = bound_home
        self.bound_key = bound_key
        self.session_id = session_id
        # #8804: a PLAIN weak reference to the departed agent (no holder-table
        # callback, never a strong one), so a deferred legacy release can mark it
        # shut down if it is still alive (``_mark_legacy_shut_down``).
        self.agent_ref = agent_ref
        # Futures already carrying this intent's done-callback, by identity (the
        # future itself is kept so a reused id() can never match). Holder lock.
        self.armed: dict = {}
        # #8817: the profile homes whose constructions in flight refused this
        # release, while it waits for them to finish (``_end_acquisition`` then
        # starts the retry). None when it is not waiting on a construction.
        # Holder lock.
        self.fence_homes: Optional[frozenset] = None
        # True once a refusal has kept this intent on the entries, so a caller
        # that finds no entry carrying it any more knows a retry already took
        # it over rather than that it was never kept. Holder lock.
        self.retained = False
        # What the release was last deferred for, for its log line: "work"
        # (queued memory work) or "construction" (#8817). Holder lock.
        self.deferred_for = "work"


def _outstanding_futures(manager: Any) -> list:
    """``manager``'s queued memory work that has not finished, read without any
    future's lock (``_future_is_done``); ``[]`` when it cannot be read."""
    futures = getattr(manager, "_background_futures", None) if manager is not None else None
    if not isinstance(futures, dict) or not futures:
        return []
    try:
        return [future for future in list(futures) if not _future_is_done(future)]
    except Exception:
        return []


def _drop_intent_locked(intent: _ReleaseIntent) -> None:
    """Retire ``intent`` from every entry still carrying it. Holder lock held."""
    intent.armed.clear()
    intent.fence_homes = None
    for key, entry in list(_provider_holders.items()):
        if entry.intent is intent:
            entry.intent = None
            if not entry.holders and not entry.teardown and not entry.work and not entry.claiming:
                _provider_holders.pop(key, None)


def _arm_release_intent(intent: _ReleaseIntent) -> str:
    """Start ``intent`` once the queued work holding it back has drained.

    ``"unretained"`` when no entry carries it (the refusal was not about queued
    work: nothing to arm), ``"hooked"`` when a done-callback now waits on that
    work, ``"fenced"`` when it waits for a construction in flight instead
    (``_end_acquisition`` starts it, #8817), ``"unhookable"`` when nothing could
    be hooked: the caller then re-checks it itself, bounded
    (``_run_release_intent`` with ``poll``).

    Never called with the holder lock held: ``add_done_callback`` takes the
    future's own condition lock, and holder lock -> future lock is the inversion
    round 2 (finding C) removed. The callback only starts a thread: it runs on the
    manager's single writer thread, where the release's own flush would wait on
    that same thread.
    """
    with _provider_holders_lock:
        _drain_pending_forgets_locked()
        # Snapshot first (Codex r2): a GC callback run by an allocation here
        # re-enters the holder RLock on this thread and may prune the table.
        entries = [entry for entry in list(_provider_holders.values()) if entry.intent is intent]
        if not entries:
            return "unretained"
        if intent.fence_homes:
            # #8817: kept because a construction in flight refused it, not for
            # queued work. ``_end_acquisition`` starts the retry when that
            # construction finishes; there is nothing to hook here.
            return "fenced"
        managers = {id(intent._memory_manager): intent._memory_manager}
        for entry in entries:
            for manager_ref in entry.work.values():
                held = manager_ref()
                if held is not None:
                    managers[id(held)] = held
    hooks = []
    for manager in managers.values():
        pending = _outstanding_futures(manager)
        if pending:
            # One writer, first in first out: its last queued task finishes last.
            hooks.append(pending[-1])
    if not hooks:
        return "unhookable"
    # Arm each future once per intent. A retry that still finds queued work
    # re-arms, and adding the callback again to a future that already carries it
    # doubled the retries at every completion (2**N - 1 threads for N managers
    # draining in turn). Reserved under the holder lock, attached outside it
    # (the lock order above); a future already armed still holds the retry.
    with _provider_holders_lock:
        fresh = [future for future in hooks if intent.armed.get(id(future)) is not future]
        for future in fresh:
            intent.armed[id(future)] = future
    for index, future in enumerate(fresh):
        try:
            future.add_done_callback(lambda _done, intent=intent: _start_intent_retry(intent))
        except Exception:
            # Un-reserve this future and every one not attached yet, so a later
            # arm hooks them instead of trusting a callback that never went on.
            with _provider_holders_lock:
                for unattached in fresh[index:]:
                    intent.armed.pop(id(unattached), None)
            return "unhookable"
    return "hooked"


def _start_intent_retry(intent: _ReleaseIntent, *, poll: bool = False) -> None:
    """Run ``intent`` on its own short-lived thread (see ``_arm_release_intent``)."""
    try:
        threading.Thread(
            target=_run_release_intent, args=(intent, poll),
            name="webui-8613-deferred-release", daemon=True,
        ).start()
    except Exception:
        # The intent is NOT dropped here (#8817 Codex r2): retries of one intent
        # can be started by several sources (each armed manager's queued work,
        # a construction's end), and a sibling retry that did start may not have
        # claimed it yet. Retiring it would cancel that retry's release. A
        # failed start only leaves the release to whichever retry runs next.
        logger.warning(
            "[webui] #8613: the deferred release of the memory provider of evicted session %s "
            "could not start; the provider keeps its worker until the WebUI restarts.",
            intent.session_id, exc_info=True,
        )


def _claim_release_intent(intent: _ReleaseIntent) -> tuple[str, str, list]:
    """The proof and the teardown claim for a retained intent, in ONE critical
    section, exactly like ``_claim_last_held_teardown``: ``("claimed", "",
    instances)``, ``("work", why, [])`` while queued work still holds it, or
    ``("dropped", why, [])`` once it is retired. A construction in flight for the
    profile is waited for, bounded, on the same Condition."""
    deadline = time.monotonic() + _ACQUISITION_FENCE_WAIT_S
    with _provider_holders_lock:
        while True:
            _drain_pending_forgets_locked()
            instances = _provider_instances(intent)
            entries = [_live_holders_entry(provider) for provider in instances]
            if not instances or any(entry is None or entry.intent is not intent for entry in entries):
                # A new holder took the instance over, a newer intent replaced
                # this one, another retry of it already claimed it, or the
                # instance is no longer tracked (its release already ran).
                if not instances or any(entry is None for entry in entries):
                    gone = "the provider instance is no longer tracked; its release already ran"
                elif any(entry.teardown for entry in entries):
                    gone = "another retry of this release is already running it"
                else:
                    gone = "another agent holds the provider instance now, or a newer release replaced this one"
                _drop_intent_locked(intent)
                return "dropped", gone, []
            ok, why, wait_for = _last_provider_holder_locked(intent, self_dropped=True)
            if ok:
                for entry in entries:
                    entry.intent = None
                    entry.teardown = True
                intent.armed.clear()
                return "claimed", "", instances
            if wait_for in ("own_work", "work"):
                intent.deferred_for = "work"
                return "work", why, []
            if wait_for == "fence":
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    _provider_holders_cond.wait(remaining)
                    continue
                # #8817: the construction is still in flight. Give up the wait,
                # not the release: the intent waits for that construction to
                # finish (``_end_acquisition`` restarts it), in this same
                # critical section, so the end cannot be missed.
                intent.fence_homes = frozenset(entry.home_key for entry in entries if entry is not None)
                intent.deferred_for = "construction"
                return "fenced", why, []
            _drop_intent_locked(intent)
            return "dropped", why, []


def _run_release_intent(intent: _ReleaseIntent, poll: bool = False) -> None:
    """Release the provider of a retained intent once its queued work drained.
    ``poll`` marks a retry started because the work could not be hooked; every
    retry re-checks the same way, so it only names why the thread started."""
    give_up = time.monotonic() + _INTENT_POLL_LIMIT_S
    while True:
        state, why, claimed = _claim_release_intent(intent)
        if state == "claimed":
            _run_claimed_release(
                intent, claimed, manager=intent._memory_manager, bound_home=intent.bound_home,
                bound_key=intent.bound_key, session_id=intent.session_id,
                deferred=intent.deferred_for or "work",
            )
            return
        if state == "fenced":
            # #8817: still a construction in flight; ``_end_acquisition`` starts
            # the next retry when it finishes.
            logger.info(
                "[webui] #8613: the deferred release of the memory provider of evicted "
                "session %s waits for a construction of the same profile: %s.",
                intent.session_id, why,
            )
            return
        if state != "work":
            logger.info(
                "[webui] #8613: the deferred release of the memory provider of evicted "
                "session %s is not run: %s.",
                intent.session_id, why,
            )
            return
        # Still queued work: wait for it again. A hooked future starts a new
        # retry when it finishes, so this one is done; work that cannot be
        # hooked is re-checked here, bounded.
        if _arm_release_intent(intent) != "unhookable":
            return
        if time.monotonic() >= give_up:
            with _provider_holders_lock:
                _drop_intent_locked(intent)
            logger.warning(
                "[webui] #8613: the memory provider of evicted session %s was NOT released: "
                "its queued memory work did not drain within %.0fs. The provider keeps its "
                "worker until the WebUI restarts.",
                intent.session_id, _INTENT_POLL_LIMIT_S,
            )
            return
        time.sleep(_INTENT_POLL_INTERVAL_S)
