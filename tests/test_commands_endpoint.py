"""Tests for GET /api/commands -- exposes hermes-agent COMMAND_REGISTRY."""
import io
import json
import pathlib
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


_FAKE_MEMORY_STORE = object()  # sentinel: proves _run_memory_write_approval_command passes
                                # load_on_disk_store()'s *return value* through, not the function


def _install_fake_write_approval(monkeypatch, *, result="ok", raise_exc=None):
    """Fake hermes_cli.write_approval_commands + tools.write_approval (+ tools.memory_tool's
    load_on_disk_store, for the /memory path), recording every handle_pending_subcommand call.
    Mirrors _install_fake_codex_runtime_switch's __path__ restore for hermes_cli (a real
    package elsewhere in this suite) and _install_fake_mcp_tool's plain replacement for
    `tools` (never real here)."""
    import sys

    calls = []

    tools_pkg = ModuleType("tools")
    tools_pkg.__path__ = []
    write_approval = ModuleType("tools.write_approval")
    write_approval_any = cast(Any, write_approval)
    write_approval_any.SKILLS = "skills"
    write_approval_any.MEMORY = "memory"
    memory_tool = ModuleType("tools.memory_tool")
    cast(Any, memory_tool).load_on_disk_store = lambda: _FAKE_MEMORY_STORE
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.write_approval", write_approval)
    monkeypatch.setitem(sys.modules, "tools.memory_tool", memory_tool)

    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    write_approval_commands = ModuleType("hermes_cli.write_approval_commands")

    def handle_pending_subcommand(subsystem, args, *, memory_store=None, set_mode_fn=None):
        calls.append((subsystem, list(args), memory_store, set_mode_fn))
        if raise_exc is not None:
            raise raise_exc
        return result

    write_approval_commands_any = cast(Any, write_approval_commands)
    write_approval_commands_any.handle_pending_subcommand = handle_pending_subcommand
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.write_approval_commands", write_approval_commands)
    return calls


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


class _RouteHandler:
    """Minimal in-process POST handler for route persistence tests."""
    def __init__(self, body):
        raw = json.dumps(body).encode()
        self.status = None
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *_args):
        pass

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body) or b"{}")


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


def test_commands_exec_persists_and_deduplicates_owner_transcript(monkeypatch):
    """A retry with the same command id must not execute or append twice."""
    from api import commands, routes

    class Session:
        session_id = "persist-command-session"
        profile = "default"
        read_only = False
        is_read_only = False

        def __init__(self):
            self.messages = []
            self.saved = 0

        def save(self):
            self.saved += 1

    session = Session()
    lock = threading.RLock()
    calls = []
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock)
    monkeypatch.setattr(commands, "execute_agent_command", lambda command: calls.append(command) or "saved output")
    monkeypatch.setattr(commands, "execute_plugin_command", lambda _command: (_ for _ in ()).throw(AssertionError("plugin fallback")))
    payload = {
        "command": "/memory pending",
        "session_id": session.session_id,
        "command_id": "webui-command-test-1",
    }

    first = _RouteHandler(payload)
    routes.handle_post(first, SimpleNamespace(path="/api/commands/exec", query=""))
    second = _RouteHandler(payload)
    routes.handle_post(second, SimpleNamespace(path="/api/commands/exec", query=""))

    assert first.status == second.status == 200
    assert first.json_body() == second.json_body() == {
        "output": "saved output",
        "command_id": "webui-command-test-1",
    }
    assert calls == ["/memory pending"]
    assert session.saved == 2
    assert [(m["role"], m["content"]) for m in session.messages] == [
        ("user", "/memory pending"),
        ("assistant", "saved output"),
    ]
    assert {m["_webui_command_id"] for m in session.messages} == {"webui-command-test-1"}


def test_commands_exec_does_not_execute_when_initial_marker_cannot_be_saved(monkeypatch):
    """A command must not run until its durable idempotency marker exists."""
    from api import commands, routes

    class Session:
        session_id = "failed-marker-session"
        profile = "default"
        read_only = False
        is_read_only = False

        def __init__(self):
            self.messages = []

        def save(self):
            raise OSError("disk unavailable")

    session = Session()
    calls = []
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    monkeypatch.setattr(commands, "execute_agent_command", lambda command: calls.append(command) or "must not run")
    payload = {
        "command": "/memory pending",
        "session_id": session.session_id,
        "command_id": "webui-command-save-fail",
    }

    handler = _RouteHandler(payload)
    routes.handle_post(handler, SimpleNamespace(path="/api/commands/exec", query=""))

    assert handler.status == 503
    assert calls == []
    assert session.messages == []


