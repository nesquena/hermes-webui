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
        agent = agent_cls(**agent_kwargs)
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
        return agent, True
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
