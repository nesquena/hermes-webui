"""Tests for the /api/commands/skills/resolve endpoint and frontend wiring."""

import json
import queue
import shutil
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType
from unittest import mock

import sys
import pytest

import api.commands as commands

_MISSING = object()
NODE = shutil.which("node")

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


def _skill_resolver_source():
    """Source of the extracted `_resolveSkillAgentMessage()` helper.

    #5896 re-gate blocker 2 moved the skill resolution out of send()'s slash
    block so it also runs when files are staged. The resolution invariants live
    in the helper; the call sites only assign its return value.
    """
    idx = MESSAGES_JS.find("async function _resolveSkillAgentMessage(")
    assert idx != -1, "skill resolver helper not found in static/messages.js"
    end = MESSAGES_JS.find("\nasync function send(){", idx)
    assert end > idx, "send() sentinel not found after the skill resolver"
    return MESSAGES_JS[idx:end]


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    """Point session storage at tmp_path and restore the SESSIONS registry.

    #5896 re-gate: tests that call new_session() must not leak in-process
    sessions or on-disk sidecars into sibling tests (the same isolation class
    the maintainer required in the rounds 5/6 gates).
    """
    from api import models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "session_index.json")
    snapshot = dict(models.SESSIONS)
    try:
        yield session_dir
    finally:
        models.SESSIONS.clear()
        models.SESSIONS.update(snapshot)


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
    assert "_resolveSkillAgentMessage(text, _parsedCmd, _sendProjectPickerClaim, {ownerSid:_slashOwner.sid})" in body
    resolver = _skill_resolver_source()
    assert "loadSkillCommands()" in resolver
    assert "_skillCommandSlug(parsedCmd.name)" in resolver
    # RAW/agent-only split: the expansion is carried separately, never by
    # mutating the outbound `text` (the persisted message stays `/skill …`).
    assert "text = " not in resolver


def test_skill_dispatch_skips_when_bundle_or_agent_cmd_matched():
    """The skill check guards on !_bundleCmd and !_agentCmd so bundles and
    agent commands have priority."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "!_bundleCmd" in body
    assert "!_agentCmd" in body
    # The with-attachment path calls the resolver directly, so the resolver
    # itself must enforce bundle priority for a shared slug.
    resolver = _skill_resolver_source()
    assert "getBundleCommandMetadata(parsedCmd.name)" in resolver


def test_skill_dispatch_falls_through_silently_on_error():
    """If the server endpoint fails, the resolver must not show an error to the
    user — the raw text falls through to the agent."""
    resolver = _skill_resolver_source()
    assert "}catch(_e){" in resolver
    assert "return null" in resolver


def test_skill_dispatch_passes_session_id():
    """The skill resolver must pass the OWNED session id to
    resolveSkillCommand(): the one it created for a first-message send, else the
    one visible before the resolve. Reading S.session directly after the awaits
    would template ${HERMES_SESSION_ID} against a conversation the user opened."""
    resolver = _skill_resolver_source()
    assert "resolveSkillCommand(text, _resolutionSid)" in resolver
    assert (
        "const _resolutionSid = _createdSid || _ownerSid || (S.session && S.session.session_id) || null;"
        in resolver
    )


def test_skill_dispatch_normalizes_underscores_to_slug():
    """`/my_skill` must match the cached `my-skill` entry, mirroring the Agent
    contract's `_` → `-` normalization (gate-fail #4)."""
    resolver = _skill_resolver_source()
    assert "_skillCommandSlug(parsedCmd.name)" in resolver
    assert "s.name === slug" in resolver


