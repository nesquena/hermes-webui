import io
import subprocess
import types
from api import routes


class MockPopen:
    def __init__(self, args, stdout=None, stderr=None, text=True, env=None):
        self.args = args
        self.env = env
        self.returncode = 0
        self.stdout = io.StringIO("✓ Service restarted")
        self.stderr = io.StringIO("")
        self._should_timeout = False
        self._should_raise = False

    def communicate(self, timeout=None):
        if self._should_raise:
            raise OSError("Subprocess execution failed")
        if self._should_timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.stdout.getvalue(), self.stderr.getvalue()


def test_handle_health_restart_success(monkeypatch):
    # Mock profiles home path
    monkeypatch.setattr("api.routes.get_active_hermes_home", lambda: "/mock/hermes/home")

    # Mock shutil.which to find hermes CLI
    monkeypatch.setattr("shutil.which", lambda cmd: "/mock/bin/hermes" if cmd == "hermes" else None)

    called_args = []
    called_env = {}

    def mock_popen(args, stdout=None, stderr=None, text=True, env=None):
        called_args.append(args)
        called_env.update(env or {})
        return MockPopen(args, stdout, stderr, text, env)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    # Mock response helper j
    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True)

    handler = types.SimpleNamespace()

    # Call _handle_health_restart
    result = routes._handle_health_restart(handler)

    assert result is True
    assert called_args == [["/mock/bin/hermes", "gateway", "restart"]]
    assert called_env.get("HERMES_HOME") == "/mock/hermes/home"
    assert responses == [({"ok": True, "message": "Gateway service restarted successfully"}, 200)]


def test_handle_health_restart_failure(monkeypatch):
    # Mock profiles home path
    monkeypatch.setattr("api.routes.get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr("shutil.which", lambda cmd: "/mock/bin/hermes" if cmd == "hermes" else None)

    # Mock subprocess.Popen failure
    def mock_popen(args, stdout=None, stderr=None, text=True, env=None):
        mp = MockPopen(args, stdout, stderr, text, env)
        mp.returncode = 1
        mp.stdout = io.StringIO("")
        mp.stderr = io.StringIO("Error: something went wrong")
        return mp

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True)

    handler = types.SimpleNamespace()
    result = routes._handle_health_restart(handler)

    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: Error: something went wrong"}, 500)]


def test_handle_health_restart_exception(monkeypatch):
    # Mock profiles home path
    monkeypatch.setattr("api.routes.get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr("shutil.which", lambda cmd: "/mock/bin/hermes" if cmd == "hermes" else None)

    # Mock Popen raising OSError immediately on start
    def mock_popen(args, **kwargs):
        raise OSError("Subprocess execution failed")

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True)

    handler = types.SimpleNamespace()
    result = routes._handle_health_restart(handler)

    assert result is True
    assert responses[0][0]["ok"] is False
    assert "Internal error running restart" in responses[0][0]["error"]
    assert responses[0][1] == 500


def test_handle_health_restart_timeout(monkeypatch):
    # Mock profiles home path
    monkeypatch.setattr("api.routes.get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr("shutil.which", lambda cmd: "/mock/bin/hermes" if cmd == "hermes" else None)

    # Mock Popen raising TimeoutExpired on communicate
    def mock_popen(args, stdout=None, stderr=None, text=True, env=None):
        mp = MockPopen(args, stdout, stderr, text, env)
        mp._should_timeout = True
        return mp

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True)

    handler = types.SimpleNamespace()
    result = routes._handle_health_restart(handler)

    assert result is True
    assert responses == [({"ok": True, "message": "Gateway service restart initiated (in progress)"}, 200)]
