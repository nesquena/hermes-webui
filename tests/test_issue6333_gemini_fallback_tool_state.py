"""Regression coverage for #6333 Gemini fallback request-boundary replay repair."""
import copy
import pathlib
import queue
import sys
import types
from unittest.mock import MagicMock

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


GOOGLE_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _mark_boundary(messages, index):
    from api.streaming import _GEMINI_REQUEST_BOUNDARY_MARKER

    marked = copy.deepcopy(messages)
    marked[index] = dict(marked[index])
    marked[index][_GEMINI_REQUEST_BOUNDARY_MARKER] = True
    return marked


def _boundary_marker_key():
    from api.streaming import _GEMINI_REQUEST_BOUNDARY_MARKER

    return _GEMINI_REQUEST_BOUNDARY_MARKER


def _boundary_wrapper_type():
    from api.streaming import _GeminiRequestBoundaryMessage

    return _GeminiRequestBoundaryMessage


def _assert_no_materialized_boundary_marker(messages):
    marker = _boundary_marker_key()
    assert not any(
        marker in message
        for message in messages
        if isinstance(message, dict)
    )


def _assert_no_boundary_wrapper_instances(messages):
    wrapper_type = _boundary_wrapper_type()
    assert not any(
        isinstance(message, wrapper_type)
        for message in messages
        if isinstance(message, dict)
    )


def _assert_no_boundary_artifacts(messages):
    _assert_no_materialized_boundary_marker(messages)
    _assert_no_boundary_wrapper_instances(messages)


def _import_real_conversation_loop():
    original_agent_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "agent" or name.startswith("agent.")
    }

    for name, module in list(original_agent_modules.items()):
        if name != "agent" and not name.startswith("agent."):
            continue
        if isinstance(module, types.ModuleType) and getattr(module, "__file__", None) is None:
            sys.modules.pop(name, None)

    def _restore_agent_modules():
        for name in list(sys.modules):
            if (name == "agent" or name.startswith("agent.")) and name not in original_agent_modules:
                sys.modules.pop(name, None)
        for name, module in original_agent_modules.items():
            sys.modules[name] = module

    try:
        conversation_loop = pytest.importorskip(
            "agent.conversation_loop",
            reason="hermes-agent conversation loop not importable",
        )
    except BaseException:
        _restore_agent_modules()
        raise

    agent_pkg = sys.modules.get("agent")
    if isinstance(agent_pkg, types.ModuleType):
        agent_pkg.conversation_loop = conversation_loop  # type: ignore[attr-defined]
    return conversation_loop, _restore_agent_modules


def _repair(messages, *, model, base_url, boundary_index):
    from api.streaming import _repair_google_gemini_current_turn_tool_state_for_request

    return _repair_google_gemini_current_turn_tool_state_for_request(
        _mark_boundary(messages, boundary_index),
        model=model,
        base_url=base_url,
    )


