"""Regression coverage for issue #4633: crash visibility hardening.

server.py was exiting SILENTLY after 9-16h — no traceback, no shutdown-audit
line, no core dump; the log just stopped mid-request. It runs a
ThreadingHTTPServer with daemon_threads=True, so an unhandled exception in a
request/SSE handler thread could terminate work with nothing recorded, and
faulthandler was not enabled so a native crash left nothing at all.

These tests assert the diagnostic hardening in api/crash_visibility.py (wired
into server.main()):

  * faulthandler is enabled after install (native crash → C-level traceback);
  * threading.excepthook is installed and a real daemon-thread exception is
    logged (thread name + traceback) instead of vanishing;
  * sys.excepthook is installed and logs uncaught main-thread exceptions;
  * an atexit exit-audit breadcrumb is registered;
  * server.py wires install_crash_visibility() into main().

The paired MEMORY root-cause is fixed separately in #4765; this is purely
crash VISIBILITY.

No live server socket is required — the helpers are exercised directly.
"""
import faulthandler
import logging
import sys
import threading

import pytest

import api.crash_visibility as cv


@pytest.fixture
def fresh_hooks(tmp_path):
    """Reset crash-visibility global state and restore process hooks after.

    Installing crash visibility mutates the process-wide threading.excepthook,
    sys.excepthook and faulthandler state, so snapshot and restore them to keep
    the rest of the suite hermetic.
    """
    saved_thread_hook = threading.excepthook
    saved_sys_hook = sys.excepthook
    saved_installed = cv._INSTALLED
    saved_stream = cv._FAULT_STREAM
    saved_prev_thread = cv._PREV_THREAD_HOOK
    saved_prev_sys = cv._PREV_SYS_HOOK
    was_fh_enabled = faulthandler.is_enabled()
    saved_atexit_register = cv.atexit.register

    # Start from stock hooks so the module records the *stock* hook as "prev"
    # and never chains into pytest's own thread-exception collector during the
    # test (which would otherwise double-report and can fail the test).
    threading.excepthook = threading.__excepthook__
    sys.excepthook = sys.__excepthook__
    cv._INSTALLED = False
    cv._FAULT_STREAM = None
    cv._PREV_THREAD_HOOK = None
    cv._PREV_SYS_HOOK = None
    # Prevent tests from leaking real atexit handlers into the pytest session:
    # the fixture resets _INSTALLED each test, so every install() would register
    # a fresh (real) _exit_audit that fires at interpreter shutdown. Capture
    # them instead. The dedicated exit-audit test re-patches this itself.
    cv.atexit.register = lambda fn: fn

    stream_path = tmp_path / "faultstream.log"
    stream = stream_path.open("w", encoding="utf-8")
    try:
        yield stream, stream_path
    finally:
        try:
            stream.flush()
        except Exception:
            pass
        # Detach faulthandler from the about-to-close temp file before closing.
        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        if was_fh_enabled:
            try:
                faulthandler.enable()
            except Exception:
                pass
        threading.excepthook = saved_thread_hook
        sys.excepthook = saved_sys_hook
        cv._INSTALLED = saved_installed
        cv._FAULT_STREAM = saved_stream
        cv._PREV_THREAD_HOOK = saved_prev_thread
        cv._PREV_SYS_HOOK = saved_prev_sys
        cv.atexit.register = saved_atexit_register


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


# ── 1. faulthandler ─────────────────────────────────────────────────────────
def test_faulthandler_enabled_after_install(fresh_hooks):
    stream, _ = fresh_hooks

    installed = cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)

    assert installed is True
    assert cv.is_installed() is True
    # faulthandler must be active so a native segfault/abort dumps a C-level
    # traceback of every thread instead of vanishing (the #4633 failure mode).
    assert faulthandler.is_enabled() is True


def test_install_is_idempotent(fresh_hooks):
    stream, _ = fresh_hooks
    assert cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False) is True
    # Second call must be a no-op (no stacked hooks / duplicate atexit handlers).
    assert cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False) is False


# ── 2. threading.excepthook ─────────────────────────────────────────────────
def test_thread_excepthook_installed(fresh_hooks):
    stream, _ = fresh_hooks
    cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)
    assert threading.excepthook is cv.thread_excepthook


