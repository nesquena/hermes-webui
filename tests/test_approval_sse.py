"""Tests for the approval SSE (Server-Sent Events) long-connection implementation.

Verifies:
  - SSE subscribe/unsubscribe/notify lifecycle
  - Initial snapshot delivery on connect
  - Instant push when submit_pending() fires
  - Client disconnect triggers unsubscribe cleanup
  - Multiple concurrent subscribers per session
  - Queue overflow (slow subscriber) drops silently
  - Cross-session isolation (notify only reaches matching subscribers)
  - Frontend EventSource / fallback polling patterns
"""

import json
import pathlib
import queue
import re
import sys
import threading
import time
import uuid

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

ROUTES_SRC = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
# Approval SSE state and helpers live in route_approvals after the #1907
# extraction; combine both files so structural assertions below still pass.
_ROUTE_APPROVALS = REPO_ROOT / "api" / "route_approvals.py"
APPROVAL_SRC = _ROUTE_APPROVALS.read_text(encoding="utf-8") if _ROUTE_APPROVALS.exists() else ""
ROUTES_SRC_FULL = ROUTES_SRC + APPROVAL_SRC
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Static-analysis tests (no server needed)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSSEStaticAnalysis:
    """Verify the SSE infrastructure exists and is wired correctly in routes.py."""

    def test_sse_route_registered(self):
        """The /api/approval/stream route must be registered."""
        assert '"/api/approval/stream"' in ROUTES_SRC, \
            "Route /api/approval/stream must be registered in the URL dispatch"

    def test_sse_handler_function_exists(self):
        """_handle_approval_sse_stream handler must exist."""
        assert "def _handle_approval_sse_stream(" in ROUTES_SRC, \
            "_handle_approval_sse_stream handler function must exist"

    def test_subscribe_function_exists(self):
        """_approval_sse_subscribe must exist and use a Queue."""
        assert "def _approval_sse_subscribe(" in ROUTES_SRC_FULL, \
            "_approval_sse_subscribe must be defined"

    def test_unsubscribe_function_exists(self):
        """_approval_sse_unsubscribe must exist and clean up empty lists."""
        assert "def _approval_sse_unsubscribe(" in ROUTES_SRC_FULL, \
            "_approval_sse_unsubscribe must be defined"

    def test_notify_function_exists(self):
        """_approval_sse_notify must exist and push to subscriber queues."""
        assert "def _approval_sse_notify(" in ROUTES_SRC_FULL, \
            "_approval_sse_notify must be defined"

    def test_sse_subscribers_dict_exists(self):
        """Module-level _approval_sse_subscribers dict must exist."""
        assert "_approval_sse_subscribers" in ROUTES_SRC, \
            "_approval_sse_subscribers module-level dict must exist"

    def test_sse_content_type(self):
        """SSE handler must set text/event-stream content type."""
        assert "text/event-stream" in ROUTES_SRC, \
            "SSE handler must set Content-Type to text/event-stream"

    def test_sse_keepalive(self):
        """SSE handler must send keepalive comments to prevent proxy timeout."""
        assert "keepalive" in ROUTES_SRC, \
            "SSE handler must send keepalive comments"

    def test_sse_cache_control(self):
        """SSE handler must set Cache-Control: no-cache."""
        assert "no-cache" in ROUTES_SRC, \
            "SSE handler must set Cache-Control: no-cache"

    def test_sse_initial_snapshot(self):
        """SSE handler must send initial snapshot on connect."""
        assert "'initial'" in ROUTES_SRC, \
            "SSE handler must send an 'initial' event with snapshot data"

    def test_sse_approval_event(self):
        """SSE handler must send 'approval' events on push."""
        assert "'approval'" in ROUTES_SRC, \
            "SSE handler must send 'approval' events when pushing notifications"

    def test_notify_called_from_submit_pending(self):
        """submit_pending must call _approval_sse_notify_locked."""
        # Pinned to the inner-lock variant: must run inside the same `with _lock:`
        # block as the queue mutation so two parallel submit_pending calls can't
        # deliver out-of-order with stale pending_count. Tracks the v0.50.248
        # MUST-FIX A fix.
        assert "_approval_sse_notify_locked(session_key, head, total)" in ROUTES_SRC_FULL, \
            ("submit_pending() must call _approval_sse_notify_locked(session_key, head, total) "
             "from inside the `with _lock:` block — not the unlocked _approval_sse_notify wrapper, "
             "and head must be queue_list[0] (the head, not the just-appended entry).")

    def test_unsubscribe_in_finally(self):
        """SSE handler must unsubscribe in a finally block."""
        # Find the finally block that calls _approval_sse_unsubscribe
        assert re.search(r"finally:.*\n.*_approval_sse_unsubscribe\(", ROUTES_SRC, re.DOTALL), \
            "SSE handler must call _approval_sse_unsubscribe in a finally block"

    def test_client_disconnect_handled(self):
        """SSE handler must catch client disconnect errors."""
        assert "_CLIENT_DISCONNECT_ERRORS" in ROUTES_SRC, \
            "SSE handler must catch client disconnect errors"

    def test_subscriber_queue_maxsize(self):
        """Subscriber queues must have a bounded maxsize to prevent memory leaks."""
        assert "queue.Queue(maxsize=" in ROUTES_SRC, \
            "Subscriber queues must have maxsize set to prevent unbounded memory growth"

    def test_notify_drops_on_full(self):
        """_approval_sse_notify must silently drop events when subscriber is slow."""
        # The queue.Full exception handler
        assert "queue.Full" in ROUTES_SRC_FULL, \
            "_approval_sse_notify must handle queue.Full to drop events for slow subscribers"

    def test_subscribe_uses_shared_lock(self):
        """subscribe/unsubscribe/notify must all use the same _lock."""
        # All three functions must use _lock; search the combined corpus since the
        # helpers live in api.route_approvals after the #1907 extraction.
        for func in ["_approval_sse_subscribe", "_approval_sse_unsubscribe", "_approval_sse_notify"]:
            # Find the function and verify it uses "with _lock"
            func_start = ROUTES_SRC_FULL.find(f"def {func}(")
            assert func_start != -1, f"{func} must exist"
            # Find the next function definition after this one
            next_func = ROUTES_SRC_FULL.find("\ndef ", func_start + 1)
            func_body = ROUTES_SRC_FULL[func_start:next_func] if next_func != -1 else ROUTES_SRC_FULL[func_start:]
            assert "with _lock:" in func_body, \
                f"{func} must use 'with _lock:' for thread safety"

    def test_unsubscribe_cleans_empty_session(self):
        """Unsubscribe must remove empty session keys from the dict."""
        assert "_approval_sse_subscribers.pop(session_id, None)" in ROUTES_SRC_FULL, \
            "_approval_sse_unsubscribe must pop session_id when subscriber list is empty"


