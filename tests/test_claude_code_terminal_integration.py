from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

if os.name != "posix":
    pytest.skip("managed PTY integration requires POSIX", allow_module_level=True)

import api.terminal as terminal
from api.claude_code_runner import acquire_resume_lease


def _executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _runner_fixture(tmp_path: Path, *, ignore_hup: bool = False):
    repository = Path(__file__).parents[1]
    config_dir = tmp_path / ".claude-local"
    projects_dir = config_dir / "projects"
    authoritative_cwd = tmp_path / "authoritative-workspace"
    wrong_cwd = tmp_path / "caller-workspace"
    projects_dir.mkdir(parents=True)
    authoritative_cwd.mkdir()
    wrong_cwd.mkdir()
    session_id = str(uuid4())
    transcript = projects_dir / "project" / f"{session_id}.jsonl"
    transcript.parent.mkdir()
    transcript.write_text("{}\n", encoding="utf-8")
    claude_bin = _executable(
        tmp_path / "claude-probe",
        "import sys\nsys.stdout.write('[]')\n",
    )
    cwd_record = tmp_path / "wrapper.cwd"
    signal_setup = "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n" if ignore_hup else ""
    wrapper = _executable(
        tmp_path / "wrapper",
        "import os,signal,time\n"
        "from pathlib import Path\n"
        + signal_setup
        + f"Path({str(cwd_record)!r}).write_text(os.getcwd())\n"
        + "print('pty-ready', flush=True)\n"
        + "time.sleep(30)\n",
    )
    helper = tmp_path / "disposable_runner.py"
    helper.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from types import MappingProxyType\n"
        f"sys.path.insert(0, {str(repository)!r})\n"
        "import api.claude_code_runner as runner\n"
        "from api.claude_code_bridge import (\n"
        "    ClaudeModelProfile, ClaudeRuntimeStatus, ClaudeSessionDescriptor, ClaudeStore\n"
        ")\n"
        f"profile=ClaudeModelProfile('anthropic.qwen-aeon','Claude Qwen',({str(wrapper)!r},))\n"
        "store=ClaudeStore(\n"
        "    store_id='local-models', label='Claude Local',\n"
        f"    config_dir=Path({str(config_dir)!r}), projects_dir=Path({str(projects_dir)!r}),\n"
        f"    claude_bin=Path({str(claude_bin)!r}), workspace_roots=(Path({str(authoritative_cwd)!r}),),\n"
        "    models=MappingProxyType({profile.model_id: profile}),\n"
        ")\n"
        "descriptor=ClaudeSessionDescriptor(\n"
        "    public_id='public-integration', store=store,\n"
        f"    claude_session_id={session_id!r}, transcript_path=Path({str(transcript)!r}),\n"
        f"    profile=profile, cwd=Path({str(authoritative_cwd)!r}), workspace_label='workspace',\n"
        "    messages=(), message_count=1, title='test', created_at=None, updated_at=None,\n"
        f"    file_updated_at=Path({str(transcript)!r}).stat().st_mtime, can_remote_resume=True,\n"
        ")\n"
        "runner.resolve_session=lambda public_id: descriptor if public_id == descriptor.public_id else None\n"
        "runner.probe_runtime_status=lambda descriptor, fresh=False: ClaudeRuntimeStatus('inactive')\n"
        "raise SystemExit(runner.main())\n",
        encoding="utf-8",
    )
    return {
        "helper": helper,
        "config_dir": config_dir,
        "session_id": session_id,
        "authoritative_cwd": authoritative_cwd,
        "wrong_cwd": wrong_cwd,
        "cwd_record": cwd_record,
    }


def _real_helper_spawn(helper: Path):
    def spawn(*, argv, cwd, env, slave_fd, pass_fds):
        return subprocess.Popen(
            [sys.executable, str(helper), argv[2], argv[3]],
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )

    return spawn


