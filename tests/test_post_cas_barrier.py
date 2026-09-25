"""
Production-composed post-CAS barrier test for the terminal fence.

The gate-certifier's round-13 rework point 4 requires a test that:
  - runs the real cancel_stream() settlement loop (not an isolated fence check)
  - publishes generation B AFTER A's CAS pop + fence set (not from
    save.side_effect, which fires before the CAS)
  - asserts B was REJECTED by the terminal fence
  - reloads from disk and asserts A is the sole durable current-turn notice

The hook point is _redacted_session_payload_with_full_messages, called at
line ~12472 after the settlement loop completes (post-CAS) and before the
inner finally.  Patching it to publish B through the REAL _publish_fallback_notice
gate exercises the exact adversarial window the reviewer named.
"""
import copy
import json
import os
import queue
import tempfile
import threading
import pytest
from unittest.mock import patch, Mock

# IMPORTANT: reference mutable module-level registries through the module
# object (api.streaming) rather than importing them by value.  Another test in
# the shard (test_optionz_liveview_perf.py) calls importlib.reload(api.streaming),
# which rebinds every module-level global to a fresh object.  A `from
# api.streaming import _STREAM_FALLBACK_NOTICES` captures the pre-reload
# reference; writes go to the orphaned old dict while cancel_stream reads the
# new one — the test silently writes into a void and fails.  Module-qualified
# access always resolves to the current binding.  (Full-suite CI failure,
# Aug 1 2026.)
import api.streaming as _streaming_mod
from api.config import (
    AGENT_INSTANCES, STREAMS, CANCEL_FLAGS,
    STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS,
)


def _clear_all_settlement_registries():
    """Reliably retire every process-global settlement map.

    Called by the autouse fixture before AND after every test in this file so
    no class can leak state into a sibling test — including the historical
    auto-compression file that runs after this one in the same shard
    (gate-certifier isolation defect: a sibling class restored a dead-letter
    as its final cleanup action, leaking notice_generations and dead_letters).
    Never restores a dead-letter as cleanup; always clears.
    """
    AGENT_INSTANCES.clear()
    STREAMS.clear()
    CANCEL_FLAGS.clear()
    _streaming_mod._STREAM_FALLBACK_NOTICES.clear()
    _streaming_mod._STREAM_CANCEL_CLAIMED.clear()
    _streaming_mod._STREAM_SETTLEMENT_TERMINAL.clear()
    _streaming_mod._STREAM_WORKER_SAVED.clear()
    _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.clear()
    _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS.clear()
    _streaming_mod._STREAM_SETTLEMENT_COMPLETED.clear()
    _streaming_mod._STREAM_NOTICE_GENERATION.clear()
    try:
        from api import models
        models.SESSIONS.clear()
    except Exception:
        pass
    try:
        from api.config import SESSION_WRITEBACK_OWNERS, SESSION_WRITEBACK_OWNERS_LOCK
        with SESSION_WRITEBACK_OWNERS_LOCK:
            SESSION_WRITEBACK_OWNERS.clear()
    except Exception:
        pass
    try:
        STREAM_PARTIAL_TEXT.clear()
        STREAM_REASONING_TEXT.clear()
        STREAM_LIVE_TOOL_CALLS.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_settlement_registries():
    """Module-level autouse fixture: clear ALL settlement state before and
    after every test in this file, regardless of class.

    This guarantees no test can leak process-global settlement maps, notice
    generations, dead-letters, writeback-owner entries, or models.SESSIONS
    entries into any sibling test — including the auto-compression file that
    runs after this shard.  Per-class setup/teardown methods are retained for
    compatibility but this fixture is the authoritative cleanup boundary.
    """
    _clear_all_settlement_registries()
    yield
    _clear_all_settlement_registries()


