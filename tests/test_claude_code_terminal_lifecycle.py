import base64
import fcntl
import os
import queue
import sys
import threading
from pathlib import Path
from uuid import UUID

import pytest

if os.name != "posix":
    pytest.skip("managed terminal tests require POSIX", allow_module_level=True)

import api.terminal as terminal


PUBLIC_SESSION_ID = "claude_code_public_row"


class _DummyThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started

    def join(self, timeout=None):
        return None


class _ReadyProc:
    pid = 424_242

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class _IdentityHandle:
    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    with terminal._LOCK:
        terminal._TERMINALS.clear()
    monkeypatch.setattr(terminal.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        terminal,
        "_capture_managed_process_identity",
        lambda proc: terminal.ManagedProcessIdentity(
            pid=proc.pid,
            handle=_IdentityHandle(),
            backend="test",
        ),
    )
    yield
    with terminal._LOCK:
        terms = list(terminal._TERMINALS.values())
        terminal._TERMINALS.clear()
    for term in terms:
        try:
            os.close(term.master_fd)
        except OSError:
            pass


def _start(monkeypatch, tmp_path, *, state="ready", public_id=PUBLIC_SESSION_ID):
    captured = {}
    proc = _ReadyProc()

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        readiness_fd = kwargs["pass_fds"][0]
        os.write(readiness_fd, f'{{"state":"{state}"}}\n'.encode())
        return proc

    monkeypatch.setattr(terminal.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(terminal.threading, "Thread", _DummyThread)
    monkeypatch.setattr(terminal.os, "getpgid", lambda pid: pid)
    term = terminal.start_managed_terminal(public_id, tmp_path)
    return term, captured, proc


def _drain(output):
    items = []
    while True:
        try:
            items.append(output.get_nowait())
        except queue.Empty:
            return items


def test_managed_readiness_accepts_valid_launch_near_probe_budget(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"state":"ready"}\n')
    os.close(write_fd)
    clock = [100.0]
    valid_launch_seconds = 11.0

    monkeypatch.setattr(terminal.time, "monotonic", lambda: clock[0])

    def delayed_select(readers, _writers, _errors, timeout):
        if timeout < valid_launch_seconds:
            clock[0] += timeout
            return [], [], []
        clock[0] += valid_launch_seconds
        return readers, [], []

    monkeypatch.setattr(terminal.select, "select", delayed_select)

    assert terminal._read_managed_runner_readiness(read_fd) == "ready"


def test_managed_terminal_spawns_task2_runner_as_exact_argv_without_shell(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SECRET_API_KEY", "must-not-leak")
    registry = tmp_path / "stores.json"
    registry.write_text("[]")
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", str(registry))

    term, captured, proc = _start(monkeypatch, tmp_path)

    kwargs = captured["kwargs"]
    expected_prefix = (
        sys.executable,
        str(Path(terminal.__file__).with_name("claude_code_runner.py").resolve()),
        PUBLIC_SESSION_ID,
    )
    assert captured["args"] == ()
    assert tuple(kwargs["args"][:3]) == expected_prefix
    assert kwargs["args"][3] == str(kwargs["pass_fds"][0])
    assert "shell" not in kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert kwargs["env"]["HERMES_WEBUI_CLAUDE_STORES_FILE"] == str(registry)
    assert "SECRET_API_KEY" not in kwargs["env"]

    assert term.argv == tuple(kwargs["args"])
    assert term.workspace == str(tmp_path.resolve())
    assert term.proc is proc
    assert term.pgid == proc.pid
    assert term.kind == "claude_code"
    assert term.persistent_when_unwatched is True
    assert term.runner_owns_lease is True
    assert not hasattr(term, "lease_fd")
    assert term.handle != PUBLIC_SESSION_ID
    assert len(base64.urlsafe_b64decode(term.handle + "=")) == 32
    assert str(UUID(term.generation)) == term.generation


def test_public_session_id_cannot_drive_managed_terminal(monkeypatch, tmp_path):
    term, _captured, _proc = _start(monkeypatch, tmp_path)
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "input"
    )

    with pytest.raises(KeyError):
        terminal.write_managed_terminal(
            handle=PUBLIC_SESSION_ID,
            generation=term.generation,
            capability=capability,
            data="x",
        )
    with pytest.raises(KeyError):
        terminal.write_terminal(term.handle, "x")
    assert terminal.attach_terminal(term.handle) is None
    assert terminal.close_terminal(term.handle) is False
    with pytest.raises(KeyError):
        terminal.start_terminal(term.handle, tmp_path)
    with pytest.raises(KeyError):
        terminal.start_terminal(term.handle, tmp_path, restart=True)


def test_capabilities_are_hashed_expiring_and_operation_scoped(
    monkeypatch, tmp_path
):
    now = 10_000.0
    monotonic = [now]
    wall = [now]
    monkeypatch.setattr(terminal.time, "monotonic", lambda: monotonic[0])
    monkeypatch.setattr(terminal.time, "time", lambda: wall[0])
    term, _captured, _proc = _start(monkeypatch, tmp_path)

    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "input"
    )

    assert capability not in repr(term._capabilities)
    assert all(len(digest) == 64 for digest in term._capabilities)
    with pytest.raises(KeyError):
        terminal.attach_managed_terminal(
            handle=term.handle,
            generation=term.generation,
            capability=capability,
        )

    writes = []
    monkeypatch.setattr(terminal.os, "write", lambda fd, data: writes.append(data))
    wall[0] += terminal._MANAGED_CAPABILITY_TTL_SECONDS * 10
    terminal.write_managed_terminal(
        handle=term.handle,
        generation=term.generation,
        capability=capability,
        data="still-valid",
    )

    monotonic[0] += terminal._MANAGED_CAPABILITY_TTL_SECONDS + 1
    with pytest.raises(KeyError):
        terminal.write_managed_terminal(
            handle=term.handle,
            generation=term.generation,
            capability=capability,
            data="x",
        )
    assert writes == [b"still-valid"]


