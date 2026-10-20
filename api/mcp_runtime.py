"""Profile-bound view of Hermes Agent's in-process MCP runtime.

The MCP status page, the tool inventory and ``/reload-mcp`` read or reset
Hermes Agent's MCP ledger from the HTTP request thread. Hermes Agent scopes
that ledger per profile (connection key ``(profile_home_key, name)``) only
when the calling task serves a *routed* profile, i.e. its context-local
Hermes-home override differs from the process home. These helpers bind the
request profile's home (without touching ``os.environ``) and check that the
agent's routing decision matches the profile WebUI resolved, so a read never
shows another profile's connection and a reload never resets one.

Ledger helpers below take ``core`` (``tools.mcp_tool``) explicitly and read
its maps with defaults, so an agent that lacks a map degrades to "unscoped"
rather than raising inside a status read.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Optional

logger = logging.getLogger(__name__)

# Scope label reported by the MCP endpoints.
SCOPE_PROFILE = "profile"            # runtime view bound to the request profile
SCOPE_LEGACY = "legacy_process"      # agent predates profile-scoped MCP; process-wide view
SCOPE_UNAVAILABLE = "unavailable"    # profile scope could not be confirmed; runtime hidden


@dataclass(frozen=True)
class McpRuntimeView:
    """How the current context sees Hermes Agent's MCP runtime.

    ``trusted``: runtime reads/writes in this context belong to ``profile_home``.
    ``legacy``: the agent has no profile-scoped MCP ledger (process-wide by name).
    ``registry_scope``: the tool-registry slot owning this profile's MCP tools
    (the profile overlay key when routed, ``None`` for the process profile).
    """

    profile_home: Path
    trusted: bool
    legacy: bool
    registry_scope: Optional[str] = None

    @property
    def scope_label(self) -> str:
        if not self.trusted:
            return SCOPE_UNAVAILABLE
        return SCOPE_LEGACY if self.legacy else SCOPE_PROFILE


def _routing_view(profile_home: Path, override_bound: bool) -> McpRuntimeView:
    try:
        from agent.secret_scope import serves_routed_profile
        from hermes_constants import hermes_home_key
        from tools.registry import registry
    except ImportError:
        return McpRuntimeView(profile_home, trusted=True, legacy=True)
    from api.profiles import get_process_profile_home

    try:
        expected_routed = hermes_home_key(profile_home) != hermes_home_key(get_process_profile_home())
        # Without the override a routed profile cannot be served; with it, the agent must
        # agree. It disagrees when a streaming turn has mirrored this profile's home into
        # os.environ['HERMES_HOME'] on an agent without a pinned process home.
        if expected_routed and not override_bound:
            return McpRuntimeView(profile_home, trusted=False, legacy=False)
        if bool(serves_routed_profile()) != expected_routed:
            return McpRuntimeView(profile_home, trusted=False, legacy=False)
        scope = registry.current_scope_key() if expected_routed else None
    except Exception:
        logger.debug("Failed to resolve MCP runtime scope for %s", profile_home, exc_info=True)
        return McpRuntimeView(profile_home, trusted=False, legacy=False)
    return McpRuntimeView(profile_home, trusted=True, legacy=False, registry_scope=scope)


@contextmanager
def mcp_runtime_scope(purpose: str) -> Generator[McpRuntimeView, None, None]:
    """Bind the active request profile for Hermes Agent MCP runtime calls.

    Installs the context-local Hermes-home override and secret scope for the
    request profile (including the root profile) and restores them on exit,
    including on exceptions. Never mutates ``os.environ``, starts, or probes
    MCP servers.
    """
    from api.profiles import get_active_hermes_home, profile_env_for_active_request_readonly

    profile_home = Path(get_active_hermes_home())
    with profile_env_for_active_request_readonly(
        purpose, logger_override=logger, include_root=True
    ) as override_bound:
        yield _routing_view(profile_home, bool(override_bound))


def accepts_keywords(fn: Callable, *names: str) -> bool:
    """True when *fn*'s signature exposes every keyword in *names*.

    Introspection failures (builtins, C callables, exotic wrappers) count as
    "not supported" so callers fall back to the bare call instead of dropping
    the whole read.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return all(name in params for name in names)


