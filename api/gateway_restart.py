"""Helpers for restarting the active-profile Hermes gateway."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from api.profiles import (
    _PROFILE_ID_RE,
    _is_root_profile,
    get_active_hermes_home,
    get_active_profile_name,
    get_hermes_home_for_profile,
)

logger = logging.getLogger(__name__)

_GATEWAY_RESTART_LOCK = threading.Lock()

# Name of the gateway's persisted runtime status file, written by the gateway
# process itself (gateway/status.py) into the HERMES_HOME it was launched with.
# The record carries the gateway's own PID under the "pid" key and a
# timezone-aware ISO-8601 "updated_at" timestamp.
_GATEWAY_RUNTIME_STATUS_FILE = "gateway_state.json"

# Canonical gateway PID file, written by the gateway alongside the runtime
# status record as a JSON object (``{"pid": 4242, ...}``). Used to cross-check
# that the PID recorded in ``gateway_state.json`` belongs to the current
# gateway generation rather than a stale record whose PID was later reused by
# an unrelated process.
_GATEWAY_PID_FILE = "gateway.pid"


def _resolve_hermes_command() -> str:
    """Resolve the CLI path used for active-profile gateway restarts."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return hermes_cmd

    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return str(sibling)
    return "hermes"


def _consume_stream(stream) -> None:
    """Drain a subprocess stream to prevent stdout/stderr pipe deadlocks."""
    try:
        while stream and stream.read(4096):
            pass
    except Exception:
        pass


def _release_lock() -> None:
    try:
        _GATEWAY_RESTART_LOCK.release()
    except RuntimeError:
        # The lock may already have been released by another path.
        pass


def _record_updated_at_epoch(runtime_status: dict) -> float | None:
    """Return the record's ``updated_at`` as epoch seconds, or ``None`` when
    missing or unparseable.

    The gateway writes a timezone-aware ISO-8601 timestamp (the same format
    ``gateway/status.py`` uses and ``api/agent_health.py`` already parses).
    Naive or unparseable timestamps are refused — they cannot prove the record
    belongs to the current gateway generation, so the caller must fail closed.
    """
    raw_updated_at = runtime_status.get("updated_at")
    if not isinstance(raw_updated_at, str) or not raw_updated_at:
        return None
    try:
        updated_at = datetime.fromisoformat(raw_updated_at)
    except (TypeError, ValueError):
        return None
    if updated_at.tzinfo is None:
        return None
    return updated_at.timestamp()


def _read_canonical_gateway_pid(pid_file: Path) -> int | None:
    """Return the canonical gateway PID from a ``gateway.pid`` file, or None.

    Hermes writes ``gateway.pid`` as a JSON object (``{"pid": 4242, ...}``),
    so the file must be JSON-decoded and its ``pid`` member extracted.  A
    legacy top-level bare integer (``"4242"``) is still accepted.  Anything
    else — missing/invalid ``pid`` member, string/float/bool value, unreadable
    content, non-UTF-8 bytes, or unparseable JSON (including the Python 3.11+
    integer-string conversion limit on oversized digit strings) — returns
    None so callers fail closed.  The read/parse is deliberately
    non-throwing: an escaping exception would propagate out of the
    background restart thread and skip the timed-out child's terminate/kill
    cleanup, leaking a genuinely hung restart child.  ``int()`` is never used
    to coerce, because it would accept ``"4242"`` and truncate ``4242.9``,
    misclassifying a stuck child as the healthy gateway.
    """
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # Missing/unreadable/non-UTF-8 file: cannot confirm the canonical PID.
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        # Non-JSON content, or a JSON value json.loads refused (e.g. an
        # oversized integer beyond the interpreter's digit-conversion limit).
        # ``json.JSONDecodeError`` and ``UnicodeDecodeError`` are ValueError
        # subclasses, so this also covers malformed JSON.  Fall back to a
        # legacy plain-integer file (e.g. "4242").
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if type(value) is int else None
    if isinstance(payload, dict):
        pid = payload.get("pid")
    else:
        pid = payload
    if type(pid) is not int:
        return None
    return pid