class TestFrontendSSEImplementation:
    """Verify the frontend approval prompt transport.

    As of #3913 the frontend no longer opens an approval-stream EventSource:
    six persistent SSE connections exhausted the browser's 6-per-origin
    HTTP/1.1 pool, hanging the approval POST itself ("Request timed out").
    ``startApprovalPolling`` now routes straight to the HTTP fallback poller.
    The backend SSE route remains for compatibility (its tests are above);
    these assertions pin the poll-only frontend so the regression can't return.
    """

    def _approval_polling_body(self):
        start = MESSAGES_JS.index("function startApprovalPolling(")
        end = MESSAGES_JS.index("\nfunction ", start + 1)
        return MESSAGES_JS[start:end]

    def test_frontend_does_not_open_approval_stream(self):
        """startApprovalPolling must NOT create an approval-stream EventSource (#3913)."""
        body = self._approval_polling_body()
        assert "api/approval/stream" not in body, \
            "Frontend must not open the approval-stream EventSource (browser conn-pool exhaustion, #3913)"
        assert "new EventSource(" not in body, \
            "startApprovalPolling must not construct an EventSource — it polls over HTTP now"

    def test_routes_directly_to_fallback_poll(self):
        """startApprovalPolling must call _startApprovalFallbackPoll directly."""
        body = self._approval_polling_body()
        assert "_startApprovalFallbackPoll(sid)" in body, \
            "startApprovalPolling must route to the HTTP fallback poller"

    def test_fallback_poll_hits_pending_endpoint(self):
        """The fallback poller must GET the approval/pending endpoint relative to the mount."""
        assert 'api("/api/approval/pending?session_id="' in MESSAGES_JS, \
            "Fallback poll must query /api/approval/pending"
        assert "EventSource('/api/approval/stream" not in MESSAGES_JS, \
            "No root-absolute approval EventSource may remain (subpath-mount safety)"

    def test_fallback_poll_interval(self):
        """Approval fallback polling interval must keep the 1500ms cadence."""
        assert "1500" in MESSAGES_JS, \
            "Approval fallback polling interval must be 1500ms (degraded-mode parity with v0.50.247)"

    def test_stop_defensively_closes_any_eventsource(self):
        """stopApprovalPolling must still defensively close a lingering EventSource handle."""
        # The _approvalEventSource var stays declared (always null now) and the
        # null-guarded close() remains so any future re-introduction stays safe.
        assert "_approvalEventSource" in MESSAGES_JS, \
            "stopApprovalPolling must keep the defensive _approvalEventSource cleanup"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Unit tests (in-process, no HTTP server)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSSESubscribeUnsubscribe:
    """Test the subscribe/unsubscribe lifecycle."""

    def setup_method(self):
        """Clean SSE subscriber state before each test."""
        from api import routes as r
        with r._lock:
            r._approval_sse_subscribers.clear()

    def teardown_method(self):
        """Clean up after each test."""
        from api import routes as r
        with r._lock:
            r._approval_sse_subscribers.clear()

    def test_subscribe_returns_queue(self):
        """_approval_sse_subscribe must return a Queue."""
        from api import routes as r
        sid = f"sse-test-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        assert isinstance(q, queue.Queue), "subscribe must return a queue.Queue"
        # Cleanup
        r._approval_sse_unsubscribe(sid, q)

    def test_subscribe_registers_subscriber(self):
        """After subscribe, the queue must appear in _approval_sse_subscribers."""
        from api import routes as r
        sid = f"sse-reg-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        try:
            with r._lock:
                subs = r._approval_sse_subscribers.get(sid, [])
            assert q in subs, "Subscribed queue must be in the subscribers list"
        finally:
            r._approval_sse_unsubscribe(sid, q)

    def test_unsubscribe_removes_queue(self):
        """After unsubscribe, the queue must not be in the subscribers list."""
        from api import routes as r
        sid = f"sse-unsub-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        r._approval_sse_unsubscribe(sid, q)
        with r._lock:
            subs = r._approval_sse_subscribers.get(sid, [])
        assert q not in subs, "Unsubscribed queue must not be in the list"

    def test_unsubscribe_removes_empty_session_key(self):
        """When the last subscriber is removed, the session key must be cleaned up."""
        from api import routes as r
        sid = f"sse-empty-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        r._approval_sse_unsubscribe(sid, q)
        with r._lock:
            assert sid not in r._approval_sse_subscribers, \
                "Session key must be removed when subscriber list is empty"

    def test_unsubscribe_idempotent(self):
        """Unsubscribing twice must not raise."""
        from api import routes as r
        sid = f"sse-idem-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        r._approval_sse_unsubscribe(sid, q)
        r._approval_sse_unsubscribe(sid, q)  # should not raise

    def test_unsubscribe_unknown_queue_noop(self):
        """Unsubscribing a queue that was never subscribed must not crash."""
        from api import routes as r
        sid = f"sse-noop-{uuid.uuid4().hex[:8]}"
        q = queue.Queue()
        r._approval_sse_unsubscribe(sid, q)  # should not raise


