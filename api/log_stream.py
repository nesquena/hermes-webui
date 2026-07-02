"""Durable log stream: access logging that survives sys.stdout hijacks.

The WebUI runs agent turns in-process on worker threads (api/streaming.py),
and agent tool dispatch rebinds process-wide sys.stdout/sys.stderr around
every tool call (devnull swap with a finally-restore). Concurrent turns race
those save/restore pairs, which can leave sys.stdout bound to a closed file
object permanently — after which every print()-based log line raises and is
silently dropped (#0095: service serving but log-blind for 14h).

This module duplicates the real stdout file descriptor once at import time —
during server startup, before any agent code can touch the streams — and
writes log lines straight to that private fd. No in-process rebinding,
closing, or capture of sys.stdout can silence it; only closing fd-level
stdout for the whole process could, and that would kill journald logging
regardless.
"""
import os
import threading

_write_lock = threading.Lock()
_log_fd = None


def capture_stdout_fd() -> None:
    """(Re)capture a private duplicate of the process's current stdout fd.

    Runs once at import (server startup). Tests re-invoke it after
    redirecting fd 1 so the durable stream follows their capture.
    """
    global _log_fd
    with _write_lock:
        previous, _log_fd = _log_fd, None
        if previous is not None:
            try:
                os.close(previous)
            except OSError:
                pass
        try:
            fd = os.dup(1)
            os.set_inheritable(fd, False)
            _log_fd = fd
        except OSError:
            _log_fd = None  # fall back to plain print() in durable_print


def durable_print(message: str) -> None:
    """Write a log line to the stdout captured at startup; never raises.

    Callers use this for request/error logging that must not break responses
    (and must not die when agent code hijacks sys.stdout). If fd duplication
    failed at startup, degrades to plain print() — best-effort, like the old
    behavior.
    """
    try:
        data = (message + '\n').encode('utf-8', errors='replace')
        with _write_lock:
            if _log_fd is None:
                print(message, flush=True)
                return
            while data:
                data = data[os.write(_log_fd, data):]
    except Exception:
        pass


capture_stdout_fd()
