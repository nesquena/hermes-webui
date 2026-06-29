"""Regression tests for issue #5121: provider auth failures must persist as terminal error turns."""

from __future__ import annotations

import queue
import sys
import types
from unittest import mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    if hasattr(config, "STREAM_REASONING_TEXT"):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, "STREAM_LIVE_TOOL_CALLS"):
        config.STREAM_LIVE_TOOL_CALLS.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    if hasattr(config, "STREAM_REASONING_TEXT"):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, "STREAM_LIVE_TOOL_CALLS"):
        config.STREAM_LIVE_TOOL_CALLS.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    config.SESSION_AGENT_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()


@pytest.fixture(autouse=True)
def _neutralize_credential_self_heal(monkeypatch):
    """Make the 401 self-heal path a deterministic no-op by default.

    The terminal-auth-failure tests assert that an unrecoverable 401 surfaces
    an ``apperror`` / persisted ``_error`` turn. The streaming settlement path
    first tries ``_attempt_credential_self_heal`` (#1401), which calls
    ``read_auth_json()``. On a host with a populated ``~/.hermes/auth.json``
    (e.g. a developer's real Hermes box) self-heal can succeed and silently
    retry the mock agent, swallowing the error the test expects — so the
    outcome would depend on host credentials. CI / Windows boxes have no such
    credentials, which is why the tests pass there but fail on a live agent
    host. Force self-heal off by default so every host exercises the
    unrecoverable-failure path; the one test that intentionally verifies a
    successful retry patches this symbol explicitly inside its own body.
    """
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **k: None)
    yield


@pytest.fixture(autouse=True)
def _mock_hermes_modules(monkeypatch):
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)

    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in injected}
    sys.modules.update(injected)
    yield
    for name, prev in saved.items():
        if prev is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


class MockAgent:
    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.reasoning_callback = kwargs.get("reasoning_callback")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.context_compressor = None
        self._last_error = None
        self.ephemeral_system_prompt = None

    def run_conversation(self, **kwargs):
        raise NotImplementedError

    def interrupt(self, _message):
        pass


def _prepare_session(session_id: str, stream_id: str, *, pending_user_message: str, partial_source: str = "cli"):
    session = Session(session_id=session_id, title="Test Session")
    session.messages = []
    session.context_messages = []
    session.pending_user_message = pending_user_message
    session.pending_attachments = ["attachment.txt"]
    session.pending_started_at = 1234567890.0
    session.pending_user_source = partial_source
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _seed_prior_turn(session, *, prior_user: str, prior_assistant: str):
    session.messages = [
        {"role": "user", "content": prior_user, "timestamp": 1},
        {"role": "assistant", "content": prior_assistant, "timestamp": 2},
    ]
    session.context_messages = [
        {"role": "user", "content": prior_user},
        {"role": "assistant", "content": prior_assistant},
    ]
    session.save()


def _queue_events(fake_queue):
    return [(item[0], item[1]) for item in list(fake_queue.queue)]


def _auth_failure_error_payload():
    return {
        "error": {
            "type": "authentication_error",
            "status_code": 401,
            "code": "auth_unavailable",
            "message": "Your authentication token has been invalidated. Please try signing in again.",
        }
    }


def _build_auth_failure_agent(*, token_text: str | None, success_text: str = "Recovered auth reply"):
    class AuthFailureAgent(MockAgent):
        runs = 0

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs == 1:
                if self.stream_delta_callback is not None and token_text is not None:
                    self.stream_delta_callback(token_text)
                return {
                    "messages": history,
                    "error": _auth_failure_error_payload(),
                }
            return {
                "status": "ok",
                "messages": history + [{"role": "assistant", "content": success_text}],
            }

    return AuthFailureAgent


def _run_stream(monkeypatch, session, stream_id, agent_cls, *, workspace):
    fake_queue = queue.Queue()
    streaming.STREAMS[stream_id] = fake_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=agent_cls), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=workspace,
            stream_id=stream_id,
        )

    return fake_queue


def test_auth_401_without_delivery_persists_error_turn(tmp_path, monkeypatch):
    session = _prepare_session("auth_no_delivery", "stream_auth_no_delivery", pending_user_message="Please respond")
    agent_cls = _build_auth_failure_agent(token_text=None)

    fake_queue = _run_stream(monkeypatch, session, "stream_auth_no_delivery", agent_cls, workspace=str(tmp_path))
    saved = Session.load("auth_no_delivery")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for auth failure"
    assert apperrors[-1]["type"] == "auth_mismatch"
    assert not any(event == "done" for event, _ in events)

    assert saved.active_stream_id is None
    assert saved.pending_user_message is None
    assert saved.pending_attachments == []
    assert saved.pending_started_at is None
    assert saved.pending_user_source is None
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["role"] == "assistant"
    assert any(msg.get("role") == "user" for msg in saved.messages)


