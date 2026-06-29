"""Health route and shared gateway restart helper checks."""

import io
import subprocess
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
    assert called["args"] == ["/mock/bin/hermes", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/home"
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


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