class TestPostCASBarrier:
    """Production-composed post-CAS barrier tests."""

    def setup_method(self):
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        _streaming_mod._STREAM_FALLBACK_NOTICES.clear()
        _streaming_mod._STREAM_CANCEL_CLAIMED.clear()
        _streaming_mod._STREAM_SETTLEMENT_TERMINAL.clear()
        _streaming_mod._STREAM_WORKER_SAVED.clear()
        _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.clear()
        _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS.clear()
        _streaming_mod._STREAM_SETTLEMENT_COMPLETED.clear()
        _streaming_mod._STREAM_NOTICE_GENERATION.clear()
        try:
            from api import models
            models.SESSIONS.clear()
        except Exception:
            pass
        try:
            from api.config import SESSION_WRITEBACK_OWNERS, SESSION_WRITEBACK_OWNERS_LOCK
            with SESSION_WRITEBACK_OWNERS_LOCK:
                SESSION_WRITEBACK_OWNERS.clear()
        except Exception:
            pass
        try:
            from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS
            STREAM_PARTIAL_TEXT.clear()
            STREAM_REASONING_TEXT.clear()
            STREAM_LIVE_TOOL_CALLS.clear()
        except Exception:
            pass

    def teardown_method(self):
        AGENT_INSTANCES.clear()
        STREAMS.clear()
        CANCEL_FLAGS.clear()
        _streaming_mod._STREAM_FALLBACK_NOTICES.clear()
        _streaming_mod._STREAM_CANCEL_CLAIMED.clear()
        _streaming_mod._STREAM_SETTLEMENT_TERMINAL.clear()
        _streaming_mod._STREAM_WORKER_SAVED.clear()
        _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.clear()
        # Clear session cache and writeback owners so Mock sessions don't
        # leak into later tests' session index sorting (gate-certifier
        # test-isolation defect: TypeError: '<' not supported between
        # 'float' and 'Mock' when Session.save() rebuilds the index).
        try:
            from api import models
            models.SESSIONS.clear()
        except Exception:
            pass
        try:
            from api.config import SESSION_WRITEBACK_OWNERS, SESSION_WRITEBACK_OWNERS_LOCK
            with SESSION_WRITEBACK_OWNERS_LOCK:
                SESSION_WRITEBACK_OWNERS.clear()
        except Exception:
            pass
        # Clear settlement registries not covered above
        try:
            _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS.clear()
            _streaming_mod._STREAM_SETTLEMENT_COMPLETED.clear()
            _streaming_mod._STREAM_NOTICE_GENERATION.clear()
        except Exception:
            pass
        # Clear STREAM_PARTIAL_TEXT / STREAM_REASONING_TEXT / STREAM_LIVE_TOOL_CALLS
        # which some tests populate
        try:
            from api.config import STREAM_PARTIAL_TEXT, STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS
            STREAM_PARTIAL_TEXT.clear()
            STREAM_REASONING_TEXT.clear()
            STREAM_LIVE_TOOL_CALLS.clear()
        except Exception:
            pass

    def test_post_cas_barrier_rejects_B_through_real_callback(self):
        """Production-composed: after cancel_stream's CAS pops A and sets the
        terminal fence, a status callback that publishes B through the REAL
        _publish_fallback_notice gate is REJECTED.  B never enters the map,
        so the finalizers cannot delete it unsaved.  A is the sole durable
        notice after the full cancel_stream() flow.

        This exercises the exact window the gate-certifier named:
        "A fallback callback can run after the inner finalizer and before
        the outer finalizer, pass the fence check, and publish generation B."

        With the fence held through the finalizer window (the ea2e0d51 fix),
        _publish_fallback_notice returns False and B is blocked.
        """
        stream_id = "post_cas_barrier_real"
        session_id = "sess_post_cas_barrier_real"

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

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial text"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _streaming_mod._publish_fallback_notice(stream_id, _notice_a)

        _save_snapshots = []
        _b_published = [False]

        def _save_with_snapshot():
            _save_snapshots.append(copy.deepcopy(mock_session.messages))

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}
        mock_session = Mock()
        mock_session.session_id = session_id
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = "q"
        mock_session.pending_attachments = []
        mock_session.pending_started_at = 1.0
        mock_session.messages = [_prior]
        mock_session.save = Mock(side_effect=_save_with_snapshot)

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        # Hook: _redacted_session_payload_with_full_messages runs AFTER the
        # settlement loop (post-CAS, fence set) and BEFORE the inner finally.
        # Patch it to publish B through the REAL production gate.
        _orig_payload = None

        def _payload_hook(session, **kwargs):
            nonlocal _orig_payload
            # At this point the settlement loop has completed:
            # - A was saved (snapshot captured)
            # - CAS popped A and set _STREAM_SETTLEMENT_TERMINAL
            # - The fence is HELD
            # Publish B through the real gate — it must be REJECTED.
            if not _b_published[0]:
                _b_published[0] = True
                published = _streaming_mod._publish_fallback_notice(stream_id, dict(_notice_b))
                assert published is False, (
                    "terminal fence failed to block post-CAS B — "
                    "_publish_fallback_notice accepted B after CAS pop, "
                    "B would be deleted by finalizers without saving"
                )
                assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES, (
                    "post-CAS B entered the map despite the terminal fence"
                )
            return None

        with patch("api.streaming.get_session", return_value=mock_session), \
             patch("api.streaming._redacted_session_payload_with_full_messages",
                   side_effect=_payload_hook):
            result = _streaming_mod.cancel_stream(stream_id)

        assert result["cancelled"] is True

        # Exactly one save (A) — B was never persisted
        assert len(_save_snapshots) == 1, (
            f"expected 1 save (A only), got {len(_save_snapshots)} — "
            "B was persisted despite the fence"
        )

        # A is the sole durable notice
        stamped = [
            m for m in _save_snapshots[-1]
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 durable notice, got {len(stamped)}"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert notice.get("to_model") == "m1", "A was not the durable notice"
        assert set(notice.keys()) == {"message", "to_model", "to_provider"}

        # All registries retired (fence is retired by the worker's finally,
        # not cancel's finally — gate-certifier blocker #2).
        _streaming_mod._retire_worker_cancelled_state(stream_id)
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED

    def test_post_cas_barrier_disk_reload_asserts_one_durable_notice(self):
        """Production-composed with disk reload: after cancel_stream completes
        with a post-CAS B publication attempt, reload the session from disk
        and assert exactly one durable current-turn notice (A).

        Uses a real temp-file session to prove durability survives reload,
        not just in-memory state (gate-certifier: "the new test does not
        prove durability").
        """
        stream_id = "post_cas_disk_reload"
        session_id = "sess_post_cas_disk_reload"

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

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial text"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _streaming_mod._publish_fallback_notice(stream_id, _notice_a)

        # Use a real temp-file-backed session to prove disk durability
        tmpdir = tempfile.mkdtemp()
        session_path = os.path.join(tmpdir, f"{session_id}.json")

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1000}

        class _FileSession:
            """Minimal session that persists to a real JSON file on save()."""
            def __init__(self):
                self.session_id = session_id
                self.active_stream_id = stream_id
                self.pending_user_message = "q"
                self.pending_attachments = []
                self.pending_started_at = 1.0
                self.path = session_path
                self.messages = [_prior]

            def save(self):
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump({"messages": self.messages}, f)

        file_session = _FileSession()

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        _b_published = [False]

        def _payload_hook(session, **kwargs):
            if not _b_published[0]:
                _b_published[0] = True
                # Publish B through the REAL production gate after CAS
                published = _streaming_mod._publish_fallback_notice(stream_id, dict(_notice_b))
                assert published is False, (
                    "post-CAS B was not blocked by the terminal fence"
                )
            return None

        with patch("api.streaming.get_session", return_value=file_session), \
             patch("api.streaming._redacted_session_payload_with_full_messages",
                   side_effect=_payload_hook):
            result = _streaming_mod.cancel_stream(stream_id)

        assert result["cancelled"] is True

        # Reload from disk and assert exactly one durable notice
        assert os.path.exists(session_path), "session was not saved to disk"
        with open(session_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        disk_messages = disk_data["messages"]

        stamped = [
            m for m in disk_messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) == 1, (
            f"expected exactly 1 durable notice on disk, got {len(stamped)}"
        )
        notice = stamped[0]["_fallbackNotice"]
        assert notice.get("to_model") == "m1", "A was not the durable notice on disk"
        assert notice.get("to_provider") == "p1"
        assert set(notice.keys()) == {"message", "to_model", "to_provider"}, (
            f"dirty keys on disk: {set(notice.keys())}"
        )

        # Registries retired (fence is retired by the worker's finally,
        # not cancel's finally — gate-certifier blocker #2).
        _streaming_mod._retire_worker_cancelled_state(stream_id)
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL

        # Cleanup
        os.unlink(session_path)
        os.rmdir(tmpdir)

    def test_cancel_first_save_failure_then_worker_retry_success(self):
        """Cancel-first save failure + worker retry success matrix:
        cancel_stream's settlement save FAILS (returns False, leaves notice
        in map), then the worker's _finalize_cancelled_turn retries and
        SUCCEEDS — stamping the notice durably and retiring all registries.

        This covers the cancel-first order of the save-failure/retry matrix
        (gate-certifier rework point 4).
        """
        from api.streaming import (
            _finalize_cancelled_turn, _retire_worker_cancelled_state,
        )

        stream_id = "cancel_first_fail_worker_retry"
        session_id = "sess_cancel_first_fail_retry"
        _notice = {"message": "fb", "to_model": "m1", "to_provider": "p1"}

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _streaming_mod._publish_fallback_notice(stream_id, _notice)

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1}
        cancel_session = Mock()
        cancel_session.session_id = session_id
        cancel_session.active_stream_id = stream_id
        cancel_session.pending_user_message = "q"
        cancel_session.pending_attachments = []
        cancel_session.pending_started_at = 1.0
        cancel_session.messages = [_prior]
        cancel_session.save = Mock(side_effect=RuntimeError("disk full"))

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=cancel_session):
            result = _streaming_mod.cancel_stream(stream_id)

        # Cancel save failed -> non-silent disposition
        assert result["cancelled"] is False
        # Notice transferred to dead-letter (bounded owner — gate-certifier
        # blocker #3: failed persistence has a named owner, not an ownerless
        # residue in _STREAM_FALLBACK_NOTICES).
        assert stream_id in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER
        # Cancel claim retired; fence was never set on the failure path
        # (settlement did not converge — gate-certifier blocker #2).
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL

        # Worker retries: _finalize_cancelled_turn with a session whose save succeeds
        worker_session = Mock()
        worker_session.session_id = session_id
        worker_session.active_stream_id = stream_id
        worker_session.pending_user_message = "q"
        worker_session.pending_attachments = []
        worker_session.pending_started_at = 1.0
        worker_session.messages = [_prior]
        worker_session.save = Mock()

        # Register session + writeback owner so the #6623 generation guard
        # in _finalize_cancelled_turn resolves the current session and
        # authorizes the finalize against this stream_id.
        from api import models, config
        models.SESSIONS[session_id] = worker_session
        config.register_session_writeback_owner(session_id, stream_id)
        _finalize_cancelled_turn(worker_session, stream_id=stream_id)
        assert stream_id in _streaming_mod._STREAM_WORKER_SAVED, (
            "worker retry did not mark stream as durably saved"
        )

        stamped = [
            m for m in worker_session.messages
            if isinstance(m, dict) and m.get("_fallbackNotice")
        ]
        assert len(stamped) >= 1, "worker retry did not persist a durable notice"

        # Worker retirement pops the exact notice and empties all registries
        _retire_worker_cancelled_state(stream_id)
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL

    def test_cancel_first_save_failure_then_worker_retry_also_fails(self):
        """Cancel-first save failure + worker retry ALSO fails matrix:
        both cancel and worker saves fail.  The notice must remain in the
        map (not silently dropped), and all ownership registries must be
        retired — no unbounded growth (gate-certifier rework point 4).
        """
        from api.streaming import (
            _finalize_cancelled_turn, _retire_worker_cancelled_state,
        )

        stream_id = "cancel_fail_worker_fail"
        session_id = "sess_cancel_fail_worker_fail"
        _notice = {"message": "fb", "to_model": "m1", "to_provider": "p1"}

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _streaming_mod._publish_fallback_notice(stream_id, _notice)

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1}
        cancel_session = Mock()
        cancel_session.session_id = session_id
        cancel_session.active_stream_id = stream_id
        cancel_session.pending_user_message = "q"
        cancel_session.pending_attachments = []
        cancel_session.pending_started_at = 1.0
        cancel_session.messages = [_prior]
        cancel_session.save = Mock(side_effect=RuntimeError("disk full"))

        mock_agent = Mock()
        mock_agent.session_id = session_id
        AGENT_INSTANCES[stream_id] = mock_agent

        with patch("api.streaming.get_session", return_value=cancel_session):
            result = _streaming_mod.cancel_stream(stream_id)

        assert result["cancelled"] is False
        assert stream_id in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER, "notice dropped after cancel save failure"

        # Worker retry also fails
        worker_session = Mock()
        worker_session.session_id = session_id
        worker_session.active_stream_id = stream_id
        worker_session.pending_user_message = "q"
        worker_session.pending_attachments = []
        worker_session.pending_started_at = 1.0
        worker_session.messages = [_prior]
        worker_session.save = Mock(side_effect=RuntimeError("disk full again"))

        _finalize_cancelled_turn(worker_session, stream_id=stream_id)
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED, (
            "worker marked saved despite save failure"
        )

        # Worker retirement must NOT pop (not durably saved)
        _retire_worker_cancelled_state(stream_id)
        # Notice still in dead-letter — not silently dropped
        assert stream_id in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER, (
            "notice silently dropped after both cancel and worker saves failed"
        )
        # But ownership registries are retired — no unbounded growth
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL

        # Cleanup
        _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.pop(stream_id, None)

    def test_cancel_first_save_failure_settlement_exhaustion_disposition(self):
        """Settlement exhaustion disposition: cancel_stream returns False
        (non-silent), notice left in map, all registries retired to bounded
        empty state.  Verifies the exact return/raise disposition and
        bounded-empty registries (gate-certifier rework point 4).
        """
        stream_id = "settlement_exhaust_disposition"
        session_id = "sess_exhaust_disposition"
        _notice = {"message": "fb", "to_model": "m1", "to_provider": "p1"}

        q = queue.Queue()
        STREAMS[stream_id] = q
        CANCEL_FLAGS[stream_id] = threading.Event()
        STREAM_PARTIAL_TEXT[stream_id] = "partial"
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []
        _streaming_mod._STREAM_SETTLEMENT_TERMINAL.clear()
        _streaming_mod._publish_fallback_notice(stream_id, _notice)

        _save_count = [0]

        def _save_then_publish_next():
            _save_count[0] += 1
            _streaming_mod._publish_fallback_notice(stream_id, {
                "message": f"gen{_save_count[0]}",
                "to_model": f"m{_save_count[0]}",
                "to_provider": f"p{_save_count[0]}",
            })

        _prior = {"role": "assistant", "content": "Prior.", "timestamp": 1}
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
            result = _streaming_mod.cancel_stream(stream_id)

        # Exact disposition: False (not True, not raise)
        assert result["cancelled"] is False, (
            f"settlement exhaustion must return False, got {result}"
        )

        # Bounded-empty registries (cancel-side)
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        # Fence was never set on the failure path (settlement did not converge)
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        # Notice in dead-letter (bounded owner — gate-certifier blocker #3)
        assert stream_id in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER
        _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.pop(stream_id, None)