class TestSSENotify:
    """Test the notification mechanism."""

    def setup_method(self):
        from api import routes as r
        with r._lock:
            r._approval_sse_subscribers.clear()

    def teardown_method(self):
        from api import routes as r
        with r._lock:
            r._approval_sse_subscribers.clear()

    def test_notify_delivers_payload(self):
        """_approval_sse_notify must put the payload on subscriber queues."""
        from api import routes as r
        sid = f"sse-notify-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        try:
            entry = {"command": "rm -rf /tmp/test", "pattern_key": "delete"}
            r._approval_sse_notify(sid, entry, 1)
            payload = q.get(timeout=1)
            assert payload["pending"]["command"] == "rm -rf /tmp/test"
            assert payload["pending_count"] == 1
        finally:
            r._approval_sse_unsubscribe(sid, q)

    def test_notify_multiple_subscribers(self):
        """All subscribers for a session must receive the notification."""
        from api import routes as r
        sid = f"sse-multi-{uuid.uuid4().hex[:8]}"
        q1 = r._approval_sse_subscribe(sid)
        q2 = r._approval_sse_subscribe(sid)
        q3 = r._approval_sse_subscribe(sid)
        try:
            entry = {"command": "test-cmd"}
            r._approval_sse_notify(sid, entry, 2)
            for q in [q1, q2, q3]:
                payload = q.get(timeout=1)
                assert payload["pending"]["command"] == "test-cmd"
                assert payload["pending_count"] == 2
        finally:
            for q in [q1, q2, q3]:
                r._approval_sse_unsubscribe(sid, q)

    def test_notify_cross_session_isolation(self):
        """Notify for session A must NOT deliver to session B subscribers."""
        from api import routes as r
        sid_a = f"sse-iso-a-{uuid.uuid4().hex[:8]}"
        sid_b = f"sse-iso-b-{uuid.uuid4().hex[:8]}"
        qa = r._approval_sse_subscribe(sid_a)
        qb = r._approval_sse_subscribe(sid_b)
        try:
            entry = {"command": "only-for-a"}
            r._approval_sse_notify(sid_a, entry, 1)
            # qa should have the event
            payload = qa.get(timeout=1)
            assert payload["pending"]["command"] == "only-for-a"
            # qb should be empty
            assert qb.empty(), "Session B subscriber must not receive session A events"
        finally:
            r._approval_sse_unsubscribe(sid_a, qa)
            r._approval_sse_unsubscribe(sid_b, qb)

    def test_notify_no_subscribers_is_noop(self):
        """Notifying a session with no subscribers must not raise."""
        from api import routes as r
        sid = f"sse-nosub-{uuid.uuid4().hex[:8]}"
        r._approval_sse_notify(sid, {"command": "test"}, 1)  # should not raise

    def test_notify_drops_on_full_queue(self):
        """When subscriber queue is full, events must be silently dropped."""
        from api import routes as r
        sid = f"sse-full-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        try:
            # Fill the queue (maxsize=16)
            for i in range(20):
                r._approval_sse_notify(sid, {"command": f"cmd-{i}"}, i + 1)
            # Queue should have at most 16 items
            assert q.qsize() <= 16, "Queue must not exceed maxsize"
            assert q.qsize() > 0, "Queue should have some items"
        finally:
            r._approval_sse_unsubscribe(sid, q)


