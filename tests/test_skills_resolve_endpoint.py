"""Tests for the /api/commands/skills/resolve endpoint and frontend wiring."""

from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType

import sys
import pytest

import api.commands as commands

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
    assert "async function resolveSkillCommand(text)" in COMMANDS_JS
    assert "api('/api/commands/skills/resolve'" in COMMANDS_JS


def test_resolve_skill_command_uses_post_with_command_body():
    """resolveSkillCommand() must POST a JSON body with a 'command' field."""
    idx = COMMANDS_JS.index("async function resolveSkillCommand(text)")
    body = COMMANDS_JS[idx:]
    assert "method:'POST'" in body
    assert "body:JSON.stringify({command})" in body
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
    assert "_parsedCmd.name" in body
    assert "_slashDisplayTextOverride = text" in body
    assert "text = _skillMessage" in body


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


# ── Static source-code assertions (Python backend) ─────────────────────────


def test_skills_resolve_route_wired():
    """POST /api/commands/skills/resolve must be registered in routes.py."""
    assert '/api/commands/skills/resolve"' in ROUTES_PY
    assert "resolve_skill_command(command)" in ROUTES_PY


def test_resolve_skill_command_function_defined():
    """resolve_skill_command() must be defined in api/commands.py."""
    assert "def resolve_skill_command(command: str) -> dict[str, Any]:" in COMMANDS_PY
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
        seen["build"] = (key, instr)
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
        "build": ("/llm-wiki", "list pages"),
    }


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
        seen["build"] = (key, instr)
        return f"resolved: {instr}"

    _install_fake_skill_commands(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/gif-search cats and dogs")

    assert result["message"] == "resolved: cats and dogs"
    assert seen["build"] == ("/gif-search", "cats and dogs")


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
        seen["build"] = (key, instr)
        assert instr == "", f"Expected empty string, got {instr!r}"
        return f"resolved bare skill: {key}"

    _install_fake_skill_commands(monkeypatch, resolver=_resolve, builder=_build)
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/llm-wiki")

    assert result["message"] == "resolved bare skill: /llm-wiki"
    assert seen["build"][1] == ""


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
        return ("stacked body: loaded!", ["skill-a", "skill-b"], [])

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

    result = commands.resolve_skill_command("/skill-a /skill-b do X")

    assert result["name"] == "skill-a"
    assert result["source"] == "stacked_skill"
    assert result["message"] == "stacked body: loaded!"
    assert seen["type"] == "stacked"
    assert seen["keys"] == ["/skill-a", "/skill-b"]
    assert seen["instr"] == "do X"


def test_resolve_stacked_skills_falls_back_to_single(monkeypatch):
    """``/skill-a do X`` with no extra stacked skills must call the single
    builder (not the stacked builder)."""

    @contextmanager
    def _profile_scope(purpose):
        yield

    seen = {"type": None}

    def _resolve(name):
        return f"/{name}"

    def _splitter(rest):
        return ([], rest)  # No extra keys

    def _stacked_builder(keys, instr="", task_id=None):
        seen["type"] = "stacked"
        return None

    def _single_builder(key, instr="", task_id=None, runtime_note=""):
        seen["type"] = "single"
        return "[IMPORTANT: The user has invoked the \"llm-wiki\" skill...]\nbody\nUser instruction: do X"

    _install_fake_skill_commands(
        monkeypatch,
        resolver=_resolve,
        builder=_single_builder,
        stacked_builder=_stacked_builder,
        splitter=_splitter,
    )
    monkeypatch.setattr(commands, "_bundle_profile_context", _profile_scope)

    result = commands.resolve_skill_command("/skill-a do X")

    assert result["name"] == "skill-a"
    assert result["source"] == "skill"
    assert seen["type"] == "single"


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
    assert '/api/commands/skills/resolve"' in ROUTES_PY
