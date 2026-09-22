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
from functools import lru_cache
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
    coerce_reasoning_effort_for_model,
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
# O_NOFOLLOW is POSIX; on platforms that lack it the fast os.open() path is not
# taken at all (see _MARKER_SAFE_OPEN_AVAILABLE below).
# The marker read hardening relies on two POSIX-only open flags to stay both
# non-blocking (never hang on a FIFO/device) and symlink-safe. O_NONBLOCK is
# Unix-only and O_NOFOLLOW is absent on some platforms; accessing them
# unconditionally raises AttributeError on native Windows. Resolve them safely
# and only take the os.open() fast path when BOTH are genuinely available —
# otherwise the read cannot prove non-blocking + no-follow and must fall back to
# an lstat-only classification (see _read_live_agent_update).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_MARKER_SAFE_OPEN_AVAILABLE = bool(getattr(os, "O_NOFOLLOW", 0)) and bool(
    getattr(os, "O_NONBLOCK", 0)
)
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


def _reasoning_config_for_agent_destination(agent, value):
    """Clamp an Agent reasoning assignment against its current destination.

    Hermes Agent re-resolves ``reasoning_config`` when a fallback activates and
    when ``/model`` switches the live agent. Older Agent builds parse the global
    preference at those chokepoints but do not apply the destination model and
    provider ceiling. Intercepting the shared attribute assignment keeps both
    transitions safe without patching the installed Agent checkout.
    """
    if not isinstance(value, dict) or value.get("enabled") is False:
        return value
    effort = value.get("effort")
    if not effort:
        return value
    try:
        coerced = coerce_reasoning_effort_for_model(
            effort,
            getattr(agent, "model", None),
            provider_id=getattr(agent, "provider", None),
            base_url=getattr(agent, "base_url", None),
            config_data=getattr(agent, "_webui_reasoning_config_snapshot", None),
        )
    except Exception:
        # Transition-time capability uncertainty must fail closed for the two
        # supra-ceiling tiers this PR adds. Preserve the established GPT-5
        # landing where it can be identified without metadata; every other
        # unresolved destination gets the universally safe ``high`` ceiling.
        if str(effort).strip().lower() in {"max", "ultra"}:
            bare_model = str(getattr(agent, "model", None) or "").lower().rsplit("/", 1)[-1]
            fallback_effort = (
                "xhigh"
                if bare_model.startswith("gpt-5") and "gpt-5.6" not in bare_model
                else "high"
            )
            clamped = dict(value)
            clamped["effort"] = fallback_effort
            return clamped
        return value
    if not coerced:
        return None
    if coerced == effort:
        return value
    clamped = dict(value)
    clamped["effort"] = coerced
    return clamped


def _agent_destination_fields_ready(agent) -> bool:
    """True once the instance itself holds its route (provider + base_url).

    The installed Agent constructor (revision d6ad555a16b7ad9a3324db3df1e1db7edec3e0a1)
    stores ``model`` and ``reasoning_config`` through ``_PASSTHROUGH_PARAMS``
    BEFORE assigning ``base_url`` and ``provider``. Until both route fields
    exist on the instance, destination coercion would resolve the route from
    the DEFAULT PROFILE instead of the session destination — e.g. a Gemini or
    Copilot profile re-coercing a constructor ``max`` for OpenAI-Codex
    GPT-5.6 down to ``xhigh``/``high``. The constructor value is already
    destination-coerced by the WebUI caller and must pass through untouched;
    later writes (fallback activation, /model switch) run with the full route
    present and stay guarded. (#6018 gate 2026-09-09)
    """
    try:
        instance_dict = getattr(agent, "__dict__", None)
    except Exception:
        return True
    if not isinstance(instance_dict, dict):
        # Slotted instances cannot be inspected reliably; treat any resolvable
        # provider as ready rather than skip the guard on a technicality.
        return getattr(agent, "provider", None) is not None
    return "provider" in instance_dict and (
        "base_url" in instance_dict or "_base_url" in instance_dict
    )


