"""Tests for api.agent_config_bridge — the agent persistence bridge module.

These tests fake the hermes_cli.config and hermes_constants modules in
sys.modules so they work identically on CI (no agent checkout) and dev
machines (real checkout) without importing a real agent.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from api import agent_config_bridge as bridge

# ── Test fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def fake_agent_modules(monkeypatch, tmp_path):
    """Install fake hermes_cli.config + hermes_constants in sys.modules.

    Returns the mutable state dict so tests can inspect what was saved.
    """
    state = {"config": {}, "saved_configs": [], "env_values": {}, "home_override_stack": []}

    hermes_constants = types.ModuleType("hermes_constants")

    def _set_hermes_home_override(path):
        state["home_override_stack"].append(Path(path))
        return object()

    def _reset_hermes_home_override(token):
        if state["home_override_stack"]:
            state["home_override_stack"].pop()
        return None

    hermes_constants.set_hermes_home_override = _set_hermes_home_override
    hermes_constants.reset_hermes_home_override = _reset_hermes_home_override

    config_mod = types.ModuleType("hermes_cli.config")

    def _load_config():
        return {k: v for k, v in state["config"].items()}

    def _save_config(config, **kwargs):
        state["config"] = dict(config)
        state["saved_configs"].append(dict(config))

    def _save_env_value(key, value):
        state["env_values"][key] = value

    config_mod.load_config = _load_config
    config_mod.save_config = _save_config
    config_mod.save_env_value = _save_env_value

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []

    # Suppress the environment disable env var that conftest sets
    monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)
    monkeypatch.setattr(bridge, "_import_state", None, raising=False)

    try:
        yield state
    finally:
        bridge._import_state = None
        # Tear down in reverse order
        for mod in ("hermes_cli.config", "hermes_cli", "hermes_constants"):
            monkeypatch.delitem(sys.modules, mod, raising=False)


@pytest.fixture
def fake_security_modules(monkeypatch):
    """Install fake hermes_cli.mcp_security module."""
    security_mod = types.ModuleType("hermes_cli.mcp_security")

    def _validate_mcp_server_entry(name, entry):
        return []

    security_mod.validate_mcp_server_entry = _validate_mcp_server_entry
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_security", security_mod)


@pytest.fixture
def fake_mcp_config_modules(monkeypatch, tmp_path):
    """Install fake hermes_cli.mcp_config module."""
    state = {"bearer_tokens": {}}

    mcp_mod = types.ModuleType("hermes_cli.mcp_config")

    def _save_bearer_auth_token(name, token):
        state["bearer_tokens"][name] = token
        env_key = f"MCP_{name.upper().replace('-', '_')}_API_KEY"
        return {"Authorization": f"Bearer ${{{env_key}}}"}

    mcp_mod._save_bearer_auth_token = _save_bearer_auth_token
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_config", mcp_mod)
    yield state


# ── Probe / availability tests ───────────────────────────────────────────────


class TestBridgeProbe:
    def test_standalone_mode_is_not_available(self, monkeypatch):
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        monkeypatch.setattr(bridge, "_AGENT_DIR", None, raising=False)
        assert not bridge.bridge_available()
        assert bridge._probe_import() == "unavailable:no agent checkout discovered"

    def test_disabled_via_env_is_not_available(self, monkeypatch):
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        monkeypatch.setenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", "1")
        try:
            assert not bridge.bridge_available()
            assert "disabled" in bridge._probe_import()
        finally:
            bridge._import_state = None

    def test_agent_checkout_missing_modules_is_not_available(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
        monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)

        class _Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname in ("hermes_constants", "hermes_cli", "hermes_cli.config"):
                    raise ImportError(f"{fullname} blocked by test")
                return None

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        try:
            assert not bridge.bridge_available()
            assert "unavailable:" in bridge._probe_import()
        finally:
            sys.meta_path.remove(blocker)
            bridge._import_state = None

    def test_require_bridge_noop_standalone(self, monkeypatch):
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        monkeypatch.setattr(bridge, "_AGENT_DIR", None, raising=False)
        bridge.require_bridge()  # should not raise

    def test_require_bridge_noop_disable_env(self, monkeypatch):
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        monkeypatch.setenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", "1")
        try:
            bridge.require_bridge()  # should not raise
        finally:
            bridge._import_state = None

    def test_require_bridge_raises_on_broken_checkout(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
        monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)

        class _Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname in ("hermes_constants", "hermes_cli"):
                    raise ImportError(f"{fullname} blocked")
                return None

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        try:
            with pytest.raises(bridge.AgentBridgeUnavailable):
                bridge.require_bridge()
        finally:
            sys.meta_path.remove(blocker)
            bridge._import_state = None

    def test_keyboard_interrupt_during_probe_propagates(self, monkeypatch, tmp_path):
        """KeyboardInterrupt in the import probe must propagate, not be swallowed."""
        monkeypatch.setattr(bridge, "_import_state", None, raising=False)
        monkeypatch.delenv("HERMES_WEBUI_DISABLE_AGENT_CONFIG_BRIDGE", raising=False)
        monkeypatch.setattr(bridge, "_AGENT_DIR", str(tmp_path / "agent"), raising=False)

        class _RaisingFinder:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "hermes_constants":
                    raise KeyboardInterrupt()
                return None

        finder = _RaisingFinder()
        sys.meta_path.insert(0, finder)
        try:
            with pytest.raises(KeyboardInterrupt):
                bridge._probe_import()
        finally:
            sys.meta_path.remove(finder)
            bridge._import_state = None


# ── config.yaml persistence tests ────────────────────────────────────────────


class TestConfigPersistence:
    def test_load_agent_config(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        state["config"] = {"key": "value", "mcp_servers": {"srv": {}}}
        result = bridge.load_agent_config(tmp_path)
        assert result == state["config"]

    def test_save_agent_config(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        bridge.save_agent_config({"mcp_servers": {"new-srv": {"url": "http://x"}}}, tmp_path)
        assert state["config"]["mcp_servers"]["new-srv"]["url"] == "http://x"

    def test_scoped_agent_home_pushes_context_var(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        with bridge.scoped_agent_home(tmp_path):
            pass
        assert len(state["home_override_stack"]) == 0  # pushed then reset


# ── MCP server tests ─────────────────────────────────────────────────────────


class TestMcpBridgeOps:
    def test_save_mcp_server_valid(self, fake_agent_modules, fake_security_modules, tmp_path):
        state = fake_agent_modules
        result = bridge.save_mcp_server("test-srv", {"url": "http://x"}, tmp_path)
        assert result == []
        assert state["config"]["mcp_servers"]["test-srv"]["url"] == "http://x"

    def test_remove_mcp_server_existing(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        state["config"] = {"mcp_servers": {"srv": {"url": "http://x"}}}
        assert bridge.remove_mcp_server("srv", tmp_path) is True
        assert "mcp_servers" not in state["config"]

    def test_remove_mcp_server_missing(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        state["config"] = {"mcp_servers": {}}
        assert bridge.remove_mcp_server("nope", tmp_path) is False

    def test_set_mcp_server_enabled(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        state["config"] = {"mcp_servers": {"srv": {"url": "http://x", "enabled": False}}}
        assert bridge.set_mcp_server_enabled("srv", True, tmp_path) is True
        assert state["config"]["mcp_servers"]["srv"]["enabled"] is True

    def test_set_mcp_server_enabled_missing(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        state["config"] = {"mcp_servers": {}}
        assert bridge.set_mcp_server_enabled("nope", True, tmp_path) is False

    def test_save_mcp_bearer_token(self, fake_agent_modules, fake_mcp_config_modules, tmp_path):
        state = fake_mcp_config_modules
        result = bridge.save_mcp_bearer_token("web-srv", "secret-token", tmp_path)
        assert result == {"Authorization": "Bearer ${MCP_WEB_SRV_API_KEY}"}
        assert state["bearer_tokens"]["web-srv"] == "secret-token"


# ── Skills config tests ──────────────────────────────────────────────────────


class TestSkillsConfig:
    def test_save_skills_config(self, fake_agent_modules, tmp_path):
        state = fake_agent_modules
        skills_cfg = {
            "disabled": ["old-skill"],
            "platform_disabled": {"webui": ["web-skill"]},
        }
        bridge.save_skills_config(skills_cfg, tmp_path)
        assert state["config"]["skills"] == skills_cfg
