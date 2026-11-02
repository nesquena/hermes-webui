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
import re
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
# Marker v2 (2026-10-03) carries an optional surviving-delegate line:
# ``delegate:<pid> ct:<create time>``.  A delegate keeps the update in
# flight after the owner process died, so the owner's death alone is not
# proof the update finished.  Only the pid is needed here; the ct value
# belongs to the Agent's own incarnation probe.
_AGENT_DELEGATE_LINE = re.compile(
    r"delegate:([0-9]+)\s+ct:[0-9]+(?:\.[0-9]+)?", re.ASCII
)
# Fold order across candidate marker homes and identity claims: an
# unverifiable marker outranks an in-flight one, an in-flight one outranks
# a leftover, and only an everywhere-absent marker reads as no update.
_MARKER_STATE_PRECEDENCE = ("unknown", "active", "stale_live", "stale", "absent")
# States a restart guard must refuse: an update in flight (a young owner or
# a long-lived one past the v1 ceiling), an unverifiable marker, an
# incomplete environment, or a checkout whose kernel lock is still held by
# an updater child (``held``).  A crashed update's leftover (``stale``)
# stays recoverable so a dead owner cannot block the operator's restart
# forever.
_AGENT_UPDATE_UNSAFE_STATES = ("active", "stale_live", "incomplete", "unknown", "held")
# Slow local Git reads must not look like source drift after just two seconds.
# Share one total budget across all reads; unreadable revisions fail closed.
_GIT_REVISION_TIMEOUT_SECONDS = 10
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
    """Return the tracked Agent HEAD within one total Git-read budget."""
    deadline = time.monotonic() + _GIT_REVISION_TIMEOUT_SECONDS

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", _GIT_REVISION_TIMEOUT_SECONDS)
        return remaining

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
            timeout=remaining_timeout(),
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
            timeout=remaining_timeout(),
            creationflags=windows_hide_flags(),
        )
        if tracked_result.returncode != 0:
            return None
        revision_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=remaining_timeout(),
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        return None

    # Scheduling/process-creation overhead can exceed a subprocess timeout.
    if time.monotonic() >= deadline:
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
        # Advertise that this server exposes the operator-initiated
        # /api/runtime/restart recovery (#7934) so clients only offer a
        # Restart action where the route exists. Older servers omit the key.
        "restart_available": True,
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

    owner_state = _marker_identity_state(pid, age_seconds)
    if owner_state == "unknown":
        return "unknown"
    if owner_state != "stale":
        return owner_state

    # The owner is gone.  A surviving delegate keeps the update in flight;
    # without one the marker is a crashed update's leftover.
    delegate_pid = _marker_delegate_pid(lines[2:])
    if delegate_pid is None:
        return "stale"
    delegate_state = _marker_identity_state(delegate_pid, age_seconds)
    return "unknown" if delegate_state == "unknown" else delegate_state


def _marker_identity_state(pid: int, age_seconds: float) -> str:
    """Classify one marker identity by process liveness and claim age.

    A process that still answers is never proof the update finished: v2
    markers run long-lived owners past the 20-minute ceiling (the ceiling
    only ages out v1 markers, which carry no creation time), and even a v1
    claim whose pid is alive fails closed rather than authorising a
    destructive restart.  Only a dead owner — with no live delegate — is a
    leftover that reads ``stale``.
    """
    alive = _pid_is_alive(pid)
    if alive is None:
        return "unknown"
    if not alive:
        return "stale"
    if age_seconds > _AGENT_UPDATE_MAX_AGE_SECONDS:
        return "stale_live"
    return "active"


def _marker_delegate_pid(tagged_lines: list[str]) -> int | None:
    """Return the first well-formed ``delegate:<pid> ct:<ct>`` line's pid."""
    for line in tagged_lines:
        match = _AGENT_DELEGATE_LINE.fullmatch(line.strip())
        if match:
            pid = int(match.group(1))
            if pid > 0:
                return pid
    return None


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


def _agent_update_marker_homes() -> tuple[Path, ...]:
    """Return candidate homes for the shared Agent update marker.

    The installed Agent (marker v2, ``hermes_cli/update_lock.py``) writes
    the marker at the profile-tree ROOT via ``get_default_hermes_root()``,
    while a WebUI launched on a named profile (``-p`` / sticky profile) has
    its captured ``HERMES_HOME`` re-homed to ``<root>/profiles/<p>``.
    Reading only the captured home then sees ``absent`` while an update is
    live — and an absent marker is exactly what authorises the destructive
    stale-runtime restart.  List the unwrapped base root first and keep the
    captured profile home as the legacy second candidate.
    """
    homes: list[Path] = []
    seen: set[str] = set()
    for candidate in (_base_marker_home(), _HERMES_HOME):
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key not in seen:
            seen.add(key)
            homes.append(candidate)
    return tuple(homes)


