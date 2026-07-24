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
import signal
import subprocess
import threading
from pathlib import Path
import time
from typing import Optional

from api.gateway_restart import _gateway_restart_profile_context, _resolve_hermes_command

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Keyed by profile ("HERMES_HOME") so one profile's in-flight/last-result
# state -- including any credentials redacted into `name`/`log_tail` -- can
# never be read back through another profile's GET /status (#audit MEDIUM:
# these were process-wide globals, so any authenticated session could see a
# DIFFERENT profile's install source/log regardless of which profile it was
# scoped to).
_RUNNING_PROFILES: set[str] = set()
_LAST_BY_PROFILE: dict[str, dict] = {}  # profile_key -> {"action","name","ok","log_tail","finished_at"}

_ACTION_TIMEOUT_SECONDS = 600
_LOG_TAIL_MAX_BYTES = 200_000
_LIST_TIMEOUT_SECONDS = 30

# Registry-name shorthand segments: safe charset only (letters, digits, dot,
# underscore, hyphen), and must NOT *start* with a hyphen -- otherwise a
# segment like "-force" is indistinguishable from a CLI flag once it lands in
# argv (see hermes_cli's own `skills install` flag set for why this matters:
# an unvalidated leading-hyphen identifier can get consumed by argparse as an
# option instead of the positional value it was meant to be).
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")

# Plugin directory names on disk (hermes_cli.plugins_cmd._sanitize_plugin_name
# for a fresh install path never allows a subdir) -- flat, safe charset, no
# leading hyphen (same argv-ambiguity reasoning as _SAFE_SEGMENT_RE above).
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")

# Redacts HTTP Basic-auth-style userinfo embedded in a URL (e.g.
# "https://user:token@host/repo.git" -> "https://***@host/repo.git") before
# anything derived from an install source is stored where GET
# /api/plugins/lifecycle/status can return it. validate_source() only checks
# the https:// prefix, not what follows, so a credential-bearing URL reaches
# this module; the credential itself must still reach the `hermes` subprocess
# (it needs it to actually clone), so this is applied only at the
# storage/display boundary, never to the argv passed to subprocess.run.
_URL_USERINFO_RE = re.compile(r"://[^/\s]*@")
# Secrets carried in query/fragment parameters (?token=..., #access_token=...)
# survive the userinfo scrub; catch the common credential-ish parameter names.
_URL_SECRET_PARAM_RE = re.compile(
    r"([?&#][A-Za-z0-9_\-]*(?:token|secret|key|password|passwd|auth|bearer|credential)"
    r"[A-Za-z0-9_\-]*=)[^&#\s]+",
    re.IGNORECASE,
)


def _redact_credentials(text: str) -> str:
    text = _URL_USERINFO_RE.sub("://***@", text or "")
    return _URL_SECRET_PARAM_RE.sub(r"\1***", text)


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
        # Review P1: the source reaches the CLI via argv, visible to local
        # process inspection, and the display-side redaction cannot help
        # there. Until the agent grows a secret-safe source/credential
        # channel, refuse URLs that carry secrets in-band: userinfo
        # (user:token@host), query strings, or fragments.
        rest = source[len("https://"):]
        host_part = rest.split("/", 1)[0]
        if "@" in host_part:
            raise PluginSourceError(
                "Credentials in the URL are not accepted (they would be visible "
                "in the process list). Configure Git credentials on the server "
                "instead."
            )
        if "?" in source or "#" in source:
            raise PluginSourceError(
                "Query strings and fragments are not accepted in plugin source "
                "URLs (tokens there would be visible in the process list)."
            )
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


def _user_plugins_dir() -> Path:
    active_home, _cli_profile = _gateway_restart_profile_context()
    return Path(active_home) / "plugins"


def _manifest_name(plugin_dir) -> Optional[str]:
    """Best-effort ``name:`` from ``plugin.yaml`` -- display metadata only."""
    manifest = plugin_dir / "plugin.yaml"
    try:
        if not manifest.is_file() or manifest.is_symlink():
            return None
        for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^name:\s*['\"]?([^'\"#]+)", line.strip())
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return None


