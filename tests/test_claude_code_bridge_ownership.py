from __future__ import annotations

import json
import multiprocessing
import os
import shlex
import subprocess
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import pytest


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _descriptor(tmp_path: Path, claude_body: str):
    from api.claude_code_bridge import (
        ClaudeModelProfile,
        ClaudeSessionDescriptor,
        ClaudeStore,
    )

    config_dir = tmp_path / ".claude-local"
    projects_dir = config_dir / "projects"
    workspace = tmp_path / "workspace"
    projects_dir.mkdir(parents=True)
    workspace.mkdir()
    claude_bin = _executable(tmp_path / "claude", claude_body)
    wrapper = _executable(tmp_path / "wrapper", "exit 0\n")
    profile = ClaudeModelProfile("anthropic.qwen-aeon", "Claude Qwen", (str(wrapper),))
    store = ClaudeStore(
        store_id="local-models",
        label="Claude Local",
        config_dir=config_dir,
        projects_dir=projects_dir,
        claude_bin=claude_bin,
        workspace_roots=(workspace,),
        models=MappingProxyType({profile.model_id: profile}),
    )
    session_id = str(uuid4())
    transcript = projects_dir / "project" / f"{session_id}.jsonl"
    transcript.parent.mkdir()
    transcript.write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "cwd": str(workspace),
                "message": {"role": "user", "content": "hello"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "model": profile.model_id,
                    "content": "answer",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return ClaudeSessionDescriptor(
        public_id="claude_code_test",
        store=store,
        claude_session_id=session_id,
        transcript_path=transcript,
        profile=profile,
        cwd=workspace,
        workspace_label=workspace.name,
        messages=(),
        message_count=2,
        title="hello",
        created_at=None,
        updated_at=None,
        file_updated_at=transcript.stat().st_mtime,
        can_remote_resume=True,
    )


def _agent(session_id: str, *, status: str = "busy") -> dict:
    return {
        "sessionId": session_id,
        "status": status,
        "kind": "interactive",
        "cwd": "/tmp/workspace",
        "pid": 1234,
        "name": "claude",
    }


def _lease_worker(config_dir: str, session_id: str, started, release, results) -> None:
    from api.claude_code_runner import acquire_resume_lease, resume_lock_path

    started.wait()
    lease = acquire_resume_lease(Path(config_dir), "local-models", session_id)
    lock_path = resume_lock_path(Path(config_dir), "local-models", session_id)
    results.put((lease is not None, lock_path.stat().st_ino))
    if lease is not None:
        release.wait(timeout=5)
        lease.close()


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "malformed", "oversized", "duplicate"])
def test_probe_failure_never_allows_spawn(tmp_path, failure):
    from api.claude_code_bridge import probe_runtime_status

    if failure == "timeout":
        body = "/bin/sleep 4\n"
    elif failure == "nonzero":
        body = "exit 7\n"
    elif failure == "malformed":
        body = "printf '%s' '{not-json'\n"
    elif failure == "oversized":
        body = "/usr/bin/yes x | /usr/bin/head -c 1048577\n"
    else:
        session_id = str(uuid4())
        payload = json.dumps([_agent(session_id), _agent(session_id, status="idle")])
        body = f"printf '%s' {shlex.quote(payload)}\n"
    descriptor = _descriptor(tmp_path, body)

    assert probe_runtime_status(descriptor).state == "ownership_unknown"


@pytest.mark.parametrize("status", ["busy", "idle", "blocked"])
def test_any_live_matching_agent_blocks_resume(tmp_path, status):
    from api.claude_code_bridge import probe_runtime_status

    descriptor = _descriptor(tmp_path, "exit 99\n")
    payload = json.dumps([_agent(descriptor.claude_session_id, status=status)])
    descriptor.store.claude_bin.write_text(
        "#!/bin/sh\nprintf '%s' " + shlex.quote(payload) + "\n",
        encoding="utf-8",
    )

    assert probe_runtime_status(descriptor).state == "active_elsewhere"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        ["not-an-object"],
        [{"sessionId": "not-a-uuid", "status": "idle", "kind": "interactive", "cwd": "/tmp", "pid": 1, "name": "claude"}],
        [{"sessionId": str(uuid4()), "status": 1, "kind": "interactive", "cwd": "/tmp", "pid": 1, "name": "claude"}],
        [{"sessionId": str(uuid4()), "status": "idle", "kind": "interactive", "cwd": "/tmp", "pid": True, "name": "claude"}],
        [_agent(str(uuid4())) for _ in range(257)],
    ],
)
def test_probe_rejects_invalid_agent_schema_and_record_limit(tmp_path, payload):
    from api.claude_code_bridge import probe_runtime_status

    descriptor = _descriptor(tmp_path, f"printf '%s' {shlex.quote(json.dumps(payload))}\n")

    assert probe_runtime_status(descriptor).state == "ownership_unknown"


