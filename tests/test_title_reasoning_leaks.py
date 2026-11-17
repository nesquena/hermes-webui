"""Session titles from open-weight models that leak reasoning into the reply.

Covers the strict ``{"title"}`` schema request on both routes, unwrapping a
bare JSON title reply, and the fresh-candidate rules that reject reasoning
sentences, leftover thinking tags, payload fragments and overlong replies.
Saved titles are not affected by these rules.
"""
import copy
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from api.streaming import (
    _TITLE_RESPONSE_FORMAT,
    _looks_invalid_generated_title,
    _title_structured_output_rejected,
    _sanitize_generated_title,
    _send_title_request,
    generate_title_raw_via_agent,
    generate_title_raw_via_aux,
)
from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config


@pytest.mark.parametrize("raw, expected", [
    ('{"title": "Docker Volume Permissions"}', "Docker Volume Permissions"),
    ('```json\n{"title": "Docker Volume Permissions"}\n```', "Docker Volume Permissions"),
    ('{"title": "Docker Volume Permissions", "reason": "short"}', "Docker Volume Permissions"),
    ('<think>They want a title.</think>{"title": "Docker Volume Permissions"}', "Docker Volume Permissions"),
    ("<thinking>\nShort topic label.\n</thinking>\nDocker Volume Permissions", "Docker Volume Permissions"),
    ("<reasoning>pick one</reasoning> Docker Volume Permissions", "Docker Volume Permissions"),
])
def test_extracts_title_from_json_and_thinking_wrappers(raw, expected):
    assert _sanitize_generated_title(raw) == expected


@pytest.mark.parametrize("raw", [
    "Maybe something like Docker Volume Permissions",
    "Perhaps: Docker Volume Permissions",
    "I think a good title would be Docker Volume Permissions",
    "I'd go with Docker Volume Permissions",
    "I’d suggest Docker Volume Permissions",
    "Hmm, how about Docker Volume Permissions?",
    "Let's go with Docker Volume Permissions",
    "Something like \"Docker Volume Permissions\"",
    "A good title: Docker Volume Permissions",
    "The best title is Docker Volume Permissions",
    "Title should be 3-8 words, so Docker Volume Permissions",
    "**Maybe** Docker Volume Permissions",
])
def test_rejects_reasoning_sentences(raw):
    assert _sanitize_generated_title(raw) == ""


@pytest.mark.parametrize("raw", [
    "<think>The user wants a title for Docker volumes so I will",  # unclosed
    "Docker Volume Permissions</think>",
    'Okay here it is {"title": "Docker Volume Permissions"}',
    '{"name": "Docker Volume Permissions"}',
])
def test_rejects_payload_fragments_and_tags(raw):
    assert _sanitize_generated_title(raw) == ""


@pytest.mark.parametrize("title", [
    "Docker Volume Permissions",
    "[Bug] Login Fails On Submit",
    "Fix [1, 2] Parsing Bug",
    "Analysis: Q3 Sales Trends",
    "Maybelline Product Comparison",
    "Python dict lookups",
    # Vietnamese spaces syllables, so long titles have many tokens (#9129 gate).
    "Sửa lỗi phân quyền thư mục khi dùng Docker volume trên máy chủ Linux",
    "Sửa lỗi phân quyền Docker volume trên Linux",
])
def test_keeps_ordinary_titles(title):
    assert _sanitize_generated_title(title) == title


def test_saved_titles_are_not_judged_by_fresh_rules():
    # Self-heal of persisted titles keeps master's rules only.
    assert not _looks_invalid_generated_title("Maybe We Should Talk Podcast")
    assert not _looks_invalid_generated_title("What About Bob Film Review")


def _response(content):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop",
    )])


