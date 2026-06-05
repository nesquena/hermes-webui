"""Tests for GET /api/commands -- exposes hermes-agent COMMAND_REGISTRY."""
import json
import urllib.error
import urllib.request
import threading
import time
from types import ModuleType

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