def test_probe_uses_only_scratch_environment_and_pinned_binary(tmp_path, monkeypatch):
    from api.claude_code_bridge import probe_runtime_status

    descriptor = _descriptor(tmp_path, "exit 99\n")
    expected_config = shlex.quote(str(descriptor.store.config_dir))
    descriptor.store.claude_bin.write_text(
        "#!/bin/sh\n"
        f"[ \"$CLAUDE_CONFIG_DIR\" = {expected_config} ] || exit 10\n"
        "[ -n \"$HOME\" ] || exit 11\n"
        "[ -n \"$TMPDIR\" ] || exit 12\n"
        "[ \"$HERMES_TEST_PROVIDER_SECRET\" = '' ] || exit 13\n"
        "[ \"$HERMES_WEBUI_CLAUDE_STORES_FILE\" = '' ] || exit 14\n"
        "[ \"$1\" = agents ] && [ \"$2\" = --json ] || exit 15\n"
        "printf '%s' '[]'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_TEST_PROVIDER_SECRET", "must-not-leak")
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_STORES_FILE", "/secret/registry")

    assert probe_runtime_status(descriptor).state == "inactive"


def test_probe_is_single_flight_and_cached_per_store(tmp_path):
    from api.claude_code_bridge import probe_runtime_status

    calls = tmp_path / "calls"
    descriptor = _descriptor(
        tmp_path,
        f"printf x >> {shlex.quote(str(calls))}\n/bin/sleep 0.2\nprintf '%s' '[]'\n",
    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(lambda _: probe_runtime_status(descriptor).state, range(6)))

    assert states == ["inactive"] * 6
    assert calls.read_text(encoding="utf-8") == "x"


def test_build_resume_argv_uses_only_fixed_descriptor_values(tmp_path):
    from api.claude_code_bridge import build_resume_argv

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")

    assert build_resume_argv(descriptor) == (
        str(descriptor.profile.argv[0]),
        "--hermes-lease-held",
        "--resume",
        descriptor.claude_session_id,
    )


