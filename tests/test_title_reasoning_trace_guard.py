"""Regression coverage for reasoning-trace leakage in WebUI session titles."""

from contextlib import nullcontext
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

import api.streaming as streaming
from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config


BAD_TITLE_OUTPUTS = (
    "Title should be 3-8 words, matching the user's language (English), topic label s…",
    'Something like "Fix Session Title Generation" or "Audit Session Title Generation…',
    'Good title: "Budget 2-Stage Snow Blower Recommendations" or "Cheapest Well-Revie…',
    'Options: - "Durable Bathroom Flooring Options" - "Cost-Effective Bathroom Floor…',
    "The title should be 3-8 words, matching the user's language (English), as a topi",
    'Something like "HOA Docs Review and Filing" or "File HOA Documents in Sub...',
    'Something like "House Value Projection to 2038" or "Home Appre...',
    "The title should be concise",
)

EXPECTED_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "session_title",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
    },
}


def _response(content):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]
    )


@pytest.mark.parametrize("candidate", BAD_TITLE_OUTPUTS)
def test_sanitizer_rejects_observed_reasoning_trace_titles(candidate):
    assert streaming._sanitize_generated_title(candidate) == ""


@pytest.mark.parametrize(
    "candidate",
    (
        "Fix session title generation",
        "Reparar botón de inicio móvil",
        "移动端登录按钮修复",
    ),
)
def test_sanitizer_preserves_valid_titles_in_multiple_languages(candidate):
    assert streaming._sanitize_generated_title(candidate) == candidate


@pytest.mark.parametrize(
    "content",
    (
        '{"title": "Fix login button on mobile"}',
        '```json\n{"title": "Fix login button on mobile"}\n```',
        'Here is the requested JSON:\n{"title": "Fix login button on mobile"}\nDone.',
    ),
)
def test_extract_title_response_parses_json_wrappers(content):
    assert streaming._extract_title_response(_response(content)) == (
        "Fix login button on mobile",
        "",
    )


def test_extract_title_response_parses_top_level_json_string():
    assert streaming._extract_title_response(_response('"Fix login button on mobile"')) == (
        "Fix login button on mobile",
        "",
    )


def test_sanitizer_rejects_quoted_alternatives_and_bulleted_options():
    assert streaming._sanitize_generated_title('"Fix title generation" or "Audit title generation"') == ""
    assert streaming._sanitize_generated_title('- "Fix title generation"\n- "Audit title generation"') == ""


def test_sanitizer_rejects_more_than_twelve_words_instead_of_truncating():
    candidate = "Investigate why the mobile login button stops responding after users rotate their devices twice"
    assert len(candidate.split()) > 12
    assert streaming._sanitize_generated_title(candidate) == ""


def test_overlong_title_response_uses_local_fallback(monkeypatch):
    candidate = "Investigate why the mobile login button stops responding after users rotate their devices twice"
    session = types.SimpleNamespace(
        session_id="title-reasoning-fallback",
        title="Untitled",
        llm_title_generated=False,
        manual_title=False,
        messages=[
            {"role": "user", "content": "Fix the mobile login button."},
            {"role": "assistant", "content": "I will inspect the mobile event handling."},
        ],
        save=MagicMock(),
    )
    events = []

    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(
        streaming,
        "generate_title_raw_via_aux",
        lambda *_args, **_kwargs: (candidate, "llm_aux"),
    )
    monkeypatch.setattr(
        "api.profiles.profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )

    streaming._run_background_title_update(
        session_id=session.session_id,
        user_text="Fix the mobile login button.",
        assistant_text="I will inspect the mobile event handling.",
        placeholder_title="Untitled",
        put_event=lambda name, data: events.append((name, data)),
        agent=None,
    )

    assert session.title == "Fix mobile login button"
    assert session.save.call_args.kwargs == {"touch_updated_at": False}
    status = [data for name, data in events if name == "title_status"]
    assert status[-1]["status"] == "fallback"
    assert status[-1]["reason"] == "local_summary:llm_invalid_aux"


def test_aux_title_request_uses_strict_json_schema():
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return _response('{"title": "Fix login button on mobile"}')

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "openai", "model": "gpt-4o-mini"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Why is login broken on mobile?",
                    "The click handler is not attached.",
                )

    assert result == "Fix login button on mobile"
    assert status == "llm_aux"
    assert captured["extra_body"]["response_format"] == EXPECTED_RESPONSE_FORMAT


