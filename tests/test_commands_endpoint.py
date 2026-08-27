"""Tests for GET /api/commands -- exposes hermes-agent COMMAND_REGISTRY."""
import contextvars
from contextlib import contextmanager
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


def _install_profile_scoped_hermes_tweet(monkeypatch):
    """Install a minimal profile-aware copy of Hermes Tweet's command contract."""
    import sys

    state = {"active": False, "scopes": [], "calls": []}
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    plugins = ModuleType("hermes_cli.plugins")

    def get_plugin_commands():
        state["calls"].append(("list", state["active"]))
        if not state["active"]:
            return {"default-status": {"description": "Default profile command"}}
        return {"xstatus": {"description": "Show Xquik account and usage status"}}

    def get_plugin_command_handler(name):
        state["calls"].append(("lookup", name, state["active"]))
        if name != "xstatus" or not state["active"]:
            return None

        def xstatus(arg):
            state["calls"].append(("execute", arg, state["active"]))
            return f"Xquik status for {arg}"

        return xstatus

    plugins.get_plugin_commands = get_plugin_commands
    plugins.get_plugin_command_handler = get_plugin_command_handler
    plugins.resolve_plugin_command_result = lambda result: result
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    @contextmanager
    def profile_scope(purpose, *, serialize_process_env=False):
        assert serialize_process_env is True
        state["scopes"].append(purpose)
        previous = state["active"]
        state["active"] = True
        try:
            yield
        finally:
            state["active"] = previous

    return state, profile_scope


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


def test_list_commands_discovers_hermes_tweet_in_active_profile(monkeypatch):
    """Autocomplete should read plugin commands from the selected profile."""
    import api.commands as commands

    state, profile_scope = _install_profile_scoped_hermes_tweet(monkeypatch)
    monkeypatch.setattr(commands, "_bundle_profile_context", profile_scope)

    result = commands.list_commands(_registry=[])

    assert [command["name"] for command in result] == ["xstatus"]
    assert result[0]["description"] == "Show Xquik account and usage status"
    assert state["scopes"] == ["/api/commands"]
    assert state["calls"] == [("list", True)]


def test_execute_hermes_tweet_command_keeps_active_profile(monkeypatch):
    """Plugin lookup and execution should share the selected profile scope."""
    import api.commands as commands

    state, profile_scope = _install_profile_scoped_hermes_tweet(monkeypatch)
    monkeypatch.setattr(commands, "_bundle_profile_context", profile_scope)

    result = commands.execute_plugin_command("/xstatus research")

    assert result == "Xquik status for research"
    assert state["scopes"] == ["/api/commands/exec"]
    assert state["calls"] == [
        ("lookup", "xstatus", True),
        ("execute", "research", True),
    ]


