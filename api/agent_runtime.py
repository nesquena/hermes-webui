"""Fail-closed guard for in-process Hermes Agent source revisions.

Hermes WebUI currently imports ``run_agent.AIAgent`` into its long-lived server
process. If the Agent checkout changes while that process is alive, Python may
combine already-cached modules with newly-read source. Refuse to reuse that
mixed runtime and require a clean WebUI restart instead.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import threading

from api.config import _AGENT_DIR


_RESTART_MESSAGE = (
    "Hermes Agent was updated while Hermes WebUI was running. "
    "Restart Hermes WebUI before retrying this action."
)


def _read_agent_revision(agent_dir: Path) -> str | None:
    """Return the checkout HEAD, or ``None`` for a non-Git/unavailable source."""
    try:
        result = subprocess.run(
            ["git", "-C", str(agent_dir), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


_AGENT_REVISION = _read_agent_revision(_AGENT_DIR)
_AIAgent = None
_RUNTIME_LOCK = threading.Lock()


class AgentRuntimeChangedError(RuntimeError):
    """Raised when the loaded Agent runtime no longer matches its source tree."""


def ensure_agent_runtime_current() -> None:
    """Reject a known Git checkout change instead of mixing Python modules."""
    if _AGENT_REVISION is None:
        return
    if _read_agent_revision(_AGENT_DIR) != _AGENT_REVISION:
        raise AgentRuntimeChangedError(_RESTART_MESSAGE)


def require_ai_agent_class():
    """Import ``AIAgent`` after proving the loaded source revision is current."""
    ensure_agent_runtime_current()
    from run_agent import AIAgent  # noqa: PLC0415

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
            if _AGENT_REVISION is None:
                _AGENT_REVISION = _read_agent_revision(_AGENT_DIR)
        return _AIAgent