def test_aux_schema_request_does_not_send_reasoning_extension():
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        extra = kwargs.get("extra_body") or {}
        if "reasoning" in extra:
            raise ValueError("strict gateway rejects reasoning")
        return _response('{"title": "Schema Works"}')

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "strict-gateway"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a strict schema route.",
                    "The route supports response_format.",
                )

    assert result == "Schema Works"
    assert status == "llm_aux"
    assert len(calls) == 1
    assert "reasoning" not in calls[0]["extra_body"]


def test_aux_schema_rejection_falls_back_to_existing_request_shape():
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise ValueError("response_format unsupported")
        return _response("Compatibility Fallback Title")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "legacy-gateway"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a legacy title route.",
                    "The route rejects response_format.",
                )

    assert result == "Compatibility Fallback Title"
    assert status == "llm_aux_retry"
    assert len(calls) == 2
    assert calls[1]["extra_body"] == {"reasoning": {"enabled": False}}


def test_aux_schema_reasoning_only_retries_with_reasoning_disabled_shape():
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            return {
                "choices": [
                    {
                        "message": {"content": "", "reasoning": "hidden reasoning"},
                        "finish_reason": "length",
                    }
                ]
            }
        return _response("Compatibility Reasoning Fallback")

    with auxiliary_client_modules():
        with patch_tg_config({"provider": "custom", "model": "reasoning-gateway"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Use a reasoning title route.",
                    "The schema request returns reasoning only.",
                )

    assert result == "Compatibility Reasoning Fallback"
    assert status == "llm_aux_retry"
    assert len(calls) == 2
    assert calls[1]["extra_body"] == {"reasoning": {"enabled": False}}


def test_extract_title_response_rejects_json_without_string_title():
    assert streaming._extract_title_response(_response('{"options": ["one", "two"]}')) == (
        "",
        "llm_empty",
    )


def test_agent_openai_title_request_uses_strict_json_schema():
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _response('{"title": "Fix login button on mobile"}')

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "openai"
    agent.model = "gpt-4o-mini"
    agent.base_url = "https://api.openai.com/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {}
    agent._ensure_primary_openai_client.return_value = client

    result, status = streaming.generate_title_raw_via_agent(
        agent,
        "Why is login broken on mobile?",
        "The click handler is not attached.",
    )

    assert result == "Fix login button on mobile"
    assert status == "llm"
    assert captured["extra_body"]["response_format"] == EXPECTED_RESPONSE_FORMAT
    assert agent.reasoning_config is None


def test_agent_schema_rejection_falls_back_to_existing_request_shape():
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise ValueError("response_format unsupported")
        return _response("Compatibility Agent Title")

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "minimax"
    agent.model = "minimax-m2"
    agent.base_url = "https://api.minimaxi.com/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {}
    agent._ensure_primary_openai_client.return_value = client

    result, status = streaming.generate_title_raw_via_agent(
        agent,
        "Why is the title route failing?",
        "The endpoint rejects response_format.",
    )

    assert result == "Compatibility Agent Title"
    assert status == "llm_retry"
    assert len(calls) == 2
    assert calls[1]["extra_body"] == {"reasoning_split": True}
    assert agent.reasoning_config is None


@pytest.mark.parametrize(
    "schema_response",
    (
        _response(""),
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning": "hidden reasoning"},
                    "finish_reason": "length",
                }
            ]
        },
    ),
)
def test_agent_empty_schema_response_tries_compatibility_shape(schema_response):
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return schema_response
        return _response("Compatibility After Empty Schema")

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create))
    )
    agent = MagicMock()
    agent.api_mode = "openai"
    agent.provider = "custom"
    agent.model = "reasoning-gateway"
    agent.base_url = "https://reasoning.example/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.side_effect = lambda messages: {
        "messages": messages,
        "extra_body": {"reasoning_effort": "none"},
    }
    agent._ensure_primary_openai_client.return_value = client

    result, status = streaming.generate_title_raw_via_agent(
        agent,
        "Why is the schema response empty?",
        "Compatibility mode can still return a title.",
    )

    assert result == "Compatibility After Empty Schema"
    assert status == "llm_retry"
    assert len(calls) == 2
    assert calls[1]["messages"] == calls[0]["messages"]
    assert calls[1]["max_tokens"] == calls[0]["max_tokens"]
    assert calls[1]["extra_body"] == {"reasoning_effort": "none"}
    assert agent.reasoning_config is None