def test_commands_exec_does_not_repeat_after_final_save_failure(monkeypatch):
    """Once execution begins, a failed result save must not allow a duplicate run."""
    from api import commands, routes

    class Session:
        session_id = "failed-result-save-session"
        profile = "default"
        read_only = False
        is_read_only = False

        def __init__(self):
            self.messages = []
            self.save_calls = 0
            self.first_saved_snapshot = None

        def save(self):
            self.save_calls += 1
            if self.save_calls == 1:
                self.first_saved_snapshot = [dict(message) for message in self.messages]
                return
            raise OSError("disk unavailable")

    session = Session()
    calls = []
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    monkeypatch.setattr(commands, "execute_agent_command", lambda command: calls.append(command) or "ran once")
    payload = {
        "command": "/memory pending",
        "session_id": session.session_id,
        "command_id": "webui-command-result-save-fail",
    }

    first = _RouteHandler(payload)
    routes.handle_post(first, SimpleNamespace(path="/api/commands/exec", query=""))
    second = _RouteHandler(payload)
    routes.handle_post(second, SimpleNamespace(path="/api/commands/exec", query=""))

    assert first.status == second.status == 200
    assert calls == ["/memory pending"]
    assert first.json_body()["persistence_warning"] is True
    assert second.json_body() == {
        "output": "ran once",
        "command_id": "webui-command-result-save-fail",
    }
    assert session.first_saved_snapshot[1]["_webui_command_pending"] is True

    # Simulate a process restart: only the durable pre-execution marker is
    # available, and a user retry necessarily has a newly generated id.
    reloaded = Session()
    reloaded.messages = [dict(message) for message in session.first_saved_snapshot]
    reloaded.save_calls = 99
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: reloaded)
    retry_payload = dict(payload, command_id="webui-command-new-id-after-restart")
    after_restart = _RouteHandler(retry_payload)
    routes.handle_post(after_restart, SimpleNamespace(path="/api/commands/exec", query=""))

    assert after_restart.status == 409
    assert calls == ["/memory pending"]


def test_session_delete_holds_agent_lock_through_cli_cleanup(monkeypatch, tmp_path):
    """Delete must not expose a gap where command execution can recover the session."""
    from api import routes

    sid = "delete-command-race-session"
    session_file = tmp_path / f"{sid}.json"
    session_file.write_text("{}", encoding="utf-8")

    class Session:
        session_id = sid
        profile = "default"

    class RecordingLock:
        def __init__(self):
            self.held = False

        def acquire(self, timeout=None):
            self.held = True
            return True

        def release(self):
            self.held = False

    lock = RecordingLock()
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: Session())
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda _sid: {})
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock)
    monkeypatch.setattr(routes, "prune_session_from_index", lambda _sid: None)
    monkeypatch.setattr(routes, "_record_webui_deleted_session_tombstone", lambda _sid: None)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: None)

    import api.config as config
    import api.models as models
    import api.upload as upload
    import api.turn_journal as turn_journal
    import api.run_journal as run_journal
    import api.background_process as background_process
    import api.terminal as terminal_mod

    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda _sid: tmp_path / "attachments")
    monkeypatch.setattr(turn_journal, "delete_turn_journal", lambda _sid: None)
    monkeypatch.setattr(run_journal, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(background_process, "forget_bg_task_completion_dedup", lambda _sid: None)
    monkeypatch.setattr(terminal_mod, "close_terminal", lambda _sid: None)

    observed = []
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: observed.append(lock.held) or True)
    handler = _RouteHandler({"session_id": sid})
    routes.handle_post(handler, SimpleNamespace(path="/api/session/delete", query=""))

    assert handler.status == 200
    assert observed == [True]
    assert lock.held is False


def test_commands_exec_releases_session_lock_while_command_runs(monkeypatch):
    """Arbitrary command handlers must never run under the non-reentrant session lock."""
    from api import commands, routes

    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    class Session:
        session_id = "locked-command-session"
        profile = "default"
        read_only = False
        is_read_only = False
        messages = []

        def save(self):
            pass

    def execute(command):
        assert lock.acquire(blocking=False), "command executed while holding the session lock"
        lock.release()
        entered.set()
        assert release.wait(5)
        return "done"

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: Session())
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock)
    monkeypatch.setattr(commands, "execute_agent_command", execute)
    payload = {"command": "/memory pending", "session_id": Session.session_id, "command_id": "webui-command-lock"}
    handler = _RouteHandler(payload)
    thread = threading.Thread(
        target=routes.handle_post,
        args=(handler, SimpleNamespace(path="/api/commands/exec", query="")),
        daemon=True,
    )
    thread.start()
    assert entered.wait(5)
    assert lock.acquire(timeout=0.05) is True
    lock.release()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert handler.status == 200


