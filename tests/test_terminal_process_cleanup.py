import os
import subprocess
import threading
import time

import pytest

import api.terminal as terminal


class _DummyThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


class _FakeProc:
    pid = 999_999_999

    def __init__(self):
        self.wait_calls = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


def test_terminal_shell_does_not_use_pdeathsig_preexec(monkeypatch, tmp_path):
    """Regression for #2853.

    The previous implementation passed a ``preexec_fn`` that called
    ``prctl(PR_SET_PDEATHSIG, SIGTERM)``.  Because that signal is *per-thread*
    and WebUI's ``ThreadingHTTPServer`` spawns a new thread for every HTTP
    request, the PTY shell registered the request-handler thread as its
    parent and was killed within ~10 ms of being created on Linux.

    The fix is to spawn the shell without ``preexec_fn`` at all.  Graceful
    shutdown remains covered by ``atexit.register(close_all_terminals)`` and
    the explicit ``close_terminal`` paths.
    """
    captured = {}
    proc = _FakeProc()

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(terminal.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(terminal.threading, "Thread", _DummyThread)
    monkeypatch.setattr(terminal, "_set_size", lambda *args, **kwargs: None)

    term = terminal.start_terminal("term-no-preexec", tmp_path)

    try:
        assert term.proc is proc
        assert "preexec_fn" not in captured["kwargs"], (
            "preexec_fn must not be set — the PR_SET_PDEATHSIG implementation "
            "killed every Linux user's terminal (#2853). See module-level note."
        )
        assert captured["kwargs"]["start_new_session"] is True
        assert captured["kwargs"]["stdin"] == captured["kwargs"]["stdout"] == captured["kwargs"]["stderr"]
    finally:
        terminal.close_terminal("term-no-preexec")


@pytest.mark.skipif(
    not hasattr(os, "openpty") or os.name != "posix",
    reason="PTY-spawn test requires a POSIX host",
)
def test_pty_shell_survives_when_spawning_thread_exits(tmp_path):
    """End-to-end regression for #2853.

    Spawn a real PTY shell via ``start_terminal`` from inside a worker thread
    that then exits.  The shell must remain alive after the spawning thread
    joins, otherwise we've regressed back to the PR_SET_PDEATHSIG behaviour
    that killed every Linux user's embedded terminal.
    """
    sid = "term-thread-survival"
    holder: dict = {}

    def worker():
        try:
            holder["term"] = terminal.start_terminal(sid, tmp_path)
        except Exception as exc:  # pragma: no cover - surface in assertion
            holder["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "spawn worker thread should have exited"
    assert "error" not in holder, holder.get("error")
    term = holder["term"]

    try:
        # Give the kernel a beat — if PR_SET_PDEATHSIG were re-introduced the
        # shell would receive SIGTERM right about now.
        time.sleep(0.5)
        assert term.proc.poll() is None, (
            "PTY shell exited after the spawning thread joined — likely a "
            "PR_SET_PDEATHSIG regression (#2853). "
            f"exit_code={term.proc.poll()!r}"
        )
    finally:
        terminal.close_terminal(sid)


def test_close_terminal_waits_again_after_sigkill(monkeypatch):
    class TimeoutThenReapedProc(_FakeProc):
        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired(cmd="shell", timeout=timeout)
            return -9

    proc = TimeoutThenReapedProc()
    term = terminal.TerminalSession(
        session_id="term-timeout",
        workspace="/tmp",
        proc=proc,
        master_fd=12345,
    )
    terminal._TERMINALS["term-timeout"] = term
    kills = []
    monkeypatch.setattr(terminal.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(terminal.os, "close", lambda fd: None)

    assert terminal.close_terminal("term-timeout") is True

    assert proc.wait_calls == [1.5, 1.0]
    assert kills == [(proc.pid, terminal.signal.SIGHUP), (proc.pid, terminal.signal.SIGKILL)]


def test_close_all_terminals_closes_snapshot(monkeypatch):
    terminal._TERMINALS.clear()
    terminal._TERMINALS.update({"a": object(), "b": object()})
    closed = []

    def fake_close(session_id):
        closed.append(session_id)
        terminal._TERMINALS.pop(session_id, None)
        return True

    monkeypatch.setattr(terminal, "close_terminal", fake_close)

    terminal.close_all_terminals()

    assert closed == ["a", "b"]
    assert terminal._TERMINALS == {}


def test_terminal_module_registers_graceful_shutdown_reaper():
    """atexit is still the reap path; pdeathsig must NOT be re-introduced."""
    src = terminal.Path(terminal.__file__).read_text()

    assert "atexit.register(close_all_terminals)" in src
    # The PR_SET_PDEATHSIG implementation broke every Linux user (#2853);
    # guard against accidentally bringing it back.
    assert "preexec_fn=_terminal_shell_preexec_fn" not in src
    assert "libc.prctl(1, signal.SIGTERM)" not in src