# ── Ledger keys ──────────────────────────────────────────────────────────────
# Hermes Agent's own helpers (``tools.mcp_tool_scope``) are preferred so the key
# shape has one owner; the lambdas only cover agents that predate that module.

def _key_helper(name: str, fallback: Callable) -> Callable:
    try:
        # Full-name import: works with a stubbed module and no ``tools`` package.
        mcp_tool_scope = importlib.import_module("tools.mcp_tool_scope")
    except Exception:
        return fallback
    helper = getattr(mcp_tool_scope, name, None)
    return helper if callable(helper) else fallback


def mcp_key_name(key: Any) -> str:
    """Server name of an MCP connection-ledger key (bare name or ``(scope, name)``)."""
    return str(_key_helper("_key_name", lambda k: k[1] if isinstance(k, tuple) else k)(key))


def mcp_key_scope(key: Any) -> Optional[str]:
    """Owning registry scope encoded in a ledger key (``None`` for a bare key)."""
    return _key_helper("_key_scope", lambda k: k[0] if isinstance(k, tuple) else None)(key)


def ledger_key_owner(core: Any, key: Any) -> Optional[str]:
    """Scope owning the connection under *key*: recorded at adoption, else the key's own."""
    owners = getattr(core, "_server_scope_keys", None)
    if isinstance(owners, dict) and key in owners:
        return owners[key]
    return mcp_key_scope(key)


def ledger_key_serves_view(core: Any, key: Any, view: McpRuntimeView) -> bool:
    """True when the connection under *key* serves the profile in *view*.

    Owned by the view's registry scope, or adopted into it (a routed profile
    sharing an identical connection; ``_server_tool_scopes``). Hermes Agent's
    own launch-profile view (scope ``None``) is process-wide, so this predicate
    is what narrows the root profile to its unscoped connections. Legacy agents
    have one process-wide ledger, so every key counts.
    """
    if view.legacy:
        return True
    if ledger_key_owner(core, key) == view.registry_scope:
        return True
    if view.registry_scope is None:
        return False
    adopters = getattr(core, "_server_tool_scopes", None)
    if not isinstance(adopters, dict):
        return False
    return view.registry_scope in (adopters.get(key) or ())


@dataclass(frozen=True)
class ServedRuntime:
    """Runtime state of one server name as seen from a profile's ledger keys."""

    live_tools: Optional[int] = None   # tool count of a live (session-bearing) connection
    connecting: bool = False
    error: Optional[str] = None
    lazy_tools: Optional[int] = None

    @property
    def live(self) -> bool:
        return self.live_tools is not None


def _served_keys(core: Any, ledger: str, view: McpRuntimeView) -> list:
    keys = getattr(core, ledger, None)
    if not isinstance(keys, (dict, set, frozenset, list, tuple)):
        return []
    return [key for key in list(keys) if ledger_key_serves_view(core, key, view)]


def view_served_runtime(core: Any, view: McpRuntimeView) -> dict[str, ServedRuntime]:
    """Per-name runtime state of the ledger entries serving *view*.

    Caller holds ``core._lock`` or tolerates a racy read (status surfaces).
    """
    live: dict[str, int] = {}
    connecting: set[str] = set()
    errors: dict[str, str] = {}
    lazy: dict[str, int] = {}
    servers = getattr(core, "_servers", None)
    if isinstance(servers, dict):
        for key in _served_keys(core, "_servers", view):
            server = servers.get(key)
            if server is None or getattr(server, "session", None) is None:
                continue  # parked / mid-connect task: retained, not connected
            names = getattr(server, "_registered_tool_names", None)
            if not isinstance(names, (list, tuple, set)):
                names = getattr(server, "_tools", None) or ()
            live[mcp_key_name(key)] = len(names)
    for key in _served_keys(core, "_server_connecting", view):
        connecting.add(mcp_key_name(key))
    error_map = getattr(core, "_server_connect_errors", None)
    if isinstance(error_map, dict):
        for key in _served_keys(core, "_server_connect_errors", view):
            errors[mcp_key_name(key)] = str(error_map.get(key, ""))
    lazy_tools = getattr(core, "_lazy_server_tool_names", None)
    for key in _served_keys(core, "_lazy_server_configs", view):
        count = lazy_tools.get(key) if isinstance(lazy_tools, dict) else None
        lazy[mcp_key_name(key)] = len(count) if isinstance(count, (list, tuple, set)) else 0
    out: dict[str, ServedRuntime] = {}
    for name in set(live) | connecting | set(errors) | set(lazy):
        out[name] = ServedRuntime(
            live_tools=live.get(name),
            connecting=name in connecting,
            error=errors.get(name),
            lazy_tools=lazy.get(name),
        )
    return out


