"""Tests for GET /api/commands -- exposes hermes-agent COMMAND_REGISTRY."""
import io
import json
import urllib.error
import urllib.request
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from tests.conftest import TEST_BASE, requires_agent_modules


def _install_fake_mcp_tool(monkeypatch, shutdown, discover, servers=None, lock=None):
    import sys
    tools_pkg = ModuleType("tools")
    tools_pkg.__path__ = []
    mcp_tool = ModuleType("tools.mcp_tool")
    mcp_tool.shutdown_mcp_servers = shutdown
    mcp_tool.discover_mcp_tools = discover
    mcp_tool._servers = servers if servers is not None else {}
    mcp_tool._lock = lock if lock is not None else threading.Lock()
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mcp_tool)
    return mcp_tool


def _install_fake_codex_runtime_switch(monkeypatch):
    import sys
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    # Restore the real hermes_cli.__path__ on teardown instead of emptying it in
    # place: `sys.modules.get(...)` grabs the REAL package object, so a bare
    # `__path__ = []` permanently strands it (later `import hermes_cli.<sub>`
    # fails for the rest of the suite). monkeypatch.setattr snapshots and restores.
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    codex_runtime_switch = ModuleType("hermes_cli.codex_runtime_switch")
    calls = []

    def parse_args(arg_string):
        calls.append(("parse_args", arg_string))
        if arg_string in ("on", "codex_app_server"):
            return "codex_app_server", []
        if arg_string in ("", None):
            return None, []
        return None, [f"bad arg: {arg_string}"]

    def apply(config, new_value, *, persist_callback=None):
        calls.append(("apply", new_value, config.get("model", {}).get("openai_runtime")))
        if new_value is not None:
            config.setdefault("model", {})["openai_runtime"] = new_value
            if persist_callback:
                persist_callback(config)
        return SimpleNamespace(
            success=True,
            message=f"codex runtime -> {new_value or config.get('model', {}).get('openai_runtime', 'auto')}",
        )

    codex_runtime_switch_any = cast(Any, codex_runtime_switch)
    codex_runtime_switch_any.parse_args = parse_args
    codex_runtime_switch_any.apply = apply
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.codex_runtime_switch", codex_runtime_switch)
    return calls


def _install_fake_skill_commands(monkeypatch, reload_skills):
    import sys
    agent_pkg = sys.modules.get("agent") or ModuleType("agent")
    # See _install_fake_codex_runtime_switch: monkeypatch.setattr restores the
    # real agent.__path__ on teardown so `from agent.<sub> import ...` keeps
    # working in later tests (chronic full-suite poison otherwise).
    monkeypatch.setattr(agent_pkg, "__path__", [], raising=False)
    skill_commands = ModuleType("agent.skill_commands")
    skill_commands.reload_skills = reload_skills
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_commands", skill_commands)
    return skill_commands


def _install_fake_account_usage(monkeypatch, *, view=None, exc=None):
    import sys

    agent_pkg = sys.modules.get("agent") or ModuleType("agent")
    # monkeypatch.setattr restores the real agent.__path__ on teardown (see
    # _install_fake_skill_commands) to avoid permanently poisoning the package.
    monkeypatch.setattr(agent_pkg, "__path__", [], raising=False)
    account_usage = ModuleType("agent.account_usage")

    def build_credits_view(*, markdown=False, timeout=10.0):
        assert markdown is True
        if exc is not None:
            raise exc
        return view

    account_usage_any = cast(Any, account_usage)
    account_usage_any.build_credits_view = build_credits_view
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.account_usage", account_usage)
    return account_usage


def _get(path):
    """GET helper -- returns parsed JSON or raises HTTPError."""
    with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _post(path, body):
    payload = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        TEST_BASE + path,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return getattr(r, 'status', 200), json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


@requires_agent_modules
def test_commands_endpoint_returns_list():
    """GET /api/commands returns a JSON object with a 'commands' list."""
    body = _get('/api/commands')
    assert 'commands' in body
    assert isinstance(body['commands'], list)
    assert len(body['commands']) > 0


@requires_agent_modules
def test_commands_endpoint_includes_help():
    """The 'help' command must always be present (it's not cli_only)."""
    body = _get('/api/commands')
    names = {c['name'] for c in body['commands']}
    assert 'help' in names