def test_skill_dispatch_creates_session_before_resolve():
    """First-message skill sends must create a session before resolving so
    ${HERMES_SESSION_ID} templates correctly (gate-fail #3), and must forward
    the captured picker generation so a delayed newSession() cannot claim and
    close a project picker the user opened while metadata was loading
    (#5896 re-gate blocker 3)."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "_resolveSkillAgentMessage(text, _parsedCmd, _sendProjectPickerClaim, {ownerSid:_slashOwner.sid})" in body
    resolver = _skill_resolver_source()
    assert "if(!S.session){" in resolver
    assert "await newSession(false,{pickerClaimEpoch:pickerClaim});" in resolver
    assert "_skillCreatedSessionSid = _createdSid;" in resolver


def test_skill_dispatch_keeps_raw_message_out_of_model_mutation():
    """The skill intercept must never replace the outbound `text` with the
    expansion — the RAW `/skill …` command is the persisted/displayed message
    and the expansion travels separately as `agent_message`."""
    idx = MESSAGES_JS.find("// ── Skill commands:")
    body = MESSAGES_JS[idx:]
    assert "text = " not in _skill_resolver_source()
    assert "_skillAgentMessage = await _resolveSkillAgentMessage(" in body


def test_chat_start_post_carries_agent_message_separately():
    """The /api/chat/start POST must keep `message` as the raw display turn and
    carry the model-facing text (skill expansion or forced `/use` prefix) in a
    separate agent-only `agent_message` field."""
    post_idx = MESSAGES_JS.find("const startData=await api('/api/chat/start'")
    assert post_idx != -1, "chat/start POST body not found"
    body = MESSAGES_JS[post_idx:]
    assert "message:_persistedMessage" in body
    assert "agent_message:_agentMessage||undefined" in body


def test_skill_agent_message_carries_uploaded_attachment_paths():
    """#5896 re-gate blocker 2: a skill send with a non-image upload must carry
    the uploaded paths into the model-facing expansion (`agent_message`), not
    only into `msgText`. The streaming worker prefers `agent_message` over
    `msgText`, so paths appended only to msgText never reach the model. The
    enrichment must extend the expansion, never the visible/title row."""
    assert "if(_skillAgentMessage&&uploaded.length&&text){" in MESSAGES_JS, (
        "the expansion must be enriched with uploaded attachment paths"
    )
    assert "_skillAgentMessage=`${_skillAgentMessage}" in MESSAGES_JS, (
        "the uploaded paths must be appended to the skill expansion itself"
    )
    # The visible/title row stays the RAW command: no attachment suffix may be
    # folded into displayText.
    display_idx = MESSAGES_JS.find("const displayText=")
    assert display_idx != -1
    assert "uploadedPaths" not in MESSAGES_JS[display_idx:display_idx + 300]


def _js_function_body(source, marker, end_marker="\n}"):
    start = source.index(marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_profile_switch_invalidates_slash_command_caches():
    """Profile A's skill/agent/bundle command lists must never remain
    authoritative in profile B (gate-fail #7): switchToProfile must drop the
    cached lists and invalidateSlashSkillCaches must reset all three."""
    panels_src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    switch_body = _js_function_body(panels_src, "async function switchToProfile(")
    assert "window.invalidateSlashSkillCaches" in switch_body
    # Assert the resets INSIDE invalidateSlashSkillCaches: the bare strings also
    # occur in the top-level `let` declarations, so a whole-file search would
    # pass even if the reset block were deleted (#5896 re-gate).
    invalidate_body = _js_function_body(COMMANDS_JS, "function invalidateSlashSkillCaches(")
    for reset in (
        "_bundleCommandCache=[];",
        "_bundleCommandLoadPromise=null;",
        "_bundleCommandCacheReady=false;",
        "_agentCommandCache=null;",
        "_agentCommandCachePromise=null;",
        "_agentCommandCacheReady=false;",
        "_agentCommandCacheAvailable=false;",
        "_slashPersonalityCache=null;",
        "_slashPersonalityCachePromise=null;",
    ):
        assert reset in invalidate_body, f"invalidateSlashSkillCaches missing {reset}"


def test_skill_cache_failure_remains_retryable():
    """Failed /api/skills loads must not mark the cache ready, so the next
    send-path lookup retries instead of serving a stale empty list
    (gate-fail #6)."""
    # master publishes the ready flag from a success-only `_committed` marker in
    # the finally block, so a failed load leaves the cache not-ready and the
    # next lookup retries (instead of serving a stale empty list).
    assert "_skillCommandCacheReady=_committed" in COMMANDS_JS
    cache_body = COMMANDS_JS[COMMANDS_JS.find("async function loadSkillCommands"):]
    cache_body = cache_body[:cache_body.find("\nasync function ")]
    assert "catch(_){" in cache_body
    commit_idx = cache_body.find("_committed=true;")
    catch_idx = cache_body.find("catch(_){")
    ready_idx = cache_body.find("_skillCommandCacheReady=_committed")
    assert commit_idx != -1 and catch_idx != -1 and ready_idx != -1
    assert commit_idx < catch_idx, "success marker must be set inside try"
    assert catch_idx < ready_idx, "ready flag must be published in finally, not catch"


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
        "loaded_skills": ["llm-wiki"],
        "missing_skills": [],
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
        return (
            f"stacked body: loaded! (task_id={task_id})",
            ["skill-a", "skill-b"],
            ["skill-missing"],
        )

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
    assert result["loaded_skills"] == ["skill-a", "skill-b"]
    assert result["missing_skills"] == ["skill-missing"]
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


def test_sync_chat_rewrites_model_context_with_resolved_skill(isolated_sessions):
    """RAW/agent-only split on the synchronous /api/chat path: the model sees
    the resolved skill payload and the next-turn context keeps it, while the
    persisted display row stays the raw command (round-3 re-gate)."""
    import io

    import api.routes as routes

    raw_command = "/llm-wiki list active pages"
    expansion = (
        '[IMPORTANT: The user has invoked the "llm-wiki" skill, indicating they '
        "want you to follow its instructions. The full skill content is loaded below.]\n"
        "# llm-wiki\n\nfull skill body... (+500 lines)\n"
        "The user has provided the following instruction alongside the skill "
        "invocation: list active pages"
    )

    captured = {}

    class EchoAgent:
        def __init__(self, *args, **kwargs):
            pass

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

    from api.models import new_session

    s = new_session(workspace="/tmp", model="gpt-test")
    s.save()
    handler = mock.MagicMock()
    handler.wfile = io.BytesIO()

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
        with mock.patch.object(routes, "_agent_runtime_barrier_response", return_value=None), \
             mock.patch.object(routes, "_session_is_subagent_view_only", return_value=False), \
             mock.patch.object(routes, "require_ai_agent_class", return_value=EchoAgent), \
             mock.patch("api.config.resolve_model_provider", return_value=("gpt-test", "openai", None)), \
             mock.patch.object(routes, "_read_profile_model_config", return_value=(None, None, None)), \
             mock.patch.object(routes, "_resolve_compatible_session_model_state", return_value=("gpt-test", "openai", False)), \
             mock.patch.object(routes, "resolve_trusted_workspace", return_value=Path("/tmp")), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            body = {
                "session_id": s.session_id,
                "message": raw_command,
                "agent_message": expansion,
                "workspace": "/tmp",
            }
            routes._handle_chat_sync(handler, body)
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # The model saw the expansion; the persisted user row stayed raw.
    assert captured.get("persist_user_message") == raw_command
    assert expansion in captured.get("user_message", "")

    from api.models import get_session as _get_session

    # Force a COLD reload: get_session() returns the cached object otherwise,
    # so the test would not prove the sidecar carried the raw display rows.
    from api import models as _models
    _models.SESSIONS.pop(s.session_id, None)
    reloaded = _get_session(s.session_id)
    user_rows = [m for m in reloaded.messages if isinstance(m, dict) and m.get("role") == "user"]
    assert user_rows, "expected a persisted user row"
    assert user_rows[-1].get("content") == raw_command, (
        "sync path must persist the RAW slash command"
    )
    context_user_rows = [
        m for m in reloaded.context_messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert context_user_rows, "expected a context user row"
    assert expansion in str(context_user_rows[-1].get("content") or ""), (
        "sync path must keep the expanded skill payload for the next turn"
    )


def test_sync_chat_agent_model_row_does_not_duplicate_the_transcript(isolated_sessions):
    """#5896 re-gate: an Agent that returns its own model-facing user row
    (instead of ``persist_user_message``) must still settle as ONE raw display
    turn. The merge used to append the workspace-prefixed expansion beside the
    raw prompt, and the context rewrite then kept both representations."""
    raw_command = "/llm-wiki list active pages"
    expansion = "EXPANSION-BODY"

    class ModelRowAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["user_message"]},
                    {"role": "assistant", "content": "answer"},
                ]
            }

        def interrupt(self, _message):
            pass

    from api.models import get_session as _get_session, new_session

    s = new_session(workspace="/tmp", model="gpt-test")
    s.save()
    _sync_chat_turn(s.session_id, raw_command, expansion, ModelRowAgent)

    from api import models as _models
    _models.SESSIONS.pop(s.session_id, None)
    reloaded = _get_session(s.session_id)
    display_user_rows = [
        m for m in reloaded.messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert [m.get("content") for m in display_user_rows] == [raw_command], (
        f"the model-facing row leaked into the display transcript: {display_user_rows!r}"
    )
    context_user_rows = [
        m for m in reloaded.context_messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert len(context_user_rows) == 1, (
        f"the current turn must have exactly one model-context row: {context_user_rows!r}"
    )
    assert context_user_rows[0].get("content") == expansion
    assert context_user_rows[0].get("_skill_raw_user_text") == raw_command


def test_settle_normalizes_agent_model_row_to_one_raw_turn(isolated_sessions):
    """#5896 re-gate: the settle owns one representation per turn.

    A result whose current user row is the model-facing (workspace-prefixed)
    expansion must not be materialized beside a second raw checkpoint. That
    duplicate is then rewritten into a second expansion row, which is what the
    previous head persisted into the next turn's model context.
    """
    from api.models import Session
    from api.process_event_utils import build_active_turn_token
    from api.streaming import _rewrite_context_user_turn, _settle_result_messages

    raw_command = "/llm-wiki list active pages"
    expansion = "EXPANSION-BODY"
    session = Session(session_id="settle-model-row", workspace="/tmp")
    session.messages = []
    session.context_messages = []
    identity = {
        "token": build_active_turn_token("stream-model-row", 1.0),
        "text": raw_command,
        "timestamp": 1.0,
    }
    result = [
        {"role": "user", "content": f"[Workspace::v1: /tmp]\n{expansion}", "timestamp": 1.0},
        {"role": "assistant", "content": "answer", "timestamp": 2.0},
    ]
    _settle_result_messages(
        session,
        [],
        [],
        result,
        raw_command,
        "webui",
        identity,
        None,
        agent_message=expansion,
    )
    _rewrite_context_user_turn(session, expansion, raw_command)

    display_user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert [m.get("content") for m in display_user_rows] == [raw_command]
    context_user_rows = [m for m in session.context_messages if m.get("role") == "user"]
    assert len(context_user_rows) == 1, context_user_rows
    assert context_user_rows[0].get("content") == expansion
    assert context_user_rows[0].get("_skill_raw_user_text") == raw_command


def _sync_chat_turn(session_id, message, agent_message, agent_cls):
    """Drive one synchronous /api/chat turn with a capture-only Agent seam."""
    import io

    import api.routes as routes

    handler = mock.MagicMock()
    handler.wfile = io.BytesIO()
    body = {"session_id": session_id, "message": message, "workspace": "/tmp"}
    if agent_message is not None:
        body["agent_message"] = agent_message

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
        with mock.patch.object(routes, "_agent_runtime_barrier_response", return_value=None), \
             mock.patch.object(routes, "_session_is_subagent_view_only", return_value=False), \
             mock.patch.object(routes, "require_ai_agent_class", return_value=agent_cls), \
             mock.patch("api.config.resolve_model_provider", return_value=("gpt-test", "openai", None)), \
             mock.patch.object(routes, "_read_profile_model_config", return_value=(None, None, None)), \
             mock.patch.object(routes, "_resolve_compatible_session_model_state", return_value=("gpt-test", "openai", False)), \
             mock.patch.object(routes, "resolve_trusted_workspace", return_value=Path("/tmp")), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            routes._handle_chat_sync(handler, body)
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# Shape contract: the two-turn regression uses the Agent shape the WebUI settle
# relies on — `conversation_history` (the sanitized model projection) is echoed
# back, and the current turn returns `persist_user_message` (RAW). The
# WebUI-only `_skill_raw_user_text` marker never reaches the Agent (the
# sanitizer strips it), so a variant that rewrites the historical row to RAW
# models a different shape and is not a valid regression here.
def test_two_turn_skill_expansion_stays_out_of_display(isolated_sessions):
    """#5896 re-gate blocker 1: after ONE skill turn followed by ONE ordinary
    turn, the expansion must remain a model-context representation only. The
    display merge must not treat it as a context-only turn and backfill it into
    ``session.messages`` (which cold reload and the older-message GET read)."""
    from api.models import get_session as _get_session, new_session

    raw_command = "/llm-wiki list active pages"
    expansion = (
        '[IMPORTANT: The user has invoked the "llm-wiki" skill, indicating they '
        "want you to follow its instructions. The full skill content is loaded below.]\n"
        "# llm-wiki\n\nfull skill body... (+500 lines)\n"
        "The user has provided the following instruction alongside the skill "
        "invocation: list active pages"
    )

    class EchoAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": f"reply after {len(history)}"},
                ]
            }

        def interrupt(self, _message):
            pass

    s = new_session(workspace="/tmp", model="gpt-test")
    s.save()

    _sync_chat_turn(s.session_id, raw_command, expansion, EchoAgent)
    _sync_chat_turn(s.session_id, "continue", None, EchoAgent)

    # Force a COLD reload: get_session() returns the cached object otherwise,
    # so the test would not prove the sidecar carried the raw display rows.
    from api import models as _models
    _models.SESSIONS.pop(s.session_id, None)
    reloaded = _get_session(s.session_id)
    user_texts = [
        m.get("content")
        for m in reloaded.messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert raw_command in user_texts, "the raw command must stay in the transcript"
    assert "continue" in user_texts, "the ordinary turn must stay in the transcript"
    assert all(expansion not in str(t or "") for t in user_texts), (
        "the expanded skill body must not be backfilled into the visible "
        f"transcript; user rows were {user_texts!r}"
    )

    # Continuation contract is preserved: the model context keeps the expansion.
    context_user_rows = [
        m for m in reloaded.context_messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert any(expansion in str(m.get("content") or "") for m in context_user_rows), (
        "model context must keep the expansion for continuation"
    )


def test_streaming_two_turn_skill_expansion_stays_out_of_display():
    """#5896 re-gate blocker 1 (default streaming worker): same two-turn
    contract as the synchronous path — the expansion stays model-context only
    and is never backfilled into session.messages."""
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
            self.session_id = "skills_two_turn_streaming"
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

    class EchoAgent:
        def __init__(self, *args, **kwargs):
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
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": f"reply after {len(history)}"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
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
            for turn, (msg_text, agent_message) in enumerate([
                (raw_command, expansion),
                ("continue", None),
            ]):
                stream_id = f"stream_skills_two_turn_{turn}"
                fake_session.active_stream_id = stream_id
                streaming.STREAMS[stream_id] = queue.Queue()
                try:
                    streaming._run_agent_streaming(
                        session_id=fake_session.session_id,
                        msg_text=msg_text,
                        model="gpt-test",
                        workspace="/tmp",
                        stream_id=stream_id,
                        attachments=None,
                        agent_message=agent_message,
                    )
                finally:
                    streaming.STREAMS.pop(stream_id, None)
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    user_texts = [
        m.get("content")
        for m in fake_session.messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert raw_command in user_texts, "the raw command must stay in the transcript"
    assert "continue" in user_texts, "the ordinary turn must stay in the transcript"
    assert all(expansion not in str(t or "") for t in user_texts), (
        "streaming worker must not backfill the expansion into the display: "
        f"{user_texts!r}"
    )
    context_user_rows = [
        m for m in fake_session.context_messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert any(expansion in str(m.get("content") or "") for m in context_user_rows), (
        "model context must keep the expansion for continuation"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_skill_send_with_staged_file_carries_expansion_and_paths():
    """#5896 re-gate blocker 2 (behavioral): a `/skill` send with a staged file
    must still resolve the skill and send BOTH the expansion and the uploaded
    path in `agent_message`. send()'s slash block is gated on
    `!S.pendingFiles.length`, so this exercises the pending-files resolution
    path, not the in-block one. Fails if the outside-gate call is removed."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending=null;
const msg={value:'/my-skill do x'};
const S={session:{session_id:'sess-1',workspace:'/tmp',title:'Untitled'},pendingFiles:[{name:'f.txt'}],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(){return {message:'EXPANSION'};}
async function newSession(){S.session={session_id:'sess-1'};}
function renderSessionList(){}
async function uploadPendingFiles(){return [{name:'f.txt',path:'/tmp/f.txt'}];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);return {stream_id:'s1'};}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  await send();
  console.log('RESULT ' + JSON.stringify({body:captured.body||null,messages:S.messages}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(line[len(marker):])
    body = payload["body"]
    assert body is not None, f"chat/start POST never fired; stderr={result.stderr!r}"
    assert str(body.get("message") or "").startswith("/my-skill do x"), body.get("message")
    assert "/tmp/f.txt" in str(body.get("message") or "")
    agent_message = str(body.get("agent_message") or "")
    assert "EXPANSION" in agent_message, agent_message
    assert "/tmp/f.txt" in agent_message, agent_message
    user_rows = [m for m in payload["messages"] if isinstance(m, dict) and m.get("role") == "user"]
    assert user_rows, "expected an optimistic user row"
    assert user_rows[-1].get("content") == "/my-skill do x", user_rows[-1]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_skill_first_send_forwards_originating_picker_claim():
    """#5896 re-gate blocker 3 (behavioral): the first-message skill branch must
    pass the send's captured picker generation to newSession(), so a picker the
    user opened while the resolver was pending is not claimed and closed.
    Fails when the branch calls newSession() without the epoch."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending=null;
const msg={value:'/my-skill do x'};
const S={session:null,pendingFiles:[],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const claims=[];
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
let releaseResolver;
let resolverStartedResolve;
const resolverStarted=new Promise(resolve=>{resolverStartedResolve=resolve;});
const resolverGate=new Promise(resolve=>{releaseResolver=resolve;});
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(){resolverStartedResolve();await resolverGate;return {message:'EXPANSION'};}
async function newSession(_flash,options){claims.push(options&&options.pickerClaimEpoch);S.session={session_id:'sess-1'};}
function renderSessionList(){}
async function uploadPendingFiles(){return [];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);return {stream_id:'s1'};}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  const pending=send();
  await resolverStarted;
  _projectPickerOpenEpoch=8;
  releaseResolver();
  await new Promise(r=>setTimeout(r,30));
  console.log('RESULT ' + JSON.stringify({claims,currentEpoch:_projectPickerOpenEpoch,body:captured.body||null}));
  process.exit(0);
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(line[len(marker):])
    assert payload["claims"] == [7], payload
    assert payload["currentEpoch"] == 8
    body = payload["body"]
    assert body is not None, (
        "a first-message /skill send must reach /api/chat/start; dropping the "
        "_skillCreatedSessionSid arm silently abandons it"
    )
    assert body.get("session_id") == "sess-1", body
    assert body.get("agent_message") == "EXPANSION", body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_skill_send_abandons_when_conversation_changes_during_resolve():
    """#5896 re-gate P1 (Greptile): if the user switches conversations while
    resolveSkillCommand() is awaited, the send must be abandoned. Otherwise the
    raw command and the A-scoped ${HERMES_SESSION_ID} expansion fire into the
    newly opened session B."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending=null;
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
const msg={value:'/my-skill do x'};
const S={session:{session_id:'A',workspace:'/tmp',title:'Untitled'},pendingFiles:[],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
let releaseResolver;
let resolverStartedResolve;
const resolverStarted=new Promise(resolve=>{resolverStartedResolve=resolve;});
const resolverGate=new Promise(resolve=>{releaseResolver=resolve;});
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(){resolverStartedResolve();await resolverGate;return {message:'EXPANSION-A'};}
async function newSession(){S.session={session_id:'A'};}
function renderSessionList(){}
async function uploadPendingFiles(){return [];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  const pending=send();
  await resolverStarted;
  S.session={session_id:'B'};
  releaseResolver();
  await pending;
  console.log('RESULT ' + JSON.stringify({body:captured.body||null}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(line[len(marker):])
    assert payload["body"] is None, (
        "a send whose conversation changed during resolve must not reach "
        f"/api/chat/start; got {payload['body']!r}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_skill_send_abandons_when_conversation_opens_during_cache_load():
    """#5896 re-gate P1 (Greptile), null-start variant: a new chat /skill send
    whose cache load is interrupted by the user opening conversation B must
    abandon. The helper must not resolve against B (it did not create it)."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending=null;
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
const msg={value:'/my-skill do x'};
const S={session:null,pendingFiles:[],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
let releaseResolver;
let resolverStartedResolve;
const resolverStarted=new Promise(resolve=>{resolverStartedResolve=resolve;});
const resolverGate=new Promise(resolve=>{releaseResolver=resolve;});
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
async function loadSkillCommands(){resolverStartedResolve();await resolverGate;return [{name:'my-skill'}];}
async function resolveSkillCommand(){S.session={session_id:'RESOLVED-B'};return {message:'EXPANSION-B'};}
async function newSession(){S.session={session_id:'CREATED'};}
function renderSessionList(){}
async function uploadPendingFiles(){return [];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  const pending=send();
  await resolverStarted;
  S.session={session_id:'B'};
  releaseResolver();
  await pending;
  console.log('RESULT ' + JSON.stringify({body:captured.body||null,session:S.session&&S.session.session_id}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(line[len(marker):])
    assert payload["body"] is None, (
        "a null-start send whose cache load was interrupted by conversation B "
        f"must not reach /api/chat/start; got {payload['body']!r}"
    )
    assert payload["session"] == "B"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_skill_send_abandons_when_conversation_opens_after_session_creation():
    """#5896 re-gate P1 (Greptile), post-creation variant: the resolver creates
    the first-message session A, then the user opens B while the session list
    renders. The send must abandon instead of firing A's expansion into B, and
    the resolver must template ${HERMES_SESSION_ID} against A, never B."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending=null;
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
const msg={value:'/my-skill do x'};
const S={session:null,pendingFiles:[],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
let releaseRender;
let renderStartedResolve;
const renderStarted=new Promise(resolve=>{renderStartedResolve=resolve;});
const renderGate=new Promise(resolve=>{releaseRender=resolve;});
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(text,sid){captured.resolvedSid=sid;return {message:'EXPANSION-A'};}
async function newSession(){S.session={session_id:'A'};}
async function renderSessionList(){renderStartedResolve();await renderGate;}
async function uploadPendingFiles(){return [];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  const pending=send();
  await renderStarted;
  S.session={session_id:'B'};
  releaseRender();
  await pending;
  console.log('RESULT ' + JSON.stringify({body:captured.body||null,session:S.session&&S.session.session_id,resolvedSid:captured.resolvedSid===undefined?null:captured.resolvedSid}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(line[len(marker):])
    assert payload["body"] is None, (
        "a first-message send whose created session was superseded by "
        f"conversation B must not reach /api/chat/start; got {payload['body']!r}"
    )
    assert payload["session"] == "B"
    assert payload["resolvedSid"] == "A", (
        "the resolver must template ${HERMES_SESSION_ID} against the session it "
        f"created (A), never the conversation the user opened (B); got {payload['resolvedSid']!r}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_agent_command_send_survives_its_own_session_creation():
    """/moa and the other continuing slash branches may create the first
    session themselves before the send reaches the navigation guard. The guard
    must only fire when a skill resolution actually ran; a first /moa send must
    still reach /api/chat/start (#5896 re-gate)."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending=null;
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
const msg={value:'/moa do x'};
const S={session:null,pendingFiles:[],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/moa')?{name:'moa',args:'do x'}:null;}
function _isWebuiDispatchableAgentCommand(){return true;}
async function getAgentCommandMetadata(){return {command:{name:'moa'}};}
async function loadSkillCommands(){return [];}
async function resolveSkillCommand(){return {message:'EXPANSION'};}
async function newSession(){S.session={session_id:'M',workspace:'/tmp'};}
function renderSessionList(){}
async function uploadPendingFiles(){return [];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);return {stream_id:'s1'};}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  await send();
  console.log('RESULT ' + JSON.stringify({body:captured.body||null,session:S.session&&S.session.session_id}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(line[len(marker):])
    assert payload["body"] is not None, (
        "a first /moa send creates its own session and must still reach "
        "/api/chat/start; the navigation guard only applies to skill sends"
    )
    assert payload["body"].get("message") == "do x", payload["body"]
    assert payload["session"] == "M"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_forced_skill_directive_reaches_the_model_facing_text():
    """#5896 re-gate: a pending `/use skill-a` override is prepended to `msgText`,
    but the worker prefers `agent_message`. The override must be carried there
    too, or the model never receives it."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending={sessionId:'A',promise:Promise.resolve({directive:'FORCED DIRECTIVE',name:'skill-a',content:'BODY'})};
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
const msg={value:'/my-skill do x'};
const S={session:{session_id:'A',workspace:'/tmp'},pendingFiles:[{name:'f.txt'}],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
function _isWebuiDispatchableAgentCommand(){return true;}
async function getAgentCommandMetadata(){return null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(){return {message:'EXPANSION'};}
async function newSession(){S.session={session_id:'A',workspace:'/tmp'};}
function renderSessionList(){}
async function uploadPendingFiles(){return [{name:'f.txt',path:'/tmp/f.txt'}];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);return {stream_id:'s1'};}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  await send();
  console.log('RESULT ' + JSON.stringify({body:captured.body||null}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    body = json.loads(line[len(marker):])["body"]
    assert body is not None, "the send never reached /api/chat/start"
    agent_message = body.get("agent_message") or ""
    assert "FORCED DIRECTIVE" in agent_message, agent_message
    assert "[FORCED SKILL CONTEXT: skill-a]" in agent_message, agent_message
    assert "EXPANSION" in agent_message, agent_message
    # The forced prefix is model-facing only: the persisted `message` must stay
    # the turn text (raw command + attachment paths), never the display-only
    # `displayText` that omits them.
    message = body.get("message") or ""
    assert message.startswith("/my-skill do x"), message
    assert "/tmp/f.txt" in message, message


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_skill_resolver_failure_falls_back_to_the_raw_command():
    """A transient /api/commands/skills/resolve failure must not abort the send:
    the raw command is sent and no agent_message is attached (fail open)."""
    send_start = MESSAGES_JS.index("async function send(){")
    send_end = MESSAGES_JS.index("\nfunction ", send_start + 1)
    send_body = MESSAGES_JS[send_start:send_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _sendInProgress=false;
let _sendInProgressSid=null;
let _projectPickerOpenEpoch=7;
let _pendingSelections=[];
let _forcedSkillDirectivePending=null;
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
const msg={value:'/my-skill do x'};
const S={session:{session_id:'A',workspace:'/tmp'},pendingFiles:[],busy:false,messages:[],activeProfile:'default',toolCalls:[]};
const document={querySelector(){return null;}};
const COMMANDS=[];
const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
const INFLIGHT={};
const captured={};
const window={_defaultModel:'gpt-test',_activeProvider:'openai'};
function $(id){return id==='msg'?msg:{value:'',focus(){}};}
function _composerTextWithPendingSelections(){return msg.value;}
function _flushSelectionBlocksToComposer(){}
function _clearStaleBusyStateBeforeSend(){}
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
function _isWebuiDispatchableAgentCommand(){return true;}
async function getAgentCommandMetadata(){return null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(){throw new Error('boom');}
async function newSession(){S.session={session_id:'A',workspace:'/tmp'};}
function renderSessionList(){}
async function uploadPendingFiles(){return [];}
function setComposerStatus(){}
function clearLiveToolCards(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function _runOptionalPreStartUiStep(_n,fn){try{fn();}catch(_e){}}
function _runOptionalPostStartUiStep(_n,fn){try{fn();}catch(_e){}}
function upsertActiveSessionForLocalTurn(){}
function saveInflightState(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function updateSendBtn(){}
function _chatPayloadModelState(){return {model:'gpt-test',model_provider:'openai'};}
function _readPendingSessionModel(){return null;}
function renderMessages(){}
function autoResize(){}
function hideCmdDropdown(){}
function renderTray(){}
function showToast(){}
function _normalizeGoalContinuationId(id){return String(id||'').trim();}
function _takeRestoredDraftGoalContinuationId(){return '';}
function attachLiveStream(){}
function markInflight(){}
function showLiveRunStatus(){}
function removeThinking(){}
function clearOptimisticSessionStreaming(){}
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);return {stream_id:'s1'};}
  return {};
}
""" + resolver_body + send_body + r"""
(async()=>{
  await send();
  console.log('RESULT ' + JSON.stringify({body:captured.body||null}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    body = json.loads(line[len(marker):])["body"]
    assert body is not None, "a resolver failure must not abort the send (fail open)"
    assert body.get("message") == "/my-skill do x", body
    assert body.get("agent_message") is None, body


def test_context_marker_identity_matches_the_raw_display_row():
    """#5896 re-gate: every sidecar/state.db identity layer must key a
    `_skill_raw_user_text` context row on its raw display turn. Otherwise the
    expansion row and the raw state.db row are two different turns to
    reconciliation, which re-appends the state row and backfills the skill body."""
    from api import models

    marked = {
        "role": "user",
        "content": "EXPANSION-BODY",
        "_skill_raw_user_text": "/llm-wiki list pages",
        "timestamp": 1.0,
    }
    raw = {"role": "user", "content": "/llm-wiki list pages", "timestamp": 1.0}
    assert models._session_message_merge_key(marked) == models._session_message_merge_key(raw)
    assert models._session_message_dedup_key(marked) == models._session_message_dedup_key(raw)
    assert models._session_message_content_key(marked) == models._session_message_content_key(raw)
    assert models._session_message_visible_key(marked) == models._session_message_visible_key(raw)
    from api.streaming import _message_replay_key

    assert _message_replay_key(marked) == _message_replay_key(raw)


def test_settle_preserves_multimodal_parts_when_normalizing_model_row(isolated_sessions):
    """A skill turn with an image upload must keep its native parts while the
    text part moves from the model expansion to the raw display command (#5896)."""
    from api.models import Session
    from api.process_event_utils import build_active_turn_token
    from api.streaming import _rewrite_context_user_turn, _settle_result_messages

    raw_command = "/llm-wiki describe this screenshot"
    expansion = "EXPANSION-BODY"
    image_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    session = Session(session_id="settle-multimodal", workspace="/tmp")
    session.messages = []
    session.context_messages = []
    identity = {
        "token": build_active_turn_token("stream-multimodal", 1.0),
        "text": raw_command,
        "timestamp": 1.0,
    }
    result = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"[Workspace::v1: /tmp]\n{expansion}"},
                image_part,
            ],
            "timestamp": 1.0,
        },
        {"role": "assistant", "content": "answer", "timestamp": 2.0},
    ]
    _settle_result_messages(
        session, [], [], result, raw_command, "webui", identity, None,
        agent_message=expansion,
    )
    _rewrite_context_user_turn(session, expansion, raw_command)

    display_user = next(m for m in session.messages if m.get("role") == "user")
    assert display_user["content"] == raw_command
    context_user = next(m for m in session.context_messages if m.get("role") == "user")
    assert context_user["content"][0]["text"] == expansion
    assert context_user["content"][1] == image_part
    assert context_user.get("_skill_raw_user_text") == raw_command


def test_regeneration_replays_stored_model_text_for_forced_directive():
    """#5896 re-gate: regeneration of a non-`/skill` turn must replay the
    model-facing text recorded on the retained context row (the forced `/use`
    envelope), not the raw display prompt. A client-side `startsWith('/')`
    resolver can never reconstruct it."""
    from api.routes import _stored_model_text_for_regeneration

    raw = "please explain the plan"
    stored = (
        "[USER OVERRIDE] You MUST follow the skill 'writer' content provided below "
        "before responding to the next message.\n\n"
        "[FORCED SKILL CONTEXT: writer]\nfollow the guide\n[/FORCED SKILL CONTEXT]\n\n"
        "please explain the plan"
    )
    row = {"role": "user", "content": stored, "_skill_raw_user_text": raw}
    assert _stored_model_text_for_regeneration(row, raw) == stored
    # A raw-only row has nothing to replay.
    assert _stored_model_text_for_regeneration(
        {"role": "user", "content": raw, "_skill_raw_user_text": raw}, raw
    ) is None
    # Without the link the row is not authoritative for this turn.
    assert _stored_model_text_for_regeneration({"role": "user", "content": stored}, raw) is None
    # A different raw turn must not borrow another turn's model text.
    assert _stored_model_text_for_regeneration(row, "another prompt") is None


def test_regeneration_worker_receives_the_stored_model_text(monkeypatch, isolated_sessions):
    """#5896 re-gate: the locked regeneration starter must pass the stored
    model-facing text to its worker, overriding the client's optional value."""
    import queue
    import threading
    from types import SimpleNamespace

    from api import routes, session_ops
    from api.models import Session

    raw = "please explain the plan"
    stored = (
        "[USER OVERRIDE] You MUST follow the skill 'writer' content provided below "
        "before responding to the next message.\n\n"
        "[FORCED SKILL CONTEXT: writer]\nfollow the guide\n[/FORCED SKILL CONTEXT]\n\n"
        "please explain the plan"
    )
    session = Session(session_id="regen-stored-model-text", workspace="/tmp")
    session.messages = [
        {"role": "user", "content": raw, "timestamp": 1.0},
        {"role": "assistant", "content": "old answer", "timestamp": 2.0},
    ]
    session.context_messages = [
        {
            "role": "user",
            "content": stored,
            "timestamp": 1.0,
            "_skill_raw_user_text": raw,
        },
        {"role": "assistant", "content": "old answer", "timestamp": 2.0},
    ]
    turn = SimpleNamespace(
        revision="rev", message_text=raw, attachments=[], source="webui"
    )
    captured = {}
    done = threading.Event()

    monkeypatch.setattr(
        session_ops, "plan_regeneration", lambda *_a, **_k: SimpleNamespace(turn=turn)
    )
    monkeypatch.setattr(session_ops, "snapshot_regeneration_state", lambda _s: {})
    monkeypatch.setattr(
        session_ops,
        "apply_regeneration_plan",
        lambda s, _plan, return_context_user=False: (True, s.context_messages[0]),
    )
    monkeypatch.setattr(
        routes, "_prepare_chat_start_session_for_stream", lambda *_a, **_k: None
    )
    monkeypatch.setattr(routes, "create_stream_channel", lambda: queue.Queue())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _s: False)
    monkeypatch.setattr(Session, "save", lambda *_a, **_k: None)
    monkeypatch.setattr("api.turn_journal.append_turn_journal_event", lambda *_a, **_k: {})

    def fake_worker(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        done.set()

    monkeypatch.setattr(routes, "_run_agent_streaming", fake_worker)

    result = {}
    try:
        result = routes._start_regeneration_stream_locked(
            session,
            turn=turn,
            workspace="/tmp",
            model="gpt-test",
            model_provider="openai",
            normalized_model=False,
            diag=None,
            goal_related=False,
            source="webui",
            moa_config=None,
            backend_is_gateway=False,
            agent_message="client-resolved-value",
        )

        assert result.get("stream_id"), result
        assert done.wait(3), "the regeneration worker never ran"
        assert captured["kwargs"].get("agent_message") == stored, captured
    finally:
        import api.config as config_api

        stream_id = result.get("stream_id")
        with config_api.STREAMS_LOCK:
            config_api.STREAMS.pop(stream_id, None)
            config_api.STREAM_GOAL_RELATED.pop(stream_id, None)
            config_api.PRE_ADMISSION_CLAIMS.pop(stream_id, None)
            config_api.CANCEL_FLAGS.pop(stream_id, None)


def test_chat_start_forwards_agent_message_to_start_run(isolated_sessions):
    """#5896 re-gate route coverage: /api/chat/start must parse `agent_message`
    from the body and forward it to `_start_run` on the default local path, so
    a mutation of either line cannot silently drop the expansion."""
    import io

    from api import routes
    from api.models import new_session

    s = new_session(workspace="/tmp", model="gpt-test")
    s.save()
    handler = mock.MagicMock()
    handler.wfile = io.BytesIO()
    captured = {}

    def fake_start_run(session, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "s1", "session_id": session.session_id}

    with mock.patch.object(routes, "_agent_runtime_barrier_response", return_value=None), \
         mock.patch.object(routes, "_get_or_materialize_session", return_value=s), \
         mock.patch.object(routes, "_resolve_chat_workspace_with_recovery", return_value="/tmp"), \
         mock.patch.object(routes, "_read_profile_model_config", return_value=(None, "gpt-test", {})), \
         mock.patch.object(routes, "get_config", return_value={}), \
         mock.patch.object(routes, "_resolve_compatible_session_model_state", return_value=("gpt-test", None, False)), \
         mock.patch.object(routes, "_start_run", side_effect=fake_start_run):
        routes._handle_chat_start(handler, {
            "session_id": s.session_id,
            "message": "/skill-a x",
            "agent_message": "EXPANSION",
        })

    assert captured.get("agent_message") == "EXPANSION", captured


def test_start_run_forwards_agent_message_to_start_chat_stream(isolated_sessions):
    """#5896 re-gate route coverage: `_start_run` must forward `agent_message`
    into `_start_chat_stream_for_session` on the local backend."""
    from api import routes
    from api.models import new_session

    s = new_session(workspace="/tmp", model="gpt-test")
    captured = {}

    def fake_start_chat_stream(session, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "s1", "session_id": session.session_id}

    with mock.patch("api.runtime_adapter.runtime_adapter_enabled", return_value=False), \
         mock.patch("api.runtime_adapter.runtime_adapter_runner_enabled", return_value=False), \
         mock.patch.object(routes, "_start_chat_stream_for_session", side_effect=fake_start_chat_stream):
        routes._start_run(
            s,
            msg="/skill-a x",
            agent_message="EXPANSION",
            attachments=None,
            workspace="/tmp",
            model="gpt-test",
            model_provider="openai",
            normalized_model=False,
            source="webui",
            route="/api/chat/start",
            gateway_chat_enabled=False,
        )

    assert captured.get("agent_message") == "EXPANSION", captured


def test_regeneration_start_forwards_agent_message_to_the_locked_starter(isolated_sessions):
    """#5896 re-gate: a regenerated `/skill` turn must forward the client-resolved
    expansion into `_start_regeneration_stream_locked` (and therefore the
    worker). Removing the `agent_message=agent_message` keyword fails here."""
    from api import routes
    from api.models import new_session

    s = new_session(workspace="/tmp", model="gpt-test")
    captured = {}

    def fake_regen(value, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "s1", "session_id": value.session_id}

    with mock.patch.object(routes, "_agent_runtime_barrier_response", lambda **_k: None), \
         mock.patch.object(routes, "_active_run_stream_for_session", lambda *_a: None), \
         mock.patch.object(routes, "_start_regeneration_stream_locked", side_effect=fake_regen):
        routes._start_chat_stream_for_session(
            s,
            msg="/skill-a x",
            agent_message="EXPANSION",
            attachments=[],
            workspace="/tmp",
            model="gpt-test",
            model_provider="openai",
            external_runtime_owned=False,
            regeneration=mock.MagicMock(revision="rev"),
        )

    assert captured.get("agent_message") == "EXPANSION", captured


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_start_regeneration_client_sends_agent_message_for_skill_turn():
    """#5896 re-gate: the client regeneration path must re-resolve a `/skill`
    turn and send `agent_message` with `regenerate:true`; otherwise the replayed
    turn reaches the model as the raw command."""
    regen_start = MESSAGES_JS.index("async function startRegeneration(")
    regen_end = MESSAGES_JS.index("\nfunction ", regen_start + 1)
    regen_body = MESSAGES_JS[regen_start:regen_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    send_start = MESSAGES_JS.index("async function send(){")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _projectPickerOpenEpoch=7;
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
let _sendInProgress=false;
let _sendInProgressSid=null;
const S={session:{session_id:'s1',regeneration_revision:'rev'},messages:[{role:'user',content:'/my-skill do x'},{role:'assistant',content:'answer'}],activeProfile:'default'};
const INFLIGHT={};
const captured={};
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(){return {message:'EXPANSION'};}
async function newSession(){S.session={session_id:'CREATED'};}
function renderSessionList(){}
function renderMessages(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function markInflight(){}
function saveInflightState(){}
function showLiveRunStatus(){}
function updateSendBtn(){}
function attachLiveStream(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);return {stream_id:'s1'};}
  return {};
}
""" + resolver_body + regen_body + r"""
(async()=>{
  await startRegeneration('s1','rev');
  console.log('RESULT ' + JSON.stringify({body:captured.body||null}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    body = json.loads(line[len(marker):])["body"]
    assert body is not None, f"regeneration POST never fired; stderr={result.stderr!r}"
    assert body.get("regenerate") is True, body
    assert body.get("agent_message") == "EXPANSION", body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_start_regeneration_claims_the_chat_while_resolving():
    """#5896 re-gate: regeneration must claim the chat BEFORE awaiting the skill
    resolver. Otherwise a send started during the wait runs first, and the
    regeneration's error path restores the old snapshot over it."""
    regen_start = MESSAGES_JS.index("async function startRegeneration(")
    regen_end = MESSAGES_JS.index("\nfunction ", regen_start + 1)
    regen_body = MESSAGES_JS[regen_start:regen_end]
    resolver_start = MESSAGES_JS.index("async function _resolveSkillAgentMessage(")
    send_start = MESSAGES_JS.index("async function send(){")
    resolver_body = MESSAGES_JS[resolver_start:send_start]

    script = r"""
let _projectPickerOpenEpoch=7;
let _skillCreatedSessionSid=null;
let _skillResolutionRan=false;
let _sendInProgress=false;
let _sendInProgressSid=null;
const S={session:{session_id:'s1',regeneration_revision:'rev'},messages:[{role:'user',content:'/my-skill do x'},{role:'assistant',content:'answer'}],activeProfile:'default'};
const INFLIGHT={};
const captured={};
let releaseResolve;
let resolveStartedResolve;
const resolveStarted=new Promise(resolve=>{resolveStartedResolve=resolve;});
const resolveGate=new Promise(resolve=>{releaseResolve=resolve;});
function parseCommand(t){return String(t||'').startsWith('/my-skill')?{name:'my-skill',args:'do x'}:null;}
async function loadSkillCommands(){return [{name:'my-skill'}];}
async function resolveSkillCommand(){resolveStartedResolve();await resolveGate;return {message:'EXPANSION'};}
async function newSession(){S.session={session_id:'CREATED'};}
function renderSessionList(){}
function renderMessages(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function markInflight(){}
function saveInflightState(){}
function showLiveRunStatus(){}
function updateSendBtn(){}
function attachLiveStream(){}
async function api(path,opts){
  if(path==='/api/chat/start'){captured.body=JSON.parse(opts.body);return {stream_id:'s1'};}
  return {};
}
""" + resolver_body + regen_body + r"""
(async()=>{
  const pending=startRegeneration('s1','rev');
  await resolveStarted;
  const claimedDuringResolve=_sendInProgress===true;
  releaseResolve();
  await pending;
  console.log('RESULT ' + JSON.stringify({claimedDuringResolve,releasedAfter:_sendInProgress===false,body:captured.body||null}));
})().catch(e=>{console.error(e&&e.stack||e);process.exit(1);});
"""
    result = subprocess.run([NODE, "-e", script], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    marker = "RESULT "
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(marker)), "")
    assert line, f"no RESULT line; stdout={result.stdout!r} stderr={result.stderr!r}"
    payload = json.loads(line[len(marker):])
    assert payload["claimedDuringResolve"] is True, (
        "regeneration must claim the chat before awaiting the skill resolver; "
        f"a send would otherwise run first; got {payload!r}"
    )
    assert payload["releasedAfter"] is True, f"the claim must be released; got {payload!r}"
    assert payload["body"] is not None, f"regeneration POST never fired; stderr={result.stderr!r}"


def test_two_turn_skill_expansion_paginated_window_stays_raw(isolated_sessions):
    """#5896 re-gate blocker 1 (pagination): the older-message window built by
    `_message_window_for_display` reads session.messages and must contain the
    raw command only, never the expansion."""
    from api.models import get_session as _get_session, new_session
    from api.routes import _message_window_for_display

    raw_command = "/llm-wiki list active pages"
    expansion = (
        '[IMPORTANT: The user has invoked the "llm-wiki" skill, indicating they '
        "want you to follow its instructions. The full skill content is loaded below.]\n"
        "# llm-wiki\n\nfull skill body... (+500 lines)\n"
        "The user has provided the following instruction alongside the skill "
        "invocation: list active pages"
    )

    class EchoAgent:
        def __init__(self, *args, **kwargs):
            pass

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": f"reply after {len(history)}"},
                ]
            }

        def interrupt(self, _message):
            pass

    s = new_session(workspace="/tmp", model="gpt-test")
    s.save()
    _sync_chat_turn(s.session_id, raw_command, expansion, EchoAgent)
    _sync_chat_turn(s.session_id, "continue", None, EchoAgent)

    from api import models as _models
    _models.SESSIONS.pop(s.session_id, None)
    reloaded = _get_session(s.session_id)

    window, offset = _message_window_for_display(reloaded.messages, msg_limit=2)
    assert window, "expected a paginated window"
    assert offset >= 0
    assert all(expansion not in str(m.get("content") or "") for m in window), window
    all_user_rows = [
        m for m in reloaded.messages if isinstance(m, dict) and m.get("role") == "user"
    ]
    assert all(expansion not in str(m.get("content") or "") for m in all_user_rows)
    assert any(str(m.get("content") or "") == raw_command for m in all_user_rows)
