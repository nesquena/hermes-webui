"""Expose hermes-agent's COMMAND_REGISTRY to the webui frontend.

This module is the single integration point with hermes_cli.commands.
If hermes-agent is unavailable the endpoint degrades to an empty list
so the frontend can still load with WEBUI_ONLY commands.
"""
from __future__ import annotations
from contextlib import nullcontext
import contextlib
import io
import logging
import shlex
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
_AGENT_COMMANDS_RETURNING_AGENT_MESSAGE = frozenset({'learn', 'blueprint'})
_ALLOWED_AGENT_COMMANDS = frozenset({
    'agents', 'blueprint', 'bundles', 'codex-runtime', 'credits', 'curator',
    'debug', 'fast', 'footer', 'insights', 'kanban', 'learn', 'memory', 'profile',
    'reload-mcp', 'reload-skills', 'resume', 'rollback', 'sessions',
    'subgoal', 'suggestions', 'version', 'whoami',
})
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


def _shellish_args(command: str) -> list[str]:
    try:
        return shlex.split(command)[1:]
    except ValueError:
        return str(command or "").split()[1:]


def _capture_stdout(fn) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        fn()
    return buf.getvalue().strip()


def _text_or_no_output(value: Any) -> str:
    text = str(value or "").strip()
    return text or "(no output)"


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


def execute_agent_command(command: str) -> str | dict[str, Any]:
    """Execute a narrow allowlist of WebUI-safe agent-side runtime commands."""

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
    if canonical == 'learn':
        return _resolve_learn_command(arg_string)
    if canonical == 'blueprint':
        return _run_blueprint_command(arg_string)
    if canonical == 'bundles':
        return _run_bundles_command()
    if canonical == 'curator':
        return _run_curator_command(command)
    if canonical == 'kanban':
        return _run_kanban_command(arg_string)
    if canonical == 'memory':
        return _run_memory_command(command)
    if canonical == 'suggestions':
        return _run_suggestions_command(arg_string)
    if canonical == 'version':
        return _run_version_command()
    if canonical in {'profile', 'whoami'}:
        return _run_profile_command()
    if canonical == 'agents':
        return _run_agents_command()
    if canonical == 'sessions':
        return _run_sessions_command(arg_string)
    if canonical == 'resume':
        return _run_resume_command(arg_string)
    if canonical == 'insights':
        return _run_insights_command(arg_string)
    if canonical == 'fast':
        return _run_fast_command(arg_string)
    if canonical == 'footer':
        return _run_footer_command(arg_string)
    if canonical == 'rollback':
        return _run_rollback_command(arg_string)
    if canonical == 'subgoal':
        return _run_subgoal_command(arg_string)
    if canonical == 'debug':
        return _run_debug_command(arg_string)

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


def _resolve_learn_command(arg_string: str) -> dict[str, Any]:
    user_request = str(arg_string or "").strip()
    try:
        from agent.learn_prompt import build_learn_prompt
        message = build_learn_prompt(user_request)
    except Exception:
        message = _build_webui_learn_prompt(user_request)

    lead = "Learning a skill from what you described." if user_request else "Learning a skill from this conversation."
    return {"output": f"⚡ {lead}", "message": message}


def _build_webui_learn_prompt(user_request: str) -> str:
    request = user_request or "what we just did in this conversation"
    return (
        "[/learn] The user wants you to learn a reusable skill from the request below, and save it.\n\n"
        f"THE REQUEST:\n{request}\n\n"
        "Use the skill system governance rules: inspect the relevant material, distill the reusable procedure, "
        "write or patch a focused skill with triggers, steps, pitfalls, and verification, then briefly report what changed."
    )