def _prior_and_current_turn_history():
    return [
        {"role": "user", "content": "look up the first order"},
        {
            "role": "assistant",
            "content": "Found it.",
            "tool_calls": [
                {
                    "id": "call_prior",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_prior",
            "name": "lookup",
            "content": "order 1: shipped",
        },
        {"role": "user", "content": "now check the next one"},
        {
            "role": "assistant",
            "content": "Checking the next order.",
            "tool_calls": [
                {
                    "id": "call_current",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"2"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_current",
            "name": "lookup",
            "content": "order 2: pending",
        },
    ]


def test_gemini3_request_repairs_only_current_turn_and_leaves_input_untouched():
    messages = _prior_and_current_turn_history()
    original = copy.deepcopy(messages)

    result = _repair(
        messages,
        model="gemini-3-flash",
        base_url=GOOGLE_OPENAI_BASE,
        boundary_index=3,
    )

    assert messages == original, "request-boundary repair must not mutate canonical history"

    prior_assistant = result[1]
    assert prior_assistant.get("tool_calls"), "prior completed turns must stay intact"
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_prior"
        for m in result
    ), "prior completed tool rows must stay intact"

    current_assistant = next(
        m for m in result
        if m.get("role") == "assistant" and m.get("content") == "Checking the next order."
    )
    assert "tool_calls" not in current_assistant, "current-turn unsigned Gemini 3 tool state must be stripped from the request copy"
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_current"
        for m in result
    ), "linked current-turn tool rows must be removed with the stripped group"


def test_gemini3_parallel_group_keeps_unsigned_siblings_when_first_call_is_signed():
    messages = [
        {"role": "user", "content": "check both orders"},
        {
            "role": "assistant",
            "content": "Checking both.",
            "tool_calls": [
                {
                    "id": "call_signed",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                    "extra_content": {"google": {"thought_signature": "sig-1"}},
                },
                {
                    "id": "call_unsigned",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"2"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_signed",
            "name": "lookup",
            "content": "order 1: shipped",
        },
        {
            "role": "tool",
            "tool_call_id": "call_unsigned",
            "name": "lookup",
            "content": "order 2: pending",
        },
    ]

    result = _repair(
        messages,
        model="gemini-3-flash",
        base_url=GOOGLE_OPENAI_BASE,
        boundary_index=0,
    )

    assistant = next(m for m in result if m.get("role") == "assistant")
    kept_ids = [tc.get("id") for tc in assistant.get("tool_calls") or []]
    assert kept_ids == ["call_signed", "call_unsigned"], (
        "a signed first Gemini call must keep the whole parallel group"
    )
    kept_tool_ids = [
        m.get("tool_call_id")
        for m in result
        if m.get("role") == "tool"
    ]
    assert kept_tool_ids == ["call_signed", "call_unsigned"]


def test_gemini25_request_does_not_prune_unsigned_current_turn_history():
    messages = _prior_and_current_turn_history()

    result = _repair(
        messages,
        model="gemini-2.5-flash",
        base_url=GOOGLE_OPENAI_BASE,
        boundary_index=3,
    )

    assert result == messages, "Gemini 2.5 is out of scope for the strict current-turn repair"


def test_non_google_route_does_not_prune_gemini_named_model_history():
    messages = _prior_and_current_turn_history()

    result = _repair(
        messages,
        model="gemini-3-flash",
        base_url="https://openrouter.ai/api/v1",
        boundary_index=3,
    )

    assert result == messages, "only Google Gemini requests should receive the request-boundary repair"


def test_gemini3_request_preserves_completed_tool_turn_before_next_user_message():
    messages = [
        {"role": "user", "content": "look up the first order"},
        {
            "role": "assistant",
            "content": "Found it.",
            "tool_calls": [{
                "id": "call_prior",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"id":"1"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_prior", "content": "order 1: shipped"},
        {"role": "user", "content": "Thanks, now check order 2."},
    ]
    original = copy.deepcopy(messages)

    result = _repair(
        messages,
        model="gemini-3-flash",
        base_url=GOOGLE_OPENAI_BASE,
        boundary_index=3,
    )

    assert messages == original, "request-boundary repair must not mutate canonical history"
    assert result == original, "a completed prior tool turn must survive a later user message intact"


@pytest.mark.parametrize(
    "base_url, provider_profile, expected",
    [
        ("HTTPS://GENERATIVELANGUAGE.GOOGLEAPIS.COM/v1beta/openai", None, True),
        ("https://custom.example/v1", types.SimpleNamespace(name="gemini"), True),
        ("https://generativelanguage.googleapis.com.example/v1", None, False),
        ("https://custom.example/generativelanguage.googleapis.com/v1", None, False),
        ("https://custom.example/v1?provider=google", None, False),
        ("https://custom.example/v1", types.SimpleNamespace(provider_id="google-compatible"), False),
    ],
)
def test_gemini3_route_identity_requires_exact_host_or_provider_id(
    base_url, provider_profile, expected
):
    from api.streaming import _request_targets_google_gemini_three

    assert _request_targets_google_gemini_three(
        "gemini-3-flash",
        base_url=base_url,
        provider_profile=provider_profile,
    ) is expected


def test_streaming_fallback_preserves_persisted_history_and_repairs_only_outbound_copy(
    tmp_path,
    monkeypatch,
):
    import api.config as config
    import api.models as models
    import api.oauth as oauth
    import api.session_lifecycle as lifecycle
    import api.streaming as streaming
    from api.models import Session
    conversation_loop, restore_agent_modules = _import_real_conversation_loop()
    ChatCompletionsTransport = pytest.importorskip(
        "agent.transports.chat_completions",
        reason="hermes-agent transport modules not importable",
    ).ChatCompletionsTransport
    run_agent = pytest.importorskip(
        "run_agent",
        reason="hermes-agent runner not importable",
    )

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    def _assistant_response(content, tool_calls=None, finish_reason="stop"):
        message = types.SimpleNamespace(content=content, tool_calls=tool_calls)
        choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
        return types.SimpleNamespace(choices=[choice], model="stub/model", usage=None)

    class _RateLimitError(Exception):
        status_code = 429

        def __init__(self):
            super().__init__("Error code: 429 - rate limit exceeded")
            self.response = types.SimpleNamespace(headers={})
            self.body = {"error": {"message": "rate limit exceeded"}}

    def _make_client(base_url, create_side_effect):
        create = MagicMock(side_effect=create_side_effect)
        return types.SimpleNamespace(
            api_key="synthetic-key",
            base_url=base_url,
            _custom_headers=None,
            default_headers=None,
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create)
            ),
        )

    primary_success_calls = []
    fallback_primary_calls = []
    writeback_primary_calls = []
    fallback_calls = []
    fallback_resolution_calls = []
    captured_results = {}
    tool_executions = []

    active_tool_call = types.SimpleNamespace(
        id="call_active",
        type="function",
        function=types.SimpleNamespace(
            name="lookup",
            arguments='{"id":"2"}',
        ),
    )

    def _primary_success_create(*_args, **kwargs):
        primary_success_calls.append(copy.deepcopy(kwargs))
        if len(primary_success_calls) == 1:
            return _assistant_response("Checking the next order.", [active_tool_call])
        return _assistant_response("primary stayed primary")

    def _fallback_primary_create(*_args, **kwargs):
        fallback_primary_calls.append(copy.deepcopy(kwargs))
        if len(fallback_primary_calls) == 1:
            return _assistant_response("Checking the next order.", [active_tool_call])
        if len(fallback_primary_calls) == 2:
            return _assistant_response("The order status is", finish_reason="length")
        raise _RateLimitError()

    def _fallback_create(*_args, **kwargs):
        fallback_calls.append(copy.deepcopy(kwargs))
        return _assistant_response("fallback recovered")

    def _writeback_primary_create(*_args, **kwargs):
        writeback_primary_calls.append(copy.deepcopy(kwargs))
        if len(writeback_primary_calls) == 1:
            return _assistant_response("Checking the next order.", [active_tool_call])
        return _assistant_response("primary stayed primary")

    scenarios = {
        "issue6333-primary": {
            "primary_client": _make_client(
                "https://integrate.api.nvidia.com/v1",
                _primary_success_create,
            ),
        },
        "issue6333-fallback": {
            "primary_client": _make_client(
                "https://integrate.api.nvidia.com/v1",
                _fallback_primary_create,
            ),
        },
        "issue6333-writeback": {
            "primary_client": _make_client(
                "https://integrate.api.nvidia.com/v1",
                _writeback_primary_create,
            ),
        },
    }
    fallback_client = _make_client(GOOGLE_OPENAI_BASE, _fallback_create)

    class HarnessAgent(run_agent.AIAgent):
        def __init__(self, **kwargs):
            kwargs.setdefault("skip_context_files", True)
            kwargs.setdefault("skip_memory", True)
            super().__init__(**kwargs)
            scenario = scenarios[kwargs["session_id"]]
            self.client = scenario["primary_client"]
            self._client_kwargs = {
                "api_key": scenario["primary_client"].api_key,
                "base_url": scenario["primary_client"].base_url,
            }
            self._cached_system_prompt = "You are helpful."
            self._use_prompt_caching = False
            self.tool_delay = 0
            self.compression_enabled = False
            self.save_trajectories = False
            self.valid_tool_names = {"lookup"}
            self._persist_session = lambda *args, **kwargs: None
            self._save_trajectory = lambda *args, **kwargs: None
            self._cleanup_task_resources = lambda *args, **kwargs: None

        def _create_request_openai_client(self, *, reason, api_kwargs=None):
            if self._fallback_activated or self.provider == "google":
                return fallback_client
            return scenarios[self.session_id]["primary_client"]

        def run_conversation(self, **kwargs):
            result = super().run_conversation(**kwargs)
            if self.session_id == "issue6333-writeback":
                dirty_result = copy.deepcopy(result)
                for message in dirty_result.get("messages") or []:
                    if (
                        isinstance(message, dict)
                        and message.get("role") == "user"
                        and str(message.get("content") or "").endswith("continue after dirty copied result")
                    ):
                        message[streaming._GEMINI_REQUEST_BOUNDARY_MARKER] = True
                        break
                result = dirty_result
            captured_results[self.session_id] = copy.deepcopy(result)
            return result

        def _execute_tool_calls(self, assistant_message, messages, effective_task_id, api_call_count=0):
            tool_row = {
                "role": "tool",
                "tool_call_id": assistant_message.tool_calls[0].id,
                "name": assistant_message.tool_calls[0].function.name,
                "content": "order 2: pending",
            }
            tool_executions.append(copy.deepcopy(tool_row))
            messages.append(tool_row)

    import hermes_cli.model_normalize as model_normalize
    import hermes_cli.runtime_provider as runtime_provider
    import hermes_state

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda requested=None, **_kw: {
            "provider": requested or "nvidia",
            "api_key": "synthetic-key",
            "base_url": "https://integrate.api.nvidia.com/v1",
        },
    )
    monkeypatch.setattr(
        model_normalize,
        "normalize_model_for_provider",
        lambda model, provider: model,
    )
    monkeypatch.setattr(hermes_state, "SessionDB", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda *args, **kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        run_agent,
        "OpenAI",
        lambda *args, **kwargs: _make_client(
            kwargs.get("base_url") or "https://stub.invalid/v1",
            lambda *_a, **_kw: _assistant_response("unused"),
        ),
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *args, **kwargs: 262144,
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "agent.auxiliary_client.resolve_provider_client",
        lambda provider, model=None, **kwargs: (
            fallback_resolution_calls.append(
                {
                    "provider": provider,
                    "model": model,
                    "base_url": kwargs.get("explicit_base_url"),
                }
            )
            or (fallback_client, model)
        ),
    )
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: (
            "glm-5.2",
            "nvidia",
            "https://integrate.api.nvidia.com/v1",
        ),
    )
    monkeypatch.setattr(streaming, "_maybe_schedule_title_refresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(streaming, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr(
        oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: HarnessAgent)
    monkeypatch.setattr(streaming, "get_config", lambda: {
        "fallback_providers": [
            {"provider": "google", "model": "gemini-3-flash"},
        ]
    })
    monkeypatch.setattr("api.config.get_config", lambda: {
        "fallback_providers": [
            {"provider": "google", "model": "gemini-3-flash"},
        ]
    })
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("api.config.load_settings", lambda: {})

    sessions = {}
    original_build_kwargs = ChatCompletionsTransport.build_kwargs
    original_build_turn_context = conversation_loop.build_turn_context
    original_reanchor = conversation_loop.reanchor_current_turn_user_idx
    original_flag = streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED
    sessions_snapshot = dict(models.SESSIONS)
    streams_snapshot = dict(config.STREAMS)
    cancel_flags_snapshot = dict(config.CANCEL_FLAGS)
    agent_instances_snapshot = dict(config.AGENT_INSTANCES)
    partial_text_snapshot = dict(config.STREAM_PARTIAL_TEXT)
    reasoning_text_snapshot = dict(config.STREAM_REASONING_TEXT)
    live_tool_calls_snapshot = dict(config.STREAM_LIVE_TOOL_CALLS)
    session_agent_locks_snapshot = dict(config.SESSION_AGENT_LOCKS)
    with config.SESSION_AGENT_CACHE_LOCK:
        session_agent_cache_snapshot = list(config.SESSION_AGENT_CACHE.items())
    with lifecycle._condition:
        lifecycle_sessions_snapshot = dict(lifecycle._sessions)

    def _new_session(session_id, stream_id, pending_user_message):
        session = Session(session_id=session_id, title="Gemini fallback")
        history = _prior_and_current_turn_history()[:3]
        session.messages = history
        session.context_messages = copy.deepcopy(history)
        session.pending_user_message = pending_user_message
        session.pending_attachments = []
        session.pending_started_at = 1234567890.0
        session.pending_user_source = "webui"
        session.active_stream_id = stream_id
        session.workspace = str(tmp_path)
        session.save()
        sessions[session_id] = session
        return session

    def _run_stream(session_id, stream_id, msg_text):
        session = _new_session(session_id, stream_id, msg_text)
        models.SESSIONS[session_id] = session
        config.STREAMS[stream_id] = queue.Queue()
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text=msg_text,
            model="glm-5.2",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )
        return Session.load(session_id)

    def _assert_canonical_current_turn(messages, call_id="call_active"):
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == call_id
            for m in messages
        ), "canonical history must keep the current-turn tool row"
        assert any(
            tc.get("id") == call_id
            for m in messages
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        ), "canonical history must keep the unsigned current-turn tool call metadata"

    monkeypatch.setattr(streaming, "get_session", lambda session_id: sessions[session_id])

    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.STREAM_REASONING_TEXT.clear()
    config.STREAM_LIVE_TOOL_CALLS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()
    with lifecycle._condition:
        lifecycle._sessions.clear()
        lifecycle._condition.notify_all()

    try:
        primary_saved = _run_stream(
            session_id="issue6333-primary",
            stream_id="stream-6333-primary",
            msg_text="continue without fallback",
        )
        fallback_saved = _run_stream(
            session_id="issue6333-fallback",
            stream_id="stream-6333-fallback",
            msg_text="continue with fallback",
        )
        writeback_saved = _run_stream(
            session_id="issue6333-writeback",
            stream_id="stream-6333-writeback",
            msg_text="continue after dirty copied result",
        )
    finally:
        restore_agent_modules()
        ChatCompletionsTransport.build_kwargs = original_build_kwargs
        conversation_loop.build_turn_context = original_build_turn_context
        conversation_loop.reanchor_current_turn_user_idx = original_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = original_flag
        models.SESSIONS.clear()
        models.SESSIONS.update(sessions_snapshot)
        config.STREAMS.clear()
        config.STREAMS.update(streams_snapshot)
        config.CANCEL_FLAGS.clear()
        config.CANCEL_FLAGS.update(cancel_flags_snapshot)
        config.AGENT_INSTANCES.clear()
        config.AGENT_INSTANCES.update(agent_instances_snapshot)
        config.STREAM_PARTIAL_TEXT.clear()
        config.STREAM_PARTIAL_TEXT.update(partial_text_snapshot)
        config.STREAM_REASONING_TEXT.clear()
        config.STREAM_REASONING_TEXT.update(reasoning_text_snapshot)
        config.STREAM_LIVE_TOOL_CALLS.clear()
        config.STREAM_LIVE_TOOL_CALLS.update(live_tool_calls_snapshot)
        config.SESSION_AGENT_LOCKS.clear()
        config.SESSION_AGENT_LOCKS.update(session_agent_locks_snapshot)
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.clear()
            config.SESSION_AGENT_CACHE.update(session_agent_cache_snapshot)
        with lifecycle._condition:
            lifecycle._sessions.clear()
            lifecycle._sessions.update(lifecycle_sessions_snapshot)
            lifecycle._condition.notify_all()

    assert len(primary_success_calls) == 2
    primary_first_messages = primary_success_calls[0]["messages"]
    fallback_first_messages = fallback_primary_calls[0]["messages"]
    for first_messages, user_text in (
        (primary_first_messages, "continue without fallback"),
        (fallback_first_messages, "continue with fallback"),
    ):
        assert first_messages[0]["role"] == "system"
        assert first_messages[1:-1] == _prior_and_current_turn_history()[:3]
        assert first_messages[-1]["role"] == "user"
        assert first_messages[-1]["content"].endswith(user_text)
    expected_tool_row = {
        "role": "tool",
        "tool_call_id": "call_active",
        "name": "lookup",
        "content": "order 2: pending",
    }
    assert tool_executions == [expected_tool_row, expected_tool_row, expected_tool_row]
    primary_messages = primary_success_calls[1]["messages"]
    _assert_canonical_current_turn(primary_messages)
    primary_result = captured_results["issue6333-primary"]
    _assert_canonical_current_turn(primary_result["messages"])
    _assert_no_materialized_boundary_marker(primary_result["messages"])
    assert primary_saved is not None
    _assert_canonical_current_turn(primary_saved.context_messages)
    _assert_canonical_current_turn(primary_saved.messages)
    _assert_no_materialized_boundary_marker(primary_saved.context_messages)
    _assert_no_materialized_boundary_marker(primary_saved.messages)
    assert primary_result["messages"][-1]["content"] == "primary stayed primary"
    assert primary_saved.messages[-1]["content"] == "primary stayed primary"

    assert len(fallback_primary_calls) == 3
    assert fallback_first_messages[-1]["role"] == "user"
    assert fallback_first_messages[-1]["content"].endswith("continue with fallback")
    assert len(fallback_calls) == 1
    assert len(fallback_resolution_calls) == 1
    assert fallback_resolution_calls[0]["provider"] == "google"
    assert fallback_resolution_calls[0]["model"] == "gemini-3-flash"

    fallback_primary_messages = fallback_primary_calls[2]["messages"]
    _assert_canonical_current_turn(fallback_primary_messages)
    assert any(
        message.get("role") == "user"
        and "truncated by the output length limit" in message.get("content", "")
        for message in fallback_primary_messages
    ), "the fallback request must follow the real synthetic continuation user row"

    fallback_result = captured_results["issue6333-fallback"]
    _assert_canonical_current_turn(fallback_result["messages"])
    _assert_no_materialized_boundary_marker(fallback_result["messages"])
    fallback_messages = fallback_calls[0]["messages"]
    current_assistant = next(
        m
        for m in fallback_messages
        if m.get("role") == "assistant"
        and m.get("content") == "Checking the next order."
    )
    assert "tool_calls" not in current_assistant
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_active"
        for m in fallback_messages
    ), "activated Gemini fallback must repair only its outbound request copy"

    assert fallback_saved is not None
    _assert_canonical_current_turn(fallback_saved.context_messages)
    _assert_canonical_current_turn(fallback_saved.messages)
    _assert_no_materialized_boundary_marker(fallback_saved.context_messages)
    _assert_no_materialized_boundary_marker(fallback_saved.messages)
    assert fallback_result["messages"][-1]["content"] == "fallback recovered"
    assert fallback_saved.messages[-1]["content"] == "fallback recovered"
    writeback_result = captured_results["issue6333-writeback"]
    assert any(
        _boundary_marker_key() in message
        for message in writeback_result["messages"]
        if isinstance(message, dict)
    ), "the integration must inject a dirty copied marker or the writeback assertion is vacuous"
    assert writeback_saved is not None
    _assert_canonical_current_turn(writeback_saved.context_messages)
    _assert_canonical_current_turn(writeback_saved.messages)
    _assert_no_materialized_boundary_marker(writeback_saved.context_messages)
    _assert_no_materialized_boundary_marker(writeback_saved.messages)

    from api.streaming import _GEMINI_REQUEST_BOUNDARY_MARKER
    for call in primary_success_calls + fallback_primary_calls + fallback_calls:
        assert not any(
            _GEMINI_REQUEST_BOUNDARY_MARKER in message
            for message in call["messages"]
            if isinstance(message, dict)
        )