@requires_agent_modules
def test_commands_endpoint_command_shape():
    """Each command entry has the required fields."""
    body = _get('/api/commands')
    cmd = next(c for c in body['commands'] if c['name'] == 'help')
    required = {
        'name', 'description', 'category', 'aliases',
        'args_hint', 'subcommands', 'cli_only', 'gateway_only',
    }
    assert set(cmd.keys()) >= required
    assert isinstance(cmd['aliases'], list)
    assert isinstance(cmd['subcommands'], list)
    assert isinstance(cmd['cli_only'], bool)
    assert isinstance(cmd['gateway_only'], bool)


@requires_agent_modules
def test_commands_endpoint_excludes_gateway_only_and_never_expose():
    """gateway_only commands and the _NEVER_EXPOSE set are filtered out."""
    body = _get('/api/commands')
    names = {c['name'] for c in body['commands']}
    # /sethome, /restart, /update are gateway_only; /commands is in _NEVER_EXPOSE
    for name in ('sethome', 'restart', 'update', 'commands'):
        assert name not in names, f"{name} must be excluded from /api/commands"


@requires_agent_modules
def test_commands_endpoint_keeps_new_with_reset_alias():
    """The 'new' command stays exposed and carries its 'reset' alias."""
    body = _get('/api/commands')
    new_cmd = next(c for c in body['commands'] if c['name'] == 'new')
    assert 'reset' in new_cmd['aliases']


@requires_agent_modules
def test_commands_exec_runs_allowlisted_agent_command():
    """Allowed agent-side commands execute through /api/commands/exec."""
    status, body = _post('/api/commands/exec', {'command': '/reload-mcp'})
    assert status == 200
    assert 'output' in body
    assert isinstance(body['output'], str)


@requires_agent_modules
def test_commands_exec_runs_reload_mcp_alias():
    """Telegram-style underscore alias resolves to the same allowlisted command."""
    status, body = _post('/api/commands/exec', {'command': '/reload_mcp'})
    assert status == 200
    assert 'output' in body
    assert isinstance(body['output'], str)


@requires_agent_modules
def test_commands_exec_runs_reload_skills_command():
    """`/reload-skills` executes through the same narrow shared executor path."""
    status, body = _post('/api/commands/exec', {'command': '/reload-skills'})
    assert status == 200
    assert 'output' in body
    assert isinstance(body['output'], str)


@requires_agent_modules
def test_commands_exec_runs_reload_skills_alias():
    """Telegram-style underscore alias resolves to reload-skills in the executor."""
    status, body = _post('/api/commands/exec', {'command': '/reload_skills'})
    assert status == 200
    assert 'output' in body
    assert isinstance(body['output'], str)


def test_credits_command_renders_shared_credits_view(monkeypatch):
    """`/credits` should reuse the shared Hermes credits view in WebUI output."""
    _install_fake_account_usage(
        monkeypatch,
        view=SimpleNamespace(
            logged_in=True,
            balance_lines=("📈 **Balance**", "- Subscription credits: $12.34", "- Top-up credits: $1.23"),
            identity_line="Topping up as rod@example.com / org Nous",
            topup_url="https://portal.nous.example/topup",
        ),
    )

    from api.commands import execute_agent_command

    output = execute_agent_command('/credits')

    assert output == "\n".join(
        [
            "💳 **Nous credits**",
            "- Subscription credits: $12.34",
            "- Top-up credits: $1.23",
            "",
            "Topping up as rod@example.com / org Nous",
            "",
            "Top up: https://portal.nous.example/topup",
            "Complete your top-up in the browser; credits will appear in /credits shortly.",
        ]
    )


def test_commands_exec_routes_credits_through_agent_dispatch(monkeypatch):
    """`/credits` should go through the POST route's agent-command path, not the plugin fallback."""

    class _FakeHandler:
        def __init__(self, body_bytes: bytes):
            self.status = None
            self.sent_headers = []
            self.body = bytearray()
            self.wfile = self
            self.rfile = io.BytesIO(body_bytes)
            self.headers = {"Content-Length": str(len(body_bytes))}
            self.request = None

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.sent_headers.append((name, value))

        def end_headers(self):
            pass

        def write(self, data):
            self.body.extend(data)

        def json_body(self):
            return json.loads(bytes(self.body).decode("utf-8"))

    import api.commands as commands
    from api import routes

    calls = []

    def _fake_execute_agent_command(command):
        calls.append(command)
        return "credits ok"

    def _fake_execute_plugin_command(command):
        raise AssertionError(f"plugin path should not run for {command!r}")

    monkeypatch.setattr(commands, "execute_agent_command", _fake_execute_agent_command)
    monkeypatch.setattr(commands, "execute_plugin_command", _fake_execute_plugin_command)

    raw = json.dumps({"command": "/credits"}).encode("utf-8")
    handler = _FakeHandler(raw)
    routes.handle_post(handler, SimpleNamespace(path="/api/commands/exec", query=""))

    assert calls == ["/credits"]
    assert handler.status == 200
    assert handler.json_body() == {"output": "credits ok"}


