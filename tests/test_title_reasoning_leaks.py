"""Session titles from open-weight models that leak reasoning into the reply.

Covers the strict ``{"title"}`` schema request on both routes, unwrapping a
bare JSON title reply, and the fresh-candidate rules that reject reasoning
sentences, leftover thinking tags, payload fragments and overlong replies.
Saved titles are not affected by these rules.
"""
import types
from unittest.mock import MagicMock, patch

import pytest

import api.streaming as streaming
from api.streaming import (
    _TITLE_RESPONSE_FORMAT,
    _looks_invalid_generated_title,
    _sanitize_generated_title,
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
    "The conversation covers Docker volume permissions on Linux hosts and how to fix ownership",
])
def test_rejects_payload_fragments_tags_and_overlong_replies(raw):
    assert _sanitize_generated_title(raw) == ""


@pytest.mark.parametrize("title", [
    "Docker Volume Permissions",
    "[Bug] Login Fails On Submit",
    "Fix [1, 2] Parsing Bug",
    "Analysis: Q3 Sales Trends",
    "Maybelline Product Comparison",
    "Python dict lookups",
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
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


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


def test_agent_route_resends_without_rejected_schema_and_remembers(monkeypatch):
    monkeypatch.setattr(streaming, "_TITLE_SCHEMA_REJECTED_ROUTES", set())
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise _StatusError(400)
        return _response("Docker Volume Permissions")

    agent = _agent(create, "no-schema")
    assert generate_title_raw_via_agent(agent, "q", "a") == ("Docker Volume Permissions", "llm")
    assert ["response_format" in c for c in calls] == [True, False]

    calls.clear()
    assert generate_title_raw_via_agent(agent, "q", "a") == ("Docker Volume Permissions", "llm")
    assert ["response_format" in c for c in calls] == [False]


def test_agent_route_does_not_resend_on_other_errors(monkeypatch):
    monkeypatch.setattr(streaming, "_TITLE_SCHEMA_REJECTED_ROUTES", set())
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise _StatusError(429)

    raw, status = generate_title_raw_via_agent(_agent(create, "rate-limited"), "q", "a")
    assert raw is None and status == "llm_error"
    # One schema call per prompt; a rate limit never triggers the compatibility resend.
    assert all("response_format" in c for c in calls)
    assert streaming._TITLE_SCHEMA_REJECTED_ROUTES == set()


def test_aux_route_resends_without_rejected_schema(monkeypatch):
    monkeypatch.setattr(streaming, "_TITLE_SCHEMA_REJECTED_ROUTES", set())
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        if "response_format" in (kwargs.get("extra_body") or {}):
            raise _StatusError(422)
        return _response("Docker Volume Permissions")

    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "old-server"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm, create=True):
            raw, status = generate_title_raw_via_aux("How do I fix volume perms?", "Use chown.")

    assert (raw, status) == ("Docker Volume Permissions", "llm_aux")
    assert ["response_format" in (c.get("extra_body") or {}) for c in calls] == [True, False]
