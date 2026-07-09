"""Regression tests for issue #5121: provider auth failures must persist as terminal error turns."""

from __future__ import annotations

import queue
import sys
import types
from unittest import mock

import pytest

import api.config as config
import api.models as models
import api.routes as routes
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


def _run_stream(monkeypatch, session, stream_id, agent_cls, *, workspace, ephemeral=False):
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
            ephemeral=ephemeral,
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


def test_auth_retry_streamed_answer_without_result_message_is_saved_as_final(tmp_path, monkeypatch):
    session = _prepare_session(
        "auth_retry_streamed_answer",
        "stream_auth_retry_streamed_answer",
        pending_user_message="Please retry and stream",
    )

    class AuthRetryStreamedAnswerAgent(MockAgent):
        runs = 0

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs == 1:
                if self.stream_delta_callback is not None:
                    self.stream_delta_callback("Stale first-attempt text")
                return {
                    "messages": history,
                    "error": _auth_failure_error_payload(),
                }
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Recovered streamed reply")
            return {
                "status": "ok",
                "messages": history,
                "error": "",
            }

    heal_rt = {
        "provider": "test-provider",
        "api_key": "fresh-key",
        "base_url": None,
    }

    fake_queue = queue.Queue()
    streaming.STREAMS["stream_auth_retry_streamed_answer"] = fake_queue
    config.STREAM_PARTIAL_TEXT["stream_auth_retry_streamed_answer"] = ""

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=AuthRetryStreamedAnswerAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.object(streaming, "_attempt_credential_self_heal", return_value=heal_rt):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=str(tmp_path),
            stream_id="stream_auth_retry_streamed_answer",
        )

    saved = Session.load("auth_retry_streamed_answer")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Recovered streamed reply"
    assert "Stale first-attempt text" not in saved.messages[-1]["content"]
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Recovered streamed reply"
    assert "Stale first-attempt text" not in saved.context_messages[-1]["content"]
    assert not any(msg.get("_error") for msg in saved.messages)


def test_auth_retry_streamed_answer_with_explicit_error_still_errors(tmp_path, monkeypatch):
    session = _prepare_session(
        "auth_retry_streamed_explicit_error",
        "stream_auth_retry_streamed_explicit_error",
        pending_user_message="Please retry then fail explicitly",
    )

    class AuthRetryStreamedExplicitErrorAgent(MockAgent):
        runs = 0

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs == 1:
                return {
                    "messages": history,
                    "error": _auth_failure_error_payload(),
                }
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Visible retry text before explicit error")
            return {
                "status": "ok",
                "messages": history,
                "error": "provider failed on retry",
            }

    heal_rt = {
        "provider": "test-provider",
        "api_key": "fresh-key",
        "base_url": None,
    }

    fake_queue = queue.Queue()
    streaming.STREAMS["stream_auth_retry_streamed_explicit_error"] = fake_queue
    config.STREAM_PARTIAL_TEXT["stream_auth_retry_streamed_explicit_error"] = ""

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=AuthRetryStreamedExplicitErrorAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.object(streaming, "_attempt_credential_self_heal", return_value=heal_rt):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=str(tmp_path),
            stream_id="stream_auth_retry_streamed_explicit_error",
        )

    saved = Session.load("auth_retry_streamed_explicit_error")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for explicit retry failure"
    assert not any(event == "done" for event, _ in events)
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["content"] != "Visible retry text before explicit error"