@pytest.mark.parametrize(
    ("second_profile", "second_key"),
    (("beta", "beta-key"), ("default", "default-key")),
)
def test_execute_plugin_commands_serialize_profile_process_env(
    monkeypatch,
    tmp_path,
    second_profile,
    second_key,
):
    """Plugin handlers must not observe another profile's process environment."""
    import os
    import sys

    import api.commands as commands
    import api.profiles as profiles

    base = tmp_path / ".hermes"
    base.mkdir()
    (base / ".env").write_text("XQUIK_API_KEY=default-key\n", encoding="utf-8")
    for profile in ("alpha", "beta"):
        home = base / "profiles" / profile
        home.mkdir(parents=True)
        (home / ".env").write_text(
            f"XQUIK_API_KEY={profile}-key\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_resolve_hermes_home_override", lambda: SimpleNamespace(
        set_hermes_home_override=lambda _home: None,
        reset_hermes_home_override=lambda _token: None,
    ))
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda _home: True)
    monkeypatch.setenv("XQUIK_API_KEY", "default-key")

    alpha_entered = threading.Event()
    beta_entered = threading.Event()
    alpha_release = threading.Event()
    alpha_read = threading.Event()
    beta_release = threading.Event()
    observed = {}
    errors = []

    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    plugins = ModuleType("hermes_cli.plugins")

    def get_plugin_command_handler(name):
        assert name == "xstatus"

        def xstatus(_arg):
            profile = profiles.get_active_profile_name()
            if profile == "alpha":
                alpha_entered.set()
                assert alpha_release.wait(timeout=5)
                observed[profile] = os.getenv("XQUIK_API_KEY")
                alpha_read.set()
            else:
                beta_entered.set()
                assert beta_release.wait(timeout=5)
                observed[profile] = os.getenv("XQUIK_API_KEY")
            return profile

        return xstatus

    plugins.get_plugin_command_handler = get_plugin_command_handler
    plugins.resolve_plugin_command_result = lambda result: result
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    def worker(profile):
        profiles.set_request_profile(profile)
        try:
            commands.execute_plugin_command("/xstatus")
        except BaseException as exc:
            errors.append(exc)
        finally:
            profiles.clear_request_profile()

    alpha_thread = threading.Thread(target=worker, args=("alpha",))
    beta_thread = threading.Thread(target=worker, args=(second_profile,))
    try:
        alpha_thread.start()
        assert alpha_entered.wait(timeout=5)
        beta_thread.start()
        overlapped = beta_entered.wait(timeout=0.2)
        alpha_release.set()
        assert alpha_read.wait(timeout=5)
        assert beta_entered.wait(timeout=5)
        beta_release.set()
    finally:
        alpha_release.set()
        beta_release.set()
        alpha_thread.join(timeout=5)
        beta_thread.join(timeout=5)

    assert not alpha_thread.is_alive()
    assert not beta_thread.is_alive()
    assert not errors
    assert overlapped is False
    assert observed == {"alpha": "alpha-key", second_profile: second_key}
    assert os.environ.get("XQUIK_API_KEY") == "default-key"

    if second_profile != "default":
        return

    background_entered = threading.Event()
    background_release = threading.Event()

    def background_worker():
        with profiles.profile_env_for_background_worker(
            "alpha",
            "concurrent background worker",
            scope_skill_modules=False,
        ):
            background_entered.set()
            assert background_release.wait(timeout=5)

    observed.pop("default")
    beta_entered.clear()
    background_thread = threading.Thread(target=background_worker)
    default_thread = threading.Thread(target=worker, args=("default",))
    try:
        background_thread.start()
        assert background_entered.wait(timeout=5)
        default_thread.start()
        assert not beta_entered.wait(timeout=0.2)
        background_release.set()
        assert beta_entered.wait(timeout=5)
    finally:
        background_release.set()
        background_thread.join(timeout=5)
        default_thread.join(timeout=5)

    assert not background_thread.is_alive()
    assert not default_thread.is_alive()
    assert not errors
    assert observed["default"] == "default-key"
    assert os.environ.get("XQUIK_API_KEY") == "default-key"

    from api.streaming import _ENV_LOCK

    stream_entered = threading.Event()
    stream_release = threading.Event()

    def streaming_worker(entered, release):
        with profiles.process_env_scope_for_agent_turn(
            {"XQUIK_API_KEY"},
            _ENV_LOCK,
        ):
            with _ENV_LOCK:
                previous_key = os.environ.get("XQUIK_API_KEY")
                os.environ["XQUIK_API_KEY"] = "alpha-key"
            try:
                entered.set()
                assert release.wait(timeout=5)
            finally:
                with _ENV_LOCK:
                    os.environ["XQUIK_API_KEY"] = previous_key

    observed.pop("default")
    beta_entered.clear()
    beta_release.clear()
    late_stream_entered = threading.Event()
    late_stream_release = threading.Event()
    stream_thread = threading.Thread(
        target=streaming_worker,
        args=(stream_entered, stream_release),
    )
    late_stream_thread = threading.Thread(
        target=streaming_worker,
        args=(late_stream_entered, late_stream_release),
    )
    default_thread = threading.Thread(target=worker, args=("default",))
    try:
        stream_thread.start()
        assert stream_entered.wait(timeout=5)
        default_thread.start()
        assert not beta_entered.wait(timeout=0.2)
        with profiles._process_env_scope_condition:
            assert profiles._process_env_scope_condition.wait_for(
                lambda: profiles._waiting_serialized_process_env_scopes == 1,
                timeout=5,
            )
        late_stream_thread.start()
        assert not late_stream_entered.wait(timeout=0.2)
        stream_release.set()
        assert beta_entered.wait(timeout=5)
        assert not late_stream_entered.wait(timeout=0.2)
        beta_release.set()
        assert late_stream_entered.wait(timeout=5)
        late_stream_release.set()
    finally:
        stream_release.set()
        late_stream_release.set()
        beta_release.set()
        stream_thread.join(timeout=5)
        if late_stream_thread.ident is not None:
            late_stream_thread.join(timeout=5)
        default_thread.join(timeout=5)

    assert not stream_thread.is_alive()
    assert not late_stream_thread.is_alive()
    assert not default_thread.is_alive()
    assert not errors
    assert observed["default"] == "default-key"
    assert os.environ.get("XQUIK_API_KEY") == "default-key"

    alpha_scope_entered = threading.Event()
    beta_scope_entered = threading.Event()
    alpha_scope_release = threading.Event()
    beta_scope_release = threading.Event()

    def mirrored_worker(profile, entered, release):
        with profiles.profile_env_for_background_worker(
            profile,
            "overlapping background worker",
            scope_skill_modules=False,
        ):
            entered.set()
            assert release.wait(timeout=5)

    alpha_scope_thread = threading.Thread(
        target=mirrored_worker,
        args=("alpha", alpha_scope_entered, alpha_scope_release),
    )
    beta_scope_thread = threading.Thread(
        target=mirrored_worker,
        args=("beta", beta_scope_entered, beta_scope_release),
    )
    try:
        alpha_scope_thread.start()
        assert alpha_scope_entered.wait(timeout=5)
        beta_scope_thread.start()
        assert beta_scope_entered.wait(timeout=5)
        alpha_scope_release.set()
        alpha_scope_thread.join(timeout=5)
        beta_scope_release.set()
    finally:
        alpha_scope_release.set()
        beta_scope_release.set()
        alpha_scope_thread.join(timeout=5)
        beta_scope_thread.join(timeout=5)

    assert not alpha_scope_thread.is_alive()
    assert not beta_scope_thread.is_alive()
    assert os.environ.get("XQUIK_API_KEY") == "default-key"