def _wait_for_path(path: Path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def test_real_runner_readiness_lease_exec_and_authoritative_cwd(
    tmp_path, monkeypatch
):
    fixture = _runner_fixture(tmp_path)
    monkeypatch.setattr(
        terminal, "_spawn_pty_process", _real_helper_spawn(fixture["helper"])
    )
    term = terminal.start_managed_terminal(
        "public-integration", fixture["wrong_cwd"]
    )
    stream_capability = terminal.issue_terminal_capability(
        term.handle, term.generation, "stream"
    )
    _term, output = terminal.attach_managed_terminal(
        handle=term.handle,
        generation=term.generation,
        capability=stream_capability,
    )
    try:
        _wait_for_path(fixture["cwd_record"])
        assert fixture["cwd_record"].read_text() == str(fixture["authoritative_cwd"])
        assert (
            acquire_resume_lease(
                fixture["config_dir"], "local-models", fixture["session_id"]
            )
            is None
        )
        seen = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "pty-ready" not in "".join(seen):
            try:
                _seq, event, payload = output.get(timeout=0.1)
            except queue.Empty:
                continue
            if event == "output":
                seen.append(payload["text"])
        assert "pty-ready" in "".join(seen)
    finally:
        stop_capability = terminal.issue_terminal_capability(
            term.handle, term.generation, "stop"
        )
        terminal.stop_managed_terminal(
            handle=term.handle,
            generation=term.generation,
            capability=stop_capability,
        )
    lease = acquire_resume_lease(
        fixture["config_dir"], "local-models", fixture["session_id"]
    )
    assert lease is not None
    lease.close()


def test_post_readiness_setup_failure_reaps_real_runner_and_releases_lease(
    tmp_path, monkeypatch
):
    fixture = _runner_fixture(tmp_path)
    spawned = {}
    real_spawn = _real_helper_spawn(fixture["helper"])
    opened_fds = []
    real_openpty = terminal.os.openpty
    real_pipe = terminal.os.pipe

    def capturing_spawn(**kwargs):
        proc = real_spawn(**kwargs)
        spawned["proc"] = proc
        return proc

    def tracked_openpty():
        pair = real_openpty()
        opened_fds.extend(pair)
        return pair

    def tracked_pipe():
        pair = real_pipe()
        opened_fds.extend(pair)
        return pair

    monkeypatch.setattr(terminal, "_spawn_pty_process", capturing_spawn)
    monkeypatch.setattr(terminal.os, "openpty", tracked_openpty)
    monkeypatch.setattr(terminal.os, "pipe", tracked_pipe)
    monkeypatch.setattr(terminal, "_ensure_terminal_reaper", lambda: None)
    monkeypatch.setattr(
        terminal.threading,
        "Thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("reader setup")),
    )

    with pytest.raises(RuntimeError, match="reader setup"):
        terminal.start_managed_terminal(
            "public-integration", fixture["wrong_cwd"]
        )

    assert spawned["proc"].poll() is not None
    assert terminal._MANAGED_START_RESERVATIONS == 0
    assert not terminal._TERMINALS
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    lease = acquire_resume_lease(
        fixture["config_dir"], "local-models", fixture["session_id"]
    )
    assert lease is not None
    lease.close()


def test_session_constructor_failure_reaps_verified_group_and_inherited_lease(
    tmp_path, monkeypatch
):
    repository = Path(__file__).parents[1]
    config_dir = tmp_path / ".claude-constructor-failure"
    config_dir.mkdir()
    session_id = str(uuid4())
    child_marker = tmp_path / "constructor-child.pid"
    helper = tmp_path / "constructor_failure_runner.py"
    helper.write_text(
        "import os,signal,sys,time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(repository)!r})\n"
        "from api.claude_code_runner import acquire_resume_lease\n"
        f"lease=acquire_resume_lease(Path({str(config_dir)!r}), 'local-models', {session_id!r})\n"
        "if lease is None:\n"
        "    raise SystemExit(4)\n"
        "child=os.fork()\n"
        "if child:\n"
        "    time.sleep(30)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(child_marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    spawned = {}
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

    def spawn(*, argv, cwd, env, slave_fd, pass_fds):
        proc = subprocess.Popen(
            [sys.executable, str(helper)],
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )
        spawned["proc"] = proc
        _wait_for_path(child_marker)
        spawned["lease_was_held"] = (
            acquire_resume_lease(config_dir, "local-models", session_id) is None
        )
        return proc

    monkeypatch.setattr(terminal.os, "openpty", tracked_openpty)
    monkeypatch.setattr(terminal.os, "pipe", tracked_pipe)
    monkeypatch.setattr(terminal, "_spawn_pty_process", spawn)
    monkeypatch.setattr(
        terminal,
        "TerminalSession",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("session constructor")),
    )

    try:
        with pytest.raises(RuntimeError, match="session constructor"):
            terminal.start_managed_terminal("constructor-failure", tmp_path)

        proc = spawned["proc"]
        assert spawned["lease_was_held"] is True
        assert proc.poll() is not None
        assert not _process_group_exists(proc.pid)
        assert terminal._MANAGED_START_RESERVATIONS == 0
        assert not terminal._TERMINALS
        for fd in opened_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
        lease = acquire_resume_lease(config_dir, "local-models", session_id)
        assert lease is not None
        lease.close()
    finally:
        proc = spawned.get("proc")
        if proc is not None and _process_group_exists(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)
        if proc is not None:
            proc.wait(timeout=5)


def test_orphaned_runner_lease_is_not_adopted_or_signalled(tmp_path, monkeypatch):
    fixture = _runner_fixture(tmp_path, ignore_hup=True)
    master_fd, slave_fd = os.openpty()
    read_fd, write_fd = os.pipe()
    first = subprocess.Popen(
        [
            sys.executable,
            str(fixture["helper"]),
            "public-integration",
            str(write_fd),
        ],
        cwd=fixture["wrong_cwd"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        pass_fds=(write_fd,),
        start_new_session=True,
    )
    os.close(slave_fd)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 256) == b'{"state":"ready"}\n'
        os.close(read_fd)
        _wait_for_path(fixture["cwd_record"])
        os.close(master_fd)
        time.sleep(0.1)
        assert first.poll() is None

        monkeypatch.setattr(
            terminal, "_spawn_pty_process", _real_helper_spawn(fixture["helper"])
        )
        with pytest.raises(terminal.ManagedTerminalStartError) as raised:
            terminal.start_managed_terminal(
                "public-integration", fixture["wrong_cwd"]
            )

        assert raised.value.state == "active_elsewhere"
        assert first.poll() is None
        assert not terminal._TERMINALS
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        if first.poll() is None:
            os.killpg(first.pid, signal.SIGKILL)
        first.wait(timeout=5)
