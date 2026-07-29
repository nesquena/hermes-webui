"""Agent TTS worker protocol and supervisor lifecycle tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from api import agent_tts, agent_tts_worker


class _StatusPopen:
    instances = []

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = 43210
        self.returncode = None
        self.request = None
        type(self).instances.append(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -15
        return self.returncode

    def communicate(self, payload, timeout):
        self.request = json.loads(payload.decode("utf-8"))
        status_path = Path(self.request["status_path"])
        assert os.stat(status_path.parent).st_mode & 0o777 == 0o700
        agent_tts_worker.write_status_file(
            status_path,
            {"schema": 1, "ok": True, "code": "ok", "echo": self.request["op"]},
        )
        self.returncode = 0
        return (None, None)


@pytest.fixture(autouse=True)
def _reset_supervisor_state(monkeypatch):
    _StatusPopen.instances.clear()
    monkeypatch.setattr(agent_tts, "_active_owner_keys", set())


def _scope(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    return agent_tts.AgentTtsProfileScope(
        name="voice",
        home=home.resolve(),
        config_path=(home / "config.yaml").resolve(),
        child_env={"HERMES_HOME": str(home.resolve()), "MARKER": "voice"},
    )


def test_supervisor_uses_bounded_status_file_and_cleans_request_dir(
    tmp_path, monkeypatch
):
    scope = _scope(tmp_path)
    monkeypatch.setattr(agent_tts.subprocess, "Popen", _StatusPopen)

    result = agent_tts.run_agent_tts_operation(
        "capability", profile_scope=scope, owner_key="user:voice"
    )

    assert result["echo"] == "capability"
    instance = _StatusPopen.instances[0]
    assert instance.kwargs["env"] == scope.child_env
    assert instance.kwargs["stdout"] is subprocess.DEVNULL
    assert instance.kwargs["stderr"] is subprocess.DEVNULL
    assert instance.kwargs.get("start_new_session") is (os.name == "posix")
    assert not Path(instance.request["request_dir"]).exists()


def test_worker_job_handle_is_closed_on_success(tmp_path, monkeypatch):
    scope = _scope(tmp_path)

    class Job:
        closed = False

        def close(self):
            self.closed = True

    job = Job()
    monkeypatch.setattr(agent_tts, "_create_windows_worker_job", lambda _proc: job)
    monkeypatch.setattr(agent_tts.subprocess, "Popen", _StatusPopen)

    result = agent_tts.run_agent_tts_operation(
        "capability", profile_scope=scope, owner_key="job-success"
    )

    assert result["ok"] is True
    assert job.closed is True


def test_request_root_rejects_symlink_without_writing_outside_profile(tmp_path):
    scope = _scope(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = scope.home / "cache"
    cache.symlink_to(outside, target_is_directory=True)

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts._request_directory(scope)

    assert exc_info.value.code == "agent_worker_start"
    assert list(outside.iterdir()) == []


def test_request_root_creates_fresh_profile_home(tmp_path):
    home = tmp_path / "profiles" / "fresh"
    scope = agent_tts.AgentTtsProfileScope(
        name="fresh",
        home=home,
        config_path=home / "config.yaml",
        child_env={"HERMES_HOME": str(home)},
    )

    request_dir = agent_tts._request_directory(scope)

    assert request_dir.parent == home / "cache" / "webui-tts-requests"
    assert home.is_dir()
    assert os.stat(home).st_mode & 0o777 == 0o700


def test_status_file_permissions_are_private(tmp_path):
    path = tmp_path / "status.json"
    agent_tts_worker.write_status_file(
        path, {"schema": 1, "ok": False, "code": "test"}
    )
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_supervisor_rejects_malformed_status_and_cleans(tmp_path, monkeypatch):
    scope = _scope(tmp_path)

    class Malformed(_StatusPopen):
        def communicate(self, payload, timeout):
            self.request = json.loads(payload.decode("utf-8"))
            Path(self.request["status_path"]).write_text("not-json", encoding="utf-8")
            self.returncode = 1
            return (None, None)

    monkeypatch.setattr(agent_tts.subprocess, "Popen", Malformed)

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts.run_agent_tts_operation("capability", profile_scope=scope)

    assert exc_info.value.code == "agent_worker_protocol"
    assert exc_info.value.status == 502
    assert not Path(Malformed.instances[-1].request["request_dir"]).exists()


def test_supervisor_rejects_oversized_status(tmp_path, monkeypatch):
    scope = _scope(tmp_path)

    class Oversized(_StatusPopen):
        def communicate(self, payload, timeout):
            self.request = json.loads(payload.decode("utf-8"))
            Path(self.request["status_path"]).write_bytes(
                b"{" + b"x" * agent_tts.AGENT_TTS_STATUS_MAX_BYTES
            )
            self.returncode = 1
            return (None, None)

    monkeypatch.setattr(agent_tts.subprocess, "Popen", Oversized)

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts.run_agent_tts_operation("capability", profile_scope=scope)

    assert exc_info.value.code == "agent_worker_protocol"


def test_worker_failure_log_uses_only_allowlisted_diagnostics(caplog):
    payload = {
        "schema": 1,
        "ok": False,
        "code": "synthesis_failed",
        "diagnostic": "provider_timeout",
        "provider": "gemini",
        "raw_error": "secret-token-must-not-leak",
    }

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts._raise_worker_error(payload)

    assert exc_info.value.code == "synthesis_failed"
    assert "diagnostic=provider_timeout" in caplog.text
    assert "provider=gemini" in caplog.text
    assert "secret-token-must-not-leak" not in caplog.text


def test_timeout_terminates_tree_reaps_and_cleans(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    terminated = []

    class TimedOut(_StatusPopen):
        def communicate(self, payload, timeout):
            self.request = json.loads(payload.decode("utf-8"))
            raise subprocess.TimeoutExpired(self.command, timeout)

    monkeypatch.setattr(agent_tts.subprocess, "Popen", TimedOut)
    monkeypatch.setattr(
        agent_tts, "_terminate_worker_tree", lambda proc: terminated.append(proc.pid)
    )

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts.run_agent_tts_operation(
            "synthesize", {"text": "hello"}, profile_scope=scope, timeout_seconds=0.01
        )

    assert exc_info.value.code == "agent_timeout"
    assert exc_info.value.status == 504
    assert terminated == [43210]
    assert not Path(TimedOut.instances[-1].request["request_dir"]).exists()


def test_timeout_kills_real_worker_process_group(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX process-group assertion")
    scope = _scope(tmp_path)
    pid_file = tmp_path / "provider-child.pid"
    script = tmp_path / "slow_worker.py"
    script.write_text(
        """
