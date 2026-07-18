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


def save_mcp_bearer_token(name: str, token: str, home: Path) -> Dict[str, str]:
    """Store a bearer token in the profile's ``.env`` and return the header
    template (``Authorization: Bearer ${MCP_<NAME>_API_KEY}``) to persist in
    ``config.yaml`` — matching the agent CLI/Dashboard convention so secrets
    never land in YAML."""
    from hermes_cli.mcp_config import _save_bearer_auth_token

    with scoped_agent_home(home):
        return _save_bearer_auth_token(name, token)


# ── skills config (enable/disable lists) ─────────────────────────────────────

def save_skills_config(skills_cfg: Dict[str, Any], home: Path) -> None:
    """Persist the ``skills`` section (disabled / platform_disabled lists)."""
    from hermes_cli.config import load_config, save_config

    with scoped_agent_home(home):
        config = load_config()
        config["skills"] = skills_cfg
        save_config(config)