def _actionable_plugin_dirs() -> dict:
    """Disk truth: ``{directory-name: manifest-name-or-None}`` for the active
    profile's user plugin directory. The DIRECTORY name is the identity the
    CLI's update/remove actually key on; the manifest ``name`` is mutable
    display data a hostile checkout controls."""
    out: dict = {}
    try:
        root = _user_plugins_dir()
        if not root.is_dir():
            return out
        for child in root.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if not _SAFE_NAME_RE.match(child.name):
                continue
            out[child.name] = _manifest_name(child)
    except OSError:
        pass
    return out


def validate_plugin_name(name: str, installed: list[dict]) -> str:
    """Authorize update/remove against the ACTIONABLE identity, not display data.

    The CLI keys update/remove on the plugin's directory name; ``plugins
    list --json`` reports the manifest ``name`` -- mutable data a checkout
    controls, so it cannot be deletion authority. This resolves the request
    against the on-disk directory set of the active profile:

    * a request matching a directory name directly is authorized as-is;
    * a request matching exactly ONE directory's manifest name resolves to
      that directory -- but only when the mapping is unambiguous;
    * anything ambiguous, unknown, or not present in the CLI listing is
      rejected.
    """
    name = str(name or "").strip()
    if not name or not _SAFE_NAME_RE.match(name):
        raise PluginSourceError("Invalid plugin name.")
    dirs = _actionable_plugin_dirs()
    if name in dirs:
        resolved = name
    else:
        candidates = [d for d, manifest in dirs.items() if manifest == name]
        if len(candidates) > 1:
            raise PluginSourceError(
                f"Plugin name {name!r} is ambiguous across {sorted(candidates)!r}."
            )
        if not candidates:
            raise LookupError(f"Plugin '{name}' is not installed.")
        resolved = candidates[0]
    # The CLI listing remains a secondary gate: only act on identities the
    # CLI itself currently reports (bundled/entry-point rows have no user
    # directory and are never actionable from here).
    listed = {str(p.get("name") or "") for p in installed}
    manifest = dirs.get(resolved)
    if resolved not in listed and (manifest or "") not in listed:
        raise LookupError(f"Plugin '{name}' is not installed.")
    return resolved


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


def _profile_key() -> str:
    """Return a stable identifier for the active profile's HERMES_HOME.

    Used to scope in-flight/last-result state per profile (see
    _RUNNING_PROFILES / _LAST_BY_PROFILE) so one profile's install source or
    subprocess log can never surface in another profile's status response.
    """
    active_home, _cli_profile = _gateway_restart_profile_context()
    return str(active_home)


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
            stdin=subprocess.DEVNULL,
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
    """Current lifecycle status for the ACTIVE profile: availability, in-flight
    action, last result, installed list.

    Profile resolution failure degrades to "no running/last state" rather
    than raising -- this backs the always-readable GET /status route, which
    must never 500 just because it couldn't determine a profile key.
    """
    try:
        profile_key = _profile_key()
    except Exception:
        profile_key = None

    with _LOCK:
        running = profile_key is not None and profile_key in _RUNNING_PROFILES
        last = dict(_LAST_BY_PROFILE[profile_key]) if profile_key in _LAST_BY_PROFILE else None

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


def _build_action_command(
    action: str, arg: str, *, force: bool, enable: Optional[bool],
) -> tuple[list[str], dict]:
    prefix, env = _run_env()
    if action == "install":
        cmd = prefix + ["plugins", "install", arg]
        if force:
            cmd.append("--force")
        if enable is True:
            cmd.append("--enable")
        elif enable is False:
            cmd.append("--no-enable")
        return cmd, env
    if action == "update":
        return prefix + ["plugins", "update", arg], env
    if action == "remove":
        return prefix + ["plugins", "remove", arg], env
    raise ValueError(f"unknown action: {action!r}")