LEGITIMATE_PERSISTED_TITLES = (
    "Topic Label Accessibility",
    "Maybe Monad Error Handling",
    "Understanding 3-8 Words in Regex",
    "The Good Title Debate",
    "A Good Title for Your Novel",
    "Something Like Summer Discussion",
    '"Merge" or "Rebase" in Git',
    "Parsing <analysis> Tags",
)

SCREENSHOT_PERSISTED_TRACES = (
    "The title should be 3-8 words, matching the user's language (English), as a topi",
    'Something like "HOA Docs Review and Filing" or "File HOA Documents in Sub...',
    "The title should be concise",
)

ADVERSARIAL_TRACE_TITLES = (
    "<think >secret</think> Safe Title",
    "<analysis>We need to inspect this</analysis> Safe Title",
    "<|channel|>analysis We need to inspect this",
    "Analysis: We need to fix login button",
)


@pytest.mark.parametrize("candidate", LEGITIMATE_PERSISTED_TITLES)
def test_persisted_title_check_accepts_ordinary_subject_matter(candidate):
    assert streaming._looks_invalid_generated_title(candidate) is False
    session = types.SimpleNamespace(
        title=candidate,
        llm_title_generated=True,
        messages=[
            {"role": "user", "content": "Explain the topic."},
            {"role": "assistant", "content": "Here is the explanation."},
        ],
    )
    assert streaming._background_title_generation_inputs(session) is None


