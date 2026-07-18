"""Background runner + search for the Skills panel's "Hub" tab.

Wraps ``hermes skills search/install/update/uninstall`` as fresh CLI
subprocesses. This module deliberately never imports ``tools.skills_hub`` or
``tools.skills_guard`` into the long-lived WebUI process: both cache
``SKILLS_DIR``/``HERMES_HOME`` at import time, the same problem
``tools.skills_tool`` has for the *local* skills panel (see
``api/profiles.py``'s ``patch_skill_home_modules`` / ``snapshot_skill_home_modules``
/ ``restore_skill_home_modules``, which exist only to work around that for the
one module the WebUI *does* import). A subprocess spawned fresh per request
always resolves paths from the ``HERMES_HOME`` env var set on its own
environment, so it can never go stale across a WebUI profile switch.

Pre-install scanning without installing: ``hermes skills install <id>`` runs
its security scan and prints the verdict *before* the "Confirm [y/N]:"
prompt and *before* any file is copied into ``skills/`` (verified by reading
``hermes_cli/skills_hub.py::do_install`` and by a live dry run against a
throwaway HERMES_HOME). There is no non-mutating "scan only" CLI subcommand,
so the "scan" action here pre-feeds the subprocess's stdin with "n" (decline
the prompt) via a pipe -- the CLI's own cancel path removes the quarantine
directory, so this never leaves a partial install or reads any scan module in
this process. The real "install" action instead passes ``--yes`` (and
optionally ``--force``), which is a normal, always-available CLI flag.

Uninstall has no ``--yes``/``-y`` flag in the CLI (verified via
``hermes skills uninstall --help``) and the plain CLI path always prompts, so
this module pre-feeds "y" the same way; the WebUI's own confirm dialog is the
real consent gate for that action, the same client-confirm-then-fire pattern
used elsewhere in this codebase for destructive server actions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ACTIONS = ("scan", "install", "update", "uninstall")

_ACTION_TIMEOUT_SECONDS = 300
_SEARCH_TIMEOUT_SECONDS = 35
_LOG_TAIL_MAX_CHARS = 200_000

# Held for the lifetime of a running scan/install/update/uninstall. Search is
# read-only and stateless, so it does not take this lock.
_ACTION_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()

_STATE: dict = {
    "action": None,
    "target": None,  # identifier (scan/install) or name (uninstall/update)
    "status": "idle",  # idle | running | completed | failed | timeout
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log": "",
    "error": None,
    "scan_result": None,  # populated whenever the transcript contains a scan report
}


def _resolve_hermes_command() -> str:
    """Resolve the CLI path (mirrors api/gateway_restart.py's helper)."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return hermes_cmd
    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return "hermes"


def _command_for_action(
    action: str,
    target: str,
    *,
    category: str = "",
    name_override: str = "",
    force: bool = False,
) -> list[str]:
    hermes_cmd = _resolve_hermes_command()
    # "--" marks the end of option parsing for argparse, so identifier/name
    # values that start with "-" (e.g. a hub identifier or a plugin/skill
    # name an attacker crafted to look like a flag) are always taken as the
    # positional argument instead of being swallowed as an unrecognized
    # option -- verified live: without "--", `hermes skills install "-x/-y"`
    # never reaches the identifier positional at all (argparse rejects it
    # before the CLI's own logic runs); with "--" it does. It must be the
    # LAST token before the positional, with every real flag placed before
    # it -- argparse treats everything after "--" as positional, so a flag
    # placed after "--" would itself be swallowed as a second positional.
    if action == "scan":
        # No --yes / --force: runs the scan, then stops at (or before) the
        # confirm prompt. Never installs anything on its own.
        return [hermes_cmd, "skills", "install", "--", target]
    if action == "install":
        cmd = [hermes_cmd, "skills", "install", "--yes"]
        if force:
            cmd.append("--force")
        if category:
            cmd.extend(["--category", category])
        if name_override:
            cmd.extend(["--name", name_override])
        cmd.extend(["--", target])
        return cmd
    if action == "update":
        cmd = [hermes_cmd, "skills", "update"]
        if target:
            cmd.extend(["--", target])
        return cmd
    if action == "uninstall":
        return [hermes_cmd, "skills", "uninstall", "--", target]
    raise ValueError(f"unsupported hub action: {action!r}")


# stdin pre-answer for the CLI's single "Confirm [y/N]:" prompt, or None to
# leave stdin closed (DEVNULL) for actions that never prompt.
_STDIN_ANSWER = {
    "scan": "n\n",
    "install": None,  # --yes skips the prompt
    "update": None,  # do_update() forces skip_confirm internally
    "uninstall": "y\n",
}


# ---------------------------------------------------------------------------
# Scan report parsing (best-effort text parsing of a private CLI transcript --
# see tools.skills_guard.format_scan_report; not a stable JSON API)
# ---------------------------------------------------------------------------

# The parenthesized part is "<source>/<trust_level>" where <source> itself may
# contain slashes (it is often the full hub identifier, e.g.
# "skills-sh/anthropics/skills/pdf/trusted") -- trust_level is always one of
# exactly three known values, so anchor on that fixed set from the right
# rather than splitting on the first slash (verified against live output).
_SCAN_HEADER_RE = re.compile(
    r"^Scan:\s+(?P<name>.+?)\s+\((?P<source>.+)/(?P<trust_level>builtin|trusted|community)\)"
    r"\s+Verdict:\s+(?P<verdict>SAFE|CAUTION|DANGEROUS)\s*$"
)
_DECISION_RE = re.compile(
    r"^Decision:\s+(?P<decision>ALLOWED|NEEDS CONFIRMATION|BLOCKED)\s+—\s+(?P<reason>.+)$"
)
_FINDING_RE = re.compile(
    r'^\s{2}(?P<severity>CRITICAL|HIGH|MEDIUM|LOW)\s+(?P<category>\S+)\s+(?P<location>\S+)\s+"(?P<match>.*)"\s*$'
)


def parse_scan_report(log_text: str) -> dict | None:
    """Extract the structured scan verdict from a captured CLI transcript.

    Returns ``None`` when no scan header line is found (e.g. the identifier
    could not be resolved at all) -- callers must treat that as "no verdict
    available", never as a safe/clean result.
    """
    header = None
    decision = None
    findings = []
    for raw_line in log_text.splitlines():
        line = raw_line.rstrip()
        if header is None:
            m = _SCAN_HEADER_RE.match(line)
            if m:
                header = m.groupdict()
                continue
        m = _FINDING_RE.match(line)
        if m:
            findings.append(m.groupdict())
            continue
        m = _DECISION_RE.match(line)
        if m:
            decision = m.groupdict()
    if header is None:
        return None
    return {
        "name": header["name"],
        "source": header["source"],
        "trust_level": header["trust_level"],
        "verdict": header["verdict"].lower(),
        "findings": findings,
        "decision": (decision or {}).get("decision"),
        "decision_reason": (decision or {}).get("reason"),
    }


# ---------------------------------------------------------------------------
# Search (synchronous, read-only, no single-flight lock)
# ---------------------------------------------------------------------------


def search_hub_skills(query: str, *, source: str = "all", limit: int = 20) -> dict:
    """Run ``hermes skills search <query> --json`` and parse its stdout.

    Synchronous: search is read-only and typically completes well within
    ``_SEARCH_TIMEOUT_SECONDS`` (the CLI itself caps its own fan-out at 30s).
    Raises ``RuntimeError`` on a non-zero exit or unparseable output so the
    route layer can turn that into a clean error response.
    """
    from api.profiles import get_active_hermes_home

    hermes_home = Path(get_active_hermes_home())
    hermes_cmd = _resolve_hermes_command()
    cmd = [
        hermes_cmd,
        "skills",
        "search",
        query,
        "--source",
        source or "all",
        "--limit",
        str(max(1, min(int(limit or 20), 50))),
        "--json",
    ]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    try:
        result = subprocess.run(
            cmd,
            cwd=str(hermes_home) if hermes_home.exists() else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Search timed out after {_SEARCH_TIMEOUT_SECONDS}s") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Search failed: {detail or 'exit code ' + str(result.returncode)}")

    try:
        results = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Search returned unparseable output") from exc

    if not isinstance(results, list):
        raise RuntimeError("Search returned unexpected output shape")
    return {"results": results}


# ---------------------------------------------------------------------------
# Installed hub skills (read the lock file directly -- no module import)
# ---------------------------------------------------------------------------


def list_installed_hub_skills(skills_dir: Path) -> list[dict]:
    """Read ``<skills_dir>/.hub/lock.json`` and return its installed entries.

    Reads the file directly with ``json.load`` -- never imports
    ``tools.skills_hub.HubLockFile`` -- so this has none of the import-time
    ``SKILLS_DIR`` binding risk described in the module docstring. Best
    effort: a missing or unparseable lock file yields an empty list rather
    than raising, matching how a profile with no hub installs looks.
    """
    lock_path = Path(skills_dir) / ".hub" / "lock.json"
    if not lock_path.exists():
        return []
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("Failed to read hub lock file at %s", lock_path, exc_info=True)
        return []
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        return []
    out = []
    for name, entry in installed.items():
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "name": name,
                "source": entry.get("source"),
                "identifier": entry.get("identifier"),
                "trust_level": entry.get("trust_level"),
                "scan_verdict": entry.get("scan_verdict"),
                "install_path": entry.get("install_path"),
                "installed_at": entry.get("installed_at"),
                "updated_at": entry.get("updated_at"),
            }
        )
    out.sort(key=lambda e: (e.get("name") or ""))
    return out


