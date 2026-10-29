"""Agent titles must use the submitted prompt, not WebUI's model-facing context."""
from copy import deepcopy

import pytest

from api.streaming import _build_run_conversation_kwargs


def _build(callable_obj, prompt, model_message):
    return _build_run_conversation_kwargs(
        callable_obj,
        user_message=model_message,
        system_message="Workspace context",
        conversation_history=[],
        conversation_history_revision=None,
        task_id="title-contract-test",
        persist_user_message=prompt,
        persist_user_timestamp=123.0,
    )


@pytest.mark.parametrize("prompt", [
    "Check automatic skill maintenance",
    "[Workspace: user-authored] Explain this marker",
    "[Workspace::v1: user-authored] Explain this marker",
    "",
])
def test_explicit_title_input_is_the_exact_submitted_prompt(prompt):
    def current_agent(user_message, *, title_user_message=None, **kwargs):
        return user_message, title_user_message, kwargs["persist_user_message"]

    model_message = "[Workspace::v1: /workspace/project]\n" + prompt
    kwargs = _build(current_agent, prompt, model_message)

    assert current_agent(**kwargs) == (model_message, prompt, prompt)


def test_native_images_and_injected_notifications_do_not_pollute_title_input():
    def current_agent(user_message, *, title_user_message=None, **kwargs):
        return user_message, title_user_message

    prompt = "Find bed dimensions"
    model_message = [
        {"type": "text", "text": "[Workspace::v1: /workspace/project]\nInternal notification\n" + prompt},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}},
    ]
    original = deepcopy(model_message)
    kwargs = _build(current_agent, prompt, model_message)
    content, title_input = current_agent(**kwargs)

    assert title_input == prompt
    assert content is model_message
    assert model_message == original


def test_kwargs_capable_agent_receives_the_title_override():
    def flexible_agent(**kwargs):
        return kwargs

    kwargs = _build(flexible_agent, "Actual request", "Injected workspace context")
    assert flexible_agent(**kwargs)["title_user_message"] == "Actual request"


def test_explicit_only_signature_receives_supported_arguments():
    def current_agent(user_message, system_message, conversation_history, task_id,
                      persist_user_message, *, title_user_message=None):
        return title_user_message

    kwargs = _build(current_agent, "Actual request", "Injected workspace context")
    assert current_agent(**kwargs) == "Actual request"


def test_legacy_agent_without_title_override_remains_callable():
    def legacy_agent(user_message, system_message, conversation_history, task_id, persist_user_message):
        return user_message, persist_user_message

    kwargs = _build(legacy_agent, "Actual request", "Injected workspace context")
    assert "title_user_message" not in kwargs
    assert legacy_agent(**kwargs) == ("Injected workspace context", "Actual request")