def test_credits_command_returns_not_logged_in_message(monkeypatch):
    """`/credits` should degrade to a friendly login hint when Nous auth is absent."""
    _install_fake_account_usage(
        monkeypatch,
        view=SimpleNamespace(
            logged_in=False,
            balance_lines=(),
            identity_line=None,
            topup_url=None,
        ),
    )

    from api.commands import execute_agent_command

    output = execute_agent_command('/credits')

    assert output == "Not logged into Nous. Run `hermes auth login nous` in Hermes CLI, then try /credits again."


def test_credits_command_fail_opens_on_runtime_error(monkeypatch):
    """`/credits` failures should return a short user-facing message, not 500s."""
    _install_fake_account_usage(monkeypatch, exc=RuntimeError("portal timeout"))

    from api.commands import execute_agent_command

    output = execute_agent_command('/credits')

    assert output == "Couldn't fetch credits right now."


def test_codex_runtime_command_uses_shared_switch_and_persists(monkeypatch, tmp_path):
    """`/codex-runtime` executes through the same shared switch as CLI/gateway."""
    calls = _install_fake_codex_runtime_switch(monkeypatch)
    saved = []

    from api import config as webui_config
    from api.commands import execute_agent_command

    config_data = {"model": {"openai_runtime": "auto"}}
    monkeypatch.setattr(webui_config, "get_config", lambda: config_data)
    monkeypatch.setattr(webui_config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(
        webui_config,
        "_save_yaml_config_file",
        lambda path, data: saved.append((path, data.copy())),
    )
    monkeypatch.setattr(webui_config, "reload_config", lambda: saved.append(("reload", None)))

    output = execute_agent_command('/codex-runtime on')

    assert output == "codex runtime -> codex_app_server"
    assert config_data["model"]["openai_runtime"] == "codex_app_server"
    assert calls == [
        ("parse_args", "on"),
        ("apply", "codex_app_server", "auto"),
    ]
    assert saved[0][0] == tmp_path / "config.yaml"
    assert saved[0][1] == {"model": {"openai_runtime": "codex_app_server"}}
    assert saved[1] == ("reload", None)


def test_codex_runtime_command_accepts_underscore_alias(monkeypatch):
    """Telegram/WebUI underscore spelling routes to the canonical command."""
    calls = _install_fake_codex_runtime_switch(monkeypatch)

    from api import config as webui_config
    from api.commands import execute_agent_command

    monkeypatch.setattr(webui_config, "get_config", lambda: {"model": {"openai_runtime": "auto"}})
    monkeypatch.setattr(webui_config, "_save_yaml_config_file", lambda path, data: None)
    monkeypatch.setattr(webui_config, "reload_config", lambda: None)

    output = execute_agent_command('/codex_runtime codex_app_server')

    assert output == "codex runtime -> codex_app_server"
    assert calls[0] == ("parse_args", "codex_app_server")


def test_codex_runtime_invalid_argument_returns_switch_message(monkeypatch):
    """Argument validation stays in the shared switch and returns user text."""
    calls = _install_fake_codex_runtime_switch(monkeypatch)

    from api.commands import execute_agent_command

    output = execute_agent_command('/codex-runtime nope')

    assert output == "bad arg: nope"
    assert calls == [("parse_args", "nope")]


def test_reload_mcp_error_is_generic(monkeypatch):
    """`/reload-mcp` errors must return a generic message, not raw internals."""
    calls = []

    def shutdown():
        calls.append("shutdown")
        raise RuntimeError("db_dsn=postgresql://user:pass@localhost/secret")

    def discover():
        calls.append("discover")
        return []

    _install_fake_mcp_tool(
        monkeypatch,
        shutdown=shutdown,
        discover=discover,
        servers={"old": object()},
    )

    from api.commands import execute_agent_command

    with pytest.raises(RuntimeError) as exc:
        execute_agent_command('/reload-mcp')

    assert str(exc.value) == "Failed to reload MCP servers"
    assert 'postgresql://user:pass' not in str(exc.value)
    assert 'pass@' not in str(exc.value)
    assert calls == ["shutdown"]


def test_reload_skills_command_formats_helper_diff(monkeypatch):
    """`/reload-skills` should summarize the shared helper diff in printable text."""
    def reload_skills():
        return {
            "added": [{"name": "incident-review", "description": "desc"}],
            "removed": [{"name": "legacy-skill", "description": "old"}],
            "unchanged": ["skills", "use"],
            "total": 3,
            "commands": 3,
        }

    _install_fake_skill_commands(monkeypatch, reload_skills)

    from api.commands import execute_agent_command

    output = execute_agent_command('/reload-skills')

    assert output == "\n".join([
        "Reloaded skills from disk.",
        "Added: 1",
        "Removed: 1",
        "Unchanged: 2",
        "Total skills: 3",
        "Added skills: incident-review",
        "Removed skills: legacy-skill",
    ])


def test_reload_skills_command_accepts_underscore_alias(monkeypatch):
    """Telegram/WebUI underscore spelling routes to the canonical skills reload."""
    calls = []

    def reload_skills():
        calls.append("reload_skills")
        return {
            "added": [],
            "removed": [],
            "unchanged": [],
            "total": 0,
            "commands": 0,
        }

    _install_fake_skill_commands(monkeypatch, reload_skills)

    from api.commands import execute_agent_command

    output = execute_agent_command('/reload_skills')

    assert calls == ["reload_skills"]
    assert "Added: 0" in output
    assert "Removed: 0" in output


def test_reload_skills_error_is_generic(monkeypatch):
    """`/reload-skills` failures must return a generic message, not internals."""
    def reload_skills():
        raise RuntimeError("secret_path=C:/Users/Rod/.hermes/skills/private")

    _install_fake_skill_commands(monkeypatch, reload_skills)

    from api.commands import execute_agent_command

    with pytest.raises(RuntimeError) as exc:
        execute_agent_command('/reload-skills')

    assert str(exc.value) == "Failed to reload skills"
    assert 'secret_path=' not in str(exc.value)


def test_concurrent_reload_mcp_calls_are_serialized(monkeypatch):
    """Concurrent `/reload-mcp` calls cannot run shutdown/discover interleaved."""
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()
    ready = threading.Event()

    def _track():
        with lock:
            state["active"] += 1
            if state["active"] > state["max_active"]:
                state["max_active"] = state["active"]
        time.sleep(0.12)
        with lock:
            state["active"] -= 1

    def shutdown():
        ready.set()
        _track()

    def discover():
        _track()
        return ["tool-a", "tool-b"]

    _install_fake_mcp_tool(
        monkeypatch,
        shutdown=shutdown,
        discover=discover,
        servers={"old": object()},
        lock=threading.Lock(),
    )

    from api.commands import execute_agent_command

    errors = []
    t2_started = threading.Event()

    def _call():
        try:
            execute_agent_command('/reload-mcp')
        except Exception as exc:
            errors.append(exc)

    def _call2():
        t2_started.set()
        try:
            execute_agent_command('/reload-mcp')
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_call, name="reload-1")
    t2 = threading.Thread(target=_call2, name="reload-2")

    t1.start()
    assert ready.wait(1), "first reload did not start"

    t2.start()
    assert t2_started.wait(1), "second reload did not start"
    time.sleep(0.05)

    with lock:
        observed_max = state["max_active"]
    assert observed_max == 1

    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()
    assert not errors