def _kill_process_group(proc: "subprocess.Popen[str]") -> None:
    """Kill the whole process group a timed-out action spawned, not just the
    immediate ``hermes`` child -- git/pip/npm often fork helper processes
    that would otherwise survive as orphans after a plain ``proc.kill()``."""
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # group (or process) already gone -- fall through to proc.kill()
    try:
        proc.kill()
    except Exception:
        pass


def start_action(
    action: str, arg: str, *, force: bool = False, enable: Optional[bool] = None,
) -> tuple[bool, dict]:
    """Start install/update/remove as a background subprocess.

    Single-flight PER PROFILE: only one plugin lifecycle action may run at a
    time for a given HERMES_HOME. Returns ``(started, status)`` --
    ``started=False`` means another action is already running for the active
    profile (the caller maps that to HTTP 409); ``status`` is the current
    status either way, so a 409 response body still shows what's in-flight.
    """
    # Resolve the profile key BEFORE reserving a run slot. If this raises,
    # nothing has been reserved yet, so there is nothing to leak -- the
    # exception propagates as-is (the route layer's generic error handling
    # turns it into a 500, same as any other pre-existing resolution
    # failure). This is what closes the lock-leak: NO code that can raise
    # runs between "slot reserved" and the try/except immediately below that
    # releases it.
    profile_key = _profile_key()

    with _LOCK:
        already_running = profile_key in _RUNNING_PROFILES
        if not already_running:
            _RUNNING_PROFILES.add(profile_key)
    if already_running:
        # get_status() acquires _LOCK itself -- it MUST be called after the
        # `with` block above has released it. _LOCK is a plain (non-reentrant)
        # Lock, so calling it while still held here would deadlock this
        # thread forever the first time two lifecycle requests ever raced.
        return False, get_status()

    try:
        cmd, env = _build_action_command(action, arg, force=force, enable=enable)
    except Exception:
        # Command construction (_resolve_hermes_command / profile context
        # resolution again inside _run_env) can fail the same way profile_key
        # resolution above can -- release the slot we just reserved instead
        # of leaving it permanently held (the bug this fix closes: previously
        # this call sat AFTER the reservation with no matching release).
        with _LOCK:
            _RUNNING_PROFILES.discard(profile_key)
        raise

    def _run() -> None:
        ok = False
        log = ""
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,  # the installer's input()/getpass
                # prompts must fail fast instead of consuming the server's
                # terminal or holding the profile slot for the full timeout
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,  # own process group -- see _kill_process_group
            )
            try:
                stdout, _stderr = proc.communicate(timeout=_ACTION_TIMEOUT_SECONDS)
                ok = proc.returncode == 0
                log = (stdout or "").strip()
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                # Unbounded wait is safe here: the process (and its group)
                # was just SIGKILLed, which cannot be ignored. This also
                # reaps the child so it never lingers as a zombie.
                stdout, _stderr = proc.communicate()
                ok = False
                log = ((stdout or "").strip() + f"\nTimed out after {_ACTION_TIMEOUT_SECONDS}s.").strip()
        except OSError as exc:
            log = f"Failed to run hermes CLI: {exc}"
        finally:
            log = _redact_credentials(log)
            if len(log) > _LOG_TAIL_MAX_BYTES:
                log = log[-_LOG_TAIL_MAX_BYTES:]
            with _LOCK:
                _LAST_BY_PROFILE[profile_key] = {
                    "action": action,
                    "name": _redact_credentials(arg),
                    "ok": ok,
                    "log_tail": log,
                    "finished_at": time.time(),
                }
                _RUNNING_PROFILES.discard(profile_key)

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        # Thread admission failed: _run never ran, its finally cannot release
        # the slot -- release synchronously or this profile 409s until a
        # WebUI restart (review P1).
        with _LOCK:
            _RUNNING_PROFILES.discard(profile_key)
            _LAST_BY_PROFILE[profile_key] = {
                "action": action,
                "name": _redact_credentials(arg),
                "ok": False,
                "log_tail": "Could not start the background runner",
                "finished_at": time.time(),
            }
        raise
    return True, get_status()
