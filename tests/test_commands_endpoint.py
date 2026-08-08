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


def test_learn_command_returns_agent_message_payload(monkeypatch):
    """`/learn` resolves to a normal agent-turn message instead of a fake output-only command."""
    import sys

    agent_pkg = sys.modules.get("agent") or ModuleType("agent")
    agent_pkg.__path__ = []
    learn_prompt = ModuleType("agent.learn_prompt")
    cast(Any, learn_prompt).build_learn_prompt = lambda req: f"LEARN PROMPT: {req or '<conversation>'}"
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.learn_prompt", learn_prompt)

    from api.commands import execute_agent_command

    result = execute_agent_command('/learn what we just fixed')
    assert result == {
        "output": "⚡ Learning a skill from what you described.",
        "message": "LEARN PROMPT: what we just fixed",
    }


def test_blueprint_command_can_return_agent_seed(monkeypatch):
    """Blueprint commands with missing slots should seed the agent turn in WebUI."""
    import sys

    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    hermes_cli_pkg.__path__ = []
    blueprint_cmd = ModuleType("hermes_cli.blueprint_cmd")
    cast(Any, blueprint_cmd).handle_blueprint_command = lambda args: SimpleNamespace(
        text=f"Blueprint: {args}",
        agent_seed=f"ASK FOR SLOTS: {args}",
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.blueprint_cmd", blueprint_cmd)

    from api.commands import execute_agent_command

    assert execute_agent_command('/blueprint morning') == {
        "output": "Blueprint: morning",
        "message": "ASK FOR SLOTS: morning",
    }


def test_curator_command_uses_subprocess_capture(monkeypatch):
    """Curator command capture must not mutate process-global stdout/stderr."""
    from api import commands

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return commands.subprocess.CompletedProcess(cmd, 0, "curator ok\n", "")

    monkeypatch.setattr(commands.sys, "executable", "/python")
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    assert commands._run_curator_command("/curator status") == "curator ok"
    assert calls == [(
        ["/python", "-m", "hermes_cli.main", "curator", "status"],
        {"capture_output": True, "text": True, "timeout": 30},
    )]


def test_curator_command_blocks_state_changing_subcommands(monkeypatch):
    """WebUI-safe: only read-only curator subcommands run; destructive ones (which
    archive/consolidate skills on disk) are rejected without spawning a process."""
    import pytest

    from api import commands

    ran: list = []
    monkeypatch.setattr(commands.subprocess, "run", lambda *a, **k: ran.append(a))

    for destructive in ("run", "prune", "archive", "restore", "pin", "unpin",
                        "pause", "resume", "backup", "prune --yes"):
        with pytest.raises(RuntimeError, match="not available from the WebUI"):
            commands._run_curator_command(f"/curator {destructive}")
    assert ran == [], "a blocked curator subcommand must not spawn a subprocess"

    # Read-only subcommands (and their flags) are still allowed.
    def _ok(cmd, **kw):
        return commands.subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(commands.subprocess, "run", _ok)
    for allowed in ("status", "usage", "usage --json", "list-archived", ""):
        assert commands._run_curator_command(f"/curator {allowed}".strip()) == "ok"


def _fake_cli_module(monkeypatch, name, attr, fn):
    import sys
    import types

    if "hermes_cli" not in sys.modules:
        pkg = types.ModuleType("hermes_cli")
        pkg.__path__ = []  # mark as package
        monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    mod = types.ModuleType(name)
    setattr(mod, attr, fn)
    monkeypatch.setitem(sys.modules, name, mod)


def test_kanban_command_blocks_mutations_and_worker_spawns(monkeypatch):
    """WebUI-safe: only read-only kanban subcommands run; mutations, dispatch/swarm
    (spawn workers) and tail/daemon (block the request thread) are rejected."""
    from api import commands

    ran = []
    _fake_cli_module(monkeypatch, "hermes_cli.kanban", "run_slash",
                     lambda a: ran.append(a) or f"kb:{a}")
    for blocked in ("create x", "assign 1 me", "complete 1", "archive 1", "edit 1",
                    "block 1", "swarm", "dispatch", "daemon --force", "tail 1",
                    "boards create b", "boards rm b"):
        with pytest.raises(RuntimeError, match="not available from the WebUI"):
            commands._run_kanban_command(blocked)
    assert ran == [], "a blocked kanban subcommand reached run_slash"
    for allowed in ("", "list", "ls", "show 1", "boards list"):
        assert commands._run_kanban_command(allowed) == f"kb:{allowed}"


def test_blueprint_command_blocks_direct_create(monkeypatch):
    """WebUI-safe: `/blueprint <name> slot=value` (direct create_job) is rejected;
    catalog listing and the agent-seed path stay allowed."""
    import types

    from api import commands

    for blocked in ("morning slot=x", "daily hour=9 minute=0", "x a=b"):
        with pytest.raises(RuntimeError, match="not available from the WebUI"):
            commands._run_blueprint_command(blocked)

    seen = []

    def _fake_handle(arg):
        seen.append(arg)
        return types.SimpleNamespace(text="ok", agent_seed=None)

    _fake_cli_module(monkeypatch, "hermes_cli.blueprint_cmd",
                     "handle_blueprint_command", _fake_handle)
    for allowed in ("", "morning", "list"):
        commands._run_blueprint_command(allowed)
    assert seen == ["", "morning", "list"]


def test_suggestions_command_blocks_subcommands(monkeypatch):
    """WebUI-safe: only the bare `/suggestions` listing runs; state-changing
    subcommands (accept/add/schedule/dismiss/reject/clear) are rejected."""
    from api import commands

    seen = []
    _fake_cli_module(monkeypatch, "hermes_cli.suggestions_cmd",
                     "handle_suggestions_command",
                     lambda a, origin=None: seen.append(a) or "sg")
    for blocked in ("accept 1", "add foo", "schedule 2", "dismiss 1", "reject 1", "clear"):
        with pytest.raises(RuntimeError, match="not available from the WebUI"):
            commands._run_suggestions_command(blocked)
    assert seen == [], "a blocked suggestions subcommand reached the handler"
    assert commands._run_suggestions_command("") == "sg"


def test_memory_command_blocks_shared_config_approval_toggle(monkeypatch):
    """WebUI-safe: `/memory approval|mode on|off` writes memory.write_approval to the
    shared Hermes config (cross-session) and is blocked; pending/approve/reject
    (in-session) and bare `approval`/`mode` (status) pass through."""
    import sys
    import types

    from api import commands

    for blocked in (
        "/memory approval on",
        "/memory approval off",
        "/memory mode on",
        "/memory mode off",
    ):
        with pytest.raises(RuntimeError, match="not available from the WebUI"):
            commands._run_memory_command(blocked)

    seen = []
    wac = types.ModuleType("hermes_cli.write_approval_commands")
    wac.handle_pending_subcommand = (
        lambda mem, args, memory_store=None, set_mode_fn=None: seen.append(list(args)) or "mem-ok"
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.write_approval_commands", wac)
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    wa = types.ModuleType("tools.write_approval")
    wa.MEMORY = "MEMORY"
    monkeypatch.setitem(sys.modules, "tools.write_approval", wa)
    mt = types.ModuleType("tools.memory_tool")
    mt.load_on_disk_store = lambda: {}
    monkeypatch.setitem(sys.modules, "tools.memory_tool", mt)

    for allowed in (
        "/memory pending",
        "/memory approve 1",
        "/memory reject 1",
        "/memory approval",
        "/memory mode",
    ):
        assert commands._run_memory_command(allowed) == "mem-ok"
    assert ["pending"] in seen and ["approval"] in seen and ["mode"] in seen


def _install_case_folding_write_approval(monkeypatch, writes):
    """Install a `handle_pending_subcommand` with the REAL dispatch semantics.

    `hermes_cli/write_approval_commands.handle_pending_subcommand` does
    ``sub = args[0].lower()`` before matching ``{"approval", "mode"}``, so a
    case-sensitive guard in the WebUI lets mixed case through to `set_mode_fn`,
    which writes ``memory.write_approval`` into the SHARED Hermes config.
    """
    import sys
    import types

    def handle_pending_subcommand(mem, args, memory_store=None, set_mode_fn=None):
        if not args:
            return "state"
        sub = args[0].lower()
        rest = args[1:]
        if sub in {"approval", "mode"}:
            if not rest:
                return "state"
            arg = rest[0].strip().lower()
            if arg in {"on", "true", "yes", "1", "enable", "enabled"}:
                set_mode_fn(True)
                return "enabled"
            if arg in {"off", "false", "no", "0", "disable", "disabled"}:
                set_mode_fn(False)
                return "disabled"
            return "usage"
        return "mem-ok"

    wac = types.ModuleType("hermes_cli.write_approval_commands")
    wac.handle_pending_subcommand = handle_pending_subcommand
    monkeypatch.setitem(sys.modules, "hermes_cli.write_approval_commands", wac)

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    wa = types.ModuleType("tools.write_approval")
    wa.MEMORY = "MEMORY"
    monkeypatch.setitem(sys.modules, "tools.write_approval", wa)
    mt = types.ModuleType("tools.memory_tool")
    mt.load_on_disk_store = lambda: {}
    monkeypatch.setitem(sys.modules, "tools.memory_tool", mt)

    cfg = types.ModuleType("hermes_cli.config")
    cfg.set_config_value = lambda key, value: writes.append((key, value))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", cfg)


@pytest.mark.parametrize(
    "blocked",
    [
        "/memory approval on",
        "/memory approval off",
        "/memory mode on",
        "/memory mode off",
        # The downstream handler lowercases args[0]; a case-sensitive guard in
        # the WebUI would let every row below reach `set_config_value`.
        "/memory Mode on",
        "/memory MODE off",
        "/memory Approval on",
        "/memory APPROVAL off",
        "/memory ApPrOvAl enable",
        "/memory MoDe disabled",
    ],
)
def test_memory_approval_toggle_is_blocked_case_insensitively(monkeypatch, blocked):
    """No casing of `/memory approval|mode <value>` may write the shared config."""
    from api import commands

    writes: list[tuple[str, str]] = []
    _install_case_folding_write_approval(monkeypatch, writes)

    with pytest.raises(RuntimeError, match="not available from the WebUI"):
        commands._run_memory_command(blocked)

    assert writes == [], f"{blocked!r} reached the shared-config writer: {writes}"


def test_memory_status_and_pending_still_pass_through_in_any_case(monkeypatch):
    """Read-only subcommands must stay usable, including mixed case."""
    from api import commands

    writes: list[tuple[str, str]] = []
    _install_case_folding_write_approval(monkeypatch, writes)

    # Bare approval/mode report status; they carry no value token to apply.
    assert commands._run_memory_command("/memory Approval") == "state"
    assert commands._run_memory_command("/memory MODE") == "state"
    assert commands._run_memory_command("/memory Pending") == "mem-ok"
    assert commands._run_memory_command("/memory Approve 1") == "mem-ok"
    assert writes == []


def test_commands_exec_runs_under_the_requesting_profile(monkeypatch):
    """`/api/commands/exec` handlers must see the requesting browser's profile.

    The handlers delegate into Hermes helpers that resolve state from process
    env / `get_hermes_home()`. Without the active-request profile scope,
    `/memory pending|approve|reject` acts on the process-default profile's
    store instead of the requesting profile's.
    """
    import contextlib
    import os
    import sys
    import types

    from api import commands

    active = {"profile": "default"}
    homes = {
        "default": "/home/u/.hermes",
        "research": "/home/u/.hermes/profiles/research",
        "work": "/home/u/.hermes/profiles/work",
    }
    purposes: list[str] = []

    @contextlib.contextmanager
    def fake_scope(purpose="active request", logger_override=None):
        purposes.append(purpose)
        previous = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = homes[active["profile"]]
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous

    profiles_mod = types.ModuleType("api.profiles")
    profiles_mod.profile_env_for_active_request = fake_scope
    monkeypatch.setitem(sys.modules, "api.profiles", profiles_mod)

    seen: list[str | None] = []
    monkeypatch.setattr(
        commands,
        "_run_memory_command",
        lambda command: seen.append(os.environ.get("HERMES_HOME")) or "ok",
    )

    outside = os.environ.get("HERMES_HOME")

    active["profile"] = "research"
    assert commands.execute_agent_command("/memory pending") == "ok"
    active["profile"] = "work"
    assert commands.execute_agent_command("/memory pending") == "ok"

    assert seen == [homes["research"], homes["work"]], (
        "handlers did not observe the requesting profile's Hermes home"
    )
    assert purposes == ["/api/commands/exec", "/api/commands/exec"]
    assert os.environ.get("HERMES_HOME") == outside, "profile scope leaked past the call"


def test_commands_exec_scope_is_released_when_a_handler_raises(monkeypatch):
    """A failing handler must not strand the profile env of the failed request."""
    import contextlib
    import os
    import sys
    import types

    from api import commands

    @contextlib.contextmanager
    def fake_scope(purpose="active request", logger_override=None):
        previous = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = "/scoped/home"
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous

    profiles_mod = types.ModuleType("api.profiles")
    profiles_mod.profile_env_for_active_request = fake_scope
    monkeypatch.setitem(sys.modules, "api.profiles", profiles_mod)

    def boom(_command):
        raise RuntimeError("Memory command failed")

    monkeypatch.setattr(commands, "_run_memory_command", boom)

    outside = os.environ.get("HERMES_HOME")
    with pytest.raises(RuntimeError, match="Memory command failed"):
        commands.execute_agent_command("/memory pending")
    assert os.environ.get("HERMES_HOME") == outside


def test_agents_command_renders_the_real_process_registry(monkeypatch):
    """`/agents` (and its `/tasks` alias) must read `list_sessions()`.

    The registry has no `list_processes()`; calling it raised an AttributeError
    that the handler's `except Exception` swallowed, so the command always
    rendered the "nothing running" fallback even with live processes.
    """
    import sys
    import types

    from api import commands

    registry = types.SimpleNamespace(
        list_sessions=lambda: [
            {
                "session_id": "s-1",
                "command": "npm run dev",
                "pid": 4242,
                "status": "running",
                "uptime_seconds": 12,
                "profile": "alpha",
                "principal": "alice",
            },
            {
                "session_id": "s-2",
                "command": "pytest -q",
                "pid": 4243,
                "status": "exited",
                "exit_code": 1,
                "profile": "alpha",
                "principal": "alice",
            },
        ]
    )
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    pr = types.ModuleType("tools.process_registry")
    pr.process_registry = registry
    monkeypatch.setitem(sys.modules, "tools.process_registry", pr)

    out = commands._run_agents_command(profile="alpha", principal="alice")

    assert "Tracked processes (2):" in out
    assert "npm run dev — running (pid 4242)" in out
    assert "pytest -q — exited (1) (pid 4243)" in out
    assert "currently running" not in out


def test_agents_command_reports_an_empty_registry_as_nothing_running(monkeypatch):
    import sys
    import types

    from api import commands

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    pr = types.ModuleType("tools.process_registry")
    pr.process_registry = types.SimpleNamespace(list_sessions=lambda: [])
    monkeypatch.setitem(sys.modules, "tools.process_registry", pr)

    assert commands._run_agents_command(profile="alpha", principal="alice") == (
        "No background agents or tracked processes are currently running."
    )


def test_agents_command_does_not_call_the_nonexistent_list_processes(monkeypatch):
    """Guard against regressing to an API the Agent registry never exposed."""
    import sys
    import types

    from api import commands

    class Registry:
        def list_sessions(self):
            return [{
                "session_id": "s-1", "command": "sleep 1", "pid": 7, "status": "running",
                "profile": "alpha", "principal": "alice",
            }]

        def __getattr__(self, name):  # pragma: no cover - only hit on regression
            raise AssertionError(f"handler reached for absent registry attribute {name!r}")

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    pr = types.ModuleType("tools.process_registry")
    pr.process_registry = Registry()
    monkeypatch.setitem(sys.modules, "tools.process_registry", pr)

    assert "sleep 1 — running (pid 7)" in commands._run_agents_command(
        profile="alpha", principal="alice"
    )


def test_webui_safe_agent_commands_are_allowlisted(monkeypatch):
    """Safe non-CLI agent commands should be accepted by the executor allowlist."""
    from api import commands

    monkeypatch.setattr(commands, "_run_kanban_command", lambda arg: f"kanban {arg}")
    monkeypatch.setattr(commands, "_run_profile_command", lambda: "profile ok")

    assert commands.execute_agent_command('/kanban list') == "kanban list"
    assert commands.execute_agent_command('/whoami') == "profile ok"


def test_webui_safe_agent_command_aliases_resolve_to_allowlisted_handlers(monkeypatch):
    """Registry aliases must not be intercepted by the frontend then rejected by the API."""
    from api import commands

    monkeypatch.setattr(commands, "_run_agents_command", lambda **_kw: "agents ok")
    monkeypatch.setattr(commands, "_run_suggestions_command", lambda arg: f"suggestions {arg}")
    monkeypatch.setattr(commands, "_run_blueprint_command", lambda arg: f"blueprint {arg}")
    monkeypatch.setattr(commands, "_run_version_command", lambda: "version ok")

    assert commands.execute_agent_command('/tasks') == "agents ok"
    assert commands.execute_agent_command('/suggest') == "suggestions "
    assert commands.execute_agent_command('/bp morning') == "blueprint morning"
    assert commands.execute_agent_command('/v') == "version ok"


@requires_agent_modules
def test_commands_exec_cli_only_command_returns_404():
    """CLI-only commands should stay blocked from the generic execution endpoint."""
    status, body = _post('/api/commands/exec', {'command': '/clear'})
    assert status == 404
    assert isinstance(body, dict)


@requires_agent_modules
def test_commands_exec_regular_unallowlisted_agent_command_returns_404():
    """Unallowlisted agent commands must not become generic WebUI exec targets."""
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


# ── Re-gate 2026-07-25: the process registry is process-global ──────────────


def _registry_with(rows):
    """Build a fake tools.process_registry returning *rows*."""
    import types

    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = []
    pr = types.ModuleType("tools.process_registry")
    pr.process_registry = types.SimpleNamespace(list_sessions=lambda: rows)
    return tools_pkg, pr


_TWO_PROFILE_ROWS = [
    {"session_id": "a-run", "command": "alpha-secret --token=A", "pid": 11,
     "status": "running", "profile": "alpha", "principal": "alice"},
    {"session_id": "a-done", "command": "alpha-finished", "pid": 12,
     "status": "exited", "exit_code": 0, "profile": "alpha", "principal": "alice"},
    {"session_id": "b-run", "command": "beta-secret --token=B", "pid": 21,
     "status": "running", "profile": "beta", "principal": "bob"},
    {"session_id": "b-done", "command": "beta-finished", "pid": 22,
     "status": "exited", "exit_code": 2, "profile": "beta", "principal": "bob"},
    # Same profile, different principal — a profile-only filter would leak this.
    {"session_id": "a2-run", "command": "alpha-other-user", "pid": 31,
     "status": "running", "profile": "alpha", "principal": "carol"},
    # Pre-ownership row: unattributable, therefore nobody's.
    {"session_id": "legacy", "command": "legacy-no-owner", "pid": 41,
     "status": "running"},
]


def _agents_for(monkeypatch, *, profile, principal, rows=None):
    import sys

    from api import commands

    tools_pkg, pr = _registry_with(_TWO_PROFILE_ROWS if rows is None else rows)
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.process_registry", pr)
    return commands._run_agents_command(profile=profile, principal=principal)


def test_one_profile_never_sees_another_profiles_processes(monkeypatch):
    """`tools.process_registry` is ONE registry for every profile served.

    Projecting it unfiltered handed profile A the command lines, PIDs and
    statuses of profile B. Command lines routinely carry paths, hostnames and
    sometimes credentials, so this is disclosure rather than noise.
    """
    out = _agents_for(monkeypatch, profile="alpha", principal="alice")

    assert "alpha-secret --token=A" in out
    assert "alpha-finished" in out
    for foreign in ("beta-secret --token=B", "beta-finished", "21", "22"):
        assert foreign not in out, f"another profile's data leaked: {foreign}"


def test_one_principal_never_sees_another_principal_in_the_same_profile(monkeypatch):
    """A profile-only filter would still show every user inside that profile."""
    out = _agents_for(monkeypatch, profile="alpha", principal="alice")
    assert "alpha-other-user" not in out, "a second principal's process was shown"


def test_both_running_and_finished_rows_are_filtered(monkeypatch):
    """Status must not decide visibility — ownership does."""
    out = _agents_for(monkeypatch, profile="beta", principal="bob")
    assert "beta-secret --token=B — running (pid 21)" in out
    assert "beta-finished — exited (2) (pid 22)" in out
    assert "alpha" not in out


def test_a_row_without_an_owner_is_shown_to_nobody(monkeypatch):
    """Unattributable rows fail CLOSED.

    "We cannot tell whose this is" must not resolve to "yours" for whichever
    profile happens to ask first.
    """
    for profile, principal in (("alpha", "alice"), ("beta", "bob"), ("gamma", "dave")):
        out = _agents_for(monkeypatch, profile=profile, principal=principal)
        assert "legacy-no-owner" not in out, (
            f"an ownerless row was shown to {profile}/{principal}"
        )


def test_a_missing_request_identity_shows_nothing(monkeypatch):
    """Without a server-derived identity there is nothing to filter against."""
    for profile, principal in ((None, "alice"), ("alpha", None), (None, None)):
        out = _agents_for(monkeypatch, profile=profile, principal=principal)
        assert "alpha-secret" not in out and "beta-secret" not in out
        assert "currently visible" in out


def test_the_identity_is_never_taken_from_the_row_being_filtered(monkeypatch):
    """A row must not be able to name itself into the caller's view."""
    rows = [{
        "session_id": "forged", "command": "forged-row", "pid": 99, "status": "running",
        "profile": "alpha", "principal": "alice", "owner": "alice",
    }]
    out = _agents_for(monkeypatch, profile="beta", principal="bob", rows=rows)
    assert "forged-row" not in out
