"""Regression: server.py must ignore SIGPIPE so a client dropping the
connection mid-response cannot kill the whole WebUI process (salvaged from
#3407).

Python's default SIGPIPE action is ``Term``: a single broken-pipe ``send()``
in any ThreadingHTTPServer worker thread would terminate the entire server
silently — no exception, no log, no ``/health`` response. server.py sets
``SIGPIPE`` to ``SIG_IGN`` during startup so the kernel surfaces EPIPE as a
catchable ``BrokenPipeError`` and the server keeps serving.

The handler is guarded with ``getattr(signal, "SIGPIPE", None)`` because
SIGPIPE is POSIX-only and does not exist on Windows (native-Windows support,
#1952) — importing server.py on Windows must not raise.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_sigpipe_set_to_ignore_by_startup_helper():
    """The startup helper installs SIG_IGN on POSIX."""
    if not hasattr(signal, "SIGPIPE"):
        # Windows / no-SIGPIPE platform: nothing to assert, importing server
        # must simply not raise (covered by test_import_does_not_raise below).
        return
    import server
    previous = signal.getsignal(signal.SIGPIPE)
    try:
        server._ignore_sigpipe()
        current = signal.getsignal(signal.SIGPIPE)
        assert current == signal.SIG_IGN, (
            "server.py must set SIGPIPE to SIG_IGN so a dropped client mid-response "
            f"cannot Term the whole process; got handler {current!r}"
        )
    finally:
        signal.signal(signal.SIGPIPE, previous)


def test_import_does_not_raise():
    """Importing server must not raise — proves the getattr guard works on
    any platform (including a hypothetical no-SIGPIPE one)."""
    import server

    assert server is not None


def test_import_does_not_change_sigpipe_handler():
    """Plain import must leave process-global SIGPIPE handling unchanged."""
    if not hasattr(signal, "SIGPIPE"):
        return
    previous = signal.getsignal(signal.SIGPIPE)
    sys.modules.pop("server", None)
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        import server
        assert signal.getsignal(signal.SIGPIPE) == signal.SIG_DFL
        assert server is not None
    finally:
        signal.signal(signal.SIGPIPE, previous)


def test_main_installs_sigpipe_ignore_before_startup():
    """The real startup path must invoke the SIGPIPE helper before serving."""
    if not hasattr(signal, "SIGPIPE"):
        return
    import server

    class _StopStartup(Exception):
        pass

    previous = signal.getsignal(signal.SIGPIPE)
    original = server.install_crash_visibility
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

        def _stop_startup():
            raise _StopStartup()

        server.install_crash_visibility = _stop_startup
        try:
            server.main()
        except _StopStartup:
            pass
        assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN
    finally:
        server.install_crash_visibility = original
        signal.signal(signal.SIGPIPE, previous)


def test_sigpipe_handler_is_getattr_guarded_in_source():
    """The handler must be guarded with getattr(signal, 'SIGPIPE', None) so the
    POSIX-only signal can't AttributeError on Windows (native-Windows support,
    #1952)."""
    src = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    assert 'getattr(signal, "SIGPIPE"' in src or "getattr(signal, 'SIGPIPE'" in src, (
        "server.py must resolve SIGPIPE via getattr so it is Windows-safe; a "
        "bare signal.SIGPIPE reference would AttributeError on native Windows."
    )
    assert "SIG_IGN" in src, "server.py must set the SIGPIPE handler to SIG_IGN"
    assert "def main() -> None:" in src and "_ignore_sigpipe()" in src
