import pytest

import api.channel_gateway_supervisor as supervisor


class FakeProcess:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _reset():
    supervisor._PROCESSES.clear()
    supervisor._FAILURES.clear()


@pytest.fixture(autouse=True)
def _prevent_real_process_group_signals(monkeypatch):
    def missing_group(_pid, _signal):
        raise ProcessLookupError

    monkeypatch.setattr(supervisor.os, "killpg", missing_group, raising=False)


def test_start_profile_gateway_is_profile_scoped(monkeypatch, tmp_path):
    _reset()
    home = tmp_path / "profiles" / "maverick"
    home.mkdir(parents=True)
    (home / ".env").write_text("HERMES_WEBUI_MATRIX_GATEWAY_ENABLED=1\n")
    created = []
    monkeypatch.setattr(supervisor, "_gateway_restart_profile_context", lambda profile: (home, profile))
    monkeypatch.setattr(supervisor, "_resolve_hermes_command", lambda: "/venv/bin/hermes")
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda args, **kwargs: created.append(FakeProcess(args, **kwargs)) or created[-1])

    result = supervisor.start_profile_gateway("maverick")

    assert result["status"] == "running"
    assert result["profile"] == "maverick"
    assert "pid" not in result
    assert created[0].args == ["/venv/bin/hermes", "gateway", "run"]
    assert created[0].kwargs["env"]["HERMES_HOME"] == str(home)
    assert created[0].kwargs["start_new_session"] is True


def test_start_is_idempotent(monkeypatch, tmp_path):
    _reset()
    home = tmp_path / "maverick"
    home.mkdir()
    monkeypatch.setattr(supervisor, "_gateway_restart_profile_context", lambda profile: (home, profile))
    monkeypatch.setattr(supervisor, "_resolve_hermes_command", lambda: "hermes")
    calls = []
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **kw: calls.append(1) or FakeProcess(a[0], **kw))
    supervisor.start_profile_gateway("maverick")
    result = supervisor.start_profile_gateway("maverick")
    assert result["status"] == "running"
    assert len(calls) == 1


def test_restart_stops_managed_process_then_starts(monkeypatch, tmp_path):
    _reset()
    home = tmp_path / "maverick"
    home.mkdir()
    monkeypatch.setattr(supervisor, "_gateway_restart_profile_context", lambda profile: (home, profile))
    monkeypatch.setattr(supervisor, "_resolve_hermes_command", lambda: "hermes")
    made = []
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda args, **kw: made.append(FakeProcess(args, **kw)) or made[-1])
    supervisor.start_profile_gateway("maverick")
    first = made[0]
    result = supervisor.restart_profile_gateway("maverick")
    assert first.terminated is True
    assert len(made) == 2
    assert result["status"] == "running"


def test_start_enabled_gateways_uses_only_explicit_opt_in(monkeypatch, tmp_path):
    _reset()
    enabled = tmp_path / "enabled"
    disabled = tmp_path / "disabled"
    enabled.mkdir(); disabled.mkdir()
    (enabled / ".env").write_text("HERMES_WEBUI_MATRIX_GATEWAY_ENABLED=1\n")
    (disabled / ".env").write_text("HERMES_WEBUI_MATRIX_GATEWAY_ENABLED=0\n")
    monkeypatch.setattr(supervisor, "list_profiles_api", lambda: [
        {"name": "enabled", "path": str(enabled)},
        {"name": "disabled", "path": str(disabled)},
    ])
    started = []
    monkeypatch.setattr(supervisor, "start_profile_gateway", lambda p, **kw: started.append(p) or {"status": "running"})
    supervisor.start_enabled_gateways()
    assert started == ["enabled"]


def test_status_does_not_expose_command_env_or_pid(monkeypatch, tmp_path):
    _reset()
    home = tmp_path / "maverick"; home.mkdir()
    monkeypatch.setattr(supervisor, "_gateway_restart_profile_context", lambda profile: (home, profile))
    monkeypatch.setattr(supervisor, "_resolve_hermes_command", lambda: "hermes")
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda args, **kw: FakeProcess(args, **kw))
    supervisor.start_profile_gateway("maverick")
    result = supervisor.get_profile_gateway_status("maverick")
    assert result == {"profile": "maverick", "status": "running", "managed": True}


