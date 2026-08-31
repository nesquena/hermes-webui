import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

if os.name != "posix":
    pytest.skip("terminal process cleanup tests require POSIX terminal support", allow_module_level=True)

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


class _IdentityHandle:
    def close(self):
        pass


def _fake_identity(proc):
    return terminal.ManagedProcessIdentity(
        pid=proc.pid,
        handle=_IdentityHandle(),
        backend="test",
    )


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


def test_stop_managed_terminal_escalates_owned_group_and_drains_reader(monkeypatch):
    class EscalatedProc(_FakeProc):
        pid = 771_771

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            return -9

    class Reader:
        def __init__(self):
            self.joins = []

        def join(self, timeout=None):
            self.joins.append(timeout)

        def is_alive(self):
            return False

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    proc = EscalatedProc()
    reader = Reader()
    term = terminal.TerminalSession(
        session_id="managed-stop",
        workspace="/tmp",
        proc=proc,
        master_fd=read_fd,
    )
    term.kind = "claude_code"
    term.handle = term.session_id
    term.generation = "5f47cc5d-ec48-4ed8-973b-5972f115c9cc"
    term.pgid = proc.pid
    term.owned_pgid_verified = True
    term.leader_identity = _fake_identity(proc)
    term.persistent_when_unwatched = True
    term.reader = reader
    term.reader_started = True
    terminal._TERMINALS[term.handle] = term
    monkeypatch.setattr(
        terminal,
        "_managed_leader_is_unreaped",
        lambda _proc, _identity: True,
    )
    monkeypatch.setattr(
        terminal,
        "_wait_for_verified_process_group_exit",
        lambda _proc, _pgid, _identity, _timeout: False,
    )
    kills = []
    monkeypatch.setattr(
        terminal.os,
        "killpg",
        lambda pgid, sig: kills.append((pgid, sig)) if sig else None,
    )
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "stop"
    )

    assert terminal.stop_managed_terminal(
        handle=term.handle,
        generation=term.generation,
        capability=capability,
    )

    assert [sig for _pgid, sig in kills] == [
        signal.SIGHUP,
        signal.SIGTERM,
        signal.SIGKILL,
    ]
    assert {pgid for pgid, _sig in kills} == {term.pgid}
    assert reader.joins
    with pytest.raises(OSError):
        os.fstat(read_fd)


def test_managed_teardown_never_signals_unverified_process_group(monkeypatch):
    proc = _FakeProc()
    term = terminal.TerminalSession(
        session_id="managed-foreign-group",
        workspace="/tmp",
        proc=proc,
        master_fd=12345,
    )
    term.kind = "claude_code"
    term.pgid = proc.pid
    monkeypatch.setattr(terminal.os, "getpgid", lambda pid: proc.pid + 1)
    signals = []
    monkeypatch.setattr(
        terminal.os, "killpg", lambda pgid, sig: signals.append((pgid, sig))
    )
    monkeypatch.setattr(terminal.os, "close", lambda fd: None)

    terminal._teardown_terminal(term)

    assert signals == []


def test_owned_group_check_survives_unreaped_leader_exit(
    monkeypatch,
):
    class RacingProc:
        pid = 717_717
        returncode = None

    proc = RacingProc()
    term = terminal.TerminalSession(
        session_id="managed-racing-leader",
        workspace="/tmp",
        proc=proc,
        master_fd=-1,
        kind="claude_code",
        pgid=proc.pid,
        owned_pgid_verified=True,
        leader_identity=_fake_identity(proc),
    )

    signals = []
    monkeypatch.setattr(
        terminal.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)) if sig else None,
    )

    assert terminal._signal_owned_group(term, signal.SIGTERM) is True
    assert signals == [(proc.pid, signal.SIGTERM)]


