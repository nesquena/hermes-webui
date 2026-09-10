"""Fail-closed guard for in-process Hermes Agent source revisions.

Hermes WebUI currently imports ``run_agent.AIAgent`` into its long-lived server
process. If the Agent checkout changes while that process is alive, Python may
combine already-cached modules with newly-read source. Refuse to reuse that
mixed runtime and require a clean WebUI restart instead.
"""

from __future__ import annotations

import errno
import math
import os
from pathlib import Path
import stat
import sys
import subprocess
import threading
import time

# Retain the discovered path as a diagnostic/test-visible compatibility value;
# runtime identity is deliberately captured from the loaded module below.
from api.config import (
    PYTHON_EXE,
    _AGENT_DIR,  # noqa: F401
    _DEFAULT_STATE_HOME,
)
from api.subprocess_utils import windows_hide_flags

_RESTART_REQUIRED_MESSAGE = (
    "Hermes Agent was updated while Hermes WebUI was running. "
    "WebUI cannot verify that the Agent update completed safely. "
    "Check the Agent update outcome and environment first. "
    "Restart Hermes WebUI manually before retrying this action."
)
_AGENT_UPDATE_MARKER = ".hermes-update-in-progress"
_AGENT_RECOVERY_MARKERS = (".update-incomplete", ".lazy-refresh-incomplete")
_AGENT_UPDATE_MAX_AGE_SECONDS = 20 * 60
# The update marker holds a PID and a start timestamp (two short numeric lines).
# Anything larger is not a legitimate marker; cap the read so a huge or growing
# regular file can never exhaust memory on the stale-runtime request path.
_AGENT_UPDATE_MARKER_MAX_BYTES = 64 * 1024
# O_NOFOLLOW is POSIX; on platforms that lack it (e.g. some Windows builds) fall
# back to 0 so the open still succeeds — the fstat regular-file check below is
# the portable backstop.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_HERMES_HOME = Path(_DEFAULT_STATE_HOME)
_AGENT_PYTHON = Path(PYTHON_EXE).expanduser() if PYTHON_EXE else None


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


class AgentRuntimeChangedError(RuntimeError):
    """Raised when the loaded Agent runtime no longer matches its source tree."""

    def __init__(
        self,
        message: str,
        *,
        agent_update_state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.agent_update_state = agent_update_state


def agent_runtime_stale_payload(exc: AgentRuntimeChangedError) -> dict:
    """Return the shared retry response for every stale-runtime entry point."""
    payload = {
        "error": str(exc),
        "type": "agent_runtime_stale",
        "retryable": True,
        "restart_scheduled": False,
    }
    if exc.agent_update_state is not None:
        payload["agent_update_state"] = exc.agent_update_state
    return payload


def _pid_is_alive(pid: int) -> bool | None:
    """Return PID liveness, or ``None`` when the platform cannot confirm it."""
    if pid <= 0:
        return False
    if pid.bit_length() > 32:
        return None
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
    except (OverflowError, ValueError):
        return None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return None
    return True


def _read_live_agent_update(marker: Path) -> str:
    """Classify the shared Agent update marker without changing Agent state.

    The marker is attacker-adjacent shared state (any process that can write the
    Agent home can create it), so the read is hardened: never follow a symlink,
    never block on a FIFO/device, and never read an unbounded regular file.
    Anything that is not a small regular file is classified ``unknown`` rather
    than allowed to hang or exhaust memory on a stale-runtime request path.
    """
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NONBLOCK | _O_NOFOLLOW)
    except FileNotFoundError:
        try:
            marker.lstat()
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unknown"
        # Path exists to lstat (e.g. a dangling/looping symlink) but O_NOFOLLOW
        # refused to open it — treat as an unverifiable marker.
        return "unknown"
    except (OSError, ValueError, TypeError):
        # ELOOP (symlink under O_NOFOLLOW), ENXIO/EWOULDBLOCK (FIFO with no
        # writer under O_NONBLOCK), a non-path marker object, or any other open
        # failure — fail closed: an unreadable marker is never proof of safety.
        return "unknown"

    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return "unknown"
        if not stat.S_ISREG(st.st_mode):
            # FIFO, device, directory, socket — never a legitimate marker.
            return "unknown"
        if st.st_size > _AGENT_UPDATE_MARKER_MAX_BYTES:
            return "unknown"
        try:
            # Read one byte past the cap so an oversized file that lied about
            # st_size (or grew mid-read) is still rejected rather than truncated.
            data = os.read(fd, _AGENT_UPDATE_MARKER_MAX_BYTES + 1)
        except (OSError, BlockingIOError):
            return "unknown"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    if len(data) > _AGENT_UPDATE_MARKER_MAX_BYTES:
        return "unknown"
    try:
        raw = data.decode("utf-8")
    except UnicodeError:
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
        # A venv Python is commonly a symlink to a shared interpreter. Keep the
        # configured venv path so its installation's recovery markers are read.
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
    """Report marker diagnostics, never proof of successful completion.

    The Agent removes its active marker on failed/interrupted exits too. Neither
    its absence nor a stale PID proves the checkout or environment is healthy.
    """
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
    return "unverified" if live_state == "absent" else live_state


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
        # Automatic restart needs an Agent-owned success receipt bound to this
        # transaction, final revision and healthy environment, plus an atomic
        # handoff excluding mutations across replacement. Marker polling and a
        # final revision read supply neither contract. Keep this path manual.
        raise AgentRuntimeChangedError(
            _RESTART_REQUIRED_MESSAGE,
            agent_update_state=_agent_update_transaction_state(),
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
