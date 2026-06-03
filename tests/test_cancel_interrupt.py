"""
Unit tests for cancel/interrupt functionality.
Tests the integration between cancel_stream() and agent.interrupt().
"""
import queue
import threading
from unittest.mock import Mock

from api.streaming import cancel_stream
from api.config import AGENT_INSTANCES, STREAMS, CANCEL_FLAGS, ACTIVE_RUNS, SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK


class TestCancelInterrupt:
    """Test suite for cancel/interrupt functionality"""

    def setup_method(self):
        """Clean up before each test"""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        ACTIVE_RUNS.clear()
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()

    def teardown_method(self):
        """Clean up after each test"""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        ACTIVE_RUNS.clear()
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()

    def test_cancel_calls_agent_interrupt(self):
        """Verify that cancel_stream() calls agent.interrupt() when agent exists"""
        # Setup
        stream_id = "test_stream_123"
        mock_agent = Mock()
        mock_agent.interrupt = Mock()

        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        AGENT_INSTANCES[stream_id] = mock_agent

        # Execute
        result = cancel_stream(stream_id)

        # Assert
        assert result is True
        mock_agent.interrupt.assert_called_once_with("Cancelled by user")
        # CANCEL_FLAGS is eagerly popped after cancel (#776 fix) so the flag
        # is no longer in the dict — verify the pop happened instead
        assert stream_id not in CANCEL_FLAGS, \
            "cancel_stream() should eagerly pop CANCEL_FLAGS after signalling"

    def test_cancel_handles_interrupt_exception(self):
        """Verify that cancel_stream() handles interrupt() exceptions gracefully"""
        stream_id = "test_stream_456"
        mock_agent = Mock()
        mock_agent.interrupt = Mock(side_effect=RuntimeError("Agent error"))

        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        AGENT_INSTANCES[stream_id] = mock_agent

        # Should not raise exception
        result = cancel_stream(stream_id)

        # Assert
        assert result is True
        mock_agent.interrupt.assert_called_once()
        assert stream_id not in CANCEL_FLAGS, \
            "cancel_stream() should eagerly pop CANCEL_FLAGS even on interrupt exception"

    def test_cancel_before_agent_ready(self):
        """Test cancel when agent not yet stored in AGENT_INSTANCES (race condition)"""
        stream_id = "test_stream_789"

        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        # Note: AGENT_INSTANCES[stream_id] not set (simulating race condition)

        # Should succeed even without agent
        result = cancel_stream(stream_id)

        # Assert
        assert result is True
        # CANCEL_FLAGS is eagerly popped; the agent thread checks the event
        # object it already has a reference to — pop doesn't clear the event
        assert stream_id not in CANCEL_FLAGS, \
            "cancel_stream() should eagerly pop CANCEL_FLAGS even without an agent"
        # Agent will check this flag (it holds a reference to the event object)

    def test_cancel_nonexistent_stream(self):
        """Test cancel for a stream that doesn't exist"""
        result = cancel_stream("nonexistent_stream")
        assert result is False

    def test_cancel_falls_back_to_active_run_registry(self):
        """Cancel should still work when STREAMS is gone but the worker is alive."""
        from unittest.mock import patch

        stream_id = "detached_stream_123"
        session_id = "sess_detached_123"
        mock_agent = Mock()
        mock_agent.interrupt = Mock()
        mock_agent.session_id = session_id

        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "hello"
        mock_session.pending_attachments = ["file.txt"]
        mock_session.pending_started_at = 1234567890.0
        mock_session.messages = []
        mock_session.save = Mock()

        ACTIVE_RUNS[stream_id] = {
            "session_id": session_id,
            "started_at": 1234567890.0,
            "phase": "running",
        }
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[session_id] = (mock_agent, "sig")

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True
        mock_agent.interrupt.assert_called_once_with("Cancelled by user")
        assert mock_session.active_stream_id is None
        assert mock_session.pending_user_message is None
        assert mock_session.pending_attachments == []
        assert mock_session.pending_started_at is None
        mock_session.save.assert_called_once()

    def test_cancel_sets_cancel_event(self):
        """Verify that cancel_stream() sets the cancel_event flag"""
        stream_id = "test_stream_event"

        STREAMS[stream_id] = queue.Queue()
        cancel_event = threading.Event()
        CANCEL_FLAGS[stream_id] = cancel_event

        result = cancel_stream(stream_id)

        assert result is True
        assert cancel_event.is_set()

    def test_cancel_puts_sentinel_in_queue(self):
        """Verify that cancel_stream() puts cancel sentinel in queue"""
        stream_id = "test_stream_queue"
        q = queue.Queue()

        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()

        result = cancel_stream(stream_id)

        assert result is True
        # Check that cancel message was queued
        assert not q.empty()
        event_type, data = q.get_nowait()
        assert event_type == 'cancel'
        assert data['message'] == 'Cancelled by user'