class TestImmutableSnapshotInterleavings:
    """Real save/reload tests for live→dead-letter and dead-letter→live
    interleavings (gate-certifier blocker: rereading mutable registries in
    _persist_cancelled_turn can miss the notice during the handoff interval).

    These tests verify that the immutable (generation, cleaned_notice) snapshot
    captured under the owner lock survives the live→dead-letter and
    dead-letter→live transitions — the notice is NOT lost when neither
    registry contains it.
    """

    def test_live_to_dead_letter_snapshot_survives_persist(self):
        """When a notice is in _STREAM_FALLBACK_NOTICES (live) and the
        _finalize_cancelled_turn captures the snapshot, then the live entry
        is moved to dead-letter BEFORE _persist_cancelled_turn runs, the
        snapshot must still stamp the notice on the cancel marker.

        This is the exact handoff interval the gate-certifier identified:
        _finalize captures under STREAMS_LOCK, but _persist rereads the
        registries — which can be empty during the live→dead-letter gap.
        The fix passes the snapshot directly.
        """
        from api.streaming import (
            STREAMS_LOCK, _STREAM_FALLBACK_NOTICES, _STREAM_FALLBACK_DEAD_LETTER,
            _publish_fallback_notice, _clean_fallback_notice,
            _persist_cancelled_turn,
        )
        from unittest.mock import Mock

        stream_id = "interleave-live-to-dl-1"
        notice = {"message": "fb-live-to-dl", "to_model": "m1", "to_provider": "p1"}

        # Publish a live notice
        _publish_fallback_notice(stream_id, dict(notice))

        # Capture the snapshot (what _finalize_cancelled_turn does)
        with STREAMS_LOCK:
            _fb = _STREAM_FALLBACK_NOTICES.get(stream_id)
            assert _fb is not None
            from api.streaming import _current_notice_generation
            _gen = _current_notice_generation(stream_id)
            _snapshot = (_gen, _clean_fallback_notice(_fb))

            # Move the notice to dead-letter (simulating the handoff)
            # NOW: live map is empty, dead-letter has it
            _STREAM_FALLBACK_NOTICES.pop(stream_id, None)
            _STREAM_FALLBACK_DEAD_LETTER[stream_id] = {
                'notice': dict(notice),
                'generation': _gen,
                'owner_session_id': 'test',
                'owner_profile': None,
                'created_at': 0, 'updated_at': 0, 'attempts': 0,
                'next_retry_at': 0, 'terminal_status': 'failed',
            }
            # Clear live map to simulate the gap
            # At this point, if _persist rereads registries it would find
            # the dead-letter entry.  But the gate-certifier's probe forced
            # a window where NEITHER has it.  We simulate that by clearing
            # both momentarily:
            _dl_backup = _STREAM_FALLBACK_DEAD_LETTER.pop(stream_id, None)

        # NOW neither registry has the notice — the exact gap
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_FALLBACK_DEAD_LETTER

        # Create a session with no cancel marker
        mock_session = Mock()
        mock_session.session_id = "sess-interleave-1"
        mock_session.messages = []
        mock_session.pending_user_message = None
        mock_session.pending_attachments = []
        mock_session.pending_started_at = None
        mock_session.pending_user_source = None
        mock_session.profile = None

        # Call _persist_cancelled_turn WITH the snapshot
        # This must stamp the notice even though neither registry has it
        _persist_cancelled_turn(mock_session, stream_id=stream_id,
                               notice_snapshot=_snapshot)

        # Verify the notice was stamped on the cancel marker
        assert len(mock_session.messages) >= 1
        _cancel_row = mock_session.messages[-1]
        assert isinstance(_cancel_row, dict)
        assert _cancel_row.get('_fallbackNotice') is not None, (
            "notice was lost during live→dead-letter gap — the immutable "
            "snapshot must be used directly, not reread from registries"
        )
        assert _cancel_row['_fallbackNotice']['message'] == notice['message']

        # No dead-letter restoration as cleanup — the autouse fixture clears
        # every settlement registry (including dead-letters and notice
        # generations) after every test.  Restoring a dead-letter here was the
        # exact isolation leak the gate-certifier reproduced
        # (interleave-live-to-dl-1 leaked into _STREAM_FALLBACK_DEAD_LETTER
        # and _STREAM_NOTICE_GENERATION for the sibling class).

    def test_dead_letter_to_live_snapshot_survives_persist(self):
        """When a notice is in _STREAM_FALLBACK_DEAD_LETTER and the
        _finalize_cancelled_turn captures the snapshot from the dead-letter,
        then the dead-letter is cleared BEFORE _persist_cancelled_turn runs
        (e.g. by a concurrent retirement), the snapshot must still stamp
        the notice on the cancel marker.
        """
        from api.streaming import (
            STREAMS_LOCK, _STREAM_FALLBACK_NOTICES, _STREAM_FALLBACK_DEAD_LETTER,
            _clean_fallback_notice, _dead_letter_notice,
            _persist_cancelled_turn, _current_notice_generation,
        )
        from unittest.mock import Mock

        stream_id = "interleave-dl-to-live-1"
        notice = {"message": "fb-dl-to-live", "to_model": "m2", "to_provider": "p2"}

        # Put the notice in dead-letter
        with STREAMS_LOCK:
            _STREAM_FALLBACK_DEAD_LETTER[stream_id] = {
                'notice': dict(notice),
                'generation': 2,
                'owner_session_id': 'test',
                'owner_profile': None,
                'created_at': 0, 'updated_at': 0, 'attempts': 0,
                'next_retry_at': 0, 'terminal_status': 'failed',
            }

        # Capture the snapshot (what _finalize_cancelled_turn does for DL)
        with STREAMS_LOCK:
            _dl = _STREAM_FALLBACK_DEAD_LETTER.get(stream_id)
            _gen = int((_dl or {}).get('generation') or _current_notice_generation(stream_id))
            _snapshot = (_gen, _clean_fallback_notice(_dead_letter_notice(_dl)))

            # Simulate concurrent retirement clearing the dead-letter
            _STREAM_FALLBACK_DEAD_LETTER.pop(stream_id, None)

        # NOW neither registry has the notice
        assert stream_id not in _STREAM_FALLBACK_NOTICES
        assert stream_id not in _STREAM_FALLBACK_DEAD_LETTER

        # Create a session with a pre-existing cancel marker
        mock_session = Mock()
        mock_session.session_id = "sess-interleave-2"
        mock_session.messages = [
            {'role': 'user', 'content': 'hello'},
            {
                'role': 'assistant',
                'content': '**Task cancelled:** Task cancelled.\n\n*Task was cancelled.*',
                '_error': True,
                'timestamp': 1000,
            },
        ]
        mock_session.pending_user_message = None
        mock_session.pending_attachments = []
        mock_session.pending_started_at = None
        mock_session.pending_user_source = None

        # Call _persist_cancelled_turn WITH the snapshot
        _persist_cancelled_turn(mock_session, stream_id=stream_id,
                               notice_snapshot=_snapshot)

        # Verify the notice was stamped on the existing cancel marker
        _cancel_row = mock_session.messages[1]
        assert _cancel_row.get('_fallbackNotice') is not None, (
            "notice was lost during dead-letter→live gap — the immutable "
            "snapshot must be used directly, not reread from registries"
        )
        assert _cancel_row['_fallbackNotice']['message'] == notice['message']
        assert _cancel_row['_fallbackNotice']['to_model'] == notice['to_model']


