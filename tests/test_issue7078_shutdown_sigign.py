"""Regression tests for #7078: 'Stop server' no-ops on ctl.sh-managed daemons.

ctl.sh spawns the daemon from a non-interactive bash background job
(`( cd ...; exec nohup python bootstrap.py ... ) >> log 2>&1 &`), so the
child inherits SIGINT = SIG_IGN (bash: "asynchronous commands ignore SIGINT
and SIGQUIT in addition to SIGHUP"). The Settings "Stop server" button (POST
/api/shutdown) signals the process with SIGINT via `os.kill(os.getpid(),
SIGINT)`; with the inherited ignore disposition that signal is a silent
OS-level no-op and the process keeps running (200 {"status":
"shutting_down"} but no shutdown). server.py must explicitly install its
SIGINT handler, overriding the inherited SIG_IGN, so the button, external
`kill -INT`, and foreground Ctrl-C all take the same graceful path through
the serve_forever() finally block.
"""
import os
import signal
import subprocess
import sys
import textwrap
import threading
import types

import pytest

if os.name != "posix":
    pytest.skip("SIGINT/SIG_IGN background-job semantics require POSIX", allow_module_level=True)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_child_with_sigint_ignored(script: str, timeout: float = 15.0):
    """Run `script` in a child that inherited SIGINT = SIG_IGN (ctl.sh-style).

    Signal dispositions survive fork/exec, and CPython does not reinstall its
    KeyboardInterrupt handler when SIGINT was ignored at interpreter startup,
    so the child reproduces the exact ctl.sh daemon condition.
    """
    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        signal.signal(signal.SIGINT, prev)


# Mirrors server.py main(): installs the shutdown signal handlers, then runs
# serve_forever() on the main thread. A helper fires the signal under test;
# the handler's helper thread calls httpd.shutdown(), serve_forever() returns
# and the finally block runs (the drain path).
_CHILD_SCRIPT = textwrap.dedent(
    f"""
    import os, signal, sys, threading, time
    sys.path.insert(0, {REPO_ROOT!r})
    import server

    shutdown_requested = threading.Event()
    released = threading.Event()

    class FakeHTTPD:
        def serve_forever(self):
            while not released.is_set():
                time.sleep(0.02)

        def shutdown(self):
            released.set()

        def server_close(self):
            pass

    httpd = FakeHTTPD()

    # Precondition: the ctl.sh spawn model leaves SIGINT ignored at the OS
    # level (bash background job with job control off, CPython preserving the
    # inherited SIG_IGN). If this check fails the test is not simulating the
    # bug condition, so fail loudly instead of passing vacuously.
    if signal.getsignal(signal.SIGINT) is not signal.SIG_IGN:
        print("SIGINT_NOT_INHERITED_IGNORED")
        sys.exit(3)

    server._install_shutdown_signal_handlers(httpd, shutdown_requested)

    # The fix must override that with the graceful-shutdown handler.
    if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        print("SIGINT_STILL_IGNORED")
        sys.exit(2)

    threading.Timer(0.4, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    try:
        httpd.serve_forever()
        print("SERVE_FOREVER_RETURNED")
    finally:
        print("FINALLY_RAN")
    """
)


def test_sigint_shutdown_works_when_sigint_inherited_ignored():
    """POST /api/shutdown's SIGINT must stop a ctl.sh-style daemon (SIG_IGN)."""
    proc = _run_child_with_sigint_ignored(_CHILD_SCRIPT)
    assert proc.returncode == 0, f"child failed:\n{proc.stdout}\n{proc.stderr}"
    assert "SIGINT_STILL_IGNORED" not in proc.stdout, proc.stdout
    assert "SERVE_FOREVER_RETURNED" in proc.stdout, proc.stdout
    assert "FINALLY_RAN" in proc.stdout, proc.stdout


def test_sigterm_shutdown_still_works_when_sigint_inherited_ignored():
    """SIGTERM (ctl.sh stop path) keeps taking the graceful path too."""
    script = _CHILD_SCRIPT.replace(
        "signal.SIGINT)).start()", "signal.SIGTERM)).start()"
    )
    proc = _run_child_with_sigint_ignored(script)
    assert proc.returncode == 0, f"child failed:\n{proc.stdout}\n{proc.stderr}"
    assert "SERVE_FOREVER_RETURNED" in proc.stdout, proc.stdout
    assert "FINALLY_RAN" in proc.stdout, proc.stdout


def test_install_shutdown_signal_handlers_wires_sigterm_and_sigint(monkeypatch):
    """Both signals are wired to one idempotent graceful-shutdown handler."""
    import server

    registered = {}

    def _fake_signal(sig, handler):
        registered[sig] = handler

    monkeypatch.setattr(signal, "signal", _fake_signal)

    started = []

    class FakeThread:
        def __init__(self, target, name=None, daemon=False):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            started.append(self)

    monkeypatch.setattr(threading, "Thread", FakeThread)

    httpd = types.SimpleNamespace(shutdown=lambda: None)
    requested = threading.Event()
    server._install_shutdown_signal_handlers(httpd, requested)

    assert signal.SIGTERM in registered, registered
    assert signal.SIGINT in registered, registered
    assert registered[signal.SIGTERM] is registered[signal.SIGINT]

    handler = registered[signal.SIGINT]
    handler(None, None)  # first request -> spawns the shutdown helper thread
    handler(None, None)  # second request -> idempotent guard ignores it
    assert len(started) == 1, started
    assert started[0].name == "webui-sigterm-shutdown"
    assert started[0].daemon is True
