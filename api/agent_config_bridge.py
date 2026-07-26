"""Shared write path into the Hermes agent's configuration.

The WebUI historically wrote ``config.yaml`` through its own
``yaml.safe_dump`` serializer (``api.config._save_yaml_config_file``).
That writer is correct for value round-trips but destroys user comments
and formatting, skips the agent's ``mcp_security`` validation, and stores
secrets inline instead of routing them to ``.env``.

This module routes admin writes through the agent's own persistence layer
(``hermes_cli.config.save_config`` — comment-preserving Ruamel round-trip,
atomic write, managed-scope aware) when the agent checkout is importable,
using the context-local Hermes-home override so writes land in the active
WebUI profile's home without mutating process-global environment.

Fallback behavior is deliberately two-tiered:

- No agent checkout discovered (standalone WebUI deployments, CI): callers
  keep using the legacy WebUI writer — behavior is unchanged from before
  this module existed.
- Agent checkout discovered but the import fails (broken checkout,
  unsupported layout): raise ``AgentBridgeUnavailable`` instead of silently
  falling back, so a mis-wired deployment cannot half-apply admin writes.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from api.config import _AGENT_DIR

logger = logging.getLogger(__name__)

_import_lock = threading.Lock()
_import_state: Optional[str] = None  # None=unprobed, "ok", "unavailable:<reason>"


class AgentBridgeUnavailable(RuntimeError):
    """Agent checkout exists but its config layer could not be imported."""


def agent_dir_configured() -> bool:
    """True when an agent checkout was discovered at startup."""
    return _AGENT_DIR is not None


def _probe_import() -> str:
    """Import the agent config layer once; cache the outcome."""
    global _import_state
    if _import_state is not None:
        return _import_state
    with _import_lock:
        if _import_state is not None:
            return _import_state
        if os.getenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", "").strip().lower() in {"1", "true", "yes", "on"}:
            # Explicit operator kill-switch: behave exactly like a standalone
            # deployment (legacy writer), e.g. to rule the bridge out while
            # debugging or to pin pre-bridge behavior.
            _import_state = "unavailable:disabled via HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE"
            return _import_state
        if _AGENT_DIR is None:
            _import_state = "unavailable:no agent checkout discovered"
            return _import_state
        try:
            import hermes_constants  # noqa: F401
            from hermes_cli import config as _agent_config  # noqa: F401

            for required in ("save_config", "load_config", "save_env_value"):
                if not callable(getattr(_agent_config, required, None)):
                    raise ImportError(f"hermes_cli.config.{required} missing")
            _import_state = "ok"
        except BaseException as exc:  # ImportError, SyntaxError, SystemExit guards
            logger.warning("agent config bridge unavailable: %s", exc)
            _import_state = f"unavailable:{exc}"
    return _import_state



def config_path_conflict(home: Path) -> Optional[str]:
    """Reason the bridge must not be used for *home*, or ``None``.

    The WebUI resolver honours ``HERMES_CONFIG_PATH``; the agent's config layer
    resolves ``<home>/config.yaml`` and has no contract for an explicit path. So
    when an override names a DIFFERENT file, bridge writes would land in one
    file while every read follows the other — a save reporting success against
    a file the runtime never looks at.

    Deliberately not folded into ``_probe_import()``: that answers "can the
    agent config layer be imported", is cached process-wide, and an override
    pointing at the active home's own config.yaml (which is what test harnesses
    set) is not a conflict at all. Conflating the two would have disabled the
    bridge everywhere it is exercised.
    """
    override = os.getenv("HERMES_CONFIG_PATH", "").strip()
    if not override:
        return None
    try:
        if Path(override).expanduser().resolve() == (Path(home) / "config.yaml").resolve():
            return None
    except (OSError, TypeError, ValueError):
        # A home we cannot turn into a path tells us nothing about a conflict,
        # and guessing "conflict" here would disable the bridge for callers
        # that never had one.
        return None
    return (
        "HERMES_CONFIG_PATH points outside the active profile home and the "
        "agent config layer cannot honour an explicit config path"
    )


def bridge_available() -> bool:
    """True when writes can be routed through the agent's persistence layer."""
    return _probe_import() == "ok"


