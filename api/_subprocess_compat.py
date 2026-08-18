"""Win32 subprocess creationflags — the WebUI's single source of truth.

Every ``subprocess`` spawn of a console application on Windows creates a
console window unless ``CREATE_NO_WINDOW`` is passed. For a long-lived
server that shells out to ``git`` on ordinary page loads, that means a
burst of black windows flashing across the user's desktop on every
navigation, plus orphaned ``conhost.exe`` processes accumulating over a
session.

This has now been fixed three times in three places, each time for one
module in isolation:

* #3710 — test-helper subprocesses popping focus-stealing windows.
* #4626 — the self-update / supervisor restart path flashing an empty
  console (users closed that window, which took the WebUI down with it).
* #5692 — workspace git commands (``workspace_git._run_git``).

Each fix added its own private ``_windows_hide_flags()`` and patched only
the call sites in front of it, so the next unpatched ``subprocess.run(
["git", ...])`` reintroduced the same bug class. This module ends that
cycle: one helper, imported everywhere, enforced by
``tests/test_windows_console_window_guard.py``, which AST-scans the API
package and fails on any spawn that omits ``creationflags``.

**All helpers return 0 on non-Windows** — ``creationflags=0`` is the
``subprocess`` default, so passing these on Linux/macOS is a genuine
no-op. Call sites therefore stay platform-unconditional; no
``if sys.platform == "win32":`` branching at the point of use.

Deliberately standalone: the agent package ships an equivalent
``hermes_cli._subprocess_compat``, but ``hermes_cli`` is an *optional*
dependency (a standalone WebUI runs without an agent install), and
importing it here would make core git functionality contingent on it.
The duplication is intentional and load-bearing — see the note in
``workspace_git`` history for #5692.
"""

from __future__ import annotations

import subprocess
import sys

__all__ = [
    "IS_WINDOWS",
    "windows_hide_flags",
    "windows_detach_flags",
]


IS_WINDOWS = sys.platform == "win32"


# Win32 CreationFlags, defined numerically rather than read off the
# ``subprocess`` module: these attributes only exist on Windows builds of
# CPython, so ``subprocess.CREATE_NO_WINDOW`` raises AttributeError at
# import time on Linux/macOS. Literals keep this module importable
# everywhere, which the guard test depends on.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


def windows_hide_flags() -> int:
    """Creationflags that hide a child's console window WITHOUT detaching it.

    This is the right helper for the overwhelming majority of spawns in
    this codebase: short-lived console programs (``git``, ``pip``,
    ``hermes gateway ...``, VS Code's ``code.cmd`` shim) that we run
    synchronously and whose output we collect.

    The key difference from :func:`windows_detach_flags` is the absence of
    ``DETACHED_PROCESS``. A detached child gets no console *and* loses the
    inherited stdio handles, which silently breaks ``capture_output=True``
    / ``communicate()``. Use this one whenever you read the child's
    stdout, stderr, or exit code.

    Returns 0 on non-Windows.
    """
    if not IS_WINDOWS:
        return 0
    return _CREATE_NO_WINDOW


def windows_detach_flags() -> int:
    """Creationflags that fully detach a long-lived background child.

    For spawns that must outlive this process (the self-update respawn,
    supervisor restarts) rather than short probes. ``DETACHED_PROCESS``
    severs the console so closing a terminal doesn't kill the child;
    ``CREATE_NEW_PROCESS_GROUP`` stops Ctrl+C propagating into it; and
    ``CREATE_NO_WINDOW`` is still required alongside ``DETACHED_PROCESS``
    on modern Windows, because a console-subsystem child (``python.exe``)
    otherwise flashes an empty window regardless — the exact regression
    #4626 fixed.

    Callers must NOT capture output from a process spawned with these;
    redirect to ``DEVNULL`` or a file. Returns 0 on non-Windows, where the
    POSIX equivalent is ``start_new_session=True``.
    """
    if not IS_WINDOWS:
        return 0
    return _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
