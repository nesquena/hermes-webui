import queue
import sys
import types
from typing import Callable, cast
from unittest import mock


_MISSING = object()


def test_visible_progress_token_reasoning_and_interim_are_deduped(cleanup_test_sessions):
    """Progress text can arrive through three Hermes callbacks; WebUI must show it once.

    Some runtimes emit a user-visible progress sentence as a normal token, mirror the
    same text through reasoning, and then report it through interim_assistant before
    a tool call. The SSE bridge should keep the visible token, suppress the hidden
    reasoning echo, and mark interim_assistant as already_streamed so the client and
    journal recovery do not append the same paragraph again.
    """
    import api.streaming as streaming

    progress = "Gefunden: der Skill-Tab lädt `/api/skill-html?slug=...`."

    class FakeSession:
        def __init__(self):
            self.session_id = "issue_progress_echo_dedupe"
            self.title = "Progress echo"
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
                "created_at": 0,
                "updated_at": 0,
                "pinned": False,
                "archived": False,
                "project_id": None,
                "profile": self.profile,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost": self.estimated_cost,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "personality": self.personality,
            }

    class EchoAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            prefill_messages=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            interim_assistant_callback=None,
            **_kwargs,
        ):
            self.stream_delta_callback = cast(Callable[[str], None], stream_delta_callback)
            self.reasoning_callback = cast(Callable[[str], None], reasoning_callback)
            self.tool_progress_callback = cast(Callable[..., None], tool_progress_callback)
            self.interim_assistant_callback = cast(Callable[[str], None], interim_assistant_callback)
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
            self.stream_delta_callback(progress)
            self.reasoning_callback(progress)
            self.tool_progress_callback("reasoning.available", "progress", progress, {})
            self.interim_assistant_callback(progress)
            history = kwargs.get("conversation_history", [])
            return {"messages": history + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": progress},
            ]}

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = "stream_issue_progress_echo_dedupe"
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
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
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__dict__["runtime_provider"] = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
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
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="scan",
                model="gpt-test",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)
        for k, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = cast(types.ModuleType, prev)

    events = list(fake_queue.queue)
    assert [(event, payload) for event, payload in events if event == "token"] == [
        ("token", {"text": progress})
    ]
    assert not [payload for event, payload in events if event == "reasoning" and payload.get("text") == progress]
    interim = [payload for event, payload in events if event == "interim_assistant"]
    assert interim == [{"text": progress, "already_streamed": True}]


def test_reasoning_then_interim_progress_marks_reasoning_echo(cleanup_test_sessions):
    """A progress sentence mirrored as reasoning first must become prose, not Thinking.

    Some reasoning-heavy runtimes emit the same user-facing status sentence first
    through the reasoning callback and later through interim_assistant. The first
    reasoning SSE may already be in the browser/journal, so the bridge must mark
    the interim event and strip the durable reasoning tail before settlement.
    """
    import api.streaming as streaming

    progress = "我先检查当前仓库状态，然后定位重复渲染路径。"

    class FakeSession:
        def __init__(self):
            self.session_id = "issue_reasoning_interim_echo"
            self.title = "Reasoning interim echo"
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
                "created_at": 0,
                "updated_at": 0,
                "pinned": False,
                "archived": False,
                "project_id": None,
                "profile": self.profile,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost": self.estimated_cost,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "personality": self.personality,
            }

    class ReasoningThenInterimAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            prefill_messages=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            interim_assistant_callback=None,
            **_kwargs,
        ):
            self.reasoning_callback = cast(Callable[[str], None], reasoning_callback)
            self.interim_assistant_callback = cast(Callable[[str], None], interim_assistant_callback)
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
            self.reasoning_callback(progress)
            self.interim_assistant_callback(progress)
            history = kwargs.get("conversation_history", [])
            return {"messages": history + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": progress},
            ]}

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = "stream_issue_reasoning_interim_echo"
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
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
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__dict__["runtime_provider"] = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
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
             mock.patch.object(streaming, "_get_ai_agent", return_value=ReasoningThenInterimAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-test", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="scan",
                model="gpt-test",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)
        for k, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = cast(types.ModuleType, prev)

    events = list(fake_queue.queue)
    interim = [payload for event, payload in events if event == "interim_assistant"]
    assert interim == [{
        "text": progress,
        "already_streamed": False,
        "reasoning_echo": True,
    }]
    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "run should settle"
    final_messages = done_payloads[-1]["session"]["messages"]
    assert not any(message.get("reasoning") == progress for message in final_messages)


