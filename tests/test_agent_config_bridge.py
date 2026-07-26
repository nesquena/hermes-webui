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
from unittest.mock import MagicMock

import pytest

from api import agent_config_bridge as bridge
from tests.agent_bridge_fakes import activate_fake_agent


@pytest.fixture
def fake_agent(monkeypatch, tmp_path):
    """Activate the bridge against a fully faked agent checkout."""
    fake = activate_fake_agent(monkeypatch, tmp_path)
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


# ── Re-gate 2026-07-25: lossless edit, revision, profile-scoped secrets ─────


def test_an_edit_preserves_fields_the_form_does_not_own():
    """Blocker 3: the handler built a fresh mapping and replaced the entry.

    The form sends neither `enabled` nor `connect_timeout`, so editing a
    disabled server silently re-enabled it, dropped its connect timeout, and
    discarded any key this WebUI version has never heard of.
    """
    from api.routes import _merge_mcp_entry

    stored = {
        "url": "https://old.test/mcp",
        "enabled": False,
        "connect_timeout": 45,
        "headers": {"Authorization": "Bearer ${MCP_X_API_KEY}"},
        "some_future_agent_key": {"nested": True},
    }
    form = {"url": "https://new.test/mcp", "timeout": 120}

    merged = _merge_mcp_entry(stored, form)

    assert merged["url"] == "https://new.test/mcp", "the form's own field must win"
    assert merged["timeout"] == 120
    assert merged["enabled"] is False, "editing re-enabled a disabled server"
    assert merged["connect_timeout"] == 45, "connect_timeout was dropped"
    assert merged["some_future_agent_key"] == {"nested": True}, "unknown keys were discarded"
    # The form owns headers, so an edit that sends none clears them rather than
    # resurrecting the old ones — that is the field's semantics, not a loss.
    assert "headers" not in merged


def test_a_revision_changes_when_the_entry_changes():
    """The concurrency token has to actually track content."""
    from api.routes import _mcp_entry_revision

    a = {"url": "https://x.test/mcp", "timeout": 60}
    assert _mcp_entry_revision(a) == _mcp_entry_revision(dict(a))
    assert _mcp_entry_revision(a) == _mcp_entry_revision({"timeout": 60, "url": "https://x.test/mcp"})
    assert _mcp_entry_revision(a) != _mcp_entry_revision({**a, "timeout": 61})
    assert _mcp_entry_revision({}) == _mcp_entry_revision(None)


def test_a_bearer_write_does_not_leak_into_the_process_environment(monkeypatch, tmp_path):
    """Blocker 4: same-named servers crossed profile credential authority.

    The agent derives the env key from the server NAME alone and its
    save_env_value() writes os.environ, which the home ContextVar does not
    scope. Two profiles with a server called `shared` therefore had profile B's
    token become the value profile A's config expanded.
    """
    import os as _os

    from api import agent_config_bridge as bridge

    monkeypatch.delenv("MCP_SHARED_API_KEY", raising=False)
    with bridge.process_env_restored():
        _os.environ["MCP_SHARED_API_KEY"] = "profile-b-token"
    assert "MCP_SHARED_API_KEY" not in _os.environ, (
        "a secret written for one profile stayed in the process environment"
    )

    monkeypatch.setenv("MCP_SHARED_API_KEY", "profile-a-token")
    with bridge.process_env_restored():
        _os.environ["MCP_SHARED_API_KEY"] = "profile-b-token"
    assert _os.environ["MCP_SHARED_API_KEY"] == "profile-a-token", (
        "profile B's token replaced profile A's in the process environment"
    )


def test_the_bearer_writer_restores_the_process_environment():
    """The scoping has to be wired into the real writer, not just available."""
    import inspect

    from api import agent_config_bridge as bridge

    src = inspect.getsource(bridge.save_mcp_bearer_token)
    assert "process_env_restored()" in src
