"""Regression tests for silent provider failure retry in streaming.

Silent provider failures occur when the API returns no error AND no messages
(empty response body, provider timeout without HTTP error, rate limits without
HTTP 429). The streaming layer retries up to 3 times with exponential backoff.
"""

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


def _prepare_session(session_id: str, stream_id: str, *, pending_user_message: str):
    session = Session(session_id=session_id, title="Test Session")
    session.messages = []
    session.context_messages = []
    session.pending_user_message = pending_user_message
    session.pending_attachments = []
    session.pending_started_at = 1234567890.0
    session.pending_user_source = "cli"
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _queue_events(fake_queue):
    return [(item[0], item[1]) for item in list(fake_queue.queue)]


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


def _build_silent_failure_agent(success_on_attempt: int = 2):
    """Agent that returns empty result (no messages, no error key) for first N-1 attempts."""
    class SilentFailureAgent(MockAgent):
        runs = 0

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs < success_on_attempt:
                # Silent failure: no error key, no messages
                return {}
            return {
                "status": "ok",
                "messages": history + [{"role": "assistant", "content": "Success after retry"}],
            }

    return SilentFailureAgent


def _build_none_result_agent(success_on_attempt: int = 2):
    """Agent that returns None for first N-1 attempts."""
    class NoneResultAgent(MockAgent):
        runs = 0

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            if type(self).runs < success_on_attempt:
                return None
            return {
                "status": "ok",
                "messages": history + [{"role": "assistant", "content": "Success after retry"}],
            }

    return NoneResultAgent


def test_silent_provider_failure_retries_and_succeeds(tmp_path, monkeypatch):
    """Silent failure on attempt 1 should retry and succeed on attempt 2."""
    session = _prepare_session("silent_retry", "stream_silent_retry", pending_user_message="Test message")
    agent_cls = _build_silent_failure_agent(success_on_attempt=2)

    with mock.patch("api.streaming.time.sleep"):  # Skip actual delays
        fake_queue = _run_stream(monkeypatch, session, "stream_silent_retry", agent_cls, workspace=str(tmp_path))

    assert agent_cls.runs == 2  # Two attempts: initial + one retry
    saved = Session.load("silent_retry")
    assert saved is not None
    assert saved.messages[-1]["role"] == "assistant"
    assert saved.messages[-1]["content"] == "Success after retry"

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)


def test_silent_provider_failure_none_result_retries_and_succeeds(tmp_path, monkeypatch):
    """None result should also trigger retry and succeed."""
    session = _prepare_session("silent_none", "stream_silent_none", pending_user_message="Test message")
    agent_cls = _build_none_result_agent(success_on_attempt=3)

    with mock.patch("api.streaming.time.sleep"):  # Skip actual delays
        fake_queue = _run_stream(monkeypatch, session, "stream_silent_none", agent_cls, workspace=str(tmp_path))

    assert agent_cls.runs == 3  # Three attempts: initial + two retries
    saved = Session.load("silent_none")
    assert saved is not None
    assert saved.messages[-1]["role"] == "assistant"


def test_silent_provider_failure_all_retries_exhausted(tmp_path, monkeypatch):
    """After 3 retries exhausted, should return empty response to user."""
    session = _prepare_session("silent_exhausted", "stream_silent_exhausted", pending_user_message="Test message")
    agent_cls = _build_silent_failure_agent(success_on_attempt=10)  # Never succeeds

    with mock.patch("api.streaming.time.sleep"):  # Skip actual delays
        fake_queue = _run_stream(monkeypatch, session, "stream_silent_exhausted", agent_cls, workspace=str(tmp_path))

    assert agent_cls.runs == 3  # Only 3 attempts max
    saved = Session.load("silent_exhausted")
    assert saved is not None
    # After retries exhausted, should have empty response
    assert any(msg.get("role") == "assistant" for msg in saved.messages)


def test_silent_provider_failure_no_error_field_succeeds_first_try(tmp_path, monkeypatch):
    """Normal success with content should not trigger retry."""
    session = _prepare_session("no_error_success", "stream_no_error_success", pending_user_message="Test message")

    class ImmediateSuccessAgent(MockAgent):
        runs = 0

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            history = list(kwargs.get("conversation_history") or [])
            return {
                "status": "ok",
                "messages": history + [{"role": "assistant", "content": "Immediate success"}],
            }

    agent_cls = ImmediateSuccessAgent

    with mock.patch("api.streaming.time.sleep"):
        fake_queue = _run_stream(monkeypatch, session, "stream_no_error_success", agent_cls, workspace=str(tmp_path))

    assert agent_cls.runs == 1  # Only one attempt
    saved = Session.load("no_error_success")
    assert saved is not None
    assert saved.messages[-1]["content"] == "Immediate success"

    events = _queue_events(fake_queue)
    assert any(event == "done" for event, _ in events)