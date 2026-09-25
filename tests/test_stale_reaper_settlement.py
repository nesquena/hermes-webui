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
(_abandon_stale_stream_settlement) that retires every settlement registry
WITHOUT discarding an accepted notice before its deadline:

- an unexpired live notice is transferred to the owner-scoped
  _STREAM_FALLBACK_DEAD_LETTER (with owner attribution snapshotted by the
  reaper BEFORE it unregisters the stream owner);
- an existing unexpired dead-letter is PRESERVED until its own deadline_at —
  compare-delete after persistence belongs to the settlement paths, not
  stale-run abandonment;
- expired entries retire, and repeated cleanup is idempotent and bounded.

These tests production-compose both reapers and verify the ownership/
durability invariant plus full settlement baseline.
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


def _seed_dead_letter(stream_id, notice, generation=1, owner_session_id=None,
                      deadline_at=None, terminal_status='failed'):
    """Seed a dead-letter entry directly with controllable deadline metadata."""
    now = time.time()
    with _streaming_mod.STREAMS_LOCK:
        _streaming_mod._STREAM_FALLBACK_DEAD_LETTER[stream_id] = {
            'notice': _streaming_mod._clean_fallback_notice(notice),
            'generation': generation,
            'owner_session_id': owner_session_id,
            'owner_profile': None,
            'created_at': now,
            'updated_at': now,
            'attempts': 1,
            'next_retry_at': now,
            'deadline_at': deadline_at if deadline_at is not None else now + 300,
            'terminal_status': terminal_status,
        }


def _register_stale_cancelling_run(stream_id, session_id):
    """Register a stale cancelling run past the 180s unwind ceiling with no
    live STREAMS channel — the exact production shape both reapers reap."""
    register_stream_owner(stream_id, session_id)
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS[stream_id] = {
            "session_id": session_id,
            "stream_id": stream_id,
            "phase": "cancelling",
            "started_at": time.time() - 400,
            "cancelled_at": time.time() - 400,
        }


