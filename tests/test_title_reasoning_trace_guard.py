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
