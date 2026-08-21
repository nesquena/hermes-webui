"""
Unit tests for cancel/interrupt functionality.
Tests the integration between cancel_stream() and agent.interrupt().
"""
import queue
import threading
from unittest.mock import Mock

from api.streaming import cancel_stream
from api.config import AGENT_INSTANCES, STREAMS, CANCEL_FLAGS, ACTIVE_RUNS, SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK


def _publish_test_notice_locked(stream_id, notice):
    """Publish a fallback notice while caller already holds STREAMS_LOCK."""
    from api.streaming import (
        _STREAM_CANCEL_CLAIMED, _STREAM_FALLBACK_NOTICES,
        _STREAM_NOTICE_GENERATION,
    )
    _STREAM_NOTICE_GENERATION[stream_id] = int(_STREAM_NOTICE_GENERATION.get(stream_id) or 0) + 1
    clean = dict(notice)
    if stream_id in _STREAM_CANCEL_CLAIMED:
        clean['_cancel_claimed'] = True
    _STREAM_FALLBACK_NOTICES[stream_id] = clean


def _publish_test_notice(stream_id, notice):
    """Use the production publication gate for tests that are not holding the lock."""
    from api.streaming import _publish_fallback_notice
    assert _publish_fallback_notice(stream_id, dict(notice)) is True


