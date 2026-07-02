"""Crash-visibility hardening for the Hermes WebUI server (issue #4633).

Background
----------
``server.py`` runs a ``ThreadingHTTPServer`` with ``daemon_threads = True``.
Under that model a *silent* process death is possible in ways that leave the
log ending mid-request with no traceback, no shutdown-audit line, and no core
dump:

* A **native** crash (segfault / abort in a C extension, e.g. sqlite, ssl,
  a provider SDK) terminates the interpreter with no Python-level traceback.
  Without ``faulthandler`` enabled, nothing is written.
* An **uncaught Python exception in a daemon request/SSE/long-poll thread**
  is reported through ``threading.excepthook``.  The stock hook prints to
  ``sys.stderr`` with no context, and if that stream is gone the report is
  lost — so a single handler-thread exception can vanish silently.
* An uncaught exception on the **main thread** goes through ``sys.excepthook``.

The paired *root cause* (a memory issue that eventually triggers the death)
is being fixed separately in #4765.  This module is purely about
**diagnostics**: when the process dies we want a recorded reason, and one
handler-thread exception must never disappear without a log line.

Design constraints
------------------
* Standard library only; no new dependencies.
* Every hook is defensive — a logging hook must NEVER raise (a raising hook
  would itself become a new silent-failure mode).
* Diagnostics are written **directly to the stderr stream** (which the
  bootstrap redirects into the WebUI log file) *and* mirrored through the
  ``logging`` module.  The WebUI configures no logging handlers, so an
  ``INFO``/``ERROR`` record would otherwise be swallowed by logging's
  ``lastResort`` filter (WARNING+ only) and never reach the log — the exact
  failure mode of #4633.  The direct write guarantees the line lands.
* ``install_crash_visibility()`` is idempotent and safe to call at import or
  startup time.
"""
from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
import sys
import threading
import traceback
from typing import Optional, TextIO

__all__ = [
    "install_crash_visibility",
    "is_installed",
    "thread_excepthook",
    "main_excepthook",
]

_LOGGER = logging.getLogger("server")

# Guard so repeated calls (e.g. import + explicit startup call, or a test that
# re-imports) don't stack duplicate hooks / atexit registrations.
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()

# Strong reference to the stream faulthandler writes into. faulthandler keeps
# the file descriptor, but holding the object prevents premature GC of a
# wrapper and lets tests introspect what we enabled against.
_FAULT_STREAM: Optional[TextIO] = None

# Remember prior hooks so we can chain (and so tests can restore them).
_PREV_THREAD_HOOK = None
_PREV_SYS_HOOK = None


def is_installed() -> bool:
    """Return True once :func:`install_crash_visibility` has run."""
    return _INSTALLED


def _resolve_stream(stream: Optional[TextIO]) -> TextIO:
    """Pick a real, writable stream, defaulting to stderr."""
    if stream is not None:
        return stream
    err = sys.stderr
    if err is not None:
        return err
    # As a last resort, wrap fd 2 directly so we can still emit something.
    return os.fdopen(os.dup(2), "w", closefd=True)


def _direct_write(text: str) -> None:
    """Write a diagnostic line straight to the fault stream. Never raises."""
    try:
        stream = _FAULT_STREAM if _FAULT_STREAM is not None else sys.stderr
        if stream is None:
            return
        stream.write(text if text.endswith("\n") else text + "\n")
        try:
            stream.flush()
        except Exception:
            pass
    except Exception:
        # Absolutely nothing a diagnostic writer may raise should propagate.
        pass


def _emit(level: int, message: str, *, exc_info=None) -> None:
    """Emit a diagnostic via logging AND directly to the fault stream.

    Never raises. The direct write is what guarantees visibility in the WebUI
    log (which configures no logging handlers); the logging call is what lets
    tests/caplog and any future handler capture the same record.
    """
    text = message
    if exc_info is not None:
        try:
            etype, evalue, etb = exc_info
            tb_text = "".join(traceback.format_exception(etype, evalue, etb))
            text = f"{message}\n{tb_text}".rstrip()
        except Exception:
            text = message
    _direct_write(text)
    try:
        _LOGGER.log(level, message, exc_info=exc_info)
    except Exception:
        # If the logging subsystem itself blows up, the direct write above
        # already captured the diagnostic — swallow so we never re-crash here.
        pass


def thread_excepthook(args) -> None:
    """threading.excepthook: log any uncaught exception in a daemon/handler thread.

    ``args`` is a ``threading.ExceptHookArgs`` (exc_type, exc_value,
    exc_traceback, thread). A normal thread shutdown may pass exc_type=None.
    """
    try:
        exc_type = getattr(args, "exc_type", None)
        exc_value = getattr(args, "exc_value", None)
        exc_tb = getattr(args, "exc_traceback", None)
        thread = getattr(args, "thread", None)

        # SystemExit inside a thread is an intentional stop, not a crash.
        if exc_type is None or issubclass(exc_type, SystemExit):
            return

        thread_name = getattr(thread, "name", None) or "unknown"
        thread_ident = getattr(thread, "ident", None)
        daemon = getattr(thread, "daemon", None)
        _emit(
            logging.ERROR,
            "[crash-visibility] uncaught exception in thread "
            "name=%s ident=%s daemon=%s type=%s"
            % (thread_name, thread_ident, daemon, getattr(exc_type, "__name__", exc_type)),
            exc_info=(exc_type, exc_value, exc_tb),
        )
    except Exception:
        # A hook that raises would re-introduce the silent-death class of bug.
        try:
            _direct_write("[crash-visibility] thread_excepthook failed to log an exception")
        except Exception:
            pass
    finally:
        # Chain to any previously-installed hook, but only if it isn't the
        # stock one (which just prints to stderr again — we already did better).
        prev = _PREV_THREAD_HOOK
        try:
            if prev is not None and prev is not threading.__excepthook__:
                prev(args)
        except Exception:
            pass


