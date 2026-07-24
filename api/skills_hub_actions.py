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
import signal
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

# Captured CLI output is useful to diagnose hub actions, but it is a browser
# response surface. Redact common inline secret assignments while leaving
# ordinary status/progress output readable. Scan finding bodies are handled
# separately below because a finding is explicitly a matched source fragment.
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    r"[a-z][a-z0-9_]*(?:access_key|secret_access_key)|cookie|session(?:[_-]?(?:id|token|key))?)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_AUTHORIZATION_BASIC_RE = re.compile(r"(?i)\bauthorization\s*:\s*basic\s+[^\s,;]+")
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\r\n]+")

# Per-profile run state (gate finding 3): action status/log/scan metadata are
# owned by the profile that started the run — another profile neither sees
# them nor is blocked by them (each profile home is an independent skills
# store, so cross-profile serialization would only couple unrelated stores).
_REGISTRY_LOCK = threading.Lock()

_IDLE_STATE: dict = {
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


class _ProfileHub:
    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.state_lock = threading.Lock()
        self.state = dict(_IDLE_STATE)
        self.running = False


_PROFILES: dict[str, _ProfileHub] = {}


def _profile_hub(profile: str) -> _ProfileHub:
    key = str(profile or "default")
    with _REGISTRY_LOCK:
        hub = _PROFILES.get(key)
        if hub is None:
            hub = _ProfileHub(key)
            _PROFILES[key] = hub
        return hub


# ── Evidence-based completion (gate finding 2): the CLI exits 0 for fetch
# failures, scanner blocks, already-installed no-ops and invalid uninstall
# targets, so the return code alone is NOT authority. An action counts as
# completed only with the CLI's own positive success evidence in the
# transcript; blocked/no-op/evidence-less runs fail. All markers below are
# the CLI's real output strings (hermes_cli/skills_hub.py / tools/skills_hub.py).
_UPDATED_RE = re.compile(r"Updated (\d+) skill\(s\)")


def _evaluate_outcome(action: str, log: str, scan_result) -> tuple[bool, str | None]:
    """Return (ok, error). rc==0 and no timeout are already established."""
    decision = (scan_result or {}).get("decision") if isinstance(scan_result, dict) else None
    if action == "scan":
        if scan_result is not None:
            return True, None
        return False, "The CLI produced no scan report for this identifier."
    if action == "install":
        if decision == "BLOCKED":
            return False, "Blocked by the security scan."
        if "Installed:" in log:
            return True, None
        if "is already installed at" in log:
            return False, "Already installed — nothing was changed."
        return False, "The CLI reported no completed install."
    if action == "update":
        m = _UPDATED_RE.search(log)
        if m and int(m.group(1)) > 0:
            return True, None
        if "No updates available." in log:
            return False, "No updates available — nothing was changed."
        if decision == "BLOCKED":
            return False, "Update blocked by the security scan."
        return False, "The CLI reported no completed update."
    if action == "uninstall":
        if "Uninstalled '" in log:
            return True, None
        if "is not a hub-installed skill" in log:
            return False, "Not a hub-installed skill — nothing was removed."
        return False, "The CLI reported no completed uninstall."
    return False, f"Unknown action {action!r}"


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
        # NO --force, ever (gate finding 1): the browser must not be able to
        # override a security verdict. Replacement flows go through the
        # scan-gated update pipeline instead.
        cmd = [hermes_cmd, "skills", "install", "--yes"]
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
# A terminal renderer may wrap the quoted ``match`` field before its closing
# quote. Keep this deliberately narrower than a generic indented log line: it
# recognizes only the fixed scan-finding prefix, including an incomplete record.
_FINDING_RECORD_START_RE = re.compile(
    r"^\s{2}(?:CRITICAL|HIGH|MEDIUM|LOW)\s+\S+\s+\S+(?:\s|$)"
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


def _redact_sensitive_text(text: object) -> object:
    """Return text safe for browser-visible action status responses."""
    if not isinstance(text, str):
        return text
    text = _COOKIE_HEADER_RE.sub("Cookie: <redacted>", text)
    text = _AUTHORIZATION_BASIC_RE.sub("Authorization: Basic <redacted>", text)
    text = _INLINE_SECRET_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}<redacted>", text
    )
    return _BEARER_TOKEN_RE.sub("Bearer <redacted>", text)