def test_active_agent_turn_reenters_before_serialized_waiter(monkeypatch, tmp_path):
    """A turn may enter a nested profile scope while a command waits."""
    import api.profiles as profiles
    from api.streaming import _ENV_LOCK

    root_home = tmp_path / ".hermes"
    profile_home = root_home / "profiles" / "alpha"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root_home)
    monkeypatch.setattr(
        profiles,
        "_resolve_hermes_home_override",
        lambda: SimpleNamespace(
            set_hermes_home_override=lambda _home: None,
            reset_hermes_home_override=lambda _token: None,
        ),
    )
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda _home: True)

    outer_entered = threading.Event()
    enter_nested = threading.Event()
    nested_entered = threading.Event()
    nested_finished = threading.Event()
    outer_release = threading.Event()
    serialized_entered = threading.Event()
    nested_threads = []
    errors = []

    def nested_profile_scope():
        try:
            with profiles.profile_scope_for_detached_worker("alpha", "model resolution"):
                nested_entered.set()
        except BaseException as exc:
            errors.append(exc)
        finally:
            nested_finished.set()

    def agent_turn():
        try:
            with profiles.process_env_scope_for_agent_turn(set(), _ENV_LOCK):
                outer_entered.set()
                assert enter_nested.wait(timeout=5)
                nested_context = contextvars.copy_context()
                nested_thread = threading.Thread(
                    target=nested_context.run,
                    args=(nested_profile_scope,),
                    daemon=True,
                )
                nested_threads.append(nested_thread)
                nested_thread.start()
                assert nested_finished.wait(timeout=5)
                assert outer_release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)

    def serialized_command():
        try:
            profiles._begin_process_env_scope(serialized=True)
            try:
                serialized_entered.set()
            finally:
                profiles._end_process_env_scope(env_lock=_ENV_LOCK)
        except BaseException as exc:
            errors.append(exc)

    agent_thread = threading.Thread(target=agent_turn, daemon=True)
    command_thread = threading.Thread(target=serialized_command, daemon=True)
    try:
        agent_thread.start()
        assert outer_entered.wait(timeout=5)
        command_thread.start()
        with profiles._process_env_scope_condition:
            assert profiles._process_env_scope_condition.wait_for(
                lambda: profiles._waiting_serialized_process_env_scopes == 1,
                timeout=5,
            )
        enter_nested.set()
        assert nested_entered.wait(timeout=1)
        assert not serialized_entered.is_set()
        outer_release.set()
        assert serialized_entered.wait(timeout=5)
    finally:
        enter_nested.set()
        outer_release.set()
        agent_thread.join(timeout=5)
        command_thread.join(timeout=5)
        for nested_thread in nested_threads:
            nested_thread.join(timeout=5)

    assert not agent_thread.is_alive()
    assert not command_thread.is_alive()
    assert all(not nested_thread.is_alive() for nested_thread in nested_threads)
    assert not errors


