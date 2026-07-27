"""Health route and shared gateway restart helper checks."""

import io
import subprocess
import sys
import threading
import types

import api.gateway_restart as gateway_restart
import api.routes as routes


class MockPopen:
    def __init__(
        self,
        args,
        *,
        stdout_text="",
        stderr_text="",
        returncode=0,
        communicate_timeout=False,
        wait_timeout=False,
        env=None,
    ):
        self.args = args
        self.env = env or {}
        self.returncode = returncode
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.communicate_timeout = communicate_timeout
        self.wait_timeout = wait_timeout
        self.terminated = False
        self.killed = False
        self.communicate_timeout_arg = None
        self.wait_timeout_arg = None

    def communicate(self, timeout=None):
        self.communicate_timeout_arg = timeout
        if self.communicate_timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.stdout.getvalue(), self.stderr.getvalue()

    def wait(self, timeout=None):
        self.wait_timeout_arg = timeout
        if self.wait_timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class InlineThread:
    def __init__(self, *, target, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


def _call_health_restart(monkeypatch, helper_result):
    handler = types.SimpleNamespace()
    responses = []
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True,
    )
    monkeypatch.setattr(routes, "restart_active_profile_gateway", lambda: dict(helper_result))
    return routes._handle_health_restart(handler), responses


def test_restart_active_profile_gateway_success_uses_active_profile_home(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(
            args,
            stdout_text="✓ Service restarted",
            returncode=0,
            env=env,
        )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "completed"
    assert result["message"] == "Gateway service restarted successfully"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/home"
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_uses_source_cli_without_installed_launcher(monkeypatch, tmp_path):
    from api import config

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    agent_dir = tmp_path / "hermes-agent"
    main_py = agent_dir / "hermes_cli" / "main.py"
    main_py.parent.mkdir(parents=True)
    (main_py.parent / "__init__.py").write_text("", encoding="utf-8")
    main_py.write_text("print('fake hermes cli')\n", encoding="utf-8")
    python_exe = tmp_path / "venv" / "bin" / "python"
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None, cwd=None):
        called["args"] = args
        called["cwd"] = cwd
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", str(python_exe))
    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(gateway_restart.sys, "executable", str(python_exe))
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "completed"
    assert called["args"] == [
        str(python_exe),
        "-m",
        "hermes_cli.main",
        "--profile",
        "default",
        "gateway",
        "restart",
    ]
    assert called["cwd"] == str(agent_dir)
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_runs_source_cli_in_clean_venv(monkeypatch, tmp_path):
    from api import config

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    agent_dir = tmp_path / "hermes-agent"
    cli_dir = agent_dir / "hermes_cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "__init__.py").write_text("SENTINEL = 'source-cli-ok'\n", encoding="utf-8")
    (cli_dir / "main.py").write_text(
        "import importlib.metadata as metadata\n"
        "from hermes_cli import SENTINEL\n"
        "try:\n"
        "    metadata.version('hermes-agent')\n"
        "except metadata.PackageNotFoundError:\n"
        "    print(SENTINEL)\n"
        "else:\n"
        "    raise RuntimeError('hermes-agent distribution must be absent')\n",
        encoding="utf-8",
    )
    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
    )
    python_exe = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    hermes_exe = venv_dir / ("Scripts/hermes.exe" if sys.platform == "win32" else "bin/hermes")

    assert python_exe.exists()
    assert not hermes_exe.exists()

    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", str(python_exe))
    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(gateway_restart.sys, "executable", str(python_exe))
    monkeypatch.delenv("PYTHONPATH", raising=False)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "completed"
    assert result["detail"] == "source-cli-ok"
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_fails_closed_for_remote_gateway(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    monkeypatch.setenv("HERMES_API_URL", "http://hermes-agent:8642")

    def fail_popen(*args, **kwargs):
        raise AssertionError("remote gateway restart must not launch a local subprocess")

    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fail_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="work")

    assert result == {
        "status": "unsupported",
        "message": (
            "Gateway lifecycle control is unavailable for remote gateway deployments. "
            "Restart the hermes-agent service through its container supervisor."
        ),
        "error_code": "remote_gateway_control_unsupported",
    }
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_remote_gateway_restart_precedes_local_lock_and_profile_resolution(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://hermes-agent:8642")
    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: (_ for _ in ()).throw(RuntimeError("profile unavailable")),
    )

    assert gateway_restart._GATEWAY_RESTART_LOCK.acquire(blocking=False) is True
    try:
        result = gateway_restart.restart_active_profile_gateway(profile="work")
    finally:
        gateway_restart._GATEWAY_RESTART_LOCK.release()

    assert result["status"] == "unsupported"
    assert result["error_code"] == "remote_gateway_control_unsupported"


