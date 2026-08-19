"""Fail-closed guard for in-process Hermes Agent source revisions.

Hermes WebUI currently imports ``run_agent.AIAgent`` into its long-lived server
process. If the Agent checkout changes while that process is alive, Python may
combine already-cached modules with newly-read source. Refuse to reuse that
mixed runtime and require a clean WebUI restart instead.
"""

from __future__ import annotations

import errno
import logging
import math
import os
from pathlib import Path
import sys
import subprocess
import threading
import time

# Retain the discovered path as a diagnostic/test-visible compatibility value;
# runtime identity is deliberately captured from the loaded module below.
from api.config import (
    PYTHON_EXE,
    SERVER_START_TIME,
    _AGENT_DIR,  # noqa: F401
    _DEFAULT_STATE_HOME,
)
from api.subprocess_utils import windows_hide_flags

logger = logging.getLogger(__name__)

_RESTART_PENDING_MESSAGE = (
    "Hermes Agent was updated while Hermes WebUI was running. "
    "Hermes WebUI will restart after the Agent update finishes. "
    "Retry this action after WebUI reconnects."
)
_RESTART_FAILED_MESSAGE = (
    "Hermes Agent was updated while Hermes WebUI was running. "
    "The automatic WebUI restart could not be scheduled. "
    "Restart Hermes WebUI before retrying this action."
)
_AGENT_UPDATE_MARKER = ".hermes-update-in-progress"
_AGENT_RECOVERY_MARKERS = (".update-incomplete", ".lazy-refresh-incomplete")
_AGENT_UPDATE_MAX_AGE_SECONDS = 20 * 60
_HERMES_HOME = Path(_DEFAULT_STATE_HOME)
_AGENT_PYTHON = Path(PYTHON_EXE).expanduser() if PYTHON_EXE else None
_SERVER_STARTED_AT = SERVER_START_TIME


def _read_agent_revision(
    agent_dir: Path | None,
    *,
    module_path: Path | None = None,
) -> str | None:
    """Return the loaded Agent checkout HEAD, or ``None`` if it is not tracked."""
    if agent_dir is None:
        return None

    if module_path is None:
        module = sys.modules.get("run_agent")
        module_file = getattr(module, "__file__", None)
        if not module_file:
            return None
        try:
            module_path = Path(module_file).resolve()
        except (OSError, RuntimeError, TypeError):
            return None

    try:
        worktree_result = subprocess.run(
            ["git", "-C", str(agent_dir), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=windows_hide_flags(),
        )
        if worktree_result.returncode != 0:
            return None
        worktree = Path(worktree_result.stdout.strip()).resolve()
        relative_module = module_path.relative_to(worktree).as_posix()
        tracked_result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(worktree),
                "ls-files",
                "--error-unmatch",
                "--",
                relative_module,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=windows_hide_flags(),
        )
        if tracked_result.returncode != 0:
            return None
        revision_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        return None

    revision = revision_result.stdout.strip()
    return revision if revision_result.returncode == 0 and revision else None


_AGENT_SOURCE_DIR: Path | None = None
_AGENT_MODULE_PATH: Path | None = None
_AGENT_REVISION: str | None = None
_AIAgent = None
_RUNTIME_LOCK = threading.Lock()
_AUTO_RESTART_LOCK = threading.Lock()
_AUTO_RESTART_SCHEDULED = False


class AgentRuntimeChangedError(RuntimeError):
    """Raised when the loaded Agent runtime no longer matches its source tree."""

    def __init__(
        self,
        message: str,
        *,
        restart_scheduled: bool | None = None,
        server_started_at: float | None = None,
    ) -> None:
        super().__init__(message)
        self.restart_scheduled = restart_scheduled
        self.server_started_at = server_started_at


def agent_runtime_stale_payload(exc: AgentRuntimeChangedError) -> dict:
    """Return the shared retry response for every stale-runtime entry point."""
    payload = {
        "error": str(exc),
        "type": "agent_runtime_stale",
        "retryable": True,
    }
    if exc.restart_scheduled is not None:
        payload["restart_scheduled"] = exc.restart_scheduled
    if exc.server_started_at is not None:
        payload["server_started_at"] = exc.server_started_at
    return payload


def _pid_is_alive(pid: int) -> bool | None:
    """Return PID liveness, or ``None`` when the platform cannot confirm it."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            )
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error == 5:  # ERROR_ACCESS_DENIED still proves the PID exists.
                    return True
                if error == 87:  # ERROR_INVALID_PARAMETER for a missing PID.
                    return False
                return None
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return None
    return True


def _read_live_agent_update(marker: Path) -> str:
    """Classify the shared Agent update marker without changing Agent state."""
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            marker.lstat()
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unknown"
        return "unknown"
    except OSError:
        return "unknown"

    lines = raw.splitlines()
    try:
        pid = int(lines[0].strip())
        started_at = float(lines[1].strip())
    except (IndexError, TypeError, ValueError):
        return "unknown"
    if pid <= 0 or not math.isfinite(started_at):
        return "unknown"

    age_seconds = time.time() - started_at
    if age_seconds < 0:
        return "unknown"
    if age_seconds > _AGENT_UPDATE_MAX_AGE_SECONDS:
        return "stale"
    alive = _pid_is_alive(pid)
    if alive is None:
        return "unknown"
    return "active" if alive else "stale"


def _marker_presence(marker: Path) -> str:
    """Return ``present``, ``absent``, or ``unknown`` for a recovery marker."""
    try:
        marker.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def _agent_install_roots() -> tuple[Path, ...]:
    """Return portable roots that can own the Agent's venv recovery markers."""
    candidates: list[Path] = []
    if _AGENT_SOURCE_DIR is not None:
        candidates.append(_AGENT_SOURCE_DIR)
    if _AGENT_PYTHON is not None:
        try:
            python_path = _AGENT_PYTHON.resolve()
        except (OSError, RuntimeError):
            python_path = _AGENT_PYTHON
        if python_path.parent.name.lower() in {"bin", "scripts"}:
            venv_dir = python_path.parent.parent
            if venv_dir.name.lower() in {"venv", ".venv"}:
                candidates.append(venv_dir.parent)

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key not in seen:
            seen.add(key)
            roots.append(candidate)
    return tuple(roots)


