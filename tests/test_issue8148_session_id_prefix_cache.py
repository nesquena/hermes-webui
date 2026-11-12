"""#8148 — two new chats must send the same system text.

``_webui_surface_context_prompt()`` wrote ``- Session ID: <id>`` into the
ephemeral system prompt, above the progress prompt. The id is different for
every chat, so the system text of two new chats differed from that line on, and
a provider's prefix cache could reuse nothing after it. hermes-agent appends
the ephemeral prompt to the end of the system text, so that line was the first
per-chat difference in the whole request.

The id is now left out unless ``webui.pass_session_id: true`` asks for it (the
same switch and default as hermes-agent's ``pass_session_id``), and when it is
asked for it is the last thing in the prompt.

The last tests drive the production worker, ``_run_agent_streaming``, with a
stand-in agent, and read what the agent was handed: a field added above the
fixed text later fails here and not on someone's bill.
"""
from __future__ import annotations

import queue
import sys
import types
from unittest import mock

import pytest

from api.streaming import _webui_ephemeral_system_prompt

FIRST = "20261010_190000_aaaaaa"
SECOND = "20261010_190500_bbbbbb"
OPT_IN = {"webui": {"pass_session_id": True}}


def _prompt(session_id, *, profile="default", workspace="/home/me/project", config=None,
            personality=None):
    return _webui_ephemeral_system_prompt(
        personality,
        surface_context={
            "source": "webui",
            "session_id": session_id,
            "profile": profile,
            "workspace": workspace,
        },
        config_data={} if config is None else config,
    )


def _shared_prefix(first: str, second: str) -> int:
    for index, (left, right) in enumerate(zip(first, second, strict=False)):
        if left != right:
            return index
    return min(len(first), len(second))


class TestByDefault:
    def test_two_new_chats_get_the_same_text(self):
        assert _prompt(FIRST) == _prompt(SECOND)

    def test_the_id_is_nowhere_in_the_text(self):
        prompt = _prompt(FIRST)

        assert FIRST not in prompt
        assert "Session ID" not in prompt

    def test_the_rest_of_the_surface_context_is_still_there(self):
        prompt = _prompt(FIRST)

        assert "WebUI session context:" in prompt
        assert "- Source: webui" in prompt
        assert "- Profile: default" in prompt
        assert "- Workspace: /home/me/project" in prompt
        assert "Connected Platforms:" in prompt

    def test_with_a_personality_too(self):
        assert _prompt(FIRST, personality="Be brief.") == _prompt(SECOND, personality="Be brief.")

    @pytest.mark.parametrize(
        "other", [{"profile": "work"}, {"workspace": "/home/me/other"}],
    )
    def test_a_different_profile_or_workspace_is_still_a_different_text(self, other):
        """The control: these two do describe a different chat surface."""
        assert _prompt(FIRST) != _prompt(FIRST, **other)

    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"webui": {}},
            {"webui": None},
            {"webui": "pass_session_id"},
            {"webui": ["pass_session_id"]},
            {"webui": {"pass_session_id": False}},
            {"webui": {"pass_session_id": None}},
            {"webui": {"pass_session_id": 0}},
            {"webui": {"pass_session_id": "false"}},
            {"webui": {"pass_session_id": "no"}},
            {"pass_session_id": True},
        ],
    )
    def test_anything_but_an_explicit_true_leaves_it_out(self, config):
        assert FIRST not in _prompt(FIRST, config=config)


class TestWhenAskedFor:
    def test_the_id_is_the_last_line(self):
        prompt = _prompt(FIRST, config=OPT_IN)

        assert prompt.splitlines()[-1] == f"- Session ID: {FIRST}"
        assert prompt.count(FIRST) == 1

    def test_everything_before_the_id_is_shared(self):
        first, second = _prompt(FIRST, config=OPT_IN), _prompt(SECOND, config=OPT_IN)

        assert first != second
        shared = _shared_prefix(first, second)
        assert first[:shared] == _prompt(FIRST) + "\n\nWebUI session:\n- Session ID: 20261010_190"
        # nothing but the id itself differs
        assert len(first) - shared == len(FIRST) - len("20261010_190")

    @pytest.mark.parametrize("session_id", ["", "   ", None])
    def test_no_id_no_line(self, session_id):
        prompt = _prompt(session_id, config=OPT_IN)

        assert "Session ID" not in prompt
        assert prompt == _prompt(session_id)

    def test_the_id_is_trimmed(self):
        assert _prompt(f"  {FIRST}\n", config=OPT_IN).endswith(f"- Session ID: {FIRST}")

    @pytest.mark.parametrize("value", ["true", "True", "yes", "on", 1])
    def test_yaml_spellings_of_true_that_arrive_as_text_or_one(self, value):
        """A hand-edited config.yaml may quote the value."""
        prompt = _prompt(FIRST, config={"webui": {"pass_session_id": value}})

        assert prompt.endswith(f"- Session ID: {FIRST}")

    def test_without_config_data_the_saved_config_decides(self, monkeypatch):
        import api.streaming as streaming

        monkeypatch.setattr(streaming, "get_config", lambda: OPT_IN)
        asked = _webui_ephemeral_system_prompt(
            None, surface_context={"source": "webui", "session_id": FIRST}
        )
        monkeypatch.setattr(streaming, "get_config", lambda: {})
        default = _webui_ephemeral_system_prompt(
            None, surface_context={"source": "webui", "session_id": FIRST}
        )

        assert asked.endswith(f"- Session ID: {FIRST}")
        assert FIRST not in default