_RUNTIME_ROW_FIELDS = ("connected", "tools", "status", "error", "sampling", "tool_schemas")


def filter_runtime_status_to_view(statuses: Iterable[Any], view: McpRuntimeView) -> list:
    """Rebuild the runtime fields of ``get_mcp_status()`` rows from the keys serving *view*.

    Under a routed scope the agent already filters by owner/adopter; under the
    launch profile (scope ``None``) it reports every profile's connection and
    indexes them by bare name, so the root profile would show "Active, N tools"
    for a routed profile's server it never connected (or its own parked server
    as connected because another profile's same-named one is live). Rows keep
    their config-derived fields (name, transport, disabled) and get
    ``connected`` / ``tools`` / ``status`` / ``error`` from this profile's own
    ledger entries only.
    """
    rows = [row for row in statuses if isinstance(row, dict)]
    if view.legacy or not rows:
        return rows
    try:
        core = importlib.import_module("tools.mcp_tool")
    except Exception:
        return rows
    lock = getattr(core, "_lock", None)
    try:
        if lock is not None:
            with lock:
                served = view_served_runtime(core, view)
        else:
            served = view_served_runtime(core, view)
    except Exception:
        logger.debug("Failed to read MCP ledger ownership for %s", view.profile_home, exc_info=True)
        return []
    out = []
    for row in rows:
        rebuilt = {k: v for k, v in row.items() if k not in _RUNTIME_ROW_FIELDS}
        state = served.get(str(row.get("name")))
        disabled = bool(row.get("disabled")) or row.get("status") == "disabled"
        rebuilt["connected"] = bool(state and state.live)
        rebuilt["tools"] = state.live_tools if state and state.live else (
            state.lazy_tools if state and state.lazy_tools is not None and not disabled else 0
        )
        if state and state.live:
            rebuilt["status"] = "connected"
        elif disabled:
            rebuilt["status"] = "disabled"
        elif state and state.connecting:
            rebuilt["status"] = "connecting"
        elif state and state.error is not None:
            rebuilt["status"] = "failed"
            rebuilt["error"] = state.error
        elif state and state.lazy_tools is not None:
            rebuilt["status"] = "lazy"
        else:
            rebuilt["status"] = "configured"
        out.append(rebuilt)
    return out


_COOLDOWN_LEDGERS = ("_server_connect_retry_after", "_server_connect_failures", "_server_connect_errors")


def clear_profile_connect_cooldowns(core: Any, view: McpRuntimeView) -> None:
    """Forget connect backoff/error state of the servers *view*'s profile owns.

    A server that failed to spawn never reaches ``_servers``, so a scoped
    ``shutdown_mcp_servers(scope=..., names=...)`` (which clears cooldowns for
    the live keys it tears down) leaves its backoff in place and the following
    ``discover_mcp_tools()`` skips it. The process-wide wildcard cleared every
    cooldown; this keeps ``/reload-mcp`` a retry for the profile's own servers
    without touching another owner's backoff. Owned keys only, never adopted ones.
    """
    lock = getattr(core, "_lock", None)
    if lock is None:
        return
    with lock:
        for ledger in _COOLDOWN_LEDGERS:
            entries = getattr(core, ledger, None)
            if not isinstance(entries, dict):
                continue
            for key in [k for k in entries if ledger_key_owner(core, k) == view.registry_scope]:
                entries.pop(key, None)


def registry_tool_owned_by_view(registry, tool_name: str, view: McpRuntimeView) -> bool:
    """True when *tool_name* is registered in this profile's own registry slot.

    The merged registry view overlays a profile's tools on the global slot, so a
    routed profile would otherwise also list the process profile's MCP tools.
    """
    if view.legacy:
        return True
    snapshot = getattr(registry, "snapshot_registration", None)
    if not callable(snapshot):
        return True
    try:
        return snapshot(tool_name, scope=view.registry_scope) is not None
    except Exception:
        return False
