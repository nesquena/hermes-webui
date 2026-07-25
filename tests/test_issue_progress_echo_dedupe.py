import queue
import re
import sys
import types
from typing import Callable, cast
from unittest import mock

import pytest


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


def test_redaction_tolerant_match_treats_only_mask_runs_as_wildcards():
    """Unit: `***` mask runs match one scalar; every other char must match literally.

    The live token stream keeps the real secret; the interim progress echo is
    credential-redacted. `_redaction_tolerant_match` lets each `***` mask run
    stand for exactly one bounded scalar of the original value, so a
    redaction-only difference matches — while a genuinely different
    (non-credential) value still fails, including non-credential *string* values
    (the false-positive boundary).
    """
    import api.streaming as streaming

    match = streaming._redaction_tolerant_match

    streamed = 'seed with `{"username":"sam","password":"koreader1","status":"pending"}`'
    redacted = 'seed with `{"username":"sam","password":"***","status":"pending"}`'
    # Redaction-only difference must be recognized as an echo (suffix + substring).
    assert match(redacted, streamed, anchor_end=True), "redaction-only diff must match as tail echo"
    assert match(redacted, streamed, anchor_end=False), "redaction-only diff must match as substring"

    # A non-credential STRING value that differs alongside the credential must NOT
    # match — otherwise a real status update would be silently dropped (P2 boundary).
    streamed_started = 'seed with `{"password":"koreader1","status":"started"}`'
    redacted_completed = 'seed with `{"password":"***","status":"completed"}`'
    assert not match(redacted_completed, streamed_started, anchor_end=True), (
        "a differing non-credential string value must NOT be masked away"
    )
    assert not match(redacted_completed, streamed_started, anchor_end=False)

    # A mask-free candidate never engages the relaxed path (strict compare owns it).
    assert not match(streamed, streamed, anchor_end=True)

    # The strict comparator must still see the redacted vs. unredacted text as different.
    assert streaming._compact_for_echo_compare(streamed) != streaming._compact_for_echo_compare(redacted)


def test_redaction_mask_stays_within_one_scalar_across_all_delimiters():
    """P2 regression: a `***` mask must occupy exactly ONE scalar/token.

    Two reported false-dedups where a single mask consumed changed status text:

      1. First review — space-delimited prose: `Using token *** completed`
         matched `Using token sk-abc123 failed then retried and completed`
         because the old `[^"]*` wildcard ate the words between the secret and
         the trailing `completed`.
      2. Second review — the `;`/`|` field separators were not treated as scalar
         boundaries, so `token=***;status=completed` matched
         `token=sk-abc123;status=failed;status=completed`, silently dropping the
         genuinely-different `status=failed` interim.

    All of these must fail closed on BOTH the suffix and substring paths, while
    legitimate redaction-only differences across the same delimiters still dedup.
    """
    import api.streaming as streaming

    match = streaming._redaction_tolerant_match

    # (1) space-delimited prose — must not be consumed by one mask.
    prose_cand = "Using token *** completed"
    prose_vis = "Using token sk-abc123 failed then retried and completed"
    assert not match(prose_cand, prose_vis, anchor_end=True)
    assert not match(prose_cand, prose_vis, anchor_end=False)

    # (2) semicolon and pipe field separators — must be scalar boundaries.
    semi_cand = "token=***;status=completed"
    semi_vis = "token=sk-abc123;status=failed;status=completed"
    assert not match(semi_cand, semi_vis, anchor_end=False), (
        "a mask must not span the ';' delimiter into a changed status field"
    )
    assert not match(semi_cand, semi_vis, anchor_end=True)

    pipe_cand = "token=***|status=completed"
    pipe_vis = "token=sk-abc123|status=failed|status=completed"
    assert not match(pipe_cand, pipe_vis, anchor_end=False), (
        "a mask must not span the '|' delimiter into a changed status field"
    )

    # (3) Third review: EVERY supported outside separator must fail closed the same
    # way. `&`, `=` and `?` were missing from the boundary set, so a query-string /
    # form-encoded progress line could still have its changed status swallowed.
    for sep in (";", "|", "&", ",", "=", "?", " "):
        cand = f"token=***{sep}status=completed"
        vis = f"token=sk-abc123{sep}status=failed{sep}status=completed"
        assert not match(cand, vis, anchor_end=False), (
            f"a mask must not span the {sep!r} separator into a changed status field"
        )
        assert not match(cand, vis, anchor_end=True), (
            f"a mask must not span the {sep!r} separator into a changed status field"
        )

    # Two-mask semicolon variant — the middle field genuinely changed.
    assert not match("a=***;b=***;c=x", "a=s1;b=y;b=s2;c=x", anchor_end=False)

    # Legitimate redaction-only differences across the same delimiters dedup.
    assert match("token=***;status=ok", "token=sk-abc123;status=ok", anchor_end=True)
    assert match("token=***|status=ok", "token=sk-abc123|status=ok", anchor_end=False)
    assert match("token=***&status=ok", "token=sk-abc123&status=ok", anchor_end=True)
    assert match("api_key: ***", "api_key: sk-abc123", anchor_end=True)
    assert match("api_key: ***", "api_key: sk-abc123", anchor_end=False)