def test_auth_401_after_partial_preserves_partial_then_error(tmp_path, monkeypatch):
    session = _prepare_session("auth_partial", "stream_auth_partial", pending_user_message="Please stream then fail")
    agent_cls = _build_auth_failure_agent(token_text="Partial auth text")

    fake_queue = _run_stream(monkeypatch, session, "stream_auth_partial", agent_cls, workspace=str(tmp_path))
    saved = Session.load("auth_partial")
    assert saved is not None

    partial = next((msg for msg in saved.messages if msg.get("_partial")), None)
    assert partial is not None
    assert partial["role"] == "assistant"
    assert partial["content"] == "Partial auth text"

    error_idx = next(i for i, msg in enumerate(saved.messages) if msg.get("_error"))
    partial_idx = saved.messages.index(partial)
    assert partial_idx < error_idx

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors and apperrors[-1]["type"] == "auth_mismatch"


def test_auth_401_seeded_multi_turn_partial_persists_error_turn(tmp_path, monkeypatch):
    session = _prepare_session("auth_seeded_partial", "stream_auth_seeded_partial", pending_user_message="Please stream then fail")
    _seed_prior_turn(
        session,
        prior_user="Earlier question",
        prior_assistant="Earlier answer",
    )
    agent_cls = _build_auth_failure_agent(token_text="Partial auth text")

    fake_queue = _run_stream(monkeypatch, session, "stream_auth_seeded_partial", agent_cls, workspace=str(tmp_path))
    saved = Session.load("auth_seeded_partial")
    assert saved is not None

    assert any(msg.get("role") == "assistant" and msg.get("content") == "Earlier answer" for msg in saved.messages)
    assert any(msg.get("_partial") and msg.get("content") == "Partial auth text" for msg in saved.messages)
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["role"] == "assistant"
    assert any(msg.get("role") == "user" and msg.get("content") == "Please stream then fail" for msg in saved.messages)

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors and apperrors[-1]["type"] == "auth_mismatch"
    assert not any(event == "done" for event, _ in events)


def test_auth_401_classification_receives_stringified_probe_text(tmp_path, monkeypatch):
    session = _prepare_session("auth_probe_text", "stream_auth_probe_text", pending_user_message="Please fail")
    agent_cls = _build_auth_failure_agent(token_text=None)
    observed = {}
    real_classify = streaming._classify_provider_error

    def _spy_classify_provider_error(err_str, exc=None, *, silent_failure=False):
        observed["err_str"] = err_str
        observed["exc"] = exc
        observed["silent_failure"] = silent_failure
        return real_classify(err_str, exc, silent_failure=silent_failure)

    with mock.patch.object(streaming, "_classify_provider_error", side_effect=_spy_classify_provider_error):
        _run_stream(monkeypatch, session, "stream_auth_probe_text", agent_cls, workspace=str(tmp_path))

    assert observed["err_str"] == str(_auth_failure_error_payload())
    assert observed["exc"] == _auth_failure_error_payload()
    assert observed["silent_failure"] is False


def test_auth_401_seeded_replayed_assistant_does_not_satisfy_current_turn(tmp_path, monkeypatch):
    session = _prepare_session("auth_seeded_replay", "stream_auth_seeded_replay", pending_user_message="Please respond now")
    _seed_prior_turn(
        session,
        prior_user="Earlier question",
        prior_assistant="Earlier answer",
    )

    class ReplayAssistantAuthFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history + [{"role": "assistant", "content": "Earlier answer"}],
                "error": _auth_failure_error_payload(),
            }

    fake_queue = _run_stream(monkeypatch, session, "stream_auth_seeded_replay", ReplayAssistantAuthFailureAgent, workspace=str(tmp_path))
    saved = Session.load("auth_seeded_replay")
    assert saved is not None

    assert any(msg.get("role") == "user" and msg.get("content") == "Please respond now" for msg in saved.messages)
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["role"] == "assistant"

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors and apperrors[-1]["type"] == "auth_mismatch"
    assert not any(event == "done" for event, _ in events)


def test_auth_retry_success_does_not_append_error_turn(tmp_path, monkeypatch):
    session = _prepare_session("auth_retry", "stream_auth_retry", pending_user_message="Please retry")
    agent_cls = _build_auth_failure_agent(token_text="")

    heal_rt = {
        "provider": "test-provider",
        "api_key": "fresh-key",
        "base_url": None,
    }

    fake_queue = queue.Queue()
    streaming.STREAMS["stream_auth_retry"] = fake_queue
    config.STREAM_PARTIAL_TEXT["stream_auth_retry"] = ""

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=agent_cls), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.object(streaming, "_attempt_credential_self_heal", return_value=heal_rt):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=str(tmp_path),
            stream_id="stream_auth_retry",
        )

    saved = Session.load("auth_retry")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert not any(event == "apperror" for event, _ in events)
    assert any(event == "done" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Recovered auth reply"
    assert not any(msg.get("_error") for msg in saved.messages)


def test_success_repeated_assistant_text_stays_successful_current_turn(tmp_path, monkeypatch):
    session = _prepare_session("repeat_success", "stream_repeat_success", pending_user_message="Please say it again")
    _seed_prior_turn(
        session,
        prior_user="Earlier question",
        prior_assistant="Same answer",
    )

    class RepeatedSuccessAgent(MockAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history + [{"role": "assistant", "content": "Same answer"}],
            }

    fake_queue = _run_stream(monkeypatch, session, "stream_repeat_success", RepeatedSuccessAgent, workspace=str(tmp_path))
    saved = Session.load("repeat_success")
    assert saved is not None

    assert any(msg.get("role") == "user" and msg.get("content") == "Please say it again" for msg in saved.messages)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Same answer"
    assert not any(msg.get("_error") for msg in saved.messages)

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)


