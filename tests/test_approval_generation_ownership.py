"""Regression tests for generation-aware gateway approval enqueue ownership.

PR #6208 re-gate (issue #6100): the previous tombstone design left two
lifecycle regressions —

1. The callback-snapshot → late-enqueue race stayed open: a worker that
   snapshotted the notify callback before teardown could append its
   ``_ApprovalEntry`` AFTER ``unregister_gateway_notify`` drained the queue.
   The WebUI tombstone hid the mirror but never removed or signaled that
   agent-side entry, so the worker stayed blocked.

2. The tombstone was never cleared: after the first teardown,
   ``reconcile_gateway_pending_mirror_locked`` treated the session's gateway
   queue as empty forever, so a second conversation turn on the same session
   could not rebuild the typed pending mirror.

The fix replaces the permanent session-id tombstone with generation-aware
ownership: every register (``begin_gateway_notify_ownership``) and every
teardown (``end_gateway_notify_ownership``) advances a per-session
generation; the callback wrapper carries its generation and, when stale,
signals ONLY its exact entry (``entry.data is approval`` — the agent's
``_await_gateway_decision`` passes the same dict object to the callback) so
the worker returns promptly without recreating a queue/mirror.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from api import route_approvals as ra


def _make_approval(approval_id: str, run_id: str | None = None) -> dict:
    data = {
        "command": "rm -rf /tmp/test",
        "description": f"Approval {approval_id}",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
        "approval_id": approval_id,
        "choices": ["once", "session", "always", "deny"],
    }
    if run_id is not None:
        data["run_id"] = run_id
    return data


def _drain_gateway_queue(session_key: str) -> None:
    """Pop + signal all entries — mirrors unregister_gateway_notify's drain."""
    with ra._lock:
        entries = ra._gateway_queues.pop(session_key, [])
    for entry in entries:
        entry.event.set()


def _cleanup(session_key: str) -> None:
    with ra._lock:
        ra._gateway_queues.pop(session_key, None)
        ra._pending.pop(session_key, None)
        ra._approval_tombstones.discard(session_key)
        ra._gateway_notify_generations.pop(session_key, None)


def test_stale_callback_late_enqueue_signals_exact_entry_no_mirror():
    """Barrier: callback snapshot → unregister/teardown → late enqueue.

    A worker that snapshotted the callback before teardown and then appends
    its entry AFTER the queue was drained must not recreate a queue/mirror —
    the stale callback signals only its exact entry so the worker returns
    promptly (#6100 re-gate regression #1).
    """
    sid = "sess-6208-stale-late"
    approval_data = _make_approval("appr-6208-stale-late", "run-6208-stale-late")
    _cleanup(sid)

    try:
        # Stream start: a generation opens for the session.
        gen = ra.begin_gateway_notify_ownership(sid)
        assert gen == 1

        # Deterministic barrier — the worker has ALREADY snapshotted the
        # callback.  The stream tears down: unregister (pops the callback
        # and drains the queue) + end ownership.
        _drain_gateway_queue(sid)
        ra.end_gateway_notify_ownership(sid)
        assert sid in ra._approval_tombstones

        # The worker loses the race and appends its entry AFTER the drain.
        entry = SimpleNamespace(data=dict(approval_data), event=threading.Event(), result=None)
        with ra._lock:
            ra._gateway_queues.setdefault(sid, []).append(entry)

        # The stale callback fires (captured generation is now stale).
        mirrored = ra.submit_gateway_pending_mirror(sid, entry.data, generation=gen)

        assert mirrored is False, "stale callback must not mirror"
        assert entry.event.is_set(), "stale callback must signal the worker's exact entry"
        with ra._lock:
            queue = ra._gateway_queues.get(sid)
            assert not queue, f"no queue residue expected, got {queue!r}"
            assert sid not in ra._pending, "no mirror may reappear after teardown"
    finally:
        _cleanup(sid)