@lru_cache(maxsize=1)
def _destination_aware_ai_agent_class(agent_class):
    """Return a bounded-cached class guarding transition-time reasoning writes."""
    if agent_class is None or getattr(
        agent_class, "_webui_destination_reasoning_guard", False
    ):
        return agent_class

    class DestinationAwareMeta(type(agent_class)):
        def __call__(cls, *args, **kwargs):
            agent = super().__call__(*args, **kwargs)
            # Internal Agent constructors (delegation, review, compression)
            # import run_agent.AIAgent directly and do not pass WebUI's already-
            # coerced config. Capture the active Agent profile snapshot, then
            # replay the constructor value once model/provider/base_url exist.
            try:
                from hermes_cli.config import load_config_readonly

                snapshot = load_config_readonly()
            except Exception:
                snapshot = None
            if isinstance(snapshot, dict):
                agent._webui_reasoning_config_snapshot = snapshot
            if _agent_destination_fields_ready(agent):
                current = getattr(agent, "reasoning_config", None)
                if current is not None:
                    agent.reasoning_config = current
            return agent

    class DestinationAwareAIAgent(agent_class, metaclass=DestinationAwareMeta):
        _webui_destination_reasoning_guard = True

        def __setattr__(self, name, value):
            if name == "reasoning_config":
                # Constructor-phase assignment (route fields not yet on the
                # instance): the value was already coerced against the session
                # destination by the caller — do NOT re-coerce it against the
                # default profile. Every post-construction write keeps the
                # destination guard. (#6018 gate 2026-09-09)
                if _agent_destination_fields_ready(self):
                    value = _reasoning_config_for_agent_destination(self, value)
            super().__setattr__(name, value)

    # Keep diagnostics and inspect.signature output aligned with the installed
    # Agent class; only the attribute-assignment guard differs.
    DestinationAwareAIAgent.__name__ = agent_class.__name__
    DestinationAwareAIAgent.__qualname__ = agent_class.__qualname__
    DestinationAwareAIAgent.__module__ = agent_class.__module__
    return DestinationAwareAIAgent


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
    if not _MARKER_SAFE_OPEN_AVAILABLE:
        # Without both O_NONBLOCK and O_NOFOLLOW we cannot prove the read is
        # non-blocking and symlink-safe (e.g. native Windows), so never open the
        # marker: an unverifiable marker fails closed to ``unknown``, and only a
        # genuinely missing path is ``absent``.
        try:
            marker.lstat()
        except FileNotFoundError:
            return "absent"
        except (OSError, ValueError, TypeError):
            return "unknown"
        return "unknown"
    try:
        fd = os.open(marker, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW)
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
    fresh_revision = None
    try:
        fresh_revision = _read_agent_revision(
            _AGENT_SOURCE_DIR, module_path=_AGENT_MODULE_PATH
        )
    except Exception:
        # An unreadable revision is indistinguishable from a changed one, so a
        # failed read must fail CLOSED like every other identity-loss shape
        # (deleted, permission-denied, empty, corrupt, removed directory).
        # Letting the raw exception escape would surface as an HTTP 500 instead
        # of the typed stale-runtime response the barrier is meant to produce.
        fresh_revision = None
    if fresh_revision == _AGENT_REVISION:
        return

    # Automatic restart needs an Agent-owned success receipt bound to this
    # transaction, final revision and healthy environment, plus an atomic
    # handoff excluding mutations across replacement. Marker polling and a
    # final revision read supply neither contract. Keep this path manual.
    raise AgentRuntimeChangedError(
        _RESTART_REQUIRED_MESSAGE,
        agent_update_state=_agent_update_transaction_state(),
    )


def require_ai_agent_class():
    """Import the guarded ``AIAgent`` after proving its revision is current."""
    ensure_agent_runtime_current()
    import run_agent  # noqa: PLC0415

    try:
        agent_class = run_agent.AIAgent
    except AttributeError as exc:
        # Preserve the historical lazy-import contract used by gateway-only
        # startup, whose test/runtime stub intentionally has no AIAgent symbol.
        raise ImportError("cannot import name 'AIAgent' from 'run_agent'") from exc
    _capture_loaded_agent_revision()
    guarded = _destination_aware_ai_agent_class(agent_class)
    # Delegation/review/compression paths import this canonical symbol locally
    # after the parent agent is running. Publish the bounded-cached guard there
    # so no in-process constructor can silently recover the undecorated class.
    run_agent.AIAgent = guarded
    return guarded


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
