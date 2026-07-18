"""Background runner for the Settings -> System "Maintenance actions" card.

Spawns the active-profile Hermes CLI (``hermes doctor`` / ``hermes security
audit`` / ``hermes backup``) as a subprocess, buffers its combined
stdout+stderr into a bounded log tail, and exposes a poll-friendly status
snapshot for the frontend. Only one action may run at a time.

Gating (fail-closed) is enforced by the caller (api.routes, via the existing
``_truthy_env("HERMES_WEBUI_ALLOW_OPS_ACTIONS")`` pattern) -- this module does
not gate on its own so it stays testable without env-var plumbing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ACTIONS = ("doctor", "security_audit", "backup")

_ACTION_TIMEOUT_SECONDS = 600
_LOG_TAIL_MAX_CHARS = 200_000  # ~200 KB buffered log tail

# Held for the lifetime of a running action (released by the background
# runner thread when the subprocess exits). A non-blocking acquire is how
# callers enforce "only one action at a time" without blocking the request
# thread on the whole subprocess.
_ACTION_LOCK = threading.Lock()
# Guards reads/writes of _STATE. Short critical sections only.
_STATE_LOCK = threading.Lock()

_STATE: dict = {
    "action": None,  # last-started / currently-running action name
    "status": "idle",  # idle | running | completed | failed | timeout
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log": "",
    "backup_path": None,  # set once a backup action completes successfully
    "error": None,
}


def _resolve_hermes_command() -> str:
    """Resolve the CLI path used to spawn ops actions (mirrors gateway_restart)."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return hermes_cmd
    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return "hermes"


def _backups_dir() -> Path:
    """Tempfile-style directory under the WebUI state root, never inside the repo."""
    from api.config import STATE_DIR

    d = STATE_DIR / "ops_backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _command_for_action(action: str, *, backup_dir: Path) -> list[str]:
    hermes_cmd = _resolve_hermes_command()
    if action == "doctor":
        return [hermes_cmd, "doctor"]
    if action == "security_audit":
        return [hermes_cmd, "security", "audit", "--json"]
    if action == "backup":
        output_path = backup_dir / f"hermes-backup-{int(time.time())}.zip"
        return [
            hermes_cmd,
            "backup",
            "--quick",
            "--label",
            "webui-ops",
            "--output",
            str(output_path),
        ]
    raise ValueError(f"unsupported ops action: {action!r}")


def _append_log(text: str) -> None:
    if not text:
        return
    with _STATE_LOCK:
        _STATE["log"] += text
        if len(_STATE["log"]) > _LOG_TAIL_MAX_CHARS:
            _STATE["log"] = _STATE["log"][-_LOG_TAIL_MAX_CHARS:]


def _drain_stdout(stream) -> None:
    """Read the subprocess's combined stdout/stderr into the log tail."""
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            _append_log(line)
    except Exception:
        logger.debug("ops action log reader failed", exc_info=True)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def get_status() -> dict:
    """Return a shallow copy of the current/last action status."""
    with _STATE_LOCK:
        return dict(_STATE)


def latest_backup_path() -> Path | None:
    with _STATE_LOCK:
        raw = _STATE.get("backup_path")
    return Path(raw) if raw else None


def start_action(action: str) -> tuple[bool, dict]:
    """Start *action* in the background if no other action is running.

    Returns ``(started, status_snapshot)``. ``started=False`` means another
    action is already running -- the caller (routes.py) should respond 409
    with the returned (in-progress) status.
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported ops action: {action!r}")

    if not _ACTION_LOCK.acquire(blocking=False):
        return False, get_status()

    # Imported locally (not at module load) so tests -- and any future
    # profile-switch mid-run -- see the current active-profile resolution,
    # matching the pattern api.routes._run_gateway_lifecycle_command uses.
    from api.profiles import get_active_hermes_home

    # get_active_hermes_home() and _backups_dir() (which mkdir()s) can both
    # raise (profile resolution failure, disk full, permissions). Both must
    # stay inside this try/except -- if either raised outside of it, the
    # lock acquired above would never be released, and every subsequent
    # action would 409 forever until the WebUI process restarts.
    try:
        hermes_home = Path(get_active_hermes_home())
        backup_dir = _backups_dir()
        cmd = _command_for_action(action, backup_dir=backup_dir)
    except Exception:
        _ACTION_LOCK.release()
        raise

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)

    with _STATE_LOCK:
        _STATE.update(
            {
                "action": action,
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "returncode": None,
                "log": "",
                "backup_path": None,
                "error": None,
            }
        )

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(hermes_home) if hermes_home.exists() else None,
        )
    except Exception as exc:
        logger.exception("Failed to spawn ops action %r", action)
        with _STATE_LOCK:
            _STATE["status"] = "failed"
            _STATE["finished_at"] = time.time()
            _STATE["error"] = f"{type(exc).__name__}: {exc}"
        _ACTION_LOCK.release()
        return True, get_status()

    expected_backup_path = cmd[-1] if action == "backup" else None

    def _run() -> None:
        timed_out = False
        try:
            reader = threading.Thread(
                target=_drain_stdout, args=(proc.stdout,), daemon=True
            )
            reader.start()
            try:
                returncode = proc.wait(timeout=_ACTION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                logger.error(
                    "ops action %r timed out after %ss; killing process",
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
                        name=f"hermes-webui-ops-{action}-reap",
                        daemon=True,
                    ).start()
            reader.join(timeout=5)

            with _STATE_LOCK:
                _STATE["returncode"] = returncode
                _STATE["finished_at"] = time.time()
                if timed_out:
                    _STATE["status"] = "timeout"
                    _STATE["error"] = (
                        f"Action timed out after {_ACTION_TIMEOUT_SECONDS}s and was killed"
                    )
                elif returncode == 0:
                    _STATE["status"] = "completed"
                    if (
                        action == "backup"
                        and expected_backup_path
                        and Path(expected_backup_path).exists()
                    ):
                        _STATE["backup_path"] = expected_backup_path
                else:
                    _STATE["status"] = "failed"
                    _STATE["error"] = f"Exited with code {returncode}"
        except Exception as exc:
            logger.exception("ops action %r runner failed", action)
            with _STATE_LOCK:
                _STATE["status"] = "failed"
                _STATE["finished_at"] = time.time()
                _STATE["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _ACTION_LOCK.release()

    threading.Thread(
        target=_run, name=f"hermes-webui-ops-{action}", daemon=True
    ).start()
    return True, get_status()
