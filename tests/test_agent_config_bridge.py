"""Tests for api/agent_config_bridge.py — the shared agent-config write path.

The bridge routes MCP/skills config writes through the agent's own
persistence layer (comment-preserving, security-validated, secrets to .env)
when an agent checkout is importable, and falls back to the legacy WebUI
writer in standalone deployments.

These tests never import a real hermes-agent checkout: the agent modules
(``hermes_constants``, ``hermes_cli.config``, ``hermes_cli.mcp_config``,
``hermes_cli.mcp_security``) are faked in ``sys.modules`` so behavior is
identical on CI (no checkout) and developer machines (real checkout present).
"""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from api import agent_config_bridge as bridge


class FakeAgent:
    """Builds fake agent modules and records calls against them."""

    def __init__(self):
        self.saved_configs = []
        self.env_values = {}
        self.config_store = {}
        self.override_calls = []

        hermes_constants = types.ModuleType("hermes_constants")

        def set_hermes_home_override(path):
            self.override_calls.append(("set", str(path)))
            return object()

        def reset_hermes_home_override(token):
            self.override_calls.append(("reset", token))

        hermes_constants.set_hermes_home_override = set_hermes_home_override
        hermes_constants.reset_hermes_home_override = reset_hermes_home_override

        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []  # mark as package

        config_mod = types.ModuleType("hermes_cli.config")
        config_mod.load_config = lambda: {k: v for k, v in self.config_store.items()}
        config_mod.save_config = self._save_config
        config_mod.save_env_value = self._save_env_value

        security_mod = types.ModuleType("hermes_cli.mcp_security")
        security_mod.validate_mcp_server_entry = lambda name, entry: (
            ["suspicious command"] if entry.get("command") == "evil" else []
        )

        mcp_mod = types.ModuleType("hermes_cli.mcp_config")

        def _save_bearer_auth_token(name, token):
            if not str(token).strip():
                raise ValueError("Bearer token is required")
            key = f"MCP_{name.upper().replace('-', '_')}_API_KEY"
            self._save_env_value(key, token)
            return {"Authorization": f"Bearer ${{{key}}}"}

        mcp_mod._save_bearer_auth_token = _save_bearer_auth_token

        self.modules = {
            "hermes_constants": hermes_constants,
            "hermes_cli": hermes_cli,
            "hermes_cli.config": config_mod,
            "hermes_cli.mcp_security": security_mod,
            "hermes_cli.mcp_config": mcp_mod,
        }

    def _save_config(self, config, **kwargs):
        self.saved_configs.append(dict(config))
        self.config_store = dict(config)

    def _save_env_value(self, key, value):
        self.env_values[key] = value


@pytest.fixture
def fake_agent(monkeypatch, tmp_path):
    """Activate the bridge against a fully faked agent checkout."""
    fake = FakeAgent()
    monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
    for name, module in fake.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)
    monkeypatch.setattr(bridge, "_import_state", None, raising=False)
    yield fake
    bridge._import_state = None


@pytest.fixture
def bridge_unavailable(monkeypatch, tmp_path):
    """Agent checkout configured but import broken → bridge must fail closed."""
    monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
    for name in ("hermes_constants", "hermes_cli", "hermes_cli.config"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "missing-agent"), raising=False)
    monkeypatch.setattr(bridge, "_import_state", None, raising=False)
    # Block real imports of agent modules even when a checkout exists on the
    # machine: an import hook that rejects exactly these module names.
    class _Blocker:
        def find_module(self, fullname, path=None):
            if fullname in ("hermes_constants", "hermes_cli", "hermes_cli.config"):
                return self
            return None

        def find_spec(self, fullname, path=None, target=None):
            if fullname in ("hermes_constants", "hermes_cli", "hermes_cli.config"):
                raise ImportError(f"{fullname} blocked by test")
            return None

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    yield
    sys.meta_path.remove(blocker)
    bridge._import_state = None


