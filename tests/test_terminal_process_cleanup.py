import subprocess

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


def test_terminal_shell_uses_parent_death_signal_preexec(monkeypatch, tmp_path):
    captured = {}
    proc = _FakeProc()

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(terminal.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(terminal.threading, "Thread", _DummyThread)
    monkeypatch.setattr(terminal, "_set_size", lambda *args, **kwargs: None)

    term = terminal.start_terminal("term-preexec", tmp_path)

    try:
        assert term.proc is proc
        assert captured["kwargs"]["preexec_fn"] is terminal._terminal_shell_preexec_fn
        assert captured["kwargs"]["start_new_session"] is True
        assert captured["kwargs"]["stdin"] == captured["kwargs"]["stdout"] == captured["kwargs"]["stderr"]
    finally:
        terminal.close_terminal("term-preexec")


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
    src = terminal.Path(terminal.__file__).read_text()

    assert "atexit.register(close_all_terminals)" in src
    assert "preexec_fn=_terminal_shell_preexec_fn" in src
    assert "libc.prctl(1, signal.SIGTERM)" in src