@requires_agent_modules
def test_commands_exec_cli_only_command_returns_404():
    """CLI-only commands should stay blocked from the generic execution endpoint."""
    status, body = _post('/api/commands/exec', {'command': '/clear'})
    assert status == 404
    assert isinstance(body, dict)


@requires_agent_modules
def test_commands_exec_regular_agent_command_returns_404():
    """Non-allowlisted agent commands must not become generic WebUI exec targets."""
    status, body = _post('/api/commands/exec', {'command': '/help'})
    assert status == 404
    assert isinstance(body, dict)


def test_list_commands_returns_empty_for_empty_registry():
    """list_commands(_registry=[]) returns [] -- the same path as when
    hermes_cli is missing (the empty-or-missing case)."""
    from api.commands import list_commands
    assert list_commands(_registry=[]) == []


def test_list_commands_degrades_when_agent_missing(monkeypatch):
    """If hermes_cli.commands is not importable, list_commands() returns []
    via the ImportError path. Verified by stubbing sys.modules; test cleanup
    is handled by monkeypatch + the fact that we don't reload api.commands."""
    import sys
    monkeypatch.setitem(sys.modules, 'hermes_cli.commands', None)
    # NOTE: we do NOT reload api.commands. The lazy import inside
    # list_commands() will re-attempt the import on each call and hit
    # the stubbed-None module, raising ImportError, taking the fallback path.
    from api.commands import list_commands
    assert list_commands() == []