def test_sync_chat_blocks_serialized_plugin_command(monkeypatch, tmp_path):
    """A synchronous agent turn must keep plugin commands outside its env scope."""
    import os
    import sys

    import api.commands as commands
    import api.config as config
    import api.models as models
    import api.oauth as oauth
    import api.profiles as profiles
    import api.routes as routes
    from api.models import Session

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    profile_home = tmp_path / ".hermes"
    profile_home.mkdir()
    (profile_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", profile_home)
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda _value: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(
        config,
        "resolve_custom_provider_connection",
        lambda _provider: (None, None),
    )
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda _value: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _session_id: False)
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda _session, _provider: ("test-provider", "test-model", {}),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", False),
    )
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    sync_entered = threading.Event()
    sync_release = threading.Event()
    plugin_entered = threading.Event()
    observed = {}
    errors = []

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            observed["sync_session_key"] = os.getenv("HERMES_SESSION_KEY")
            sync_entered.set()
            assert sync_release.wait(timeout=5)
            return {
                "messages": [
                    *kwargs["conversation_history"],
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "done"},
                ],
                "final_response": "done",
                "completed": True,
            }

    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: FakeAgent)

    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    plugins = ModuleType("hermes_cli.plugins")
    runtime_provider = ModuleType("hermes_cli.runtime_provider")

    def plugin_handler(_arg):
        observed["plugin_session_key"] = os.getenv("HERMES_SESSION_KEY")
        plugin_entered.set()
        return "ok"

    plugins.get_plugin_command_handler = lambda _name: plugin_handler
    plugins.resolve_plugin_command_result = lambda result: result
    runtime_provider.resolve_runtime_provider = lambda **_kwargs: {
        "provider": "test-provider",
        "api_key": None,
        "base_url": None,
    }
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_provider)
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )

    session = Session(
        session_id="sync-chat-env-scope",
        workspace=str(tmp_path),
        messages=[],
        context_messages=[],
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)

    class FakeHandler:
        def __init__(self):
            self.status = None
            self.headers = {}
            self.body = bytearray()
            self.wfile = self

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

        def write(self, data):
            self.body.extend(data)

    handler = FakeHandler()

    def run_sync_chat():
        try:
            routes._handle_chat_sync(
                handler,
                {
                    "session_id": session.session_id,
                    "message": "hello",
                    "workspace": str(tmp_path),
                },
            )
        except BaseException as exc:
            errors.append(exc)

    def run_plugin_command():
        try:
            observed["plugin_result"] = commands.execute_plugin_command("/xstatus")
        except BaseException as exc:
            errors.append(exc)

    sync_thread = threading.Thread(target=run_sync_chat)
    plugin_thread = threading.Thread(target=run_plugin_command)
    try:
        sync_thread.start()
        assert sync_entered.wait(timeout=5)
        plugin_thread.start()
        overlapped = plugin_entered.wait(timeout=0.2)
        sync_release.set()
    finally:
        sync_release.set()
        sync_thread.join(timeout=5)
        plugin_thread.join(timeout=5)

    assert not sync_thread.is_alive()
    assert not plugin_thread.is_alive()
    assert not errors
    assert overlapped is False
    assert handler.status == 200
    assert observed == {
        "sync_session_key": session.session_id,
        "plugin_session_key": None,
        "plugin_result": "ok",
    }
    assert os.getenv("HERMES_SESSION_KEY") is None