def test_transport_wrapper_repairs_real_chat_transport_and_preserves_primary_history():
    from api.streaming import _install_gemini_request_boundary_wrapper
    import api.streaming as streaming
    conversation_loop, restore_agent_modules = _import_real_conversation_loop()
    get_transport = pytest.importorskip(
        "agent.transports",
        reason="hermes-agent transport modules not importable",
    ).get_transport
    ChatCompletionsTransport = pytest.importorskip(
        "agent.transports.chat_completions",
        reason="hermes-agent transport modules not importable",
    ).ChatCompletionsTransport

    original_flag = streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED
    original_build_kwargs = ChatCompletionsTransport.build_kwargs
    original_build_turn_context = conversation_loop.build_turn_context
    original_reanchor = conversation_loop.reanchor_current_turn_user_idx
    baseline_build_kwargs = getattr(
        original_build_kwargs,
        "_webui_original_build_kwargs",
        original_build_kwargs,
    )
    try:
        ChatCompletionsTransport.build_kwargs = baseline_build_kwargs
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = False

        _install_gemini_request_boundary_wrapper()
        transport = get_transport("chat_completions")
        assert transport is not None

        messages = _mark_boundary(_prior_and_current_turn_history(), 3)
        marked_original = copy.deepcopy(messages)
        original = [dict(message) for message in messages]
        original[3].pop(_boundary_marker_key(), None)

        primary_kwargs = transport.build_kwargs(
            model="glm-5.2",
            messages=messages,
            tools=None,
            base_url="https://integrate.api.nvidia.com/v1",
            provider_preferences={
                "fallback_providers": [
                    {"provider": "google", "model": "gemini-3-flash"},
                ]
            },
        )
        assert primary_kwargs["messages"] == original
        assert messages[3].get(_boundary_marker_key()) is True
        assert messages == marked_original

        fallback_kwargs = transport.build_kwargs(
            model="gemini-3-flash",
            messages=messages,
            tools=None,
            base_url=GOOGLE_OPENAI_BASE,
        )

        result = fallback_kwargs["messages"]
        assert messages == marked_original, "the transport wrapper must repair only the outbound request copy"
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_prior"
            for m in result
        ), "prior completed turns must stay intact through the wrapped build path"
        assert not any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_current"
            for m in result
        ), "the wrapped build path must strip the current-turn unsigned group before Gemini sees it"
    finally:
        restore_agent_modules()
        ChatCompletionsTransport.build_kwargs = original_build_kwargs
        conversation_loop.build_turn_context = original_build_turn_context
        conversation_loop.reanchor_current_turn_user_idx = original_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = original_flag


