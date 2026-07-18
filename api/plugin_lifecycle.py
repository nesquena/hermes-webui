"""Hermes-agent plugin lifecycle actions (install/update/remove) for the
WebUI's Settings -> Plugins panel.

HIGHEST-RISK write surface in the WebUI's Settings area: installing a plugin
pulls arbitrary Python (and whatever dependencies it declares) from a Git
repository and runs it as part of the Hermes agent. That execution must
happen OUTSIDE the WebUI's own long-running server process -- a malicious or
merely broken plugin must never get a chance to touch the WebUI process's own
memory (auth state, other users' sessions, provider credentials). So every
action here spawns ``hermes plugins install/update/remove <arg>`` as an
isolated subprocess -- mirroring api/gateway_restart.py's HERMES_HOME-scoped
``hermes gateway restart`` spawn -- instead of importing
``hermes_cli.plugins_cmd`` in-process (which is what the *native* Hermes
Agent dashboard does at hermes_cli/web_server.py's
``/api/dashboard/agent-plugins/*`` routes; that's a different trust boundary
-- that dashboard already runs inside the agent process).

Fail-closed gating (HERMES_WEBUI_ALLOW_PLUGIN_WRITE) is enforced by the
caller (api/routes.py), matching api/ops_actions.py's separation of
mechanism (this module) from policy (the route's env check). This module
only reports ``available`` (a resolvable ``hermes`` CLI) and refuses actions
when it can't run one -- it never reads the gate env var itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

from api.gateway_restart import _gateway_restart_profile_context, _resolve_hermes_command

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUNNING = False
_LAST: Optional[dict] = None  # {"action","name","ok","log_tail","finished_at"}

_ACTION_TIMEOUT_SECONDS = 600
_LOG_TAIL_MAX_BYTES = 200_000
_LIST_TIMEOUT_SECONDS = 30

# Registry-name shorthand segments: safe charset only (letters, digits, dot,
# underscore, hyphen) -- no shell metacharacters, no path traversal.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Plugin directory names on disk (hermes_cli.plugins_cmd._sanitize_plugin_name
# for a fresh install path never allows a subdir) -- flat, safe charset.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class PluginSourceError(ValueError):
    """The plugin source/identifier/name failed WebUI-side validation."""


def _resolve_hermes_command_if_real() -> Optional[str]:
    """Like ``_resolve_hermes_command`` but returns None instead of a bare
    ``"hermes"`` fallback string when nothing was actually found on disk/PATH.

    ``api.gateway_restart._resolve_hermes_command`` always returns *some*
    string (falling back to the literal ``"hermes"``) because a restart
    failure there just surfaces as a failed subprocess. Availability checks
    need a real yes/no, so this only trusts a match that
    ``shutil.which``/an explicit sibling-of-python path actually confirmed.
    """
    import shutil
    import sys
    from pathlib import Path

    found = shutil.which("hermes")
    if found:
        return found
    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return None


def is_available() -> bool:
    """True if a ``hermes`` CLI is resolvable (agent checkout or PATH install).

    Standalone WebUI deployments (no Hermes Agent checkout) have no `hermes`
    executable at all -- every write action must degrade to a clear "not
    available" instead of trying (and failing confusingly) to spawn one.
    """
    return bool(_resolve_hermes_command_if_real())


def validate_source(source: str) -> str:
    """Reject anything that isn't an https:// URL or a safe owner/repo[/sub] shorthand.

    Intentionally NARROWER than ``hermes_cli.plugins_cmd._resolve_git_url``,
    which also accepts ``git@``/``ssh://``/``http://``/``file://`` (the
    latter two with only a soft warning) -- appropriate for a trusted local
    CLI user, not a web-exposed install form. subprocess calls here always
    use a list argv (never ``shell=True``), so this validation isn't a shell-
    injection guard; it exists so the identifier itself can't be crafted to
    look like a CLI flag (e.g. starting with ``-``) or exploit a bug deeper
    in the git/URL parsing chain.
    """
    source = str(source or "").strip()
    if not source:
        raise PluginSourceError("source is required")
    if source.startswith("https://"):
        return source
    if source.startswith(("http://", "file://", "git@", "ssh://")):
        raise PluginSourceError("Only https:// URLs or 'owner/repo' shorthand are allowed.")
    segments = source.strip("/").split("/")
    if not (2 <= len(segments) <= 4):
        raise PluginSourceError("Use an https:// Git URL or 'owner/repo[/subdir]' shorthand.")
    for segment in segments:
        if not segment or segment in (".", "..") or not _SAFE_SEGMENT_RE.match(segment):
            raise PluginSourceError(f"Invalid source segment: {segment!r}")
    return source


def validate_plugin_name(name: str, installed: list[dict]) -> str:
    """Reject path traversal and names the current installed list doesn't recognize.

    Checking membership in ``installed`` (itself sourced from ``hermes
    plugins list --json``, disk truth) means update/remove can never be
    pointed at an arbitrary string the CLI's own sanitizer would have to
    catch -- the WebUI only ever asks for a name it just saw listed.
    """
    name = str(name or "").strip()
    if not name or not _SAFE_NAME_RE.match(name):
        raise PluginSourceError("Invalid plugin name.")
    if not any(p.get("name") == name for p in installed):
        raise LookupError(f"Plugin '{name}' is not installed.")
    return name


def _run_env() -> tuple[list[str], dict]:
    """Return the ``[hermes, --profile, ...]`` argv prefix and env for the active profile."""
    hermes_cmd = _resolve_hermes_command()
    active_home, cli_profile = _gateway_restart_profile_context()
    env = os.environ.copy()
    env["HERMES_HOME"] = str(active_home)
    prefix = [hermes_cmd]
    if cli_profile is not None:
        prefix.extend(["--profile", cli_profile])
    return prefix, env


def list_installed_plugins() -> list[dict]:
    """``hermes plugins list --json``, parsed into ``{name, version, source, enabled}`` rows.

    Raises RuntimeError on a non-zero exit, timeout, or unparsable output --
    callers treat that as "nothing to show" rather than surfacing a raw
    stack trace or subprocess output to the client.
    """
    prefix, env = _run_env()
    cmd = prefix + ["plugins", "list", "--json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_LIST_TIMEOUT_SECONDS, env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"Failed to list plugins: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "hermes plugins list failed").strip())
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse plugin list: {exc}") from exc

    installed = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        installed.append({
            "name": str(entry.get("name") or ""),
            "version": str(entry.get("version") or "") or None,
            "source": str(entry.get("source") or "") or None,
            "enabled": str(entry.get("status") or "").strip().lower() == "enabled",
        })
    return installed


def get_status() -> dict:
    """Current lifecycle status: availability, in-flight action, last result, installed list."""
    with _LOCK:
        running = _RUNNING
        last = dict(_LAST) if _LAST else None

    available = is_available()
    installed: list[dict] = []
    if available:
        try:
            installed = list_installed_plugins()
        except RuntimeError:
            installed = []
    return {
        "available": available,
        "running": running,
        "last": last,
        "installed": installed,
    }


def _build_action_command(action: str, arg: str, *, force: bool, enable: Optional[bool]) -> list[str]:
    prefix, _env = _run_env()
    if action == "install":
        cmd = prefix + ["plugins", "install", arg]
        if force:
            cmd.append("--force")
        if enable is True:
            cmd.append("--enable")
        elif enable is False:
            cmd.append("--no-enable")
        return cmd
    if action == "update":
        return prefix + ["plugins", "update", arg]
    if action == "remove":
        return prefix + ["plugins", "remove", arg]
    raise ValueError(f"unknown action: {action!r}")


def start_action(
    action: str, arg: str, *, force: bool = False, enable: Optional[bool] = None,
) -> tuple[bool, dict]:
    """Start install/update/remove as a background subprocess.

    Single-flight: only one plugin lifecycle action may run at a time.
    Returns ``(started, status)`` -- ``started=False`` means another action
    is already running (the caller maps that to HTTP 409); ``status`` is the
    current status either way, so a 409 response body still shows what's
    in-flight.
    """
    global _RUNNING
    with _LOCK:
        already_running = _RUNNING
        if not already_running:
            _RUNNING = True
    if already_running:
        # get_status() acquires _LOCK itself -- it MUST be called after the
        # `with` block above has released it. _LOCK is a plain (non-reentrant)
        # Lock, so calling it while still held here would deadlock this
        # thread forever the first time two lifecycle requests ever raced.
        return False, get_status()

    cmd = _build_action_command(action, arg, force=force, enable=enable)
    _prefix, env = _run_env()

    def _run() -> None:
        global _RUNNING, _LAST
        ok = False
        log = ""
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_ACTION_TIMEOUT_SECONDS, env=env,
            )
            log = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            log = f"Timed out after {_ACTION_TIMEOUT_SECONDS}s."
        except OSError as exc:
            log = f"Failed to run hermes CLI: {exc}"
        finally:
            if len(log) > _LOG_TAIL_MAX_BYTES:
                log = log[-_LOG_TAIL_MAX_BYTES:]
            with _LOCK:
                _LAST = {
                    "action": action,
                    "name": arg,
                    "ok": ok,
                    "log_tail": log,
                    "finished_at": time.time(),
                }
                _RUNNING = False

    threading.Thread(target=_run, daemon=True).start()
    return True, get_status()