def test_stale_callback_after_newer_stream_takeover_signals_only_own_entry():
    """A stale callback must never wipe a newer stream's queue or mirror.

    When a newer stream takes ownership of the session, a late callback from
    the OLD stream must signal only its exact entry — the newer stream's
    queue entries and typed mirror stay intact (#6208 regression #1, second
    half).
    """
    sid = "sess-6208-newer-stream"
    old_data = _make_approval("appr-6208-old", "run-6208-old")
    new_data = _make_approval("appr-6208-new", "run-6208-new")
    _cleanup(sid)

    try:
        # Turn 1 (old stream) opens ownership and enqueues.
        gen_old = ra.begin_gateway_notify_ownership(sid)
        entry_old = SimpleNamespace(data=dict(old_data), event=threading.Event(), result=None)
        with ra._lock:
            ra._gateway_queues.setdefault(sid, []).append(entry_old)
        # Turn 1 tears down (drain + end ownership).
        _drain_gateway_queue(sid)
        ra.end_gateway_notify_ownership(sid)

        # Turn 2 (newer stream) reopens the lifecycle and mirrors its head.
        gen_new = ra.begin_gateway_notify_ownership(sid)
        assert gen_new > gen_old
        assert sid not in ra._approval_tombstones, "tombstone must reopen for the next turn"
        entry_new = SimpleNamespace(data=dict(new_data), event=threading.Event(), result=None)
        with ra._lock:
            ra._gateway_queues.setdefault(sid, []).append(entry_new)
        assert ra.submit_gateway_pending_mirror(sid, entry_new.data, generation=gen_new) is True
        with ra._lock:
            pending = ra._pending[sid]
            assert pending[0]["approval_id"] == new_data["approval_id"]
            assert pending[0]["run_id"] == new_data["run_id"]
            assert pending[0].get(ra._GATEWAY_MIRROR_FLAG) is True

        # The OLD worker's late enqueue now lands in the newer stream's queue.
        with ra._lock:
            ra._gateway_queues.setdefault(sid, []).append(entry_old)
        assert ra.submit_gateway_pending_mirror(sid, entry_old.data, generation=gen_old) is False

        assert entry_old.event.is_set(), "stale entry must be signaled"
        assert not entry_new.event.is_set(), "newer stream's live entry must be untouched"
        with ra._lock:
            queue = ra._gateway_queues.get(sid) or []
            assert entry_new in queue, "newer stream's entry must survive"
            assert entry_old not in queue, "stale entry must be removed"
            pending = ra._pending.get(sid)
            assert pending and pending[0]["approval_id"] == new_data["approval_id"], (
                "mirror must still reflect the newer stream's head"
            )
    finally:
        _cleanup(sid)


def test_second_turn_same_session_rebuilds_typed_gateway_mirror():
    """Regression #2: a second sequential turn rebuilds the typed mirror.

    The permanent session-id tombstone made
    ``reconcile_gateway_pending_mirror_locked`` treat the session's gateway
    queue as empty forever.  With generation-aware ownership the lifecycle
    reopens on the next registration, so the second turn can create a typed
    mirror carrying its own approval_id and run_id.
    """
    sid = "sess-6208-two-turns"
    turn1_data = _make_approval("appr-6208-turn1", "run-6208-turn1")
    turn2_data = _make_approval("appr-6208-turn2", "run-6208-turn2")
    _cleanup(sid)

    try:
        # ── Turn 1: register, enqueue, mirror, teardown ──────────────────
        gen1 = ra.begin_gateway_notify_ownership(sid)
        entry1 = SimpleNamespace(data=dict(turn1_data), event=threading.Event(), result=None)
        with ra._lock:
            ra._gateway_queues.setdefault(sid, []).append(entry1)
        assert ra.submit_gateway_pending_mirror(sid, entry1.data, generation=gen1) is True
        with ra._lock:
            assert ra._pending[sid][0]["approval_id"] == turn1_data["approval_id"]
            assert ra._pending[sid][0]["run_id"] == turn1_data["run_id"]
            assert ra._pending[sid][0].get(ra._GATEWAY_MIRROR_FLAG) is True

        _drain_gateway_queue(sid)
        ra.end_gateway_notify_ownership(sid)
        ra.force_clean_pending_approvals(sid)
        with ra._lock:
            assert sid not in ra._pending, "turn-1 stale mirror must be purged"
            assert sid in ra._approval_tombstones

        # ── Turn 2: same session — lifecycle must reopen ─────────────────
        gen2 = ra.begin_gateway_notify_ownership(sid)
        assert gen2 > gen1
        assert sid not in ra._approval_tombstones, (
            "tombstone must be cleared/advanced before registering the next callback"
        )
        entry2 = SimpleNamespace(data=dict(turn2_data), event=threading.Event(), result=None)
        with ra._lock:
            ra._gateway_queues.setdefault(sid, []).append(entry2)
        assert ra.submit_gateway_pending_mirror(sid, entry2.data, generation=gen2) is True

        with ra._lock:
            pending = ra._pending.get(sid)
            assert pending, "second turn must rebuild the typed gateway approval mirror"
            assert pending[0]["approval_id"] == turn2_data["approval_id"]
            assert pending[0]["run_id"] == turn2_data["run_id"]
            assert pending[0].get(ra._GATEWAY_MIRROR_FLAG) is True
    finally:
        _cleanup(sid)