def test_stale_stream_generation_receives_reset_through_authorised_attach(
    monkeypatch, tmp_path
):
    term, _captured, _proc = _start(monkeypatch, tmp_path)
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "stream"
    )
    signals = []
    monkeypatch.setattr(
        terminal.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)) if sig else None,
    )

    attached, output = terminal.attach_managed_terminal(
        handle=term.handle,
        generation=str(UUID(int=0)),
        capability=capability,
        after_seq=0,
    )

    assert attached is term
    assert [event for _seq, event, _payload in _drain(output)] == [
        "terminal_reset"
    ]
    assert signals == [(term.pgid, terminal.signal.SIGWINCH)]
    with pytest.raises(KeyError, match="terminal not found"):
        terminal.attach_managed_terminal(
            handle="retired-handle",
            generation=str(UUID(int=0)),
            capability=capability,
        )


def test_stale_generation_is_indistinguishable_from_bad_capability(
    monkeypatch, tmp_path
):
    term, _captured, _proc = _start(monkeypatch, tmp_path)
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "input"
    )

    with pytest.raises(KeyError, match="terminal not found"):
        terminal.write_managed_terminal(
            handle=term.handle,
            generation=str(UUID(int=0)),
            capability=capability,
            data="x",
        )
    with pytest.raises(KeyError, match="terminal not found"):
        terminal.write_managed_terminal(
            handle=term.handle,
            generation=term.generation,
            capability="wrong",
            data="x",
        )


def test_valid_input_capability_writes_to_owned_pty(monkeypatch, tmp_path):
    term, _captured, _proc = _start(monkeypatch, tmp_path)
    capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "input"
    )
    writes = []
    monkeypatch.setattr(terminal.os, "write", lambda fd, data: writes.append((fd, data)))

    terminal.write_managed_terminal(
        handle=term.handle,
        generation=term.generation,
        capability=capability,
        data="hello",
    )

    assert writes == [(term.master_fd, b"hello")]