def test_auth_exception_retry_streamed_answer_emits_done_and_saves_final(tmp_path, monkeypatch):
    session = _prepare_session(
        "auth_exception_retry_streamed_answer",
        "stream_auth_exception_retry_streamed_answer",
        pending_user_message="Please recover from auth exception",
    )

    class AuthExceptionRetryStreamedAnswerAgent(MockAgent):
        runs = 0

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs == 1:
                raise RuntimeError("401 unauthorized: token expired")
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Recovered streamed reply after exception")
            return {
                "status": "ok",
                "messages": history,
                "error": "",
            }

    heal_rt = {
        "provider": "test-provider",
        "api_key": "fresh-key",
        "base_url": None,
    }

    fake_queue = queue.Queue()
    streaming.STREAMS["stream_auth_exception_retry_streamed_answer"] = fake_queue
    config.STREAM_PARTIAL_TEXT["stream_auth_exception_retry_streamed_answer"] = ""

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=AuthExceptionRetryStreamedAnswerAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.object(streaming, "_attempt_credential_self_heal", return_value=heal_rt):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=str(tmp_path),
            stream_id="stream_auth_exception_retry_streamed_answer",
        )

    saved = Session.load("auth_exception_retry_streamed_answer")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert any(event == "stream_end" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Recovered streamed reply after exception"
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Recovered streamed reply after exception"
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


def test_streamed_answer_with_no_response_error_is_saved_as_final_answer(tmp_path, monkeypatch):
    """A streamed visible answer must win over a synthetic no_response label.

    Regression for live session bf2df88ac95a: thousands of token events produced
    a complete visible answer, then the agent surfaced ``No response from
    provider`` instead of returning a final assistant row. That is not the same
    as a hard provider exception; WebUI should persist the visible streamed
    answer and emit ``done``.
    """
    session = _prepare_session(
        "streamed_answer_with_no_response_error",
        "stream_streamed_answer_with_no_response_error",
        pending_user_message="Please stream and then return no_response",
    )

    class StreamedNoResponseAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Visible answer before no_response label.")
            self._last_error = "No response from provider."
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "No response from provider.",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_streamed_answer_with_no_response_error",
        StreamedNoResponseAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("streamed_answer_with_no_response_error")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Visible answer before no_response label."
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Visible answer before no_response label."
    assert not any(msg.get("_error") for msg in saved.messages)


def test_ephemeral_explicit_provider_error_emits_apperror(tmp_path, monkeypatch):
    session = _prepare_session(
        "ephemeral_explicit_error",
        "stream_ephemeral_explicit_error",
        pending_user_message="Please fail ephemerally",
    )

    class EphemeralExplicitErrorAgent(MockAgent):
        def run_conversation(self, **kwargs):
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "provider failed",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_ephemeral_explicit_error",
        EphemeralExplicitErrorAgent,
        workspace=str(tmp_path),
        ephemeral=True,
    )

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for ephemeral provider failure"
    assert apperrors[-1]["type"] == "error"
    assert not any(event == "done" for event, _ in events)


def test_ephemeral_empty_result_emits_no_response(tmp_path, monkeypatch):
    session = _prepare_session(
        "ephemeral_empty_result",
        "stream_ephemeral_empty_result",
        pending_user_message="Please say something ephemerally",
    )

    class EphemeralEmptyResultAgent(MockAgent):
        def run_conversation(self, **kwargs):
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_ephemeral_empty_result",
        EphemeralEmptyResultAgent,
        workspace=str(tmp_path),
        ephemeral=True,
    )

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected no_response for ephemeral empty result"
    assert apperrors[-1]["type"] == "no_response"
    assert not any(event == "done" for event, _ in events)


