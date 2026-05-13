"""Regression coverage for #716 Hermes agent/gateway heartbeat monitor."""

from __future__ import annotations

import json
import pathlib
import sys
import types

REPO_ROOT = pathlib.Path(__file__).parent.parent

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


class _FakeGatewayStatus:
    def __init__(self, runtime_status, running_pid):
        self._runtime_status = runtime_status
        self._running_pid = running_pid

    def read_runtime_status(self):
        return self._runtime_status

    def get_running_pid(self, cleanup_stale=False):
        assert cleanup_stale is False
        return self._running_pid


class _PathSensitiveGatewayStatus:
    _RUNTIME_STATUS_FILE = "gateway_state.json"

    def __init__(self, root_home: pathlib.Path):
        self.root_home = root_home
        self.runtime_pid_path = None
        self.running_pid_path = None

    def read_runtime_status(self, pid_path=None):
        self.runtime_pid_path = pathlib.Path(pid_path) if pid_path is not None else None
        if self.runtime_pid_path:
            base = self.runtime_pid_path.parent
        else:
            base = self.root_home / "profiles" / "troubleshooting"
        path = base / self._RUNTIME_STATUS_FILE
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def get_running_pid(self, pid_path=None, cleanup_stale=False):
        assert cleanup_stale is False
        self.running_pid_path = pathlib.Path(pid_path) if pid_path is not None else None
        if self.running_pid_path == self.root_home / "gateway.pid":
            return 98765
        return None


def _runtime_status(**overrides):
    payload = {
        "gateway_state": "running",
        "updated_at": "2026-05-04T12:00:00+00:00",
        "active_agents": 2,
        "platforms": {
            "discord": {"state": "connected"},
            "telegram": {"state": "starting"},
        },
        # Sensitive/raw process fields that must never reach the browser.
        "pid": 12345,
        "argv": ["hermes", "gateway", "--token", "secret-token"],
        "command": "hermes gateway --token secret-token",
        "executable": "/home/user/.hermes/hermes-agent/venv/bin/python",
        "env": {"API_KEY": "secret"},
    }
    payload.update(overrides)
    return payload


def test_agent_health_uses_root_gateway_state_when_hermes_home_is_profile(monkeypatch, tmp_path):
    from api import agent_health

    root_home = tmp_path / "root-home"
    profile_home = root_home / "profiles" / "troubleshooting"
    profile_home.mkdir(parents=True)
    (root_home / "gateway.pid").write_text(json.dumps({"pid": 98765}), encoding="utf-8")
    (root_home / "gateway_state.json").write_text(json.dumps(_runtime_status()), encoding="utf-8")
    fake_gateway_status = _PathSensitiveGatewayStatus(root_home)

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(get_default_hermes_root=lambda: root_home),
    )
    monkeypatch.setattr(agent_health, "_gateway_status_module", lambda: fake_gateway_status)

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["details"]["state"] == "alive"
    assert fake_gateway_status.runtime_pid_path == root_home / "gateway.pid"
    assert fake_gateway_status.running_pid_path == root_home / "gateway.pid"


def test_agent_health_payload_alive_uses_safe_runtime_details(monkeypatch):
    from api import agent_health

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(_runtime_status(), running_pid=12345),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is True
    assert payload["checked_at"]
    assert payload["details"] == {
        "state": "alive",
        "gateway_state": "running",
        "updated_at": "2026-05-04T12:00:00+00:00",
        "active_agents": 2,
        "platform_count": 2,
        "platform_states": {"connected": 1, "starting": 1},
    }
    rendered = repr(payload)
    assert "secret-token" not in rendered
    assert "API_KEY" not in rendered
    assert "argv" not in rendered
    assert "command" not in rendered
    assert "executable" not in rendered
    assert "pid" not in payload["details"]


def test_agent_health_payload_down_when_gateway_metadata_exists_but_no_process(monkeypatch):
    from api import agent_health

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(_runtime_status(gateway_state="stale"), running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is False
    assert payload["details"]["state"] == "down"
    assert payload["details"]["reason"] == "gateway_not_running"
    assert payload["details"]["gateway_state"] == "stale"


def test_agent_health_payload_unknown_when_gateway_is_not_configured(monkeypatch):
    from api import agent_health

    monkeypatch.setattr(
        agent_health,
        "_gateway_status_module",
        lambda: _FakeGatewayStatus(runtime_status=None, running_pid=None),
    )

    payload = agent_health.build_agent_health_payload()

    assert payload["alive"] is None
    assert payload["details"] == {"state": "unknown", "reason": "gateway_not_configured"}


def test_agent_health_route_is_registered_with_tri_state_payload_shape():
    assert 'parsed.path == "/api/health/agent"' in ROUTES_PY
    assert "build_agent_health_payload()" in ROUTES_PY
    src = (REPO_ROOT / "api" / "agent_health.py").read_text(encoding="utf-8")
    assert '"alive"' in src
    assert '"checked_at"' in src
    assert '"details"' in src


def test_agent_health_banner_markup_and_styles_exist():
    assert 'id="agentHealthBanner"' in INDEX_HTML
    assert 'role="alert"' in INDEX_HTML
    assert 'aria-live="assertive"' in INDEX_HTML
    assert 'onclick="dismissAgentHealthAlert()"' in INDEX_HTML
    assert ".agent-health-banner" in STYLE_CSS
    assert ".agent-health-banner.visible" in STYLE_CSS
    assert ".agent-health-dismiss" in STYLE_CSS


def test_agent_health_frontend_polls_only_visible_and_distinguishes_states():
    assert "const AGENT_HEALTH_INTERVAL_MS=30000" in UI_JS
    assert "api('/api/health/agent')" in UI_JS
    assert "document.visibilityState !== 'visible'" in UI_JS
    assert "document.addEventListener('visibilitychange',_syncAgentHealthMonitorVisibility)" in UI_JS
    assert "if(payload.alive === true)" in UI_JS
    assert "if(payload.alive === false)" in UI_JS
    assert "if(payload.alive == null)" in UI_JS
    assert "_showAgentHealthAlert(payload)" in UI_JS
    assert "_hideAgentHealthAlert()" in UI_JS


def test_agent_health_dismiss_persists_until_recovery():
    assert "const AGENT_HEALTH_DISMISSED_KEY='agent-health-dismissed'" in UI_JS
    assert "localStorage.setItem(AGENT_HEALTH_DISMISSED_KEY,'1')" in UI_JS
    assert "localStorage.removeItem(AGENT_HEALTH_DISMISSED_KEY)" in UI_JS
    assert "function dismissAgentHealthAlert()" in UI_JS
    assert "if(_agentHealthDismissed()) return;" in UI_JS
    assert "_setAgentHealthDismissed(false)" in UI_JS


def test_agent_health_backend_does_not_use_shell_or_expose_raw_process_fields():
    src = (REPO_ROOT / "api" / "agent_health.py").read_text(encoding="utf-8")
    assert "import subprocess" not in src
    assert "import psutil" not in src
    for private_field in ("argv", "command", "executable", "env"):
        assert f'details["{private_field}"]' not in src
        assert f"details['{private_field}']" not in src