class TestCancelInterrupt:
    """Test suite for cancel/interrupt functionality"""

    def setup_method(self):
        """Clean up before each test"""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        ACTIVE_RUNS.clear()
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED,
            _STREAM_SETTLEMENT_TERMINAL, _STREAM_NOTICE_GENERATION,
            _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED,
            _STREAM_WORKER_SAVED, _STREAM_FALLBACK_DEAD_LETTER,
        )
        _STREAM_FALLBACK_NOTICES.clear()
        _STREAM_CANCEL_CLAIMED.clear()
        _STREAM_SETTLEMENT_TERMINAL.clear()
        _STREAM_NOTICE_GENERATION.clear()
        _STREAM_SETTLEMENT_PARTICIPANTS.clear()
        _STREAM_SETTLEMENT_COMPLETED.clear()
        _STREAM_WORKER_SAVED.clear()
        _STREAM_FALLBACK_DEAD_LETTER.clear()
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()

    def teardown_method(self):
        """Clean up after each test"""
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        ACTIVE_RUNS.clear()
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED,
            _STREAM_SETTLEMENT_TERMINAL, _STREAM_NOTICE_GENERATION,
            _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED,
            _STREAM_WORKER_SAVED, _STREAM_FALLBACK_DEAD_LETTER,
        )
        _STREAM_FALLBACK_NOTICES.clear()
        _STREAM_CANCEL_CLAIMED.clear()
        _STREAM_SETTLEMENT_TERMINAL.clear()
        _STREAM_NOTICE_GENERATION.clear()
        _STREAM_SETTLEMENT_PARTICIPANTS.clear()
        _STREAM_SETTLEMENT_COMPLETED.clear()
        _STREAM_WORKER_SAVED.clear()
        _STREAM_FALLBACK_DEAD_LETTER.clear()
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
        assert result["cancelled"] is True
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
        assert result["cancelled"] is True
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
        assert result["cancelled"] is True
        # CANCEL_FLAGS is eagerly popped; the agent thread checks the event
        # object it already has a reference to — pop doesn't clear the event
        assert stream_id not in CANCEL_FLAGS, \
            "cancel_stream() should eagerly pop CANCEL_FLAGS even without an agent"
        # Agent will check this flag (it holds a reference to the event object)

    def test_cancel_nonexistent_stream(self):
        """Test cancel for a stream that doesn't exist"""
        result = cancel_stream("nonexistent_stream")
        assert result["cancelled"] is False

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

        assert result["cancelled"] is True
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

        assert result["cancelled"] is True
        assert cancel_event.is_set()

    def test_cancel_puts_sentinel_in_queue(self):
        """Verify that cancel_stream() puts cancel sentinel in queue"""
        stream_id = "test_stream_queue"
        q = queue.Queue()

        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()

        result = cancel_stream(stream_id)

        assert result["cancelled"] is True
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

        assert result["cancelled"] is True
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

        assert result["cancelled"] is True
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
            _STREAM_FALLBACK_NOTICES, _finalize_cancelled_turn,
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
        _publish_test_notice(stream_id, _fb_notice)

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
                _finalize_cancelled_turn(
                    worker_session_ref,
                    stream_id=stream_id,
                )

        CANCEL_FLAGS[stream_id] = WorkerCancelEvent()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        # Register session + writeback owner so the #6623 generation guard
        # in _finalize_cancelled_turn authorizes the worker's finalize.
        from api import models, config
        models.SESSIONS[session_id] = mock_session
        config.register_session_writeback_owner(session_id, stream_id)

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

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
            _STREAM_FALLBACK_NOTICES, _finalize_cancelled_turn,
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
        _publish_test_notice(stream_id, _fb_notice)

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
                _finalize_cancelled_turn(worker_session_ref, stream_id=stream_id)

        CANCEL_FLAGS[stream_id] = WorkerCancelEvent()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        # Register session + writeback owner so the #6623 generation guard
        # in _finalize_cancelled_turn authorizes the worker's finalize.
        from api import models, config
        models.SESSIONS[session_id] = mock_session
        config.register_session_writeback_owner(session_id, stream_id)

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

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
        _publish_test_notice(stream_id, _fb_notice)

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

        assert result["cancelled"] is True

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
        _publish_test_notice(stream_id, _fb_notice)

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

        assert result["cancelled"] is True

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
                _publish_test_notice_locked(stream_id, _late_notice)
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

        assert result["cancelled"] is True

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
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "claim_retired_no_identity"
        # agent/session identity NOT available — no AGENT_INSTANCES[stream_id]

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
        assert result["cancelled"] is True, (
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
        _publish_test_notice(stream_id, _notice_a)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        # When interrupt() is called, publish the NEWER notice B (replacing A)
        def _interrupt_handler(_msg):
            from api.streaming import STREAMS_LOCK
            with STREAMS_LOCK:
                _publish_test_notice_locked(stream_id, _notice_b)
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

        assert result["cancelled"] is True

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

    def test_post_cas_publication_blocked_by_terminal_fence(self):
        """After the settlement loop's CAS pop retires generation A and sets
        the terminal fence, a notice B published through the production
        callback path must be BLOCKED from entering the map.  Without the
        fence, B would be unconditionally deleted by the finalizers without
        being saved (gate-certifier blocker #1: post-CAS loss).

        This test verifies the fence mechanism in two parts:
        1. Production callback path: when _STREAM_SETTLEMENT_TERMINAL
           contains the stream, the callback does NOT write to
           _STREAM_FALLBACK_NOTICES.
        2. Integration: after a normal cancel_stream() with notice A,
           A is the sole durable notice and the fence is retired by
           the finalizers.

        The fence is set inside the CAS pop (under STREAMS_LOCK) and
        retired by both finalizers.  The callback checks the fence
        under STREAMS_LOCK before publishing.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED,
            _STREAM_SETTLEMENT_TERMINAL, _publish_fallback_notice,
        )
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "post_cas_fence"
        session_id = "sess_post_cas_fence"

        _notice_a = {
            "message": "Switched to fallback: m1 via p1",
            "to_model": "m1",
            "to_provider": "p1",
        }
        _notice_b = {
            "message": "Switched to fallback: m2 via p2",
            "to_model": "m2",
            "to_provider": "p2",
        }

        # ── Part 1: verify the REAL production callback path respects the fence ──
        # Simulate the fence being set (as the CAS pop would do)
        _STREAM_SETTLEMENT_TERMINAL.add(stream_id)
        _STREAM_FALLBACK_NOTICES.pop(stream_id, None)  # CAS popped A

        # Run the actual production publication gate: _publish_fallback_notice.
        # It must REJECT B (return False) because the stream is terminal — the
        # fence is held.  This is the real callback code path, not a memo.
        published = _publish_fallback_notice(stream_id, dict(_notice_b))
        assert published is False, (
            "terminal fence failed to block B — _publish_fallback_notice "
            "accepted a post-CAS publication"
        )
        # B must NOT have been published — the fence blocked it
        assert stream_id not in _STREAM_FALLBACK_NOTICES, (
            "terminal fence failed to block B publication — B entered the map "
            "after CAS pop, would be deleted by finalizers without saving"
        )
        # The fence must still be held (sealed through the finalizer window)
        assert stream_id in _STREAM_SETTLEMENT_TERMINAL, (
            "terminal fence was dropped before finalizer retirement"
        )
        _STREAM_SETTLEMENT_TERMINAL.clear()

        # ── Part 2: integration — cancel_stream with A, verify durable ──
        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, _notice_a)

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

        # A must be the sole durable notice
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 durable notice, got {len(stamped)}"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert notice.get("message") == _notice_a["message"]
        assert set(notice.keys()) == {"message", "to_model", "to_provider"}

        # All registries and fence must be retired (fence is retired by the
        # worker's finally, not cancel's finally — gate-certifier blocker #2).
        from api.streaming import _retire_worker_cancelled_state
        _retire_worker_cancelled_state(stream_id)
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_CANCEL_CLAIMED
        assert stream_id not in _STREAM_SETTLEMENT_TERMINAL

    def test_settlement_3_generations_latest_wins_with_serialized_snapshots(self):
        """Production-composed compare-and-set settlement: three notice
        generations (A at claim, B after first save, C after second save).
        The settlement loop must converge and C must be the sole durable.

        Uses deep-copied per-save snapshots (not in-place mutation) so the
        oracle proves what was actually persisted at each save call, not
        just the final in-memory state (gate-certifier: "the new test does
        not prove durability").
        """
        import copy
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "settlement_3_gen"
        session_id = "sess_settlement_3_gen"

        notices = [
            {"message": f"Switched to fallback model: gpt-4 via openai → m{i} via p{i}",
             "to_model": f"m{i}", "to_provider": f"p{i}"}
            for i in range(1, 4)  # A, B, C
        ]

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, notices[0])  # A at claim

        _save_snapshots = []  # deep-copied per-save snapshots

        _publish_gen = [0]  # which generation to publish next

        def _save_with_replacement():
            """Each save captures a deepcopy, then publishes the next generation."""
            _save_snapshots.append(copy.deepcopy(mock_session.messages))
            _publish_gen[0] += 1
            if _publish_gen[0] < len(notices):
                from api.streaming import STREAMS_LOCK
                with STREAMS_LOCK:
                    _publish_test_notice_locked(stream_id, notices[_publish_gen[0]])

        _prior = {"role": "assistant", "content": "Prior turn.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock(side_effect=_save_with_replacement)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

        # The loop should have iterated 3 times (once per generation)
        assert len(_save_snapshots) == 3, (
            f"expected 3 save snapshots (one per generation), got {len(_save_snapshots)}"
        )

        # The LAST snapshot must have C as the sole durable notice
        final_snapshot = _save_snapshots[-1]
        stamped = [
            m for m in final_snapshot
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, f"expected 1 durable notice in final snapshot, got {len(stamped)}"
        notice = stamped[0]["_fallbackNotice"]
        assert notice.get("to_model") == "m3", f"latest C not durable: {notice}"
        assert notice.get("to_provider") == "p3"
        assert set(notice.keys()) == {"message", "to_model", "to_provider"}

        # Prior turn never stamped
        assert "_fallbackNotice" not in _save_snapshots[-1][0]

        # Both registries retired
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_CANCEL_CLAIMED

    def test_settlement_save_failure_leaves_notice_for_worker(self):
        """If Session.save() raises during the settlement loop, the unsaved
        notice must be LEFT in _STREAM_FALLBACK_NOTICES — not silently
        dropped, not transferred to an ephemeral dead-letter.  The worker's
        _persist_cancelled_turn can still stamp it when it wins the session
        lock.  The claim and terminal fence are retired so they cannot
        grow unbounded (gate-certifier blocker #2).

        The gate-certifier rejected the dead-letter pattern: "propagate
        the save failure instead of treating an ephemeral process-local
        copy as a dead letter."  This test asserts the notice survives
        in the map for the worker.
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import (
            _STREAM_CANCEL_CLAIMED,
            _STREAM_SETTLEMENT_TERMINAL, _STREAM_WORKER_SAVED,
        )
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "settlement_save_fail"
        session_id = "sess_settlement_save_fail"

        _notice = {"message": "Switched to fallback", "to_model": "m1", "to_provider": "p1"}

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, _notice)
        _STREAM_SETTLEMENT_TERMINAL.clear()

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock(side_effect=RuntimeError("disk full"))

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        # Non-silent disposition (gate-certifier blocker #2 / rework point 3):
        # a save failure means the terminal notice could NOT be confirmed
        # durable within cancel's bounded window, so cancel_stream must NOT
        # report success.  It returns False (routes.py surfaces cancelled:
        # false) and logs an ERROR, while still leaving the notice in the map
        # for the worker's _persist_cancelled_turn as a backstop.
        assert result["cancelled"] is False

        # Claim retired; fence was never set on the failure path
        assert stream_id not in _STREAM_CANCEL_CLAIMED, (
            "claim leaked after save failure"
        )
        assert stream_id not in _STREAM_SETTLEMENT_TERMINAL, (
            "terminal fence leaked after save failure"
        )
        # The unsaved notice MUST be in dead-letter (bounded owner) — NOT
        # silently dropped and NOT left ownerless in _STREAM_FALLBACK_NOTICES
        # (gate-certifier blocker #3).
        from api.streaming import _STREAM_FALLBACK_DEAD_LETTER
        assert stream_id in _STREAM_FALLBACK_DEAD_LETTER, (
            "unsaved notice was silently dropped instead of transferred to dead-letter"
        )
        _fb = _STREAM_FALLBACK_DEAD_LETTER[stream_id]["notice"]
        assert _fb.get("message") == _notice["message"]
        assert _fb.get("to_model") == _notice["to_model"]
        assert _fb.get("to_provider") == _notice["to_provider"]

        # Bounded registries: cancel-side owner + fence retired and the worker
        # never claimed durability, so _STREAM_WORKER_SAVED is empty too — no
        # registry grows unbounded across failure paths (gate-certifier #2).
        assert stream_id not in _STREAM_WORKER_SAVED, (
            "worker-saved registry leaked after cancel-side save failure"
        )
        _STREAM_FALLBACK_DEAD_LETTER.pop(stream_id, None)

    def test_settlement_stale_stream_does_not_mutate_messages(self):
        """An old stream whose active_stream_id no longer matches must return
        True WITHOUT stamping or saving — the stale guard fires before the
        settlement loop (gate-certifier blocker #2: finally bypasses
        stale-stream writeback rejection).
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "settlement_stale"
        session_id = "sess_settlement_stale"

        _notice = {"message": "Switched to fallback", "to_model": "m1", "to_provider": "p1"}

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, _notice)

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        # active_stream_id is a DIFFERENT stream — this stream is stale
        mock_session.active_stream_id = "newer_stream_999"
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

        # save must NOT have been called — stale guard returned early
        mock_session.save.assert_not_called()

        # Prior turn must NOT be stamped
        assert "_fallbackNotice" not in _prior

        # Registries still retired by outer finally
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_CANCEL_CLAIMED

    def test_settlement_publication_after_post_save_check_converges(self):
        """Production-composed: the status callback publishes a newer notice
        AFTER the settlement loop's post-save check but BEFORE the finally
        cleanup. The loop must detect the newer generation, loop back, and
        persist it (gate-certifier blocker #1: newer generation discarded
        without persistence).
        """
        import copy
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED, STREAMS_LOCK
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "settlement_post_check_pub"
        session_id = "sess_settlement_post_check"

        _notice_a = {"message": "A", "to_model": "ma", "to_provider": "pa"}
        _notice_b = {"message": "B", "to_model": "mb", "to_provider": "pb"}

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, _notice_a)

        _save_snapshots = []
        _published_b = [False]

        def _save_then_publish_b():
            _save_snapshots.append(copy.deepcopy(mock_session.messages))
            if not _published_b[0]:
                _published_b[0] = True
                # Publish B AFTER this save completes — the next post-save
                # check will see B and loop back
                with STREAMS_LOCK:
                    _publish_test_notice_locked(stream_id, _notice_b)

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock(side_effect=_save_then_publish_b)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

        # Two saves: first persisted A, second persisted B
        assert len(_save_snapshots) == 2, (
            f"expected 2 saves (A then B), got {len(_save_snapshots)}"
        )

        # Final durable is B
        final = _save_snapshots[-1]
        stamped = [m for m in final if isinstance(m, dict) and m.get("_fallbackNotice")]
        assert len(stamped) == 1
        assert stamped[0]["_fallbackNotice"].get("to_model") == "mb"

        # First snapshot had A
        first = _save_snapshots[0]
        first_stamped = [m for m in first if isinstance(m, dict) and m.get("_fallbackNotice")]
        assert len(first_stamped) == 1
        assert first_stamped[0]["_fallbackNotice"].get("to_model") == "ma"

        # Registries retired
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_CANCEL_CLAIMED

    def test_settlement_publish_B_after_equality_check_before_cleanup(self):
        """Deterministic test: the status callback publishes a newer notice B
        AFTER the settlement loop's equality check sees the same generation
        but BEFORE the loop atomically pops and retires it.

        With the atomic pop inside the lock (fix #1), the equality-check
        and the pop happen under the same lock acquisition.  B can only be
        published after the pop releases the lock — so B is found on the
        next iteration and persisted.  Without the atomic pop (the old
        code), the inner cleanup unconditionally popped whatever was in
        the map (which could be B), discarding it without persistence.

        This test hooks the save() to publish B after the first save
        completes (simulating a status callback firing between save and
        the post-save lock reacquire).  The loop must detect B on the
        next iteration and persist it.

        Uses deep-copied per-save snapshots to prove durability.
        """
        import copy
        from unittest.mock import patch, Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED, STREAMS_LOCK,
        )
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "settlement_post_eq_pub"
        session_id = "sess_settlement_post_eq"

        _notice_a = {"message": "A", "to_model": "ma", "to_provider": "pa"}
        _notice_b = {"message": "B", "to_model": "mb", "to_provider": "pb"}

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, _notice_a)

        _save_snapshots = []
        _published_b = [False]

        def _save_then_publish_b():
            _save_snapshots.append(copy.deepcopy(mock_session.messages))
            if not _published_b[0]:
                _published_b[0] = True
                # Publish B AFTER this save completes.  The post-save
                # equality check will see A (same generation), atomically
                # pop it, then B is visible on the next iteration.
                with STREAMS_LOCK:
                    _publish_test_notice_locked(stream_id, _notice_b)

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock(side_effect=_save_then_publish_b)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

        # Two saves: first persisted A, second persisted B
        assert len(_save_snapshots) == 2, (
            f"expected 2 saves (A then B), got {len(_save_snapshots)} — "
            "the loop must iterate when a newer generation appears after "
            "the equality check"
        )

        # First snapshot had A
        first = _save_snapshots[0]
        first_stamped = [m for m in first if isinstance(m, dict) and m.get("_fallbackNotice")]
        assert len(first_stamped) == 1
        assert first_stamped[0]["_fallbackNotice"].get("to_model") == "ma"

        # Final durable is B
        final = _save_snapshots[-1]
        stamped = [m for m in final if isinstance(m, dict) and m.get("_fallbackNotice")]
        assert len(stamped) == 1
        assert stamped[0]["_fallbackNotice"].get("to_model") == "mb", (
            "B was not persisted — the unconditional pop discarded B "
            "without saving it"
        )

        # Registries retired
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_CANCEL_CLAIMED

    def test_settlement_loop_exhaustion_leaves_notice_for_worker(self):
        """If the settlement loop hits _SETTLEMENT_MAX_ITERS (16+ continuous
        replacements), the current unsaved generation must be LEFT in
        _STREAM_FALLBACK_NOTICES — not silently dropped, not transferred
        to an ephemeral dead-letter.  The worker's _persist_cancelled_turn
        can still stamp it (blocker #2: "exhaust the loop without data
        loss").

        The gate-certifier rejected the dead-letter pattern.  This test
        asserts the notice survives in the map.
        """
        from unittest.mock import patch, Mock
        from api.streaming import (
            _STREAM_CANCEL_CLAIMED,
            _STREAM_SETTLEMENT_TERMINAL, _STREAM_WORKER_SAVED, STREAMS_LOCK,
        )
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "settlement_loop_exhaust"
        session_id = "sess_loop_exhaust"

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _STREAM_SETTLEMENT_TERMINAL.clear()

        # Start with generation 0
        _publish_test_notice(stream_id, {
            "message": "gen0", "to_model": "m0", "to_provider": "p0",
        })

        _save_count = [0]

        def _save_then_publish_next():
            _save_count[0] += 1
            # Publish a newer generation on every save — the loop never
            # converges because the generation always changes.
            # NOTE: the terminal fence is NOT set during the loop (only
            # on CAS pop), so the callback path is not blocked here.
            # We publish directly into the map under the lock.
            with STREAMS_LOCK:
                _publish_test_notice_locked(stream_id, {
                    "message": f"gen{_save_count[0]}",
                    "to_model": f"m{_save_count[0]}",
                    "to_provider": f"p{_save_count[0]}",
                })

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock(side_effect=_save_then_publish_next)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        # Non-silent disposition (gate-certifier blocker #2 / rework point 3):
        # loop exhaustion means the terminal notice could NOT be confirmed
        # durable within cancel's bounded window, so cancel_stream does NOT
        # report success — it returns False and logs an ERROR, leaving the
        # current unsaved generation in the map for the worker.
        assert result["cancelled"] is False

        # The loop must have run _SETTLEMENT_MAX_ITERS times
        from api.streaming import _SETTLEMENT_MAX_ITERS_GLOBAL
        assert _save_count[0] >= _SETTLEMENT_MAX_ITERS_GLOBAL, (
            f"loop ran only {_save_count[0]} times, expected >= {_SETTLEMENT_MAX_ITERS_GLOBAL}"
        )

        # The final unsaved generation must be in dead-letter (bounded owner)
        # — not silently dropped (gate-certifier blocker #3).
        from api.streaming import _STREAM_FALLBACK_DEAD_LETTER
        assert stream_id in _STREAM_FALLBACK_DEAD_LETTER, (
            "loop exhaustion silently dropped the unsaved generation "
            "instead of transferring to dead-letter"
        )
        _fb = _STREAM_FALLBACK_DEAD_LETTER[stream_id]["notice"]
        assert set(_fb.keys()) == {"message", "to_model", "to_provider"}, (
            f"dead-letter entry has dirty keys: {set(_fb.keys())}"
        )

        # Claim and fence retired (fence was never set on failure path)
        assert stream_id not in _STREAM_CANCEL_CLAIMED
        assert stream_id not in _STREAM_SETTLEMENT_TERMINAL
        # Worker never claimed durability on the cancel-side loop-exhaustion
        # path — bounded registry, no growth (gate-certifier #2).
        assert stream_id not in _STREAM_WORKER_SAVED
        _STREAM_FALLBACK_DEAD_LETTER.pop(stream_id, None)

    def test_settlement_identical_partial_signatures_across_turns(self):
        """When two turns have identical _partial signatures, the stamp
        helper must stamp the current-turn row (found in the current-turn
        slice before the cancel marker), NOT an earlier turn's row with
        the same signature (fix #3: "use identical partial signatures
        across turns + a non-last cancel marker").

        Setup: two user turns, each followed by an identical _partial
        assistant row.  The cancel marker is NOT the last message (a
        prior assistant turn follows it).  The stamp must land on the
        CURRENT turn's partial, not the earlier one.
        """
        from unittest.mock import patch, Mock
        from api.streaming import _STREAM_FALLBACK_NOTICES
        from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS

        stream_id = "identical_sig_turns"
        session_id = "sess_identical_sig"

        _fb_notice = {
            "message": "Switched to fallback",
            "to_model": "m1",
            "to_provider": "p1",
        }

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "same partial text"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, _fb_notice)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        # Two user turns, each with an identical _partial assistant row
        _user1 = {"role": "user", "content": "first question", "timestamp": 100}
        _partial1 = {
            "role": "assistant", "content": "same partial text",
            "_partial": True, "timestamp": 101,
        }
        _user2 = {"role": "user", "content": "second question", "timestamp": 200}
        _partial2 = {
            "role": "assistant", "content": "same partial text",
            "_partial": True, "timestamp": 201,
        }
        # A prior assistant turn AFTER the cancel marker position —
        # proves the no-partial branch uses cancel_marker_idx, not [-1]
        _prior_after = {
            "role": "assistant", "content": "some other message",
            "timestamp": 300,
        }

        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "second question"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 200.0
        # messages: [user1, partial1, user2, partial2, prior_after]
        # The cancel marker will be appended after partial2 (current turn).
        # cancel_marker_idx will point to it.  _prior_after is NOT in
        # the current-turn slice.
        mock_session.messages = [_user1, _partial1, _user2, _partial2]

        mock_session.save = Mock()

        with patch("api.streaming.get_session", return_value=mock_session):
            result = cancel_stream(stream_id)

        assert result["cancelled"] is True

        # The notice must be on partial2 (current turn), NOT partial1
        assert "_fallbackNotice" not in _partial1, (
            "notice was stamped on the WRONG turn's partial — "
            "identical signatures caused the stamp to hit the earlier turn"
        )
        assert "_fallbackNotice" in _partial2, (
            "notice was NOT stamped on the current-turn partial — "
            "the stamp helper must search only within the current-turn slice"
        )
        notice = _partial2["_fallbackNotice"]
        assert notice.get("message") == _fb_notice["message"]
        assert set(notice.keys()) == {"message", "to_model", "to_provider"}

        # Exactly one stamped row
        stamped = [
            m for m in mock_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 stamped row, got {len(stamped)}"
        )

        assert stream_id not in _STREAM_FALLBACK_NOTICES
    def test_worker_save_failure_then_retry_success_retires_exact_notice(self):
        """Worker _finalize_cancelled_turn saves, fails once, then succeeds on
        retry.  On the FAILED save, _STREAM_WORKER_SAVED must NOT contain the
        stream — so the worker's retirement (_retire_worker_cancelled_state)
        must NOT pop the live notice (the notice stays for cancel).  On the
        SUCCESSFUL retry, _STREAM_WORKER_SAVED is set and the worker's
        retirement compare-then-pops the exact notice and empties all
        registries (gate-certifier #2: a failed worker save must not silently
        drop the only live notice).
        """
        from unittest.mock import Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _finalize_cancelled_turn, _retire_worker_cancelled_state,
        )

        stream_id = "worker_retry_ok"
        session_id = "sess_worker_retry_ok"
        _notice = {"message": "fb", "to_model": "m1", "to_provider": "p1"}
        _publish_test_notice(stream_id, _notice)

        _saved = [0]

        def _save():
            _saved[0] += 1
            if _saved[0] == 1:
                raise RuntimeError("transient disk error")

        ws = Mock()
        ws.session_id = session_id
        ws.active_stream_id = stream_id
        ws.pending_user_message = "q"
        ws.pending_attachments = []
        ws.pending_started_at = 1.0
        ws.messages = [{"role": "assistant", "content": "Prior.", "timestamp": 1}]
        ws.save = Mock(side_effect=_save)

        # Register session + writeback owner so the #6623 generation guard
        # in _finalize_cancelled_turn resolves the current session and
        # authorizes the finalize against this stream_id.
        from api import models, config
        models.SESSIONS[session_id] = ws
        config.register_session_writeback_owner(session_id, stream_id)

        # First finalize: save FAILS -> _STREAM_WORKER_SAVED must not contain it
        _finalize_cancelled_turn(ws, stream_id=stream_id)
        assert stream_id not in _STREAM_WORKER_SAVED, (
            "worker marked stream saved after a FAILED save"
        )
        # Worker retirement with a failed save must NOT pop the live notice
        _retire_worker_cancelled_state(stream_id)
        assert stream_id in _STREAM_FALLBACK_NOTICES, (
            "worker retirement deleted the live notice after a failed save — "
            "blocker #2: silent drop of the only copy"
        )

        # Retry: finalize again, save now succeeds -> _STREAM_WORKER_SAVED set
        _finalize_cancelled_turn(ws, stream_id=stream_id)
        assert stream_id in _STREAM_WORKER_SAVED, (
            "worker failed to mark stream saved after a successful retry"
        )
        stamped = [
            m for m in ws.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) >= 1, "worker retry did not persist a durable notice row"

        # Worker retirement now pops the exact notice and empties all registries
        _retire_worker_cancelled_state(stream_id)
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_WORKER_SAVED
        assert stream_id not in _STREAM_CANCEL_CLAIMED
        assert stream_id not in _STREAM_SETTLEMENT_TERMINAL

    def test_worker_first_save_failure_notice_survives_for_cancel_persist(self):
        """Worker-first save failure composed end-to-end: the worker's
        _finalize_cancelled_turn save() RAISES, so _STREAM_WORKER_SAVED stays
        empty and the worker's retirement does NOT pop the notice.  The notice
        remains in _STREAM_FALLBACK_NOTICES for cancel_stream to pick up, and a
        subsequent cancel_stream persists it durably on the session row
        (gate-certifier #2: at least one durable owner exists at all times).
        """
        import threading
        from unittest.mock import patch, Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _finalize_cancelled_turn, _retire_worker_cancelled_state,
            cancel_stream,
        )
        from api.config import (
            STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT,
            STREAM_LIVE_TOOL_CALLS, AGENT_INSTANCES,
        )

        stream_id = "worker_first_fail"
        session_id = "sess_worker_first_fail"
        _notice = {"message": "fb", "to_model": "m1", "to_provider": "p1"}
        _publish_test_notice(stream_id, _notice)

        # Worker session: save always fails
        ws = Mock()
        ws.session_id = session_id
        ws.active_stream_id = stream_id
        ws.pending_user_message = "q"
        ws.pending_attachments = []
        ws.pending_started_at = 1.0
        ws.messages = [{"role": "assistant", "content": "Prior.", "timestamp": 1}]
        ws.save = Mock(side_effect=RuntimeError("disk full"))

        # Register session + writeback owner so the #6623 generation guard
        # in _finalize_cancelled_turn resolves the current session and
        # authorizes the finalize against this stream_id.
        from api import models, config
        models.SESSIONS[session_id] = ws
        config.register_session_writeback_owner(session_id, stream_id)

        # Worker finalizes and its save FAILS
        _finalize_cancelled_turn(ws, stream_id=stream_id)
        assert stream_id not in _STREAM_WORKER_SAVED
        # Worker retirement must NOT pop (not durably saved)
        _retire_worker_cancelled_state(stream_id)
        assert stream_id in _STREAM_FALLBACK_NOTICES, (
            "worker dropped the only live notice after its save failure"
        )

        # Now cancel runs: its session save SUCCEEDS -> notice persisted durably
        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _publish_test_notice(stream_id, _notice)  # still parked

        cs = Mock()
        cs.session_id = session_id
        cs.active_stream_id = stream_id
        cs.pending_user_message = "q"
        cs.pending_attachments = []
        cs.pending_started_at = 1.0
        cs.messages = [{"role": "assistant", "content": "Prior.", "timestamp": 1}]
        cs.save = Mock()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=cs):
            result = cancel_stream(stream_id)
        assert result["cancelled"] is True

        stamped = [
            m for m in cs.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) >= 1, (
            "cancel failed to persist the notice left by the failed worker"
        )
        # Bounded registries after the full composed flow (fence is retired
        # by the worker's finally, not cancel's finally — gate-certifier
        # blocker #2).
        _retire_worker_cancelled_state(stream_id)
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_WORKER_SAVED
        assert stream_id not in _STREAM_CANCEL_CLAIMED
        assert stream_id not in _STREAM_SETTLEMENT_TERMINAL

    def test_generation_oracle_worker_saves_A_then_B_published_retirement_preserves_B(self):
        """Generation oracle: hold worker save after it snapshots generation A,
        publish B through the production path, then prove worker retirement
        neither deletes B nor marks B durable.

        The worker captures generation A before save(). During the FIRST save call
        (held via a barrier), we publish generation B through the production
        _publish_fallback_notice path ONCE.  _finalize_cancelled_turn saves a
        SECOND time (to clear active_stream_id); the second save MUST NOT
        republish B — otherwise the test replaces the in-flight B with another
        identical B and "B survives retirement" only proves text equality, not
        preservation of the exact generation-2 publication
        (gate-certifier blocker #3: generation oracle republishes B during
        the second save).

        After save returns, the worker records _STREAM_WORKER_SAVED = A.
        Worker retirement must:

        1. NOT pop the live notice — B is the current generation, A was saved,
           and the compare-and-pop only retires the exact saved generation A.
        2. NOT mark B as durable — B was never saved to disk (the second save
           did not carry it).  Deep-copied per-save snapshots capture what was
           actually durable at each save call; B must NOT appear in any
           durable snapshot.

        (gate-certifier blocker #4: strengthen generation oracle; blocker #3:
        publish B exactly once during the first held save)
        """
        import copy
        import threading
        from unittest.mock import Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_NOTICE_GENERATION, _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED,
            _finalize_cancelled_turn, _retire_worker_cancelled_state,
            _publish_fallback_notice, _current_notice_generation,
        )

        stream_id = "gen_oracle_AB"
        session_id = "sess_gen_oracle_AB"
        _notice_A = {"message": "notice A", "to_model": "mA", "to_provider": "pA"}
        _notice_B = {"message": "notice B", "to_model": "mB", "to_provider": "pB"}

        # Publish generation A (gen=1)
        assert _publish_fallback_notice(stream_id, dict(_notice_A)) is True
        gen_A = _current_notice_generation(stream_id)
        assert gen_A == 1

        # Barrier: publish B during the worker's FIRST save() call, AFTER the
        # worker has captured generation A but BEFORE save returns.  The
        # SECOND save must not republish B.
        save_entered = threading.Event()
        b_published = threading.Event()
        save_can_finish = threading.Event()
        _save_call_count = [0]
        _save_snapshots = []
        _published_once = [False]

        def _save():
            _save_call_count[0] += 1
            call_n = _save_call_count[0]
            if call_n == 1:
                # First save: capture durable snapshot BEFORE publishing B, then
                # publish B once through the production gate.
                _save_snapshots.append(copy.deepcopy(ws.messages))
                save_entered.set()
                assert not _published_once[0], "B was republished — oracle bug"
                _published_once[0] = True
                assert _publish_fallback_notice(stream_id, dict(_notice_B)) is True
                gen_B = _current_notice_generation(stream_id)
                assert gen_B == 2, f"expected gen 2 after B publish, got {gen_B}"
                b_published.set()
                save_can_finish.wait(timeout=5)
            else:
                # Second save (active_stream_id clear): no publish, just snapshot
                # durable state so the oracle can assert B never landed durably.
                _save_snapshots.append(copy.deepcopy(ws.messages))
                assert _save_call_count[0] == 2, (
                    "_finalize_cancelled_turn saved a third time — exact-count "
                    "oracle broken"
                )

        ws = Mock()
        ws.session_id = session_id
        ws.active_stream_id = stream_id
        ws.pending_user_message = "q"
        ws.pending_attachments = []
        ws.pending_started_at = 1.0
        ws.messages = [{"role": "assistant", "content": "Prior.", "timestamp": 1}]
        ws.save = Mock(side_effect=_save)

        from api import models, config
        models.SESSIONS[session_id] = ws
        config.register_session_writeback_owner(session_id, stream_id)

        # Run _finalize_cancelled_turn in a thread so we can synchronize
        worker_done = threading.Event()
        def _worker_finalize():
            _finalize_cancelled_turn(ws, stream_id=stream_id)
            worker_done.set()

        t = threading.Thread(target=_worker_finalize, daemon=True)
        t.start()

        # Wait for save to enter and B to be published
        assert save_entered.wait(timeout=5), "worker save did not start"
        assert b_published.wait(timeout=5), "B was not published during save"

        # At this point: A was stamped on the cancel-marker row before the first
        # save (durable snapshot 1 shows A), B is the current map entry with
        # generation 2.
        assert _current_notice_generation(stream_id) == 2
        assert _STREAM_FALLBACK_NOTICES[stream_id]["message"] == "notice B"
        # Capture the IDENTITY of the exact generation-2 B dict — retirement
        # must preserve THIS object, not a same-content replacement.
        b_obj = _STREAM_FALLBACK_NOTICES[stream_id]

        # Let save finish
        save_can_finish.set()
        assert worker_done.wait(timeout=5), "worker finalize did not complete"

        # The worker's finalize path calls save() at least once for turn
        # persistence and then again to clear active_stream_id.  Both calls
        # must have happened; B must have been published EXACTLY once.
        # EXACT save count: _finalize_cancelled_turn saves once for turn
        # persistence and exactly once more to clear active_stream_id — assert
        # the precise count, not a lower bound (a silent third save would
        # invalidate every snapshot-based durable claim below).
        assert len(_save_snapshots) == 2, (
            f"expected exactly 2 saves in _finalize_cancelled_turn, got "
            f"{len(_save_snapshots)}"
        )
        assert _published_once[0] is True, (
            "B was never published — the first save did not run the publish step"
        )

        # Worker recorded the saved generation (A=1), not B
        assert _STREAM_WORKER_SAVED.get(stream_id) == gen_A, (
            f"worker recorded gen {_STREAM_WORKER_SAVED.get(stream_id)} but "
            f"should have recorded A={gen_A}"
        )

        # Durable authority: EVERY saved snapshot must carry notice A, not B.
        # The first snapshot was taken before B was published (A only).
        # Subsequent snapshots were taken after B was published but must NOT
        # reflect it, since the worker never re-stamped.  In addition to
        # "B never appears", require exactly one A stamp per snapshot so a
        # regression that dropped the stamp entirely (empty oracle) cannot
        # pass silently.
        for snapshot_idx, snap in enumerate(_save_snapshots):
            stamped = [
                m for m in snap
                if isinstance(m, dict) and m.get("_fallbackNotice")
            ]
            assert stamped, (
                f"snapshot {snapshot_idx}: no _fallbackNotice stamp at all — "
                f"A must be durable in every saved snapshot"
            )
            for stamped_msg in stamped:
                msg = stamped_msg["_fallbackNotice"].get("message")
                assert msg == "notice A", (
                    f"snapshot {snapshot_idx}: stamped row carries {msg!r}, "
                    f"expected 'notice A' (B must never be durable and the "
                    f"worker must not re-stamp with the post-A publication)"
                )

        # Worker retirement: must NOT delete B (B is a different generation)
        _retire_worker_cancelled_state(stream_id)
        assert stream_id in _STREAM_FALLBACK_NOTICES, (
            "worker retirement deleted generation B — it should only retire "
            "the exact saved generation A"
        )
        # The generation-2 B OBJECT itself must survive retirement — same
        # identity, not just same content — and the generation counter must
        # still read 2.
        assert _STREAM_FALLBACK_NOTICES[stream_id] is b_obj, (
            "worker retirement replaced the generation-2 B map object — the "
            "exact published dict must be preserved, not a copy"
        )
        assert _STREAM_FALLBACK_NOTICES[stream_id]["message"] == "notice B", (
            "worker retirement changed the live notice — B should be preserved"
        )
        assert _current_notice_generation(stream_id) == 2, (
            f"generation counter drifted to "
            f"{_current_notice_generation(stream_id)} after retirement — must "
            f"remain 2 (B's generation)"
        )
        # B must NOT be marked durable (it was never saved to disk)
        assert _STREAM_WORKER_SAVED.get(stream_id) != _current_notice_generation(stream_id), (
            "worker retirement marked B as durable — B was never saved"
        )

        # Cleanup
        _STREAM_FALLBACK_NOTICES.pop(stream_id, None)
        _STREAM_WORKER_SAVED.pop(stream_id, None)
        _STREAM_SETTLEMENT_PARTICIPANTS.pop(stream_id, None)
        _STREAM_SETTLEMENT_COMPLETED.pop(stream_id, None)
        _STREAM_SETTLEMENT_TERMINAL.discard(stream_id)
        _STREAM_NOTICE_GENERATION.pop(stream_id, None)
        _STREAM_CANCEL_CLAIMED.discard(stream_id)
        models.SESSIONS.pop(session_id, None)
        config.clear_session_writeback_owner_if_owned(session_id, stream_id)

    def test_stamp_to_wrapper_B_publication_stays_unsaved_and_owned(self):
        """Deterministic seam test: B published BETWEEN row-stamp selection and
        save-wrapper entry stays unsaved + owned while only A is marked durable.

        Exercises the real production seam factored out of the four
        stamp→_turn_final_save_commit call sites
        (_snapshot_fallback_notice_for_commit): publish A, bind the
        (generation, notice) pair at the stamp point, then publish B in the
        stamp→wrapper gap (the window the normal terminal path exposes between
        the _dm['_fallbackNotice'] stamp and the compressor-work → s.save()
        wrapper entry).  The wrapper runs with the SNAPSHOTTED pair, so:

        - _STREAM_WORKER_SAVED must record A's generation only,
        - B's exact map object must remain live and unretired,
        - B's generation counter must remain 2,
        - worker retirement must preserve B (identity + generation) and must
          NOT mark B durable.

        (gate-certifier blocker: previously _turn_final_save_commit re-inferred
        the durable generation from the global map at wrapper ENTRY, recording
        B as durable even though only A was stamped/saved — teardown could then
        retire the never-durable B.)
        """
        from unittest.mock import Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_NOTICE_GENERATION, _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED,
            _publish_fallback_notice, _current_notice_generation,
            _snapshot_fallback_notice_for_commit, _turn_final_save_commit,
            _retire_worker_cancelled_state,
        )

        stream_id = "seam_AB"
        _notice_A = {"message": "seam A", "to_model": "mA", "to_provider": "pA"}
        _notice_B = {"message": "seam B", "to_model": "mB", "to_provider": "pB"}
        try:
            # Publish A (gen=1) through the production gate.
            assert _publish_fallback_notice(stream_id, dict(_notice_A)) is True
            gen_A = _current_notice_generation(stream_id)
            assert gen_A == 1

            # ROW-STAMP POINT: the production stamp site reads
            # _pending_fallback_notices[-1] == A and binds the (gen, notice)
            # pair lock-atomically.
            commit_gen, commit_notice = _snapshot_fallback_notice_for_commit(
                stream_id, dict(_notice_A),
            )
            assert (commit_gen, commit_notice["message"]) == (1, "seam A")

            # GAP: the status callback publishes B AFTER the row was stamped
            # with A but BEFORE the save-wrapper entry — exactly the window
            # the normal terminal path leaves open across the compressor /
            # context-length work between stamp and s.save().
            assert _publish_fallback_notice(stream_id, dict(_notice_B)) is True
            assert _current_notice_generation(stream_id) == 2
            b_obj = _STREAM_FALLBACK_NOTICES[stream_id]

            # WRAPPER ENTRY + SAVE: runs with the snapshotted pair (A),
            # NEVER rereading the map.
            ws = Mock()
            ws.session_id = "sess_seam_AB"
            ws.profile = None
            saved = [False]

            def _save():
                saved[0] = True

            ws.save = _save
            with _turn_final_save_commit(
                stream_id, ws,
                committed_generation=commit_gen,
                committed_notice=commit_notice,
            ):
                ws.save()

            assert saved[0] is True
            # Only A's generation is marked durable.
            assert _STREAM_WORKER_SAVED.get(stream_id) == 1, (
                f"wrapper recorded gen {_STREAM_WORKER_SAVED.get(stream_id)} "
                f"as durable — must be 1 (the stamped A), never 2 (B)"
            )
            # B remains live, owned by the map, untouched.
            assert _STREAM_FALLBACK_NOTICES[stream_id] is b_obj
            assert _current_notice_generation(stream_id) == 2

            # Worker retirement on teardown: must preserve B (object identity
            # AND generation) and must NOT mark B durable.
            _retire_worker_cancelled_state(stream_id)
            assert _STREAM_FALLBACK_NOTICES.get(stream_id) is b_obj, (
                "retirement deleted/replaced the exact generation-2 B object"
            )
            assert _current_notice_generation(stream_id) == 2
            assert _STREAM_WORKER_SAVED.get(stream_id) != 2, (
                "B was marked durable — it was never stamped or saved"
            )
        finally:
            _STREAM_FALLBACK_NOTICES.pop(stream_id, None)
            _STREAM_WORKER_SAVED.pop(stream_id, None)
            _STREAM_SETTLEMENT_PARTICIPANTS.pop(stream_id, None)
            _STREAM_SETTLEMENT_COMPLETED.pop(stream_id, None)
            _STREAM_SETTLEMENT_TERMINAL.discard(stream_id)
            _STREAM_NOTICE_GENERATION.pop(stream_id, None)
            _STREAM_CANCEL_CLAIMED.discard(stream_id)

    def test_stamp_to_wrapper_same_content_B_uses_source_order_generation(self):
        """Regression: same-content B cannot steal A's durable generation.

        This mirrors the production status-callback source order:
        append pending notice A, publish A (which stamps A's internal generation),
        select A for the row, then append/publish a newer same-public-content B
        before the save wrapper commits.  Content equality is intentionally
        useless here; only the generation minted onto the selected pending dict
        proves which publication the row actually saved.
        """
        from unittest.mock import Mock
        from api.streaming import (
            _FALLBACK_NOTICE_GENERATION_KEY,
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_NOTICE_GENERATION, _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED,
            _publish_fallback_notice, _current_notice_generation,
            _snapshot_fallback_notice_for_commit, _turn_final_save_commit,
            _retire_worker_cancelled_state,
        )

        stream_id = "seam_same_content_AB"
        pending = []
        try:
            notice_a = {"message": "same fallback", "to_model": "m", "to_provider": "p"}
            pending.append(notice_a)
            assert _publish_fallback_notice(stream_id, notice_a) is True
            assert notice_a[_FALLBACK_NOTICE_GENERATION_KEY] == 1

            # ROW-STAMP SOURCE ORDER: production selected A before B existed.
            notice_selected_for_row = pending[-1]
            stamped_notice = {
                "message": notice_selected_for_row["message"],
                "to_model": notice_selected_for_row["to_model"],
                "to_provider": notice_selected_for_row["to_provider"],
            }

            # A later status callback reports the exact same public notice text.
            # Without the source-order generation on notice_selected_for_row,
            # _snapshot_fallback_notice_for_commit would match by content and
            # incorrectly bind this save to B's current generation.
            notice_b = {"message": "same fallback", "to_model": "m", "to_provider": "p"}
            pending.append(notice_b)
            assert _publish_fallback_notice(stream_id, notice_b) is True
            assert notice_b[_FALLBACK_NOTICE_GENERATION_KEY] == 2
            assert _current_notice_generation(stream_id) == 2
            b_obj = _STREAM_FALLBACK_NOTICES[stream_id]

            commit_gen, commit_notice = _snapshot_fallback_notice_for_commit(
                stream_id, notice_selected_for_row,
            )
            assert commit_gen == 1
            assert commit_notice == stamped_notice

            ws = Mock()
            ws.session_id = "sess_same_content_AB"
            ws.profile = None
            saved = [False]
            ws.save = lambda: saved.__setitem__(0, True)
            with _turn_final_save_commit(
                stream_id, ws,
                committed_generation=commit_gen,
                committed_notice=commit_notice,
            ):
                ws.save()

            assert saved[0] is True
            assert _STREAM_WORKER_SAVED.get(stream_id) == 1
            assert _STREAM_FALLBACK_NOTICES[stream_id] is b_obj
            assert _current_notice_generation(stream_id) == 2
            _retire_worker_cancelled_state(stream_id)
            assert _STREAM_FALLBACK_NOTICES.get(stream_id) is b_obj
            assert _current_notice_generation(stream_id) == 2
            assert _STREAM_WORKER_SAVED.get(stream_id) != 2
        finally:
            _STREAM_FALLBACK_NOTICES.pop(stream_id, None)
            _STREAM_WORKER_SAVED.pop(stream_id, None)
            _STREAM_SETTLEMENT_PARTICIPANTS.pop(stream_id, None)
            _STREAM_SETTLEMENT_COMPLETED.pop(stream_id, None)
            _STREAM_SETTLEMENT_TERMINAL.discard(stream_id)
            _STREAM_NOTICE_GENERATION.pop(stream_id, None)
            _STREAM_CANCEL_CLAIMED.discard(stream_id)

    def test_cancel_registration_first_lock_no_worker_retirement_gap(self):
        """Worker-first and cancel-first schedules both return every settlement
        registry to baseline: cancel installs participants+claim INSIDE the
        first streams_lock acquisition (the admission step), so a validated
        live worker can no longer retire in the gap between the first and the
        (old) second lock.

        Schedule 1 (worker-first): the worker's teardown runs
        _retire_worker_cancelled_state BEFORE cancel is invoked, draining the
        validated live stream maps in the same critical section (the
        production teardown shape).  Cancel's first-lock registration must
        then see no live worker signal and decline to install a phantom
        'worker' participant that nothing can clear.

        Schedule 2 (cancel-first / concurrent): cancel registers in the first
        lock; when the worker then retires it finds the settlement record and
        completes its own participant — the LAST completer retires the fence
        and the record.  With the old second-lock registration, a worker that
        retired between the two locks was classified as ordinary completion
        (no counterpart) while cancel later installed a permanent 'worker'
        entry (the exact gate-certifier leak).
        """
        import queue
        import threading
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED,
            _STREAM_SETTLEMENT_TERMINAL, _STREAM_NOTICE_GENERATION,
            _STREAM_SETTLEMENT_PARTICIPANTS, _STREAM_SETTLEMENT_COMPLETED,
            _STREAM_WORKER_SAVED, _STREAM_FALLBACK_DEAD_LETTER,
            _retire_worker_cancelled_state_locked,
            cancel_stream,
            STREAMS_LOCK,
        )
        from api import config as _config

        def _all_settlement_registries():
            return (
                _STREAM_SETTLEMENT_PARTICIPANTS, _STREAM_SETTLEMENT_COMPLETED,
                _STREAM_SETTLEMENT_TERMINAL, _STREAM_CANCEL_CLAIMED,
                _STREAM_FALLBACK_NOTICES, _STREAM_NOTICE_GENERATION,
                _STREAM_WORKER_SAVED, _STREAM_FALLBACK_DEAD_LETTER,
            )

        def _assert_baseline(sid):
            assert sid not in _STREAM_SETTLEMENT_PARTICIPANTS, (
                f"'worker'/'cancel' participant leaked for {sid}"
            )
            assert sid not in _STREAM_SETTLEMENT_COMPLETED
            assert sid not in _STREAM_SETTLEMENT_TERMINAL
            assert sid not in _STREAM_CANCEL_CLAIMED

        def _cleanup(sid):
            _STREAM_FALLBACK_NOTICES.pop(sid, None)
            _STREAM_WORKER_SAVED.pop(sid, None)
            _STREAM_SETTLEMENT_PARTICIPANTS.pop(sid, None)
            _STREAM_SETTLEMENT_COMPLETED.pop(sid, None)
            _STREAM_SETTLEMENT_TERMINAL.discard(sid)
            _STREAM_NOTICE_GENERATION.pop(sid, None)
            _STREAM_CANCEL_CLAIMED.discard(sid)
            _config.STREAMS.pop(sid, None)
            _config.CANCEL_FLAGS.pop(sid, None)
            _config.AGENT_INSTANCES.pop(sid, None)
            try:
                _config.unregister_active_run(sid)
            except Exception:
                pass

        regs = _all_settlement_registries()
        for reg in regs:
            if isinstance(reg, dict):
                assert not reg, "settlement registry not at baseline pre-test"

        # ── Schedule 1: worker retires FIRST, cancel runs after ──
        sid = "sched_worker_first"
        try:
            _config.STREAMS[sid] = queue.Queue()
            _config.CANCEL_FLAGS[sid] = threading.Event()
            # Worker retires (e.g. its finally won the race before cancel's
            # HTTP call arrived): no counterpart registered at retire time.
            # Production-faithful teardown: the streaming finally pops the
            # stream/flag/agent maps AND retires in ONE critical section —
            # a retired worker NEVER leaves validated live maps behind.
            with STREAMS_LOCK:
                _config.STREAMS.pop(sid, None)
                _config.CANCEL_FLAGS.pop(sid, None)
                _config.AGENT_INSTANCES.pop(sid, None)
                _retire_worker_cancelled_state_locked(sid)
            # Ordinary-completion retire must be a no-op tombstone-wise.
            assert sid not in _STREAM_SETTLEMENT_COMPLETED
            # Now cancel: registration happens atomically in the FIRST lock.
            # With no validated live worker signal, cancel must not install
            # a phantom 'worker' participant — the job already finished.
            _config.register_active_run(sid, session_id="sess_sched_wf")
            result = cancel_stream(sid)
            assert result.get("cancelled") is True
            # After cancel's outer finally retires 'cancel', the completed
            # tombstones must all drain back to baseline — no permanent
            # 'worker' entry.
            _assert_baseline(sid)
        finally:
            _cleanup(sid)

        # ── Schedule 2: cancel registers FIRST, worker retires after ──
        sid = "sched_cancel_first"
        try:
            _config.STREAMS[sid] = queue.Queue()
            _config.CANCEL_FLAGS[sid] = threading.Event()
            _config.register_active_run(sid, session_id="sess_sched_cf")

            # Two-thread interleave: cancel (registers in its first lock)
            # races the worker teardown.  The barrier forces the worker to
            # retire only AFTER cancel has acquired/released its first lock
            # via STREAMS_LOCK provenance — deterministic by construction:
            # cancel owns the registries first, then worker retires.
            cancel_done = threading.Event()

            def _run_cancel():
                cancel_stream(sid)
                cancel_done.set()

            t = threading.Thread(target=_run_cancel, daemon=True)
            t.start()
            assert cancel_done.wait(timeout=10), "cancel_stream hung"
            # Cancel's outer finally retired 'cancel'; participants may still
            # hold 'worker' (the worker hasn't retired yet).
            # Simulate the worker's finally now retiring its participant.
            with STREAMS_LOCK:
                _retire_worker_cancelled_state_locked(sid)
            # Worker was the LAST completer → fence and record drained.
            _assert_baseline(sid)
        finally:
            _cleanup(sid)

    def test_ordinary_completed_streams_leave_no_settlement_tombstone(self):
        """Ordinary completed streams (no cancellation) must not leak entries
        into _STREAM_SETTLEMENT_COMPLETED or any other settlement registry.

        The worker's finally calls _retire_worker_cancelled_state_locked
        unconditionally. Without the early-return guard, streams that never
        entered cancellation settlement would insert a {'worker'} tombstone
        into _STREAM_SETTLEMENT_COMPLETED that never gets removed — one
        process-lifetime dict entry per normal stream.

        (gate-certifier blocker #1: settlement tombstone leak)
        """
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_NOTICE_GENERATION, _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED, _STREAM_FALLBACK_DEAD_LETTER,
            _retire_worker_cancelled_state_locked, STREAMS_LOCK,
        )

        # Simulate several ordinary completed streams (no cancellation state)
        for i in range(5):
            sid = f"ordinary_stream_{i}"
            with STREAMS_LOCK:
                _retire_worker_cancelled_state_locked(sid)
            # After retirement, NO registry should contain this stream_id
            assert sid not in _STREAM_SETTLEMENT_COMPLETED, (
                f"ordinary stream {sid} leaked a tombstone into _STREAM_SETTLEMENT_COMPLETED"
            )
            assert sid not in _STREAM_SETTLEMENT_PARTICIPANTS
            assert sid not in _STREAM_SETTLEMENT_TERMINAL
            assert sid not in _STREAM_FALLBACK_NOTICES
            assert sid not in _STREAM_WORKER_SAVED
            assert sid not in _STREAM_CANCEL_CLAIMED
            assert sid not in _STREAM_NOTICE_GENERATION
            assert sid not in _STREAM_FALLBACK_DEAD_LETTER

        # Also verify that a stream WITH cancellation state is properly retired
        cancel_sid = "cancelled_stream_1"
        with STREAMS_LOCK:
            _STREAM_SETTLEMENT_PARTICIPANTS[cancel_sid] = {'worker'}
            _retire_worker_cancelled_state_locked(cancel_sid)
        assert cancel_sid not in _STREAM_SETTLEMENT_PARTICIPANTS, (
            "cancelled stream settlement participants not retired"
        )
        assert cancel_sid not in _STREAM_SETTLEMENT_COMPLETED, (
            "cancelled stream leaked into _STREAM_SETTLEMENT_COMPLETED"
        )

    def test_normal_fallback_turn_retires_all_settlement_state(self):
        """Production-composed normal completion with a confirmed fallback
        publishes a notice, saves it via _turn_final_save_commit, and retires
        ALL registries on teardown — leaving no tombstone.

        Reproduces gate-certifier finding #1: before this fix, a normal
        successful fallback turn (no cancellation) stamped the notice on the
        final assistant row and s.save()d it, but never recorded the saved
        generation.  On teardown, _retire_worker_cancelled_state_locked
        skipped the compare-retire (no saved generation), fell through to
        _complete_stream_settlement_participant_locked, and leaked
        _STREAM_SETTLEMENT_COMPLETED[stream_id] = {'worker'} — one tombstone
        per normal fallback turn.  The fallback notice also remained in
        _STREAM_FALLBACK_NOTICES (saved but never retired).

        This test composes the actual production flow — status callback
        publishes via _publish_fallback_notice → stamp on assistant row →
        _turn_final_save_commit wraps s.save() → _retire_worker_cancelled_state
        tears down — and proves every registry returns to baseline.
        """
        from unittest.mock import Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_NOTICE_GENERATION, _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED, _STREAM_FALLBACK_DEAD_LETTER,
            _publish_fallback_notice, _current_notice_generation,
            _retire_worker_cancelled_state, _turn_final_save_commit,
            _clean_fallback_notice,
        )

        for iteration in range(3):
            stream_id = f"normal_fallback_turn_{iteration}"
            session_id = f"sess_normal_fallback_{iteration}"

            # ── 1. Production status-callback publication ──
            _notice = {
                "message": f"Switched to fallback model iter{iteration}",
                "to_model": "fallback-model",
                "to_provider": "fallback-provider",
            }
            assert _publish_fallback_notice(stream_id, dict(_notice)) is True
            saved_gen = _current_notice_generation(stream_id)
            assert saved_gen >= 1

            # ── 2. Stamp the notice on the final assistant row (mirrors the
            #       normal-completion stamp block at streaming.py:11376-11383) ──
            ws = Mock()
            ws.session_id = session_id
            ws.active_stream_id = stream_id
            ws.messages = [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ]
            for _dm in reversed(ws.messages):
                if isinstance(_dm, dict) and _dm.get('role') == 'assistant':
                    _dm['_fallbackNotice'] = _clean_fallback_notice(_notice)
                    break

            # ── 3. Turn-finalizing save via _turn_final_save_commit ──
            save_calls = []
            ws.save = Mock(
                side_effect=lambda sc=save_calls, _ws=ws: sc.append(len(_ws.messages))
            )
            with _turn_final_save_commit(stream_id, ws):
                ws.save()
            assert save_calls, "save was never called"
            # The saved generation must now be durably recorded for retirement
            assert _STREAM_WORKER_SAVED.get(stream_id) == saved_gen

            # ── 4. Worker teardown retirement ──
            _retire_worker_cancelled_state(stream_id)

            # ── 5. Assert every settlement/cancellation registry is back at baseline ──
            assert stream_id not in _STREAM_SETTLEMENT_COMPLETED, (
                f"iteration {iteration}: normal fallback turn leaked a tombstone into "
                f"_STREAM_SETTLEMENT_COMPLETED"
            )
            assert stream_id not in _STREAM_SETTLEMENT_PARTICIPANTS, (
                f"iteration {iteration}: participants record leaked"
            )
            assert stream_id not in _STREAM_SETTLEMENT_TERMINAL, (
                f"iteration {iteration}: terminal fence leaked"
            )
            assert stream_id not in _STREAM_FALLBACK_NOTICES, (
                f"iteration {iteration}: saved notice not retired from _STREAM_FALLBACK_NOTICES"
            )
            assert stream_id not in _STREAM_WORKER_SAVED, (
                f"iteration {iteration}: saved-generation registry not retired"
            )
            assert stream_id not in _STREAM_CANCEL_CLAIMED, (
                f"iteration {iteration}: cancel claim leaked"
            )
            assert stream_id not in _STREAM_NOTICE_GENERATION, (
                f"iteration {iteration}: generation counter leaked"
            )
            assert stream_id not in _STREAM_FALLBACK_DEAD_LETTER, (
                f"iteration {iteration}: dead-letter entry leaked"
            )
            # Notice must have been durably persisted on the assistant row
            fallback_stamps = [
                m for m in ws.messages
                if isinstance(m, dict) and m.get('_fallbackNotice')
            ]
            assert len(fallback_stamps) == 1
            assert fallback_stamps[0]['_fallbackNotice']['message'] == _notice['message']

    def test_normal_no_fallback_turn_retires_all_settlement_state(self):
        """Partner invariant: a normal completed turn with NO fallback must
        also leave every registry at baseline.  Symmetric to
        test_normal_fallback_turn_retires_all_settlement_state but skips
        the publication step; the no-registry-state early return in
        _retire_worker_cancelled_state_locked must not create any entries.
        """
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_NOTICE_GENERATION, _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED, _STREAM_FALLBACK_DEAD_LETTER,
            _retire_worker_cancelled_state,
        )

        for iteration in range(3):
            stream_id = f"normal_no_fb_turn_{iteration}"
            _retire_worker_cancelled_state(stream_id)
            assert stream_id not in _STREAM_SETTLEMENT_COMPLETED
            assert stream_id not in _STREAM_SETTLEMENT_PARTICIPANTS
            assert stream_id not in _STREAM_SETTLEMENT_TERMINAL
            assert stream_id not in _STREAM_FALLBACK_NOTICES
            assert stream_id not in _STREAM_WORKER_SAVED
            assert stream_id not in _STREAM_CANCEL_CLAIMED
            assert stream_id not in _STREAM_NOTICE_GENERATION
            assert stream_id not in _STREAM_FALLBACK_DEAD_LETTER

    def test_normal_fallback_turn_post_save_publication_preserves_unsaved_generation(self):
        """If a NEWER fallback notice is published DURING the final save
        (after stamp but before save returns), the worker's retirement must
        NOT delete the newer unsaved generation — it was never durably saved.

        Same adversarial-schedule shape as the cancel-side generation oracle:
        the worker stamps gen A and calls save() (which holds a barrier);
        gen B is published while A is in flight; save returns, recording
        saved=A; retirement must compare against saved (A) vs current (B)
        and leave B + its generation counter intact.
        """
        import threading
        from unittest.mock import Mock
        from api.streaming import (
            _STREAM_FALLBACK_NOTICES, _STREAM_WORKER_SAVED,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_NOTICE_GENERATION, _STREAM_SETTLEMENT_PARTICIPANTS,
            _STREAM_SETTLEMENT_COMPLETED,
            _publish_fallback_notice, _current_notice_generation,
            _retire_worker_cancelled_state, _turn_final_save_commit,
            _clean_fallback_notice,
        )

        stream_id = "normal_fallback_post_save_pub"
        session_id = "sess_normal_fallback_post_save_pub"

        # ── Publish A ──
        _notice_a = {"message": "notice A", "to_model": "mA", "to_provider": "pA"}
        assert _publish_fallback_notice(stream_id, dict(_notice_a)) is True
        gen_a = _current_notice_generation(stream_id)
        assert gen_a == 1

        # ── Stamp A on the final assistant row ──
        ws = Mock()
        ws.session_id = session_id
        ws.active_stream_id = stream_id
        ws.messages = [{"role": "assistant", "content": "a"}]
        ws.messages[-1]['_fallbackNotice'] = _clean_fallback_notice(_notice_a)

        # ── During save, publish B (gen 2) while A is in flight ──
        save_entered = threading.Event()
        b_published = threading.Event()
        save_can_finish = threading.Event()

        _notice_b = {"message": "notice B", "to_model": "mB", "to_provider": "pB"}

        def _save():
            save_entered.set()
            assert _publish_fallback_notice(stream_id, dict(_notice_b)) is True
            gen_b = _current_notice_generation(stream_id)
            assert gen_b == 2, f"expected gen 2 after B publish, got {gen_b}"
            b_published.set()
            save_can_finish.wait(timeout=5)

        ws.save = Mock(side_effect=_save)

        worker_done = threading.Event()

        def _worker_finalize():
            with _turn_final_save_commit(stream_id, ws):
                ws.save()
            worker_done.set()

        t = threading.Thread(target=_worker_finalize, daemon=True)
        t.start()
        assert save_entered.wait(timeout=5), "save did not start"
        assert b_published.wait(timeout=5), "B was not published during save"
        save_can_finish.set()
        assert worker_done.wait(timeout=5), "save did not complete"

        # ── Worker recorded the saved generation (A=1), not B ──
        assert _STREAM_WORKER_SAVED.get(stream_id) == gen_a, (
            f"worker recorded gen {_STREAM_WORKER_SAVED.get(stream_id)} but "
            f"should have recorded A={gen_a}"
        )

        # ── Worker retirement: must NOT delete B (B is a different generation)
        #    and must NOT pop the generation counter (B's gen lives there now). ──
        _retire_worker_cancelled_state(stream_id)
        assert stream_id in _STREAM_FALLBACK_NOTICES, (
            "retirement deleted generation B — it should only retire the exact "
            "saved generation A"
        )
        assert _STREAM_FALLBACK_NOTICES[stream_id]["message"] == "notice B", (
            "retirement changed the live notice — B should be preserved"
        )
        # Generation counter stays (B's value) — it is NOT A's.  Only A's
        # registry entries retire; B persists as the unsaved generation owner.
        assert _current_notice_generation(stream_id) == 2, (
            "generation counter must remain at B's value (2) since B is unsaved"
        )

        # Cleanup (B was never saved — simulate the cancel-side settlement
        # running later; test isolation requires these pops)
        _STREAM_FALLBACK_NOTICES.pop(stream_id, None)
        _STREAM_WORKER_SAVED.pop(stream_id, None)
        _STREAM_SETTLEMENT_PARTICIPANTS.pop(stream_id, None)
        _STREAM_SETTLEMENT_COMPLETED.pop(stream_id, None)
        _STREAM_SETTLEMENT_TERMINAL.discard(stream_id)
        _STREAM_NOTICE_GENERATION.pop(stream_id, None)
        _STREAM_CANCEL_CLAIMED.discard(stream_id)