def test_child_env_scrubs_parent_secrets_and_applies_selected_profile(monkeypatch, tmp_path):
    home = tmp_path / "maverick"
    home.mkdir()
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "wrong-profile-token")
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-key")
    monkeypatch.setenv("HERMES_WEBUI_AUTH_PASSWORD", "operator-password")
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setattr(supervisor, "_profile_secret_env_names", lambda _home: {"OPENAI_API_KEY"})
    monkeypatch.setattr(
        supervisor,
        "get_profile_runtime_env",
        lambda _home: {
            "MATRIX_ACCESS_TOKEN": "maverick-token",
            "OPENAI_API_KEY": "maverick-provider-key",
        },
    )
    monkeypatch.setattr(supervisor, "filter_runtime_env_for_gateway_parity", lambda env: env)

    env = supervisor._profile_child_env(home)

    assert env["MATRIX_ACCESS_TOKEN"] == "maverick-token"
    assert env["OPENAI_API_KEY"] == "maverick-provider-key"
    assert "HERMES_WEBUI_AUTH_PASSWORD" not in env
    assert env["PATH"] == "/safe/bin"
    assert env["HERMES_HOME"] == str(home)


def test_default_aliases_share_resolved_home_process(monkeypatch, tmp_path):
    _reset()
    home = tmp_path / "default"
    home.mkdir()
    alias = tmp_path / "default-alias"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setattr(
        supervisor,
        "_gateway_restart_profile_context",
        lambda profile: (alias if profile == "default" else home, None if profile == "default" else profile),
    )
    monkeypatch.setattr(supervisor, "_resolve_hermes_command", lambda: "hermes")
    made = []
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda args, **kw: made.append(FakeProcess(args, **kw)) or made[-1],
    )

    supervisor.start_profile_gateway("default")
    result = supervisor.start_profile_gateway("root")

    assert result == {"profile": "root", "status": "running", "managed": True}
    assert len(made) == 1


def test_stop_terminates_process_group_then_kills_after_timeout(monkeypatch):
    proc = FakeProcess([])
    waits = []
    signals = []

    def wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            raise supervisor.subprocess.TimeoutExpired([], timeout)
        proc.returncode = -9
        return proc.returncode

    proc.wait = wait
    monkeypatch.setattr(supervisor.os, "name", "posix")
    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    supervisor._terminate_process(proc)

    assert signals == [
        (proc.pid, supervisor.signal.SIGTERM),
        (proc.pid, supervisor.signal.SIGKILL),
    ]
    assert waits == [10, 5]
    assert proc.terminated is False
    assert proc.killed is False


def test_stop_uses_portable_process_fallback(monkeypatch):
    proc = FakeProcess([])
    monkeypatch.setattr(supervisor.os, "name", "nt")

    supervisor._terminate_process(proc)

    assert proc.terminated is True


def test_fatal_exit_78_stays_failed_without_monitor_restart(monkeypatch, tmp_path):
    _reset()
    home = tmp_path / "maverick"
    home.mkdir()
    (home / ".env").write_text("HERMES_WEBUI_MATRIX_GATEWAY_ENABLED=1\n")
    monkeypatch.setattr(supervisor, "_gateway_restart_profile_context", lambda profile: (home, profile))
    monkeypatch.setattr(supervisor, "_resolve_hermes_command", lambda: "hermes")
    made = []

    def popen(args, **kwargs):
        proc = FakeProcess(args, **kwargs)
        proc.returncode = 78
        made.append(proc)
        return proc

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)

    first = supervisor.start_profile_gateway("maverick", require_enabled=True)
    retried = supervisor.start_profile_gateway("maverick", require_enabled=True)

    assert first["status"] == "failed"
    assert retried["status"] == "failed"
    assert len(made) == 1


def test_transient_failure_respects_monitor_backoff(monkeypatch, tmp_path):
    _reset()
    home = tmp_path / "maverick"
    home.mkdir()
    (home / ".env").write_text("HERMES_WEBUI_MATRIX_GATEWAY_ENABLED=1\n")
    now = [100.0]
    monkeypatch.setattr(supervisor, "_gateway_restart_profile_context", lambda profile: (home, profile))
    monkeypatch.setattr(supervisor, "_resolve_hermes_command", lambda: "hermes")
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now[0])
    made = []

    def popen(args, **kwargs):
        proc = FakeProcess(args, **kwargs)
        if not made:
            proc.returncode = 1
        made.append(proc)
        return proc

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)

    assert supervisor.start_profile_gateway("maverick", require_enabled=True)["status"] == "failed"
    now[0] += 29
    assert supervisor.start_profile_gateway("maverick", require_enabled=True)["status"] == "failed"
    assert len(made) == 1
    now[0] += 1
    assert supervisor.start_profile_gateway("maverick", require_enabled=True)["status"] == "running"
    assert len(made) == 2
