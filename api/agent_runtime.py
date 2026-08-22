"""Auto-restart guard for in-process Hermes Agent source revisions.

Hermes WebUI currently imports ``run_agent.AIAgent`` into its long-lived server
process. If the Agent checkout changes while that process is alive, Python may
combine already-cached modules with newly-read source.

The guard fails closed (typed 409 / ``AgentRuntimeChangedError``) whenever the
loaded runtime no longer matches its source tree, and for a concrete known
old→new revision transition it additionally schedules exactly one restart
through the repository's shared restart authority
(``api.updates._schedule_restart``) — the same cross-platform re-exec machinery
the self-update flow uses (POSIX ``os.execv``, native-Windows replacement,
frozen/source argv handling, active stream/run drain, bytecode purge, and
supervisor fallback). The restart revalidates the revision immediately before
re-exec so an A→B→A rollback cancels it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import subprocess
import threading

logger = logging.getLogger(__name__)

# Retain the discovered path as a diagnostic/test-visible compatibility value;
# runtime identity is deliberately captured from the loaded module below.
from api.config import _AGENT_DIR  # noqa: F401
from api.subprocess_utils import windows_hide_flags

_RESTART_MESSAGE = (
    "Hermes Agent was updated while Hermes WebUI was running. "
    "Restart Hermes WebUI before retrying this action — the WebUI is "
    "restarting automatically to pick up the changes; please wait a few "
    "seconds and reload the page."
)

# Guard flag: only schedule one restart per process lifetime, so concurrent
# barrier hits cannot stack restart timers.
_SCHEDULED_RESTART = False
_SCHEDULE_LOCK = threading.Lock()


def _schedule_self_restart(delay: float = 2.0) -> None:
    """Schedule exactly one restart through the shared restart authority.

    Single-flight: concurrent barrier hits must not stack restart timers.
    The actual process replacement is delegated to
    ``api.updates._schedule_restart``, preserving POSIX self-exec,
    native-Windows replacement, frozen/source argv handling, active
    stream/run drain, bytecode purge, and the retriable supervisor fallback.

    A revalidation callback is passed to the authority so an A→B→A rollback
    (or an unreadable tree) that happens while the restart is still pending
    cancels the re-exec instead of bouncing a healthy process.
    """
    global _SCHEDULED_RESTART

    with _SCHEDULE_LOCK:
        if _SCHEDULED_RESTART:
            return
        _SCHEDULED_RESTART = True

    def _revalidate() -> bool:
        """Return True only while a concrete known old→new transition holds."""
        global _SCHEDULED_RESTART
        old_rev = _AGENT_REVISION
        if old_rev is None:
            return False
        current_rev = _read_agent_revision(
            _AGENT_SOURCE_DIR, module_path=_AGENT_MODULE_PATH
        )
        if current_rev is not None and current_rev != old_rev:
            return True
        # Rolled back (A→B→A) or unreadable: re-arm the scheduler so a later
        # concrete transition can schedule a fresh restart.
        with _SCHEDULE_LOCK:
            _SCHEDULED_RESTART = False
        logger.warning(
            "Agent revision no longer differs from the loaded runtime "
            "(rollback?); cancelling scheduled restart"
        )
        return False

    def _do_restart() -> None:
        try:
            from api.updates import _schedule_restart
        except Exception:
            # The shared authority is unavailable — never keep serving a
            # mixed runtime. Exit so a supervisor (systemd, start.sh,
            # Docker/Compose) respawns us.
            logger.exception("restart authority unavailable; exiting for supervisor")
            os._exit(1)
        try:
            _schedule_restart(delay=delay, revalidate=_revalidate)
        except Exception:
            # Same fail-safe: the restart authority refused to run, so exit
            # for the supervisor instead of serving a mixed runtime.
            logger.exception("restart authority failed; exiting for supervisor")
            os._exit(1)

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
    """Fail closed on a stale Agent runtime; auto-restart on a concrete upgrade.

    A previously-known revision that becomes unreadable is indistinguishable
    from source drift, so it stays fail-closed (blocking requests) without
    scheduling a restart. A concrete known old→new transition schedules one
    restart through the shared authority and raises synchronously so the typed
    409/SSE error is durably emitted before the delayed re-exec begins.
    """
    if _AGENT_REVISION is None:
        return

    old_rev = _AGENT_REVISION
    current_rev = _read_agent_revision(
        _AGENT_SOURCE_DIR, module_path=_AGENT_MODULE_PATH
    )
    if current_rev is None:
        # Fail closed: losing a previously-known revision is indistinguishable
        # from source drift; never reuse the mixed runtime.
        raise AgentRuntimeChangedError(_RESTART_MESSAGE)
    if current_rev != old_rev:
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