def main_excepthook(exc_type, exc_value, exc_tb) -> None:
    """sys.excepthook: log any uncaught exception on the main thread."""
    try:
        is_keyboard_interrupt = bool(
            isinstance(exc_type, type) and issubclass(exc_type, KeyboardInterrupt)
        )
        if is_keyboard_interrupt:
            # Preserve default Ctrl-C behaviour: no scary traceback dump.
            prev = _PREV_SYS_HOOK or sys.__excepthook__
            try:
                prev(exc_type, exc_value, exc_tb)
            except Exception:
                pass
            return
        _emit(
            logging.CRITICAL,
            "[crash-visibility] uncaught exception on main thread type=%s"
            % (getattr(exc_type, "__name__", exc_type),),
            exc_info=(exc_type, exc_value, exc_tb),
        )
    except Exception:
        try:
            _direct_write("[crash-visibility] main_excepthook failed to log an exception")
        except Exception:
            pass
    finally:
        prev = _PREV_SYS_HOOK
        try:
            if prev is not None and prev is not sys.__excepthook__:
                prev(exc_type, exc_value, exc_tb)
        except Exception:
            pass


def _exit_audit() -> None:
    """atexit hook: record that the process is exiting.

    A plain interpreter exit (return from main, sys.exit, or the tail of a
    fatal error that still unwinds atexit) leaves a breadcrumb so we can tell a
    *clean* shutdown apart from a truly silent death — if the log ends with no
    exit-audit line at all, the process was killed without unwinding
    (OOM kill / SIGKILL / native abort), which itself narrows the diagnosis.
    """
    try:
        _emit(
            logging.INFO,
            "[crash-visibility] process exit pid=%s thread=%s"
            % (os.getpid(), threading.current_thread().name),
        )
    except Exception:
        pass


def install_crash_visibility(
    *,
    stream: Optional[TextIO] = None,
    enable_faulthandler: bool = True,
    register_sigusr1_dump: bool = True,
) -> bool:
    """Install faulthandler + excepthooks + exit audit. Idempotent.

    Parameters
    ----------
    stream:
        Where diagnostics are written. Defaults to ``sys.stderr`` (redirected
        into the WebUI log file by the bootstrap).
    enable_faulthandler:
        Enable ``faulthandler`` so a native segfault/abort dumps a C-level
        traceback of every thread instead of vanishing.
    register_sigusr1_dump:
        On POSIX, register ``SIGUSR1`` to dump all thread stacks on demand —
        invaluable for diagnosing a *hang* (send ``kill -USR1 <pid>``) as
        opposed to a crash. Ignored where SIGUSR1 is unavailable (Windows).

    Returns True if it installed on this call, False if it was already active.
    """
    global _INSTALLED, _FAULT_STREAM, _PREV_THREAD_HOOK, _PREV_SYS_HOOK

    with _INSTALL_LOCK:
        if _INSTALLED:
            return False

        _FAULT_STREAM = _resolve_stream(stream)

        # 1) faulthandler: native crash → C-level traceback of all threads.
        if enable_faulthandler:
            try:
                faulthandler.enable(file=_FAULT_STREAM, all_threads=True)
            except Exception:
                # Some frozen/embedded builds restrict faulthandler; degrade
                # gracefully rather than blocking startup.
                _direct_write("[crash-visibility] faulthandler.enable() failed")

        # 1b) On-demand all-thread stack dump for diagnosing hangs.
        if register_sigusr1_dump:
            sigusr1 = getattr(signal, "SIGUSR1", None)
            if sigusr1 is not None:
                try:
                    faulthandler.register(
                        sigusr1, file=_FAULT_STREAM, all_threads=True, chain=True
                    )
                except Exception:
                    pass

        # 2) threading.excepthook: daemon/handler-thread crash → logged.
        try:
            _PREV_THREAD_HOOK = threading.excepthook
            threading.excepthook = thread_excepthook
        except Exception:
            _direct_write("[crash-visibility] failed to install threading.excepthook")

        # 3) sys.excepthook: main-thread crash → logged.
        try:
            _PREV_SYS_HOOK = sys.excepthook
            sys.excepthook = main_excepthook
        except Exception:
            _direct_write("[crash-visibility] failed to install sys.excepthook")

        # 4) atexit exit audit: breadcrumb on clean/unwound exit.
        try:
            atexit.register(_exit_audit)
        except Exception:
            pass

        _INSTALLED = True
        return True
