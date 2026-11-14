"""A Git-message request must not combine another profile's URL with its key."""

from types import SimpleNamespace
import sys

import pytest

from tests.test_issue1806_named_custom_provider_resolution import (
    _RouteAgentCaptured,
    _setup_route_consumer_runtime,
)


@pytest.mark.parametrize("auxiliary", [True, False])
def test_generic_custom_git_message_uses_runtime_endpoint_and_key_together(monkeypatch, auxiliary):
    import api.routes as routes

    captured, session = _setup_route_consumer_runtime(
        monkeypatch,
        cfg_dict={"model": {
            "default": "shared-model", "provider": "custom",
            "base_url": "https://profile-b.invalid/v1",
        }},
        runtime_dict={"provider": "custom", "base_url": "https://profile-a.invalid/v1", "api_key": "dummy-a-key"},
    )
    session.model = "shared-model"
    session.model_provider = "custom"
    sent = {}

    def get_client(task, main_runtime=None):
        sent.update(main_runtime or {})
        if not auxiliary:
            return None, None

        def create(**kwargs):
            sent["messages"] = kwargs["messages"]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="A message"))])

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "shared-model"

    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", SimpleNamespace(get_text_auxiliary_client=get_client))
    if auxiliary:
        assert routes._llm_git_commit_message("system", "profile-a-only-marker", session=session) == "A message"
        assert sent["messages"][1]["content"] == "profile-a-only-marker"
    else:
        with pytest.raises(_RouteAgentCaptured):
            routes._llm_git_commit_message("system", "profile-a-only-marker", session=session)
        assert captured["init_kwargs"]["base_url"] == "https://profile-a.invalid/v1"
        assert captured["init_kwargs"]["api_key"] == "dummy-a-key"
    assert sent["base_url"] == "https://profile-a.invalid/v1"
    assert sent["api_key"] == "dummy-a-key"


def test_generic_custom_runtime_failure_refuses_stale_endpoint(monkeypatch):
    import api.oauth as oauth
    import api.routes as routes

    captured, session = _setup_route_consumer_runtime(
        monkeypatch,
        cfg_dict={"model": {"default": "shared-model", "provider": "custom", "base_url": "https://profile-b.invalid/v1"}},
    )
    session.model = "shared-model"
    session.model_provider = "custom"

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("dummy runtime unavailable")

    monkeypatch.setattr(oauth, "resolve_runtime_provider_with_anthropic_env_lock", unavailable)
    with pytest.raises(ValueError, match="active profile's custom provider"):
        routes._llm_git_commit_message("system", "profile-a-only-marker", session=session)
    assert "init_kwargs" not in captured