import json, os, subprocess, sys, time
request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
child = subprocess.Popen(['sleep', '60'])
with open(os.environ['TEST_PROVIDER_PID_FILE'], 'w', encoding='utf-8') as handle:
    handle.write(str(child.pid))
    handle.flush()
time.sleep(60)
""",
        encoding="utf-8",
    )
    env = dict(scope.child_env)
    env["TEST_PROVIDER_PID_FILE"] = str(pid_file)
    scope = agent_tts.AgentTtsProfileScope(
        scope.name, scope.home, scope.config_path, env
    )
    monkeypatch.setattr(agent_tts, "_worker_command", lambda: [sys.executable, str(script)])

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts.run_agent_tts_operation(
            "synthesize",
            {"text": "hello"},
            profile_scope=scope,
            timeout_seconds=0.4,
        )

    assert exc_info.value.code == "agent_timeout"
    assert pid_file.exists(), "fixture child did not start before timeout"
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("provider child survived worker process-group timeout")


def test_success_cleanup_kills_descendant_after_worker_leader_exits(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX process-group assertion")
    scope = _scope(tmp_path)
    pid_file = tmp_path / "orphan-child.pid"
    script = tmp_path / "orphan_worker.py"
    script.write_text(
        """
import json, os, subprocess, sys
request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
child = subprocess.Popen(['sleep', '60'])
with open(os.environ['TEST_PROVIDER_PID_FILE'], 'w', encoding='utf-8') as handle:
    handle.write(str(child.pid))