def _subprocess_became_gateway(
    proc: subprocess.Popen,
    hermes_home: Path,
    *,
    proc_started_at: float | None = None,
) -> bool:
    """Return True when the restart subprocess has itself become the gateway.

    Single-container deployments have no service manager, so ``hermes gateway
    restart`` falls through to ``run_gateway()`` and the CLI process becomes
    the gateway itself: it binds the API-server port and never exits (#6730).
    The gateway writes ``gateway_state.json`` into the HERMES_HOME it was
    launched with (the same ``hermes_home`` we put on the subprocess env),
    recording its own PID under the ``pid`` key.  When that recorded PID
    equals the subprocess PID and the gateway self-reports as **running**, the
    timeout means "restart succeeded and this process IS the gateway", not
    "restart hung" — terminating it would SIGTERM the healthy replacement.

    The exemption is deliberately narrow.  The gateway writes ``starting``
    *before* potentially-blocking plugin/MCP/platform initialization and only
    flips to ``running`` once fully up, so a ``starting`` record may be a
    restart child wedged during startup and must still fall through to the
    240s cleanup + terminate.  Raw PID equality alone also cannot prove
    process generation: a stale record from a previous gateway generation
    whose PID was later reused would falsely match.  The PID is therefore
    validated against canonical metadata — both PID values must be real
    integers (no ``int()`` coercion, so string/float PIDs fail closed), the
    record must postdate the restart subprocess (``proc_started_at``
    start-time proof), and, when the canonical ``gateway.pid`` file is
    present, the recorded PID must agree with the JSON-decoded value.

    Any unverifiable input fails closed (returns False), preserving the
    previous terminate-on-timeout behaviour for genuinely hung restarts.
    """
    try:
        proc_pid = int(proc.pid)
    except (AttributeError, TypeError, ValueError):
        return False
    try:
        runtime_status = json.loads(
            (hermes_home / _GATEWAY_RUNTIME_STATUS_FILE).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(runtime_status, dict):
        return False
    # Only a CONFIRMED running gateway is exempt.  A ``starting`` record is
    # not proof the child finished initializing — it may be wedged mid-startup.
    if runtime_status.get("gateway_state") != "running":
        return False
    # Strict integer PID: ``int()`` would accept "4242" and truncate 4242.9,
    # failing open (exempting a stuck child) when the canonical pid file is
    # absent.  Only a genuine int can prove identity.
    recorded_pid = runtime_status.get("pid")
    if type(recorded_pid) is not int:
        return False
    if recorded_pid != proc_pid:
        return False
    # Generation proof via start-time metadata: a record written before the
    # restart subprocess was spawned belongs to a previous gateway generation
    # (stale record).  With PID reuse it would falsely match the raw equality.
    if proc_started_at is not None:
        record_time = _record_updated_at_epoch(runtime_status)
        if record_time is None or record_time < proc_started_at:
            return False
    # Cross-check the recorded PID against the canonical ``gateway.pid`` file
    # when present.  A mismatching, unreadable, or non-integer pid file means
    # the record is not the current gateway generation -> fail closed.
    pid_file = hermes_home / _GATEWAY_PID_FILE
    if pid_file.exists():
        canonical_pid = _read_canonical_gateway_pid(pid_file)
        if canonical_pid is None or canonical_pid != recorded_pid:
            return False
    return True


def _gateway_restart_profile_context(profile: str | None = None) -> tuple[Path, str | None]:
    """Return the HERMES_HOME and CLI profile arg for a gateway restart."""
    if profile is None:
        raw_profile = str(get_active_profile_name() or "default").strip()
        active_home = Path(get_active_hermes_home())
    else:
        raw_profile = str(profile or "")
        if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile):
            raise ValueError(f"Invalid profile for gateway restart: {profile!r}")
        active_home = Path(get_hermes_home_for_profile(raw_profile))

    if (
        raw_profile == "default"
        and active_home.name == "default"
        and active_home.parent.name == "profiles"
    ):
        return active_home, None
    if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile) or _is_root_profile(raw_profile):
        return active_home, "default"
    return active_home, raw_profile


