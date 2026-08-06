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
        from api.streaming import _STREAM_FALLBACK_NOTICES
        _STREAM_FALLBACK_NOTICES.clear()
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()

    def teardown_method(self):
        """Clean up after each test"""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        ACTIVE_RUNS.clear()
        from api.streaming import _STREAM_FALLBACK_NOTICES
        _STREAM_FALLBACK_NOTICES.clear()
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
        assert ACTIVE_RUNS[stream_id]["phase"] == "cancelling"
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

    def test_cancel_preserves_partial_text_when_interrupt_pops_buffers(self):
        """Regression (Codex pre-release finding on #3475): the partial-text /
        reasoning / tool-call buffers must be snapshotted UNDER streams_lock
        BEFORE agent.interrupt() runs. Otherwise the worker's finally block can
        pop those live buffers (it does so under STREAMS_LOCK) the instant the
        interrupt wakes it, and the cancelled turn silently loses its
        already-streamed partial text.

        We simulate that race deterministically: the mock agent's interrupt()
        clears the live STREAM_* maps, mimicking the worker finally. The fix
        must still persist the partial text captured before the interrupt.
        """
        from unittest.mock import patch
        from api.config import (
            STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS,
        )

        stream_id = "race_stream_partial"
        session_id = "sess_race_partial"

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial answer so far"
        STREAM_REASONING_TEXT[stream_id] = "thinking..."
        STREAM_LIVE_TOOL_CALLS[stream_id] = [{"name": "search"}]

        mock_agent = Mock()
        mock_agent.session_id = session_id

        def _interrupt(_msg):
            # Mimic the worker's finally block popping the live buffers the
            # moment the interrupt wakes it (it runs under STREAMS_LOCK in prod;
            # here we just clear to model the worst-case post-interrupt state).
            STREAM_PARTIAL_TEXT.pop(stream_id, None)
            STREAM_REASONING_TEXT.pop(stream_id, None)
            STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)

        mock_agent.interrupt = Mock(side_effect=_interrupt)
        AGENT_INSTANCES[stream_id] = mock_agent

        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = []
        mock_session.save = Mock()

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True
        mock_agent.interrupt.assert_called_once_with("Cancelled by user")
        # The cancelled turn must carry the partial text that was live BEFORE the
        # interrupt popped the buffers. Find it in the appended messages.
        appended = [m for m in mock_session.messages if isinstance(m, dict)]
        joined = " ".join(str(m.get("content", "")) for m in appended)
        assert "partial answer so far" in joined, (
            "cancelled turn lost its already-streamed partial text — the snapshot "
            "must be captured under streams_lock BEFORE agent.interrupt()"
        )

    def test_cancel_preserves_partial_text_on_detached_active_run_path(self):
        """Regression (Codex 2nd pre-release finding on #3475): the under-lock
        snapshot must also cover the STREAMS-absent / ACTIVE_RUNS-present path.
        When the browser SSE has detached (no STREAMS entry) but the worker is
        still live in ACTIVE_RUNS, cancel resolves the agent from
        SESSION_AGENT_CACHE; the partial-text snapshot must still be taken
        before agent.interrupt() pops the live buffers.
        """
        from unittest.mock import patch
        from api.config import (
            STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS,
        )

        stream_id = "detached_race_stream"
        session_id = "sess_detached_race"

        # NOTE: deliberately NO STREAMS entry — this is the detached path.
        STREAM_PARTIAL_TEXT[stream_id] = "detached partial text"
        STREAM_REASONING_TEXT[stream_id] = "detached reasoning"
        STREAM_LIVE_TOOL_CALLS[stream_id] = [{"name": "tool"}]

        mock_agent = Mock()
        mock_agent.session_id = session_id

        def _interrupt(_msg):
            STREAM_PARTIAL_TEXT.pop(stream_id, None)
            STREAM_REASONING_TEXT.pop(stream_id, None)
            STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)

        mock_agent.interrupt = Mock(side_effect=_interrupt)

        ACTIVE_RUNS[stream_id] = {
            "session_id": session_id,
            "started_at": 1.0,
            "phase": "running",
        }
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[session_id] = (mock_agent, "sig")

        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = []
        mock_session.save = Mock()

        with patch("api.streaming.get_session", return_value=mock_session), \
                patch("api.streaming._cached_agent_matches_session", return_value=True):
            result = cancel_stream(stream_id)

        assert result is True
        mock_agent.interrupt.assert_called_once_with("Cancelled by user")
        appended = [m for m in mock_session.messages if isinstance(m, dict)]
        joined = " ".join(str(m.get("content", "")) for m in appended)
        assert "detached partial text" in joined, (
            "detached-path cancel lost its partial text — the under-lock snapshot "
            "must cover the ACTIVE_RUNS-only path too, not just STREAMS-present"
        )

    def test_cancel_fallback_notice_worker_first_schedule_with_partial(self):
        """Production-composed test: cancel_stream() claims the fallback notice
        BEFORE calling interrupt(), so the worker's finally cannot pop it.

        This exercises the worker-first schedule the gate-certifier identified:
        1. Notice is pre-seeded in _STREAM_FALLBACK_NOTICES (simulating a
           callback that already published).
        2. cancel_stream() claims the notice under STREAMS_LOCK (sets
           _cancel_claimed) BEFORE calling agent.interrupt().
        3. The mock interrupt() simulates the worker's finally block running
           BEFORE interrupt() returns — the adversarial schedule.
        4. The worker's finally sees _cancel_claimed=True and SKIPS popping.
        5. interrupt() returns, cancel stamps the notice on the current-turn
           partial row, saves, and cleans up.

        Asserts: exactly one durable current-turn notice with clean keys (no
        _cancel_claimed), no prior-turn mutation, no deadlock, final map cleanup.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES, STREAMS_LOCK
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "worker_first_partial"
        session_id = "sess_worker_first_partial"

        _fb_notice = {
            "message": "Switched to fallback model: gpt-4 via openai → claude-3 via anthropic",
            "to_model": "claude-3",
            "to_provider": "anthropic",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial answer so far"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _STREAM_FALLBACK_NOTICES[stream_id] = dict(_fb_notice)

        mock_agent = Mock()
        mock_agent.session_id = session_id

        # The mock interrupt() simulates the worker-first schedule: the
        # worker's finally block runs BEFORE interrupt() returns.  This is
        # the adversarial schedule the gate-certifier identified — the
        # worker pops _STREAM_FALLBACK_NOTICES if _cancel_claimed is not set.
        def _interrupt_side_effect(_msg):
            # Simulate the worker's finally block: pop if not claimed
            with STREAMS_LOCK:
                _entry = _STREAM_FALLBACK_NOTICES.get(stream_id)
                if _entry is not None and not _entry.get('_cancel_claimed'):
                    _STREAM_FALLBACK_NOTICES.pop(stream_id, None)

        mock_agent.interrupt = Mock(side_effect=_interrupt_side_effect)
        AGENT_INSTANCES[stream_id] = mock_agent

        _prior_assistant = {
            "role": "assistant",
            "content": "This is from a previous turn.",
            "timestamp": 1000,
        }

        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior_assistant]
        mock_session.save = Mock()

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True, "cancel_stream must return True"
        mock_agent.interrupt.assert_called_once_with("Cancelled by user")

        # The notice must survive the worker-first schedule: cancel claimed
        # it before interrupt(), so the worker's finally skipped popping.
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 stamped notice, got {len(stamped)} — "
            "the worker-first schedule must NOT lose the notice when "
            "cancel claims before interrupt()"
        )
        # No _cancel_claimed in the persisted notice
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice, (
            f"_cancel_claimed leaked into persisted _fallbackNotice: {notice}"
        )
        assert notice.get("message") == _fb_notice["message"]
        assert notice.get("to_model") == _fb_notice["to_model"]
        assert notice.get("to_provider") == _fb_notice["to_provider"]
        # Prior turn must NOT be mutated
        assert "_fallbackNotice" not in _prior_assistant, (
            "prior-turn assistant message was mutated"
        )
        # Final map cleanup
        assert stream_id not in _STREAM_FALLBACK_NOTICES, (
            "fallback notice map entry was not cleaned up"
        )

    def test_cancel_fallback_notice_worker_first_schedule_no_partial(self):
        """No-partial case: when no partial text exists, the notice is stamped
        on the newly created cancellation marker (current-turn row).

        Uses the same worker-first schedule as the with_partial test but with
        empty partial text, forcing the stamp onto the cancel marker.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "worker_first_no_partial"
        session_id = "sess_worker_first_no_partial"

        _fb_notice = {
            "message": "Switched to fallback model: gpt-4 via openai → claude-3 via anthropic",
            "to_model": "claude-3",
            "to_provider": "anthropic",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        # NO partial text
        STREAM_PARTIAL_TEXT[stream_id] = ""
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _STREAM_FALLBACK_NOTICES[stream_id] = dict(_fb_notice)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        _prior_assistant = {
            "role": "assistant",
            "content": "This is from a previous turn.",
            "timestamp": 1000,
        }
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior_assistant]
        mock_session.save = Mock()

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True

        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 stamped notice on the cancel marker, got {len(stamped)}"
        )
        assert stamped[0].get("_error") is True, (
            "notice was stamped on a non-cancel-marker message"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice, (
            f"_cancel_claimed leaked into persisted _fallbackNotice: {notice}"
        )
        assert "_fallbackNotice" not in _prior_assistant, (
            "prior-turn assistant message was mutated in the no-partial case"
        )
        assert stream_id not in _STREAM_FALLBACK_NOTICES, (
            "fallback notice map entry was not cleaned up"
        )

    def test_cancel_fallback_notice_late_publication_no_stamp(self):
        """When no notice has been published when cancel claims, cancel finds
        nothing and does not stamp. The worker's own error-path save is
        responsible for persisting via _pending_fallback_notices.

        This exercises the late-publication schedule: the notice arrives after
        cancel has already claimed (found nothing). Cancel must not stamp
        anything; the worker's error save handles persistence.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "late_publication"
        session_id = "sess_late_publication"

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial answer"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        # NO notice pre-seeded — simulates late publication

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = []
        mock_session.save = Mock()

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True

        # No notice was published, so no stamp
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 0, (
            "cancel should not stamp when no notice was published"
        )
        # Map should be clean (nothing to clean up)
        assert stream_id not in _STREAM_FALLBACK_NOTICES
