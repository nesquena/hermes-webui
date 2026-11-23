import json
import os
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from api.config import register_profile_hooks
from tests.test_8436_sync_chat_title_input import _drive_sync_chat, _reply


@pytest.fixture
def real_hooks(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.delenv("HERMES_ACCEPT_HOOKS", raising=False)
    shell_hooks = pytest.importorskip("agent.shell_hooks", exc_type=ImportError)
    monkeypatch.setattr(shell_hooks, "_process_accept", False)
    plugins = pytest.importorskip("hermes_cli.plugins", exc_type=ImportError)
    constants = pytest.importorskip("hermes_constants", exc_type=ImportError)
    return shell_hooks, plugins, constants


def _profile(tmp_path, name, *, approved=True):
    home = tmp_path / name
    home.mkdir()
    marker = home / "hook-ran"
    script = home / "gate.py"
    script.write_text(
        "import json\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        f"print({json.dumps({'decision': 'block', 'reason': name})!r})\n"
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    cfg = {"hooks": {"pre_tool_call": [
        {"command": command, "matcher": "terminal", "fail_closed": True},
    ]}}
    (home / "config.yaml").write_text(json.dumps(cfg))
    if approved:
        (home / "shell-hooks-allowlist.json").write_text(json.dumps({
            "approvals": [{"event": "pre_tool_call", "command": command}],
        }))
    return home, cfg, marker


def test_terminal_gate_blocks_a_sync_turn(monkeypatch, tmp_path, real_hooks):
    from api import config, routes
    import run_agent
    from run_agent import AIAgent
    from openai.resources.chat.completions import Completions
    from openai.types.chat import ChatCompletion

    home, cfg, marker = _profile(tmp_path, "denied")
    tool_marker = tmp_path / "terminal-ran"
    responses = iter([
        ChatCompletion.model_validate({
            "id": "test-call", "object": "chat.completion", "created": 0, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "terminal-call", "type": "function", "function": {
                        "name": "terminal", "arguments": json.dumps({
                            "command": f"touch {shlex.quote(str(tool_marker))}",
                        }),
                    },
                }],
            }}],
        }),
        ChatCompletion.model_validate({
            "id": "test-final", "object": "chat.completion", "created": 0, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "The command was blocked.",
            }}],
        }),
    ])
    model_inputs = []

    def model_reply(*_args, **kwargs):
        model_inputs.extend(deepcopy(kwargs.get("messages") or kwargs.get("extra_body", {}).get("messages", [])))
        return next(responses)

    monkeypatch.setattr(Completions, "create", model_reply)
    from api import oauth
    monkeypatch.setattr(oauth, "resolve_runtime_provider_with_anthropic_env_lock", lambda *_args, **_kwargs: {
        "provider": "openai", "api_key": "synthetic-key", "base_url": "http://127.0.0.1:1/v1",
    })
    monkeypatch.setattr(routes, "session_profile_home_or_empty", lambda _session: str(home))
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda _home, **_kwargs: cfg)

    class TestAgent(AIAgent):
        def __init__(self, **kwargs):
            kwargs.update(
                provider="openai", model="gpt-4o-mini", api_key="synthetic-key",
                base_url="http://127.0.0.1:1/v1", enabled_toolsets=["terminal"],
                skip_memory=True,
            )
            super().__init__(**kwargs)

    handler = _drive_sync_chat(
        monkeypatch, tmp_path, TestAgent, message="Run the terminal command",
        agent_module=run_agent, config_data={"model": "gpt-4o-mini", "provider": "openai"},
    )
    assert handler.status == 200
    assert marker.exists()
    assert any(message.get("role") == "tool" and '"error": "denied"' in str(message.get("content"))
               for message in model_inputs)
    assert not tool_marker.exists()


def test_concurrent_profiles_dispatch_only_their_own_hooks(monkeypatch, tmp_path, real_hooks):
    shell_hooks, plugins, constants = real_hooks
    profiles = [_profile(tmp_path, name) for name in ("alpha", "beta")]
    barrier = Barrier(2)
    initial_home = os.environ.get("HERMES_HOME")
    register = shell_hooks.register_from_config

    def synchronized_registration(cfg, **kwargs):
        barrier.wait(timeout=10)
        assert os.environ.get("HERMES_HOME") == initial_home
        return register(cfg, **kwargs)

    monkeypatch.setattr(shell_hooks, "register_from_config", synchronized_registration)

    def dispatch(profile):
        home, cfg, marker = profile
        token = constants.set_hermes_home_override(home)
        try:
            barrier.wait(timeout=10)
            register_profile_hooks(cfg, profile_home=home)
            barrier.wait(timeout=10)
            register_profile_hooks(cfg, profile_home=home)
            result = plugins.invoke_hook("pre_tool_call", tool_name="terminal", args={})
            assert marker.exists()
            assert os.environ.get("HERMES_HOME") == initial_home
            return result
        finally:
            constants.reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, profiles))
    assert [[item["message"] for item in result] for result in results] == [["alpha"], ["beta"]]
    assert os.environ.get("HERMES_HOME") == initial_home


def test_registration_failure_stops_hooked_turns_only(monkeypatch, tmp_path, caplog):
    from api import config

    ran = []

    class Model:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            ran.append(True)
            return _reply(kwargs)

    cfg = {"hooks": {"pre_tool_call": [{"command": "unavailable"}]}}
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda _home, **_kwargs: cfg)
    monkeypatch.setitem(sys.modules, "agent.shell_hooks", None)
    with pytest.raises(RuntimeError, match="Profile hook registration failed"):
        _drive_sync_chat(monkeypatch, tmp_path / "hooked", Model)
    assert not ran
    assert any(record.levelname == "WARNING" and record.exc_info and "home=" in record.message
               and "stage=" in record.message for record in caplog.records)
    cfg.clear()
    handler = _drive_sync_chat(monkeypatch, tmp_path / "plain", Model)
    assert handler.status == 200
    assert ran == [True]


@pytest.mark.parametrize("allowlist", ["missing", "unreadable"])
def test_unavailable_consent_stops_turn_before_agent_construction(monkeypatch, tmp_path, real_hooks, allowlist):
    from api import config, routes

    home, cfg, marker = _profile(tmp_path, allowlist, approved=False)
    if allowlist == "unreadable":
        (home / "shell-hooks-allowlist.json").mkdir()
    events = []

    class Model:
        def __init__(self, **_kwargs):
            events.append("constructed")

        def run_conversation(self, **kwargs):
            events.append("ran")
            return _reply(kwargs)

    monkeypatch.setattr(routes, "session_profile_home_or_empty", lambda _session: str(home))
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda _home, **_kwargs: cfg)
    with pytest.raises(RuntimeError, match="Profile hook registration failed"):
        _drive_sync_chat(monkeypatch, tmp_path, Model)
    assert events == []
    assert not marker.exists()