def test_daemon_thread_exception_is_logged(fresh_hooks, caplog):
    stream, stream_path = fresh_hooks
    cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)

    def _boom():
        raise ValueError("simulated SSE handler crash 4633")

    caplog.set_level(logging.ERROR, logger="server")

    t = threading.Thread(target=_boom, name="sse-handler-42", daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()

    stream.flush()
    written = _read(stream_path)
    logged = "\n".join(r.getMessage() for r in caplog.records)

    # Diagnostic must land in the fault stream (which the bootstrap redirects
    # into the WebUI log file) AND be visible via the logging subsystem.
    for haystack in (written, logged):
        assert "[crash-visibility] uncaught exception in thread" in haystack
        assert "sse-handler-42" in haystack
    # The stream copy carries the full traceback + the original exception text.
    assert "ValueError" in written
    assert "simulated SSE handler crash 4633" in written


def test_thread_excepthook_ignores_clean_exit_and_never_raises(fresh_hooks):
    stream, stream_path = fresh_hooks
    cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)

    # A normal thread teardown passes exc_type=None; must produce nothing and
    # must not raise.
    Args = type(
        "Args",
        (),
        {"exc_type": None, "exc_value": None, "exc_traceback": None, "thread": None},
    )
    cv.thread_excepthook(Args())

    # Wholly malformed args must also be swallowed without raising.
    cv.thread_excepthook(object())

    stream.flush()
    assert "uncaught exception in thread" not in _read(stream_path)


# ── 3. sys.excepthook ───────────────────────────────────────────────────────
def test_sys_excepthook_installed(fresh_hooks):
    stream, _ = fresh_hooks
    cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)
    assert sys.excepthook is cv.main_excepthook


def test_main_thread_exception_is_logged(fresh_hooks, caplog):
    stream, stream_path = fresh_hooks
    cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)

    caplog.set_level(logging.CRITICAL, logger="server")
    try:
        raise RuntimeError("simulated main-thread fatal 4633")
    except RuntimeError:
        cv.main_excepthook(*sys.exc_info())

    stream.flush()
    written = _read(stream_path)
    logged = "\n".join(r.getMessage() for r in caplog.records)

    for haystack in (written, logged):
        assert "[crash-visibility] uncaught exception on main thread" in haystack
    assert "RuntimeError" in written
    assert "simulated main-thread fatal 4633" in written


def test_keyboard_interrupt_not_treated_as_crash(fresh_hooks, caplog):
    stream, stream_path = fresh_hooks
    cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)

    caplog.set_level(logging.DEBUG, logger="server")
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        cv.main_excepthook(*sys.exc_info())

    stream.flush()
    # Ctrl-C is intentional; it must not be logged as an uncaught crash.
    assert "uncaught exception on main thread" not in _read(stream_path)
    assert all("crash-visibility" not in r.getMessage() for r in caplog.records)


# ── 4. exit audit ───────────────────────────────────────────────────────────
def test_exit_audit_registered_and_emits(fresh_hooks, monkeypatch, caplog):
    stream, stream_path = fresh_hooks

    registered = []
    monkeypatch.setattr(cv.atexit, "register", lambda fn: registered.append(fn) or fn)

    cv.install_crash_visibility(stream=stream, register_sigusr1_dump=False)
    assert cv._exit_audit in registered

    caplog.set_level(logging.INFO, logger="server")
    cv._exit_audit()  # simulate interpreter exit unwinding

    stream.flush()
    written = _read(stream_path)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for haystack in (written, logged):
        assert "[crash-visibility] process exit" in haystack


# ── 5. server.py wiring ─────────────────────────────────────────────────────
def test_server_imports_and_calls_install(fresh_hooks):
    import server

    assert hasattr(server, "install_crash_visibility")
    assert server.install_crash_visibility is cv.install_crash_visibility

    from tests.conftest import SERVER_SCRIPT

    src = SERVER_SCRIPT.read_text(encoding="utf-8")
    assert "from api.crash_visibility import install_crash_visibility" in src
    assert "install_crash_visibility()" in src