def require_bridge() -> None:
    """Raise ``AgentBridgeUnavailable`` when an agent checkout exists but the
    bridge cannot import it. No-op in standalone mode (no checkout at all)
    and when the operator kill-switch explicitly disabled the bridge."""
    state = _probe_import()
    if state == "ok":
        return
    if not agent_dir_configured():
        return
    if state.startswith("unavailable:disabled via"):
        return
    raise AgentBridgeUnavailable(state.split(":", 1)[1] if ":" in state else state)


@contextmanager
def scoped_agent_home(home: Path):
    """Scope agent-side path resolution to *home* for the current context.

    Uses the agent's context-local override (a ``ContextVar``) so concurrent
    requests against different WebUI profiles cannot cross-write each other's
    ``config.yaml``/``.env`` — unlike an ``os.environ`` mutation, which is
    process-global.
    """
    import hermes_constants

    token = hermes_constants.set_hermes_home_override(str(home))
    try:
        yield
    finally:
        hermes_constants.reset_hermes_home_override(token)


# ── config.yaml ──────────────────────────────────────────────────────────────

@contextmanager
def process_env_restored():
    """Undo any process-global environment mutation made inside this block.

    The agent derives an MCP bearer token's env key from the SERVER NAME alone,
    and its ``save_env_value()`` writes ``os.environ`` as well as the profile's
    dotenv file. The home ``ContextVar`` scopes the FILES, not that global — so
    two profiles each holding a server called ``shared`` end up with whichever
    one wrote last supplying the expanded value for both. Profile B's token
    becomes the credential profile A's config expands and the Test button
    probes with.

    Keeping the write to the file and rolling the process environment back
    makes the effective secret profile-scoped again: every read resolves it
    from the active profile's dotenv file rather than from whatever a
    concurrent request happened to leave behind.
    """
    before = dict(os.environ)
    try:
        yield
    finally:
        for key in [k for k in os.environ if k not in before]:
            os.environ.pop(key, None)
        for key, value in before.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


def load_agent_config(home: Path) -> Dict[str, Any]:
    from hermes_cli.config import load_config

    with scoped_agent_home(home):
        return load_config()


def save_agent_config(config: Dict[str, Any], home: Path) -> None:
    """Persist *config* through the agent's comment-preserving writer."""
    from hermes_cli.config import save_config

    with scoped_agent_home(home):
        save_config(config)


# ── MCP servers ──────────────────────────────────────────────────────────────

def validate_mcp_entry(name: str, entry: Dict[str, Any]) -> List[str]:
    """Return security-validation issues for one MCP server entry."""
    from hermes_cli.mcp_security import validate_mcp_server_entry

    return list(validate_mcp_server_entry(name, entry) or [])


def save_mcp_server(name: str, server_config: Dict[str, Any], home: Path) -> List[str]:
    """Validate and persist one MCP server. Returns issues; empty on success."""
    issues = validate_mcp_entry(name, server_config)
    if issues:
        return issues
    from hermes_cli.config import load_config, save_config

    with scoped_agent_home(home):
        config = load_config()
        config.setdefault("mcp_servers", {})[name] = server_config
        save_config(config)
    return []


def remove_mcp_server(name: str, home: Path) -> bool:
    """Remove one MCP server. Returns True when it existed."""
    from hermes_cli.config import load_config, save_config

    with scoped_agent_home(home):
        config = load_config()
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict) or name not in servers:
            return False
        del servers[name]
        if servers:
            config["mcp_servers"] = servers
        else:
            config.pop("mcp_servers", None)
        save_config(config)
    return True


def set_mcp_server_enabled(name: str, enabled: bool, home: Path) -> bool:
    """Flip one server's ``enabled`` flag. Returns False when it is missing."""
    from hermes_cli.config import load_config, save_config

    with scoped_agent_home(home):
        config = load_config()
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict) or not isinstance(servers.get(name), dict):
            return False
        servers[name]["enabled"] = bool(enabled)
        config["mcp_servers"] = servers
        save_config(config)
    return True


