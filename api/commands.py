"""Expose hermes-agent's COMMAND_REGISTRY to the webui frontend.

This module is the single integration point with hermes_cli.commands.
If hermes-agent is unavailable the endpoint degrades to an empty list
so the frontend can still load with WEBUI_ONLY commands.
"""
from __future__ import annotations
from contextlib import nullcontext
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
    'reload_skills': 'reload-skills',
    'codex_runtime': 'codex-runtime',
}
_ALLOWED_AGENT_COMMANDS = frozenset({'reload-mcp', 'reload-skills', 'codex-runtime', 'credits'})
_RELOAD_MCP_LOCK = threading.Lock()
_RELOAD_SKILLS_LOCK = threading.Lock()
_CODEX_RUNTIME_LOCK = threading.Lock()


def _parse_agent_command(command: str) -> tuple[str, str]:
    """Return ``(canonical_name, arg_string)`` from slash-command text."""

    cmd_base, arg_string = _parse_slash_command(command)
    return _AGENT_COMMAND_ALIASES.get(cmd_base, cmd_base), arg_string


def _parse_slash_command(command: str) -> tuple[str, str]:
    """Return ``(command_name, arg_string)`` from slash-command text."""

    raw = str(command or "").strip()
    if not raw:
        raise ValueError("command is required")

    cmd_text = raw[1:] if raw.startswith("/") else raw
    cmd_parts = cmd_text.split(maxsplit=1)
    cmd_base = (cmd_parts[0] if cmd_parts else "").strip().lower()
    if not cmd_base:
        raise ValueError("command is required")

    return cmd_base, cmd_parts[1] if len(cmd_parts) > 1 else ""


def _bundle_profile_context(purpose: str):
    """Resolve the active-profile env wrapper used by bundle APIs."""

    try:
        from api.profiles import profile_env_for_active_request
    except ImportError:
        return nullcontext()
    return profile_env_for_active_request(purpose, logger_override=logger)


def _normalize_agent_command_name(command: str) -> str:
    """Normalize slash text to a canonical command name."""

    canonical, _arg_string = _parse_agent_command(command)
    return canonical


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