def test_final_answer_prefix_reasoning_echo_is_not_journaled_or_merged(cleanup_test_sessions):
    """A final-answer prefix mirrored through reasoning must not enter Worklog.

    The observed production failure had the final answer stream normally through
    token events, then a later reasoning event carried the first 500 characters
    of that same final answer. Since `put()` journals before queue delivery, this
    regression covers the live stream and run-journal replay boundary together;
    the done payload covers final session merge/reload state.
    """
    import api.streaming as streaming
    from api.run_journal import read_run_events

    final_answer = (
        "已按 Hermes WebUI workflow 在独立 worktree 完成，本地 review-ready；"
        "没有 push、没有开 PR、没有改真实 cron/config，也没有在主 checkout 实现。\n\n"
        "## 位置\n\n"
        "| 项 | 值 |\n|---|---|\n"
        "| Worktree | `/Users/xuefusong/hermes-webui-worktrees/example` |\n"
        "| Branch | `fix/example` |\n\n"
        "## 根因\n\n"
        "Final Answer 正文已经作为 assistant token 流出，不应该再作为 reasoning/Worklog 事件出现。\n\n"
        "## 验证\n\n"
        "相关 targeted regression 覆盖 live stream、run journal replay 和 final merge。"
    )
    leaked_prefix = final_answer[:500]

    class FakeSession:
        def __init__(self):
            self.session_id = "issue_final_answer_reasoning_echo"
            self.title = "Final echo"
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
                "created_at": 0,
                "updated_at": 0,
                "pinned": False,
                "archived": False,
                "project_id": None,
                "profile": self.profile,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "estimated_cost": self.estimated_cost,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "personality": self.personality,
            }

    class FinalEchoAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            prefill_messages=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            interim_assistant_callback=None,
            **_kwargs,
        ):
            self.stream_delta_callback = cast(Callable[[str], None], stream_delta_callback)
            self.reasoning_callback = cast(Callable[[str], None], reasoning_callback)
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
            self.stream_delta_callback(final_answer)
            # This mirrors the production journal shape: final answer content was
            # emitted as visible tokens first, then incorrectly reported as a
            # reasoning delta near stream end.
            self.reasoning_callback(leaked_prefix)
            history = kwargs.get("conversation_history", [])
            return {"messages": history + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": final_answer},
            ]}

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = "stream_issue_final_answer_reasoning_echo"
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
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
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__dict__["runtime_provider"] = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
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
             mock.patch.object(streaming, "_get_ai_agent", return_value=FinalEchoAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-test", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="ship it",
                model="gpt-test",
                workspace="/tmp",
                stream_id=fake_stream_id,
            )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)
        for k, prev in saved.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = cast(types.ModuleType, prev)

    events = list(fake_queue.queue)
    assert [(event, payload) for event, payload in events if event == "token"] == [
        ("token", {"text": final_answer})
    ]
    assert not [payload for event, payload in events if event == "reasoning" and payload.get("text") == leaked_prefix]

    journal_events = read_run_events(fake_session.session_id, fake_stream_id)["events"]
    assert any(event.get("type") == "token" and event.get("payload", {}).get("text") == final_answer for event in journal_events)
    assert not [
        event for event in journal_events
        if event.get("type") == "reasoning" and event.get("payload", {}).get("text") == leaked_prefix
    ]

    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "run should settle"
    final_messages = done_payloads[-1]["session"]["messages"]
    assistant_messages = [message for message in final_messages if message.get("role") == "assistant"]
    assert assistant_messages[-1]["content"] == final_answer
    assert leaked_prefix not in str(assistant_messages[-1].get("reasoning") or "")
    assert leaked_prefix not in str(assistant_messages[-1].get("reasoning_content") or "")
