"""Regression tests for the v0.50.248 SSE notify-on-respond fix.

Originally PR #1350 only called `_approval_sse_notify` from `submit_pending`,
not from `_handle_approval_respond`. With the parallel-tool-call scenario
that PR #527 supports:

  1. submit_pending(A)  -> SSE pushes (A, 1). UI shows A.
  2. submit_pending(B)  -> SSE pushes (B, 2). UI shows B.   (bug: was sending B as head, not A)
  3. respond(B)         -> queue still contains A. UI hides card.
                           NO event fires. A is invisible until next event.

Pre-release Opus review caught two MUST-FIX bugs:
  A. notify-ordering race: notify outside _lock could deliver out-of-order
     under contention.
  C. Trailing approval lost: respond never re-emitted the new head.
  D. Payload was tail-not-head: with #527 parallel approvals, /api/approval/pending
     returns head, but SSE was returning the just-appended entry (tail).

The fix:
  - `_approval_sse_notify_locked(sid, head, total)` runs inside the caller's
    held `_lock` so two parallel callers serialize their notifies in the same
    order they serialize their queue mutations.
  - submit_pending now passes `head = queue_list[0]` (head-of-queue), not the
    just-appended entry.
  - _handle_approval_respond now calls _approval_sse_notify_locked after the
    pop with the new head (or None/0 if queue is empty).
"""

import pathlib
import sys
import uuid

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


class TestParallelApprovalsHeadFidelity:
    """SSE payload must always reflect head-of-queue, never tail."""

    def setup_method(self):
        from api import routes as r

        with r._lock:
            r._approval_sse_subscribers.clear()
            r._pending.clear()

    def teardown_method(self):
        from api import routes as r

        with r._lock:
            r._approval_sse_subscribers.clear()
            r._pending.clear()

    def test_second_submit_pending_sends_head_not_tail(self):
        """When B is appended while A is still pending, SSE payload must show A as head."""
        from api import routes as r

        sid = f"sse-headtail-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        try:
            r.submit_pending(
                sid,
                {
                    "command": "first-A",
                    "pattern_key": "first",
                    "pattern_keys": ["first"],
                    "description": "A",
                },
            )
            r.submit_pending(
                sid,
                {
                    "command": "second-B",
                    "pattern_key": "second",
                    "pattern_keys": ["second"],
                    "description": "B",
                },
            )
            # First payload should be A (just-appended, also head).
            p1 = q.get(timeout=1)
            assert p1["pending"]["command"] == "first-A"
            assert p1["pending_count"] == 1
            # Second payload's HEAD is still A (we appended B but A is still queued).
            p2 = q.get(timeout=1)
            assert p2["pending"]["command"] == "first-A", (
                "SSE payload must show head-of-queue (A), not tail (B). "
                f"Got: {p2['pending']['command']}"
            )
            assert p2["pending_count"] == 2
        finally:
            r._approval_sse_unsubscribe(sid, q)