def test_webui_command_run_tokens_survive_stale_finalizer_after_invalidation():
    """Clear/truncate invalidation must prevent an old run from owning a replacement marker."""
    from api import routes

    sid = "command-generation-session"
    command_id = "webui-command-generation"
    routes._invalidate_webui_command_runs(sid)
    first = routes._register_webui_command_run(sid, command_id)
    assert first
    assert routes._webui_command_run_is_active(sid, command_id, first)

    routes._invalidate_webui_command_runs(sid)
    assert not routes._webui_command_run_is_active(sid, command_id, first)

    second = routes._register_webui_command_run(sid, command_id)
    assert second and second != first
    routes._unregister_webui_command_run(sid, command_id, first)
    assert routes._webui_command_run_is_active(sid, command_id, second)
    routes._unregister_webui_command_run(sid, command_id, second)


def test_commands_exec_rolls_back_marker_when_run_registration_loses(monkeypatch):
    """A duplicate live owner must not leave a pending transcript row behind."""
    from api import commands, routes

    class Session:
        session_id = "registration-race-session"
        profile = "default"
        read_only = False
        is_read_only = False

        def __init__(self):
            self.messages = []
            self.saved = 0

        def save(self):
            self.saved += 1

    session = Session()
    command_id = "webui-command-registration-race"
    routes._invalidate_webui_command_runs(session.session_id)
    live_token = routes._register_webui_command_run(session.session_id, command_id)
    assert live_token
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.RLock())
    monkeypatch.setattr(commands, "execute_agent_command", lambda _command: "must not run")

    try:
        handler = _RouteHandler({
            "command": "/memory pending",
            "session_id": session.session_id,
            "command_id": command_id,
        })
        routes.handle_post(handler, SimpleNamespace(path="/api/commands/exec", query=""))
        assert handler.status == 409
        assert session.messages == []
        assert session.saved == 0  # registration loses before any marker is persisted
    finally:
        routes._unregister_webui_command_run(session.session_id, command_id, live_token)


def test_commands_exec_stale_finalizer_cannot_overwrite_replacement_marker(monkeypatch):
    """An in-flight result must not resurrect itself into a same-id row created after clear."""
    from api import commands, routes

    entered = threading.Event()
    release = threading.Event()
    lock = threading.RLock()
    command_id = "webui-command-clear-race"

    class Session:
        session_id = "clear-race-session"
        profile = "default"
        read_only = False
        is_read_only = False

        def __init__(self):
            self.messages = []

        def save(self):
            pass

    session = Session()

    def execute(_command):
        entered.set()
        assert release.wait(5)
        return "stale result"

    routes._invalidate_webui_command_runs(session.session_id)
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock)
    monkeypatch.setattr(commands, "execute_agent_command", execute)
    handler = _RouteHandler({
        "command": "/memory pending",
        "session_id": session.session_id,
        "command_id": command_id,
    })
    thread = threading.Thread(
        target=routes.handle_post,
        args=(handler, SimpleNamespace(path="/api/commands/exec", query="")),
        daemon=True,
    )
    thread.start()
    assert entered.wait(5)

    with lock:
        routes._invalidate_webui_command_runs(session.session_id)
        session.messages = []
        replacement_token = routes._register_webui_command_run(session.session_id, command_id)
        assert replacement_token
        session.messages.extend([
            {"role": "user", "content": "/memory pending", "_webui_command_id": command_id},
            {
                "role": "assistant",
                "content": "replacement pending",
                "_webui_command_id": command_id,
                "_webui_command_pending": True,
                "_webui_command_run_token": replacement_token,
            },
        ])

    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert handler.status == 200
    assert handler.json_body()["persistence_warning"] is True
    assert session.messages[1]["content"] == "replacement pending"
    assert session.messages[1]["_webui_command_pending"] is True
    assert routes._webui_command_run_is_active(
        session.session_id, command_id, replacement_token
    )
    routes._unregister_webui_command_run(
        session.session_id, command_id, replacement_token
    )


