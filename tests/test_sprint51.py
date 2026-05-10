"""
Test plan for the #653 fix (eager session lock release in cancel_stream).

These tests verify that after cancel_stream() is called:
1. STREAMS is popped (so the 409 guard passes)
2. CANCEL_FLAGS is popped
3. AGENT_INSTANCES is popped
4. Session active_stream_id is cleared (when agent is available)
5. Session pending fields are cleared (when agent is available)

All tests are isolated and clean up after themselves.
"""

import queue
import threading
from unittest.mock import Mock, patch

from api.streaming import cancel_stream
from api.config import AGENT_INSTANCES, STREAMS, CANCEL_FLAGS


class TestCancelStreamEagerRelease:
    """Test suite for #653: eager session lock release on cancel."""

    def setup_method(self):
        """Clean up before each test."""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()

    def teardown_method(self):
        """Clean up after each test."""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()

    def test_cancel_pops_stream_from_streams_dict(self):
        """After cancel, stream_id should no longer be in STREAMS."""
        stream_id = "test_eager_pop"
        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()

        result = cancel_stream(stream_id)

        assert result is True
        assert stream_id not in STREAMS, (
            "cancel_stream() should eagerly pop from STREAMS to release the session lock"
        )

    def test_cancel_pops_cancel_flags(self):
        """After cancel, stream_id should no longer be in CANCEL_FLAGS."""
        stream_id = "test_eager_flags"
        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()

        cancel_stream(stream_id)

        assert stream_id not in CANCEL_FLAGS, (
            "cancel_stream() should eagerly pop from CANCEL_FLAGS"
        )

    def test_cancel_pops_agent_instances(self):
        """After cancel, stream_id should no longer be in AGENT_INSTANCES."""
        stream_id = "test_eager_agent"
        mock_agent = Mock()
        mock_agent.interrupt = Mock()
        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        AGENT_INSTANCES[stream_id] = mock_agent

        cancel_stream(stream_id)

        assert stream_id not in AGENT_INSTANCES, (
            "cancel_stream() should eagerly pop from AGENT_INSTANCES"
        )

    def test_cancel_clears_session_active_stream_id(self):
        """After cancel, session.active_stream_id should be None."""
        stream_id = "test_session_clear"
        session_id = "sess_abc123"
        mock_agent = Mock()
        mock_agent.interrupt = Mock()
        mock_agent.session_id = session_id

        mock_session = Mock()
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "hello"
        mock_session.pending_attachments = ["file.txt"]
        mock_session.pending_started_at = 1234567890.0

        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            cancel_stream(stream_id)

        assert mock_session.active_stream_id is None, (
            "cancel_stream() should clear session.active_stream_id"
        )
        assert mock_session.pending_user_message is None, (
            "cancel_stream() should clear session.pending_user_message"
        )
        assert mock_session.pending_attachments == [], (
            "cancel_stream() should clear session.pending_attachments"
        )
        assert mock_session.pending_started_at is None, (
            "cancel_stream() should clear session.pending_started_at"
        )
        mock_session.save.assert_called_once()

    def test_cancel_without_agent_still_pops_streams(self):
        """Cancel should pop STREAMS even when no agent instance exists."""
        stream_id = "test_no_agent"
        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        # No AGENT_INSTANCES entry

        cancel_stream(stream_id)

        assert stream_id not in STREAMS, (
            "cancel_stream() should pop STREAMS even without agent instance"
        )
        assert stream_id not in CANCEL_FLAGS

    def test_cancel_sentinel_still_queued(self):
        """Cancel sentinel should still be queued before popping STREAMS."""
        stream_id = "test_sentinel"
        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()

        cancel_stream(stream_id)

        # The cancel sentinel should have been queued before the pop
        assert not q.empty()
        event_type, data = q.get_nowait()
        assert event_type == "cancel"
        assert data["message"] == "Cancelled by user"

    def test_double_cancel_is_safe(self):
        """Calling cancel_stream() twice should not raise."""
        stream_id = "test_double"
        mock_agent = Mock()
        mock_agent.interrupt = Mock()
        mock_agent.session_id = "sess_xyz"

        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        AGENT_INSTANCES[stream_id] = mock_agent

        # First cancel
        result1 = cancel_stream(stream_id)
        assert result1 is True
        assert stream_id not in STREAMS

        # Second cancel (stream already popped)
        result2 = cancel_stream(stream_id)
        assert result2 is False

    def test_cancel_handle_get_session_failure(self):
        """Cancel should not raise even if get_session fails."""
        stream_id = "test_session_fail"
        mock_agent = Mock()
        mock_agent.interrupt = Mock()
        mock_agent.session_id = "sess_nonexistent"

        STREAMS[stream_id] = queue.Queue()
        CANCEL_FLAGS[stream_id] = threading.Event()
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch(
            "api.streaming.get_session", side_effect=KeyError("Session not found")
        ):
            # Should not raise
            result = cancel_stream(stream_id)

        assert result is True
        assert stream_id not in STREAMS