def list_command_bundles() -> list[dict[str, Any]]:
    """Return installed skill bundles for the active WebUI profile."""

    try:
        from agent.skill_bundles import list_bundles as _list_bundles
    except ImportError:
        logger.debug("agent.skill_bundles not importable -- /api/commands/bundles returns []")
        return []

    try:
        with _bundle_profile_context("/api/commands/bundles"):
            bundles = _list_bundles() or []
    except Exception:
        logger.warning("Failed to list skill bundles", exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for bundle in bundles:
        slug = str((bundle or {}).get("slug", "")).strip().lower()
        if not slug:
            continue
        skills = list((bundle or {}).get("skills") or [])
        out.append({
            "name": slug,
            "description": str((bundle or {}).get("description") or "").strip() or "Skill bundle",
            "skill_count": len(skills),
            "source": "bundle",
        })
    return out


def resolve_bundle_command(command: str) -> dict[str, Any]:
    """Expand a bundle slash command into the backend invocation payload."""

    bundle_name, user_instruction = _parse_slash_command(command)
    try:
        from agent.skill_bundles import (
            build_bundle_invocation_message,
            resolve_bundle_command_key,
        )
    except ImportError as exc:
        logger.warning("Skill bundle runtime unavailable", exc_info=True)
        raise RuntimeError("Skill bundle runtime unavailable") from exc

    try:
        with _bundle_profile_context("/api/commands/bundles/resolve"):
            bundle_key = resolve_bundle_command_key(bundle_name)
            if bundle_key is None:
                raise KeyError(bundle_name)
            bundle_result = build_bundle_invocation_message(bundle_key, user_instruction)
    except (KeyError, ValueError, RuntimeError):
        raise
    except Exception as exc:
        logger.warning("Failed to resolve skill bundle command", exc_info=True)
        raise RuntimeError("Skill bundle command unavailable") from exc

    if not bundle_result:
        raise RuntimeError("Bundle command returned no invocation text")

    message, loaded_skills, missing_skills = bundle_result
    resolved_message = str(message or "").strip()
    if not resolved_message:
        raise RuntimeError("Bundle command returned no invocation text")

    return {
        "name": bundle_key.lstrip("/"),
        "source": "bundle",
        "message": resolved_message,
        "loaded_skills": list(loaded_skills or []),
        "missing_skills": list(missing_skills or []),
    }


def execute_agent_command(command: str) -> str:
    """Execute a narrow allowlist of agent-side runtime commands."""

    canonical, arg_string = _parse_agent_command(command)
    if canonical not in _ALLOWED_AGENT_COMMANDS:
        raise KeyError(canonical)

    if canonical == 'reload-mcp':
        return _run_reload_mcp_command()
    if canonical == 'reload-skills':
        return _run_reload_skills_command()
    if canonical == 'codex-runtime':
        return _run_codex_runtime_command(arg_string)
    if canonical == 'credits':
        return _run_credits_command()

    raise KeyError(canonical)


def _run_codex_runtime_command(arg_string: str) -> str:
    """Execute Hermes' shared Codex runtime switch for the active profile."""
    try:
        from hermes_cli.codex_runtime_switch import apply, parse_args
    except Exception as exc:
        logger.warning("Codex runtime switch unavailable", exc_info=True)
        raise RuntimeError("Codex runtime switch unavailable") from exc

    new_value, errors = parse_args(arg_string)
    if errors:
        return "\n".join(str(error) for error in errors)

    with _CODEX_RUNTIME_LOCK:
        try:
            from api import config as webui_config

            active_config = webui_config.get_config()

            def _persist_config(config_data: dict) -> None:
                webui_config._save_yaml_config_file(
                    webui_config._get_config_path(),
                    config_data,
                )
                webui_config.reload_config()

            status = apply(active_config, new_value, persist_callback=_persist_config)
        except Exception as exc:
            logger.warning("Failed to execute /codex-runtime", exc_info=True)
            raise RuntimeError("Failed to update Codex runtime") from exc

    return str(getattr(status, "message", "") or "(no output)")


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


def _run_reload_skills_command() -> str:
    """Re-scan the installed skills directory and summarize the diff."""
    with _RELOAD_SKILLS_LOCK:
        try:
            from agent.skill_commands import reload_skills
        except Exception as exc:
            logger.warning("Failed to import skills runtime for /reload-skills", exc_info=True)
            raise RuntimeError("Skills runtime unavailable") from exc

        try:
            result = reload_skills() or {}
        except Exception as exc:
            logger.warning("Failed to reload skills", exc_info=True)
            raise RuntimeError("Failed to reload skills") from exc

    added = result.get("added", [])
    removed = result.get("removed", [])
    unchanged = result.get("unchanged", [])
    total = int(result.get("total", 0) or 0)

    def _names(items: Any) -> list[str]:
        out: list[str] = []
        for item in items or []:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
            else:
                name = str(item).strip()
            if name:
                out.append(name)
        return out

    added_names = _names(added)
    removed_names = _names(removed)

    lines = [
        "Reloaded skills from disk.",
        f"Added: {len(added_names)}",
        f"Removed: {len(removed_names)}",
        f"Unchanged: {len(list(unchanged or []))}",
        f"Total skills: {total}",
    ]
    if added_names:
        lines.append(f"Added skills: {', '.join(sorted(added_names))}")
    if removed_names:
        lines.append(f"Removed skills: {', '.join(sorted(removed_names))}")
    return "\n".join(lines)


def _run_credits_command() -> str:
    """Render Hermes' shared credits view for the WebUI slash-command path."""
    try:
        from agent.account_usage import build_credits_view
    except Exception:
        logger.warning("Failed to import credits view runtime", exc_info=True)
        return "Couldn't fetch credits right now."

    try:
        view = build_credits_view(markdown=True)
    except Exception:
        logger.warning("Failed to build /credits view", exc_info=True)
        return "Couldn't fetch credits right now."

    if not getattr(view, "logged_in", False):
        return "Not logged into Nous. Run `hermes auth login nous` in Hermes CLI, then try /credits again."

    lines = ["💳 **Nous credits**"]
    for line in tuple(getattr(view, "balance_lines", ()) or ()):
        if str(line).lstrip().startswith("📈"):
            continue
        lines.append(str(line))

    identity_line = str(getattr(view, "identity_line", "") or "").strip()
    if identity_line:
        lines.append("")
        lines.append(identity_line)

    topup_url = str(getattr(view, "topup_url", "") or "").strip()
    if topup_url:
        lines.append("")
        lines.append(f"Top up: {topup_url}")
        lines.append("Complete your top-up in the browser; credits will appear in /credits shortly.")
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