@pytest.mark.parametrize("mutation", ["clear", "truncate"])
def test_commands_exec_same_id_cannot_rerun_while_session_mutation_is_in_flight(monkeypatch, tmp_path, mutation):
    """Clearing or truncating must not release a live command's idempotency fence."""
    from api import commands, config, routes, session_ops

    entered = threading.Event()
    release = threading.Event()
    calls = []
    lock = threading.RLock()
    command_id = "webui-command-clear-running"
    sid = "clear-running-command-session"
    session_path = tmp_path / "session.json"

    class Session:
        session_id = sid
        profile = "default"
        read_only = False
        is_read_only = False
        parent_session_id = None
        path = session_path

        def __init__(self):
            self.messages = []
            self.context_messages = []
            self.tool_calls = []
            self.saved = 0

        def save(self):
            self.saved += 1
            self.path.write_text(json.dumps({
                "messages": self.messages,
                "context_messages": self.context_messages,
                "truncation_watermark": 0.0,
                "truncation_boundary": 0.0,
                "active_stream_id": getattr(self, "active_stream_id", None),
                "pending_user_message": getattr(self, "pending_user_message", None),
                "pending_attachments": getattr(self, "pending_attachments", []),
                "pending_started_at": getattr(self, "pending_started_at", None),
                "pending_user_source": getattr(self, "pending_user_source", None),
                "clear_generation": getattr(self, "clear_generation", None),
            }))

        def compact(self):
            return {"session_id": self.session_id, "messages": self.messages}

    session = Session()

    def execute(_command):
        calls.append(_command)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return "first result"
        return "duplicate result"

    def clear_messages(target, _keep):
        target.messages = []
        target.context_messages = []
        target.truncation_watermark = 0.0
        target.truncation_boundary = 0.0
        return 0, 0

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock)
    monkeypatch.setattr(commands, "execute_agent_command", execute)
    monkeypatch.setattr(session_ops, "truncate_session_at_keep", clear_messages)
    monkeypatch.setattr(session_ops, "apply_session_title_rename", lambda *_args: None)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None)

    payload = {
        "command": "/memory pending",
        "session_id": sid,
        "command_id": command_id,
    }
    first_handler = _RouteHandler(payload)
    first_thread = threading.Thread(
        target=routes.handle_post,
        args=(first_handler, SimpleNamespace(path="/api/commands/exec", query="")),
        daemon=True,
    )
    first_thread.start()
    assert entered.wait(5)

    mutation_handler = _RouteHandler({
        "session_id": sid,
        **({"keep_count": 0} if mutation == "truncate" else {}),
    })
    routes.handle_post(
        mutation_handler,
        SimpleNamespace(
            path=f"/api/session/{mutation}",
            query="",
        ),
    )
    assert mutation_handler.status == 200

    retry_handler = _RouteHandler(payload)
    routes.handle_post(retry_handler, SimpleNamespace(path="/api/commands/exec", query=""))
    assert retry_handler.status == 409
    assert calls == ["/memory pending"]

    release.set()
    first_thread.join(5)
    assert not first_thread.is_alive()
    assert first_handler.status == 200


def test_commands_exec_recovers_orphaned_pending_marker_without_reexecution(monkeypatch):
    """A same-id retry after restart settles an orphan marker without repeating its side effect."""
    from api import commands, routes

    command_id = "webui-command-interrupted"

    class Session:
        session_id = "interrupted-command-session"
        profile = "default"
        read_only = False
        is_read_only = False

        def __init__(self):
            self.messages = [
                {
                    "role": "user",
                    "content": "/memory approve stable-id",
                    "_webui_command_id": command_id,
                },
                {
                    "role": "assistant",
                    "content": "Command started; its final result has not been saved yet.",
                    "_webui_command_id": command_id,
                    "_webui_command_pending": True,
                },
            ]
            self.saved = 0

        def save(self):
            self.saved += 1

    session = Session()
    calls = []
    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.Lock())
    monkeypatch.setattr(commands, "execute_agent_command", lambda command: calls.append(command) or "must not run")
    payload = {
        "command": "/memory approve stable-id",
        "session_id": session.session_id,
        "command_id": command_id,
    }

    handler = _RouteHandler(payload)
    routes.handle_post(handler, SimpleNamespace(path="/api/commands/exec", query=""))

    assert handler.status == 200
    response = handler.json_body()
    assert response["command_id"] == command_id
    assert response["recovered_interrupted"] is True
    assert "may have run" in response["output"]
    assert calls == []
    assert session.saved == 1
    assert "_webui_command_pending" not in session.messages[1]
    assert session.messages[1]["_webui_command_interrupted"] is True


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