def _run_blueprint_command(arg_string: str) -> dict[str, Any] | str:
    try:
        from hermes_cli.blueprint_cmd import handle_blueprint_command
    except Exception as exc:
        logger.warning("Blueprint command runtime unavailable", exc_info=True)
        raise RuntimeError("Blueprint command unavailable") from exc

    try:
        result = handle_blueprint_command(arg_string or "")
    except Exception as exc:
        logger.warning("Blueprint command failed", exc_info=True)
        raise RuntimeError("Blueprint command failed") from exc

    text = _text_or_no_output(getattr(result, "text", ""))
    seed = getattr(result, "agent_seed", None)
    if seed:
        return {"output": text, "message": str(seed)}
    return text


def _run_bundles_command() -> str:
    try:
        from agent.skill_bundles import list_bundles
    except Exception as exc:
        logger.warning("Bundle runtime unavailable", exc_info=True)
        raise RuntimeError("Bundle command unavailable") from exc

    bundles = list_bundles() or []
    if not bundles:
        return "No skill bundles installed."
    lines = [f"▣ Skill Bundles ({len(bundles)} installed):"]
    for info in bundles:
        slug = str(info.get("slug") or "").strip()
        skills = list(info.get("skills") or [])
        desc = str(info.get("description") or f"Load {len(skills)} skills").strip()
        lines.append(f"/{slug} — {desc} ({len(skills)} skills)")
        for skill in skills:
            lines.append(f"  · {skill}")
    lines.append("Invoke a bundle with /<slug>.")
    return "\n".join(lines)


def _run_curator_command(command: str) -> str:
    tokens = _shellish_args(command) or ["status"]

    def _run() -> None:
        try:
            from hermes_cli.curator import cli_main
            cli_main(tokens)
        except SystemExit:
            pass

    try:
        return _text_or_no_output(_capture_stdout(_run))
    except Exception as exc:
        logger.warning("Curator command failed", exc_info=True)
        raise RuntimeError("Curator command failed") from exc


def _run_kanban_command(arg_string: str) -> str:
    try:
        from hermes_cli.kanban import run_slash
        return _text_or_no_output(run_slash(arg_string or ""))
    except Exception as exc:
        logger.warning("Kanban command failed", exc_info=True)
        raise RuntimeError("Kanban command failed") from exc


def _run_memory_command(command: str) -> str:
    args = _shellish_args(command)
    try:
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
        from tools.memory_tool import load_on_disk_store
    except Exception as exc:
        logger.warning("Memory command runtime unavailable", exc_info=True)
        raise RuntimeError("Memory command unavailable") from exc

    def _set_memory_approval(enabled: bool) -> None:
        from hermes_cli.config import set_config_value
        set_config_value("memory.write_approval", "true" if enabled else "false")

    try:
        out = handle_pending_subcommand(
            wa.MEMORY,
            args,
            memory_store=load_on_disk_store(),
            set_mode_fn=_set_memory_approval,
        )
    except Exception as exc:
        logger.warning("Memory command failed", exc_info=True)
        raise RuntimeError("Memory command failed") from exc
    return _text_or_no_output(out or "Unknown /memory subcommand. Use: pending, approve <id>, reject <id>, approval <on|off>.")


def _run_suggestions_command(arg_string: str) -> str:
    try:
        from hermes_cli.suggestions_cmd import handle_suggestions_command
        return _text_or_no_output(handle_suggestions_command(arg_string or "", origin={"platform": "webui"}))
    except Exception as exc:
        logger.warning("Suggestions command failed", exc_info=True)
        raise RuntimeError("Suggestions command failed") from exc


def _run_version_command() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except Exception:  # pragma: no cover - importlib.metadata is stdlib on supported Python
        PackageNotFoundError = Exception  # type: ignore[assignment]
        version = None  # type: ignore[assignment]
    if version is not None:
        for package in ("hermes-agent", "hermes_agent"):
            try:
                return f"Hermes Agent {version(package)}"
            except PackageNotFoundError:
                continue
    try:
        from hermes_cli import __version__
        return f"Hermes Agent {__version__}"
    except Exception:
        return "Hermes Agent version unavailable"


