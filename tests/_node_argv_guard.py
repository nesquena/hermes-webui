"""Guard: no node harness may hand a large script to node through argv.

``execve`` caps a SINGLE argument at ``MAX_ARG_STRLEN`` (131072 bytes on Linux).
Behavioural tests that lift real functions out of ``static/*.js`` and splice them
into a DOM shim can grow past that with an ordinary production change, and the
failure is not a test failure at all — it is ``OSError: [Errno 7] Argument list
too long`` raised by ``subprocess`` before node ever starts, which tells the
reader nothing about the behaviour under test.

The fix is to run such a harness from a temporary ``.js`` file
(``tests/_node_script.run_node_script``). This guard makes that structural
rather than a habit: it records every argv entry handed to a node process and
fails the session when one of them crosses the safe line.

``subprocess.Popen`` is deliberately NOT wrapped: ``tests/_real_repo_git_guard``
subclasses it at import time, so replacing it breaks that module. Every node
harness in this suite goes through ``subprocess.run`` / ``call`` /
``check_call`` / ``check_output``.
"""

from __future__ import annotations

import os
import subprocess

# Linux MAX_ARG_STRLEN. One production-code change used to push a footer-scope
# harness from 128544 to 138883 bytes, i.e. straight past this.
ARGV_LIMIT = 131_072
# Fail earlier than the hard ceiling: at 100000 a harness is one edit away from
# E2BIG, and the fix (run from a file) is free.
ARGV_SAFE = 100_000

_RECORDS = []
_INSTALLED = False


def _argv_of(args):
    if isinstance(args, (list, tuple)):
        return list(args)
    return [args]


def _is_node(argv) -> bool:
    return bool(argv) and os.path.basename(str(argv[0])) in ("node", "node.exe")


def _record(argv) -> None:
    if not _is_node(argv):
        return
    sizes = [len(str(a).encode("utf-8", "replace")) for a in argv]
    if sizes and max(sizes) > ARGV_SAFE:
        _RECORDS.append(
            {
                "max_entry": max(sizes),
                "sizes": sizes,
                "flag": "-e" if "-e" in [str(a) for a in argv] else "path",
            }
        )


def _wrap(fn):
    def inner(*args, **kwargs):
        try:
            _record(_argv_of(args[0]) if args else [])
        except Exception:
            pass
        return fn(*args, **kwargs)

    return inner


def pytest_configure(config):
    """Install the recorder. Called from tests/conftest.py."""
    global _INSTALLED
    if _INSTALLED:
        return
    for attr in ("run", "call", "check_call", "check_output"):
        current = getattr(subprocess, attr)
        if getattr(current, "_node_argv_guard", False):
            continue
        wrapped = _wrap(current)
        wrapped._node_argv_guard = True
        setattr(subprocess, attr, wrapped)
    _INSTALLED = True


def finish():
    """Return the list of oversized node invocations seen this session."""
    return list(_RECORDS)


def reset():
    _RECORDS.clear()
