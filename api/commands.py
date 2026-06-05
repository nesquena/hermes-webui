"""Expose hermes-agent's COMMAND_REGISTRY to the webui frontend.

This module is the single integration point with hermes_cli.commands.
If hermes-agent is unavailable the endpoint degrades to an empty list
so the frontend can still load with WEBUI_ONLY commands.
"""
from __future__ import annotations
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Commands that are gateway_only in the agent registry -- webui never
# wants to expose them (sethome, restart, update etc.) even if a future
# agent version drops the gateway_only flag. /commands is the agent's
# own command-listing command; webui has its own /help that calls
# cmdHelp() locally, so /commands would be redundant and confusing.
_NEVER_EXPOSE: frozenset[str] = frozenset({
    'sethome', 'restart', 'update', 'commands',
})


# Narrow agent-side execution allowlist for /api/commands/exec.
_AGENT_COMMAND_ALIASES = {
    'reload_mcp': 'reload-mcp',
}
_ALLOWED_AGENT_COMMANDS = frozenset({'reload-mcp'})
_RELOAD_MCP_LOCK = threading.Lock()


def _normalize_agent_command_name(command: str) -> str:
    """Normalize slash text to a canonical command name."""

    raw = str(command or "").strip()
    if not raw:
        raise ValueError("command is required")

    cmd_text = raw[1:] if raw.startswith("/") else raw
    cmd_parts = cmd_text.split(maxsplit=1)
    cmd_base = (cmd_parts[0] if cmd_parts else "").strip().lower()
    if not cmd_base:
        raise ValueError("command is required")

    return _AGENT_COMMAND_ALIASES.get(cmd_base, cmd_base)


def list_commands(_registry=None) -> list[dict[str, Any]]:
    """Return COMMAND_REGISTRY entries as JSON-friendly dicts.

    Returns empty list if hermes_cli is not installed (graceful
    degradation -- the frontend has its own fallback minimum set).

    Args:
        _registry: Optional injected registry for testing. When None
            (production), imports COMMAND_REGISTRY from hermes_cli.
    """
    if _registry is None:
        try:
            from hermes_cli.commands import COMMAND_REGISTRY as _registry
        except ImportError:
            logger.warning("hermes_cli.commands not importable -- /api/commands returns []")
            return []

    out: list[dict[str, Any]] = []
    for cmd in _registry:
        if cmd.gateway_only:
            continue
        if cmd.name in _NEVER_EXPOSE:
            continue
        out.append({
            'name': cmd.name,
            'description': cmd.description,
            'category': cmd.category,
            'aliases': list(cmd.aliases),
            'args_hint': cmd.args_hint,
            'subcommands': list(cmd.subcommands),
            'cli_only': bool(cmd.cli_only),
            'gateway_only': bool(cmd.gateway_only),
        })

    # Include plugin-registered slash commands
    try:
        from hermes_cli.plugins import get_plugin_commands
        plugin_cmds = get_plugin_commands() or {}
        existing_names = {c['name'] for c in out}
        for cmd_name, cmd_info in plugin_cmds.items():
            if cmd_name in existing_names or cmd_name in _NEVER_EXPOSE:
                continue
            out.append({
                'name': cmd_name,
                'description': str(cmd_info.get('description', 'Plugin command')),
                'category': 'Plugin',
                'aliases': [],
                'args_hint': str(cmd_info.get('args_hint', '')),
                'subcommands': [],
                'cli_only': False,
                'gateway_only': False,
            })
    except Exception:
        pass
    return out


def execute_agent_command(command: str) -> str:
    """Execute a narrow allowlist of agent-side runtime commands."""

    canonical = _normalize_agent_command_name(command)
    if canonical not in _ALLOWED_AGENT_COMMANDS:
        raise KeyError(canonical)

    if canonical == 'reload-mcp':
        return _run_reload_mcp_command()

    raise KeyError(canonical)


def _run_reload_mcp_command() -> str:
    """Execute the MCP reconnect path and return a short user-facing summary."""
    with _RELOAD_MCP_LOCK:
        try:
            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _servers, _lock
        except Exception as exc:
            logger.warning("Failed to import MCP runtime for /reload-mcp", exc_info=True)
            raise RuntimeError("MCP runtime unavailable") from exc

        try:
            with _lock:
                old_servers = set(_servers.keys())

            shutdown_mcp_servers()
            new_tools = discover_mcp_tools()

            with _lock:
                connected_servers = set(_servers.keys())
        except Exception as exc:
            logger.warning("Failed to reload MCP servers", exc_info=True)
            raise RuntimeError("Failed to reload MCP servers") from exc

    added = connected_servers - old_servers
    removed = old_servers - connected_servers
    reconnected = connected_servers & old_servers

    lines = ["Reloaded MCP servers from configuration."]
    if reconnected:
        lines.append(f"Reconnected: {', '.join(sorted(reconnected))}")
    if added:
        lines.append(f"Added: {', '.join(sorted(added))}")
    if removed:
        lines.append(f"Removed: {', '.join(sorted(removed))}")

    if connected_servers:
        lines.append(f"{len(new_tools or [])} tool(s) available across {len(connected_servers)} server(s)")
    else:
        lines.append("No MCP servers connected")

    if not reconnected and not added and not removed:
        lines.append("Tooling state was already current")

    return "\n".join(lines)


def execute_plugin_command(command: str) -> str:
    """Execute a plugin-registered slash command and return printable output.

    Unknown commands raise ``KeyError`` so the HTTP layer can return 404.
    Plugin handler failures are returned as output text instead of surfacing as
    transport errors, matching Hermes' existing slash-command UX.
    """

    raw = str(command or "").strip()
    if not raw:
        raise ValueError("command is required")

    cmd_text = raw[1:] if raw.startswith("/") else raw
    cmd_parts = cmd_text.split(maxsplit=1)
    cmd_base = (cmd_parts[0] if cmd_parts else "").strip().lower()
    cmd_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""
    if not cmd_base:
        raise ValueError("command is required")

    try:
        from hermes_cli.plugins import (
            get_plugin_command_handler,
            resolve_plugin_command_result,
        )
    except ImportError as exc:
        logger.warning("Plugin command runtime unavailable", exc_info=True)
        raise RuntimeError("plugin command runtime unavailable") from exc

    try:
        handler = get_plugin_command_handler(cmd_base)
    except Exception as exc:
        logger.warning("Plugin command lookup failed for %r", cmd_base, exc_info=True)
        raise RuntimeError("plugin command lookup failed") from exc

    if not handler:
        raise KeyError(cmd_base)

    try:
        result = resolve_plugin_command_result(handler(cmd_arg))
        return str(result or "(no output)")
    except Exception as exc:
        # Don't leak raw exception str (paths, env, internal state) to the
        # user-facing chat. Type name is enough for the user to know what
        # class of failure occurred; full traceback lives in the server log.
        logger.warning("Plugin command %r execution failed", cmd_base, exc_info=True)
        return f"Plugin command error: {type(exc).__name__}"