class TestProbe:
    def test_kill_switch_forces_standalone(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", "1")
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        assert bridge.bridge_available() is False
        # Kill-switch must NOT raise even with a configured agent dir.
        monkeypatch.setattr(bridge, "_AGENT_DIR", "/tmp/x", raising=False)
        bridge.require_bridge()
        bridge._import_state = None

    def test_no_agent_dir_is_silent_standalone(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
        monkeypatch.setattr(bridge, "_AGENT_DIR", None, raising=False)
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        assert bridge.bridge_available() is False
        bridge.require_bridge()  # must not raise
        bridge._import_state = None

    def test_broken_checkout_fails_closed(self, bridge_unavailable):
        assert bridge.bridge_available() is False
        with pytest.raises(bridge.AgentBridgeUnavailable):
            bridge.require_bridge()

    def test_fake_agent_probes_ok(self, fake_agent):
        assert bridge.bridge_available() is True
        bridge.require_bridge()


class TestMcpWrites:
    def test_save_valid_server(self, fake_agent, tmp_path):
        issues = bridge.save_mcp_server("srv", {"url": "https://x.test/mcp"}, tmp_path)
        assert issues == []
        assert fake_agent.saved_configs[-1]["mcp_servers"]["srv"] == {"url": "https://x.test/mcp"}
        # Home scoping happened around the write and was reset afterwards.
        assert fake_agent.override_calls[0] == ("set", str(tmp_path))
        assert fake_agent.override_calls[-1][0] == "reset"

    def test_save_rejects_suspicious_entry(self, fake_agent, tmp_path):
        issues = bridge.save_mcp_server("srv", {"command": "evil"}, tmp_path)
        assert issues == ["suspicious command"]
        assert fake_agent.saved_configs == []

    def test_remove_and_toggle(self, fake_agent, tmp_path):
        fake_agent.config_store = {"mcp_servers": {"a": {"url": "https://a"}, "b": {"url": "https://b"}}}
        assert bridge.set_mcp_server_enabled("a", False, tmp_path) is True
        assert fake_agent.config_store["mcp_servers"]["a"]["enabled"] is False
        assert bridge.remove_mcp_server("a", tmp_path) is True
        assert "a" not in fake_agent.config_store["mcp_servers"]
        assert bridge.remove_mcp_server("missing", tmp_path) is False
        # Removing the last server drops the whole key.
        assert bridge.remove_mcp_server("b", tmp_path) is True
        assert "mcp_servers" not in fake_agent.config_store

    def test_bearer_token_goes_to_env_not_yaml(self, fake_agent, tmp_path):
        headers = bridge.save_mcp_bearer_token("my-srv", "secret-token", tmp_path)
        assert headers == {"Authorization": "Bearer ${MCP_MY_SRV_API_KEY}"}
        assert fake_agent.env_values == {"MCP_MY_SRV_API_KEY": "secret-token"}


class TestSkillsWrites:
    def test_save_skills_config(self, fake_agent, tmp_path):
        fake_agent.config_store = {"skills": {"disabled": []}, "other": 1}
        bridge.save_skills_config({"disabled": ["x"]}, tmp_path)
        assert fake_agent.config_store["skills"] == {"disabled": ["x"]}
        assert fake_agent.config_store["other"] == 1


class TestRouteIntegration:
    """Handlers pick bridge vs legacy vs fail-closed correctly."""

    def _handler(self):
        handler = MagicMock()
        handler.headers = {}
        return handler

    def test_mcp_update_uses_bridge_and_validates(self, fake_agent, tmp_path, monkeypatch):
        from api import routes

        monkeypatch.setattr(routes, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(routes, "reload_config", lambda: None)
        monkeypatch.setattr(routes, "get_config", lambda: dict(fake_agent.config_store))
        captured = {}

        def fake_j(handler, payload, status=200):
            captured["payload"], captured["status"] = payload, status
            return True

        monkeypatch.setattr(routes, "j", fake_j)
        routes._handle_mcp_server_update(self._handler(), "srv", {"url": "https://x.test/mcp"})
        assert captured["status"] == 200
        assert fake_agent.saved_configs[-1]["mcp_servers"]["srv"]["url"] == "https://x.test/mcp"

        routes._handle_mcp_server_update(self._handler(), "srv", {"command": "evil"})
        assert captured["status"] == 400
        assert captured["payload"]["issues"] == ["suspicious command"]

    def test_mcp_update_bearer_token_lands_in_env(self, fake_agent, tmp_path, monkeypatch):
        from api import routes

        monkeypatch.setattr(routes, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(routes, "reload_config", lambda: None)
        monkeypatch.setattr(routes, "get_config", lambda: dict(fake_agent.config_store))
        captured = {}

        def fake_j(handler, payload, status=200):
            captured["payload"], captured["status"] = payload, status
            return True

        monkeypatch.setattr(routes, "j", fake_j)
        routes._handle_mcp_server_update(
            self._handler(), "hub", {"url": "https://hub.test/mcp", "bearer_token": "tok-123"}
        )
        assert captured["status"] == 200
        saved = fake_agent.saved_configs[-1]["mcp_servers"]["hub"]
        assert saved["headers"] == {"Authorization": "Bearer ${MCP_HUB_API_KEY}"}
        assert fake_agent.env_values["MCP_HUB_API_KEY"] == "tok-123"
        # The raw secret must never appear in the YAML-bound server config.
        assert "tok-123" not in str(saved)

    def test_mcp_write_fails_closed_when_bridge_broken(self, bridge_unavailable, monkeypatch):
        from api import routes

        captured = {}

        def fake_j(handler, payload, status=200):
            captured["payload"], captured["status"] = payload, status
            return True

        monkeypatch.setattr(routes, "j", fake_j)
        routes._handle_mcp_server_delete(self._handler(), "any")
        assert captured["status"] == 503
        assert "unavailable" in captured["payload"]["error"].lower()

    def test_legacy_path_when_standalone(self, monkeypatch, tmp_path):
        """No agent checkout at all → the pre-bridge writer keeps working."""
        from api import routes

        monkeypatch.setattr(bridge, "_AGENT_DIR", None, raising=False)
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        cfg = {"mcp_servers": {"old": {"url": "https://old"}}}
        monkeypatch.setattr(routes, "get_config", lambda: cfg)
        monkeypatch.setattr(routes, "_get_config_path", lambda: tmp_path / "config.yaml")
        saved = {}
        monkeypatch.setattr(routes, "_save_yaml_config_file", lambda path, data: saved.update(data))
        monkeypatch.setattr(routes, "reload_config", lambda: None)
        captured = {}

        def fake_j(handler, payload, status=200):
            captured["payload"], captured["status"] = payload, status
            return True

        monkeypatch.setattr(routes, "j", fake_j)
        routes._handle_mcp_server_delete(self._handler(), "old")
        assert captured["status"] == 200
        assert saved["mcp_servers"] == {}
        bridge._import_state = None