def test_boundary_wrapper_reanchors_rebuilt_message_list_and_strips_marker():
    import api.streaming as streaming
    from api.streaming import (
        _GeminiRequestBoundaryMessage,
        _install_gemini_request_boundary_wrapper,
    )
    conversation_loop, restore_agent_modules = _import_real_conversation_loop()

    original_reanchor = conversation_loop.reanchor_current_turn_user_idx
    original_flag = streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED
    baseline_reanchor = getattr(
        original_reanchor,
        "_webui_original_reanchor",
        original_reanchor,
    )
    try:
        conversation_loop.reanchor_current_turn_user_idx = baseline_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = False
        _install_gemini_request_boundary_wrapper()

        rebuilt = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "summary"},
            _GeminiRequestBoundaryMessage({"role": "user", "content": "current"}).copy(),
        ]
        assert rebuilt[2].get(streaming._GEMINI_REQUEST_BOUNDARY_MARKER) is True
        assert conversation_loop.reanchor_current_turn_user_idx(rebuilt, "current") == 2
        _assert_no_materialized_boundary_marker(rebuilt)
        outbound = rebuilt[2].copy()
        assert outbound.pop(streaming._GEMINI_REQUEST_BOUNDARY_MARKER) is True
        assert rebuilt[2] == {"role": "user", "content": "current"}
    finally:
        restore_agent_modules()
        conversation_loop.reanchor_current_turn_user_idx = original_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = original_flag