def test_live_settlement_empty_hint_does_not_append_empty_emphasis(tmp_path, monkeypatch):
    session = _prepare_session(
        "empty_hint_failure",
        "stream_empty_hint_failure",
        pending_user_message="Please fail plainly",
    )

    class EmptyHintFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            return {
                "status": "failed",
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "synthetic hard failure",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_empty_hint_failure",
        EmptyHintFailureAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("empty_hint_failure")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for generic terminal failure"
    assert apperrors[-1]["type"] == "error"
    assert apperrors[-1].get("hint") in (None, "")

    error_content = saved.messages[-1]["content"]
    assert saved.messages[-1]["_error"] is True
    assert error_content == "**Error:** synthetic hard failure"
    assert "\n\n**" not in error_content
    assert not error_content.endswith("**")


def test_silent_failure_suppressed_when_final_answer_already_persisted(tmp_path, monkeypatch):
    session = _prepare_session(
        "silent_failure_already_persisted",
        "stream_silent_failure_already_persisted",
        pending_user_message="Please use persisted answer",
    )
    persisted = Session.load("silent_failure_already_persisted")
    assert persisted is not None
    persisted.messages = [
        {"role": "user", "content": "Please use persisted answer", "timestamp": 1234567890},
        {"role": "assistant", "content": "Already persisted answer", "timestamp": 1234567891},
    ]
    persisted.context_messages = [
        {"role": "user", "content": "Please use persisted answer", "timestamp": 1234567890},
        {"role": "assistant", "content": "Already persisted answer", "timestamp": 1234567891},
    ]
    persisted.active_stream_id = "stream_silent_failure_already_persisted"
    persisted.pending_user_message = "Please use persisted answer"
    persisted.pending_attachments = ["attachment.txt"]
    persisted.pending_started_at = 1234567890.0
    persisted.save()

    # Simulate a stale in-memory worker that did not see the already-finalized
    # sidecar transcript. The no_response path must re-read persisted truth
    # before appending a synthetic provider error.
    session.messages = []
    session.context_messages = []
    models.SESSIONS[session.session_id] = session

    class SilentFailureAfterPersistAgent(MockAgent):
        def run_conversation(self, **kwargs):
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_silent_failure_already_persisted",
        SilentFailureAfterPersistAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("silent_failure_already_persisted")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.active_stream_id is None
    assert saved.pending_user_message is None
    assert saved.pending_attachments == []
    assert saved.pending_started_at is None
    assert saved.pending_user_source is None
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Already persisted answer"
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Already persisted answer"
    assert not any(msg.get("_error") for msg in saved.messages)


def test_repeated_prompt_old_answer_does_not_suppress_current_no_response(tmp_path, monkeypatch):
    session = _prepare_session(
        "repeated_prompt_silent_failure",
        "stream_repeated_prompt_silent_failure",
        pending_user_message="Repeat this exact request",
    )
    persisted = Session.load("repeated_prompt_silent_failure")
    assert persisted is not None
    persisted.messages = [
        {"role": "user", "content": "Repeat this exact request", "timestamp": 1234567890.0},
        {"role": "assistant", "content": "Old answer must not satisfy the new turn", "timestamp": 1234567890.1},
    ]
    persisted.context_messages = list(persisted.messages)
    persisted.active_stream_id = "stream_repeated_prompt_silent_failure"
    persisted.pending_user_message = "Repeat this exact request"
    persisted.pending_attachments = ["attachment.txt"]
    persisted.pending_started_at = 1234567890.75
    persisted.save()

    # The in-flight worker is for a newer same-text prompt in the same integer
    # second as the older persisted prompt. Prompt text plus a 1s floor tolerance
    # is still ambiguous under retries; because the persisted user row predates
    # this turn's exact send anchor, the guard must fail closed and let the real
    # no_response surface.
    session.messages = []
    session.context_messages = []
    session.pending_started_at = 1234567890.75
    models.SESSIONS[session.session_id] = session

    class SilentFailureAfterOldRepeatedPromptAgent(MockAgent):
        def run_conversation(self, **kwargs):
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_repeated_prompt_silent_failure",
        SilentFailureAfterOldRepeatedPromptAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("repeated_prompt_silent_failure")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected no_response for the newer repeated prompt"
    assert apperrors[-1]["type"] == "no_response"
    assert not any(event == "done" for event, _ in events)
    assert saved.messages[-1]["_error"] is True


def test_eager_checkpointed_final_answer_suppresses_stale_no_response(tmp_path, monkeypatch):
    """Eager checkpoint timestamps must preserve the current-turn anchor.

    The chat-start eager path writes the current user turn before the streaming
    worker runs. If that checkpoint truncates ``pending_started_at`` to integer
    seconds, the strict current-turn guard rejects the real persisted answer and
    appends the stale no_response this PR is meant to suppress.
    """
    msg_text = "Please use eager persisted answer"
    session = _prepare_session(
        "eager_checkpointed_final_answer",
        "stream_eager_checkpointed_final_answer",
        pending_user_message=msg_text,
    )
    session.pending_started_at = 1234567890.75
    session.pending_user_source = "webui"
    routes._checkpoint_user_message_for_eager_session_save(
        session,
        msg_text,
        session.pending_attachments,
        started_at=session.pending_started_at,
        source=session.pending_user_source,
    )
    assert session.messages[0]["timestamp"] == session.pending_started_at
    session.messages.append(
        {"role": "assistant", "content": "Eager persisted answer", "timestamp": 1234567891.0}
    )
    session.context_messages = list(session.messages)
    session.save()

    # Simulate a stale in-memory worker that did not see the eager checkpointed
    # user turn or the persisted final assistant answer.
    session.messages = []
    session.context_messages = []
    models.SESSIONS[session.session_id] = session

    class SilentFailureAfterEagerPersistedAgent(MockAgent):
        def run_conversation(self, **kwargs):
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_eager_checkpointed_final_answer",
        SilentFailureAfterEagerPersistedAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("eager_checkpointed_final_answer")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.active_stream_id is None
    assert saved.pending_user_message is None
    assert saved.pending_attachments == []
    assert saved.pending_started_at is None
    assert saved.pending_user_source is None
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Eager persisted answer"
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Eager persisted answer"
    assert not any(msg.get("_error") for msg in saved.messages)


def test_persisted_final_guard_accepts_already_settled_previous_display():
    """A stale no_response branch must trust a finished persisted transcript.

    Real WebUI recovery can re-enter the silent-failure branch after the sidecar
    and state.db already contain the current user turn and final assistant
    answer. In that case the stale worker's ``previous_display`` may itself be
    the already-settled transcript; the persisted-truth guard must not reject
    the answer merely because it falls before that stale boundary.
    """
    msg_text = "repeat visible CUA tests"
    settled = [
        {"role": "user", "content": msg_text, "timestamp": 1},
        {"role": "assistant", "content": "Visible CUA tests completed", "timestamp": 2},
    ]

    assert streaming._messages_have_final_assistant_for_current_turn(
        settled,
        msg_text,
        previous_display=list(settled),
        min_user_timestamp=1,
    ) is True


def test_persisted_final_guard_trusts_timestamp_anchor_across_previous_display_drift():
    """A proven current user timestamp must not be rejected by cross-list indexes."""
    msg_text = "repeat visible CUA tests"
    previous_display = [
        {"role": "user", "content": "older", "timestamp": 1},
        {"role": "assistant", "content": "older answer", "timestamp": 2},
        {"role": "user", "content": "different pending view", "timestamp": 3},
    ]
    persisted = [
        {"role": "assistant", "content": "compression marker", "timestamp": 4},
        {"role": "user", "content": msg_text, "timestamp": 100.8},
        {"role": "assistant", "content": "Current persisted answer", "timestamp": 101.0},
    ]

    assert streaming._messages_have_final_assistant_for_current_turn(
        persisted,
        msg_text,
        previous_display=previous_display,
        min_user_timestamp=100.8,
    ) is True


def test_persisted_final_guard_rejects_pre_anchor_assistant_candidate():
    msg_text = "repeat visible CUA tests"
    persisted = [
        {"role": "user", "content": msg_text, "timestamp": 100.8},
        {"role": "assistant", "content": "Old out-of-order answer", "timestamp": 100.2},
    ]

    assert streaming._messages_have_final_assistant_for_current_turn(
        persisted,
        msg_text,
        previous_display=[{"role": "user", "content": msg_text, "timestamp": 100.8}],
        min_user_timestamp=100.8,
    ) is False


def test_persisted_final_guard_accepts_ts_anchor_alias():
    msg_text = "repeat visible CUA tests"
    persisted = [
        {"role": "user", "content": msg_text, "_ts": 100.8},
        {"role": "assistant", "content": "Current persisted answer", "_ts": 101.0},
    ]

    assert streaming._messages_have_final_assistant_for_current_turn(
        persisted,
        msg_text,
        previous_display=[{"role": "user", "content": msg_text, "_ts": 100.8}],
        min_user_timestamp=100.8,
    ) is True


def test_completed_assistant_answer_with_stale_partial_flag_settles_done(tmp_path, monkeypatch):
    session = _prepare_session(
        "completed_answer_stale_partial",
        "stream_completed_answer_stale_partial",
        pending_user_message="Please finish cleanly",
    )

    class CompletedAnswerStalePartialAgent(MockAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "status": "partial",
                "partial": True,
                "messages": history + [{"role": "assistant", "content": "Completed answer"}],
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_completed_answer_stale_partial",
        CompletedAnswerStalePartialAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("completed_answer_stale_partial")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Completed answer"
    assert not any(msg.get("_error") for msg in saved.messages)


def test_streamed_answer_without_result_message_is_saved_as_final_answer(tmp_path, monkeypatch):
    """Visible streamed text is a real answer when no terminal error was reported.

    Regression for the live WebUI trace in session 3c748eadef9a: the agent
    streamed a complete assistant answer via ``stream_delta_callback`` and then
    returned a result payload that replayed only conversation history plus an
    empty error field. WebUI must not append ``No response from provider`` after
    text it already showed to the user.
    """
    session = _prepare_session(
        "streamed_answer_no_result_message",
        "stream_streamed_answer_no_result_message",
        pending_user_message="Please ship the CUA patch",
    )

    class StreamedAnswerNoResultAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Implemented and verified. Ready for review.")
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_streamed_answer_no_result_message",
        StreamedAnswerNoResultAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("streamed_answer_no_result_message")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Implemented and verified. Ready for review."
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Implemented and verified. Ready for review."
    assert not any(msg.get("_partial") for msg in saved.messages)
    assert not any(msg.get("_error") for msg in saved.messages)


def test_streamed_answer_with_explicit_provider_error_still_errors(tmp_path, monkeypatch):
    session = _prepare_session(
        "streamed_answer_explicit_error",
        "stream_streamed_answer_explicit_error",
        pending_user_message="Please stream then fail explicitly",
    )

    class StreamedAnswerExplicitErrorAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Visible text before explicit provider error")
            return {
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "provider failed after streaming",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_streamed_answer_explicit_error",
        StreamedAnswerExplicitErrorAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("streamed_answer_explicit_error")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for explicit provider error"
    assert not any(event == "done" for event, _ in events)
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["content"] != "Visible text before explicit provider error"


def test_streamed_answer_with_compression_exhausted_still_errors(tmp_path, monkeypatch):
    session = _prepare_session(
        "streamed_answer_compression_exhausted",
        "stream_streamed_answer_compression_exhausted",
        pending_user_message="Please stream then exhaust compression",
    )

    class StreamedAnswerCompressionExhaustedAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Visible text before compression exhaustion")
            return {
                "status": "failed",
                "failed": True,
                "partial": True,
                "compression_exhausted": True,
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "Context length exceeded: cannot compress further.",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_streamed_answer_compression_exhausted",
        StreamedAnswerCompressionExhaustedAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("streamed_answer_compression_exhausted")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for compression exhaustion"
    assert apperrors[-1]["type"] == "compression_exhausted"
    assert not any(event == "done" for event, _ in events)
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["content"] != "Visible text before compression exhaustion"


def test_streamed_answer_with_tool_limit_still_errors(tmp_path, monkeypatch):
    session = _prepare_session(
        "streamed_answer_tool_limit",
        "stream_streamed_answer_tool_limit",
        pending_user_message="Please stream then hit tool limit",
    )

    class StreamedAnswerToolLimitAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Visible text before tool limit")
            return {
                "turn_exit_reason": "max_iterations_reached",
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_streamed_answer_tool_limit",
        StreamedAnswerToolLimitAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("streamed_answer_tool_limit")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for tool-limit result without final answer"
    assert not any(event == "done" for event, _ in events)
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["content"] != "Visible text before tool limit"


def test_stale_partial_with_unfinished_tool_call_still_reports_no_response(tmp_path, monkeypatch):
    session = _prepare_session(
        "unfinished_tool_call_stale_partial",
        "stream_unfinished_tool_call_stale_partial",
        pending_user_message="Use a tool first",
    )

    class UnfinishedToolCallStalePartialAgent(MockAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "status": "partial",
                "partial": True,
                "messages": history + [
                    {"role": "user", "content": "Use a tool first"},
                    {
                        "role": "assistant",
                        "content": "Checking the tool result",
                        "tool_calls": [{"id": "call_1", "type": "function"}],
                    },
                ],
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_unfinished_tool_call_stale_partial",
        UnfinishedToolCallStalePartialAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("unfinished_tool_call_stale_partial")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for unfinished tool-call partial"
    assert apperrors[-1]["type"] == "no_response"
    assert not any(event == "done" for event, _ in events)
    assert saved.messages[-1]["_error"] is True


def test_soft_partial_streamed_answer_for_repeated_prompt_is_saved_as_current_answer(tmp_path, monkeypatch):
    session = _prepare_session(
        "repeated_prompt_replay_stale_partial",
        "stream_repeated_prompt_replay_stale_partial",
        pending_user_message="Please repeat this",
    )
    _seed_prior_turn(
        session,
        prior_user="Please repeat this",
        prior_assistant="Old answer",
    )

    class RepeatedPromptReplayStalePartialAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Current streamed answer for the repeated prompt")
            return {
                "status": "partial",
                "partial": True,
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_repeated_prompt_replay_stale_partial",
        RepeatedPromptReplayStalePartialAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("repeated_prompt_replay_stale_partial")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Current streamed answer for the repeated prompt"
    assert any(msg.get("role") == "assistant" and msg.get("content") == "Old answer" for msg in saved.messages)
    assert not any(msg.get("_error") for msg in saved.messages)


def test_hard_failure_with_completed_answer_still_reports_no_response(tmp_path, monkeypatch):
    session = _prepare_session(
        "hard_failure_completed_answer",
        "stream_hard_failure_completed_answer",
        pending_user_message="Please finish despite failure",
    )

    class HardFailureCompletedAnswerAgent(MockAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            return {
                "status": "failed",
                "messages": history + [{"role": "assistant", "content": "Completed answer"}],
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_hard_failure_completed_answer",
        HardFailureCompletedAnswerAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("hard_failure_completed_answer")
    assert saved is not None

    events = _queue_events(fake_queue)
    apperrors = [data for event, data in events if event == "apperror"]
    assert apperrors, "expected apperror for hard failed result"
    assert apperrors[-1]["type"] == "no_response"
    assert not any(event == "done" for event, _ in events)
    assert saved.messages[-1]["_error"] is True


def test_streamed_answer_with_soft_partial_result_is_saved_as_final_answer(tmp_path, monkeypatch):
    """Soft partial/no-error results must not append no_response after visible text.

    Regression for live session dc3acc5acdaa: the agent streamed a complete
    answer via ``stream_delta_callback`` and then returned a soft ``partial``
    result with no explicit provider error and no final assistant row. WebUI
    should trust the visible streamed answer instead of appending a misleading
    ``No response from provider`` card.
    """
    session = _prepare_session(
        "soft_partial_streamed_answer",
        "stream_soft_partial_streamed_answer",
        pending_user_message="Please finish with streamed text",
    )

    class SoftPartialStreamedAnswerAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Completed visible answer from the stream.")
            return {
                "status": "partial",
                "partial": True,
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(
        monkeypatch,
        session,
        "stream_soft_partial_streamed_answer",
        SoftPartialStreamedAnswerAgent,
        workspace=str(tmp_path),
    )
    saved = Session.load("soft_partial_streamed_answer")
    assert saved is not None

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Completed visible answer from the stream."
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Completed visible answer from the stream."
    assert not any(msg.get("_partial") for msg in saved.messages)
    assert not any(msg.get("_error") for msg in saved.messages)


def test_non_auth_seeded_multi_turn_partial_saves_streamed_text_as_final_answer(tmp_path, monkeypatch):
    session = _prepare_session(
        "seeded_partial_escape",
        "stream_seeded_partial_escape",
        pending_user_message="Please handle partial silence",
    )
    _seed_prior_turn(
        session,
        prior_user="Earlier question",
        prior_assistant="Earlier answer",
    )

    class PartialSilentFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback is not None:
                self.stream_delta_callback("Streamed answer before soft partial result")
            return {
                "status": "partial",
                "partial": True,
                "messages": list(kwargs.get("conversation_history") or []),
                "error": "",
            }

    fake_queue = _run_stream(monkeypatch, session, "stream_seeded_partial_escape", PartialSilentFailureAgent, workspace=str(tmp_path))
    saved = Session.load("seeded_partial_escape")
    assert saved is not None

    assert any(msg.get("role") == "assistant" and msg.get("content") == "Earlier answer" for msg in saved.messages)
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Streamed answer before soft partial result"
    assert saved.context_messages[-1]["role"] == "assistant"
    assert saved.context_messages[-1]["content"] == "Streamed answer before soft partial result"
    assert not any(msg.get("_partial") for msg in saved.messages)
    assert not any(msg.get("_error") for msg in saved.messages)

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)
    assert not any(event == "apperror" for event, _ in events)


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


def test_display_merge_preserves_identical_partial_text_across_separate_turns():
    previous_display = [
        {"role": "user", "content": "first cancelled turn"},
        {
            "role": "assistant",
            "content": "Same partial progress",
            "_partial": True,
            "_partial_tool_calls": [
                {"name": "terminal", "args": {"command": "first"}, "done": True}
            ],
        },
        {"role": "assistant", "content": "First turn cancelled", "_error": True},
        {"role": "user", "content": "second cancelled turn"},
        {
            "role": "assistant",
            "content": "Same partial progress",
            "_partial": True,
            "_partial_tool_calls": [
                {"name": "read_file", "args": {"path": "second"}, "done": True}
            ],
        },
        {"role": "assistant", "content": "Second turn cancelled", "_error": True},
        {"role": "user", "content": "successful turn"},
    ]
    previous_context = list(previous_display)
    result_messages = previous_context + [
        {"role": "assistant", "content": "Successful final answer"}
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "successful turn",
    )

    partials = [msg for msg in merged if msg.get("_partial")]
    assert len(partials) == 2
    assert [msg["_partial_tool_calls"][0]["name"] for msg in partials] == [
        "terminal",
        "read_file",
    ]