def _run_worker(session_id, config, tmp_path, monkeypatch):
    """Drive the production local worker for one turn of a new chat.

    Only the agent and the provider lookup are stand-ins. Returns what the
    agent was handed: its ephemeral prompt and ``run_conversation``'s kwargs.
    """
    from api import run_journal, streaming
    from api.config import SESSION_AGENT_CACHE

    SESSION_AGENT_CACHE.clear()
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path)
    stream_id = f"run-{session_id}"
    seen = {}

    class Session:
        title = "Session"
        workspace = "/tmp"
        model = "primary"
        model_provider = "anthropic"
        profile = None
        personality = None
        messages = []
        context_messages = messages
        input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0
        estimated_cost = 0.0
        tool_calls = []
        gateway_routing = None
        gateway_routing_history = []
        active_stream_id = stream_id
        pending_user_message = None
        pending_attachments = []
        pending_started_at = None
        context_length = threshold_tokens = last_prompt_tokens = 0
        llm_title_generated = True

        def save(self, *args, **kwargs):
            pass

        def compact(self):
            return {"session_id": self.session_id}

    Session.session_id = session_id

    class Agent:
        def __init__(self, model=None, provider=None, session_id=None,
                     stream_delta_callback=None, reasoning_callback=None,
                     status_callback=None, **kwargs):
            self.model, self.provider, self.session_id = model, provider, session_id
            self.stream_delta_callback = stream_delta_callback
            self._provider_fallback_active = False
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = self.session_cache_write_tokens = 0
            self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None

        def run_conversation(self, **kwargs):
            seen["ephemeral"] = self.ephemeral_system_prompt
            seen["kwargs"] = kwargs
            self.stream_delta_callback("4")
            return {"messages": kwargs.get("conversation_history", []) + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": "4"}]}

        def interrupt(self, message):
            pass

    runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = mock.Mock(return_value={
        "provider": "anthropic", "base_url": None, "api_key": "sk-test",
        "api_mode": "chat_completions", "command": None, "args": [], "credential_pool": None})
    cli_module = types.ModuleType("hermes_cli")
    cli_module.runtime_provider = runtime_module
    state_module = types.ModuleType("hermes_state")
    state_module.SessionDB = mock.Mock(return_value=None)
    injected = {"hermes_cli": cli_module, "hermes_cli.runtime_provider": runtime_module,
                "hermes_state": state_module}
    sentinel = object()
    saved = {name: sys.modules.get(name, sentinel) for name in injected}
    sys.modules.update(injected)
    try:
        with mock.patch.object(streaming, "get_session", return_value=Session()), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=Agent), \
             mock.patch.object(streaming, "resolve_model_provider",
                               return_value=("primary", "anthropic", None)), \
             mock.patch("api.config.get_config", return_value=config), \
             mock.patch.object(streaming, "get_config", return_value=config), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[stream_id] = queue.Queue()
            streaming._run_agent_streaming(
                session_id, "What is 2+2?", "primary", "/tmp", stream_id
            )
    finally:
        for name, original in saved.items():
            if original is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        streaming.STREAMS.pop(stream_id, None)
        SESSION_AGENT_CACHE.clear()
    assert seen, "the worker never reached the agent"
    return seen


class TestWhatTheAgentIsHanded:
    def test_two_new_chats_hand_the_agent_the_same_system_text(self, tmp_path, monkeypatch):
        first = _run_worker(FIRST, {}, tmp_path, monkeypatch)
        second = _run_worker(SECOND, {}, tmp_path, monkeypatch)

        assert first["ephemeral"] and first["ephemeral"] == second["ephemeral"]
        assert first["kwargs"]["system_message"] == second["kwargs"]["system_message"]
        for seen, session_id in ((first, FIRST), (second, SECOND)):
            assert session_id not in seen["ephemeral"]
            assert session_id not in (seen["kwargs"]["system_message"] or "")

    def test_the_setting_reaches_the_worker(self, tmp_path, monkeypatch):
        first = _run_worker(FIRST, OPT_IN, tmp_path, monkeypatch)
        second = _run_worker(SECOND, OPT_IN, tmp_path, monkeypatch)

        assert first["ephemeral"].endswith(f"- Session ID: {FIRST}")
        assert second["ephemeral"].endswith(f"- Session ID: {SECOND}")
        shared = _shared_prefix(first["ephemeral"], second["ephemeral"])
        assert len(first["ephemeral"]) - shared <= len(FIRST)