def test_named_plugin_command_scrubs_root_only_runtime_key(monkeypatch, tmp_path):
    """A named plugin must not inherit a key found only in the root profile."""
    import os
    import sys

    import api.commands as commands
    import api.profiles as profiles

    base = tmp_path / ".hermes"
    profile_home = base / "profiles" / "alpha"
    profile_home.mkdir(parents=True)
    (base / ".env").write_text("XQUIK_API_KEY=default-key\n", encoding="utf-8")
    (profile_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_resolve_hermes_home_override", lambda: SimpleNamespace(
        set_hermes_home_override=lambda _home: None,
        reset_hermes_home_override=lambda _token: None,
    ))
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda _home: True)
    monkeypatch.setenv("XQUIK_API_KEY", "default-key")

    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    plugins = ModuleType("hermes_cli.plugins")
    observed = []

    def handler(_arg):
        observed.append(os.getenv("XQUIK_API_KEY"))
        return "ok"

    plugins.get_plugin_command_handler = lambda _name: handler
    plugins.resolve_plugin_command_result = lambda result: result
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    profiles.set_request_profile("alpha")
    try:
        result = commands.execute_plugin_command("/xstatus")
    finally:
        profiles.clear_request_profile()

    assert result == "ok"
    assert observed == [None]
    assert os.environ.get("XQUIK_API_KEY") == "default-key"


@pytest.mark.parametrize(
    ("loaded_profile_keys", "requirement_source"),
    (
        ({"XQUIK_API_KEY"}, "loaded"),
        (set(), "file"),
        (set(), "manager"),
    ),
)
@pytest.mark.parametrize(
    ("profile", "expected_key"),
    (("alpha", None), ("default", "process-root-key")),
)
def test_plugin_command_scopes_process_loaded_plugin_key(
    monkeypatch,
    tmp_path,
    loaded_profile_keys,
    requirement_source,
    profile,
    expected_key,
):
    """Named profiles scrub process keys; the default profile preserves them."""
    import os
    import sys

    import api.commands as commands
    import api.profiles as profiles

    base = tmp_path / ".hermes"
    profile_home = base / "profiles" / "alpha"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("", encoding="utf-8")
    if requirement_source == "file":
        selected_home = base if profile == "default" else profile_home
        plugin_home = selected_home / "plugins" / "hermes-tweet"
        plugin_home.mkdir(parents=True)
        (plugin_home / "plugin.yaml").write_text(
            "requires_env:\n  - name: XQUIK_API_KEY\n    secret: true\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", loaded_profile_keys)
    monkeypatch.setattr(profiles, "_profile_runtime_env_keys", set())
    monkeypatch.setattr(profiles, "_resolve_hermes_home_override", lambda: SimpleNamespace(
        set_hermes_home_override=lambda _home: None,
        reset_hermes_home_override=lambda _token: None,
    ))
    monkeypatch.setattr(profiles, "_skill_modules_support_profile_home", lambda _home: True)
    monkeypatch.setenv("XQUIK_API_KEY", "process-root-key")

    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    plugins = ModuleType("hermes_cli.plugins")
    observed = []

    def handler(_arg):
        observed.append(os.getenv("XQUIK_API_KEY"))
        return "ok"

    manifest_requirements = (
        [{"name": "XQUIK_API_KEY", "secret": True}]
        if requirement_source == "manager"
        else []
    )
    manifest = SimpleNamespace(requires_env=manifest_requirements)
    manager = SimpleNamespace(
        _plugins={"hermes-tweet": SimpleNamespace(manifest=manifest)},
    )
    plugins.get_plugin_manager = lambda: manager
    plugins.get_plugin_command_handler = lambda _name: handler
    plugins.resolve_plugin_command_result = lambda result: result
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)

    profiles.set_request_profile(profile)
    try:
        result = commands.execute_plugin_command("/xstatus")
    finally:
        profiles.clear_request_profile()

    assert result == "ok"
    assert observed == [expected_key]
    assert os.environ.get("XQUIK_API_KEY") == "process-root-key"