def test_success_repeated_assistant_text_ignores_empty_error_field(tmp_path, monkeypatch):
    session = _prepare_session("repeat_success_empty_error", "stream_repeat_success_empty_error", pending_user_message="Please say it again")
    _seed_prior_turn(
        session,
        prior_user="Earlier question",
        prior_assistant="Same answer",
    )

    class RepeatedSuccessWithEmptyErrorAgent(MockAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history + [{"role": "assistant", "content": "Same answer"}],
                "error": None,
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_repeat_success_empty_error",
        RepeatedSuccessWithEmptyErrorAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("repeat_success_empty_error")
    assert saved is not None

    assert any(msg.get("role") == "user" and msg.get("content") == "Please say it again" for msg in saved.messages)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Same answer"
    assert not any(msg.get("_error") for msg in saved.messages)

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)


def test_non_auth_silent_failure_still_uses_no_response(tmp_path, monkeypatch):
    session = _prepare_session("silent_failure", "stream_silent_failure", pending_user_message="Please handle silence")

    class SilentFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(monkeypatch, session, "stream_silent_failure", SilentFailureAgent, workspace=str(tmp_path))
    saved = Session.load("silent_failure")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for silent failure"
    assert apperrors[-1]["type"] == "no_response"
    assert apperrors[-1]["type"] != "auth_mismatch"
    assert saved.messages[-1]["_error"] is True


def test_non_auth_partial_delivery_persists_error_turn(tmp_path, monkeypatch):
    session = _prepare_session("partial_escape", "stream_partial_escape", pending_user_message="Please handle partial silence")

    class PartialSilentFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Partial text before failure")
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(monkeypatch, session, "stream_partial_escape", PartialSilentFailureAgent, workspace=str(tmp_path))
    saved = Session.load("partial_escape")
    assert saved is not None

    partial = next((msg for msg in saved.messages if msg.get("_partial")), None)
    assert partial is not None
    assert partial["content"] == "Partial text before failure"

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for partial silent failure"
    assert apperrors[-1]["type"] == "no_response"
    assert saved.messages[-1]["_error"] is True


def test_non_auth_seeded_multi_turn_partial_persists_error_turn(tmp_path, monkeypatch):
    session = _prepare_session("seeded_partial_escape", "stream_seeded_partial_escape", pending_user_message="Please handle partial silence")
    _seed_prior_turn(
        session,
        prior_user="Earlier question",
        prior_assistant="Earlier answer",
    )

    class PartialSilentFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Partial text before failure")
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(monkeypatch, session, "stream_seeded_partial_escape", PartialSilentFailureAgent, workspace=str(tmp_path))
    saved = Session.load("seeded_partial_escape")
    assert saved is not None

    assert any(msg.get("role") == "assistant" and msg.get("content") == "Earlier answer" for msg in saved.messages)
    assert any(msg.get("_partial") and msg.get("content") == "Partial text before failure" for msg in saved.messages)
    assert saved.messages[-1]["_error"] is True

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for seeded partial silent failure"
    assert apperrors[-1]["type"] == "no_response"
    assert not any(event == "done" for event, _ in events)


def test_non_auth_seeded_replayed_assistant_does_not_satisfy_current_turn(tmp_path, monkeypatch):
    session = _prepare_session("seeded_replay_escape", "stream_seeded_replay_escape", pending_user_message="Please handle this now")
    _seed_prior_turn(
        session,
        prior_user="Earlier question",
        prior_assistant="Earlier answer",
    )

    class ReplayAssistantSilentFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "messages": history + [{"role": "assistant", "content": "Earlier answer"}],
                "error": "",
            }

    fake_queue = _run_stream(monkeypatch, session, "stream_seeded_replay_escape", ReplayAssistantSilentFailureAgent, workspace=str(tmp_path))
    saved = Session.load("seeded_replay_escape")
    assert saved is not None

    assert any(msg.get("role") == "user" and msg.get("content") == "Please handle this now" for msg in saved.messages)
    assert saved.messages[-1]["_error"] is True

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for seeded replay silent failure"
    assert apperrors[-1]["type"] == "no_response"
    assert not any(event == "done" for event, _ in events)
