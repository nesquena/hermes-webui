"""Background runner for the Settings -> System "Maintenance actions" card.

Spawns the active-profile Hermes CLI (``hermes doctor`` / ``hermes security
audit`` / ``hermes backup``) as a subprocess, buffers its combined
stdout+stderr into a bounded log tail, and exposes a poll-friendly status
snapshot for the frontend.

Ownership model (gate follow-up):

* **All run state is owned by the profile that started the run.** Status,
  log tail, errors, and backup artifacts are only ever returned for the
  requesting profile; a different profile polling while a run is active gets
  a minimal ``busy`` envelope with no log/path/error contents.
* **One action at a time, server-wide,** enforced by a non-blocking lock
  that is held until the spawned process *tree* is actually reaped — a
  timeout kill signals the whole process group and keeps the slot closed
  until ``wait()`` returns, so a second action can never overlap an
  un-reaped first one.
* **Every run has an immutable run id.** The log reader and the finalizer
  carry their run id and are rejected once it is no longer the profile's
  current run, so a late reader can never write into a newer run's state.
* **Backup artifacts are hardened.** Per-profile directory created 0700,
  archives chmod'ed 0600, collision-proof names, partial files deleted on
  failure/timeout, retention-capped, and rediscovered after a restart. The
  "latest successful backup" is tracked separately from the current run so
  a later doctor/audit run does not orphan a credential-bearing archive.

Gating (fail-closed) is enforced by the caller (api.routes, via the existing
``_truthy_env("HERMES_WEBUI_ALLOW_OPS_ACTIONS")`` pattern) -- this module does
not gate on its own so it stays testable without env-var plumbing.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

ACTIONS = ("doctor", "security_audit", "backup")

_ACTION_TIMEOUT_SECONDS = 600
_LOG_TAIL_MAX_CHARS = 200_000  # ~200 KB buffered log tail
_BACKUP_RETENTION_COUNT = 5  # newest N archives kept per profile
_TERM_GRACE_SECONDS = 5  # SIGTERM -> SIGKILL escalation window

# Server-wide single-flight. Held from a successful start until the spawned
# process tree is REAPED (not merely signalled) -- see _run().
_ACTION_LOCK = threading.Lock()

# Guards _PROFILES. Short critical sections only.
_REGISTRY_LOCK = threading.Lock()

_IDLE_STATE: dict = {
    "run_id": None,
    "action": None,  # last-started / currently-running action name
    "status": "idle",  # idle | running | completed | failed | timeout
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log": "",
    "backup_path": None,  # current RUN's backup (see last_successful_backup)
    "error": None,
}


class _ProfileRuns:
    """Per-profile run state. Never shared across profiles."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.state_lock = threading.Lock()
        self.state = dict(_IDLE_STATE)
        self.run_seq = 0
        # Independent of the current run: replaced only after a VALIDATED
        # successful backup, survives later doctor/audit runs.
        self.last_successful_backup: str | None = None


_PROFILES: dict[str, _ProfileRuns] = {}


def _profile_runs(profile: str) -> _ProfileRuns:
    key = str(profile or "default")
    with _REGISTRY_LOCK:
        runs = _PROFILES.get(key)
        if runs is None:
            runs = _ProfileRuns(key)
            _PROFILES[key] = runs
        return runs


def _profile_slug(profile: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(profile or "default")).strip("._")
    return slug or "default"


