from __future__ import annotations

import json
import multiprocessing
import os
import select
import shlex
import subprocess
import sys
import threading
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


def test_fresh_probe_waits_for_a_probe_started_after_the_fresh_request(
    tmp_path, monkeypatch
):
    import api.claude_code_bridge as bridge

    descriptor = _descriptor(tmp_path, "exit 99\n")
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def controlled_probe(store):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
            return b"[]"
        return json.dumps([_agent(descriptor.claude_session_id)]).encode("utf-8")

    bridge.invalidate_claude_session_cache()
    monkeypatch.setattr(bridge, "_run_agents_command", controlled_probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pre_lease = pool.submit(bridge.probe_runtime_status, descriptor)
        assert first_started.wait(timeout=3)
        post_lease = pool.submit(
            lambda: bridge.probe_runtime_status(descriptor, fresh=True)
        )
        release_first.set()

        assert pre_lease.result(timeout=3).state == "inactive"
        assert post_lease.result(timeout=3).state == "active_elsewhere"
    assert calls == 2


@pytest.mark.parametrize(
    "raw_payload",
    [
        lambda session_id: json.dumps(
            [{**_agent(session_id), "unexpected": "retained-extra"}]
        ),
        lambda session_id: (
            '[{"sessionId":"'
            + session_id
            + '","status":"busy","status":"idle","kind":"interactive",'
            '"cwd":"/tmp/workspace","pid":1234,"name":"claude"}]'
        ),
    ],
)
def test_probe_rejects_unknown_properties_and_duplicate_json_keys(
    tmp_path, raw_payload
):
    from api.claude_code_bridge import probe_runtime_status

    descriptor = _descriptor(tmp_path, "exit 99\n")
    payload = raw_payload(descriptor.claude_session_id)
    descriptor.store.claude_bin.write_text(
        "#!/bin/sh\nprintf '%s' " + shlex.quote(payload) + "\n",
        encoding="utf-8",
    )

    assert probe_runtime_status(descriptor).state == "ownership_unknown"


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

    def ready_without_fork(_descriptor, readiness_fd, _pgid, _close_fds):
        runner._write_readiness(readiness_fd, "ready")
        return -1

    result = runner.run_session(
        descriptor.public_id,
        write_fd,
        exec_fn=lambda executable, argv: exec_call.append((executable, tuple(argv))),
        post_exec_check_fn=ready_without_fork,
    )
    readiness = os.read(read_fd, 1024)
    os.close(read_fd)

    assert result == 0
    assert readiness == b'{"state":"ready"}\n'
    assert len(readiness) <= runner.READINESS_MAX_BYTES
    assert len(exec_call) == 1
    assert exec_call[0][1][0] == descriptor.profile.argv[0]
    wrapper_arguments = (
        "--hermes-lease-held",
        "--resume",
        descriptor.claude_session_id,
    )
    if sys.platform == "darwin":
        assert exec_call[0][0] == "/bin/sh"
        assert exec_call[0][1][1].startswith("/dev/fd/")
        assert exec_call[0][1][2:] == wrapper_arguments
    else:
        assert exec_call[0][0].startswith("/dev/fd/")
        assert exec_call[0][1][1:] == wrapper_arguments


def test_runner_execs_from_revalidated_descriptor_workspace(tmp_path, monkeypatch):
    import api.claude_code_runner as runner

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")
    wrong_workspace = tmp_path / "caller-selected"
    wrong_workspace.mkdir()
    monkeypatch.setattr(runner, "resolve_session", lambda public_id: descriptor)
    read_fd, write_fd = os.pipe()
    exec_contexts = []
    def ready_without_fork(_descriptor, readiness_fd, _pgid, _close_fds):
        runner._write_readiness(readiness_fd, "ready")
        return -1

    original_cwd = Path.cwd()
    try:
        os.chdir(wrong_workspace)
        result = runner.run_session(
            descriptor.public_id,
            write_fd,
            exec_fn=lambda _executable, _argv: exec_contexts.append(
                (Path.cwd(), os.environ.get("PWD"))
            ),
            post_exec_check_fn=ready_without_fork,
        )
    finally:
        os.chdir(original_cwd)
    readiness = os.read(read_fd, 1024)
    os.close(read_fd)

    assert result == 0
    assert readiness == b'{"state":"ready"}\n'
    assert exec_contexts == [(descriptor.cwd, str(descriptor.cwd))]


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


def test_post_exec_probe_preserves_duplicate_session_owners_for_collision_check(
    tmp_path,
    monkeypatch,
):
    import api.claude_code_bridge as bridge

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")
    rows = [
        {
            "sessionId": descriptor.claude_session_id,
            "status": "busy",
            "kind": "interactive",
            "cwd": str(descriptor.cwd),
            "pid": 101,
            "name": "Hermes",
        },
        {
            "sessionId": descriptor.claude_session_id,
            "status": "idle",
            "kind": "interactive",
            "cwd": str(descriptor.cwd),
            "pid": 202,
            "name": "Local",
        },
    ]
    monkeypatch.setattr(
        bridge,
        "_run_agents_command",
        lambda _store: json.dumps(rows).encode("utf-8"),
    )

    assert bridge.probe_runtime_owner_pids(descriptor) == (101, 202)


def test_post_exec_checker_waits_for_exec_then_reports_foreign_owner(
    tmp_path,
    monkeypatch,
):
    import api.claude_code_runner as runner

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")
    expected_pgid = 303
    foreign_pid = 404
    monkeypatch.setattr(
        runner,
        "probe_runtime_owner_pids",
        lambda _descriptor: (expected_pgid, foreign_pid),
    )
    monkeypatch.setattr(
        runner.os,
        "getpgid",
        lambda pid: expected_pgid if pid == expected_pgid else foreign_pid,
    )
    readiness_read, readiness_write = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        exec_status = runner._start_post_exec_ownership_check(
            descriptor,
            readiness_write,
            expected_pgid,
            (),
        )
    try:
        assert select.select([readiness_read], [], [], 0.05)[0] == []
        os.write(exec_status, b"E")
        os.close(exec_status)
        exec_status = -1
        assert os.read(readiness_read, 1024) == b'{"state":"ownership_conflict"}\n'
    finally:
        if exec_status >= 0:
            os.close(exec_status)
        os.close(readiness_read)


def test_retained_wrapper_fd_executes_verified_inode_after_named_replacement(
    tmp_path,
):
    import api.claude_code_runner as runner

    descriptor = _descriptor(tmp_path, "printf '%s' '[]'\n")
    wrapper_path = Path(descriptor.profile.argv[0])
    wrapper_fd = runner._open_verified_wrapper(descriptor)
    assert wrapper_fd is not None
    replacement = _executable(tmp_path / "replacement-final", "exit 42\n")
    os.replace(replacement, wrapper_path)
    argv = (
        str(wrapper_path),
        "--hermes-lease-held",
        "--resume",
        descriptor.claude_session_id,
    )
    captured = {}
    try:
        runner._execute_verified_wrapper(
            wrapper_fd,
            argv,
            fexec_fn=lambda fd, args, _env: captured.update(
                body=os.pread(fd, 1024, 0),
                argv=tuple(args),
            ),
        )
    finally:
        os.close(wrapper_fd)

    assert captured["body"] == b"#!/bin/sh\nexit 0\n"
    assert captured["argv"] == argv


@pytest.mark.parametrize(
    ("arguments", "expect_locked", "expected_arguments"),
    [
        (["--resume", "{uuid}"], True, ["--resume", "{uuid}"]),
        (["--resume={uuid}"], True, ["--resume={uuid}"]),
        (["-r", "{uuid}"], True, ["-r", "{uuid}"]),
        (["-r={uuid}"], True, ["-r={uuid}"]),
        (["--resume"], False, ["--resume"]),
        (["--resume", "not-a-uuid"], False, ["--resume", "not-a-uuid"]),
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


def test_wrapper_protocol_runs_from_the_session_workspace(tmp_path):
    protocol = Path(__file__).parents[1] / "scripts" / "claude_local_resume_protocol.zsh"
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "called"
    command = _executable(
        tmp_path / "raw-claude", f"printf called > {shlex.quote(str(output))}\n"
    )

    completed = subprocess.run(
        [str(protocol), str(config_dir), "local-models", str(command)],
        cwd=workspace,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0
    assert output.read_text(encoding="utf-8") == "called"


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
        environment = os.environ.copy()
        environment["HERMES_RESUME_LEASE_FD"] = str(lease.fd)
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
            env=environment,
            pass_fds=(lease.fd,),
            timeout=5,
        )
    finally:
        lease.close()

    assert completed.returncode == 0
    assert output.read_text(encoding="utf-8") == "called"


@pytest.mark.parametrize("failure", ["wrong_position", "missing", "closed", "wrong_inode"])
def test_private_flag_never_bypasses_without_authenticated_expected_fd(
    tmp_path, failure
):
    from api.claude_code_runner import acquire_resume_lease

    protocol = Path(__file__).parents[1] / "scripts" / "claude_local_resume_protocol.zsh"
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    output = tmp_path / "called"
    command = _executable(
        tmp_path / "raw-claude", f"printf called > {shlex.quote(str(output))}\n"
    )
    lease = acquire_resume_lease(config_dir, "local-models", session_id)
    assert lease is not None
    lock_path = lease.path
    lease.close()
    environment = os.environ.copy()
    pass_fds = ()
    opened_fd = None
    arguments = ["--hermes-lease-held", "--resume", session_id]
    if failure == "wrong_position":
        arguments.append("--verbose")
    elif failure == "closed":
        environment["HERMES_RESUME_LEASE_FD"] = "99"
    elif failure == "wrong_inode":
        wrong = tmp_path / "wrong-inode"
        wrong.touch()
        opened_fd = os.open(wrong, os.O_RDWR)
        environment["HERMES_RESUME_LEASE_FD"] = str(opened_fd)
        pass_fds = (opened_fd,)
    try:
        completed = subprocess.run(
            [
                str(protocol),
                str(config_dir),
                "local-models",
                str(command),
                *arguments,
            ],
            check=False,
            env=environment,
            pass_fds=pass_fds,
            timeout=5,
        )
    finally:
        if opened_fd is not None:
            os.close(opened_fd)

    assert completed.returncode != 0
    assert not output.exists()
    assert lock_path.exists()


def test_unlocked_expected_inherited_fd_acquires_before_private_exec(tmp_path):
    from api.claude_code_runner import acquire_resume_lease

    protocol = Path(__file__).parents[1] / "scripts" / "claude_local_resume_protocol.zsh"
    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    created = acquire_resume_lease(config_dir, "local-models", session_id)
    assert created is not None
    lock_path = created.path
    created.close()
    inherited_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    started = tmp_path / "started"
    command = _executable(
        tmp_path / "raw-claude",
        f"printf x > {shlex.quote(str(started))}\n/bin/sleep 5\n",
    )
    environment = os.environ.copy()
    environment["HERMES_RESUME_LEASE_FD"] = str(inherited_fd)
    holder = subprocess.Popen(
        [
            str(protocol),
            str(config_dir),
            "local-models",
            str(command),
            "--hermes-lease-held",
            "--resume",
            session_id,
        ],
        env=environment,
        pass_fds=(inherited_fd,),
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
        os.close(inherited_fd)


def test_lock_open_rejects_rename_symlink_substitution(tmp_path, monkeypatch):
    import api.claude_code_runner as runner

    config_dir = tmp_path / ".claude-local"
    config_dir.mkdir()
    session_id = str(uuid4())
    initial = runner.acquire_resume_lease(config_dir, "local-models", session_id)
    assert initial is not None
    lock_path = initial.path
    initial.close()
    displaced = tmp_path / "displaced-lock"
    replacement = tmp_path / "replacement-lock"
    replacement.touch(mode=0o600)
    real_open = runner.os.open
    raced = False

    def swap_after_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if not raced and path == lock_path.name and dir_fd is not None:
            raced = True
            lock_path.rename(displaced)
            lock_path.symlink_to(replacement)
        return fd

    monkeypatch.setattr(runner.os, "open", swap_after_open)

    assert runner.acquire_resume_lease(config_dir, "local-models", session_id) is None
    assert raced is True
    assert lock_path.is_symlink()
    assert displaced.stat().st_ino != replacement.stat().st_ino


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
        "os.environ['HERMES_RESUME_LEASE_FD']=str(lease.fd); "
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