def test_aux_route_requests_title_schema():
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return _response('{"title": "Docker Volume Permissions"}')

    with auxiliary_client_modules(), patch_tg_config({"provider": "ollama", "model": "qwen3"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
            raw, status = generate_title_raw_via_aux("How do I fix volume perms?", "Use chown.")

    assert captured["extra_body"]["response_format"] == _TITLE_RESPONSE_FORMAT
    assert status == "llm_aux"
    assert _sanitize_generated_title(raw) == "Docker Volume Permissions"


class _StatusError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(f"Error code: {status_code} - {message}" if message else f"HTTP {status_code}")
        self.status_code = status_code


# Real endpoint wording: a refusal of the schema field, and errors that removing
# the schema cannot repair.
_SCHEMA_REJECTED = "{'error': {'message': \"Unsupported parameter: 'response_format'\"}}"
_CONTEXT_TOO_LONG = "{'error': {'message': 'maximum context length exceeded'}}"
_MESSAGES_INVALID = "{'error': {'message': 'messages failed validation'}}"


@pytest.mark.parametrize("status, message, rejected", [
    (400, _SCHEMA_REJECTED, True),
    (422, "Extra inputs are not permitted: response_format", True),
    (400, "response_format.json_schema: str type expected", True),
    (400, "response_format is unavailable for this model", True),
    (400, "compile_grammar_error: xgrammar could not build the grammar", True),
    (400, "output_config: Extra inputs are not permitted", True),
    (400, _CONTEXT_TOO_LONG, False),
    (422, _MESSAGES_INVALID, False),
    (400, "", False),
    (400, "model gpt-x is not supported", False),
    (429, _SCHEMA_REJECTED, False),
    (500, _SCHEMA_REJECTED, False),
])
def test_only_schema_refusals_count_as_structured_output_rejections(status, message, rejected):
    assert _title_structured_output_rejected(_StatusError(status, message)) is rejected


def _agent(create, model):
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "custom"
    agent.model = model
    agent.base_url = "http://localhost:1234/v1"
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {"model": model, "messages": []}
    agent._ensure_primary_openai_client.return_value = client
    return agent


def test_agent_route_requests_title_schema():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return _response('{"title": "Docker Volume Permissions"}')

    raw, status = generate_title_raw_via_agent(_agent(create, "schema-ok"), "q", "a")
    assert status == "llm"
    assert raw == '{"title": "Docker Volume Permissions"}'
    assert calls[0]["response_format"] == _TITLE_RESPONSE_FORMAT


def test_agent_route_resends_without_rejected_schema():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise _StatusError(400, _SCHEMA_REJECTED)
        return _response("Docker Volume Permissions")

    agent = _agent(create, "no-schema")
    assert generate_title_raw_via_agent(agent, "q", "a") == ("Docker Volume Permissions", "llm")
    assert ["response_format" in c for c in calls] == [True, False]

    # Nothing is cached: the next title tries the schema again.
    calls.clear()
    assert generate_title_raw_via_agent(agent, "q", "a") == ("Docker Volume Permissions", "llm")
    assert ["response_format" in c for c in calls] == [True, False]


def test_agent_route_does_not_resend_on_other_errors():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise _StatusError(429)

    raw, status = generate_title_raw_via_agent(_agent(create, "rate-limited"), "q", "a")
    assert raw is None and status == "llm_error"
    # One schema call per prompt; a rate limit never triggers the compatibility resend.
    assert all("response_format" in c for c in calls)


def test_aux_route_resends_without_rejected_schema():
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise _StatusError(422, "Extra inputs are not permitted: response_format")
        return _response("Docker Volume Permissions")

    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "old-server"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
            raw, status = generate_title_raw_via_aux("How do I fix volume perms?", "Use chown.")

    assert (raw, status) == ("Docker Volume Permissions", "llm_aux")
    assert ["response_format" in (c.get("extra_body") or {}) for c in calls] == [True, False]


@pytest.mark.parametrize("status, message", [(400, _CONTEXT_TOO_LONG), (422, _MESSAGES_INVALID)])
def test_agent_route_does_not_resend_on_unrelated_bad_requests(status, message):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise _StatusError(status, message)

    raw, status_text = generate_title_raw_via_agent(_agent(create, "bad-request"), "q", "a")
    assert raw is None and status_text == "llm_error"
    # One schema call per prompt; removing the schema cannot fix these errors.
    assert calls and all("response_format" in c for c in calls)


@pytest.mark.parametrize("status, message", [(400, _CONTEXT_TOO_LONG), (422, _MESSAGES_INVALID)])
def test_aux_route_does_not_resend_on_unrelated_bad_requests(status, message):
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        raise _StatusError(status, message)

    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "old-server"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
            raw, status_text = generate_title_raw_via_aux("How do I fix volume perms?", "Use chown.")

    assert raw is None and status_text == "llm_error_aux"
    assert calls and all("response_format" in (c.get("extra_body") or {}) for c in calls)


def _sdk_error(status, message):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    body = {"error": {"message": message, "type": "invalid_request_error"}}
    response = httpx.Response(status, json=body, request=request)
    cls = openai.BadRequestError if status == 400 else openai.UnprocessableEntityError
    return cls(f"Error code: {status} - {body}", response=response, body=body)


@pytest.mark.parametrize("status, message, rejected", [
    (400, "Unsupported parameter: 'response_format'", True),
    (400, "maximum context length exceeded", False),
    (422, "messages failed validation", False),
])
def test_openai_sdk_errors_are_classified_by_their_message(status, message, rejected):
    assert _title_structured_output_rejected(_sdk_error(status, message)) is rejected


@pytest.mark.parametrize("status, message, expected_schema_flags, expected_status", [
    (400, "Unsupported parameter: 'response_format'", [True, False], "llm"),
    (400, "maximum context length exceeded", None, "llm_error"),
    (422, "messages failed validation", None, "llm_error"),
])
def test_agent_route_over_the_openai_sdk_resends_only_schema_refusals(
    status, message, expected_schema_flags, expected_status,
):
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    import json as _json

    seen = []

    def handler(request):
        payload = _json.loads(request.content)
        seen.append("response_format" in payload)
        # The endpoint refuses every request, except that a schema refusal is
        # answered once the schema is gone.
        if "response_format" in payload or "response_format" not in message:
            return httpx.Response(status, json={"error": {"message": message, "type": "invalid_request_error"}})
        return httpx.Response(200, json={
            "id": "t", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Docker Volume Permissions"}}],
        })

    client = openai.OpenAI(
        api_key="test-key", base_url="http://localhost:1234/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    raw, status_text = generate_title_raw_via_agent(_agent(client.chat.completions.create, "m"), "q", "a")

    assert status_text == expected_status
    if expected_schema_flags is not None:
        assert raw == "Docker Volume Permissions"
        assert seen == expected_schema_flags
    else:
        assert raw is None
        # Every request kept the schema: no compatibility resend for these errors.
        assert seen and all(seen)


@pytest.mark.parametrize("raw", [
    "```" + " " * 2000 + "x",                    # unclosed fence, under the unwrap limit
    "```json\n" + " " * 3000 + "{",              # unclosed fence, over the unwrap limit
    "```json " + " " * 2000 + "{\"title\": \"x\"}",  # closing fence missing
], ids=["unclosed-under-limit", "unclosed-over-limit", "no-closing-fence"])
def test_unclosed_fence_with_long_whitespace_is_handled_in_linear_time(raw):
    # A reply cut off at max_tokens can open a fence and never close it.
    started = time.perf_counter()
    _sanitize_generated_title(raw)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("raw", [
    '{"title": ' + "[" * 2030,                    # deep nesting under the unwrap limit
    '{"a":' * 1000 + "1" + "}" * 1000,             # deep nesting over the unwrap limit
], ids=["array-under-limit", "object-over-limit"])
def test_deeply_nested_json_reply_is_discarded_without_raising(raw):
    assert _sanitize_generated_title(raw) == ""


def test_title_schema_is_copied_for_each_send():
    pristine = copy.deepcopy(_TITLE_RESPONSE_FORMAT)
    seen = []

    def send(response_format):
        seen.append(copy.deepcopy(response_format))
        # An adapter that edits the request it is given.
        response_format["json_schema"]["strict"] = False
        response_format["json_schema"]["schema"]["properties"].clear()
        response_format.pop("type")
        return "sent"

    try:
        assert _send_title_request(send) == "sent"
        assert _send_title_request(send) == "sent"
        assert seen == [pristine, pristine]
        assert _TITLE_RESPONSE_FORMAT == pristine
    finally:
        _TITLE_RESPONSE_FORMAT.clear()
        _TITLE_RESPONSE_FORMAT.update(copy.deepcopy(pristine))


@pytest.mark.parametrize("raw", [
    "!" * 20000 + "x",
    '{"title": "Docker", "reason": "' + "!" * 20000 + 'x"}',
], ids=["plain", "json-over-unwrap-limit"])
def test_long_punctuation_run_is_handled_in_linear_time(raw):
    started = time.perf_counter()
    _sanitize_generated_title(raw)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("raw", ["pong", "Thanks!", "ok...", "Done?! ", "yes \n"])
def test_single_word_echo_replies_are_still_discarded(raw):
    assert _sanitize_generated_title(raw) == ""