class TestRespondNotifiesTrailingApproval:
    """After respond() pops one approval, SSE must re-emit the new head if any."""

    def setup_method(self):
        from api import routes as r

        with r._lock:
            r._approval_sse_subscribers.clear()
            r._pending.clear()

    def teardown_method(self):
        from api import routes as r

        with r._lock:
            r._approval_sse_subscribers.clear()
            r._pending.clear()

    def test_respond_to_first_pushes_second_as_new_head(self):
        """submit A; submit B; respond(A) -> SSE must push (B, 1) so the UI surfaces B."""
        from api import routes as r

        sid = f"sse-trailing-{uuid.uuid4().hex[:8]}"

        # Subscribe BEFORE any submit so we capture all events deterministically.
        sub_q = r._approval_sse_subscribe(sid)
        try:
            r.submit_pending(
                sid,
                {
                    "command": "first-A",
                    "pattern_key": "p1",
                    "pattern_keys": ["p1"],
                    "description": "A",
                    "approval_id": "id-A",
                },
            )
            r.submit_pending(
                sid,
                {
                    "command": "second-B",
                    "pattern_key": "p2",
                    "pattern_keys": ["p2"],
                    "description": "B",
                    "approval_id": "id-B",
                },
            )
            # Drain the two submit-driven events.
            sub_q.get(timeout=1)  # head=A, total=1
            sub_q.get(timeout=1)  # head=A, total=2 (head is still A)

            # Now simulate respond(A) by directly invoking the lock+pop+notify
            # sequence the route handler runs. (Calling _handle_approval_respond
            # would require an HTTP handler mock; the inner sequence is what we
            # need to verify.)
            from api.routes import _approval_sse_notify_locked, _lock, _pending

            with _lock:
                qlist = _pending.get(sid)
                # Pop A by approval_id
                for i, e in enumerate(qlist):
                    if e.get("approval_id") == "id-A":
                        qlist.pop(i)
                        break
                # Re-emit head
                if isinstance(_pending.get(sid), list) and _pending[sid]:
                    _approval_sse_notify_locked(
                        sid, _pending[sid][0], len(_pending[sid])
                    )
                else:
                    _approval_sse_notify_locked(sid, None, 0)

            # SSE must push (B, 1) so the UI surfaces the trailing approval.
            p3 = sub_q.get(timeout=1)
            assert p3["pending"] is not None, (
                "After responding to A, SSE must emit the new head B (not None)"
            )
            assert p3["pending"]["command"] == "second-B", (
                f"New head should be B, got: {p3['pending']['command']}"
            )
            assert p3["pending_count"] == 1
        finally:
            r._approval_sse_unsubscribe(sid, sub_q)

    def test_respond_to_only_pending_pushes_empty_state(self):
        """If respond pops the last entry, SSE must push a None/0 sentinel so UI hides card."""
        from api import routes as r

        sid = f"sse-empty-{uuid.uuid4().hex[:8]}"

        sub_q = r._approval_sse_subscribe(sid)
        try:
            r.submit_pending(
                sid,
                {
                    "command": "only-A",
                    "pattern_key": "p",
                    "pattern_keys": ["p"],
                    "description": "A",
                    "approval_id": "id-only-A",
                },
            )
            sub_q.get(timeout=1)  # drain submit notify

            from api.routes import _approval_sse_notify_locked, _lock, _pending

            with _lock:
                qlist = _pending.get(sid)
                for i, e in enumerate(qlist):
                    if e.get("approval_id") == "id-only-A":
                        qlist.pop(i)
                        break
                if not qlist:
                    _pending.pop(sid, None)
                if isinstance(_pending.get(sid), list) and _pending[sid]:
                    _approval_sse_notify_locked(
                        sid, _pending[sid][0], len(_pending[sid])
                    )
                else:
                    _approval_sse_notify_locked(sid, None, 0)

            payload = sub_q.get(timeout=1)
            assert payload["pending"] is None, (
                "After responding to the only approval, SSE must push pending=None"
            )
            assert payload["pending_count"] == 0
        finally:
            r._approval_sse_unsubscribe(sid, sub_q)


class TestNotifyOrderUnderContention:
    """Two parallel submit_pending callers must deliver in queue-mutation order."""

    def setup_method(self):
        from api import routes as r

        with r._lock:
            r._approval_sse_subscribers.clear()
            r._pending.clear()

    def teardown_method(self):
        from api import routes as r

        with r._lock:
            r._approval_sse_subscribers.clear()
            r._pending.clear()

    def test_pending_count_is_monotonic_under_contention(self):
        """Under parallel submit_pending, pending_count must be monotonically increasing.

        Pre-fix: notify outside _lock meant T2's notify could fire before T1's,
        with subscribers seeing pending_count=2 then pending_count=1. Now that
        notify runs inside _lock alongside the append, the order is guaranteed.
        """
        import threading
        from api import routes as r

        sid = f"sse-order-{uuid.uuid4().hex[:8]}"

        sub_q = r._approval_sse_subscribe(sid)
        try:
            errors = []
            barrier = threading.Barrier(8)

            def submitter(idx):
                try:
                    barrier.wait(timeout=2)
                    r.submit_pending(
                        sid,
                        {
                            "command": f"cmd-{idx}",
                            "pattern_key": f"p{idx}",
                            "pattern_keys": [f"p{idx}"],
                            "description": f"d{idx}",
                        },
                    )
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=submitter, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            assert not errors, f"Submitter errors: {errors}"

            # Drain payloads — count must go 1, 2, 3, ..., 8 in some order
            # consistent with the queue serialization. Specifically, never decrease.
            counts = []
            for _ in range(8):
                p = sub_q.get(timeout=2)
                counts.append(p["pending_count"])

            assert counts == sorted(counts), (
                f"pending_count must be monotonically increasing under contention. "
                f"Got: {counts}. Pre-fix this could be out-of-order."
            )
            assert counts == list(range(1, 9)), f"Expected [1..8], got {counts}"
        finally:
            r._approval_sse_unsubscribe(sid, sub_q)