def _agent_update_transaction_state() -> str:
    """Return ``active``, ``incomplete``, ``complete``, or ``unknown``."""
    live_state = _read_live_agent_update(_HERMES_HOME / _AGENT_UPDATE_MARKER)
    if live_state == "unknown":
        return "unknown"

    recovery_present = False
    for root in _agent_install_roots():
        for marker_name in _AGENT_RECOVERY_MARKERS:
            presence = _marker_presence(root / marker_name)
            if presence == "unknown":
                return "unknown"
            recovery_present = recovery_present or presence == "present"
    if recovery_present:
        return "incomplete"
    if live_state == "active":
        return "active"
    return "complete"


def _agent_restart_readiness() -> str:
    """Confirm transaction completion and re-read the final Agent revision."""
    transaction_state = _agent_update_transaction_state()
    if transaction_state != "complete":
        return transaction_state
    final_revision = _read_agent_revision(
        _AGENT_SOURCE_DIR,
        module_path=_AGENT_MODULE_PATH,
    )
    return "ready" if final_revision is not None else "unknown"


def _wait_for_agent_restart_ready(poll_seconds: float = 2.0) -> bool:
    """Wait without a watchdog until Agent-owned transaction state is healthy."""
    last_state = None
    while True:
        state = _agent_restart_readiness()
        if state == "ready":
            return True
        if state != last_state:
            logger.info("Automatic WebUI restart waiting on Agent update state: %s", state)
            last_state = state
        time.sleep(max(0.1, poll_seconds))


def _delegate_webui_restart(*, restart_ready) -> bool:
    """Call the existing self-restart authority without copying launch logic."""
    from api.updates import _schedule_restart  # noqa: PLC0415

    return _schedule_restart(restart_ready=restart_ready)


def _schedule_automatic_restart() -> bool:
    """Schedule one transaction-aware WebUI restart for this process."""
    global _AUTO_RESTART_SCHEDULED

    with _AUTO_RESTART_LOCK:
        if _AUTO_RESTART_SCHEDULED:
            return True
        _AUTO_RESTART_SCHEDULED = True
        try:
            scheduled = bool(
                _delegate_webui_restart(
                    restart_ready=_wait_for_agent_restart_ready,
                )
            )
        except Exception:
            logger.exception("Could not schedule automatic WebUI restart")
            scheduled = False
        if not scheduled:
            _AUTO_RESTART_SCHEDULED = False
        return scheduled


def _loaded_agent_source_identity() -> tuple[Path, Path] | None:
    """Return the source directory and file that supplied ``run_agent``."""
    module = sys.modules.get("run_agent")
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    try:
        module_path = Path(module_file).resolve()
        return module_path.parent, module_path
    except (OSError, RuntimeError, TypeError):
        return None


def _capture_loaded_agent_revision() -> None:
    """Bind the guard to the checkout that supplied the loaded Agent module."""
    global _AGENT_SOURCE_DIR, _AGENT_MODULE_PATH, _AGENT_REVISION

    if _AGENT_REVISION is not None:
        ensure_agent_runtime_current()
        return

    identity = _loaded_agent_source_identity()
    if identity is None:
        return
    source_dir, module_path = identity
    current_revision = _read_agent_revision(source_dir, module_path=module_path)
    _AGENT_SOURCE_DIR = source_dir
    _AGENT_MODULE_PATH = module_path
    _AGENT_REVISION = current_revision


def ensure_agent_runtime_current() -> None:
    """Reject a known Git checkout change instead of mixing Python modules."""
    if _AGENT_REVISION is None:
        return
    if (
        _read_agent_revision(_AGENT_SOURCE_DIR, module_path=_AGENT_MODULE_PATH)
        != _AGENT_REVISION
    ):
        restart_scheduled = _schedule_automatic_restart()
        raise AgentRuntimeChangedError(
            _RESTART_PENDING_MESSAGE if restart_scheduled else _RESTART_FAILED_MESSAGE,
            restart_scheduled=restart_scheduled,
            server_started_at=_SERVER_STARTED_AT,
        )


def require_ai_agent_class():
    """Import ``AIAgent`` after proving the loaded source revision is current."""
    ensure_agent_runtime_current()
    from run_agent import AIAgent  # noqa: PLC0415

    _capture_loaded_agent_revision()
    return AIAgent


def get_ai_agent_class():
    """Return ``AIAgent`` while preserving the existing lazy-import retry."""
    global _AIAgent, _AGENT_REVISION

    with _RUNTIME_LOCK:
        ensure_agent_runtime_current()
        if _AIAgent is None:
            try:
                agent_class = require_ai_agent_class()
            except ImportError:
                return None
            _AIAgent = agent_class
        return _AIAgent
