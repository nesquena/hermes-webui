from __future__ import annotations

import json
import subprocess
import sys
from urllib.parse import urlparse


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


def _fake_agent(tmp_path):
    agent_dir = tmp_path / "hermes-agent"
    cli_dir = agent_dir / "hermes_cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "main.py").write_text("print('fake hermes cli')\n", encoding="utf-8")
    return agent_dir


def _call_post(monkeypatch, path: str, body: dict | None = None):
    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body or {})
    handler = _FakeHandler()
    routes.handle_post(handler, urlparse(path))
    return handler, handler.get_json()


def test_gateway_start_runs_profile_scoped_agent_cli_and_returns_status(monkeypatch, tmp_path):
    from api import config, profiles, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(
        routes,
        "_gateway_status_payload",
        lambda: {
            "running": True,
            "configured": True,
            "platforms": [],
            "last_active": "",
            "session_count": 0,
            "health": {},
        },
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="gateway started\n", stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    handler, data = _call_post(monkeypatch, "/api/gateway/start", {"name": "telegram"})

    assert handler.status == 200
    assert data["ok"] is True
    assert data["action"] == "start"
    assert "stdout" not in data
    assert "stderr" not in data
    assert data["status"]["running"] is True
    cmd, kwargs = calls[0]
    assert cmd[:2] == [sys.executable, str(agent_dir / "hermes_cli" / "main.py")]
    assert cmd[2:] == ["--profile", "work", "gateway", "start"]
    assert kwargs["cwd"] == str(agent_dir)
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert kwargs["env"]["BROWSER"] == "echo"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_gateway_action_contention_returns_409_without_spawning(monkeypatch, tmp_path):
    """A second lifecycle action while one holds the lock returns 409 and does
    NOT spawn an overlapping `hermes gateway` subprocess (server-side
    single-flight guard)."""
    from api import config, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    spawned = []
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda cmd, **kw: spawned.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    # Simulate an in-flight action by holding the lock, then fire a second one.
    acquired = routes._GATEWAY_ACTION_LOCK.acquire(blocking=False)
    assert acquired, "lock should be free at test start"
    try:
        handler, data = _call_post(monkeypatch, "/api/gateway/restart", {})
    finally:
        routes._GATEWAY_ACTION_LOCK.release()

    assert handler.status == 409, f"expected 409 on contention, got {handler.status}"
    assert data["ok"] is False
    assert spawned == [], "no subprocess should be spawned while another action holds the lock"

    # After the lock is released, a normal action proceeds (lock is reusable).
    handler2, data2 = _call_post(monkeypatch, "/api/gateway/restart", {})
    assert handler2.status == 200
    assert len(spawned) == 1


def test_gateway_stop_failure_returns_bounded_error(monkeypatch, tmp_path):
    from api import config, profiles, routes
    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 7, stdout="partial output\n", stderr="stop failed\n")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    handler, data = _call_post(monkeypatch, "/api/gateway/stop")

    assert handler.status == 500
    assert data["ok"] is False
    assert data["action"] == "stop"
    assert data["returncode"] == 7
    assert "stdout" not in data
    assert "stderr" not in data
    assert data["error"] == "Gateway stop failed with exit code 7"


def test_gateway_restart_uses_restart_subcommand(monkeypatch, tmp_path):
    from api import config, profiles, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        routes,
        "_gateway_status_payload",
        lambda: {
            "running": True,
            "configured": True,
            "platforms": [],
            "last_active": "",
            "session_count": 0,
            "health": {},
        },
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="gateway restarted\n", stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    handler, data = _call_post(monkeypatch, "/api/gateway/restart")

    assert handler.status == 200
    assert data["ok"] is True
    assert data["action"] == "restart"
    assert calls[0][-2:] == ["gateway", "restart"]


def test_gateway_lifecycle_timeout_returns_gateway_timeout(monkeypatch, tmp_path):
    from api import config, profiles, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60, output="partial", stderr="slow")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    handler, data = _call_post(monkeypatch, "/api/gateway/restart")

    assert handler.status == 504
    assert data["ok"] is False
    assert data["action"] == "restart"
    assert data["error"] == "Gateway restart timed out after 60 seconds"
    assert "stdout" not in data
    assert "stderr" not in data


def test_gateway_lifecycle_invalid_action_returns_bad_request(monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "_run_gateway_lifecycle_command", lambda action: (_ for _ in ()).throw(ValueError("unsupported gateway action")))

    handler = _FakeHandler()
    routes._handle_gateway_lifecycle(handler, "bogus", {})
    data = handler.get_json()

    assert handler.status == 400
    assert data["error"] == "unsupported gateway action"


def test_gateway_lifecycle_missing_cli_returns_sanitized_error(monkeypatch, tmp_path):
    from api import config, profiles

    agent_dir = tmp_path / "missing-agent"
    agent_dir.mkdir()
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    handler, data = _call_post(monkeypatch, "/api/gateway/start")

    assert handler.status == 500
    assert data["ok"] is False
    assert data["action"] == "start"
    assert data["error"] == "Hermes agent CLI entrypoint not found"


def test_gateway_lifecycle_frontend_renders_valid_actions():
    from pathlib import Path

    panels = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    assert "api(`/api/gateway/${encodeURIComponent(action)}`" in panels
    assert "timeoutMs:70000" in panels
    assert "timeoutToast:false" in panels
    assert "(r&&r.running)?['stop','restart']:['start']" in panels
    assert "document.querySelectorAll('.gateway-action-btn')" in panels
    assert "await loadGatewayStatus();" in panels


def test_gateway_lifecycle_i18n_keys_exist():
    from pathlib import Path

    i18n = (Path(__file__).resolve().parents[1] / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in (
        "gateway_start",
        "gateway_stop",
        "gateway_restart",
        "gateway_start_failed",
        "gateway_stop_failed",
        "gateway_restart_failed",
    ):
        assert f"{key}:" in i18n