def bearer_token_env_key(name: str) -> str:
    """The env var the agent derives for *name*'s bearer token."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(name)).upper()
    return f"MCP_{safe}_API_KEY"


def snapshot_bearer_token_state(name: str, home: Path) -> Dict[str, Any]:
    """Capture enough to undo a bearer-token write.

    The write touches two places — the profile's dotenv file and the agent's
    process-global ``os.environ`` — and the config save that follows it can
    still fail. Without this, a rejected request left a live credential behind
    that no config entry referenced.
    """
    key = bearer_token_env_key(name)
    env_path = Path(home) / ".env"
    try:
        raw = env_path.read_bytes()
    except OSError:
        raw = None
    return {
        "key": key,
        "env_path": env_path,
        "dotenv_bytes": raw,
        "process_env_present": key in os.environ,
        "process_env_value": os.environ.get(key),
    }


def restore_bearer_token_state(state: Dict[str, Any]) -> None:
    """Undo a bearer-token write from ``snapshot_bearer_token_state()``.

    Restores the dotenv file byte-for-byte (so unrelated edits in the same file
    are not reformatted away) and puts the process environment back to exactly
    the presence/value it had — including removing a key that was absent.
    """
    if not state:
        return
    key = state.get("key")
    env_path = state.get("env_path")
    raw = state.get("dotenv_bytes")
    try:
        if raw is None:
            if env_path is not None:
                Path(env_path).unlink(missing_ok=True)
        else:
            Path(env_path).write_bytes(raw)
    except OSError:
        logger.warning("Could not restore %s after a failed MCP save", env_path)
    if key:
        if state.get("process_env_present"):
            os.environ[key] = state.get("process_env_value") or ""
        else:
            os.environ.pop(key, None)


def save_mcp_bearer_token(name: str, token: str, home: Path) -> Dict[str, str]:
    """Store a bearer token in the profile's ``.env`` and return the header
    template (``Authorization: Bearer ${MCP_<NAME>_API_KEY}``) to persist in
    ``config.yaml`` — matching the agent CLI/Dashboard convention so secrets
    never land in YAML."""
    from hermes_cli.mcp_config import _save_bearer_auth_token

    # The token belongs in this profile's dotenv file, not in the process
    # environment every other profile also reads (see process_env_restored).
    with process_env_restored(), scoped_agent_home(home):
        return _save_bearer_auth_token(name, token)


def probe_mcp_server(name: str, home: Path, *, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Connect to one MCP server, list its tools, disconnect.

    Raises ``KeyError`` when *name* is not configured, and re-raises any
    connection failure for the caller to map into an ``{ok: false}``
    response. Mirrors the Dashboard's own ``POST /api/mcp/servers/{name}/test``
    (``hermes_cli.web_server.test_mcp_server``) minus its ``asyncio.to_thread``
    wrapper: that wrapper exists there only to keep FastAPI's single event
    loop unblocked during the probe, but the WebUI's ``ThreadingHTTPServer``
    already gives every request its own OS thread, so a direct, bounded call
    is enough here.

    ``timeout`` overrides the server's own ``connect_timeout`` when given.
    Left at the default ``None``, the probe respects whatever the server's
    config specifies (``_probe_single_server`` falls back to 30s itself) —
    an earlier version hard-defaulted this to 15s, which cut off legitimately
    slow connects (e.g. a cold ``npx`` stdio spawn) with a false "test
    failed" instead of the server's configured budget. The Dashboard's own
    probe route passes no timeout at all for the same reason.
    """
    from hermes_cli.mcp_config import _get_mcp_servers, _oauth_tokens_present, _probe_single_server

    with scoped_agent_home(home):
        servers = _get_mcp_servers()
        if name not in servers:
            raise KeyError(name)
        cfg = servers[name]
        needs_oauth_token = cfg.get("auth") == "oauth"
        details: Dict[str, Any] = {}
        probe_kwargs = {} if timeout is None else {"connect_timeout": timeout}
        tools = _probe_single_server(name, cfg, details=details, **probe_kwargs)
        token_present = _oauth_tokens_present(name) if needs_oauth_token else True
    if not token_present:
        raise RuntimeError("OAuth authentication required — no token found.")
    return {
        "tools_count": len(tools),
        "prompts": details.get("prompts", 0),
        "resources": details.get("resources", 0),
    }


# ── skills config (enable/disable lists) ─────────────────────────────────────

def save_skills_config(skills_cfg: Dict[str, Any], home: Path) -> None:
    """Persist the ``skills`` section (disabled / platform_disabled lists)."""
    from hermes_cli.config import load_config, save_config

    with scoped_agent_home(home):
        config = load_config()
        config["skills"] = skills_cfg
        save_config(config)