def test_redaction_mask_matches_quoted_secret_containing_delimiters():
    """Third-review regression: a QUOTED secret may legitimately contain delimiters.

    The context-free lexer split every `"`, `,`, `;`, `|` and space regardless of
    whether it sat inside a quoted scalar. A redacted candidate has one `***`
    field, but an otherwise-identical live secret containing one of those
    characters lexed into several units, so the 1:1 unit alignment rejected a
    genuine redaction-only echo — leaving the original navigate-back duplication
    bug in place for exactly those credential values.

    A mask that constitutes an ENTIRE quoted scalar now consumes the whole visible
    quoted region up to the matching close quote, so such secrets dedup again. This
    must NOT weaken the false-dedup protection: the quoted region is bounded by the
    close quote, so a changed sibling field outside it still fails closed.
    """
    import api.streaming as streaming

    match = streaming._redaction_tolerant_match

    for label, secret in (
        ("semicolon", "ab;cd"),
        ("pipe", "ab|cd"),
        ("comma", "ab,cd"),
        ("space", "ab cd"),
        ("equals", "ab=cd"),
        ("ampersand", "ab&cd"),
        ("question", "ab?cd"),
        ("mixed", "a;b|c,d e=f"),
    ):
        cand = 'api_key: "***"'
        vis = f'api_key: "{secret}"'
        assert match(cand, vis, anchor_end=True), (
            f"a quoted secret containing {label} must still dedup (suffix path)"
        )
        assert match(cand, vis + " and then more streamed prose", anchor_end=False), (
            f"a quoted secret containing {label} must still dedup (substring path)"
        )

    # Backtick and single-quote scalars behave the same way.
    assert match("key: `***`", "key: `ab;cd`", anchor_end=True)
    assert match("key: '***'", "key: 'ab,cd'", anchor_end=True)

    # Realistic JSON progress shape with a delimiter-bearing secret.
    assert match(
        '{"user":"sam","password":"***","status":"ok"}',
        '{"user":"sam","password":"a;b c","status":"ok"}',
        anchor_end=True,
    )

    # An escaped quote inside the secret stays inside the scalar.
    assert match('k="***"', 'k="ab\\"cd"', anchor_end=True)

    # CRITICAL: the quoted-scalar allowance must not reopen the false-dedup hole —
    # a changed field OUTSIDE the quoted secret still fails closed.
    assert not match(
        '{"password":"***","status":"completed"}',
        '{"password":"a;b","status":"started"}',
        anchor_end=True,
    ), "a changed sibling field must not be swallowed by a quoted mask"
    assert not match(
        '{"password":"***","status":"completed"}',
        '{"password":"a;b","status":"started"}',
        anchor_end=False,
    )


def test_redaction_mask_leading_trailing_and_multiple_forms():
    """Cover leading/trailing/empty/multiple mask placements in both modes.

    A mask must consume at least one character of its scalar, the surrounding
    literals must line up, and multiple masks each bind to their own field.
    """
    import api.streaming as streaming

    match = streaming._redaction_tolerant_match

    # Leading and trailing masks (redaction-only) dedup on both anchors.
    assert match("***-tail", "secret-tail", anchor_end=True)
    assert match("***-tail", "secret-tail", anchor_end=False)
    assert match("head-***", "head-secret", anchor_end=True)

    # A mask must stand for >=1 real char — an empty expansion must NOT match.
    assert not match("***tail", "tail", anchor_end=True)

    # Multiple masks, each bound to its own quoted scalar, still dedup.
    assert match('x"***"y"***"z', 'x"secretA"y"secretB"z', anchor_end=True)
    assert match('x"***"y"***"z', 'x"secretA"y"secretB"z', anchor_end=False)

    # But if a NON-masked field between two masks differs, it must fail closed.
    assert not match('x"***"CHANGED"***"z', 'x"a"ORIG"b"z', anchor_end=True)


