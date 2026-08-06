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

    def test_cancel_fallback_notice_ownership_handoff_with_partial(self):
        """Deterministic two-thread test through real production publish/cancel/cleanup path.

        The reviewer (PR #6405 CHANGES_REQUESTED) required replacing the
        source-order test with a real two-thread Barrier/Event test that
        exercises the synchronized ownership handoff:
          - Worker thread publishes the fallback notice under STREAMS_LOCK
            (simulating _agent_status_callback).
          - Cancel thread calls cancel_stream(), which calls interrupt() then
            claims the notice under STREAMS_LOCK.
          - Worker thread's cleanup (simulating _run_agent_streaming finally)
            runs AFTER the claim and must NOT pop the notice because
            _cancel_claimed is set.
          - Cancel stamps the notice on the current-turn partial row and pops
            the entry in its finally block.

        Asserts: exactly one durable current-turn notice, no prior-turn
        mutation, no deadlock, final map cleanup.
        """
        import threading
        from unittest.mock import patch
        from api.streaming import _STREAM_FALLBACK_NOTICES, STREAMS_LOCK
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "ownership_handoff_partial"
        session_id = "sess_ownership_partial"

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

        # Barrier: both threads sync here before proceeding.
        # This forces publication to happen BEFORE cancel's post-interrupt claim.
        publish_barrier = threading.Barrier(2)
        # Event: worker sets this after publication, before cleanup.
        published_event = threading.Event()
        # Event: cancel sets this after claim, signaling worker to proceed to cleanup.
        claimed_event = threading.Event()
        # Event: worker sets this after cleanup attempt.
        cleanup_done = threading.Event()

        mock_agent = Mock()
        mock_agent.session_id = session_id

        def _interrupt(_msg):
            # Wait for the worker to publish the notice before returning from
            # interrupt — this forces the notice to be in the map when cancel
            # claims it.
            publish_barrier.wait(timeout=5)
            published_event.wait(timeout=5)

        mock_agent.interrupt = Mock(side_effect=_interrupt)
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

        cancel_result = {}

        def _cancel_thread():
            with patch("api.streaming.get_session", return_value=mock_session):
                cancel_result["result"] = cancel_stream(stream_id)

        def _worker_thread():
            # Simulate _agent_status_callback publishing the notice
            publish_barrier.wait(timeout=5)
            with STREAMS_LOCK:
                _STREAM_FALLBACK_NOTICES[stream_id] = dict(_fb_notice)
            published_event.set()
            # Wait for cancel to claim before cleanup
            claimed_event.wait(timeout=5)
            # Simulate _run_agent_streaming finally block cleanup
            with STREAMS_LOCK:
                _entry = _STREAM_FALLBACK_NOTICES.get(stream_id)
                if _entry is not None and not _entry.get('_cancel_claimed'):
                    _STREAM_FALLBACK_NOTICES.pop(stream_id, None)
            cleanup_done.set()

        cancel_t = threading.Thread(target=_cancel_thread, name="cancel")
        worker_t = threading.Thread(target=_worker_thread, name="worker")
        cancel_t.start()
        worker_t.start()

        # After interrupt returns, cancel claims the notice. We need to signal
        # the worker that the claim happened. But cancel_stream() claims inside
        # its own code — we can't intercept it directly. Instead, the worker
        # waits for claimed_event which we set after a short delay (the claim
        # happens synchronously after interrupt returns, before cancel proceeds
        # to stamping).
        # Actually, we need a different approach: the worker should wait for
        # the claim to happen by polling the map entry.
        # Let's use a simpler approach: the worker waits for claimed_event,
        # and we set it from a thread that polls for _cancel_claimed.
        def _claim_watcher():
            for _ in range(50):
                with STREAMS_LOCK:
                    _entry = _STREAM_FALLBACK_NOTICES.get(stream_id)
                    if _entry is not None and _entry.get('_cancel_claimed'):
                        claimed_event.set()
                        return
                import time as _time
                _time.sleep(0.01)
            claimed_event.set()  # timeout fallback

        watcher_t = threading.Thread(target=_claim_watcher, name="watcher")
        watcher_t.start()

        cancel_t.join(timeout=10)
        worker_t.join(timeout=10)
        watcher_t.join(timeout=10)

        assert cancel_result.get("result") is True, "cancel_stream must return True"
        mock_agent.interrupt.assert_called_once_with("Cancelled by user")

        # Exactly one durable current-turn notice — stamped on the partial or
        # cancel marker, NOT on the prior turn.
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 stamped notice, got {len(stamped)} — "
            "the ownership handoff must produce exactly one durable current-turn notice"
        )
        # Prior turn must NOT be mutated
        assert "_fallbackNotice" not in _prior_assistant, (
            "prior-turn assistant message was mutated — stamping must bind to "
            "the exact current-turn row, not reverse-search prior rows"
        )
        # Final map cleanup — no deadlock, entry popped
        assert stream_id not in _STREAM_FALLBACK_NOTICES, (
            "fallback notice map entry was not cleaned up — cancel's finally "
            "must pop the entry after stamping"
        )

        # The stamped notice must NOT contain the internal _cancel_claimed
        # flag — it is a runtime coordination marker, not session data.
        stamped_notice = stamped[0].get("_fallbackNotice", {})
        assert "_cancel_claimed" not in stamped_notice, (
            "_cancel_claimed leaked into persisted _fallbackNotice — "
            "cancel_stream() must strip the internal flag before stamping."
        )

    def test_cancel_fallback_notice_ownership_handoff_no_partial(self):
        """No-partial marker case: when no partial text exists, the notice
        must be stamped on the newly created cancellation marker (the
        current-turn row), not on a prior turn's assistant message.

        Uses the same ownership handoff but with empty partial text.
        """
        import threading
        from unittest.mock import patch
        from api.streaming import _STREAM_FALLBACK_NOTICES
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "ownership_handoff_no_partial"
        session_id = "sess_ownership_no_partial"

        _fb_notice = {
            "message": "Switched to fallback model: gpt-4 via openai → claude-3 via anthropic",
            "to_model": "claude-3",
            "to_provider": "anthropic",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        # NO partial text — the turn was cancelled before any content streamed.
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

        # The notice must be stamped on the cancellation marker (current-turn row)
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 stamped notice on the cancel marker, got {len(stamped)}"
        )
        # The stamped message must be the cancel marker (has _error=True), not the prior turn
        assert stamped[0].get("_error") is True, (
            "notice was stamped on a non-cancel-marker message — in the no-partial "
            "case it must go on the newly created cancellation marker"
        )
        # Prior turn must NOT be mutated
        assert "_fallbackNotice" not in _prior_assistant, (
            "prior-turn assistant message was mutated in the no-partial case"
        )
        # Final map cleanup
        assert stream_id not in _STREAM_FALLBACK_NOTICES, (
            "fallback notice map entry was not cleaned up in the no-partial case"
        )


    def test_cancel_fallback_notice_strips_cancel_claimed_flag(self):
        """The internal _cancel_claimed ownership-handoff flag must NOT
        leak into the persisted _fallbackNotice metadata.

        cancel_stream() sets _cancel_claimed=True on the dict to coordinate
        with the worker's finally block.  Without stripping it before
        stamping, the flag is saved to the session JSON as dirty metadata.

        This test exercises the same cancel path as the ownership-handoff
        tests but explicitly asserts that the stamped _fallbackNotice dict
        does not contain _cancel_claimed.
        """
        import threading
        from unittest.mock import patch
        from api.streaming import _STREAM_FALLBACK_NOTICES
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "strip_claimed_flag"
        session_id = "sess_strip_claimed"

        _fb_notice = {
            "message": "Switched to fallback model: gpt-4 via openai → claude-3 via anthropic",
            "to_model": "claude-3",
            "to_provider": "anthropic",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial answer"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _STREAM_FALLBACK_NOTICES[stream_id] = dict(_fb_notice)

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

        # The stamped notice must NOT contain the _cancel_claimed flag
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, f"expected 1 stamped notice, got {len(stamped)}"
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice, (
            f"_cancel_claimed leaked into persisted _fallbackNotice: {notice}. "
            "The flag is a runtime coordination marker, not session data — "
            "cancel_stream() must strip it before stamping."
        )
        # The notice must contain the expected fields
        assert notice.get("message") == _fb_notice["message"]
        assert notice.get("to_model") == _fb_notice["to_model"]
        assert notice.get("to_provider") == _fb_notice["to_provider"]
