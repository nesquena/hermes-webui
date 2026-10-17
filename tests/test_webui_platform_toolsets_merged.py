"""Behavioral regression test: WebUI streaming merges platform_toolsets.webui.

The CLI resolves its toolsets with ``_get_platform_tools(config, "cli")``.
The WebUI streaming path used only ``_resolve_cli_toolsets(_cfg)`` (the cli
composite), so any toolset configured under ``platform_toolsets.webui`` was
silently ignored — the only way to add a WebUI-only tool was to duplicate it
under the ``cli`` key, which also leaks it into terminal sessions.

These tests drive the real ``_run_agent_streaming`` path with a capturing fake
agent (harness borrowed from ``tests/test_issue1857_usage_overwrite.py``) and
assert on the ``enabled_toolsets`` actually handed to the agent — not on source
text — so a discarded merge or a swallowed exception is caught.
"""

import ast
import contextlib
import pathlib
import queue
import shutil
import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

# Sentinel for sys.modules save/restore — distinguishes "key wasn't there" from None.
_MISSING = object()


class _CapturingAgent:
    """Fake agent that records the constructor kwargs it was built with."""

    captured = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        self.session_id = kwargs.get("session_id")
        self.context_compressor = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.reasoning_config = None
        self.ephemeral_system_prompt = None
        self._last_error = None

    def run_conversation(self, **kwargs):
        history = kwargs.get("conversation_history", [])
        return {
            "messages": history + [
                {"role": "user", "content": kwargs.get("persist_user_message", "")},
                {"role": "assistant", "content": "ok"},
            ]
        }

    def interrupt(self, _message):
        pass


class _FakeSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.title = "t"
        self.workspace = "/tmp"
        self.model = "gpt-5.4"
        self.model_provider = None
        self.profile = None
        self.personality = None
        self.messages = [{"role": "user", "content": "old"}]
        self.context_messages = list(self.messages)
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = 0.0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.tool_calls = []
        self.gateway_routing = None
        self.gateway_routing_history = []
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.pending_user_source = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.llm_title_generated = True
        self.enabled_toolsets = None

    def save(self, *args, **kwargs):
        pass

    def compact(self):
        return {
            "session_id": self.session_id, "title": self.title, "workspace": self.workspace,
            "model": self.model, "created_at": 0, "updated_at": 0, "pinned": False,
            "archived": False, "project_id": None, "profile": self.profile,
            "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "personality": None,
        }


def _make_platform_getter(webui_getter):
    """Build a stand-in for hermes_cli.tools_config._get_platform_tools."""
    if webui_getter is None:
        def _boom(config, platform):
            raise ImportError("no hermes_cli.tools_config")
        return _boom
    if isinstance(webui_getter, BaseException) or (
        isinstance(webui_getter, type) and issubclass(webui_getter, BaseException)
    ):
        def _boom(config, platform):
            raise webui_getter
        return _boom
    return webui_getter


def _run_turn(session_id, cli_toolsets, webui_getter, *, session_override=None):
    """Drive one settle turn with given cli toolsets and webui platform getter.

    Returns the ``enabled_toolsets`` the fake agent was actually constructed with.

    ``session_override`` (a list) simulates a session whose ``enabled_toolsets``
    is set; it is expected to win over the merged config list.
    """
    import api.streaming as streaming

    stream_id = f"stream_{session_id}"
    session = _FakeSession(session_id)
    session.active_stream_id = stream_id
    fake_queue = queue.Queue()

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(return_value={
        "provider": "openai", "base_url": None, "api_key": "sk-test",
        "api_mode": "chat_completions", "command": None, "args": [],
        "credential_pool": None,
    })
    fake_tools_config = types.ModuleType("hermes_cli.tools_config")
    fake_tools_config._get_platform_tools = _make_platform_getter(webui_getter)
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_cli.tools_config = fake_tools_config
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)

    _injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_cli.tools_config": fake_tools_config,
        "hermes_state": fake_hermes_state,
    }
    _saved = {k: sys.modules.get(k, _MISSING) for k in _injected}
    sys.modules.update(_injected)

    # The settle path gates the #493 per-session override on the session file
    # existing; point SESSION_DIR at a throwaway temp dir that holds one.
    import tempfile
    session_dir = pathlib.Path(tempfile.mkdtemp(prefix="webui-toolsets-"))
    patches = [
        mock.patch.object(streaming, "get_session", return_value=session),
        mock.patch.object(streaming, "_get_ai_agent", return_value=_CapturingAgent),
        mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-5.4", "openai", None)),
        mock.patch("api.config.get_config", return_value={}),
        mock.patch("api.config._resolve_cli_toolsets", return_value=list(cli_toolsets)),
    ]
    try:
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            if session_override is not None:
                session_dir.mkdir(parents=True, exist_ok=True)
                (session_dir / f"{session_id}.json").write_text("{}", encoding="utf-8")
                import api.models as models_mod
                stack.enter_context(mock.patch.object(models_mod, "SESSION_DIR", session_dir))
                stack.enter_context(mock.patch.object(
                    models_mod.Session, "load_metadata_only",
                    return_value=types.SimpleNamespace(enabled_toolsets=list(session_override)),
                ))
            streaming.STREAMS[stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=session_id, msg_text="hi", model="gpt-5.4",
                workspace="/tmp", stream_id=stream_id,
            )
    finally:
        for k, prev in _saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev
        shutil.rmtree(session_dir, ignore_errors=True)

    return _CapturingAgent.captured.get("enabled_toolsets")


def test_webui_platform_toolsets_are_merged_into_agent_enabled_toolsets():
    """The agent must receive the union of cli + platform_toolsets.webui."""
    got = _run_turn(
        "toolsets_merge",
        cli_toolsets=["core", "web"],
        webui_getter=lambda cfg, platform: ["webui_only", "core"] if platform == "webui" else [],
    )
    assert got is not None, "agent was not constructed"
    assert set(got) == {"core", "web", "webui_only"}, got


def test_webui_merge_preserves_cli_toolsets():
    """Union semantics: the cli toolsets are never dropped by the merge."""
    got = _run_turn(
        "toolsets_preserve",
        cli_toolsets=["core", "web"],
        webui_getter=lambda cfg, platform: ["extra"],
    )
    assert got is not None
    assert {"core", "web", "extra"} <= set(got), got


def test_raising_helper_keeps_cli_toolsets():
    """A raising helper must not abort the turn or empty the toolset list."""
    got = _run_turn(
        "toolsets_raises",
        cli_toolsets=["core", "web"],
        webui_getter=RuntimeError("no helper"),
    )
    assert set(got) == {"core", "web"}, got


def test_missing_helper_keeps_cli_toolsets():
    """An ImportError from an older hermes-agent keeps the cli toolsets."""
    got = _run_turn(
        "toolsets_missing",
        cli_toolsets=["core", "web"],
        webui_getter=None,
    )
    assert set(got) == {"core", "web"}, got


def test_webui_merge_does_not_override_per_session_toolsets():
    """A session's explicit enabled_toolsets still takes precedence."""
    got = _run_turn(
        "toolsets_session_override",
        cli_toolsets=["core"],
        webui_getter=lambda cfg, platform: ["webui_only"],
        session_override=["only_this"],
    )
    assert got == ["only_this"], got


def test_syntax_valid():
    ast.parse((ROOT / "api" / "streaming.py").read_text(encoding="utf-8"))