def test_redaction_tolerant_match_cogrowing_near_match_is_bounded():
    """CORE regression: work must stay bounded when BOTH inputs grow — revert-sensitive.

    The `anchor_end=False` call in `_is_visible_output_echo` scans the full,
    unbounded visible output synchronously from the streaming callback, so a
    pathological input stalls the live stream for that turn.

    History of this gate:
      * regex `[^"]*`-per-mask — catastrophic backtracking (2.73s at 4 masks/128
        chars, >10s at the caller-sized probe).
      * per-offset `str.find` restart — Θ(n²) on a leading mask + absent literal.
      * the previous oracle held the CANDIDATE fixed at one unit while only the
        visible side grew, so it measured fixed-pattern scaling and passed even
        the quadratic implementation — it was not revert-sensitive.

    This probe grows the candidate AND the visible text together on a near-match
    that aligns for a long prefix before failing, which is the worst case for an
    `O(v × m)` aligner. Asserting the growth SHAPE keeps it portable across
    machines; the ratio ceiling cleanly separates linear (~2×) from quadratic
    (~4×). Measured: this implementation 1.9–2.2× per doubling, the previous
    per-offset implementation 3.6–3.8× (fails this gate).
    """
    import time
    import api.streaming as streaming

    match = streaming._redaction_tolerant_match

    def probe(n: int) -> float:
        # Leading mask + a literal that never appears, over co-growing input.
        cand = "***" + " " + "k=v " * n + "ABSENTLITERAL"
        vis = ("sk-abc123" + " " + "k=v " * n) * 2
        best = float("inf")
        # Best-of-3 to damp scheduler noise; only the growth shape matters.
        for _ in range(3):
            t0 = time.perf_counter()
            match(cand, vis, anchor_end=False)
            best = min(best, time.perf_counter() - t0)
        return best

    sizes = [75, 150, 300, 600]
    times = {n: probe(n) for n in sizes}
    ratios = [times[sizes[i + 1]] / max(times[sizes[i]], 1e-9) for i in range(len(sizes) - 1)]

    assert max(ratios) < 2.8, (
        "redaction matcher work must stay sub-quadratic when candidate and visible "
        f"text grow together; per-doubling ratios were {['%.2f' % r for r in ratios]} "
        f"(times={ {n: round(t * 1000, 3) for n, t in times.items()} } ms)"
    )

    # Repeated-literal variant: every candidate literal occurs at many visible
    # offsets, so start-offset pruning cannot help and the aligner does real work.
    def probe_repeated(n: int) -> float:
        cand = '"***" ' + "a " * n + "ZZ"
        vis = '"s" ' + "a " * (2 * n)
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            match(cand, vis, anchor_end=False)
            best = min(best, time.perf_counter() - t0)
        return best

    rep_sizes = [100, 200, 400, 800]
    rep_times = {n: probe_repeated(n) for n in rep_sizes}
    rep_ratios = [
        rep_times[rep_sizes[i + 1]] / max(rep_times[rep_sizes[i]], 1e-9)
        for i in range(len(rep_sizes) - 1)
    ]
    assert max(rep_ratios) < 2.8, (
        "repeated-literal co-growing near-match must stay sub-quadratic; "
        f"ratios were {['%.2f' % r for r in rep_ratios]}"
    )


def test_redaction_tolerant_match_hard_bounds_fail_closed():
    """The synchronous path must be hard-bounded, and every bound must fail CLOSED.

    Because alignment is `O(visible × candidate)` in the worst case, both inputs
    are capped and a total unit-comparison budget is enforced. Exceeding a bound
    returns False ("not an echo"), which is the safe direction: a false negative
    only re-appends a progress paragraph (cosmetic), while a false positive marks
    genuinely-new progress as already_streamed and destroys it.

    This also pins the documented tradeoff — oversized inputs are deliberately
    NOT deduped rather than scanned unboundedly on the streaming callback.
    """
    import time
    import api.streaming as streaming

    match = streaming._redaction_tolerant_match

    # Over-cap candidate and visible inputs must be rejected outright.
    over_cand = "***" + "x" * (streaming._ECHO_REDACTION_MAX_CAND_CHARS + 1)
    assert not match(over_cand, "y" * 100, anchor_end=False)
    over_vis = 'a "s" b' + "z" * (streaming._ECHO_REDACTION_MAX_VIS_CHARS + 1)
    assert not match('a "***" b', over_vis, anchor_end=False)

    # A mask run may not be expanded an unbounded number of times per field.
    assert not match("***" * (streaming._ECHO_REDACTION_MAX_MASKS_PER_FIELD + 4) + "z", "z", anchor_end=True)

    # At the very hard bounds the call must still return quickly enough to be safe
    # inline on the streaming callback. Generous ceiling for slow/loaded CI.
    worst_cand = '"***" ' + "a " * 2040
    worst_vis = '"s" ' + "a " * 32000
    t0 = time.perf_counter()
    match(worst_cand, worst_vis, anchor_end=False)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, (
        "a worst-case at-the-bound match must not stall the synchronous streaming "
        f"callback; took {elapsed * 1000:.1f}ms"
    )