def test_third_managed_terminal_is_rejected_without_eviction(monkeypatch, tmp_path):
    spawned = []

    def fake_popen(*args, **kwargs):
        proc = _ReadyProc()
        proc.pid += len(spawned)
        spawned.append(proc)
        os.write(kwargs["pass_fds"][0], b'{"state":"ready"}\n')
        return proc

    monkeypatch.setattr(terminal.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(terminal.threading, "Thread", _DummyThread)
    monkeypatch.setattr(terminal.os, "getpgid", lambda pid: pid)

    first = terminal.start_managed_terminal("public-one", tmp_path)
    second = terminal.start_managed_terminal("public-two", tmp_path)
    with pytest.raises(terminal.ManagedTerminalLimitError):
        terminal.start_managed_terminal("public-three", tmp_path)

    assert len(spawned) == 2
    assert terminal._TERMINALS[first.handle] is first
    assert terminal._TERMINALS[second.handle] is second


def test_managed_spawn_and_readiness_do_not_hold_global_registry_lock(
    monkeypatch, tmp_path
):
    real_thread = threading.Thread
    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    proc = _ReadyProc()

    def blocking_spawn(*, argv, cwd, env, slave_fd, pass_fds):
        entered_spawn.set()
        assert release_spawn.wait(timeout=5)
        os.write(pass_fds[0], b'{"state":"ready"}\n')
        return proc

    monkeypatch.setattr(terminal, "_spawn_pty_process", blocking_spawn)
    monkeypatch.setattr(terminal.threading, "Thread", _DummyThread)
    monkeypatch.setattr(terminal.os, "getpgid", lambda pid: pid)
    result = {}

    def start():
        result["term"] = terminal.start_managed_terminal("public-blocked", tmp_path)

    worker = real_thread(target=start)
    worker.start()
    assert entered_spawn.wait(timeout=5)

    acquired = terminal._LOCK.acquire(timeout=0.2)
    if acquired:
        terminal._LOCK.release()
    release_spawn.set()
    worker.join(timeout=5)

    assert acquired, "managed readiness held the global terminal registry lock"
    assert not worker.is_alive()
    assert result["term"].kind == "claude_code"


@pytest.mark.parametrize(
    "stage",
    [
        "nonblocking",
        "readiness",
        "reaper",
        "size",
        "thread_constructor",
        "thread_start",
        "publication",
    ],
)
def test_post_spawn_failure_rolls_back_group_fds_and_capacity(
    monkeypatch, tmp_path, stage
):
    real_thread = threading.Thread
    proc = _ReadyProc()
    opened_fds = []
    real_openpty = terminal.os.openpty
    real_pipe = terminal.os.pipe

    def tracked_openpty():
        pair = real_openpty()
        opened_fds.extend(pair)
        return pair

    def tracked_pipe():
        pair = real_pipe()
        opened_fds.extend(pair)
        return pair

    def fake_popen(*args, **kwargs):
        os.write(kwargs["pass_fds"][0], b'{"state":"ready"}\n')
        return proc

    monkeypatch.setattr(terminal.os, "openpty", tracked_openpty)
    monkeypatch.setattr(terminal.os, "pipe", tracked_pipe)
    monkeypatch.setattr(terminal.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(terminal.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(terminal, "_ensure_terminal_reaper", lambda: None)
    monkeypatch.setattr(terminal, "_set_size", lambda *_args: None)
    monkeypatch.setattr(terminal.threading, "Thread", _DummyThread)

    if stage == "nonblocking":
        monkeypatch.setattr(
            terminal, "_set_nonblocking", lambda _fd: (_ for _ in ()).throw(RuntimeError(stage))
        )
    elif stage == "readiness":
        monkeypatch.setattr(
            terminal,
            "_read_managed_runner_readiness",
            lambda _fd: (_ for _ in ()).throw(RuntimeError(stage)),
        )
    elif stage == "reaper":
        monkeypatch.setattr(
            terminal,
            "_ensure_terminal_reaper",
            lambda: (_ for _ in ()).throw(RuntimeError(stage)),
        )
    elif stage == "size":
        monkeypatch.setattr(
            terminal,
            "_set_size",
            lambda *_args: (_ for _ in ()).throw(RuntimeError(stage)),
        )
    elif stage == "thread_constructor":
        monkeypatch.setattr(
            terminal.threading,
            "Thread",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(stage)),
        )
    elif stage == "thread_start":
        def unstartable_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)

            def fail_start():
                raise RuntimeError(stage)

            thread.start = fail_start
            return thread

        monkeypatch.setattr(terminal.threading, "Thread", unstartable_thread)
    elif stage == "publication":
        class _RejectingRegistry(dict):
            def __setitem__(self, key, value):
                raise RuntimeError(stage)

        monkeypatch.setattr(terminal, "_TERMINALS", _RejectingRegistry())

    with pytest.raises(RuntimeError, match=stage):
        terminal.start_managed_terminal(f"public-{stage}", tmp_path)

    assert proc.poll() is not None, "spawned runner was orphaned"
    assert terminal._MANAGED_START_RESERVATIONS == 0
    assert not terminal._TERMINALS
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_runner_collision_fails_without_adopting_external_process(
    monkeypatch, tmp_path
):
    signalled = []
    monkeypatch.setattr(
        terminal.os, "killpg", lambda pgid, sig: signalled.append((pgid, sig))
    )

    with pytest.raises(terminal.ManagedTerminalStartError) as raised:
        _start(monkeypatch, tmp_path, state="active_elsewhere")

    assert raised.value.state == "active_elsewhere"
    assert raised.value.recovery == "stop_locally"
    assert {pgid for pgid, _sig in signalled} <= {_ReadyProc.pid}
    assert not terminal._TERMINALS


def test_process_singleton_fails_closed_for_second_owner(tmp_path):
    path = tmp_path / "claude-bridge.lock"
    first = terminal.acquire_managed_terminal_singleton(path)
    assert first is not None
    try:
        assert terminal.acquire_managed_terminal_singleton(path) is None
        info = os.fstat(first.fd)
        assert info.st_uid == os.getuid()
        assert first.path == path
    finally:
        first.close()

    replacement = terminal.acquire_managed_terminal_singleton(path)
    assert replacement is not None
    replacement.close()


def test_singleton_rejects_symlink_lock_path(tmp_path):
    target = tmp_path / "target"
    target.write_text("do not lock")
    path = tmp_path / "bridge.lock"
    path.symlink_to(target)

    assert terminal.acquire_managed_terminal_singleton(path) is None
    with target.open("r+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