def _redact_scan_finding_lines(log: object) -> object:
    """Fail closed after a raw scan finding enters a browser transcript.

    ``tools.skills_guard.format_scan_report`` writes ``Finding.match`` directly
    into a finding line. Its ``match[:60]`` value can contain newlines, blank
    rows, or text shaped like a later ``Scan:``/``Decision:`` line, so no raw
    line after a finding prefix is a trustworthy record delimiter. Keep only
    independent progress emitted before that prefix; the private runner
    transcript in ``_STATE`` remains intact and ``scan_result`` supplies the
    browser's structured, match-free finding metadata.
    """
    if not isinstance(log, str):
        return log
    safe_lines = []
    for raw_line in log.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if _FINDING_RECORD_START_RE.match(line):
            newline = raw_line[len(line):]
            safe_lines.append("[scan finding redacted; remaining scan transcript suppressed]" + newline)
            break
        safe_lines.append(raw_line)
    return "".join(safe_lines)


def _safe_scan_decision(scan_result: dict) -> str | None:
    """Derive the scanner's non-force decision without accepting raw report text."""
    trust_level = scan_result.get("trust_level")
    verdict = scan_result.get("verdict")
    if trust_level not in {"builtin", "trusted", "community"}:
        return None
    if verdict not in {"safe", "caution", "dangerous"}:
        return None
    if trust_level == "builtin" or (trust_level == "trusted" and verdict != "dangerous"):
        return "ALLOWED"
    if trust_level == "community" and verdict == "safe":
        return "ALLOWED"
    return "BLOCKED"


