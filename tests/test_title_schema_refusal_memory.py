"""The Agent title route remembers a structured-output schema refusal (#9154).

After a provider refuses the strict ``{"title"}`` schema with an HTTP
400/422 that names ``response_format`` as unsupported, the next title
request on the Agent's Chat Completions route must not send the schema
again: on DeepSeek's native API three titles cost six requests otherwise.
The memory is keyed by (provider, model), so a different model or provider
still gets the schema, and only genuine schema refusals are remembered.
"""
import threading
import types
from unittest.mock import MagicMock

import pytest

from api import streaming
from api.streaming import (
    _TITLE_RESPONSE_FORMAT,
    _send_title_request,
    generate_title_raw_via_agent,
)

# Real endpoint wording: a refusal of the schema field, and an error that
# removing the schema cannot repair.
_SCHEMA_REJECTED = "{'error': {'message': \"Unsupported parameter: 'response_format'\"}}"
_CONTEXT_TOO_LONG = "{'error': {'message': 'maximum context length exceeded'}}"


class _StatusError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(f"Error code: {status_code} - {message}" if message else f"HTTP {status_code}")
        self.status_code = status_code


def _response(content):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop",
    )])


@pytest.fixture(autouse=True)
def _clear_title_schema_refusals():
    """The refusal memory is module-level; isolate tests from each other."""
    with streaming._TITLE_SCHEMA_REFUSALS_LOCK:
        streaming._TITLE_SCHEMA_REFUSALS.clear()
    yield
    with streaming._TITLE_SCHEMA_REFUSALS_LOCK:
        streaming._TITLE_SCHEMA_REFUSALS.clear()


def _agent(create, provider, model, base_url="http://localhost:1234/v1"):
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {"model": model, "messages": []}
    agent._ensure_primary_openai_client.return_value = client
    return agent


def test_rejected_request_is_still_retried_once_without_the_schema():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise _StatusError(400, _SCHEMA_REJECTED)
        return _response("Docker Volume Permissions")

    raw, status = generate_title_raw_via_agent(_agent(create, "custom", "deepseek-chat"), "q", "a")
    assert (raw, status) == ("Docker Volume Permissions", "llm")
    # The refused request itself is resent once without the schema, unchanged.
    assert ["response_format" in c for c in calls] == [True, False]


def test_refusal_is_remembered_and_the_next_title_skips_the_schema():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise _StatusError(400, _SCHEMA_REJECTED)
        return _response("Docker Volume Permissions")

    agent = _agent(create, "custom", "deepseek-chat")
    assert generate_title_raw_via_agent(agent, "q", "a") == ("Docker Volume Permissions", "llm")
    assert ["response_format" in c for c in calls] == [True, False]
    assert any(key[:2] == ("custom", "deepseek-chat") for key in streaming._TITLE_SCHEMA_REFUSALS)

    # The next title on the same route does not send the schema again.
    calls.clear()
    assert generate_title_raw_via_agent(agent, "q", "a") == ("Docker Volume Permissions", "llm")
    assert ["response_format" in c for c in calls] == [False]
    assert calls[0].get("response_format") is None


def test_send_layer_memory_is_keyed_by_provider_and_model():
    sent = []

    def send(response_format):
        sent.append(response_format)
        if response_format is not None:
            raise _StatusError(400, _SCHEMA_REJECTED)
        return "sent"

    # Refused once: recorded, and the request is resent without the schema.
    assert _send_title_request(send, "custom", "deepseek-chat") == "sent"
    assert sent == [_TITLE_RESPONSE_FORMAT, None]
    sent.clear()
    # Same provider and model: no schema.
    assert _send_title_request(send, "custom", "deepseek-chat") == "sent"
    assert sent == [None]
    sent.clear()
    # A different model on the same provider still gets the schema.
    assert _send_title_request(send, "custom", "deepseek-reasoner") == "sent"
    assert sent == [_TITLE_RESPONSE_FORMAT, None]
    sent.clear()
    # A different provider with the remembered model still gets the schema.
    assert _send_title_request(send, "together", "deepseek-chat") == "sent"
    assert sent == [_TITLE_RESPONSE_FORMAT, None]


def test_context_length_error_is_not_remembered():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise _StatusError(400, _CONTEXT_TOO_LONG)

    agent = _agent(create, "custom", "ctx-limited")
    assert generate_title_raw_via_agent(agent, "q", "a") == (None, "llm_error")
    assert calls and all("response_format" in c for c in calls)
    assert not streaming._TITLE_SCHEMA_REFUSALS

    # The next title still sends the schema: this error keeps the schema.
    calls.clear()
    assert generate_title_raw_via_agent(agent, "q", "a") == (None, "llm_error")
    assert calls and all("response_format" in c for c in calls)


def test_concurrent_first_refusals_record_the_pair_once():
    sent = []
    lock = threading.Lock()

    def send(response_format):
        with lock:
            sent.append(response_format)
        if response_format is not None:
            raise _StatusError(400, _SCHEMA_REJECTED)
        return "sent"

    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(_send_title_request(send, "custom", "deepseek-chat"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["sent"] * 8
    assert ("custom", "deepseek-chat", None, None) in streaming._TITLE_SCHEMA_REFUSALS
    assert len(streaming._TITLE_SCHEMA_REFUSALS) == 1


@pytest.mark.parametrize("changes", ["endpoint", "profile"])
def test_title_schema_refusal_does_not_cross_route_boundaries(monkeypatch, changes):
    from api import config

    home = "/example/profile-a"
    monkeypatch.setattr(config._thread_ctx, "env", {"HERMES_HOME": home}, raising=False)
    calls = []

    def reject(**kwargs):
        if "response_format" in kwargs:
            raise _StatusError(400, _SCHEMA_REJECTED)
        return _response("First Endpoint")

    def accept(**kwargs):
        calls.append("response_format" in kwargs)
        return _response("Second Endpoint")

    first = _agent(reject, "custom", "same-model", "https://first.example.test/v1")
    next_url = "https://second.example.test/v1" if changes == "endpoint" else first.base_url
    second = _agent(accept, "custom", "same-model", next_url)
    assert generate_title_raw_via_agent(first, "q", "a") == ("First Endpoint", "llm")
    if changes == "profile":
        monkeypatch.setattr(config._thread_ctx, "env", {"HERMES_HOME": "/example/profile-b"})
    assert generate_title_raw_via_agent(second, "q", "a") == ("Second Endpoint", "llm")
    assert calls == [True]
