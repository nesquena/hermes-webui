"""Auto-restart guard for in-process Hermes Agent source revisions.

Hermes WebUI currently imports ``run_agent.AIAgent`` into its long-lived server
process. If the Agent checkout changes while that process is alive, Python may
combine already-cached modules with newly-read source.

Instead of blocking user actions (old behaviour), the guard now schedules
an automatic process restart so the user never sees a blocking error.
A cron watchdog (webui-agent-watchdog) catches any remaining edge cases.
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
import sys
import subprocess
import threading

logger = logging.getLogger(__name__)

# Retain the discovered path as a diagnostic/test-visible compatibility value;
# runtime identity is deliberately captured from the loaded module below.
from api.config import _AGENT_DIR  # noqa: F401

_RESTART_MESSAGE = (
    "Hermes Agent was updated. "
    "The WebUI is restarting automatically to pick up the changes. "
    "Please wait a few seconds and reload the page."
)

# Guard flag: only schedule one restart per process lifetime
_SCHEDULED_RESTART = False
_SCHEDULE_LOCK = threading.Lock()


def _schedule_self_restart() -> None:
    """Spawn a background thread that kills this process after 3 seconds.

    The delay lets the current HTTP response (with the friendly message) be
    sent to the user before the process dies. systemd's ``Restart=on-failure``
    or the SIGKILL exit code triggers an automatic restart.
    """
    global _SCHEDULED_RESTART

    with _SCHEDULE_LOCK:
        if _SCHEDULED_RESTART:
            return
        _SCHEDULED_RESTART = True

    def _do_restart() -> None:
        import time as _time

        _time.sleep(3)
        pid = os.getpid()
        logger.info(
            "Agent revision changed — restarting WebUI (PID %s) in 3s",
            pid,
        )
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    t = threading.Thread(target=_do_restart, daemon=True)
    t.start()


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
        )
        if tracked_result.returncode != 0:
            return None
        revision_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
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
    """Detect a Git checkout change and auto-restart instead of blocking."""
    if _AGENT_REVISION is None:
        return

    old_rev = _AGENT_REVISION
    current_rev = _read_agent_revision(
        _AGENT_SOURCE_DIR, module_path=_AGENT_MODULE_PATH
    )
    if current_rev != old_rev and current_rev is not None:
        logger.warning(
            "Agent revision changed: %s → %s. Scheduling WebUI restart.",
            old_rev[:12],
            current_rev[:12],
        )
        _schedule_self_restart()
        raise AgentRuntimeChangedError(_RESTART_MESSAGE)


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