def _base_marker_home() -> Path:
    """Unwrap a profile-shaped ``HERMES_HOME`` to its profile-tree root."""
    try:
        from api.profiles import _unwrap_profile_home_to_base
    except ImportError:  # pragma: no cover - profiles always ships with WebUI
        return _HERMES_HOME
    try:
        return _unwrap_profile_home_to_base(_HERMES_HOME)
    except (AttributeError, TypeError, ValueError):
        return _HERMES_HOME


# The installed Agent (marker v2, ``hermes_cli/update_lock.py``) guards the
# checkout with a second authority besides the marker PID: a KERNEL lock on
# ``hermes-update.lock`` inside the checkout's git dir (or
# ``.hermes-update.lock`` at the checkout root for non-git installs). An
# updater's completion/build/git child inherits the lock FD (``pass_fds``),
# so the checkout can remain locked after the marker owner died — without
# any ``delegate:`` marker line.  Recovery therefore probes this lock
# read-only and fails closed while the actual loaded checkout is held.
_AGENT_CHECKOUT_LOCK_NAME = ".hermes-update.lock"
_AGENT_GIT_CHECKOUT_LOCK_NAME = "hermes-update.lock"

# The installed Agent's Windows custody protocol
# (``hermes_cli/update_lock.py``): the checkout lock's OWNER byte sits at
# 1<<20, and 16 LEASE bytes follow it at 1048577-1048592.  A descendant
# that joins its ancestor's lock cannot inherit one on Windows, so it
# takes one of those lease bytes: a completion child the kill-on-close
# job refused outlives a killed owner and keeps the checkout busy with
# no marker line naming it.  A probe must therefore contend with BOTH
# native ranges — locking any other byte (byte 0 in an earlier revision)
# intersects neither range and would report ``free`` while the Agent
# still holds custody.
_AGENT_WINDOWS_OWNER_LOCK_OFFSET = 1 << 20
_AGENT_WINDOWS_LEASE_SLOTS = 16


def _agent_checkout_lock_path() -> Path | None:
    """Return the installed Agent's checkout kernel lock path, or ``None``.

    Mirrors ``hermes_cli/update_lock.py`` ``checkout_lock_path`` for the
    LOADED Agent checkout: a linked worktree (``.git`` is a ``gitdir:``
    file) locks inside its common git dir; any other checkout locks
    ``.hermes-update.lock`` at its root.  Read-only — the path is resolved
    from disk, never created.
    """
    if _AGENT_SOURCE_DIR is None:
        return None
    root = _AGENT_SOURCE_DIR
    try:
        dot = root / ".git"
        if dot.is_dir():
            gitdir = dot
        elif dot.is_file():
            text = dot.read_text(encoding="utf-8-sig").strip()
            if not text.startswith("gitdir:"):
                return root / _AGENT_CHECKOUT_LOCK_NAME
            gitdir = root / text[len("gitdir:"):].strip()
        else:
            return root / _AGENT_CHECKOUT_LOCK_NAME
        common = gitdir / "commondir"
        if common.is_file():
            gitdir = gitdir / common.read_text(encoding="utf-8-sig").strip()
        return Path(os.path.normpath(gitdir)) / _AGENT_GIT_CHECKOUT_LOCK_NAME
    except OSError:
        # An unreadable checkout is never provably unlocked; probe the
        # root-side lock path, hardening the admission below.
        return root / _AGENT_CHECKOUT_LOCK_NAME


