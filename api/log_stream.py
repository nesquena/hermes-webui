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

Two things make the *next* variant of this failure visible instead of silent
(#0574, after the process went log-blind for ~54h while serving 200s):

* A write that fails anyway is counted and announced once per process on a
  private duplicate of fd 2 — the old bare ``except: pass`` is what made a
  dead log stream indistinguishable from an idle one.
* :func:`access_log_status` reports that state, so an external monitor can
  ask a *serving* process whether its access log is actually reaching the
  journal (surfaced as ``GET /api/health/logging``).
"""
import os
import threading
import time

_write_lock = threading.Lock()
_log_fd = None

# Private duplicate of stderr, used only to announce that the durable stdout
# writer itself failed. Same shape as api/crash_visibility.py's fd-2 writer:
# a diagnostic path must not depend on the stream it is reporting about.
_alarm_fd = None

# Access-log liveness, read by access_log_status(). Guarded by _write_lock.
_emitted = 0
_dropped = 0
_last_emit_at = None
_last_error = None
_alarm_raised = False


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


def _capture_alarm_fd() -> None:
    """Duplicate stderr once for the "the log writer itself failed" notice."""
    global _alarm_fd
    try:
        fd = os.dup(2)
        os.set_inheritable(fd, False)
        _alarm_fd = fd
    except OSError:
        _alarm_fd = None


def _raise_alarm(detail: str) -> None:
    """Announce, once per process, that a log line was lost. Never raises."""
    global _alarm_raised
    try:
        with _write_lock:
            if _alarm_raised:
                return
            _alarm_raised = True
            fd = _alarm_fd
        data = (
            f'[webui] ERROR access logging is dropping lines: {detail} '
            '(see GET /api/health/logging)\n'
        ).encode('utf-8', errors='replace')
        if fd is None:
            return
        while data:
            data = data[os.write(fd, data):]
    except Exception:
        # A diagnostic writer that raises would itself become a silent failure.
        pass


def durable_print(message: str) -> None:
    """Write a log line to the stdout captured at startup; never raises.

    Callers use this for request/error logging that must not break responses
    (and must not die when agent code hijacks sys.stdout). If fd duplication
    failed at startup, degrades to plain print() — best-effort, like the old
    behavior.

    A line that cannot be written is counted and announced on stderr rather
    than silently discarded; :func:`access_log_status` exposes the tally.
    """
    global _emitted, _last_emit_at
    try:
        data = (message + '\n').encode('utf-8', errors='replace')
        with _write_lock:
            if _log_fd is None:
                print(message, flush=True)
            else:
                while data:
                    data = data[os.write(_log_fd, data):]
            _emitted += 1
            _last_emit_at = time.time()
    except Exception as exc:
        _note_drop(exc)


def _note_drop(exc: BaseException) -> None:
    """Record a lost log line and announce the first one. Never raises."""
    global _dropped, _last_error
    try:
        detail = f'{type(exc).__name__}: {exc}'
        with _write_lock:
            _dropped += 1
            _last_error = detail
        _raise_alarm(detail)
    except Exception:
        pass


def access_log_status() -> dict:
    """Snapshot of access-log liveness for health reporting.

    ``healthy`` is False when log lines have been lost, or when the durable
    stdout duplicate is unavailable and logging is riding the process-wide
    ``sys.stdout`` that #0095 showed can be hijacked out from under it. Both
    are sticky for the life of the process — exactly like the wedge they
    describe — so a monitor polling a *serving* process can tell "quiet" from
    "log-blind" without guessing at traffic levels.
    """
    with _write_lock:
        durable = _log_fd is not None
        emitted, dropped = _emitted, _dropped
        last_emit_at, last_error = _last_emit_at, _last_error
    if dropped:
        status = 'failing'
    elif not durable:
        status = 'degraded'
    else:
        status = 'ok'
    return {
        'status': status,
        'healthy': status == 'ok',
        'durable_fd': durable,
        'emitted': emitted,
        'dropped': dropped,
        'last_line_age_seconds': (
            None if last_emit_at is None else round(time.time() - last_emit_at, 3)
        ),
        'last_error': last_error,
    }


capture_stdout_fd()
_capture_alarm_fd()
