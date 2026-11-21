"""Unit tests for issue #8254:
Shell hooks and outbound webhooks registered from profile config.yaml in WebUI.
"""

import os
import sys
import types
from unittest.mock import MagicMock
import pytest

from api.config import register_profile_hooks


def test_register_profile_hooks_non_dict_noop():
    """Non-dict config should be a safe no-op without raising errors."""
    register_profile_hooks(None)
    register_profile_hooks("not a dict")
    register_profile_hooks(123)


def test_register_profile_hooks_calls_agent_registrations(monkeypatch):
    """Verify that both shell_hooks and outbound_webhooks register_from_config are called."""
    mock_shell_mod = types.ModuleType("agent.shell_hooks")
    mock_shell_register = MagicMock()
    mock_shell_mod.register_from_config = mock_shell_register

    mock_outbound_mod = types.ModuleType("agent.outbound_webhooks")
    mock_outbound_register = MagicMock()
    mock_outbound_mod.register_from_config = mock_outbound_register

    monkeypatch.setitem(sys.modules, "agent.shell_hooks", mock_shell_mod)
    monkeypatch.setitem(sys.modules, "agent.outbound_webhooks", mock_outbound_mod)

    cfg = {
        "hooks": [
            {"event": "post_llm_call", "command": "python test.py"},
        ]
    }

    register_profile_hooks(cfg)

    mock_shell_register.assert_called_once_with(cfg, accept_hooks=False)
    mock_outbound_register.assert_called_once_with(cfg)


def test_register_profile_hooks_scopes_hermes_home(monkeypatch, tmp_path):
    """Verify that profile_home temporarily scopes HERMES_HOME and restores it afterwards."""
    fake_profile_home = tmp_path / "custom_profile_home"
    fake_profile_home.mkdir()

    initial_home = "initial/home/path"
    monkeypatch.setenv("HERMES_HOME", initial_home)

    observed_env_home = []

    mock_shell_mod = types.ModuleType("agent.shell_hooks")
    def fake_shell_register(cfg, accept_hooks=False):
        observed_env_home.append(os.environ.get("HERMES_HOME"))
    mock_shell_mod.register_from_config = fake_shell_register

    mock_outbound_mod = types.ModuleType("agent.outbound_webhooks")
    mock_outbound_mod.register_from_config = lambda cfg: None

    monkeypatch.setitem(sys.modules, "agent.shell_hooks", mock_shell_mod)
    monkeypatch.setitem(sys.modules, "agent.outbound_webhooks", mock_outbound_mod)

    register_profile_hooks({"hooks": []}, profile_home=fake_profile_home)

    assert len(observed_env_home) == 1
    assert observed_env_home[0] == str(fake_profile_home)
    assert os.environ.get("HERMES_HOME") == initial_home


def test_register_profile_hooks_restores_env_on_exception(monkeypatch, tmp_path):
    """Verify that HERMES_HOME is restored even if hook registration raises an exception."""
    fake_profile_home = tmp_path / "custom_profile_home2"
    fake_profile_home.mkdir()

    monkeypatch.delenv("HERMES_HOME", raising=False)

    mock_shell_mod = types.ModuleType("agent.shell_hooks")
    def failing_shell_register(cfg, accept_hooks=False):
        raise RuntimeError("boom")
    mock_shell_mod.register_from_config = failing_shell_register

    monkeypatch.setitem(sys.modules, "agent.shell_hooks", mock_shell_mod)

    # Should not raise, catches and logs
    register_profile_hooks({"hooks": []}, profile_home=fake_profile_home)

    assert "HERMES_HOME" not in os.environ


def test_server_startup_registers_hooks():
    """Verify server.py includes register_profile_hooks in its startup flow."""
    with open("server.py", "r", encoding="utf-8") as f:
        content = f.read()
    assert "register_profile_hooks" in content
    assert "register_profile_hooks(get_config())" in content


def test_streaming_and_routes_register_profile_hooks():
    """Verify streaming.py and routes.py call register_profile_hooks before AIAgent."""
    with open("api/streaming.py", "r", encoding="utf-8") as f:
        streaming_content = f.read()
    assert "_register_profile_hooks(_cfg, profile_home=_profile_home)" in streaming_content

    with open("api/routes.py", "r", encoding="utf-8") as f:
        routes_content = f.read()
    assert "_sync_register_profile_hooks(_pp_cfg, profile_home=_sync_profile_home)" in routes_content