class TestStaleReaperSettlementRetirement:
    """Production-compose both stale-run reapers and verify settlement state
    is retired through the bounded abandonment helper while an accepted
    notice survives to its bounded owner."""

    def test_background_reaper_transfers_unexpired_notice_to_dead_letter(self):
        """_active_run_ids_for_session drops a stale cancelling run past its
        unwind window with no live STREAMS channel.  The abandonment helper
        must retire every settlement registry the stream populated AND
        preserve the accepted notice by transferring its exact
        (generation, notice) into the owner-scoped dead-letter with the
        owner attribution snapshotted before the reaper unregisters it."""
        from api.background_process import _active_run_ids_for_session
        from api.streaming import (
            _publish_fallback_notice, _set_stream_settlement_participants_locked,
            STREAMS_LOCK,
        )

        stream_id = "stale-reaper-bg-1"
        session_id = "sess-stale-bg-1"

        # Register a stale cancelling run (past the unwind ceiling).
        _register_stale_cancelling_run(stream_id, session_id)

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

        # The accepted live notice must NOT be silently discarded: its exact
        # generation and content transfer into the bounded dead-letter owner.
        entry = _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.get(stream_id)
        assert entry is not None, (
            "stale cleanup discarded the accepted notice instead of "
            "transferring it to the owner-scoped dead-letter"
        )
        assert entry['notice'] == {
            "message": "stale fb", "to_model": "m1", "to_provider": "p1",
        }
        assert entry['generation'] == 1
        assert entry['owner_session_id'] == session_id, (
            "dead-letter must carry the owner attribution snapshotted before "
            "unregister_stream_owner erased it"
        )
        assert entry['deadline_at'] > time.time(), (
            "transferred notice must get its own full unexpired deadline"
        )

        # All OTHER settlement registries must be at baseline for this stream.
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        assert stream_id not in _streaming_mod._STREAM_NOTICE_GENERATION

    def test_routes_reaper_retires_settlement_on_zombie_run(self):
        """The routes.py zombie reconciliation path (inside the function that
        _active_stream_blocks_chat_start feeds into) drops stale runs past
        the unwind ceiling with no live STREAMS channel.  The abandonment
        helper must retire settlement state and preserve the unexpired
        accepted notice in its dead-letter owner."""
        from api.streaming import (
            _publish_fallback_notice, _set_stream_settlement_participants_locked,
            STREAMS_LOCK,
        )

        stream_id = "stale-reaper-routes-1"
        session_id = "sess-stale-routes-1"

        # Register a stale cancelling run.
        _register_stale_cancelling_run(stream_id, session_id)

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

        def probing_abandon(raw_stream_id, owner_session_id=None):
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
            real_abandon(raw_stream_id, owner_session_id=owner_session_id)

        with patch.object(
            _streaming_mod, "_abandon_stale_stream_settlement",
            side_effect=probing_abandon,
        ):
            assert _active_run_stream_for_session(session_id) is None

        # The accepted notice must survive in its bounded owner with exact
        # generation/content and pre-unregistration owner attribution.
        entry = _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.get(stream_id)
        assert entry is not None, (
            "routes reaper discarded the accepted notice before its deadline"
        )
        assert entry['notice'] == {
            "message": "stale fb routes", "to_model": "m2", "to_provider": "p2",
        }
        assert entry['generation'] == 1
        assert entry['owner_session_id'] == session_id

        # ALL other settlement registries must be at baseline.
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        assert stream_id not in _streaming_mod._STREAM_NOTICE_GENERATION

    def test_reaper_preserves_unexpired_preexisting_dead_letter(self):
        """A seeded unexpired dead-letter (a prior failed persistence) must
        survive stale-run abandonment until its own deadline_at: compare-
        delete after persistence belongs to the settlement paths, not to
        stale-run cleanup."""
        from api.background_process import _active_run_ids_for_session
        from api.streaming import _publish_fallback_notice

        stream_id = "stale-reaper-dl-keep"
        session_id = "sess-stale-dl-keep"
        _register_stale_cancelling_run(stream_id, session_id)

        dl_notice = {
            "message": "already dead-lettered",
            "to_model": "m3", "to_provider": "p3",
        }
        _seed_dead_letter(stream_id, dl_notice, generation=2,
                          owner_session_id=session_id)
        # No live notice this time — only the pre-existing dead-letter owner.
        _publish_fallback_notice(stream_id, {
            "message": "live newer", "to_model": "m4", "to_provider": "p4",
        })

        _active_run_ids_for_session(session_id, attachable_only=False)

        entry = _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.get(stream_id)
        assert entry is not None, (
            "unexpired dead-letter was discarded by stale cleanup before its "
            "deadline_at"
        )
        # The seeded dead-letter has generation 2; a live generation 1
        # cannot replace it merely because it was encountered by the reaper.
        assert entry['notice']['message'] == "already dead-lettered"
        assert entry['generation'] == 2
        assert entry['owner_session_id'] == session_id
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS

    def test_reaper_does_not_replace_newer_dead_letter_with_older_live_notice(self):
        """A newer failed generation retains ownership when stale cleanup runs."""
        from api.background_process import _active_run_ids_for_session
        from api.streaming import _publish_fallback_notice

        stream_id = "stale-reaper-newer-dead-letter"
        session_id = "sess-stale-newer-dead-letter"
        _register_stale_cancelling_run(stream_id, session_id)
        _publish_fallback_notice(stream_id, {
            "message": "older live", "to_model": "m1", "to_provider": "p1",
        })
        _seed_dead_letter(stream_id, {
            "message": "newer failed", "to_model": "m2", "to_provider": "p2",
        }, generation=2, owner_session_id=session_id)

        _active_run_ids_for_session(session_id, attachable_only=False)

        entry = _streaming_mod._STREAM_FALLBACK_DEAD_LETTER[stream_id]
        assert entry["generation"] == 2
        assert entry["notice"]["message"] == "newer failed"
        assert entry["owner_session_id"] == session_id
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES

    def test_reaper_discards_expired_notice_and_dead_letter(self):
        """Both a live notice and a dead-letter seeded with a PAST deadline
        must be fully retired by the reaper — the bounded lifecycle owns
        expiry, and expired entries leave no residue."""
        from api.background_process import _active_run_ids_for_session
        from api.streaming import _publish_fallback_notice

        stream_id = "stale-reaper-expired"
        session_id = "sess-stale-expired"
        _register_stale_cancelling_run(stream_id, session_id)

        _publish_fallback_notice(stream_id, {
            "message": "expired live", "to_model": "m5", "to_provider": "p5",
        })
        _seed_dead_letter(
            stream_id,
            {"message": "expired dl", "to_model": "m6", "to_provider": "p6"},
            generation=2, owner_session_id=session_id,
            deadline_at=time.time() - 10,
        )

        _active_run_ids_for_session(session_id, attachable_only=False)

        # Past-deadline entries retire; nothing survives in any registry.
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL
        assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
        assert stream_id not in _streaming_mod._STREAM_CANCEL_CLAIMED
        assert stream_id not in _streaming_mod._STREAM_WORKER_SAVED
        assert stream_id not in _streaming_mod._STREAM_NOTICE_GENERATION

    def test_repeated_stale_cleanup_is_idempotent_and_bounded(self):
        """Repeated reaper passes over the same abandoned stream must be
        idempotent: the first pass transfers/preserves within deadline,
        and every later pass retires exactly-once with zero residue and no
        registry growth."""
        from api.background_process import _active_run_ids_for_session
        from api.streaming import _publish_fallback_notice

        stream_id = "stale-reaper-repeat"
        session_id = "sess-stale-repeat"
        _register_stale_cancelling_run(stream_id, session_id)
        _publish_fallback_notice(stream_id, {
            "message": "repeat fb", "to_model": "m7", "to_provider": "p7",
        })

        # First pass: re-register the stale run each time, as the production
        # reaper only sees a run while its ACTIVE_RUNS row still exists.
        _active_run_ids_for_session(session_id, attachable_only=False)
        first = _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.get(stream_id)
        assert first is not None
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES

        for round_no in range(2, 6):
            _register_stale_cancelling_run(stream_id, session_id)
            _active_run_ids_for_session(session_id, attachable_only=False)
            again = _streaming_mod._STREAM_FALLBACK_DEAD_LETTER.get(stream_id)
            assert again is first, (
                f"repeated cleanup round {round_no} replaced/recreated the "
                "dead-letter owner instead of leaving it untouched until its "
                "deadline"
            )
            assert stream_id not in _streaming_mod._STREAM_FALLBACK_NOTICES
            assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS
            assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_COMPLETED
            assert stream_id not in _streaming_mod._STREAM_SETTLEMENT_TERMINAL

        # Expire the deadline: the next pass must retire the entry exactly
        # once, and further passes stay no-ops.
        with _streaming_mod.STREAMS_LOCK:
            _streaming_mod._STREAM_FALLBACK_DEAD_LETTER[stream_id]['deadline_at'] = (
                time.time() - 1
            )
        _register_stale_cancelling_run(stream_id, session_id)
        _active_run_ids_for_session(session_id, attachable_only=False)
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER
        _register_stale_cancelling_run(stream_id, session_id)
        _active_run_ids_for_session(session_id, attachable_only=False)
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER
        assert len(_streaming_mod._STREAM_SETTLEMENT_COMPLETED) == 0
        assert len(_streaming_mod._STREAM_SETTLEMENT_PARTICIPANTS) == 0

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
        assert stream_id not in _streaming_mod._STREAM_FALLBACK_DEAD_LETTER

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
