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


def serves_single_profile(target_key: Optional[str]) -> bool:
    """True only when this WebUI process provably serves ONE profile home.

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
        _profile_homes_seen.add(target_key)
        if len(_profile_homes_seen) > 1:
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
            return agent, stable_for_process
        # #8613: reserve the acquisition BEFORE the provider is initialised. The
        # Agent initialises its native manager (and claims the shared instance)
        # inside ``agent_cls(...)``; only once it has returned can the holder be
        # recorded. The reservation is held across that whole window — through the
        # registration below and through an aborted build alike — so the
        # last-holder proof can never prove the old agent alone while this
        # construction is acquiring the same instance.
        _reserve_acquisition(target_key or "")
        try:
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
            register_provider_holders(agent, home_key=target_key or "")
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

    __slots__ = ("ref", "home_key", "holders", "teardown", "work")

    def __init__(self, ref: Callable[[], Any], home_key: str = ""):
        self.ref = ref                                    # weak reference: identity, never id() alone
        self.home_key = home_key                          # profile home key the instance serves
        self.holders: dict[int, _AgentHolder] = {}        # id(agent) -> its hold
        self.teardown = False                             # a teardown of this instance is running
        # id(agent) -> weak reference to its manager, for an agent that LEFT the
        # holder set while its manager still had queued memory work. That work
        # still writes through this instance, so it holds the instance exactly
        # like a live holder does — until the work drains, at which point the
        # weak reference dies with the manager and the hold evaporates.
        self.work: dict[int, Callable[[], Any]] = {}


_provider_holders: dict[int, _InstanceHolders] = {}
# Reentrant: a weak-reference callback can fire from a GC that runs inside the lock.
_provider_holders_lock = threading.RLock()
# The SAME lock as a Condition, so a release can WAIT for the constructions it
# fences instead of refusing and dropping the instance out of the table (#8613
# should-fix 5). ``wait()`` releases the lock, so no thread is blocked by it.
_provider_holders_cond = threading.Condition(_provider_holders_lock)


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


def _reserve_acquisition(home_key: str) -> None:
    """Reserve the acquisition of ``home_key``'s provider instances BEFORE they
    are initialised (#8613). Paired with ``_end_acquisition`` in a ``finally``."""
    if not home_key:
        return
    with _provider_holders_lock:
        _inflight_homes[home_key] = _inflight_homes.get(home_key, 0) + 1


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


def _manager_has_outstanding_work(manager: Any) -> bool:
    """True when ``manager`` still has queued or running memory work (#8613
    must-fix 1): a background save that has not drained yet. This is the Agent's
    own executor bookkeeping, read defensively; an unreadable answer counts as
    work pending, because a provider that keeps running is the status quo while
    a save that goes through a client another agent's teardown closed is not."""
    if manager is None:
        return False
    futures = getattr(manager, "_background_futures", None)
    if not isinstance(futures, dict) or not futures:
        return False
    try:
        return any(not future.done() for future in list(futures))
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
    """Weak-reference callback: the instance itself is gone, so its holder set is."""
    with _provider_holders_lock:
        entry = _provider_holders.get(inst_pid)
        if entry is not None and entry.ref is ref:
            del _provider_holders[inst_pid]


def _forget_agent_holder(agent_pid: int, ref: Callable[[], Any]) -> None:
    """Weak-reference callback: a collected agent stops holding every instance."""
    with _provider_holders_lock:
        for key, entry in list(_provider_holders.items()):
            holder = entry.holders.get(agent_pid)
            if holder is not None and holder.agent is ref:
                del entry.holders[agent_pid]
                # #8613 must-fix 1: the agent is gone, but a save its manager
                # already queued is not. That manager is still alive — the queued
                # work holds it — so the hold survives as queued work, weakly, and
                # evaporates once the work has drained.
                if _manager_has_outstanding_work(holder.manager()):
                    entry.work[agent_pid] = holder.manager
            # An entry nobody holds any more, with no teardown and no queued work
            # to protect, leaves the table.
            if not entry.holders and not entry.teardown and not entry.work:
                del _provider_holders[key]


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
                _provider_holders[inst_pid] = entry
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
            entry.holders[agent_pid] = _AgentHolder(_weak_agent_ref(agent), manager_ref)
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
        for key, entry in list(_provider_holders.items()):
            _forget_holder_locked(entry, agent_pid, manager)
            # An entry nobody holds any more, with no teardown and no queued work
            # to protect, leaves the table.
            if not entry.holders and not entry.teardown and not entry.work:
                del _provider_holders[key]


def _forget_holder_locked(entry: _InstanceHolders, agent_pid: int, manager: Any) -> None:
    """Drop ``agent_pid`` from ``entry``'s holders, with ``_provider_holders_lock``
    held, keeping a queued-work hold when its ``manager`` still has outstanding
    memory work (#8613 must-fix 1)."""
    holder = entry.holders.pop(agent_pid, None)
    if holder is None:
        return
    if _manager_has_outstanding_work(manager):
        entry.work[agent_pid] = holder.manager


def _last_provider_holder_locked(agent: Any, *, self_dropped: bool = False) -> tuple[bool, str, bool]:
    """``agent_is_last_provider_holder`` with ``_provider_holders_lock`` already
    held. Returns ``(ok, why, fenced)``; ``fenced`` is True when the ONLY reason
    for the refusal is a construction in flight for the instance's profile home,
    the one reason a bounded wait can clear (see ``_claim_last_held_teardown``).

    ``self_dropped`` is for the claim path, which has already removed ``agent``
    from the holder sets inside the same critical section: the agent is then no
    longer expected to be recorded, and its own queued work does not count
    against it — the release drains that itself.
    """
    if agent is None:
        return True, "", False
    instances = _provider_instances(agent)
    if not instances:
        return True, "", False  # nothing held: no other agent can be broken by a release
    agent_pid = id(agent)
    for provider in instances:
        name = getattr(provider, "name", "?")
        entry = _live_holders_entry(provider)
        if entry is None:
            return False, f"provider {name!r} is not tracked as held by any agent", False
        if not self_dropped:
            mine = entry.holders.get(agent_pid)
            if mine is None or mine.agent() is not agent:
                # A claim that already dropped this agent (a release in flight for
                # it) must answer as the teardown it is, not as an untracked hold.
                if entry.teardown:
                    return False, f"a teardown of provider {name!r} is already running", False
                return False, f"provider {name!r} is not tracked as held by this agent", False
        # A collected holder does not count: its weak-reference callback removes it,
        # and a stale reference must not pin an instance nobody uses any more.
        others = [h for key, h in entry.holders.items() if key != agent_pid and h.agent() is not None]
        if others:
            return False, (
                f"provider {name!r} of profile home {entry.home_key!r} is still held by "
                f"{len(others)} other agent(s)"
            ), False
        if entry.teardown:
            return False, f"a teardown of provider {name!r} is already running", False
        # #8613 must-fix 1: an agent that already left the holder set — evicted or
        # collected — while its manager still had queued memory work holds this
        # instance exactly like a live holder: that work still writes through it.
        # Its own work is excluded: the release drains the agent it releases.
        for pid, manager_ref in list(entry.work.items()):
            if pid == agent_pid:
                continue
            if _manager_has_outstanding_work(manager_ref()):
                return False, (
                    f"provider {name!r} of profile home {entry.home_key!r} still has queued "
                    f"memory work from a departed agent"
                ), False
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
            ), True
    return True, "", False


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
        ok, why, _fenced = _last_provider_holder_locked(agent)
    return ok, why