def test_exactly_one_process_acquires_same_store_uuid(tmp_path):
    from api.claude_code_runner import resume_lock_path

    context = multiprocessing.get_context("spawn")
    started = context.Event()
    release = context.Event()
    results = context.Queue()
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    processes = [
        context.Process(
            target=_lease_worker,
            args=(str(config_dir), session_id, started, release, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    started.set()
    outcomes = [results.get(timeout=5) for _ in processes]
    release.set()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert sorted(acquired for acquired, _inode in outcomes) == [False, True]
    assert len({inode for _acquired, inode in outcomes}) == 1
    lock_path = resume_lock_path(config_dir, "local-models", session_id)
    assert lock_path.exists()
    assert (lock_path.parent.stat().st_mode & 0o777) == 0o700


def test_lock_rejects_symlink_and_hardlink_without_unlinking(tmp_path):
    from api.claude_code_runner import acquire_resume_lease, resume_lock_path

    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    symlink_id = str(uuid4())
    symlink_path = resume_lock_path(config_dir, "local-models", symlink_id)
    symlink_path.parent.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    symlink_path.symlink_to(target)

    assert acquire_resume_lease(config_dir, "local-models", symlink_id) is None
    assert symlink_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "unchanged"

    hardlink_id = str(uuid4())
    hardlink_path = resume_lock_path(config_dir, "local-models", hardlink_id)
    hardlink_path.touch(mode=0o600)
    os.link(hardlink_path, tmp_path / "second-link")
    assert acquire_resume_lease(config_dir, "local-models", hardlink_id) is None
    assert hardlink_path.exists()


def test_resume_lease_survives_exec_until_replacement_exits(tmp_path):
    from api.claude_code_runner import acquire_resume_lease

    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    code = (
        "import os,sys; from pathlib import Path; "
        "from api.claude_code_runner import acquire_resume_lease; "
        "lease=acquire_resume_lease(Path(sys.argv[1]),'local-models',sys.argv[2]); "
        "print('ready',flush=True); os.execv('/bin/sleep',['sleep','5'])"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(config_dir), session_id],
        cwd=str(Path(__file__).parents[1]),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert acquire_resume_lease(config_dir, "local-models", session_id) is None
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    lease = acquire_resume_lease(config_dir, "local-models", session_id)
    assert lease is not None
    lease.close()


def test_runner_sends_one_bounded_ready_record_then_execs_fixed_wrapper(tmp_path, monkeypatch):
    import api.claude_code_runner as runner

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")
    monkeypatch.setattr(runner, "resolve_session", lambda public_id: descriptor)
    read_fd, write_fd = os.pipe()
    exec_call = []

    result = runner.run_session(
        descriptor.public_id,
        write_fd,
        exec_fn=lambda executable, argv: exec_call.append((executable, tuple(argv))),
    )
    readiness = os.read(read_fd, 1024)
    os.close(read_fd)

    assert result == 0
    assert readiness == b'{"state":"ready"}\n'
    assert len(readiness) <= runner.READINESS_MAX_BYTES
    assert exec_call == [(descriptor.profile.argv[0], (
        descriptor.profile.argv[0],
        "--hermes-lease-held",
        "--resume",
        descriptor.claude_session_id,
    ))]


def test_runner_rejects_transcript_or_wrapper_replaced_while_acquiring_lease(tmp_path, monkeypatch):
    import api.claude_code_runner as runner

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")
    monkeypatch.setattr(runner, "resolve_session", lambda public_id: descriptor)
    real_acquire = runner.acquire_resume_lease

    def acquire_then_replace(config_dir, store_id, session_id):
        lease = real_acquire(config_dir, store_id, session_id)
        old_wrapper = descriptor.profile.argv[0]
        replacement = tmp_path / "replacement"
        _executable(replacement, "exit 0\n")
        os.replace(replacement, old_wrapper)
        return lease

    monkeypatch.setattr(runner, "acquire_resume_lease", acquire_then_replace)
    read_fd, write_fd = os.pipe()
    exec_call = []

    result = runner.run_session(
        descriptor.public_id,
        write_fd,
        exec_fn=lambda executable, argv: exec_call.append((executable, tuple(argv))),
    )
    readiness = os.read(read_fd, 1024)
    os.close(read_fd)

    assert result == 1
    assert readiness == b'{"state":"invalid_session"}\n'
    assert exec_call == []


@pytest.mark.parametrize(
    ("arguments", "expect_locked", "expected_arguments"),
    [
        (["--resume", "{uuid}"], True, ["--resume", "{uuid}"]),
        (["--resume={uuid}"], True, ["--resume={uuid}"]),
        (["-r", "{uuid}"], True, ["-r", "{uuid}"]),
        (["-r={uuid}"], True, ["-r={uuid}"]),
        (["--resume"], False, ["--resume"]),
        (["--resume", "not-a-uuid"], False, ["--resume", "not-a-uuid"]),
        (["--hermes-lease-held", "--resume", "{uuid}"], False, ["--resume", "{uuid}"]),
    ],
)
def test_wrapper_protocol_locks_only_explicit_uuid_resume_forms(
    tmp_path, arguments, expect_locked, expected_arguments
):
    from api.claude_code_runner import resume_lock_path

    protocol = Path(__file__).parents[1] / "scripts" / "claude_local_resume_protocol.zsh"
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    output = tmp_path / "argv.json"
    command = _executable(
        tmp_path / "raw-claude",
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(output))}\n",
    )
    expanded = [value.format(uuid=session_id) for value in arguments]
    expected = [value.format(uuid=session_id) for value in expected_arguments]

    completed = subprocess.run(
        [str(protocol), str(config_dir), "local-models", str(command), *expanded],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0
    assert output.read_text(encoding="utf-8").splitlines() == expected
    assert resume_lock_path(config_dir, "local-models", session_id).exists() is expect_locked


def test_direct_wrapper_and_runner_contend_for_the_same_lease(tmp_path):
    from api.claude_code_runner import acquire_resume_lease

    protocol = Path(__file__).parents[1] / "scripts" / "claude_local_resume_protocol.zsh"
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    started = tmp_path / "started"
    command = _executable(
        tmp_path / "raw-claude",
        f"printf x > {shlex.quote(str(started))}\n/bin/sleep 5\n",
    )
    holder = subprocess.Popen(
        [
            str(protocol),
            str(config_dir),
            "local-models",
            str(command),
            "--resume",
            session_id,
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        assert acquire_resume_lease(config_dir, "local-models", session_id) is None
    finally:
        os.killpg(holder.pid, 15)
        holder.wait(timeout=5)

    lease = acquire_resume_lease(config_dir, "local-models", session_id)
    assert lease is not None
    lease.close()


def test_runner_private_flag_skips_wrapper_reacquisition(tmp_path):
    from api.claude_code_runner import acquire_resume_lease

    protocol = Path(__file__).parents[1] / "scripts" / "claude_local_resume_protocol.zsh"
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    output = tmp_path / "called"
    command = _executable(tmp_path / "raw-claude", f"printf called > {shlex.quote(str(output))}\n")
    lease = acquire_resume_lease(config_dir, "local-models", session_id)
    assert lease is not None
    try:
        completed = subprocess.run(
            [
                str(protocol),
                str(config_dir),
                "local-models",
                str(command),
                "--hermes-lease-held",
                "--resume",
                session_id,
            ],
            check=False,
            timeout=5,
        )
    finally:
        lease.close()

    assert completed.returncode == 0
    assert output.read_text(encoding="utf-8") == "called"


def test_runner_lease_survives_exec_through_wrapper_protocol(tmp_path):
    from api.claude_code_runner import acquire_resume_lease

    protocol = Path(__file__).parents[1] / "scripts" / "claude_local_resume_protocol.zsh"
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    code = (
        "import os,sys; from pathlib import Path; "
        "from api.claude_code_runner import acquire_resume_lease; "
        "lease=acquire_resume_lease(Path(sys.argv[1]),'local-models',sys.argv[2]); "
        "print('ready',flush=True); "
        "os.execv(sys.argv[3],[sys.argv[3],sys.argv[1],'local-models','/bin/sleep',"
        "'--hermes-lease-held','--resume',sys.argv[2],'5'])"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(config_dir), session_id, str(protocol)],
        cwd=str(Path(__file__).parents[1]),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert acquire_resume_lease(config_dir, "local-models", session_id) is None
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_post_spawn_collision_exits_runner_without_signalling_agents_pid(tmp_path, monkeypatch):
    import api.claude_code_runner as runner
    from api.claude_code_bridge import ClaudeRuntimeStatus

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")
    foreign = subprocess.Popen(["/bin/sleep", "5"])
    monkeypatch.setattr(runner, "resolve_session", lambda public_id: descriptor)
    monkeypatch.setattr(
        runner,
        "probe_runtime_status",
        lambda descriptor, fresh: ClaudeRuntimeStatus("active_elsewhere"),
    )
    read_fd, write_fd = os.pipe()
    context = multiprocessing.get_context("fork")

    def run_owned_child():
        os.close(read_fd)
        result = runner.run_session(descriptor.public_id, write_fd, exec_fn=os.execv)
        time.sleep(0.3)
        os._exit(result)

    child = context.Process(target=run_owned_child)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            child.start()
        os.close(write_fd)
        assert os.read(read_fd, 1024) == b'{"state":"ownership_conflict"}\n'
        assert foreign.poll() is None
        assert child.is_alive()
        assert (
            runner.acquire_resume_lease(
                descriptor.store.config_dir,
                descriptor.store.store_id,
                descriptor.claude_session_id,
            )
            is None
        )
        child.join(timeout=5)
        assert child.exitcode == 1
        lease = runner.acquire_resume_lease(
            descriptor.store.config_dir,
            descriptor.store.store_id,
            descriptor.claude_session_id,
        )
        assert lease is not None
        lease.close()
    finally:
        os.close(read_fd)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        foreign.terminate()
        foreign.wait(timeout=5)
