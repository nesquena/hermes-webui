"""
Diagnostic signal trap — tracks why hermes-webui exits unexpectedly.

Designed to be a no-op when not invoked. Activate by calling
`api.diag_shim.install()` early in `server.main()`, and wrap the
serving call with `api.diag_shim.wrap_serve_forever(httpd.serve_forever)`.

Writes JSON marker files to ``/tmp/hermes-webui-shim/`` so we can
distinguish, after the fact, between:

  * Clean exit (execv / os._exit(0) / sys.exit)       → no "exception"/"signal" marker
  * Unhandled exception inside serve_forever()        → "exception" marker
  * OS / supervisor signal (SIGTERM, SIGKILL, etc.)   → "signal" marker
  * Normal Python interpreter shutdown                → "atexit" marker

Each marker is a JSON file with PID, PPID, uptime, the active thread list,
the current stack frame (for signals) or full traceback (for exceptions),
and the open-file-descriptor count.

SIGKILL cannot be caught by a signal handler (kernel-level), but if a
SIGKILL is sent, a sibling marker with the "no_marker" reason is written
by a tiny companion watchdog that re-arms the trap on every /health probe
— so even an untrappable death leaves evidence on disk.

Local / debug only — does not change server behavior, only observes it.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

SHIM_DIR = "/tmp/hermes-webui-shim"

_START_TIME = time.time()
_INSTALLED = False
_COUNTER = 0
_LOCK = threading.Lock()


def _ensure_dir() -> None:
    try:
        os.makedirs(SHIM_DIR, exist_ok=True)
    except Exception:
        pass


def _marker_path(kind: str) -> str:
    global _COUNTER
    with _LOCK:
        _COUNTER += 1
        counter = _COUNTER
    # millisecond timestamp + monotonic counter for uniqueness
    return os.path.join(
        SHIM_DIR, f"{int(time.time() * 1000)}-{counter:03d}-{kind}.json"
    )


def _thread_snapshot() -> list[dict]:
    threads = []
    for t in threading.enumerate():
        try:
            frame = sys._current_frames().get(t.ident)
            stack = ""
            if frame is not None:
                stack = "".join(traceback.format_stack(frame))[:2000]
            threads.append(
                {
                    "name": t.name,
                    "ident": t.ident,
                    "daemon": t.daemon,
                    "is_alive": t.is_alive(),
                    "stack": stack,
                }
            )
        except Exception as e:
            threads.append({"name": t.name, "ident": t.ident, "error": str(e)})
    return threads


def _fd_count() -> int | None:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        return None


def _write_marker(kind: str, payload: dict) -> None:
    _ensure_dir()
    path = _marker_path(kind)
    body = {
        "kind": kind,
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "uptime_seconds": round(time.time() - _START_TIME, 3),
        "argv": sys.argv,
        "executable": sys.executable,
        "frozen": getattr(sys, "frozen", False),
        "fd_count": _fd_count(),
        **payload,
    }
    try:
        with open(path, "w") as f:
            json.dump(body, f, indent=2, default=str)
        try:
            os.sync()
        except Exception:
            pass
    except Exception as e:
        sys.stderr.write(f"[sigtrap] FAILED to write marker {path}: {e}\n")
        sys.stderr.flush()


def _signal_handler(signum, frame):  # noqa: ARG001 — frame is required by signal.signal
    try:
        sig_name = (
            signal.Signals(signum).name
            if hasattr(signal, "Signals")
            else str(signum)
        )
    except Exception:
        sig_name = str(signum)

    stack = ""
    if frame is not None:
        try:
            stack = "".join(traceback.format_stack(frame))[:4000]
        except Exception:
            pass

    _write_marker(
        "signal",
        {
            "signal_number": signum,
            "signal_name": sig_name,
            "stack": stack,
            "threads": _thread_snapshot(),
        },
    )

    # SIGPIPE is special: it does NOT indicate that the process is broken,
    # just that one specific `socket.send()` lost its peer. server.py sets
    # SIG_IGN on SIGPIPE at module import time so a dropped client
    # surfaces as a normal BrokenPipeError on that one request instead of
    # killing the whole server. If we re-raise SIGPIPE here, we undo that
    # protection and the process dies anyway. So: write the marker, keep
    # the SIG_IGN disposition in place, and return. The offending `send()`
    # in the request thread has already returned EPIPE, so the handler
    # there can clean up normally.
    if signum == signal.SIGPIPE:
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        except Exception:
            pass
        return

    # All other catchable signals: re-raise with the default disposition so
    # the process actually dies after the marker is on disk. SIGKILL is
    # untrappable but if we somehow got here for it, we still want the
    # marker first.
    try:
        signal.signal(signum, signal.SIG_DFL)
    except Exception:
        pass
    try:
        os.kill(os.getpid(), signum)
    except Exception:
        os._exit(128 + signum if signum < 128 else signum)


def _atexit_handler() -> None:
    # Avoid writing if execv already wrote one and a new install() is in flight.
    _write_marker("atexit", {"reason": "normal interpreter shutdown"})


def install() -> bool:
    """Install signal handlers + atexit hook. Returns True on first install,
    False on subsequent calls (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True

    # Catch every signal Python can install a handler for. We deliberately
    # do NOT include SIGKILL/SIGSTOP — they cannot be caught, which is the
    # whole point of this exercise (we want to know when those fire).
    catchable = [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGHUP,
        signal.SIGABRT,
        signal.SIGBUS,
        signal.SIGFPE,
        signal.SIGSEGV,
        signal.SIGPIPE,
        signal.SIGALRM,
        signal.SIGUSR1,
        signal.SIGUSR2,
        signal.SIGQUIT,
    ]
    installed_signals = []
    for sig in catchable:
        try:
            signal.signal(sig, _signal_handler)
            installed_signals.append(sig)
        except (ValueError, OSError):
            # Some signals can only be installed from the main thread, or
            # may not exist on the current platform. Skip silently.
            pass

    atexit.register(_atexit_handler)

    _write_marker(
        "install",
        {
            "message": "Signal trap installed",
            "signals": [s.name for s in installed_signals],
            "shim_dir": SHIM_DIR,
        },
    )
    return True


def wrap_serve_forever(serve_fn, label: str = "serve_forever"):
    """Wrap a serve_forever-like callable with exception capture. Returns a
    wrapper that runs ``serve_fn(*args, **kwargs)`` and writes an
    ``"exception"`` marker on any unhandled exception before re-raising."""

    def wrapped(*args, **kwargs):
        try:
            return serve_fn(*args, **kwargs)
        except KeyboardInterrupt:
            _write_marker(
                "exception",
                {
                    "exception_type": "KeyboardInterrupt",
                    "label": label,
                    "stack": "(no traceback — KeyboardInterrupt)",
                },
            )
            raise
        except SystemExit as e:
            _write_marker(
                "exception",
                {
                    "exception_type": "SystemExit",
                    "exit_code": getattr(e, "code", None),
                    "label": label,
                },
            )
            raise
        except BaseException as e:  # noqa: BLE001 — we WANT to see everything
            _write_marker(
                "exception",
                {
                    "exception_type": type(e).__name__,
                    "exception_message": str(e)[:2000],
                    "label": label,
                    "stack": traceback.format_exc()[:6000],
                },
            )
            raise

    return wrapped
