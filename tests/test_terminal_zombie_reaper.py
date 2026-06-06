import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt"
    or not sys.platform.startswith("linux")
    or not getattr(__import__("api.terminal", fromlist=["_TERMINAL_SUPPORTED"]), "_TERMINAL_SUPPORTED", False),
    reason="Linux-only terminal zombie reaper coverage",
)

import api.terminal as terminal


def _wait_until_waitable(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is not None and result.si_pid == pid:
            return
        time.sleep(0.01)
    raise AssertionError(f"child {pid} did not exit before timeout")


def test_reap_terminal_descendants_reaps_exited_child():
    ready_r, ready_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.setpgid(0, 0)
        os.write(ready_w, b"1")
        os.close(ready_w)
        os._exit(0)

    reaped = False
    try:
        os.close(ready_w)
        assert os.read(ready_r, 1) == b"1"
        os.close(ready_r)
        _wait_until_waitable(pid)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            terminal._reap_terminal_descendants(pid)
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                reaped = True
                break
            time.sleep(0.01)

        assert reaped, "terminal descendant reaper did not reap the exited child"
    finally:
        if not reaped:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def test_close_terminal_reaps_descendants_after_shell_wait(monkeypatch):
    class FakeProc:
        pid = 987654

        def __init__(self):
            self.wait_calls = []
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = -1
            return self.returncode

    proc = FakeProc()
    term = terminal.TerminalSession(
        session_id="term-descendant-reap",
        workspace="/tmp",
        proc=proc,
        master_fd=12345,
    )
    terminal._TERMINALS["term-descendant-reap"] = term
    kills = []
    reaped = []

    monkeypatch.setattr(terminal.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(terminal.os, "close", lambda fd: None)
    monkeypatch.setattr(terminal, "_reap_terminal_descendants", lambda pgid: reaped.append(pgid) or 0)

    assert terminal.close_terminal("term-descendant-reap") is True

    assert kills == [(proc.pid, terminal.signal.SIGHUP)]
    assert proc.wait_calls == [1.5]
    assert reaped == [proc.pid]


def test_reap_terminal_descendants_ignores_expected_waitpid_errors(monkeypatch):
    calls = []

    def fake_waitpid(pid, flags):
        calls.append((pid, flags))
        raise ChildProcessError()

    monkeypatch.setattr(terminal.os, "waitpid", fake_waitpid)

    assert terminal._reap_terminal_descendants(123) == 0
    assert calls == [(-123, os.WNOHANG)]


def test_reap_terminal_descendants_is_bounded(monkeypatch):
    calls = []

    def fake_waitpid(pid, flags):
        calls.append((pid, flags))
        return (len(calls), 0)

    monkeypatch.setattr(terminal.os, "waitpid", fake_waitpid)

    assert terminal._reap_terminal_descendants(123, limit=3) == 3
    assert calls == [(-123, os.WNOHANG), (-123, os.WNOHANG), (-123, os.WNOHANG)]
