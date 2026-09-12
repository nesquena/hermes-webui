"""
Production-composed stale-reaper settlement retirement tests.

Gate-certifier blocker #3: both stale-run reapers
(api.routes._active_stream_blocks_chat_start's zombie reconciliation and
api.background_process._active_run_ids_for_session's stale-cancel cleanup)
previously removed stale active run/owner entries WITHOUT retiring settlement
participant/fence state, leaking _STREAM_SETTLEMENT_PARTICIPANTS,
_STREAM_SETTLEMENT_TERMINAL, _STREAM_SETTLEMENT_COMPLETED, notice generations,
and dead-letters for the abandoned stream.

Both reapers are now routed through one bounded abandonment helper
(_abandon_stale_stream_settlement) that retires every settlement registry.
These tests production-compose both reapers and verify all settlement state
returns to baseline.
"""
import time
import pytest

import api.streaming as _streaming_mod
from api.config import (
    ACTIVE_RUNS, ACTIVE_RUNS_LOCK, STREAMS,
    register_stream_owner,
)


def _clear_all_settlement_registries():
    STREAMS.clear()
    ACTIVE_RUNS.clear()
    _streaming_mod._STREAM_FALLBACK_NOTICES.clear()
    _streaming_mod._STREAM_CANCEL_CLAIMED.clear()
    _streaming_mod._STREAM_SETTLEMENT_TERMINAL.clear()
    _streaming_mod._STREAM_WORKER_SAVED.clear()
    _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.clear()
    _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS.clear()
    _streaming_mod._STREAM_SETTLEMENT_COMPLETED.clear()
    _streaming_mod._STREAM_NOTICE_GENERATION.clear()


@pytest.fixture(autouse=True)
def _reset_registries():
    _clear_all_settlement_registries()
    yield
    _clear_all_settlement_registries()


class TestStaleReaperSettlementRetirement:
    """Production-compose both stale-run reapers and verify settlement state
    is retired through the bounded abandonment helper."""

    def test_background_reaper_retires_settlement_on_stale_cancel(self):
        """_active_run_ids_for_session drops a stale cancelling run past its
        unwind window with no live STREAMS channel.  The abandonment helper
        must retire every settlement registry the stream populated."""
        from api.background_process import _active_run_ids_for_session
        from api.streaming import (
            _publish_fallback_notice, _set_stream_settlement_participants_locked,
            STREAMS_LOCK,
        )

        stream_id = "stale-reaper-bg-1"
        session_id = "sess-stale-bg-1"

        # Register a stale cancelling run (past the unwind ceiling).
        register_stream_owner(stream_id, session_id)
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[stream_id] = {
                "session_id": session_id,
                "stream_id": stream_id,
                "phase": "cancelling",
                "started_at": time.time() - 400,
                "cancelled_at": time.time() - 400,
            }

        # Populate settlement state for this stream.
        _publish_fallback_notice(stream_id, {
            "message": "stale fb", "to_model": "m1", "to_provider": "p1",
        })
        with STREAMS_LOCK:
            _set_stream_settlement_participants_locked(stream_id, 'worker', 'cancel')
            _streaming_mod._STREAM_SETTLEMENT_TERMINAL.add(stream_id)
            _streaming_mod._STREAM_CANCEL_CLAIMED.add(stream_id)

        # Verify state exists before reaping.
        assert stream_id in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
        assert stream_id in _streaming_mod._STREAM_SETTLEMENT_TERMINAL

        # Run the reaper — attachable_only=False so stale cancels are processed.
        _active_run_ids_for_session(session_id, attachable_only=False)

        # The stale run must be gone from ACTIVE_RUNS.
        with ACTIVE_RUNS_LOCK:
            assert stream_id not in ACTIVE_RUNS

        # ALL settlement registries must be at baseline for this stream.
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        assert stream_id not in _streaming_mod._STREAM_NOTICE_GENERATION
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER

    def test_routes_reaper_retires_settlement_on_zombie_run(self):
        """The routes.py zombie reconciliation path (inside the function that
        _active_stream_blocks_chat_start feeds into) drops stale runs past
        the unwind ceiling with no live STREAMS channel.  The abandonment
        helper must retire settlement state."""
        from api.streaming import (
            _publish_fallback_notice, _set_stream_settlement_participants_locked,
            STREAMS_LOCK,
        )

        stream_id = "stale-reaper-routes-1"
        session_id = "sess-stale-routes-1"

        # Register a stale cancelling run.
        register_stream_owner(stream_id, session_id)
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[stream_id] = {
                "session_id": session_id,
                "stream_id": stream_id,
                "phase": "cancelling",
                "started_at": time.time() - 400,
                "cancelled_at": time.time() - 400,
            }

        # Populate settlement state.
        _publish_fallback_notice(stream_id, {
            "message": "stale fb routes", "to_model": "m2", "to_provider": "p2",
        })
        with STREAMS_LOCK:
            _set_stream_settlement_participants_locked(stream_id, 'worker')
            _streaming_mod._STREAM_SETTLEMENT_TERMINAL.add(stream_id)

        # Verify state exists.
        assert stream_id in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id in _streaming_mod._STREAM_SETTLEMENT_TERMINAL

        # Invoke the production routes reaper and prove its abandonment call
        # runs after ACTIVE_RUNS_LOCK is released. A competing worker teardown
        # must be able to acquire that lock while abandonment takes STREAMS_LOCK.
        import threading
        from unittest.mock import patch
        from api.routes import _active_run_stream_for_session

        real_abandon = _streaming_mod._abandon_stale_stream_settlement
        lock_acquired = threading.Event()

        def probing_abandon(raw_stream_id):
            def acquire_active_runs():
                with ACTIVE_RUNS_LOCK:
                    lock_acquired.set()
            contender = threading.Thread(target=acquire_active_runs)
            contender.start()
            assert lock_acquired.wait(1), (
                "routes reaper called settlement abandonment while holding "
                "ACTIVE_RUNS_LOCK (ABBA deadlock with worker teardown)"
            )
            contender.join(1)
            real_abandon(raw_stream_id)

        with patch.object(
            _streaming_mod, "_abandon_stale_stream_settlement",
            side_effect=probing_abandon,
        ):
            assert _active_run_stream_for_session(session_id) is None

        # ALL settlement registries must be at baseline.
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        assert stream_id not in _streaming_mod._STREAM_NOTICE_GENERATION

    def test_abandonment_is_idempotent_on_clean_stream(self):
        """Abandoning a stream with no settlement state is a no-op."""
        from api.streaming import _abandon_stale_stream_settlement

        stream_id = "clean-stream-idempotent"
        # No settlement state exists for this stream.
        _abandon_stale_stream_settlement(stream_id)
        # Still nothing — no tombstones created.
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES

    def test_repeated_normal_streams_leave_no_settlement_tombstone(self):
        """Repeated production-composed normal completions (no cancellation)
        must not leak settlement tombstones — the abandonment helper's
        underlying _retire_worker_cancelled_state_locked has an early return
        for streams with no cancellation state."""
        from api.streaming import _abandon_stale_stream_settlement

        for i in range(5):
            sid = f"normal-stream-{i}"
            # Simulate a normal completed stream — no settlement state at all.
            _abandon_stale_stream_settlement(sid)
            # No tombstone should be created.
            assert sid not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
            assert sid not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS

        # Global registries must still be empty after 5 iterations.
        assert len(_streaming_mod._STREAM_SETTLEMENT_COMPLETED) == 0
        assert len(_streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS) == 0