@pytest.mark.parametrize(
    ("nested_profile", "nested_key"),
    (("alpha", "alpha-key"), ("default", "process-root-key")),
)
def test_serialized_profile_env_scope_is_reentrant(
    monkeypatch,
    tmp_path,
    nested_profile,
    nested_key,
):
    """A plugin command may enter another serialized profile operation."""
    import os

    import api.profiles as profiles

    base = tmp_path / ".hermes"
    profile_home = base / "profiles" / "alpha"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("XQUIK_API_KEY=alpha-key\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"XQUIK_API_KEY"})
    monkeypatch.setattr(profiles, "_profile_runtime_env_keys", set())
    monkeypatch.setenv("XQUIK_API_KEY", "process-root-key")
    observed = []

    def nested_operation():
        with profiles.profile_env_for_background_worker(
            "alpha",
            "outer plugin command",
            scope_skill_modules=False,
            serialize_process_env=True,
        ):
            observed.append(os.getenv("XQUIK_API_KEY"))
            with profiles.profile_env_for_background_worker(
                nested_profile,
                "nested plugin command",
                scope_skill_modules=False,
                serialize_process_env=True,
            ):
                observed.append(os.getenv("XQUIK_API_KEY"))
            observed.append(os.getenv("XQUIK_API_KEY"))

    thread = threading.Thread(target=nested_operation, daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert observed == ["alpha-key", nested_key, "alpha-key"]
    assert os.getenv("XQUIK_API_KEY") == "process-root-key"


@pytest.mark.parametrize("first_profile", ("default", "alpha"))
def test_default_background_worker_blocks_named_serialized_scope(
    monkeypatch,
    tmp_path,
    first_profile,
):
    """Default reads and named serialized handlers must not overlap."""
    import os

    import api.profiles as profiles

    base = tmp_path / ".hermes"
    profile_home = base / "profiles" / "alpha"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("XQUIK_API_KEY=alpha-key\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setenv("XQUIK_API_KEY", "default-key")

    entered = {profile: threading.Event() for profile in ("default", "alpha")}
    release = {profile: threading.Event() for profile in ("default", "alpha")}
    observed = {}
    errors = []

    def worker(profile):
        try:
            with profiles.profile_env_for_background_worker(
                profile,
                f"{profile} worker",
                scope_skill_modules=False,
                serialize_process_env=profile == "alpha",
            ):
                observed[profile] = os.getenv("XQUIK_API_KEY")
                entered[profile].set()
                assert release[profile].wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)

    second_profile = "alpha" if first_profile == "default" else "default"
    first_thread = threading.Thread(target=worker, args=(first_profile,))
    second_thread = threading.Thread(target=worker, args=(second_profile,))
    try:
        first_thread.start()
        assert entered[first_profile].wait(timeout=5)
        second_thread.start()
        overlapped = entered[second_profile].wait(timeout=1)
        release[first_profile].set()
        assert entered[second_profile].wait(timeout=5)
        release[second_profile].set()
    finally:
        release["default"].set()
        release["alpha"].set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors
    assert overlapped is False
    assert observed == {"default": "default-key", "alpha": "alpha-key"}
    assert os.getenv("XQUIK_API_KEY") == "default-key"