def test_transport_wrapper_rewraps_replaced_real_chat_transport():
    from api.streaming import _install_gemini_request_boundary_wrapper
    import api.streaming as streaming
    conversation_loop, restore_agent_modules = _import_real_conversation_loop()
    ChatCompletionsTransport = pytest.importorskip(
        "agent.transports.chat_completions",
        reason="hermes-agent transport modules not importable",
    ).ChatCompletionsTransport

    original_flag = streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED
    original_build_kwargs = ChatCompletionsTransport.build_kwargs
    original_build_turn_context = conversation_loop.build_turn_context
    original_reanchor = conversation_loop.reanchor_current_turn_user_idx
    baseline_build_kwargs = getattr(
        original_build_kwargs,
        "_webui_original_build_kwargs",
        original_build_kwargs,
    )
    baseline_build_turn_context = getattr(
        original_build_turn_context,
        "_webui_original_build_turn_context",
        original_build_turn_context,
    )
    baseline_reanchor = getattr(
        original_reanchor,
        "_webui_original_reanchor",
        original_reanchor,
    )
    try:
        ChatCompletionsTransport.build_kwargs = baseline_build_kwargs
        conversation_loop.build_turn_context = baseline_build_turn_context
        conversation_loop.reanchor_current_turn_user_idx = baseline_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = False

        _install_gemini_request_boundary_wrapper()
        first_wrapper = ChatCompletionsTransport.build_kwargs
        first_build_turn_context = conversation_loop.build_turn_context
        first_reanchor = conversation_loop.reanchor_current_turn_user_idx
        assert getattr(first_wrapper, "_webui_gemini_request_boundary_wrapper", False)
        assert getattr(first_build_turn_context, "_webui_gemini_request_boundary_wrapper", False)
        assert getattr(first_reanchor, "_webui_gemini_request_boundary_wrapper", False)

        def replacement_build_kwargs(self, model, messages, tools=None, **params):
            return {
                "model": model,
                "messages": messages,
                "tools": tools,
                **params,
            }

        ChatCompletionsTransport.build_kwargs = replacement_build_kwargs

        def replacement_build_turn_context(*args, **kwargs):
            return args, kwargs

        def replacement_reanchor(messages, user_message):
            return 0

        conversation_loop.build_turn_context = replacement_build_turn_context
        conversation_loop.reanchor_current_turn_user_idx = replacement_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = True

        _install_gemini_request_boundary_wrapper()
        rewrapped = ChatCompletionsTransport.build_kwargs
        assert getattr(rewrapped, "_webui_gemini_request_boundary_wrapper", False)
        assert getattr(rewrapped, "_webui_original_build_kwargs", None) is replacement_build_kwargs
        assert rewrapped is not replacement_build_kwargs
        rewrapped_build_turn_context = conversation_loop.build_turn_context
        rewrapped_reanchor = conversation_loop.reanchor_current_turn_user_idx
        assert getattr(
            rewrapped_build_turn_context,
            "_webui_original_build_turn_context",
            None,
        ) is replacement_build_turn_context
        assert getattr(rewrapped_reanchor, "_webui_original_reanchor", None) is replacement_reanchor
        assert rewrapped_build_turn_context is not replacement_build_turn_context
        assert rewrapped_reanchor is not replacement_reanchor

        _install_gemini_request_boundary_wrapper()
        assert ChatCompletionsTransport.build_kwargs is rewrapped
        assert conversation_loop.build_turn_context is rewrapped_build_turn_context
        assert conversation_loop.reanchor_current_turn_user_idx is rewrapped_reanchor

        messages = _mark_boundary(_prior_and_current_turn_history(), 3)
        kwargs = ChatCompletionsTransport().build_kwargs(
            model="gemini-3-flash",
            messages=messages,
            tools=None,
            base_url=GOOGLE_OPENAI_BASE,
        )
        result = kwargs["messages"]
        assert not any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_current"
            for m in result
        ), "reinstall must re-wrap a replaced transport before Gemini sees the request"
        malformed = [{
            "role": "user",
            "content": "malformed boundary",
            streaming._GEMINI_REQUEST_BOUNDARY_MARKER: "invalid",
        }]
        delegated = ChatCompletionsTransport().build_kwargs(
            model="glm-5.2",
            messages=malformed,
            tools=None,
            base_url="https://integrate.api.nvidia.com/v1",
        )
        assert streaming._GEMINI_REQUEST_BOUNDARY_MARKER not in delegated["messages"][0]
    finally:
        restore_agent_modules()
        ChatCompletionsTransport.build_kwargs = original_build_kwargs
        conversation_loop.build_turn_context = original_build_turn_context
        conversation_loop.reanchor_current_turn_user_idx = original_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = original_flag