class TestPostFileSettlementBaseline:
    """Post-file baseline: assert every process-global settlement registry is
    empty after the entire contributor file has run in the same process.

    The gate-certifier reproduced a leak where a sibling class in this file
    left ``_STREAM_FALLBACK_DEAD_LETTER`` and ``_STREAM_NOTICE_GENERATION``
    entries behind, which then failed an unrelated downstream session/index
    test.  This class runs LAST (pytest orders classes top-to-bottom) and
    proves the autouse fixture retired every mutated map.  If any test in the
    file leaks, this baseline catches it before an unrelated test does.
    """

    def test_all_settlement_registries_are_at_baseline(self):
        """Every settlement registry must be empty after this file's tests."""
        assert len(_streaming_mod._STREAM_FALLBACK_NOTICES) == 0, (
            f"_STREAM_FALLBACK_NOTICES leaked: "
            f"{dict(_streaming_mod._STREAM_FALLBACK_NOTICES)}"
        )
        assert len(_streaming_mod._STREAM_FALLBACK_DEAD_LETTER) == 0, (
            f"_STREAM_FALLBACK_DEAD_LETTER leaked: "
            f"{dict(_streaming_mod._STREAM_FALLBACK_DEAD_LETTER)}"
        )
        assert len(_streaming_mod._STREAM_NOTICE_GENERATION) == 0, (
            f"_STREAM_NOTICE_GENERATION leaked: "
            f"{dict(_streaming_mod._STREAM_NOTICE_GENERATION)}"
        )
        assert len(_streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS) == 0, (
            f"_STREAM_SETTLEMENT_PARTICIPANTS leaked: "
            f"{dict(_streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS)}"
        )
        assert len(_streaming_mod._STREAM_SETTLEMENT_COMPLETED) == 0, (
            f"_STREAM_SETTLEMENT_COMPLETED leaked: "
            f"{dict(_streaming_mod._STREAM_SETTLEMENT_COMPLETED)}"
        )
        assert len(_streaming_mod._STREAM_SETTLEMENT_TERMINAL) == 0, (
            f"_STREAM_SETTLEMENT_TERMINAL leaked: "
            f"{set(_streaming_mod._STREAM_SETTLEMENT_TERMINAL)}"
        )
        assert len(_streaming_mod._STREAM_CANCEL_CLAIMED) == 0, (
            f"_STREAM_CANCEL_CLAIMED leaked: "
            f"{set(_streaming_mod._STREAM_CANCEL_CLAIMED)}"
        )
        assert len(_streaming_mod._STREAM_WORKER_SAVED) == 0, (
            f"_STREAM_WORKER_SAVED leaked: "
            f"{dict(_streaming_mod._STREAM_WORKER_SAVED)}"
        )
        assert len(STREAMS) == 0, f"STREAMS leaked: {dict(STREAMS)}"
        assert len(AGENT_INSTANCES) == 0, (
            f"AGENT_INSTANCES leaked: {dict(AGENT_INSTANCES)}"
        )
        try:
            from api import models
            assert len(models.SESSIONS) == 0, (
                f"models.SESSIONS leaked: {len(models.SESSIONS)} entries"
            )
        except Exception:
            pass