def _safe_status_projection(state: dict) -> dict:
    """Project private runner state onto the Skills Hub browser API contract."""
    status = dict(state)
    status["log"] = _redact_sensitive_text(_redact_scan_finding_lines(status.get("log", "")))
    status["error"] = _redact_sensitive_text(status.get("error"))
    scan_result = status.get("scan_result")
    if isinstance(scan_result, dict):
        safe_scan_result = {
            key: scan_result[key]
            for key in ("name", "source", "trust_level", "verdict")
            if key in scan_result
        }
        decision = _safe_scan_decision(scan_result)
        if decision is not None:
            safe_scan_result["decision"] = decision
        findings = scan_result.get("findings")
        if isinstance(findings, list):
            # `match` is a raw fragment of scanned Skill content. Its metadata
            # remains useful to explain the verdict, but never expose its body.
            safe_scan_result["findings"] = [
                {
                    key: finding[key]
                    for key in ("severity", "category", "location")
                    if key in finding
                }
                if isinstance(finding, dict)
                else finding
                for finding in findings
            ]
        status["scan_result"] = safe_scan_result
    return status


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
    # Keep all actual options before the end-of-options marker. The query is
    # user-controlled and must remain positional even when it starts with "-".
    cmd = [
        hermes_cmd,
        "skills",
        "search",
        "--source",
        source or "all",
        "--limit",
        str(max(1, min(int(limit or 20), 50))),
        "--json",
        "--",
        query,
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


def _append_log(hub: "_ProfileHub", gen: int, text: str) -> None:
    if not text:
        return
    with hub.state_lock:
        if hub.state.get("_gen") != gen:
            return  # late reader from a superseded run: never poison new state
        hub.state["log"] += text
        if len(hub.state["log"]) > _LOG_TAIL_MAX_CHARS:
            hub.state["log"] = hub.state["log"][-_LOG_TAIL_MAX_CHARS:]


def _drain_stdout(hub: "_ProfileHub", gen: int, stream) -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            _append_log(hub, gen, line)
    except Exception:
        logger.debug("skills hub log reader failed", exc_info=True)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _busy_snapshot() -> dict:
    """What one profile may learn about ANOTHER profile's world: nothing.
    Each profile has its own slot; this envelope only exists for legacy
    callers that query without a resolvable profile."""
    return {"status": "busy", "busy": True}


def get_status(profile: str) -> dict:
    hub = _profile_hub(profile)
    with hub.state_lock:
        snapshot = dict(hub.state)
    snapshot.pop("_gen", None)
    return _safe_status_projection(snapshot)


def start_action(
    action: str,
    target: str = "",
    *,
    category: str = "",
    name_override: str = "",
    identifier: str = "",
    profile: str = "default",
) -> tuple[bool, dict]:
    """Start *action* for *profile* if that profile's slot is free.

    Single-flight PER PROFILE (gate finding 3): profiles neither see nor
    block each other — their skill stores are independent. Returns
    ``(started, status_snapshot)``; ``started=False`` → HTTP 409 with the
    caller's OWN state.

    ``update`` runs a two-phase, fail-closed pipeline (gate finding 1): a
    plain scan of the installed skill's identifier first, and the actual
    update only when the scanner's NON-FORCE decision is ALLOWED — the
    browser can never override a security verdict, and a denial leaves the
    installed tree untouched because the update CLI is never invoked.
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported hub action: {action!r}")
    if action in ("scan", "install") and not target:
        raise ValueError("identifier is required")
    if action == "uninstall" and not target:
        raise ValueError("name is required")
    if action == "update" and not identifier:
        raise ValueError(
            "identifier is required for a verified update (the scan phase "
            "needs the hub identifier of the installed skill)"
        )

    hub = _profile_hub(profile)
    with hub.state_lock:
        if hub.running:
            already = True
        else:
            hub.running = True
            already = False
    if already:
        return False, get_status(profile)

    # Imported locally (not at module load) so the active profile is always
    # resolved fresh -- see the module docstring's SKILLS_DIR warning, and
    # api.routes._run_gateway_lifecycle_command for the established pattern.
    from api.profiles import get_active_hermes_home

    # get_active_hermes_home() can raise (profile resolution failure) just
    # like the command-building calls below -- both must stay inside this
    # try/except so the slot reserved above is always released on failure.
    try:
        hermes_home = Path(get_active_hermes_home())
        phases: list[tuple[str, list[str], str | None]] = []
        if action == "update":
            phases.append(
                ("scan", _command_for_action("scan", identifier), _STDIN_ANSWER.get("scan"))
            )
            phases.append(
                ("update", _command_for_action("update", target), _STDIN_ANSWER.get("update"))
            )
        else:
            phases.append(
                (
                    action,
                    _command_for_action(
                        action, target, category=category, name_override=name_override
                    ),
                    _STDIN_ANSWER.get(action),
                )
            )
    except Exception:
        with hub.state_lock:
            hub.running = False
        raise

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    # Rich (used for all CLI console output here) wraps text to the detected
    # console width even when stdout is not a tty; a narrow default width
    # (80) breaks scan-report finding lines across two lines and defeats the
    # line-anchored regexes above. A wide COLUMNS makes the transcript
    # reliably single-line per record (verified live).
    env["COLUMNS"] = "400"

    with hub.state_lock:
        gen = int(hub.state.get("_gen") or 0) + 1
        hub.state = dict(_IDLE_STATE)
        hub.state.update(
            {
                "_gen": gen,
                "action": action,
                "target": target or None,
                "status": "running",
                "started_at": time.time(),
            }
        )

    def _signal_group(proc, sig: int) -> None:
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                os.killpg(pid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.send_signal(sig)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _exec_phase(cmd: list[str], stdin_answer: str | None) -> tuple[int, bool]:
        """Run one CLI phase, streaming into the log. Returns (rc, timed_out).

        The subprocess owns its process GROUP, a timeout escalates
        SIGTERM->SIGKILL against the group, and this function does not
        return -- so the profile slot is not released -- until the leader
        is actually reaped (review P0: git/scanner children must never
        outlive the run into a newly admitted one).
        """
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_answer is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(hermes_home) if hermes_home.exists() else None,
            start_new_session=True,
        )
        if stdin_answer is not None:
            try:
                proc.stdin.write(stdin_answer)
                proc.stdin.close()
            except Exception:
                logger.debug("Failed to pre-answer hub subprocess stdin", exc_info=True)
        reader = threading.Thread(
            target=_drain_stdout, args=(hub, gen, proc.stdout), daemon=True
        )
        reader.start()
        try:
            rc = proc.wait(timeout=_ACTION_TIMEOUT_SECONDS)
            timed = False
        except subprocess.TimeoutExpired:
            timed = True
            logger.error(
                "skills hub action %r timed out after %ss; terminating process group",
                action,
                _ACTION_TIMEOUT_SECONDS,
            )
            _signal_group(proc, signal.SIGTERM)
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                # Reap-before-release: block until the leader is gone. A
                # process stuck in uninterruptible I/O keeps this profile's
                # slot closed -- honest 409s instead of overlapping trees.
                rc = proc.wait()
        reader.join(timeout=5)
        return rc, timed

    def _finalize(status: str, rc, error: str | None) -> None:
        with hub.state_lock:
            if hub.state.get("_gen") != gen:
                return
            hub.state["returncode"] = rc
            hub.state["finished_at"] = time.time()
            hub.state["status"] = status
            hub.state["error"] = error
            scan_result = parse_scan_report(hub.state["log"])
            if scan_result is not None:
                hub.state["scan_result"] = scan_result

    def _run() -> None:
        try:
            for phase_name, cmd, stdin_answer in phases:
                rc, timed = _exec_phase(cmd, stdin_answer)
                if timed:
                    _finalize(
                        "timeout",
                        rc,
                        f"Action timed out after {_ACTION_TIMEOUT_SECONDS}s and was killed",
                    )
                    return
                if rc != 0:
                    _finalize("failed", rc, f"Exited with code {rc}")
                    return
                if action == "update" and phase_name == "scan":
                    # Fail-closed verdict gate BETWEEN the phases: the update
                    # CLI is only ever invoked for a skill whose scan would be
                    # allowed WITHOUT any force/override semantics.
                    with hub.state_lock:
                        scan_result = parse_scan_report(hub.state["log"])
                        if scan_result is not None:
                            hub.state["scan_result"] = scan_result
                    decision = (
                        _safe_scan_decision(scan_result)
                        if isinstance(scan_result, dict)
                        else None
                    )
                    if decision != "ALLOWED":
                        _finalize(
                            "failed",
                            rc,
                            "Update refused: the security scan did not allow "
                            "this skill (installed version left untouched).",
                        )
                        return
                    _append_log(hub, gen, "\n--- scan allowed; updating ---\n")
            with hub.state_lock:
                log_text = hub.state["log"]
                scan_result = parse_scan_report(log_text)
            ok, error = _evaluate_outcome(action, log_text, scan_result)
            _finalize("completed" if ok else "failed", 0, error)
        except Exception as exc:
            logger.exception("skills hub action %r runner failed", action)
            _finalize("failed", None, f"{type(exc).__name__}: {exc}")
        finally:
            with hub.state_lock:
                hub.running = False

    try:
        threading.Thread(
            target=_run, name=f"hermes-webui-skills-hub-{action}", daemon=True
        ).start()
    except Exception:
        # Thread admission failed (resource exhaustion): _run never ran, its
        # finally can never release the slot -- release synchronously or the
        # profile 409s forever (review P1, same class as plugin-lifecycle).
        with hub.state_lock:
            hub.running = False
            if hub.state.get("_gen") == gen:
                hub.state["status"] = "failed"
                hub.state["finished_at"] = time.time()
                hub.state["error"] = "Could not start the background runner"
        raise
    return True, get_status(profile)