def test_build_turn_wrapper_cleans_stale_marker_before_second_turn_and_keeps_single_boundary(
    tmp_path,
    monkeypatch,
):
    from api.streaming import (
        _GeminiRequestBoundaryMessage,
        _install_gemini_request_boundary_wrapper,
    )
    import api.models as models
    import api.streaming as streaming
    from api.models import Session
    conversation_loop, restore_agent_modules = _import_real_conversation_loop()
    ChatCompletionsTransport = pytest.importorskip(
        "agent.transports.chat_completions",
        reason="hermes-agent transport modules not importable",
    ).ChatCompletionsTransport

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    original_flag = streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED
    original_build_kwargs = ChatCompletionsTransport.build_kwargs
    original_build_turn_context = conversation_loop.build_turn_context
    original_reanchor = conversation_loop.reanchor_current_turn_user_idx
    baseline_build_kwargs = getattr(
        original_build_kwargs,
        "_webui_original_build_kwargs",
        original_build_kwargs,
    )
    baseline_build_turn_context = getattr(
        original_build_turn_context,
        "_webui_original_build_turn_context",
        original_build_turn_context,
    )
    baseline_reanchor = getattr(
        original_reanchor,
        "_webui_original_reanchor",
        original_reanchor,
    )
    try:
        ChatCompletionsTransport.build_kwargs = baseline_build_kwargs
        conversation_loop.build_turn_context = baseline_build_turn_context
        conversation_loop.reanchor_current_turn_user_idx = baseline_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = False

        _install_gemini_request_boundary_wrapper()
        rebuilt_history = [
            {"role": "user", "content": "look up the first order"},
            {
                "role": "assistant",
                "content": "Found it.",
                "tool_calls": [{
                    "id": "call_prior",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_prior",
                "name": "lookup",
                "content": "order 1: shipped",
            },
            _GeminiRequestBoundaryMessage({"role": "user", "content": "now check the next one"}).copy(),
        ]
        assert rebuilt_history[3].get(streaming._GEMINI_REQUEST_BOUNDARY_MARKER) is True
        assert conversation_loop.reanchor_current_turn_user_idx(rebuilt_history, "now check the next one") == 3
        assert isinstance(rebuilt_history[3], _GeminiRequestBoundaryMessage)

        def replacement_build_turn_context(*args, **kwargs):
            return types.SimpleNamespace(
                messages=list(rebuilt_history) + [
                    {"role": "user", "content": "and then check the last one"},
                    {
                        "role": "assistant",
                        "content": "Checking the last order.",
                        "tool_calls": [{
                            "id": "call_current",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id":"3"}'},
                        }],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_current",
                        "name": "lookup",
                        "content": "order 3: pending",
                    },
                ],
                current_turn_user_idx=4,
            )

        conversation_loop.build_turn_context = replacement_build_turn_context
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = False
        _install_gemini_request_boundary_wrapper()

        ctx = conversation_loop.build_turn_context()
        _assert_no_materialized_boundary_marker(ctx.messages)
        assert sum(
            1
            for message in ctx.messages
            if isinstance(message, _GeminiRequestBoundaryMessage)
        ) == 1
        assert isinstance(ctx.messages[4], _GeminiRequestBoundaryMessage)

        request_copies = [
            message.copy()
            for message in ctx.messages
            if isinstance(message, dict)
        ]
        boundary_count = sum(
            1
            for message in request_copies
            if message.get(streaming._GEMINI_REQUEST_BOUNDARY_MARKER) is True
        )
        assert boundary_count == 1

        fallback_kwargs = ChatCompletionsTransport().build_kwargs(
            model="gemini-3-flash",
            messages=request_copies,
            tools=None,
            base_url=GOOGLE_OPENAI_BASE,
        )
        result = fallback_kwargs["messages"]
        _assert_no_materialized_boundary_marker(ctx.messages)
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_prior"
            for m in result
        )
        assert not any(
            m.get("role") == "tool" and m.get("tool_call_id") == "call_current"
            for m in result
        ), "the rebuilt-history second turn must still repair the current-turn unsigned group"

        canonical_ctx_messages = copy.deepcopy(ctx.messages)
        streaming._sanitize_materialized_gemini_request_boundary_history(canonical_ctx_messages)
        _assert_no_boundary_artifacts(canonical_ctx_messages)
        _assert_no_boundary_artifacts(result)

        session = Session(session_id="issue6333-second-turn-history", title="Gemini fallback")
        session.messages = copy.deepcopy(result)
        session.context_messages = copy.deepcopy(canonical_ctx_messages)
        session.save()

        saved = Session.load("issue6333-second-turn-history")
        assert saved is not None
        _assert_no_boundary_artifacts(saved.messages)
        _assert_no_boundary_artifacts(saved.context_messages)
    finally:
        restore_agent_modules()
        ChatCompletionsTransport.build_kwargs = original_build_kwargs
        conversation_loop.build_turn_context = original_build_turn_context
        conversation_loop.reanchor_current_turn_user_idx = original_reanchor
        streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = original_flag


def test_compression_copied_boundary_marker_is_stripped_before_session_save(
    tmp_path,
    monkeypatch,
):
    import api.models as models
    import api.streaming as streaming
    from api.models import Session
    from api.streaming import _GeminiRequestBoundaryMessage

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    canonical_messages = [
        {"role": "user", "content": "look up the first order"},
        _GeminiRequestBoundaryMessage({"role": "user", "content": "now check the next one"}),
        {
            "role": "assistant",
            "content": "Checking the next order.",
            "tool_calls": [{
                "id": "call_current",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"id":"2"}'},
            }],
        },
    ]
    compression_copied_history = [
        message.copy()
        for message in canonical_messages
        if isinstance(message, dict)
    ]
    assert compression_copied_history[1].get(_boundary_marker_key()) is True

    streaming._sanitize_materialized_gemini_request_boundary_history(
        compression_copied_history
    )
    _assert_no_boundary_artifacts(compression_copied_history)

    session = Session(session_id="issue6333-compression-copy", title="Gemini fallback")
    session.messages = copy.deepcopy(compression_copied_history)
    session.context_messages = copy.deepcopy(compression_copied_history)
    session.save()

    saved = Session.load("issue6333-compression-copy")
    assert saved is not None
    _assert_no_boundary_artifacts(saved.messages)
    _assert_no_boundary_artifacts(saved.context_messages)