class TestSSENotifyFromSubmitPending:
    """Test that submit_pending triggers SSE notifications."""

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

    def test_submit_pending_notifies_sse_subscriber(self):
        """submit_pending must push an SSE event to subscribers."""
        from api import routes as r
        sid = f"sse-submit-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        try:
            r.submit_pending(sid, {
                "command": "rm -rf /tmp/test",
                "pattern_key": "recursive delete",
                "pattern_keys": ["recursive delete"],
                "description": "recursive delete",
            })
            payload = q.get(timeout=1)
            assert payload["pending"]["command"] == "rm -rf /tmp/test"
            assert payload["pending_count"] == 1
        finally:
            r._approval_sse_unsubscribe(sid, q)

    def test_submit_pending_delivers_count(self):
        """Multiple submit_pending calls must report correct pending_count."""
        from api import routes as r
        sid = f"sse-count-{uuid.uuid4().hex[:8]}"
        q = r._approval_sse_subscribe(sid)
        try:
            for i in range(3):
                r.submit_pending(sid, {
                    "command": f"cmd-{i}",
                    "pattern_key": f"p{i}",
                    "pattern_keys": [f"p{i}"],
                    "description": f"d{i}",
                })
            for expected_count in [1, 2, 3]:
                payload = q.get(timeout=1)
                assert payload["pending_count"] == expected_count, \
                    f"Expected pending_count={expected_count}, got {payload['pending_count']}"
        finally:
            r._approval_sse_unsubscribe(sid, q)


class TestSSEConcurrency:
    """Test thread safety of SSE subscribe/unsubscribe/notify."""

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

    def test_concurrent_subscribe_unsubscribe(self):
        """Concurrent subscribe/unsubscribe must not corrupt state."""
        from api import routes as r
        sid = f"sse-conc-{uuid.uuid4().hex[:8]}"
        errors = []
        queues = []

        def worker():
            try:
                for _ in range(50):
                    q = r._approval_sse_subscribe(sid)
                    queues.append(q)
                    r._approval_sse_unsubscribe(sid, q)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent subscribe/unsubscribe errors: {errors}"
        # After all threads finish, no queues should remain
        with r._lock:
            subs = r._approval_sse_subscribers.get(sid, [])
        assert len(subs) == 0, "All subscribers should be cleaned up"

    def test_concurrent_notify_while_subscribing(self):
        """Notify while new subscribers are joining must not deadlock or crash."""
        from api import routes as r
        sid = f"sse-notsub-{uuid.uuid4().hex[:8]}"
        errors = []

        def notifier():
            try:
                for i in range(100):
                    r._approval_sse_notify(sid, {"command": f"cmd-{i}"}, 1)
            except Exception as e:
                errors.append(e)

        def subscriber():
            try:
                for _ in range(50):
                    q = r._approval_sse_subscribe(sid)
                    time.sleep(0.001)
                    r._approval_sse_unsubscribe(sid, q)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=notifier),
            threading.Thread(target=subscriber),
            threading.Thread(target=subscriber),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Concurrent notify/subscribe errors: {errors}"
        with r._lock:
            r._approval_sse_subscribers.clear()