def test_owned_group_check_never_signals_after_leader_identity_was_reaped(
    monkeypatch,
):
    class ReapedProc:
        pid = 717_718
        returncode = 0

        def poll(self):
            raise AssertionError("managed ownership checks must not poll/reap")

    proc = ReapedProc()
    term = terminal.TerminalSession(
        session_id="managed-reused-pgid",
        workspace="/tmp",
        proc=proc,
        master_fd=-1,
        kind="claude_code",
        pgid=proc.pid,
        owned_pgid_verified=True,
        leader_identity=_fake_identity(proc),
    )
    signals = []
    monkeypatch.setattr(
        terminal.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    assert terminal._signal_owned_group(term, signal.SIGTERM) is False
    assert signals == []


def _process_group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return False
    except ProcessLookupError:
        return False


def _wait_for_path(path: Path, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_unreaped_exit(identity, timeout=3):
    assert identity.wait_for_exit(timeout), "timed out waiting for unreaped process exit"


def test_stop_continues_after_leader_exit_until_owned_group_is_extinct(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import os,signal,time; from pathlib import Path; "
        "child=os.fork(); "
        "(os._exit(0) if child else None); "
        "signal.signal(signal.SIGHUP,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    master_fd, slave_fd = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        start_new_session=True,
    )
    identity = terminal._capture_managed_process_identity(proc)
    assert identity is not None
    os.close(slave_fd)
    try:
        _wait_for_path(child_pid_path)
        _wait_for_unreaped_exit(identity)
        assert _process_group_exists(proc.pid)
        term = terminal.TerminalSession(
            session_id="managed-real-group",
            workspace=str(tmp_path),
            proc=proc,
            master_fd=master_fd,
            kind="claude_code",
            generation="114ab946-af08-4ba0-8713-a15118716ad1",
            handle="managed-real-group",
            pgid=proc.pid,
            persistent_when_unwatched=True,
        )
        term.owned_pgid_verified = True
        term.leader_identity = identity
        terminal._TERMINALS[term.handle] = term
        capability = terminal.issue_terminal_capability(
            term.handle, term.generation, "stop"
        )

        assert terminal.stop_managed_terminal(
            handle=term.handle,
            generation=term.generation,
            capability=capability,
        )

        deadline = time.monotonic() + 2
        while _process_group_exists(proc.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _process_group_exists(proc.pid)
    finally:
        if proc.returncode is None and _process_group_exists(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_reader_drains_final_child_output_after_leader_exit(tmp_path):
    child_pid_path = tmp_path / "writer.pid"
    release_path = tmp_path / "release"
    # A small script file keeps the child wait finite and makes the final write
    # happen only after the reader starts with an already-dead group leader.
    script = tmp_path / "final_writer.py"
    script.write_text(
        "import os,time\n"
        "from pathlib import Path\n"
        "child=os.fork()\n"
        "if child:\n"
        "    os._exit(0)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
        f"release=Path({str(release_path)!r})\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)\n"
        "os.write(1,b'final-write\\n')\n",
        encoding="utf-8",
    )
    master_fd, slave_fd = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        start_new_session=True,
    )
    identity = terminal._capture_managed_process_identity(proc)
    assert identity is not None
    os.close(slave_fd)
    try:
        _wait_for_path(child_pid_path)
        _wait_for_unreaped_exit(identity)
        terminal._set_nonblocking(master_fd)
        term = terminal.TerminalSession(
            session_id="managed-final-output",
            workspace=str(tmp_path),
            proc=proc,
            master_fd=master_fd,
            kind="claude_code",
            generation="5fece16c-88c8-4c93-bc4f-271850f63589",
            handle="managed-final-output",
            pgid=proc.pid,
            persistent_when_unwatched=True,
        )
        term.owned_pgid_verified = True
        term.leader_identity = identity
        output = term.subscribe(generation=term.generation)
        term.reader = threading.Thread(target=terminal._reader_loop, args=(term,))
        term.reader.start()
        release_path.touch()
        term.reader.join(timeout=3)

        assert not term.reader.is_alive()
        texts = [
            payload["text"]
            for _seq, event, payload in list(output.queue)
            if event == "output"
        ]
        assert "final-write" in "".join(texts)
    finally:
        if proc.returncode is None and _process_group_exists(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            os.close(master_fd)
        except OSError:
            pass


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