def _run_profile_command() -> str:
    try:
        from hermes_cli.config import get_hermes_home
        home = get_hermes_home()
    except Exception:
        home = None
    try:
        from api.profiles import get_active_profile_name
        profile = get_active_profile_name() or "default"
    except Exception:
        profile = "default"
    lines = [f"Active profile: {profile}"]
    if home:
        lines.append(f"Hermes home: {home}")
    return "\n".join(lines)


def _run_agents_command() -> str:
    try:
        from tools.process_registry import process_registry
        processes = process_registry.list_processes()
    except Exception:
        logger.warning("Agents/process registry unavailable", exc_info=True)
        return "No background agents or tracked processes are currently visible."
    if not processes:
        return "No background agents or tracked processes are currently running."
    lines = [f"Tracked processes ({len(processes)}):"]
    for proc in processes[:20]:
        pid = proc.get("pid") or proc.get("process_id") or "?"
        status = proc.get("status") or proc.get("state") or "unknown"
        label = proc.get("label") or proc.get("command") or proc.get("name") or "process"
        lines.append(f"- {label} — {status} (pid {pid})")
    if len(processes) > 20:
        lines.append(f"… {len(processes) - 20} more")
    return "\n".join(lines)


def _run_sessions_command(arg_string: str) -> str:
    try:
        from hermes_state import SessionDB
    except Exception as exc:
        logger.warning("Session DB runtime unavailable", exc_info=True)
        raise RuntimeError("Session command unavailable") from exc
    try:
        limit = int((arg_string or "").strip() or "10")
    except ValueError:
        limit = 10
    limit = max(1, min(limit, 25))
    db = SessionDB()
    sessions = db.list_sessions(limit=limit) or []
    if not sessions:
        return "No sessions found."
    lines = [f"Recent sessions ({len(sessions)}):"]
    for row in sessions:
        sid = row.get("id") or row.get("session_id") or "?"
        title = row.get("title") or "Untitled"
        updated = row.get("updated_at") or row.get("last_updated") or ""
        suffix = f" — {updated}" if updated else ""
        lines.append(f"- {sid}: {title}{suffix}")
    return "\n".join(lines)


def _run_resume_command(arg_string: str) -> str:
    target = str(arg_string or "").strip()
    if not target:
        return _run_sessions_command("10") + "\n\nUse the session picker/sidebar in WebUI to resume, or run /resume <session-id> in Hermes CLI."
    return (
        "WebUI cannot switch the active browser session through /resume yet. "
        "Use the sessions sidebar/session picker to open "
        f"{target!r}, or run `hermes chat --resume {target}` in the CLI."
    )


def _run_insights_command(arg_string: str) -> str:
    try:
        from hermes_cli.insights import build_insights_report
        return _text_or_no_output(build_insights_report(days_arg=arg_string or None))
    except Exception:
        return "Usage insights are unavailable in this WebUI runtime. Try `/usage` or run `/insights` in Hermes CLI."


def _run_fast_command(arg_string: str) -> str:
    return "Fast mode is controlled by the active model/runtime in WebUI; use the model picker or Hermes CLI `/fast` for CLI-session toggling."


def _run_footer_command(arg_string: str) -> str:
    return "The CLI footer/status bar is not part of the WebUI. Use WebUI Settings/Control Center for browser UI controls."


def _run_rollback_command(arg_string: str) -> str:
    return "Rollback is CLI-session specific and is not available as a WebUI slash command. Use WebUI undo/session controls or run `/rollback` in Hermes CLI."


def _run_subgoal_command(arg_string: str) -> str:
    return "Subgoals attach to an active Hermes goal loop. WebUI does not expose goal-loop mutation through /subgoal yet; use `/goal`/CLI goal mode for this session."


def _run_debug_command(arg_string: str) -> str:
    return "Debug report upload is intentionally not run from WebUI slash commands. Run `/debug local` or `/debug nous` in Hermes CLI to review and upload logs explicitly."


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
