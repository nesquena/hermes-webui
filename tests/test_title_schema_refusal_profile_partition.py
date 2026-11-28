"""Production-worker profile partition for the title schema-refusal memo (#9245).

The memo must be keyed by the *captured* owning-profile identity of the
session that owns the title request.  A background title worker runs under
``profile_env_for_background_worker``, which is a no-op for the default
profile, and the process-wide ``HERMES_HOME`` env mirror may at that moment
hold another profile's home.  The memo must never read that mirror as proof
of ownership: a negative capability decision recorded for profile A must not
change a default-profile title request.

This regression is written against the production worker scope and stays
green on frozen master (which has no memo), then goes red on the unfixed
integration because the memo key resolves to profile A's home through the
shared env mirror.
"""

import types
from unittest.mock import MagicMock

import pytest

from api import streaming

PROFILE_A_HOME = "/example/profile-a"
ENDPOINT = "https://shared.example.test/v1"
MODEL = "same-model"

_SCHEMA_REJECTED = "{'error': {'message': \"Unsupported parameter: 'response_format'\"}}"


class _StatusError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code


def _response(content):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=types.SimpleNamespace(content=content), finish_reason="stop",
    )])


def _agent(create):
    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
    )
    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "custom"
    agent.model = MODEL
    agent.base_url = ENDPOINT
    agent.reasoning_config = None
    agent._build_api_kwargs.return_value = {"model": MODEL, "messages": []}
    agent._ensure_primary_openai_client.return_value = client
    return agent


def _session(profile=None):
    session = MagicMock()
    session.profile = profile
    session.session_id = "s-9245-profile-partition"
    session.title = "Untitled"
    session.llm_title_generated = False
    session.messages = [
        {"role": "user", "content": "Explain the docker volume permission fix."},
        {"role": "assistant", "content": "Use a named volume with the right owner."},
    ]
    session.save = MagicMock()
    return session


@pytest.fixture(autouse=True)
def _clear_memo_when_present():
    """Isolate tests; frozen master has no memo attribute yet."""
    memo = getattr(streaming, "_TITLE_SCHEMA_REFUSALS", None)
    lock = getattr(streaming, "_TITLE_SCHEMA_REFUSALS_LOCK", None)
    if memo is not None and lock is not None:
        with lock:
            memo.clear()
    yield
    memo = getattr(streaming, "_TITLE_SCHEMA_REFUSALS", None)
    lock = getattr(streaming, "_TITLE_SCHEMA_REFUSALS_LOCK", None)
    if memo is not None and lock is not None:
        with lock:
            memo.clear()


def test_default_profile_worker_does_not_reuse_another_profiles_refusal(monkeypatch):
    """The default profile owns no refusal: its worker must send the schema.

    Reproduces the production sequence: a non-default profile worker mirrored
    its home into the process env, recorded a schema refusal, and exited; a
    default-profile title worker then runs without a pinned thread-local env.
    """
    from api import config

    # The process-wide mirror left behind by another profile's worker.
    monkeypatch.setenv("HERMES_HOME", PROFILE_A_HOME)
    # Default-profile workers do not pin thread-local env
    # (profile_env_for_background_worker is a no-op for "default").
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)

    # Profile A's negative capability decision is already remembered.
    memo = getattr(streaming, "_TITLE_SCHEMA_REFUSALS", None)
    if memo is not None:
        with streaming._TITLE_SCHEMA_REFUSALS_LOCK:
            memo.add(("custom", MODEL, ENDPOINT, PROFILE_A_HOME))

    calls = []

    def create(**kwargs):
        calls.append("response_format" in kwargs)
        return _response("Docker Volume Permissions")

    from api import profiles as profiles_api
    from api.streaming import _generate_llm_session_title_for_agent

    session = _session(profile=None)
    with profiles_api.profile_env_for_background_worker(session, "background title"):
        title, status, _ = _generate_llm_session_title_for_agent(
            _agent(create), "q", "a",
        )

    assert (title, status) == ("Docker Volume Permissions", "llm")
    # The default profile never refused the schema, so the schema must be sent.
    assert calls == [True]


@pytest.mark.skipif(
    getattr(streaming, "_TITLE_SCHEMA_REFUSALS", None) is None,
    reason="schema-refusal memo does not exist on the frozen master baseline",
)
def test_profile_worker_still_reuses_its_own_refusal(monkeypatch):
    """Control: profile A's own worker may reuse profile A's decision."""
    from api import config

    monkeypatch.setenv("HERMES_HOME", PROFILE_A_HOME)
    monkeypatch.setattr(config._thread_ctx, "env", {}, raising=False)

    calls = []

    def create(**kwargs):
        calls.append("response_format" in kwargs)
        if "response_format" in kwargs:
            raise _StatusError(400, _SCHEMA_REJECTED)
        return _response("Docker Volume Permissions")

    from api import profiles as profiles_api
    from api.streaming import _generate_llm_session_title_for_agent

    session = _session(profile="profile-a")
    with profiles_api.profile_env_for_background_worker(session, "background title"):
        first = _generate_llm_session_title_for_agent(_agent(create), "q", "a")
        calls.clear()
        second = _generate_llm_session_title_for_agent(_agent(create), "q", "a")

    assert first[0] == "Docker Volume Permissions"
    assert second[0] == "Docker Volume Permissions"
    # First attempt: schema then no-schema retry. Second title: own memo hit.
    assert calls == [False]