# ---------------------------------------------------------------------------
# Single-flight background runner (scan / install / update / uninstall)
# ---------------------------------------------------------------------------


def _append_log(text: str) -> None:
    if not text:
        return
    with _STATE_LOCK:
        _STATE["log"] += text
        if len(_STATE["log"]) > _LOG_TAIL_MAX_CHARS:
            _STATE["log"] = _STATE["log"][-_LOG_TAIL_MAX_CHARS:]


def _drain_stdout(stream) -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            _append_log(line)
    except Exception:
        logger.debug("skills hub log reader failed", exc_info=True)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def get_status() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def start_action(
    action: str,
    target: str = "",
    *,
    category: str = "",
    name_override: str = "",
    force: bool = False,
) -> tuple[bool, dict]:
    """Start *action* in the background if no other hub action is running.

    Returns ``(started, status_snapshot)``; ``started=False`` means another
    hub action is already running and the caller should respond 409.
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported hub action: {action!r}")
    if action in ("scan", "install") and not target:
        raise ValueError("identifier is required")
    if action == "uninstall" and not target:
        raise ValueError("name is required")

    if not _ACTION_LOCK.acquire(blocking=False):
        return False, get_status()

    # Imported locally (not at module load) so the active profile is always
    # resolved fresh -- see the module docstring's SKILLS_DIR warning, and
    # api.routes._run_gateway_lifecycle_command for the established pattern.
    from api.profiles import get_active_hermes_home

    # get_active_hermes_home() can raise (profile resolution failure) just
    # like the command-building call below -- both must stay inside this
    # try/except. If it raised outside of it, the lock acquired above would
    # never be released, and every subsequent hub action would 409 forever
    # until the WebUI process restarts.
    try:
        hermes_home = Path(get_active_hermes_home())
        cmd = _command_for_action(
            action, target, category=category, name_override=name_override, force=force
        )
    except Exception:
        _ACTION_LOCK.release()
        raise

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    # Rich (used for all CLI console output here) wraps text to the detected
    # console width even when stdout is not a tty; a narrow default width
    # (80) breaks scan-report finding lines across two lines and defeats the
    # line-anchored regexes above. A wide COLUMNS makes the transcript
    # reliably single-line per record (verified live).
    env["COLUMNS"] = "400"

    stdin_answer = _STDIN_ANSWER.get(action)

    with _STATE_LOCK:
        _STATE.update(
            {
                "action": action,
                "target": target or None,
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "returncode": None,
                "log": "",
                "error": None,
                "scan_result": None,
            }
        )

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_answer is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(hermes_home) if hermes_home.exists() else None,
        )
    except Exception as exc:
        logger.exception("Failed to spawn skills hub action %r", action)
        with _STATE_LOCK:
            _STATE["status"] = "failed"
            _STATE["finished_at"] = time.time()
            _STATE["error"] = f"{type(exc).__name__}: {exc}"
        _ACTION_LOCK.release()
        return True, get_status()

    if stdin_answer is not None:
        try:
            proc.stdin.write(stdin_answer)
            proc.stdin.close()
        except Exception:
            logger.debug("Failed to pre-answer hub subprocess stdin for %r", action, exc_info=True)

    def _run() -> None:
        timed_out = False
        try:
            reader = threading.Thread(target=_drain_stdout, args=(proc.stdout,), daemon=True)
            reader.start()
            try:
                returncode = proc.wait(timeout=_ACTION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                logger.error(
                    "skills hub action %r timed out after %ss; killing process",
                    action,
                    _ACTION_TIMEOUT_SECONDS,
                )
                proc.kill()
                try:
                    returncode = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    returncode = -9
                    # SIGKILL should be near-instant; if the process is
                    # still not reaped after 5s (e.g. stuck in
                    # uninterruptible I/O) it would otherwise sit as a
                    # zombie forever. Keep waiting on a throwaway daemon
                    # thread -- a blocking, no-timeout proc.wait() -- so
                    # the OS process table entry is eventually reclaimed
                    # without delaying the timeout status reported below.
                    threading.Thread(
                        target=proc.wait,
                        name=f"hermes-webui-skills-hub-{action}-reap",
                        daemon=True,
                    ).start()
            reader.join(timeout=5)

            with _STATE_LOCK:
                _STATE["returncode"] = returncode
                _STATE["finished_at"] = time.time()
                scan_result = parse_scan_report(_STATE["log"])
                if scan_result is not None:
                    _STATE["scan_result"] = scan_result
                if timed_out:
                    _STATE["status"] = "timeout"
                    _STATE["error"] = f"Action timed out after {_ACTION_TIMEOUT_SECONDS}s and was killed"
                elif returncode == 0:
                    _STATE["status"] = "completed"
                else:
                    _STATE["status"] = "failed"
                    _STATE["error"] = f"Exited with code {returncode}"
        except Exception as exc:
            logger.exception("skills hub action %r runner failed", action)
            with _STATE_LOCK:
                _STATE["status"] = "failed"
                _STATE["finished_at"] = time.time()
                _STATE["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _ACTION_LOCK.release()

    threading.Thread(target=_run, name=f"hermes-webui-skills-hub-{action}", daemon=True).start()
    return True, get_status()
