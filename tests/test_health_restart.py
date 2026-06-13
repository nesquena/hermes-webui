import subprocess
import types
from api import routes


def test_handle_health_restart_success(monkeypatch):
    # Mock profiles home path
    monkeypatch.setattr("api.routes.get_active_hermes_home", lambda: "/mock/hermes/home")

    # Mock shutil.which to find hermes CLI
    monkeypatch.setattr("shutil.which", lambda cmd: "/mock/bin/hermes" if cmd == "hermes" else None)

    # Mock subprocess.run
    called_args = []
    called_env = {}

    def mock_run(args, capture_output=True, text=True, env=None, timeout=None):
        called_args.append(args)
        called_env.update(env or {})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="✓ Service restarted", stderr="")

    monkeypatch.setattr(subprocess, "run", mock_run)

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

    # Mock subprocess.run failure
    def mock_run(args, capture_output=True, text=True, env=None, timeout=None):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="Error: something went wrong")

    monkeypatch.setattr(subprocess, "run", mock_run)

    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True)

    handler = types.SimpleNamespace()
    result = routes._handle_health_restart(handler)

    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: Error: something went wrong"}, 500)]


def test_handle_health_restart_exception(monkeypatch):
    # Mock profiles home path
    monkeypatch.setattr("api.routes.get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    # Mock subprocess.run raising exception
    def mock_run(args, **kwargs):
        raise OSError("Subprocess execution failed")

    monkeypatch.setattr(subprocess, "run", mock_run)

    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True)

    handler = types.SimpleNamespace()
    result = routes._handle_health_restart(handler)

    assert result is True
    assert responses[0][0]["ok"] is False
    assert "Internal error running restart" in responses[0][0]["error"]
    assert responses[0][1] == 500