@pytest.mark.parametrize(
    "secret",
    [
        "koreader1",
        # Third review: a real secret may contain characters the lexer treats as
        # structural. The context-free lexer split these into extra units so the
        # 1:1 alignment rejected the echo and the duplication bug survived for
        # exactly these values. Both must reach already_streamed=True and replay
        # to a single copy.
        "ab;cd|ef,gh ij",
    ],
    ids=["plain-secret", "delimiter-bearing-secret"],
)
def test_redacted_interim_progress_echo_marks_already_streamed(secret, cleanup_test_sessions):
    """Regression: a credential-redacted interim echo must be flagged already_streamed.

    Reproduces the "repetition on navigate-back" bug: the assistant streams a
    progress sentence containing a secret as normal tokens (unredacted), then
    reports the same sentence through interim_assistant (credential-redacted).
    Because the redaction rewrote the secret, the strict echo check missed the
    match and flagged already_streamed=False, so run-journal replay appended the
    paragraph a second time. With the redaction-tolerant echo check the interim
    must be flagged already_streamed=True, and replaying the run journal must
    yield exactly one copy of the sentence.
    """
    import api.streaming as streaming
    from api.models import _append_journaled_partial_output

    sentence_streamed = (
        'There\'s a legacy plaintext `password` fallback field, so I can seed '
        'with `{"username":"sam","password":"' + secret + '","serverUrl":"...",'
        '"matchMethod":1}` and it\'ll load. Let me build the simulator and seed the file.'
    )
    sentence_interim = sentence_streamed.replace(secret, "***")
    # Unique per-parameter ids so the two runs cannot share journal/stream state.
    secret_slug = re.sub(r"[^a-z0-9]+", "_", secret.lower()).strip("_")

    class FakeSession:
        def __init__(self):
            self.session_id = "issue_redacted_interim_echo_" + secret_slug
            self.title = "Redacted interim echo"
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

    class RedactedInterimAgent:
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
            self.interim_assistant_callback = cast(Callable[..., None], interim_assistant_callback)
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
            # Live prose streams the real secret through tokens...
            self.stream_delta_callback(sentence_streamed)
            # ...then the same sentence is reported as an interim progress echo,
            # but credential-redacted. The runtime does NOT pre-set
            # already_streamed here (default False); the SSE bridge must infer it.
            self.interim_assistant_callback(sentence_interim)
            history = kwargs.get("conversation_history", [])
            return {"messages": history + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": sentence_streamed},
            ]}

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = "stream_issue_redacted_interim_echo_" + secret_slug
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
             mock.patch.object(streaming, "_get_ai_agent", return_value=RedactedInterimAgent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("gpt-test", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[fake_stream_id] = fake_queue
            streaming._run_agent_streaming(
                session_id=fake_session.session_id,
                msg_text="seed creds",
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
    assert interim, "interim_assistant event must be emitted"
    assert interim[-1].get("already_streamed") is True, (
        "a credential-redacted interim echo of already-streamed tokens must be "
        "flagged already_streamed so it is not appended a second time"
    )

    # Replaying the run journal must yield exactly ONE copy of the sentence — the
    # navigate-back reconstruction path (api.models._append_journaled_partial_output).
    replay_session = FakeSession()
    _append_journaled_partial_output(replay_session, fake_stream_id)
    replay_text = "\n".join(
        str(m.get("content") or "")
        for m in replay_session.messages
        if m.get("role") == "assistant"
    )
    assert replay_text.count("legacy plaintext") == 1, (
        "run-journal replay must not duplicate the redacted progress sentence; "
        f"found {replay_text.count('legacy plaintext')} copies"
    )
