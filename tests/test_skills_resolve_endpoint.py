"""Tests for the /api/commands/skills/resolve endpoint and frontend wiring."""

import queue
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType
from unittest import mock

import sys
import pytest

import api.commands as commands

_MISSING = object()

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (REPO_ROOT / "static" / "commands.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
COMMANDS_PY = (REPO_ROOT / "api" / "commands.py").read_text(encoding="utf-8")


def _install_fake_skill_commands(monkeypatch, *, resolver=None, builder=None,
                                 stacked_builder=None, splitter=None):
    """Install a fake agent.skill_commands module for testing resolve_skill_command."""

    agent_pkg = sys.modules.get("agent") or ModuleType("agent")
    monkeypatch.setattr(agent_pkg, "__path__", [], raising=False)
    skill_commands = ModuleType("agent.skill_commands")
    skill_commands.resolve_skill_command_key = resolver or (lambda name: None)
    skill_commands.build_skill_invocation_message = builder or (
        lambda key, instr="", task_id=None, runtime_note="": None
    )
    skill_commands.build_stacked_skill_invocation_message = stacked_builder or (
        lambda keys, instr="", task_id=None: None
    )
    skill_commands.split_stacked_skill_commands = splitter or (
        lambda rest: ([], rest)
    )
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_commands", skill_commands)


# ── Static source-code assertions (JS frontend) ─────────────────────────────


def test_resolve_skill_command_helper_defined_in_commands_js():
    """resolveSkillCommand() must be present in commands.js."""
    assert "async function resolveSkillCommand(text, sessionId)" in COMMANDS_JS
    assert "api('/api/commands/skills/resolve'" in COMMANDS_JS


def test_resolve_skill_command_uses_post_with_command_body():
    """resolveSkillCommand() must POST a JSON body with a 'command' field
    and optionally a 'session_id' field."""
    idx = COMMANDS_JS.index("async function resolveSkillCommand(text, sessionId)")
    body = COMMANDS_JS[idx:]
    assert "method:'POST'" in body
    assert "body:JSON.stringify(body)" in body
    assert "body.session_id=sessionId" in body
    assert "throw new Error('command is required')" in body


def test_skill_dispatch_appears_after_bundle_block():
    """The skill slash-command intercept must appear after the bundle block
    in send(), so bundles have priority over single skills."""
    bundle_idx = MESSAGES_JS.find("if(_bundleCmd){")
    skill_idx = MESSAGES_JS.find("// ── Skill commands:")
    assert bundle_idx != -1
    assert skill_idx != -1
    assert bundle_idx < skill_idx


def test_skill_dispatch_checks_loadSkillCommands():
    """The skill intercept must call loadSkillCommands() and match by slug name."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "loadSkillCommands()" in body
    assert "_skillCommandSlug(_parsedCmd.name)" in body
    # RAW/agent-only split: the expansion is carried separately, never by
    # mutating the outbound `text` (the persisted message stays `/skill …`).
    assert "_skillAgentMessage = _skillMessage" in body
    assert "text = _skillMessage" not in body


def test_skill_dispatch_skips_when_bundle_or_agent_cmd_matched():
    """The skill check guards on !_bundleCmd and !_agentCmd so bundles and
    agent commands have priority."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "!_bundleCmd" in body
    assert "!_agentCmd" in body


def test_skill_dispatch_falls_through_silently_on_error():
    """If the server endpoint fails, the catch block must not show an error
    to the user — the raw text falls through to the agent."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "} catch(_e){" in body
    assert "Silently fall through" in body


def test_skill_dispatch_passes_session_id():
    """The skill intercept must pass S.session.session_id to
    resolveSkillCommand()."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "resolveSkillCommand(text, S.session && S.session.session_id)" in body


def test_skill_dispatch_normalizes_underscores_to_slug():
    """`/my_skill` must match the cached `my-skill` entry, mirroring the Agent
    contract's `_` → `-` normalization (gate-fail #4)."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "_skillCommandSlug(_parsedCmd.name)" in body
    assert "s.name === _slug" in body


def test_skill_dispatch_creates_session_before_resolve():
    """First-message skill sends must create a session before resolving so
    ${HERMES_SESSION_ID} templates correctly (gate-fail #3)."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "if(!S.session){await newSession();await renderSessionList();}" in body


def test_skill_dispatch_keeps_raw_message_out_of_model_mutation():
    """The skill intercept must never replace the outbound `text` with the
    expansion — the RAW `/skill …` command is the persisted/displayed message
    and the expansion travels separately as `agent_message`."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "text = _skillMessage" not in body
    assert "_skillAgentMessage = _skillMessage" in body


def test_chat_start_post_carries_agent_message_separately():
    """The /api/chat/start POST must keep `message` as the RAW command and
    carry the expansion in a separate agent-only `agent_message` field."""
    post_idx = MESSAGES_JS.find("const startData=await api('/api/chat/start'")
    assert post_idx != -1, "chat/start POST body not found"
    body = MESSAGES_JS[post_idx:]
    assert "message:msgText" in body
    assert "agent_message:_skillAgentMessage||undefined" in body


def test_profile_switch_invalidates_slash_command_caches():
    """Profile A's skill/agent/bundle command lists must never remain
    authoritative in profile B (gate-fail #7): switchToProfile must drop the
    cached lists and invalidateSlashSkillCaches must reset all three."""
    panels_src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    switch_idx = panels_src.find("async function switchToProfile(")
    assert switch_idx != -1, "switchToProfile not found"
    switch_body = panels_src[switch_idx:]
    assert "window.invalidateSlashSkillCaches" in switch_body
    assert "_bundleCommandCacheReady=false" in COMMANDS_JS
    assert "_agentCommandCacheReady=false" in COMMANDS_JS


def test_skill_cache_failure_remains_retryable():
    """Failed /api/skills loads must not mark the cache ready, so the next
    send-path lookup retries instead of serving a stale empty list
    (gate-fail #6)."""
    assert "_skillCommandCacheReady=true" in COMMANDS_JS
    cache_body = COMMANDS_JS[COMMANDS_JS.find("async function loadSkillCommands"):]
    cache_body = cache_body[:cache_body.find("\nasync function ")]
    assert "catch(_){" in cache_body
    # The success path sets the ready flag inside try; the catch path must not.
    try_idx = cache_body.find("_skillCommandCacheReady=true;")
    catch_idx = cache_body.find("catch(_){")
    assert try_idx != -1 and catch_idx != -1
    assert try_idx < catch_idx, "ready flag must be set on success only"


# ── Static source-code assertions (Python backend) ─────────────────────────


def test_skills_resolve_route_wired():
    """POST /api/commands/skills/resolve must be registered in routes.py."""
    assert '/api/commands/skills/resolve"' in ROUTES_PY
    assert "resolve_skill_command(command, session_id=session_id)" in ROUTES_PY


def test_resolve_skill_command_function_defined():
    """resolve_skill_command() must be defined in api/commands.py."""
    assert "def resolve_skill_command(command: str, session_id: str | None = None) -> dict[str, Any]:" in COMMANDS_PY
    assert "build_skill_invocation_message" in COMMANDS_PY


def test_skills_resolve_route_is_after_bundles_route():
    """The skills resolve route must be registered after the bundles resolve
    route, so bundle matching gets priority at the HTTP layer too."""
    bundle_route = ROUTES_PY.index('bundles/resolve')
    skills_route = ROUTES_PY.index('skills/resolve')
    assert bundle_route < skills_route


def test_skills_resolve_route_handles_errors():
    """The POST handler must return proper HTTP error codes."""
    route_block = ROUTES_PY[ROUTES_PY.index('/api/commands/skills/resolve'):]
    route_block = route_block[:route_block.index('if parsed.path ==')]
    assert "session_id = str(body.get(" in route_block
    assert "bad(handler, \"command is required\")" in route_block
    assert "bad(handler, \"Skill command not found\", 404)" in route_block
    assert "bad(handler, str(e), 400)" in route_block
    assert "bad(handler, _sanitize_error(e), 500)" in route_block


# ── Server-side function tests (monkeypatched) ──────────────────────────────


def test_resolve_skill_command_uses_skill_runtime(monkeypatch):
    """resolve_skill_command() calls resolve_skill_command_key and
    build_skill_invocation_message from agent.skill_commands, then
    returns the result."""
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        seen["purpose"] = purpose
        yield

    def _resolve(name):
        seen["resolve_name"] = name
        return "/llm-wiki" if name == "llm-wiki" else None

    def _build(key, instr="", task_id=None, runtime_note=""):
        seen["build"] = (key, instr, task_id)
        return "[IMPORTANT: The user has invoked the \"llm-wiki\" skill...]\nfull skill body\nUser instruction: list pages"

    _install_fake_skill_commands(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/llm-wiki list pages")

    assert result == {
        "name": "llm-wiki",
        "source": "skill",
        "message": '[IMPORTANT: The user has invoked the "llm-wiki" skill...]\nfull skill body\nUser instruction: list pages',
    }
    assert seen == {
        "purpose": "/api/commands/skills/resolve",
        "resolve_name": "llm-wiki",
        "build": ("/llm-wiki", "list pages", None),
    }


def test_resolve_skill_command_passes_session_id_as_task_id(monkeypatch):
    """When session_id is provided, it must be forwarded as task_id to
    both build_skill_invocation_message and build_stacked_skill_invocation_message."""
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        yield

    def _resolve(name):
        return f"/{name}"

    def _build(key, instr="", task_id=None, runtime_note=""):
        seen["task_id"] = task_id
        return "resolved skill body"

    _install_fake_skill_commands(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    commands.resolve_skill_command("/llm-wiki list pages", session_id="test-session-123")

    assert seen["task_id"] == "test-session-123"


def test_resolve_skill_command_extracts_user_instruction(monkeypatch):
    """The user instruction (text after the skill name) must be passed as the
    second argument to build_skill_invocation_message."""
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        seen["purpose"] = purpose
        yield

    def _resolve(name):
        return f"/{name}"

    def _build(key, instr="", task_id=None, runtime_note=""):
        seen["build"] = (key, instr, task_id)
        return f"resolved: {instr}"

    _install_fake_skill_commands(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/gif-search cats and dogs")

    assert result["message"] == "resolved: cats and dogs"
    assert seen["build"] == ("/gif-search", "cats and dogs", None)


def test_resolve_skill_command_no_instruction(monkeypatch):
    """When there is no user text after the skill name, the instruction must
    be an empty string (not None)."""
    seen = {}

    @contextmanager
    def _profile_scope(purpose):
        seen["purpose"] = purpose
        yield

    def _resolve(name):
        return f"/{name}"

    def _build(key, instr="", task_id=None, runtime_note=""):
        seen["build"] = (key, instr, task_id)
        assert instr == "", f"Expected empty string, got {instr!r}"
        return f"resolved bare skill: {key}"

    _install_fake_skill_commands(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/llm-wiki")

    assert result["message"] == "resolved bare skill: /llm-wiki"
    assert seen["build"][1] == ""
    assert seen["build"][2] is None  # task_id should be None


def test_resolve_skill_command_raises_for_unknown_skill(monkeypatch):
    """An unrecognised skill name must raise KeyError."""
    _install_fake_skill_commands(monkeypatch)
    monkeypatch.setattr(commands, "_bundle_profile_context", lambda purpose: nullcontext())

    with pytest.raises(KeyError):
        commands.resolve_skill_command("/does-not-exist investigate this")


def test_resolve_skill_command_raises_on_empty_message(monkeypatch):
    """If build_skill_invocation_message returns None or empty string,
    resolve_skill_command must raise RuntimeError."""

    @contextmanager
    def _profile_scope(purpose):
        yield

    def _resolve(_name):
        return f"/{_name}"

    def _build(_key, _instr="", **kwargs):
        return None  # simulate failure

    _install_fake_skill_commands(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    with pytest.raises(RuntimeError, match="Skill command returned no invocation text"):
        commands.resolve_skill_command("/llm-wiki do stuff")


def test_resolve_skill_command_wraps_unexpected_runtime_errors(monkeypatch):
    """Unexpected errors from agent.skill_commands must be wrapped in a
    generic RuntimeError to avoid leaking internals."""

    def _explode(_name):
        raise AttributeError("skill runtime broke")

    _install_fake_skill_commands(monkeypatch, resolver=_explode)
    monkeypatch.setattr(commands, "_bundle_profile_context", lambda purpose: nullcontext())

    with pytest.raises(RuntimeError, match="Skill command unavailable"):
        commands.resolve_skill_command("/llm-wiki hello")


def test_resolve_skill_command_preserves_leading_slash(monkeypatch):
    """The command may or may not start with a leading slash; both forms
    must resolve the same name."""
    seen_slash = {}
    seen_no_slash = {}

    @contextmanager
    def _profile_scope(purpose):
        yield

    def _build_slash(key, instr="", **kwargs):
        seen_slash["build"] = (key, instr)
        return f"ok: {key}"

    def _build_no_slash(key, instr="", **kwargs):
        seen_no_slash["build"] = (key, instr)
        return f"ok: {key}"

    def _resolve_slash(name):
        return f"/{name}"

    def _resolve_no_slash(name):
        return f"/{name}"

    _install_fake_skill_commands(monkeypatch, resolver=_resolve_slash, builder=_build_slash)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)
    commands.resolve_skill_command("/llm-wiki hello")

    _install_fake_skill_commands(monkeypatch, resolver=_resolve_no_slash, builder=_build_no_slash)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)
    commands.resolve_skill_command("llm-wiki hello")

    assert seen_slash["build"][0] == "/llm-wiki"
    assert seen_no_slash["build"][0] == "/llm-wiki"


# ── Stacked skill invocation tests ────────────────────────────────────────


def test_resolve_stacked_skills_detects_extra_keys(monkeypatch):
    """``/skill-a /skill-b do X`` must detect the second skill as a stacked key
    and call build_stacked_skill_invocation_message instead of the single
    builder."""

    @contextmanager
    def _profile_scope(purpose):
        yield

    seen = {"type": None, "keys": None, "instr": None}

    def _resolve(name):
        return f"/{name}"

    def _splitter(rest):
        if rest and rest.startswith("/skill-b"):
            return (["/skill-b"], "do X")
        return ([], rest)

    def _stacked_builder(keys, instr="", task_id=None):
        seen["type"] = "stacked"
        seen["keys"] = keys
        seen["instr"] = instr
        return (f"stacked body: loaded! (task_id={task_id})", ["skill-a", "skill-b"], [])

    def _single_builder(key, instr="", task_id=None, runtime_note=""):
        seen["type"] = "single"
        seen["keys"] = [key]
        seen["instr"] = instr
        return None  # Should not be called

    _install_fake_skill_commands(
        monkeypatch,
        resolver=_resolve,
        builder=_single_builder,
        stacked_builder=_stacked_builder,
        splitter=_splitter,
    )
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/skill-a /skill-b do X", session_id="sess-xyz")

    assert result["name"] == "skill-a"
    assert result["source"] == "stacked_skill"
    assert result["message"] == "stacked body: loaded! (task_id=sess-xyz)"
    assert seen["type"] == "stacked"
    assert seen["keys"] == ["/skill-a", "/skill-b"]
    assert seen["instr"] == "do X"


def test_resolve_stacked_skills_falls_back_to_single(monkeypatch):
    """``/skill-a do X`` with no extra stacked skills must call the single
    builder (not the stacked builder), passing session_id as task_id."""

    @contextmanager
    def _profile_scope(purpose):
        yield

    seen = {"type": None, "task_id": None}

    def _resolve(name):
        return f"/{name}"

    def _splitter(rest):
        return ([], rest)  # No extra keys

    def _stacked_builder(keys, instr="", task_id=None):
        seen["type"] = "stacked"
        return None

    def _single_builder(key, instr="", task_id=None, runtime_note=""):
        seen["type"] = "single"
        seen["task_id"] = task_id
        return "[IMPORTANT: The user has invoked the \"llm-wiki\" skill...]\nbody\nUser instruction: do X"

    _install_fake_skill_commands(
        monkeypatch,
        resolver=_resolve,
        builder=_single_builder,
        stacked_builder=_stacked_builder,
        splitter=_splitter,
    )
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/skill-a do X", session_id="test-session-789")

    assert result["name"] == "skill-a"
    assert result["source"] == "skill"
    assert seen["type"] == "single"
    assert seen["task_id"] == "test-session-789"


def test_resolve_stacked_skills_raises_on_failure(monkeypatch):
    """When build_stacked_skill_invocation_message returns None, the endpoint
    must raise RuntimeError."""

    @contextmanager
    def _profile_scope(purpose):
        yield

    def _resolve(name):
        return f"/{name}"

    def _splitter(rest):
        return (["/skill-b"], "do X")

    def _stacked_builder(keys, instr="", task_id=None):
        return None  # Simulate failure

    _install_fake_skill_commands(
        monkeypatch,
        resolver=_resolve,
        stacked_builder=_stacked_builder,
        splitter=_splitter,
    )
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    with pytest.raises(RuntimeError, match="Failed to load stacked skills"):
        commands.resolve_skill_command("/skill-a /skill-b do X")


def test_resolve_skill_command_static_asserts_splitter_imported():
    """The backend must import split_stacked_skill_commands and
    build_stacked_skill_invocation_message from agent.skill_commands."""
    assert "build_stacked_skill_invocation_message" in COMMANDS_PY
    assert "split_stacked_skill_commands" in COMMANDS_PY


def test_resolve_stacked_skills_route_unchanged():
    """No new route is needed for stacked skills — the existing
    /api/commands/skills/resolve endpoint handles both single and stacked
    invocations via the same resolve_skill_command() function."""
    assert "skills/resolve" in ROUTES_PY
    assert "resolve_skill_command" in ROUTES_PY


# ── Behavioral: RAW/agent-only split preserves the slash command ────────────


def test_chat_start_persists_raw_skill_command_and_expands_model_context():
    """Driving a /<skill> turn through _run_agent_streaming must persist the RAW
    `/skill …` command as the visible user row while the model sees (and the
    next-turn context carries) the expanded skill payload.

    Regression for the #5896 re-gate CORE: before the RAW/agent-only split the
    expanded skill body replaced the raw command in the persisted transcript
    (`raw_preserved=False` on settle/reload).
    """
    import api.streaming as streaming

    raw_command = "/llm-wiki list active pages"
    expansion = (
        '[IMPORTANT: The user has invoked the "llm-wiki" skill, indicating they '
        "want you to follow its instructions. The full skill content is loaded below.]\n"
        "# llm-wiki\n\nfull skill body... (+500 lines)\n"
        "The user has provided the following instruction alongside the skill "
        "invocation: list active pages"
    )

    class FakeSession:
        def __init__(self):
            self.session_id = "skills_resolve_raw_preserved"
            self.title = "Untitled"
            self.workspace = "/tmp"
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0
            self.cache_read_tokens = 0
            self.cache_write_tokens = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = ""
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            pass

        def compact(self):
            return {
                "session_id": self.session_id,
                "title": self.title,
                "workspace": self.workspace,
                "model": self.model,
                "model_provider": self.model_provider,
                "profile": self.profile,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost": self.estimated_cost,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "personality": self.personality,
            }

    captured = {}

    class EchoAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            session_id=None,
            session_db=None,
            **_kwargs,
        ):
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            captured.update(kwargs)
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "Here is the wiki summary."},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = "stream_skills_resolve_raw"
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()
    fake_runtime_module = ModuleType("hermes_cli.runtime_provider")
    runtime_payload = {
        "provider": "openai",
        "base_url": None,
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }
    runtime_payload["api_" + "key"] = "***"
    fake_runtime_module.__dict__["resolve_runtime_provider"] = mock.Mock(return_value=runtime_payload)
    fake_hermes_cli = ModuleType("hermes_cli")
    fake_hermes_cli.__dict__["runtime_provider"] = fake_runtime_module
    fake_hermes_state = ModuleType("hermes_state")
    fake_hermes_state.__dict__["SessionDB"] = mock.Mock(return_value=None)
    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    saved = {k: sys.modules.get(k, _MISSING) for k in injected}
    sys.modules.update(injected)
    try:
        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=EchoAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-test", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[fake_stream_id] = fake_queue
            try:
                streaming._run_agent_streaming(
                    session_id=fake_session.session_id,
                    msg_text=raw_command,
                    model="gpt-test",
                    workspace="/tmp",
                    stream_id=fake_stream_id,
                    attachments=None,
                    agent_message=expansion,
                )
            finally:
                streaming.STREAMS.pop(fake_stream_id, None)
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # The model saw the expansion (prefixed by the workspace tag), while the
    # persisted user row stays the RAW command.
    assert captured.get("persist_user_message") == raw_command, (
        "persist_user_message must be the RAW slash command, got "
        f"{captured.get('persist_user_message')!r}"
    )
    assert expansion in captured.get("user_message", ""), (
        "the model must see the expanded skill payload"
    )

    user_rows = [m for m in fake_session.messages if isinstance(m, dict) and m.get("role") == "user"]
    assert user_rows, "expected a persisted user row"
    assert user_rows[-1].get("content") == raw_command, (
        "persisted user message must be the RAW slash command, got "
        f"{str(user_rows[-1].get('content'))[:200]!r}"
    )
    assert all(expansion not in str(m.get("content") or "") for m in user_rows), (
        "expanded skill body must never leak into the visible transcript"
    )

    # The model context keeps the expansion so the skill content survives into
    # the next turn (gate-fail #5).
    context_user_rows = [
        m for m in fake_session.context_messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert context_user_rows, "expected a context user row"
    assert expansion in str(context_user_rows[-1].get("content") or ""), (
        "model context must carry the expanded skill payload for the next turn"
    )