def _resolve_hermes_command() -> str:
    """Resolve the CLI path used to spawn ops actions (mirrors gateway_restart)."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return hermes_cmd
    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return "hermes"


def _backups_dir(profile: str) -> Path:
    """Per-profile archive directory under the WebUI state root, 0700.

    Never inside the repo; hardened because ``hermes backup --quick``
    archives include config, state.db, ``.env``, auth material, and cron.
    """
    from api.config import STATE_DIR

    d = STATE_DIR / "ops_backups" / _profile_slug(profile)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
        os.chmod(d.parent, 0o700)
    except OSError:
        logger.debug("could not tighten ops_backups permissions", exc_info=True)
    return d


def _command_for_action(action: str, *, backup_dir: Path) -> list[str]:
    hermes_cmd = _resolve_hermes_command()
    if action == "doctor":
        return [hermes_cmd, "doctor"]
    if action == "security_audit":
        return [hermes_cmd, "security", "audit", "--json"]
    if action == "backup":
        # Collision-proof: timestamp + random suffix; two runs in the same
        # second (or a restart replaying a timestamp) can never overwrite.
        output_path = (
            backup_dir
            / f"hermes-backup-{int(time.time())}-{secrets.token_hex(4)}.zip"
        )
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


def _validated_backup(path: Path, profile: str) -> Path | None:
    """Containment + integrity checks for a credential-bearing archive.

    Regular file (no symlink), resolves inside the profile's own backup
    root, and is a readable ZIP. Returns the resolved path or None.
    """
    try:
        if not path.is_file() or path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        root = _backups_dir(profile).resolve(strict=True)
        if not resolved.is_relative_to(root):
            return None
        if not zipfile.is_zipfile(resolved):
            return None
        return resolved
    except OSError:
        return None


def _apply_backup_retention(profile: str) -> None:
    """Keep only the newest N archives for this profile; best-effort."""
    try:
        root = _backups_dir(profile)
        archives = sorted(
            (p for p in root.glob("hermes-backup-*.zip") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in archives[_BACKUP_RETENTION_COUNT:]:
            stale.unlink(missing_ok=True)
    except OSError:
        logger.debug("backup retention sweep failed", exc_info=True)


def _append_log(runs: _ProfileRuns, run_id: str, text: str) -> None:
    """Append to the log tail -- but only while *run_id* is still current.

    A late reader thread from a previous run (its ``join(timeout=...)``
    elapsed) must never write into a newer run's state.
    """
    if not text:
        return
    with runs.state_lock:
        if runs.state.get("run_id") != run_id:
            return
        runs.state["log"] += text
        if len(runs.state["log"]) > _LOG_TAIL_MAX_CHARS:
            runs.state["log"] = runs.state["log"][-_LOG_TAIL_MAX_CHARS:]


def _drain_stdout(runs: _ProfileRuns, run_id: str, stream) -> None:
    """Read the subprocess's combined stdout/stderr into the log tail."""
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            _append_log(runs, run_id, line)
    except Exception:
        logger.debug("ops action log reader failed", exc_info=True)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _busy_snapshot() -> dict:
    """What a profile may learn about ANOTHER profile's active run: only
    that the single server-wide slot is taken. No action, log, error, or
    artifact path -- backup logs/paths are credential-adjacent."""
    return {"status": "busy", "busy": True}


def get_status(profile: str) -> dict:
    """Return the requesting profile's own current/last action status."""
    runs = _profile_runs(profile)
    with runs.state_lock:
        snapshot = dict(runs.state)
        snapshot["last_successful_backup"] = runs.last_successful_backup
    snapshot["backup_available"] = bool(
        runs.last_successful_backup
        and _validated_backup(Path(runs.last_successful_backup), profile)
    )
    return snapshot


