"""Tests for Issue #3929: Ensure turn-level error handling preserves partial work in the WebUI session sidecar."""

from __future__ import annotations

import queue
import sys
import types
import pytest
from unittest import mock

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR / SESSION_INDEX_FILE to an isolated temp dir."""
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
    """Clear all shared streaming dicts before/after each test."""
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    if hasattr(config, 'STREAM_REASONING_TEXT'):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, 'STREAM_LIVE_TOOL_CALLS'):
        config.STREAM_LIVE_TOOL_CALLS.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    if hasattr(config, 'STREAM_REASONING_TEXT'):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, 'STREAM_LIVE_TOOL_CALLS'):
        config.STREAM_LIVE_TOOL_CALLS.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    config.SESSION_AGENT_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()


@pytest.fixture(autouse=True)
def _mock_hermes_modules(monkeypatch):
    """Inject mock hermes modules to prevent side-effects during tests."""
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_provider_fn = lambda requested=None: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_runtime_module.resolve_runtime_provider = fake_runtime_provider_fn
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)

    _injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    _MISSING = object()
    _saved = {k: sys.modules.get(k, _MISSING) for k in _injected}
    sys.modules.update(_injected)
    yield
    for k, prev in _saved.items():
        if prev is _MISSING:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = prev


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
        pass

    def interrupt(self, _message):
        pass


def test_silent_failure_preserves_partials(tmp_path):
    """Test that a silent failure (agent returns no assistant reply) preserves partial streamed text."""
    fake_session = Session(session_id="test_sess_silent", title="Test Session")
    fake_session.pending_user_message = "What is python?"
    fake_session.active_stream_id = "test_stream_silent"
    fake_session.save()
    models.SESSIONS["test_sess_silent"] = fake_session

    class SilentFailureAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("Python is a programming language.")
            # Return history without new assistant message, plus error status (causes silent failure)
            return {
                "status": "error",
                "error": "Silent failure details",
                "messages": kwargs.get("conversation_history") or []
            }

    fake_queue = queue.Queue()
    streaming.STREAMS["test_stream_silent"] = fake_queue
    config.STREAM_PARTIAL_TEXT["test_stream_silent"] = ""

    with mock.patch.object(streaming, "get_session", return_value=fake_session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=SilentFailureAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="What is python?",
            model="test-model",
            workspace=str(tmp_path),
            stream_id="test_stream_silent",
        )

    # Reload session from disk/cache and verify
    saved = Session.load("test_sess_silent")
    assert saved is not None
    
    # We should have:
    # 1. The user turn (materialized by _materialize_pending_user_turn_before_error)
    # 2. The _partial assistant message containing the streamed text
    # 3. The error block
    assert len(saved.messages) >= 3, f"Expected at least 3 messages. Got: {saved.messages}"
    
    partial_msg = next((m for m in saved.messages if m.get("_partial")), None)
    assert partial_msg is not None, "Expected a partial message"
    assert partial_msg["role"] == "assistant"
    assert partial_msg["content"] == "Python is a programming language."

    err_msg = saved.messages[-1]
    assert err_msg.get("_error") is True


def test_exception_preserves_partials(tmp_path):
    """Test that an unhandled exception preserves partial streamed text."""
    fake_session = Session(session_id="test_sess_exc", title="Test Session")
    fake_session.pending_user_message = "Exception test"
    fake_session.active_stream_id = "test_stream_exc"
    fake_session.save()
    models.SESSIONS["test_sess_exc"] = fake_session

    class ExceptionAgent(MockAgent):
        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("Stream before crash.")
            raise RuntimeError("Fake provider crash!")

    fake_queue = queue.Queue()
    streaming.STREAMS["test_stream_exc"] = fake_queue
    config.STREAM_PARTIAL_TEXT["test_stream_exc"] = ""

    with mock.patch.object(streaming, "get_session", return_value=fake_session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=ExceptionAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="Exception test",
            model="test-model",
            workspace=str(tmp_path),
            stream_id="test_stream_exc",
        )

    saved = Session.load("test_sess_exc")
    assert saved is not None
    
    partial_msg = next((m for m in saved.messages if m.get("_partial")), None)
    assert partial_msg is not None, "Expected a partial message before error"
    assert partial_msg["content"] == "Stream before crash."

    err_msg = saved.messages[-1]
    assert err_msg.get("_error") is True
    assert "Fake provider crash!" in err_msg.get("content", "")


def test_empty_partials_do_not_create_spurious_messages(tmp_path):
    """Test that if no content has been streamed, no _partial message is created on error."""
    fake_session = Session(session_id="test_sess_empty", title="Test Session")
    fake_session.pending_user_message = "Empty test"
    fake_session.active_stream_id = "test_stream_empty"
    fake_session.save()
    models.SESSIONS["test_sess_empty"] = fake_session

    class ExceptionAgent(MockAgent):
        def run_conversation(self, **kwargs):
            raise RuntimeError("Fake provider crash immediately!")

    fake_queue = queue.Queue()
    streaming.STREAMS["test_stream_empty"] = fake_queue
    config.STREAM_PARTIAL_TEXT["test_stream_empty"] = ""

    with mock.patch.object(streaming, "get_session", return_value=fake_session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=ExceptionAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="Empty test",
            model="test-model",
            workspace=str(tmp_path),
            stream_id="test_stream_empty",
        )

    saved = Session.load("test_sess_empty")
    assert saved is not None
    
    # We should have user message and error message, but NO _partial message
    assert not any(m.get("_partial") for m in saved.messages)
    
    err_msg = saved.messages[-1]
    assert err_msg.get("_error") is True
