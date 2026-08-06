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
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        _STREAM_FALLBACK_NOTICES.clear()
        _STREAM_CANCEL_CLAIMED.clear()
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()

    def teardown_method(self):
        """Clean up after each test"""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        ACTIVE_RUNS.clear()
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        _STREAM_FALLBACK_NOTICES.clear()
        _STREAM_CANCEL_CLAIMED.clear()
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

    def test_cancel_fallback_notice_worker_wins_via_cancel_event_with_partial(self):
        """Production-composed test: worker observes flag.set() and finalizes
        the cancel turn before cancel_stream reaches its own stamping block.

        This exercises the adversarial schedule the gate-certifier identified:
        1. Notice is pre-seeded in _STREAM_FALLBACK_NOTICES.
        2. cancel_stream() claims the notice (sets _cancel_claimed) BEFORE
           flag.set() — so the worker's finally won't pop the entry.
        3. flag.set() triggers the worker's _finalize_cancelled_turn, which
           calls _persist_cancelled_turn with stream_id — stamping the notice
           on the cancel marker and clearing active_stream_id.
        4. cancel_stream() later finds active_stream_id is None (worker already
           finalized), so it skips its own stamping — the notice was already
           persisted by the worker.

        Asserts: exactly one durable current-turn notice with clean keys (no
        _cancel_claimed), no prior-turn mutation, final map cleanup.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _persist_cancelled_turn,
        )
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "worker_wins_partial"
        session_id = "sess_worker_wins_partial"

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

        # Mock session that the worker will "finalize" when flag.set() fires.
        # The worker's _persist_cancelled_turn stamps the notice and clears
        # active_stream_id, simulating the worker winning the race.
        _prior_assistant = {
            "role": "assistant",
            "content": "Previous turn.",
            "timestamp": 1000,
        }
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id  # Worker will clear this
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior_assistant]
        mock_session.save = Mock()

        # Mock the cancel flag: when set() is called, simulate the worker
        # observing the event and running _finalize_cancelled_turn
        worker_session_ref = mock_session

        class WorkerCancelEvent(threading.Event):
            def set(self):
                super().set()
                # Worker observes the cancel event and finalizes the turn.
                # _persist_cancelled_turn stamps the notice from
                # _STREAM_FALLBACK_NOTICES and clears active_stream_id.
                _persist_cancelled_turn(
                    worker_session_ref,
                    stream_id=stream_id,
                )
                try:
                    worker_session_ref.save()
                except Exception:
                    pass

        CANCEL_FLAGS[stream_id] = WorkerCancelEvent()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True

        # The worker's _persist_cancelled_turn should have stamped the notice
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 stamped notice, got {len(stamped)} — "
            "the worker's _persist_cancelled_turn must stamp the notice "
            "when it wins the session lock"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice, (
            f"_cancel_claimed leaked into persisted _fallbackNotice: {notice}"
        )
        assert notice.get("message") == _fb_notice["message"]
        assert notice.get("to_model") == _fb_notice["to_model"]
        assert notice.get("to_provider") == _fb_notice["to_provider"]
        # Prior turn must NOT be mutated
        assert "_fallbackNotice" not in _prior_assistant
        # Map entry should be cleaned up by cancel's finally
        assert stream_id not in _STREAM_FALLBACK_NOTICES

    def test_cancel_fallback_notice_worker_wins_via_cancel_event_no_partial(self):
        """Same worker-wins schedule, no partial text → worker stamps on the
        cancel marker it creates in _persist_cancelled_turn.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _persist_cancelled_turn,
        )
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "worker_wins_no_partial"
        session_id = "sess_worker_wins_no_partial"

        _fb_notice = {
            "message": "Switched to fallback model: gpt-4 via openai → claude-3 via anthropic",
            "to_model": "claude-3",
            "to_provider": "anthropic",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = ""
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _STREAM_FALLBACK_NOTICES[stream_id] = dict(_fb_notice)

        _prior_assistant = {
            "role": "assistant",
            "content": "Previous turn.",
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

        worker_session_ref = mock_session

        class WorkerCancelEvent(threading.Event):
            def set(self):
                super().set()
                _persist_cancelled_turn(worker_session_ref, stream_id=stream_id)
                try:
                    worker_session_ref.save()
                except Exception:
                    pass

        CANCEL_FLAGS[stream_id] = WorkerCancelEvent()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True

        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1
        assert stamped[0].get("_error") is True, (
            "notice should be on the cancel marker (has _error=True)"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice
        assert "_fallbackNotice" not in _prior_assistant
        assert stream_id not in _STREAM_FALLBACK_NOTICES

    def test_cancel_fallback_notice_cancel_wins_stamp_on_partial(self):
        """Cancel-wins schedule: cancel_stream reaches its stamping block
        before the worker finalizes. The notice is stamped on the partial row.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "cancel_wins_partial"
        session_id = "sess_cancel_wins_partial"

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
        AGENT_INSTANCES[stream_id] = mock_agent

        _prior_assistant = {
            "role": "assistant",
            "content": "Previous turn.",
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
        assert len(stamped) == 1
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice
        assert notice.get("message") == _fb_notice["message"]
        assert "_fallbackNotice" not in _prior_assistant
        assert stream_id not in _STREAM_FALLBACK_NOTICES

    def test_cancel_fallback_notice_dedup_stamps_durable_row_by_signature(self):
        """When the partial is deduplicated (already present in _cs.messages),
        the fallback notice must be stamped on the EXACT equivalent durable
        row found by signature — not the detached _partial_msg candidate,
        and not the first _partial found.

        Verifies gate-certifier finding #1: save/reload retains the notice
        when the partial was deduplicated.  Includes a second _partial with
        different content to prove the signature match (not just "first
        _partial") is what locates the correct row.
        """
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "dedup_stamp"
        session_id = "sess_dedup_stamp"

        _fb_notice = {
            "message": "Switched to fallback model: gpt-4 via openai → claude-3 via anthropic",
            "to_model": "claude-3",
            "to_provider": "anthropic",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "dedup partial answer"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _STREAM_FALLBACK_NOTICES[stream_id] = dict(_fb_notice)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        # Pre-existing partial with DIFFERENT content — proves signature matching
        # (not just "first _partial") is needed.
        _other_partial = {
            "role": "assistant",
            "content": "different earlier partial",
            "_partial": True,
            "timestamp": 999,
        }
        # The equivalent durable row (same content as the dedup candidate)
        _durable_partial = {
            "role": "assistant",
            "content": "dedup partial answer",
            "_partial": True,
            "timestamp": 1000,
        }
        _user_msg = {"role": "user", "content": "q", "timestamp": 998}

        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_user_msg, _other_partial, _durable_partial]

        # Use a real temp file for save/reload verification
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            _session_path = f.name
            f.write(json.dumps({
                "session_id": session_id,
                "messages": [_user_msg, _other_partial, _durable_partial],
            }))

        def _real_save():
            # Simulate save: serialize messages to the temp file
            data = {"session_id": session_id, "messages": mock_session.messages}
            Path(_session_path).write_text(json.dumps(data))

        mock_session.save = Mock(side_effect=_real_save)
        mock_session.path = _session_path

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result is True

        # Verify the notice was stamped on the DURABLE row (not _other_partial)
        assert "_fallbackNotice" not in _other_partial, (
            "notice was stamped on the wrong _partial (first found, not signature-matched)"
        )
        assert "_fallbackNotice" in _durable_partial, (
            "notice was NOT stamped on the signature-matched durable row — "
            "stamping the detached candidate does not survive s.save()"
        )
        notice = _durable_partial["_fallbackNotice"]
        assert notice.get("message") == _fb_notice["message"]
        assert "_cancel_claimed" not in notice

        # Verify save/reload retains the notice
        reloaded = json.loads(Path(_session_path).read_text())
        reloaded_msgs = reloaded["messages"]
        stamped_on_reload = [
            m for m in reloaded_msgs
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped_on_reload) == 1, (
            f"expected exactly 1 notice after reload, got {len(stamped_on_reload)}"
        )
        Path(_session_path).unlink(missing_ok=True)
        assert stream_id not in _STREAM_FALLBACK_NOTICES

    def test_cancel_fallback_notice_late_publication_stamps_after_claim(self):
        """Late publication: no notice exists when cancel claims, but one is
        published AFTER the claim and BEFORE cancel's save.  Cancel must
        stamp the late notice on the current-turn row so it survives reload.

        This replaces the prior test that asserted no stamp (the loss).
        The gate-certifier required: "The submitted late publication test
        still does not publish after the claim and explicitly expects no
        stamp."  Now it publishes and asserts survival.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "late_publication_v2"
        session_id = "sess_late_publication_v2"

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
        # NO notice pre-seeded — it will be published AFTER cancel claims

        mock_agent = Mock()
        mock_agent.session_id = session_id
        # When interrupt() is called, simulate the callback publishing a
        # late fallback notice (as if the agent emitted a confirmed switch
        # between cancel's claim and cancel's save).
        _late_notice = dict(_fb_notice)
        def _interrupt_handler(_msg):
            # Late publication: the notice arrives after cancel claimed
            # but before cancel reaches its save/stamp block.
            from api.streaming import STREAMS_LOCK
            with STREAMS_LOCK:
                _STREAM_FALLBACK_NOTICES[stream_id] = _late_notice
        mock_agent.interrupt = Mock(side_effect=_interrupt_handler)
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

        # The late-published notice MUST be stamped on the current-turn row.
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"late-published notice should be stamped, got {len(stamped)} stamped rows — "
            "cancel must check _STREAM_FALLBACK_NOTICES at stamp time, not just the claimed notice"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice
        assert notice.get("message") == _fb_notice["message"]
        assert notice.get("to_model") == _fb_notice["to_model"]
        assert notice.get("to_provider") == _fb_notice["to_provider"]
        # Map and claim must be cleaned up (cannot grow unbounded)
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_CANCEL_CLAIMED

    def test_cancel_claim_retired_when_agent_and_session_identity_absent(self):
        """Production cancel_stream() test for the stream-present/agent-not-yet-
        ready path: stream + cancel flag are present, but the agent (and thus
        session identity) is NOT yet available.  cancel_stream() must return
        True, and BOTH registries (_STREAM_FALLBACK_NOTICES and
        _STREAM_CANCEL_CLAIMED) must be empty at return — the claim retirement
        must live in an outer try/finally covering every path after the claim,
        not only the inner ``if _cancel_session_id:`` block
        (gate-certifier blocker #2).
        """
        import threading
        from unittest.mock import Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "claim_retired_no_identity"
        session_id = None  # agent/session identity NOT available

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        # No AGENT_INSTANCES[stream_id] — agent not yet ready
        STREAM_PARTIAL_TEXT[stream_id] = ""
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        # No notice pre-seeded — the claim adds stream_id to the set anyway
        # (late publication ownership)

        result = cancel_stream(stream_id)

        # The supported stream-present/agent-not-yet-ready path returns True
        assert result is True, (
            "cancel_stream() should return True for stream-present/agent-not-ready path"
        )

        # Both registries MUST be empty at return — the outer try/finally
        # retirement must fire even when _cancel_session_id is falsy.
        assert stream_id not in _STREAM_CANCEL_CLAIMED, (
            f"_STREAM_CANCEL_CLAIMED still contains {stream_id!r} after "
            "cancel_stream() returned — claim retirement must cover the "
            "missing-session-identity path"
        )
        assert stream_id not in _STREAM_FALLBACK_NOTICES, (
            f"_STREAM_FALLBACK_NOTICES still contains {stream_id!r} after "
            "cancel_stream() returned — map must be cleaned on every path"
        )

    def test_cancel_replacement_publication_B_overwrites_A_at_commit(self):
        """Barriered A-at-claim then B-before-save test asserting B is the sole
        durable notice and both registries are empty at return
        (gate-certifier blocker #3: stale-first and lossy).

        Schedule:
        1. Notice A is pre-seeded in _STREAM_FALLBACK_NOTICES.
        2. cancel_stream() claims the notice (A) under STREAMS_LOCK.
        3. interrupt() triggers the status callback to publish newer notice B
           into _STREAM_FALLBACK_NOTICES (replacing A) BEFORE cancel reaches
           its save/stamp block.
        4. cancel_stream() re-reads the CURRENT generation at commit time
           (under lock) and stamps B — not the stale A snapshot.

        Asserts: exactly one durable notice (B, not A), B has clean keys,
        both registries empty at return.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "replacement_b_wins"
        session_id = "sess_replacement_b_wins"

        # Notice A: exists at claim time
        _notice_a = {
            "message": "Switched to fallback model: gpt-4 via openai → m1 via p1",
            "to_model": "m1",
            "to_provider": "p1",
        }
        # Notice B: published AFTER claim, BEFORE save — must win
        _notice_b = {
            "message": "Switched to fallback model: gpt-4 via openai → m2 via p2",
            "to_model": "m2",
            "to_provider": "p2",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial answer"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _STREAM_FALLBACK_NOTICES[stream_id] = dict(_notice_a)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        # When interrupt() is called, publish the NEWER notice B (replacing A)
        def _interrupt_handler(_msg):
            from api.streaming import STREAMS_LOCK
            with STREAMS_LOCK:
                _STREAM_FALLBACK_NOTICES[stream_id] = dict(_notice_b)
        mock_agent.interrupt = Mock(side_effect=_interrupt_handler)
        AGENT_INSTANCES[stream_id] = mock_agent

        _prior_assistant = {
            "role": "assistant",
            "content": "Previous turn.",
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

        # Exactly one durable notice — and it must be B (the latest), NOT A.
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 durable notice, got {len(stamped)}"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert "_cancel_claimed" not in notice, (
            f"_cancel_claimed leaked into persisted notice: {notice}"
        )
        # B is the sole durable notice — A must NOT be persisted
        assert notice.get("message") == _notice_b["message"], (
            f"stale notice A persisted instead of latest B: {notice}"
        )
        assert notice.get("to_model") == _notice_b["to_model"]
        assert notice.get("to_provider") == _notice_b["to_provider"]
        assert set(notice.keys()) == {"message", "to_model", "to_provider"}

        # Prior turn must NOT be mutated
        assert "_fallbackNotice" not in _prior_assistant

        # Both registries must be empty at return
        assert stream_id not in _STREAM_FALLBACK_NOTICES, (
            "_STREAM_FALLBACK_NOTICES not cleaned after replacement publication"
        )
        assert stream_id not in _STREAM_CANCEL_CLAIMED, (
            "_STREAM_CANCEL_CLAIMED not retired after replacement publication"
        )