def latest_backup_path(profile: str) -> Path | None:
    """The profile's latest VALIDATED archive, rediscovered after restart.

    Prefers the recorded ``last_successful_backup``; falls back to the
    newest valid archive in the profile's own directory (a WebUI restart
    loses the in-memory record, not the artifact)."""
    runs = _profile_runs(profile)
    with runs.state_lock:
        recorded = runs.last_successful_backup
    if recorded:
        valid = _validated_backup(Path(recorded), profile)
        if valid is not None:
            return valid
    try:
        root = _backups_dir(profile)
        candidates = sorted(
            (p for p in root.glob("hermes-backup-*.zip")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for candidate in candidates:
        valid = _validated_backup(candidate, profile)
        if valid is not None:
            with runs.state_lock:
                runs.last_successful_backup = str(valid)
            return valid
    return None


def start_action(action: str, profile: str) -> tuple[bool, dict]:
    """Start *action* for *profile* if the server-wide slot is free.

    Returns ``(started, status_snapshot)``. ``started=False`` means another
    action is already running -- the caller (routes.py) responds 409. The
    snapshot is the caller's OWN state when the caller owns the running
    action, and a minimal busy envelope when another profile does.
    """
    if action not in ACTIONS:
        raise ValueError(f"unsupported ops action: {action!r}")

    runs = _profile_runs(profile)

    if not _ACTION_LOCK.acquire(blocking=False):
        with runs.state_lock:
            own_running = runs.state.get("status") == "running"
        return False, (get_status(profile) if own_running else _busy_snapshot())

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
        backup_dir = _backups_dir(profile)
        cmd = _command_for_action(action, backup_dir=backup_dir)
    except Exception:
        _ACTION_LOCK.release()
        raise

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)

    with runs.state_lock:
        runs.run_seq += 1
        run_id = f"{_profile_slug(profile)}-{runs.run_seq}-{secrets.token_hex(4)}"
        runs.state = dict(_IDLE_STATE)
        runs.state.update(
            {
                "run_id": run_id,
                "action": action,
                "status": "running",
                "started_at": time.time(),
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
            # Own the whole process tree: a timeout kill signals the group,
            # so CLI-spawned children cannot outlive the run unnoticed.
            start_new_session=True,
        )
    except Exception as exc:
        logger.exception("Failed to spawn ops action %r", action)
        with runs.state_lock:
            if runs.state.get("run_id") == run_id:
                runs.state["status"] = "failed"
                runs.state["finished_at"] = time.time()
                runs.state["error"] = f"{type(exc).__name__}: {exc}"
        _ACTION_LOCK.release()
        return True, get_status(profile)

    expected_backup_path = cmd[-1] if action == "backup" else None

    def _signal_group(sig: int) -> None:
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

    def _run() -> None:
        timed_out = False
        try:
            reader = threading.Thread(
                target=_drain_stdout,
                args=(runs, run_id, proc.stdout),
                daemon=True,
            )
            reader.start()
            try:
                returncode = proc.wait(timeout=_ACTION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                logger.error(
                    "ops action %r timed out after %ss; terminating process group",
                    action,
                    _ACTION_TIMEOUT_SECONDS,
                )
                _signal_group(signal.SIGTERM)
                try:
                    returncode = proc.wait(timeout=_TERM_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _signal_group(signal.SIGKILL)
                    # Single-flight invariant: do NOT release the slot (the
                    # finally below) until the group leader is actually
                    # reaped. A process stuck in uninterruptible I/O keeps
                    # the slot closed -- honest 409s instead of two live
                    # process trees writing over each other.
                    returncode = proc.wait()
            reader.join(timeout=5)

            with runs.state_lock:
                if runs.state.get("run_id") == run_id:
                    runs.state["returncode"] = returncode
                    runs.state["finished_at"] = time.time()
                    if timed_out:
                        runs.state["status"] = "timeout"
                        runs.state["error"] = (
                            f"Action timed out after {_ACTION_TIMEOUT_SECONDS}s "
                            "and its process group was killed"
                        )
                    elif returncode == 0:
                        runs.state["status"] = "completed"
                    else:
                        runs.state["status"] = "failed"
                        runs.state["error"] = f"Exited with code {returncode}"

            if action == "backup" and expected_backup_path:
                self_path = Path(expected_backup_path)
                if not timed_out and returncode == 0:
                    try:
                        os.chmod(self_path, 0o600)
                    except OSError:
                        logger.debug(
                            "could not chmod backup archive", exc_info=True
                        )
                    valid = _validated_backup(self_path, profile)
                    if valid is not None:
                        with runs.state_lock:
                            if runs.state.get("run_id") == run_id:
                                runs.state["backup_path"] = str(valid)
                            runs.last_successful_backup = str(valid)
                        _apply_backup_retention(profile)
                    else:
                        with runs.state_lock:
                            if runs.state.get("run_id") == run_id:
                                runs.state["status"] = "failed"
                                runs.state["error"] = (
                                    "Backup completed but the archive failed "
                                    "validation"
                                )
                        self_path.unlink(missing_ok=True)
                else:
                    # Failure/timeout: never leave a partial credential
                    # archive on disk.
                    self_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.exception("ops action %r runner failed", action)
            with runs.state_lock:
                if runs.state.get("run_id") == run_id:
                    runs.state["status"] = "failed"
                    runs.state["finished_at"] = time.time()
                    runs.state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _ACTION_LOCK.release()

    threading.Thread(
        target=_run, name=f"hermes-webui-ops-{action}", daemon=True
    ).start()
    return True, get_status(profile)