def _agent_checkout_lock_state() -> str:
    """Report whether the loaded Agent checkout's kernel lock is held.

    Mirrors the installed Agent's bounded read-only ``checkout_lock_held``
    probe (``hermes_cli/update_lock.py``): open the lock file and try one
    non-blocking exclusive lock.  A missing lock file is ``free``; a lock
    that cannot be taken is ``held``; a lock path that exists but cannot be
    probed (not a regular file, unreadable, unsupported primitives) is
    ``unknown`` and fails closed.  Never writes, truncates, or reclaims the
    lock: this is a diagnostic, not a custody claim.

    On Windows the probe speaks the Agent's actual byte-range protocol:
    it takes the owner byte at 1<<20 and the complete 16-byte lease range
    at 1048577-1048592 (``_try_lock`` in the Agent does the same), so it
    contends with the owner and with a joined descendant's lease — the
    survivor that keeps the checkout busy after its owner died.  Every
    range it acquires is released on every exit, and the O_RDWR handle
    never writes: only the byte-range probe touches the fd.
    """
    path = _agent_checkout_lock_path()
    if path is None:
        # No loaded checkout — nothing to protect.
        return "free"
    flags = getattr(os, "O_BINARY", 0)
    if sys.platform == "win32":
        # msvcrt.locking needs a writable-capable handle; O_RDWR never
        # writes here (only the byte-range probe touches the fd).
        flags |= os.O_RDWR
    else:
        flags |= os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return "free"
    except OSError:
        return "unknown"
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return "unknown"
        if not stat.S_ISREG(st.st_mode):
            return "unknown"
        if sys.platform == "win32":
            try:
                import msvcrt
            except ImportError:  # pragma: no cover - win32 ships msvcrt
                return "unknown"
            # The Agent's Windows custody ranges: the owner byte, then
            # every lease byte a surviving descendant can hold.  Both are
            # probed in one bounded, non-blocking pass, and every range
            # acquired is unlocked before any verdict is returned.
            acquired: list[tuple[int, int]] = []
            try:
                os.lseek(fd, _AGENT_WINDOWS_OWNER_LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired.append((_AGENT_WINDOWS_OWNER_LOCK_OFFSET, 1))
                lease_offset = _AGENT_WINDOWS_OWNER_LOCK_OFFSET + 1
                os.lseek(fd, lease_offset, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, _AGENT_WINDOWS_LEASE_SLOTS)
                acquired.append((lease_offset, _AGENT_WINDOWS_LEASE_SLOTS))
            except OSError:
                # Some byte in either native range is held (or the range
                # lock cannot be completed): fail closed.
                return "held"
            finally:
                for offset, count in reversed(acquired):
                    try:
                        os.lseek(fd, offset, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, count)
                    except OSError:
                        pass
            return "free"
        try:
            import fcntl
        except ImportError:  # pragma: no cover - POSIX ships fcntl
            return "unknown"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unknown"
        return "free"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _agent_update_transaction_state() -> str:
    """Report marker diagnostics, never proof of successful completion.

    The Agent removes its active marker on failed/interrupted exits too. Neither
    its absence nor a stale PID proves the checkout or environment is healthy.
    Candidate homes are folded with ``_MARKER_STATE_PRECEDENCE``: an
    unverifiable marker anywhere outranks an in-flight one, an in-flight one
    outranks a leftover, and only an everywhere-absent marker reads as no
    update at all.  A marker that reads idle is then cross-checked against
    the checkout kernel lock: the loaded checkout can stay held by an
    updater child after its marker owner died, so a held or unverifiable
    checkout reads ``held``/``unknown`` (both refused) instead of
    ``stale``/``unverified``.
    """
    live_states = [
        _read_live_agent_update(home / _AGENT_UPDATE_MARKER)
        for home in _agent_update_marker_homes()
    ]
    for state in _MARKER_STATE_PRECEDENCE:
        if state in live_states:
            live_state = state
            break
    else:  # pragma: no cover - defensive: an unclassified read fails closed
        live_state = "unknown"
    if live_state == "unknown":
        return "unknown"

    # The checkout kernel lock is a second authority on the LOADED Agent
    # checkout: an updater's completion/build/git child can hold it after
    # the marker owner died — no ``delegate:`` line needed — so a marker
    # that reads idle or leftover while the checkout is still locked is an
    # update still in flight.  A held or unverifiable checkout refuses even
    # when every marker identity is dead; only a provably free checkout
    # keeps the dead-owner/absent-marker recovery semantics.  Live marker
    # identities (``active``/``stale_live``) already refuse, so the probe
    # runs only where its verdict could authorise recovery.
    if live_state in ("absent", "stale"):
        checkout_state = _agent_checkout_lock_state()
        if checkout_state == "held":
            return "held"
        if checkout_state == "unknown":
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
    """Import ``AIAgent`` after proving the loaded source revision is current."""
    ensure_agent_runtime_current()
    from run_agent import AIAgent  # noqa: PLC0415

    _capture_loaded_agent_revision()
    # Every WebUI construction site resolves the class here: refuse a memory provider
    # instance shared across profiles before any of them initialises it (#8517).
    from api.memory_profile_scope import install_shared_provider_instance_guard  # noqa: PLC0415

    install_shared_provider_instance_guard()
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