with open(request['status_path'], 'w', encoding='utf-8') as handle:
    json.dump({'schema': 1, 'ok': True, 'code': 'ok'}, handle)
os.chmod(request['status_path'], 0o600)
""",
        encoding="utf-8",
    )
    env = dict(scope.child_env)
    env["TEST_PROVIDER_PID_FILE"] = str(pid_file)
    scope = agent_tts.AgentTtsProfileScope(scope.name, scope.home, scope.config_path, env)
    monkeypatch.setattr(agent_tts, "_worker_command", lambda: [sys.executable, str(script)])

    assert agent_tts.run_agent_tts_operation("capability", profile_scope=scope)["ok"] is True
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("provider child survived successful worker cleanup")


def test_disconnect_cancellation_terminates_real_worker_and_cleans(monkeypatch, tmp_path):
    scope = _scope(tmp_path)
    monkeypatch.setattr(
        agent_tts,
        "_worker_command",
        lambda: [
            sys.executable,
            "-c",
            "import sys,time;sys.stdin.buffer.read();time.sleep(30)",
        ],
    )
    started = time.monotonic()

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts.run_agent_tts_operation(
            "synthesize",
            {"text": "cancel me"},
            profile_scope=scope,
            owner_key="disconnect-owner",
            timeout_seconds=5,
            cancellation_check=lambda: True,
        )

    assert exc_info.value.code == "agent_cancelled"
    assert time.monotonic() - started < 2
    request_root = scope.home / "cache" / "webui-tts-requests"
    assert not list(request_root.glob("request-*"))
    assert "disconnect-owner" not in agent_tts._active_owner_keys


def test_owner_and_global_saturation_fail_before_spawn(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    spawned = []
    monkeypatch.setattr(agent_tts.subprocess, "Popen", lambda *a, **k: spawned.append(1))
    agent_tts._active_owner_keys.add("same-owner")

    with pytest.raises(agent_tts.AgentTtsError) as owner_error:
        agent_tts.run_agent_tts_operation(
            "capability", profile_scope=scope, owner_key="same-owner"
        )
    assert owner_error.value.status == 429
    assert owner_error.value.code == "agent_busy"

    agent_tts._active_owner_keys.clear()
    monkeypatch.setattr(agent_tts, "_acquire_global_slot", lambda: False)
    with pytest.raises(agent_tts.AgentTtsError) as global_error:
        agent_tts.run_agent_tts_operation("capability", profile_scope=scope)
    assert global_error.value.status == 429
    assert spawned == []


def test_request_payload_cap_is_enforced_before_spawn(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    spawned = []
    monkeypatch.setattr(agent_tts.subprocess, "Popen", lambda *a, **k: spawned.append(1))

    with pytest.raises(agent_tts.AgentTtsError) as exc_info:
        agent_tts.run_agent_tts_operation(
            "synthesize",
            {"text": "x" * agent_tts.AGENT_TTS_REQUEST_MAX_BYTES},
            profile_scope=scope,
        )

    assert exc_info.value.code == "invalid_request"
    assert spawned == []


def test_worker_rejects_unknown_schema_and_operation():
    assert agent_tts_worker.dispatch_request({"schema": 2, "op": "capability"}) == {
        "schema": 1,
        "ok": False,
        "code": "invalid_request",
    }
    assert agent_tts_worker.dispatch_request({"schema": 1, "op": "other"}) == {
        "schema": 1,
        "ok": False,
        "code": "invalid_request",
    }