@pytest.mark.parametrize(
    "candidate",
    (
        "Something Like Summer Discussion",
        '"Merge" or "Rebase" in Git',
        "Parsing <analysis> Tags",
    ),
)
def test_persisted_subject_matter_survives_normal_send(candidate, monkeypatch):
    session = types.SimpleNamespace(
        session_id="title-false-positive-persist",
        title=candidate,
        llm_title_generated=True,
        manual_title=False,
        messages=[
            {"role": "user", "content": "Explain the topic."},
            {"role": "assistant", "content": "Here is the explanation."},
        ],
        save=MagicMock(),
    )
    events = []
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(
        streaming,
        "_generate_llm_session_title_via_aux",
        lambda *_args, **_kwargs: ("Silent Rename Victim", "llm_aux", "Silent Rename Victim"),
    )
    monkeypatch.setattr(
        "api.profiles.profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )

    streaming._run_background_title_update(
        session_id=session.session_id,
        user_text="Explain the topic.",
        assistant_text="Here is the explanation.",
        placeholder_title="Untitled",
        put_event=lambda name, data: events.append((name, data)),
        agent=None,
    )

    assert session.title == candidate
    session.save.assert_not_called()
    status = [data for name, data in events if name == "title_status"]
    assert status[-1]["status"] == "skipped"
    assert status[-1]["reason"] == "already_generated"


def test_new_candidates_still_reject_embedded_traces_and_quoted_alternatives():
    assert streaming._sanitize_generated_title("Parsing <analysis> Tags") == ""
    assert streaming._sanitize_generated_title('"Merge" or "Rebase" in Git') == ""
    assert streaming._looks_invalid_generated_title("Parsing <analysis> Tags") is False
    assert streaming._looks_invalid_generated_title('"Merge" or "Rebase" in Git') is False


@pytest.mark.parametrize("candidate", SCREENSHOT_PERSISTED_TRACES)
def test_persisted_title_check_self_heals_screenshot_traces(candidate):
    assert streaming._looks_invalid_generated_title(candidate) is True
    session = types.SimpleNamespace(
        title=candidate,
        llm_title_generated=True,
        messages=[
            {"role": "user", "content": "File the HOA documents."},
            {"role": "assistant", "content": "I will inspect the HOA packet."},
        ],
    )
    assert streaming._background_title_generation_inputs(session) is not None


@pytest.mark.parametrize("candidate", ADVERSARIAL_TRACE_TITLES)
def test_sanitizer_rejects_whitespace_and_provider_trace_wrappers(candidate):
    assert streaming._looks_invalid_generated_title(candidate) is True
    assert streaming._sanitize_generated_title(candidate) == ""


def test_schema_extract_accepts_only_json_object_with_string_title():
    assert streaming._extract_title_text(
        '{"title": "Fix login button on mobile"}',
        mode="schema",
    ) == "Fix login button on mobile"
    assert streaming._extract_title_text("Fix login button on mobile", mode="schema") == ""
    assert streaming._extract_title_text('"Fix login button on mobile"', mode="schema") == ""


@pytest.mark.parametrize(
    "content",
    (
        '{"foo": "bar',
        '{"title": "None"}',
        '{"title": "undefined"}',
        '{"title": "null"}',
    ),
)
def test_schema_extract_fails_closed_on_malformed_json_and_sentinels(content):
    assert streaming._extract_title_text(content, mode="schema") == ""
    assert streaming._extract_title_response(_response(content), mode="schema") == (
        "",
        "llm_empty",
    )


def test_compatibility_extract_still_accepts_prose_and_json_string():
    assert streaming._extract_title_response(
        _response("Fix login button on mobile"),
        mode="compatibility",
    ) == ("Fix login button on mobile", "")
    assert streaming._extract_title_response(
        _response('"Fix login button on mobile"'),
        mode="compatibility",
    ) == ("Fix login button on mobile", "")


def test_legitimate_existing_title_is_not_self_healed_on_background_update(monkeypatch):
    session = types.SimpleNamespace(
        session_id="keep-maybe-monad-title",
        title="Maybe Monad Error Handling",
        llm_title_generated=True,
        manual_title=False,
        messages=[
            {"role": "user", "content": "Explain maybe monads."},
            {"role": "assistant", "content": "A maybe monad models optional values."},
        ],
        save=MagicMock(),
    )
    events = []

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("title generation must not rerun for a legitimate persisted title")

    monkeypatch.setattr(streaming, "get_session", lambda _session_id: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: True)
    monkeypatch.setattr(streaming, "generate_title_raw_via_aux", fail_if_called)
    monkeypatch.setattr(
        "api.profiles.profile_env_for_background_worker",
        lambda *_args, **_kwargs: nullcontext(),
    )

    streaming._run_background_title_update(
        session_id=session.session_id,
        user_text="Explain maybe monads.",
        assistant_text="A maybe monad models optional values.",
        placeholder_title="Untitled",
        put_event=lambda name, data: events.append((name, data)),
        agent=None,
    )

    assert session.title == "Maybe Monad Error Handling"
    assert session.save.call_count == 0
    status = [data for name, data in events if name == "title_status"]
    assert status[-1]["status"] == "skipped"
    assert status[-1]["reason"] == "already_generated"


def test_legitimate_existing_title_survives_normal_turn(tmp_path, monkeypatch):
    import queue
    import sys

    import api.config as config
    import api.models as models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "keep-maybe-monad-send"
    stream_id = "stream-keep-maybe-monad"
    original_title = "Maybe Monad Error Handling"
    session = Session(
        session_id=sid,
        title=original_title,
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "Explain maybe monads."},
            {"role": "assistant", "content": "A maybe monad models optional values."},
        ],
        llm_title_generated=True,
        manual_title=False,
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Give another example."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[sid] = session
    streaming.SESSIONS[sid] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    class FakeAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            interim_assistant_callback=None,
            clarify_callback=None,
            **kwargs,
        ):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 10
            self.session_completion_tokens = 4
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("Option types are a maybe monad.")
            return {
                "completed": True,
                "final_response": "Option types are a maybe monad.",
                "messages": [
                    {"role": "user", "content": "Explain maybe monads."},
                    {"role": "assistant", "content": "A maybe monad models optional values."},
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "Option types are a maybe monad."},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    title_update_calls = []

    def track_title_update(*args, **kwargs):
        title_update_calls.append((args, kwargs))

    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_args, **_kwargs: ("gpt-4o", "openai", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_run_background_title_update", track_title_update)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text="Give another example.",
        model="gpt-4o",
        workspace=str(tmp_path),
        stream_id=stream_id,
    )

    assert session.title == original_title
    assert session.llm_title_generated is True
    assert streaming._background_title_generation_inputs(session) is None
    assert title_update_calls == []


def test_minimax_aux_schema_request_keeps_reasoning_split():
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return _response('{"title": "MiniMax Schema Title"}')

    with auxiliary_client_modules():
        with patch_tg_config(
            {
                "provider": "minimax",
                "model": "minimax-m2",
                "base_url": "https://api.minimaxi.com/v1",
            }
        ):
            with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
                result, status = streaming.generate_title_raw_via_aux(
                    "Why is login broken on mobile?",
                    "The click handler is not attached.",
                )

    assert result == "MiniMax Schema Title"
    assert status == "llm_aux"
    extra = captured["extra_body"]
    assert extra["response_format"] == EXPECTED_RESPONSE_FORMAT
    assert extra["reasoning_split"] is True
    assert "reasoning" not in extra
