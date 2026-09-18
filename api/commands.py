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
_ALLOWED_AGENT_COMMANDS = frozenset(
    {'reload-mcp', 'reload-skills', 'codex-runtime', 'credits', 'skills', 'memory'})
_RELOAD_MCP_LOCK = threading.Lock()
_RELOAD_SKILLS_LOCK = threading.Lock()
_CODEX_RUNTIME_LOCK = threading.Lock()

# '/skills' subcommands owned by hermes-agent's write-approval gate
# (tools/write_approval.py) rather than the webui's own local skill search
# (cmdSkills in static/commands.js). Kept in sync with SKILLS_AGENT_SUBCOMMANDS there.
# '/memory' has no local webui feature to protect (unlike /skills' search), so it has
# no equivalent allowlist -- every /memory invocation, including a bare one, is handed
# straight to handle_pending_subcommand() same as gateway/slash_commands.py does.
_SKILLS_WRITE_APPROVAL_SUBCOMMANDS = frozenset({
    'pending', 'approve', 'apply', 'reject', 'deny', 'drop', 'diff', 'approval', 'mode',
})


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
    if canonical == 'skills':
        return _run_skills_write_approval_command(arg_string)
    if canonical == 'memory':
        return _run_memory_write_approval_command(arg_string)

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
            from tools.mcp_tool import _servers, _lock
            from api.agent_compat import agent_attr

            shutdown_mcp_servers = agent_attr(
                "tools.mcp_tool", "shutdown_mcp_servers", "tools.mcp_tool_lifecycle"
            )
            discover_mcp_tools = agent_attr(
                "tools.mcp_tool", "discover_mcp_tools", "tools.mcp_tool_discovery"
            )
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


def _run_skills_write_approval_command(arg_string: str) -> str:
    """Run a `/skills` write-approval subcommand (pending/approve/reject/diff/approval/mode,
    plus the apply/deny/drop aliases) against hermes-agent's shared pending store
    (tools/write_approval.py).

    This is the WebUI-native counterpart to gateway/slash_commands.py's
    `_handle_skills_command` and the interactive CLI's own wiring in
    hermes_cli/cli_commands_mixin.py -- a WebUI chat turn goes through neither of
    those (it posts to /api/chat/start -> AIAgent.run_conversation with no
    slash-command interception at all), so it needs its own dispatch here.

    Only ever called for the reserved subcommand names cmdSkills forwards
    (static/commands.js); any other argument (a bare `/skills` or a search query)
    raises KeyError so the caller's normal allowlist-miss handling applies --
    this endpoint must never swallow a plain skill search.
    """
    args = arg_string.split()
    sub = args[0].lower() if args else ""
    if sub not in _SKILLS_WRITE_APPROVAL_SUBCOMMANDS:
        raise KeyError('skills')

    try:
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
    except Exception as exc:
        logger.warning("write-approval runtime unavailable for /skills", exc_info=True)
        raise RuntimeError("Skill write-approval runtime unavailable") from exc

    out = handle_pending_subcommand(wa.SKILLS, args, set_mode_fn=_write_approval_setter('skills'))
    return out if out is not None else (
        "Unknown /skills subcommand. Use: pending, approve <id>, reject <id>, diff <id>, approval <on|off>.")


def _run_memory_write_approval_command(arg_string: str) -> str:
    """Run a `/memory` write-approval subcommand (pending/approve/reject/approval/mode, plus the
    apply/deny/drop aliases -- no `diff`, memory entries are small enough to review inline)
    against hermes-agent's shared pending store (tools/write_approval.py).

    WebUI-native counterpart to gateway/slash_commands.py's `_handle_memory_command`. Unlike
    `/skills`, there is no competing local webui feature for `/memory` to shadow, so every
    argument (including a bare `/memory`, which shows gate status + the pending list) is
    handed straight through -- no reserved-subcommand allowlist/KeyError guard needed here.

    Uses a freshly loaded on-disk memory store (same as gateway): there is no long-lived
    agent session in a WebUI exec call either, and the store persists to the same
    MEMORY.md/USER.md and honors the configured char limits regardless.
    """
    try:
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
        from tools.memory_tool import load_on_disk_store
    except Exception as exc:
        logger.warning("write-approval runtime unavailable for /memory", exc_info=True)
        raise RuntimeError("Memory write-approval runtime unavailable") from exc

    out = handle_pending_subcommand(
        wa.MEMORY, arg_string.split(), memory_store=load_on_disk_store(),
        set_mode_fn=_write_approval_setter('memory'))
    return out if out is not None else (
        "Unknown /memory subcommand. Use: pending, approve <id>, reject <id>, approval <on|off>.")


def _write_approval_setter(subsystem: str):
    """``set_mode_fn`` for '/skills approval on|off' and '/memory approval on|off' -- persists
    `<subsystem>.write_approval` to the shared config.yaml via the webui's own config module
    (there is no gateway session here to route the equivalent gateway-side persistence through).

    Locked read-modify-write, same pattern as `api.config.set_hermes_default_model`: reads the
    RAW file (not `get_config()`, which may return a merged-with-defaults snapshot that must
    never be written back) under `_cfg_lock` so a concurrent config writer elsewhere can't save
    between this read and this write and have its change discarded. `reload_config()` is called
    AFTER releasing the lock -- it acquires `_cfg_lock` internally and the lock isn't reentrant.
    """

    def _set_approval(enabled: bool) -> None:
        from api import config as webui_config

        config_path = webui_config._get_config_path()
        with webui_config._cfg_lock:
            config_data = webui_config._load_yaml_config_file(config_path)
            config_data.setdefault(subsystem, {})['write_approval'] = bool(enabled)
            webui_config._save_yaml_config_file(config_path, config_data)
        webui_config.reload_config()

    return _set_approval


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


def _load_config_for_moa_resolution() -> dict:
    from hermes_cli.config import load_config

    cfg = load_config()
    return cfg if isinstance(cfg, dict) else {}


def resolve_moa_config(preset: str | None = None) -> dict:
    try:
        from hermes_cli.moa_config import moa_usage, normalize_moa_config
    except ImportError as exc:
        raise RuntimeError("MoA runtime unavailable (hermes-agent not installed or too old)") from exc
    try:
        from hermes_cli.moa_config import resolve_moa_preset
    except ImportError:
        resolve_moa_preset = None

    try:
        cfg = _load_config_for_moa_resolution()
        moa_raw = cfg.get("moa") if isinstance(cfg, dict) else {}
        moa_cfg = normalize_moa_config(moa_raw)
    except Exception:
        moa_raw = {}
        moa_cfg = normalize_moa_config({})

    preset_name = str(preset or moa_cfg.get("default_preset") or "default").strip()
    if preset_name not in (moa_cfg.get("presets") or {}):
        preset_name = str(moa_cfg.get("default_preset") or "default")

    selected = {}
    if resolve_moa_preset is not None:
        try:
            selected = resolve_moa_preset(moa_raw, preset_name)
            if not isinstance(selected, dict):
                selected = {}
        except Exception:
            selected = {}
            preset_name = str(moa_cfg.get("default_preset") or "default")

    resolved = dict(moa_cfg)
    resolved.update(selected)
    resolved["preset"] = preset_name
    resolved["usage"] = moa_usage()
    return resolved


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