def restart_active_profile_gateway(
    *,
    profile: str | None = None,
    quick_timeout_seconds: float = 2.0,
    background_wait_seconds: float = 240.0,
) -> dict:
    """Run a non-blocking ``hermes gateway restart`` for the active profile.

    Returns a short status dict with these values:
    - completed: command finished quickly and succeeded.
    - in_progress: command did not finish within ``quick_timeout_seconds``.
    - failed: command finished quickly with non-zero exit status.
    - busy: restart already in progress from another caller.
    """
    if not _GATEWAY_RESTART_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "message": "Restart already in progress. Please wait a moment and try again.",
        }

    try:
        active_home, cli_profile = _gateway_restart_profile_context(profile)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(active_home)
        hermes_cmd = _resolve_hermes_command()
        cmd = [hermes_cmd]
        if cli_profile is not None:
            cmd.extend(["--profile", cli_profile])
        cmd.extend(["gateway", "restart"])

        if cli_profile is None:
            logger.info(
                "Restarting gateway service via CLI command: %s gateway restart (HERMES_HOME=%s)",
                hermes_cmd,
                active_home,
            )
        else:
            logger.info(
                "Restarting gateway service via CLI command: %s --profile %s gateway restart (HERMES_HOME=%s)",
                hermes_cmd,
                cli_profile,
                active_home,
            )
        # Spawn moment of the restart child.  Used as the generation boundary
        # for the terminate-exemption: only a gateway_state.json record written
        # AFTER this instant can belong to this subprocess generation.
        # Captured BEFORE ``Popen``: a fast-starting child can write its
        # ``running`` record while the parent is still inside ``Popen``, and a
        # post-Popen capture would make that fresh record look older than the
        # spawn moment, terminating the healthy replacement.
        proc_started_at = time.time()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        try:
            stdout, stderr = proc.communicate(timeout=quick_timeout_seconds)
            _release_lock()
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()
            if proc.returncode == 0:
                logger.info("Gateway service restarted successfully: %s", stdout)
                return {
                    "status": "completed",
                    "message": "Gateway service restarted successfully",
                    "detail": stdout or stderr,
                }

            logger.error("Gateway service restart failed with code %s: %s", proc.returncode, stderr)
            return {
                "status": "failed",
                "message": f"Restart failed: {stderr or stdout}",
                "detail": stdout or stderr,
                "returncode": proc.returncode,
            }

        except subprocess.TimeoutExpired:
            logger.info(
                "Gateway restart is taking longer than %.1fs (likely draining in-flight runs);"
                " continuing in background",
                quick_timeout_seconds,
            )

            threading.Thread(target=_consume_stream, args=(proc.stdout,), daemon=True).start()
            threading.Thread(target=_consume_stream, args=(proc.stderr,), daemon=True).start()

            def _wait_and_release() -> None:
                try:
                    proc.wait(timeout=background_wait_seconds)
                except subprocess.TimeoutExpired:
                    try:
                        became_gateway = _subprocess_became_gateway(
                            proc, active_home, proc_started_at=proc_started_at
                        )
                    except Exception:
                        # Fail closed: a verifier exception must never skip
                        # termination.  If we cannot confirm the child IS the
                        # healthy gateway, treat it as "not confirmed" and fall
                        # through to the terminate/kill cleanup below.
                        logger.exception(
                            "Gateway identity verification raised; treating the "
                            "timed-out restart process as NOT the gateway."
                        )
                        became_gateway = False
                    if became_gateway:
                        # Single-container image: no service manager, so the
                        # restart CLI became the gateway itself. Killing it
                        # would SIGTERM the healthy replacement and drop every
                        # active session/SSE stream (#6730). Treat the timeout
                        # as success: release the lock and leave it running.
                        logger.warning(
                            "Gateway restart process timed out after %.1fs but the "
                            "subprocess has become the gateway itself (PID %s owns "
                            "gateway_state.json); leaving it running.",
                            background_wait_seconds,
                            proc.pid,
                        )
                        return
                    logger.error(
                        "Gateway restart process timed out after %.1fs. Terminating process.",
                        background_wait_seconds,
                    )
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            try:
                                proc.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                logger.error(
                                    "Gateway restart process refused to die even after SIGKILL.",
                                )
                    except Exception:
                        logger.exception("Failed to terminate timed out gateway restart process.")
                finally:
                    _release_lock()

            threading.Thread(target=_wait_and_release, daemon=True).start()
            return {
                "status": "in_progress",
                "message": "Gateway service restart initiated (in progress)",
            }
    except Exception as exc:
        _release_lock()
        logger.exception("Failed to run gateway restart command")
        return {
            "status": "failed",
            "message": f"Internal error running restart: {type(exc).__name__}: {exc}",
        }