# How long a release waits for a construction of the same profile home to finish
# before it gives up and refuses (#8613 should-fix 5). The Agent initialises the
# provider inside its constructor, so a build that has not returned yet may still
# be about to acquire the very instance this release would close; refusing there
# instead of waiting drops the instance out of the holder table with its writer
# alive, which is the leak #8613 exists to close. ``Condition.wait`` releases the
# lock while it waits, so no other thread is blocked by it.
_ACQUISITION_FENCE_WAIT_S = 15.0


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


def _claim_last_held_teardown(agent: Any) -> tuple[bool, str, list]:
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
        ok, why, claimed = _agent_holds_instances_locked(agent)
        if not ok:
            return False, why, []
        # This agent is leaving: it must not count as its own co-holder, and a
        # queued save of its own must keep holding what it writes through.
        manager = _agent_manager(agent)
        for provider in claimed:
            entry = _live_holders_entry(provider)
            if entry is not None:
                _forget_holder_locked(entry, id(agent), manager)
        while True:
            ok, why, fenced = _last_provider_holder_locked(agent, self_dropped=True)
            if ok:
                for provider in claimed:
                    entry = _live_holders_entry(provider)
                    if entry is not None:
                        entry.teardown = True
                return True, "", claimed
            if not fenced:
                return False, why, []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "[webui] #8613: waited %.0fs for a construction of the same profile to "
                    "finish before releasing a memory provider and gave up (%s). The "
                    "provider keeps its worker until the next session boundary.",
                    _ACQUISITION_FENCE_WAIT_S, why,
                )
                return False, why, []
            _provider_holders_cond.wait(remaining)


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
    with _provider_holders_lock:
        for provider in claimed:
            entry = _live_holders_entry(provider)
            if entry is None:
                continue
            entry.teardown = False
            entry.holders.pop(agent_pid, None)
            if released:
                entry.work.pop(agent_pid, None)
            if any(h.agent() is not None for h in entry.holders.values()) or entry.work:
                raced.append(str(getattr(provider, "name", "?")))
        # An instance nobody holds any more, with no teardown and no queued work to
        # protect, leaves the table.
        for key, entry in list(_provider_holders.items()):
            if not entry.holders and not entry.teardown and not entry.work:
                del _provider_holders[key]
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
    * the manager has ``release_soft`` AND ``claim_teardown`` (Agent 2026-10-19+).
      ``release_soft`` is not a pure cancellation: it claims teardown once (a later
      session-end shutdown of this manager then runs neither ``on_session_end`` nor a
      second ``shutdown_all``), waits up to 10 s for writes the agent ALREADY queued
      (they run in the provider's bound profile, ``in_owner_home``), then runs
      ``shutdown_all`` (bounded drain + each provider's ``shutdown()``). It never
      calls ``on_session_end`` and adds no write of its own;
    * the manager reports the profile home it is bound to (``_bound_scope``); the
      release runs with that home held as the context-local home, so anything
      resolved during the drain or a provider's ``shutdown()`` names the provider's
      own profile, never the ambient env mirror (another profile's);
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
      construction, exactly as after an owned session-end teardown.
    * the manager's teardown has not been claimed already: ``release_soft`` is a
      no-op when it has, and a no-op must not be reported as a release (the
      provider's worker is still alive).

    Otherwise nothing runs and it is logged once per session: that provider
    lingers until the WebUI restarts, as before.
    """
    manager = getattr(agent, "_memory_manager", None)
    if manager is None:
        return False
    owner = str(owner_home or "").strip() or session_owner_profile_home(session_id)
    if owner and profile_home_key(owner):
        return False
    release = getattr(manager, "release_soft", None)
    if not (callable(release) and callable(getattr(manager, "claim_teardown", None))):
        _log_once(
            "release-soft-missing",
            "[webui] #8613: this Hermes Agent cannot release a memory provider without a "
            "session end (no MemoryManager.release_soft/claim_teardown; older than "
            "2026-10-19). An evicted agent whose session owner cannot be resolved keeps "
            "its provider worker until the WebUI restarts.",
        )
        return False
    state, bound_key = _bound_memory_home_key(agent)
    bound = getattr(manager, "_bound_scope", None)
    bound_home = str(bound.get("hermes_home") or "") if isinstance(bound, dict) else ""
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
    ok, why, claimed = _claim_last_held_teardown(agent)
    if not ok:
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
    restore, reason = _enter_profile_home(bound_home, bound_key)
    if reason is not None:
        _log_once(
            "release-scope",
            "[webui] #8613: an evicted agent's memory provider was not released (%s); it "
            "keeps its worker until the WebUI restarts.",
            reason,
        )
        _settle_last_held_teardown(agent, claimed)
        return False
    released = False
    try:
        # #8613 (low): ``release_soft`` claims the teardown itself and does nothing
        # when another teardown of this manager already claimed it. Reporting that
        # as a release would retire the instance with its worker still alive.
        if getattr(manager, "_teardown_claim", None):
            _log_once(
                f"release-claimed:{session_id}",
                "[webui] #8613: the memory provider of evicted session %s was NOT released: "
                "a teardown of its memory manager was already claimed, so the release "
                "would have done nothing. The provider keeps its worker until the WebUI "
                "restarts.",
                session_id,
            )
            return False
        release()
        released = True
    finally:
        restore()
        raced = _settle_last_held_teardown(agent, claimed, released=released)
    if raced:
        logger.warning(
            "[webui] #8613: provider(s) %s gained a holder while the release of evicted "
            "session %s was running; that holder may have been shut down with them.",
            raced, session_id,
        )
    logger.info(
        "[webui] #8613: released the memory provider of evicted session %s without "
        "session end (its owner cannot be resolved)",
        session_id,
    )
    return True