def test_skills_pending_dispatches_to_write_approval_handler(monkeypatch):
    """`/skills pending` must reach the shared write-approval store, not the local
    skill search -- this is the gap Greptile flagged as still-missing after the
    return-false-only fix on PR #7623 (embedded WebUI sessions never routed slash
    commands anywhere; /api/chat/start hands raw text straight to
    AIAgent.run_conversation)."""
    calls = _install_fake_write_approval(monkeypatch, result="No pending skills writes.")

    from api.commands import execute_agent_command

    output = execute_agent_command('/skills pending')

    assert output == "No pending skills writes."
    assert len(calls) == 1
    subsystem, args, memory_store, set_mode_fn = calls[0]
    assert subsystem == "skills"
    assert args == ["pending"]
    assert memory_store is None
    assert callable(set_mode_fn)


@pytest.mark.parametrize("subcommand", [
    "pending", "approve", "apply", "reject", "deny", "drop", "diff", "approval", "mode",
])
def test_skills_write_approval_all_aliases_dispatch(monkeypatch, subcommand):
    """Every alias the agent's handler accepts must reach it -- 'completes the
    class' the same way the merged alias-expansion commit did for the browser-side
    shadow check, now for the backend dispatch side too."""
    calls = _install_fake_write_approval(monkeypatch, result="handled")

    from api.commands import execute_agent_command

    output = execute_agent_command(f'/skills {subcommand} abc123')

    assert output == "handled"
    assert calls[0][1] == [subcommand, "abc123"]


def test_skills_write_approval_runs_inside_active_profile_context(monkeypatch):
    """write_approval.py resolves the skill store via the legacy get_hermes_home() path
    (process env / TLS), not anything webui-request-scoped -- without wrapping the call
    in _bundle_profile_context, a browser that has selected a non-root profile would
    silently read/write the WRONG profile's pending skill writes. Proves the ENTER/CALL/
    EXIT ordering (the call happens strictly inside the context), not just that the
    context manager was constructed somewhere."""
    from contextlib import contextmanager

    order = []
    calls = _install_fake_write_approval(monkeypatch, result="ok")

    @contextmanager
    def fake_profile_context(purpose):
        order.append(("enter", purpose))
        yield
        order.append(("exit", purpose))

    import api.commands as commands_mod
    monkeypatch.setattr(commands_mod, "_bundle_profile_context", fake_profile_context)
    orig_len = len(calls)

    import sys
    write_approval_commands = sys.modules["hermes_cli.write_approval_commands"]
    orig_handler = write_approval_commands.handle_pending_subcommand

    def recording_handler(*a, **k):
        order.append(("call", a[0]))
        return orig_handler(*a, **k)

    monkeypatch.setattr(write_approval_commands, "handle_pending_subcommand", recording_handler)

    from api.commands import execute_agent_command
    output = execute_agent_command('/skills pending')

    assert output == "ok"
    assert len(calls) == orig_len + 1
    assert order == [("enter", "/api/commands/exec:skills"), ("call", "skills"),
                      ("exit", "/api/commands/exec:skills")], order


def test_memory_write_approval_runs_inside_active_profile_context(monkeypatch):
    """Same as the /skills version above, for /memory -- both load_on_disk_store() and
    handle_pending_subcommand() must run inside the active-profile context, since the
    memory store path is resolved the same legacy, non-request-scoped way."""
    from contextlib import contextmanager

    order = []
    calls = _install_fake_write_approval(monkeypatch, result="ok")

    @contextmanager
    def fake_profile_context(purpose):
        order.append(("enter", purpose))
        yield
        order.append(("exit", purpose))

    import api.commands as commands_mod
    monkeypatch.setattr(commands_mod, "_bundle_profile_context", fake_profile_context)

    import sys
    write_approval_commands = sys.modules["hermes_cli.write_approval_commands"]
    orig_handler = write_approval_commands.handle_pending_subcommand

    def recording_handler(*a, **k):
        order.append(("call_handler", a[0]))
        return orig_handler(*a, **k)

    monkeypatch.setattr(write_approval_commands, "handle_pending_subcommand", recording_handler)

    memory_tool = sys.modules["tools.memory_tool"]
    orig_load = memory_tool.load_on_disk_store

    def recording_load():
        order.append(("call_load_store", None))
        return orig_load()

    monkeypatch.setattr(memory_tool, "load_on_disk_store", recording_load)

    from api.commands import execute_agent_command
    output = execute_agent_command('/memory pending')

    assert output == "ok"
    assert len(calls) == 1
    assert order == [
        ("enter", "/api/commands/exec:memory"),
        ("call_load_store", None),
        ("call_handler", "memory"),
        ("exit", "/api/commands/exec:memory"),
    ], order