def test_restart_active_profile_gateway_pins_explicit_default_profile(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/default" if profile == "default" else "/mock/hermes/profiles/work",
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="default")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/default"


def test_restart_active_profile_gateway_omits_profile_for_isolated_default_home(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/profiles/default",
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="default")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/profiles/default"


def test_restart_active_profile_gateway_rejects_malformed_explicit_profile(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()

    def fail_popen(*args, **kwargs):
        raise AssertionError("malformed explicit profile must not launch subprocess")

    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fail_popen)

    for profile in ("", " default", "default ", "default\n", "../bad", "bad;echo"):
        result = gateway_restart.restart_active_profile_gateway(profile=profile)

        assert result["status"] == "failed"
        assert "Invalid profile for gateway restart" in result["message"]
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_accepts_renamed_root_alias(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/root" if profile == "rootalias" else "/mock/hermes/other",
    )
    monkeypatch.setattr(
        gateway_restart,
        "_is_root_profile",
        lambda profile: profile in {"default", "rootalias"},
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="rootalias")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/root"


def test_restart_active_profile_gateway_failure_preserves_empty_output_contract(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(
        gateway_restart.subprocess,
        "Popen",
        lambda args, stdout=None, stderr=None, text=True, env=None: MockPopen(
            args,
            returncode=7,
            env=env,
        ),
    )

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "failed"
    assert result["message"] == "Restart failed: "
    assert result["returncode"] == 7
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_timeout_releases_lock_after_background_wait(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = MockPopen(
        ["/mock/bin/hermes", "gateway", "restart"],
        communicate_timeout=True,
        env={"HERMES_HOME": "/mock/hermes/home"},
    )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.communicate_timeout_arg == 2.0
    assert proc.wait_timeout_arg == 240.0
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_busy_reports_contention(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    assert gateway_restart._GATEWAY_RESTART_LOCK.acquire(blocking=False) is True

    try:
        result = gateway_restart.restart_active_profile_gateway()
    finally:
        gateway_restart._GATEWAY_RESTART_LOCK.release()

    assert result == {
        "status": "busy",
        "message": "Restart already in progress. Please wait a moment and try again.",
    }


def test_handle_health_restart_success(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "completed", "message": "Gateway service restarted successfully"},
    )
    assert result is True
    assert responses == [({"ok": True, "message": "Gateway service restarted successfully"}, 200)]


def test_handle_health_restart_timeout(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "in_progress", "message": "Gateway service restart initiated (in progress)"},
    )
    assert result is True
    assert responses == [({"ok": True, "message": "Gateway service restart initiated (in progress)"}, 200)]


def test_handle_health_restart_failure_preserves_empty_output_message(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Restart failed: "},
    )
    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: "}, 500)]


def test_handle_health_restart_failure(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Restart failed: bad thing"},
    )
    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: bad thing"}, 500)]


def test_handle_health_restart_internal_error(monkeypatch):
    _, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Internal error running restart: OSError: bad spawn"},
    )
    assert responses == [({"ok": False, "error": "Internal error running restart: OSError: bad spawn"}, 500)]


def test_handle_health_restart_concurrency(monkeypatch):
    _, responses = _call_health_restart(
        monkeypatch,
        {"status": "busy", "message": "Restart already in progress. Please wait a moment and try again."},
    )
    assert responses == [
        (
            {"ok": False, "error": "Restart already in progress. Please wait a moment and try again."},
            429,
        )
    ]


def test_handle_health_restart_remote_gateway_unsupported(monkeypatch):
    _, responses = _call_health_restart(
        monkeypatch,
        {
            "status": "unsupported",
            "message": (
                "Gateway lifecycle control is unavailable for remote gateway deployments. "
                "Restart the hermes-agent service through its container supervisor."
            ),
            "error_code": "remote_gateway_control_unsupported",
        },
    )

    assert responses == [
        (
            {
                "ok": False,
                "error": (
                    "Gateway lifecycle control is unavailable for remote gateway deployments. "
                    "Restart the hermes-agent service through its container supervisor."
                ),
                "error_code": "remote_gateway_control_unsupported",
            },
            501,
        )
    ]


def test_handle_health_restart_remote_gateway_precedes_local_contention(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    monkeypatch.setenv("HERMES_API_URL", "http://hermes-agent:8642")
    responses = []
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True,
    )

    assert gateway_restart._GATEWAY_RESTART_LOCK.acquire(blocking=False) is True
    try:
        routes._handle_health_restart(types.SimpleNamespace())
    finally:
        gateway_restart._GATEWAY_RESTART_LOCK.release()

    assert responses[0][1] == 501
    assert responses[0][0]["error_code"] == "remote_gateway_control_unsupported"
