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

    # Two-mask semicolon variant — the middle field genuinely changed.
    assert not match("a=***;b=***;c=x", "a=s1;b=y;b=s2;c=x", anchor_end=False)

    # Legitimate redaction-only differences across the same delimiters dedup.
    assert match("token=***;status=ok", "token=sk-abc123;status=ok", anchor_end=True)
    assert match("token=***|status=ok", "token=sk-abc123|status=ok", anchor_end=False)
    assert match("api_key: ***", "api_key: sk-abc123", anchor_end=True)
    assert match("api_key: ***", "api_key: sk-abc123", anchor_end=False)


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


def test_redaction_tolerant_match_growth_is_subquadratic():
    """CORE regression: the matcher must be linear, not quadratic — revert-sensitive.

    The `anchor_end=False` call in `_is_visible_output_echo` scans the full,
    unbounded visible output synchronously. The earlier implementations restarted
    a `str.find`/suffix scan at every offset, which is Θ(n²) on an adversarial
    leading-mask + absent-literal input (reviewer measured a 16.9× time increase
    for an 8× input). The tokenized single-pass matcher is linear.

    This asserts the SHAPE (growth ratio), not a wall-clock ceiling, so it is
    portable across machines AND revert-sensitive: the quadratic segment/regex
    implementation grows ~3.4–4.7× per input doubling and fails this gate, while
    the linear tokenizer grows ~2× and passes.
    """
    import time
    import api.streaming as streaming

    match = streaming._redaction_tolerant_match

    # Adversarial: a leading mask whose field literal never appears, over many
    # tiny visible tokens — the worst case for a per-offset restart.
    cand = "***zzzABSENT"

    def probe(n: int) -> float:
        vis = " ".join(["x"] * n)
        best = float("inf")
        # Best-of-3 to damp scheduler noise; we only care about growth shape.
        for _ in range(3):
            t0 = time.perf_counter()
            for _ in range(10):
                match(cand, vis, anchor_end=False)
            best = min(best, (time.perf_counter() - t0) / 10)
        return best

    sizes = [4000, 8000, 16000, 32000]
    times = {n: probe(n) for n in sizes}
    ratios = [times[sizes[i + 1]] / max(times[sizes[i]], 1e-9) for i in range(len(sizes) - 1)]

    # Linear ⇒ each doubling ≈ 2×. Quadratic ⇒ ≈ 4×. A 2.8× ceiling cleanly
    # separates the two and leaves generous headroom for CI jitter.
    assert max(ratios) < 2.8, (
        "redaction matcher growth must be sub-quadratic on the unbounded "
        f"substring path; per-doubling ratios were {['%.2f' % r for r in ratios]} "
        f"(times={ {n: round(t * 1000, 3) for n, t in times.items()} } ms)"
    )



def test_redacted_interim_progress_echo_marks_already_streamed(cleanup_test_sessions):
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

    secret = "koreader1"
    sentence_streamed = (
        'There\'s a legacy plaintext `password` fallback field, so I can seed '
        'with `{"username":"sam","password":"' + secret + '","serverUrl":"...",'
        '"matchMethod":1}` and it\'ll load. Let me build the simulator and seed the file.'
    )
    sentence_interim = sentence_streamed.replace(secret, "***")

    class FakeSession:
        def __init__(self):
            self.session_id = "issue_redacted_interim_echo"
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
    fake_stream_id = "stream_issue_redacted_interim_echo"
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