def test_memory_pending_dispatches_to_write_approval_handler(monkeypatch):
    """`/memory pending` must reach the shared write-approval store via
    /api/commands/exec, same gap as /skills had -- /memory has no `cli_only` flag
    and isn't in messages.js' _AGENT_COMMANDS_RUN_ON_WEBUI allowlist by default
    either, so it fell through to plain chat text with no local feature even
    shadowing it (unlike /skills, which at least had a wrong-but-visible answer)."""
    calls = _install_fake_write_approval(monkeypatch, result="No pending memory writes.")

    from api.commands import execute_agent_command

    output = execute_agent_command('/memory pending')

    assert output == "No pending memory writes."
    assert len(calls) == 1
    subsystem, args, memory_store, set_mode_fn = calls[0]
    assert subsystem == "memory"
    assert args == ["pending"]
    assert memory_store is _FAKE_MEMORY_STORE, \
        "must pass load_on_disk_store()'s object through, not None (unlike /skills)"
    assert callable(set_mode_fn)


@pytest.mark.parametrize("subcommand", [
    "pending", "approve", "apply", "reject", "deny", "drop", "approval", "mode",
])
def test_memory_write_approval_all_aliases_dispatch(monkeypatch, subcommand):
    """Every alias the agent's handler accepts must reach it. No `diff` here --
    memory entries are small enough to review inline, so the shared dispatcher
    never defines one for the memory subsystem (matches gateway's own /memory)."""
    calls = _install_fake_write_approval(monkeypatch, result="handled")

    from api.commands import execute_agent_command

    output = execute_agent_command(f'/memory {subcommand} abc123')

    assert output == "handled"
    assert calls[0][1] == [subcommand, "abc123"]


def test_memory_bare_command_reaches_handler(monkeypatch):
    """A bare `/memory` (no args) must reach the handler too -- it shows gate
    status + the pending list, exactly like gateway's own /memory. Unlike
    /skills, there is no competing local feature to protect, so /memory has no
    reserved-subcommand allowlist to gate a bare call out of."""
    calls = _install_fake_write_approval(monkeypatch, result="memory.write_approval = off\n\nNo pending memory writes.")

    from api.commands import execute_agent_command

    output = execute_agent_command('/memory')

    assert "write_approval" in output
    assert calls[0][1] == []


def test_memory_write_approval_runtime_unavailable_is_generic_error(monkeypatch):
    """Mirrors the /skills case: an unimportable write-approval runtime must fail
    as a sanitized RuntimeError (500), not an unhandled ImportError traceback."""
    import sys

    monkeypatch.delitem(sys.modules, "hermes_cli.write_approval_commands", raising=False)
    monkeypatch.delitem(sys.modules, "tools.write_approval", raising=False)
    monkeypatch.delitem(sys.modules, "tools.memory_tool", raising=False)

    import builtins
    real_import = builtins.__import__

    def _no_write_approval(name, *a, **k):
        if name in ("hermes_cli.write_approval_commands", "tools.write_approval",
                    "tools.memory_tool", "tools"):
            raise ImportError(f"no module named {name}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_write_approval)

    from api.commands import execute_agent_command

    with pytest.raises(RuntimeError):
        execute_agent_command('/memory pending')


def test_memory_approval_toggle_uses_its_own_config_namespace(tmp_path):
    """`_write_approval_setter` is shared between /skills and /memory -- prove
    'memory' writes under its OWN top-level config key, not 'skills' (a copy-paste
    subsystem-string bug here would silently cross-wire the two gates)."""
    from api import config as webui_config
    from api.commands import _write_approval_setter

    config_data = {}
    orig_get_path = webui_config._get_config_path
    orig_load = webui_config._load_yaml_config_file_raw
    orig_save = webui_config._save_yaml_config_file
    orig_reload = webui_config.reload_config
    try:
        webui_config._get_config_path = lambda: tmp_path / "config.yaml"
        webui_config._load_yaml_config_file_raw = lambda path: config_data
        webui_config._save_yaml_config_file = lambda path, data: None
        webui_config.reload_config = lambda: None

        _write_approval_setter('memory')(True)
    finally:
        webui_config._get_config_path = orig_get_path
        webui_config._load_yaml_config_file_raw = orig_load
        webui_config._save_yaml_config_file = orig_save
        webui_config.reload_config = orig_reload

    assert config_data == {"memory": {"write_approval": True}}
    assert "skills" not in config_data


def test_skills_plain_query_not_routed_to_write_approval(monkeypatch):
    """A bare `/skills` or a real search query must NEVER reach write-approval --
    it stays a KeyError so execute_agent_command's caller falls through to the
    normal allowlist-miss handling (and the WebUI keeps doing its local search)."""

    def _boom(*a, **k):
        raise AssertionError("write-approval must not be touched for a plain search")

    import sys
    tools_pkg = ModuleType("tools")
    tools_pkg.__path__ = []
    write_approval = ModuleType("tools.write_approval")
    cast(Any, write_approval).SKILLS = "skills"
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.write_approval", write_approval)
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    write_approval_commands = ModuleType("hermes_cli.write_approval_commands")
    cast(Any, write_approval_commands).handle_pending_subcommand = _boom
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.write_approval_commands", write_approval_commands)

    from api.commands import execute_agent_command

    with pytest.raises(KeyError):
        execute_agent_command('/skills executive-triage')
    with pytest.raises(KeyError):
        execute_agent_command('/skills')


def test_skills_write_approval_runtime_unavailable_is_generic_error(monkeypatch):
    """If hermes-agent's write-approval modules can't be imported, the endpoint must
    fail with a sanitized RuntimeError (500), not an unhandled ImportError leaking
    a traceback -- matching every other agent-runtime dependency in this file."""
    import sys

    monkeypatch.delitem(sys.modules, "hermes_cli.write_approval_commands", raising=False)
    monkeypatch.delitem(sys.modules, "tools.write_approval", raising=False)

    import builtins
    real_import = builtins.__import__

    def _no_write_approval(name, *a, **k):
        if name in ("hermes_cli.write_approval_commands", "tools.write_approval", "tools"):
            raise ImportError(f"no module named {name}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_write_approval)

    from api.commands import execute_agent_command

    with pytest.raises(RuntimeError):
        execute_agent_command('/skills pending')


@pytest.mark.parametrize("subsystem", ["skills", "memory"])
def test_approval_toggle_preserves_env_var_placeholders_in_config(tmp_path, monkeypatch, subsystem):
    """Toggling the gate must not bake resolved secrets into config.yaml.

    `_load_yaml_config_file()` expands `${VAR}` references; writing that result
    back replaced every placeholder with its live value on disk (and froze env-var
    rotation). The toggle must read the RAW file. This test uses the real loader
    and saver -- no mocks of either -- so it fails if the wrong loader is used."""
    from api import config as webui_config
    from api.commands import _write_approval_setter

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "providers:\n"
        "  main:\n"
        "    api_key: ${GATE_ROTATING_TOKEN}\n"
        f"{subsystem}:\n"
        "  write_approval: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATE_ROTATING_TOKEN", "token-v1")
    monkeypatch.setattr(webui_config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(webui_config, "reload_config", lambda: None)

    _write_approval_setter(subsystem)(True)

    on_disk = config_path.read_text(encoding="utf-8")
    assert "${GATE_ROTATING_TOKEN}" in on_disk
    assert "token-v1" not in on_disk
    assert webui_config._load_yaml_config_file_raw(config_path)[subsystem]["write_approval"] is True


def test_skills_approval_toggle_persists_via_webui_config(tmp_path):
    """`/skills approval on|off`'s set_mode_fn must persist through the webui's own
    config module (there is no gateway session to route the gateway-side
    persistence through), reading the RAW file (not `get_config()`, which may hand
    back a merged-with-defaults snapshot that must never be written back)."""
    from api import config as webui_config
    from api.commands import _write_approval_setter

    config_data = {"skills": {"write_approval": False}}
    saved = []
    reloaded = []

    orig_get_path = webui_config._get_config_path
    orig_load = webui_config._load_yaml_config_file_raw
    orig_save = webui_config._save_yaml_config_file
    orig_reload = webui_config.reload_config
    try:
        webui_config._get_config_path = lambda: tmp_path / "config.yaml"
        webui_config._load_yaml_config_file_raw = lambda path: config_data
        webui_config._save_yaml_config_file = lambda path, data: saved.append((path, data.copy()))
        webui_config.reload_config = lambda: reloaded.append(True)

        _write_approval_setter('skills')(True)
    finally:
        webui_config._get_config_path = orig_get_path
        webui_config._load_yaml_config_file_raw = orig_load
        webui_config._save_yaml_config_file = orig_save
        webui_config.reload_config = orig_reload

    assert config_data["skills"]["write_approval"] is True
    assert saved == [(tmp_path / "config.yaml", {"skills": {"write_approval": True}})]
    assert reloaded == [True]


def test_skills_approval_toggle_serializes_under_shared_cfg_lock(monkeypatch):
    """Two concurrent `/skills approval` calls must not interleave their
    read-modify-write of config.yaml -- the exact race Greptile flagged (a
    concurrent config writer saving between this read and this write, discarding
    that other update). Uses the REAL `_cfg_lock`, not a mock of it."""
    from api import config as webui_config
    from api.commands import _write_approval_setter

    state = {"active": 0, "max_active": 0}
    track_lock = threading.Lock()

    def _load(path):
        with track_lock:
            state["active"] += 1
            if state["active"] > state["max_active"]:
                state["max_active"] = state["active"]
        time.sleep(0.12)
        with track_lock:
            state["active"] -= 1
        return {}

    monkeypatch.setattr(webui_config, "_get_config_path", lambda: pathlib.Path("/tmp/fake.yaml"))
    monkeypatch.setattr(webui_config, "_load_yaml_config_file_raw", _load)
    monkeypatch.setattr(webui_config, "_save_yaml_config_file", lambda path, data: None)
    monkeypatch.setattr(webui_config, "reload_config", lambda: None)

    setter = _write_approval_setter('skills')
    errors = []

    def _call(enabled):
        try:
            setter(enabled)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_call, args=(True,))
    t2 = threading.Thread(target=_call, args=(False,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert not errors
    assert state["max_active"] == 1, "the shared _cfg_lock must serialize concurrent read-modify-writes"


def test_skills_approval_toggle_reloads_after_releasing_lock(monkeypatch):
    """`reload_config()` must run AFTER `_cfg_lock` is released, not while held --
    the real `reload_config()` acquires that same non-reentrant lock internally, so
    calling it from inside the `with` block would deadlock. Fakes reload_config to
    itself contend for the real lock with a short timeout, proving it isn't already
    held when reload_config runs."""
    from api import config as webui_config
    from api.commands import _write_approval_setter

    monkeypatch.setattr(webui_config, "_get_config_path", lambda: pathlib.Path("/tmp/fake.yaml"))
    monkeypatch.setattr(webui_config, "_load_yaml_config_file_raw", lambda path: {})
    monkeypatch.setattr(webui_config, "_save_yaml_config_file", lambda path, data: None)

    acquired = webui_config._cfg_lock.acquire(blocking=False)
    assert acquired, "test setup: _cfg_lock must be free before the call"
    webui_config._cfg_lock.release()

    reload_acquired = []

    def _fake_reload():
        got = webui_config._cfg_lock.acquire(timeout=1)
        reload_acquired.append(got)
        if got:
            webui_config._cfg_lock.release()

    monkeypatch.setattr(webui_config, "reload_config", _fake_reload)

    _write_approval_setter('skills')(True)

    assert reload_acquired == [True], (
        "reload_config() could not acquire _cfg_lock -- it is still held, "
        "meaning reload_config() is being called INSIDE the locked block (deadlock risk)")


def test_commands_exec_routes_skills_pending_over_http():
    """Full HTTP round trip through the real server: `/skills pending` must reach the
    write-approval dispatcher, not be rejected as an unsupported command.

    Deliberately NOT gated on `requires_agent_modules`: CI has no hermes-agent installed, so a
    skip there would make this test collect-and-pass while asserting nothing (the exact failure
    mode of a silently-skipping suite). Both environments prove the request was ROUTED:
      * agent present  -> 200 with the real pending listing;
      * agent absent   -> 500 with the dispatcher's own generic 'runtime unavailable' error
        (raised only AFTER the allowlist + dispatch accepted the command).
    A regression in the allowlist or dispatch wiring yields 404 (KeyError -> unsupported), which
    fails both branches. The handler's behavior against a fake agent is covered in-process by
    test_skills_pending_dispatches_to_write_approval_handler."""
    status, body = _post('/api/commands/exec', {'command': '/skills pending'})
    assert status != 404, f"/skills was rejected as an unsupported command: {body}"
    if status == 200:
        assert isinstance(body.get('output'), str) and body['output'].strip()
        assert 'not a supported' not in body['output'].lower()
    else:
        assert status == 500, (status, body)
        assert body == {'error': 'Skill write-approval runtime unavailable'}, body


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
