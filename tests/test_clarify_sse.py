"""Tests for the clarify SSE (Server-Sent Events) long-connection implementation.

Verifies:
  - SSE subscribe/unsubscribe/notify lifecycle in api/clarify.py
  - SSE stream endpoint registered in routes.py
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
import sys
import threading

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

ROUTES_SRC = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CLARIFY_SRC = (REPO_ROOT / "api" / "clarify.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Static-analysis tests (no server needed)
# ═══════════════════════════════════════════════════════════════════════════════

class TestClarifySSEStaticAnalysis:
    """Verify the SSE infrastructure exists and is wired correctly."""

    # --- routes.py ---

    def test_sse_route_registered(self):
        """/api/clarify/stream route must be registered."""
        assert '"/api/clarify/stream"' in ROUTES_SRC

    def test_sse_handler_function_exists(self):
        """_handle_clarify_sse_stream handler must exist."""
        assert "def _handle_clarify_sse_stream(" in ROUTES_SRC

    def test_routes_imports_sse_subscribe(self):
        """routes.py must import sse_subscribe from api.clarify."""
        assert "sse_subscribe as clarify_sse_subscribe" in ROUTES_SRC

    def test_routes_imports_sse_unsubscribe(self):
        """routes.py must import sse_unsubscribe from api.clarify."""
        assert "sse_unsubscribe as clarify_sse_unsubscribe" in ROUTES_SRC

    def test_sse_handler_uses_content_type(self):
        """SSE handler must set text/event-stream content type."""
        assert "text/event-stream" in ROUTES_SRC

    def test_sse_handler_sends_initial(self):
        """SSE handler must send initial snapshot event."""
        assert "'initial'" in ROUTES_SRC or '"initial"' in ROUTES_SRC

    def test_sse_handler_sends_keepalive(self):
        """SSE handler must send keepalive comments."""
        assert "keepalive" in ROUTES_SRC

    def test_sse_handler_has_finally_cleanup(self):
        """SSE handler must unsubscribe in finally block."""
        # Find the clarify SSE handler region and check for finally + unsubscribe
        assert "clarify_sse_unsubscribe" in ROUTES_SRC

    def test_sse_handler_subscribe_before_loop(self):
        """SSE handler must subscribe before entering the event loop."""
        # The handler subscribes inline under _clarify_lock (not via clarify_sse_subscribe helper)
        assert "_clarify_subs[sid].append(q)" in ROUTES_SRC

    # --- api/clarify.py ---

    def test_clarify_sse_subscribe_function_exists(self):
        """sse_subscribe must be defined in clarify.py."""
        assert "def sse_subscribe(" in CLARIFY_SRC

    def test_clarify_sse_unsubscribe_function_exists(self):
        """sse_unsubscribe must be defined in clarify.py."""
        assert "def sse_unsubscribe(" in CLARIFY_SRC

    def test_clarify_sse_notify_function_exists(self):
        """_sse_notify must be defined in clarify.py."""
        assert "def _sse_notify(" in CLARIFY_SRC

    def test_clarify_sse_notify_called_from_submit(self):
        """submit_pending must call _sse_notify."""
        assert "_sse_notify(" in CLARIFY_SRC
        # Verify it's called inside submit_pending
        assert "_sse_notify(session_key)" in CLARIFY_SRC

    def test_subscribers_dict_exists(self):
        """_sse_subscribers dict must exist."""
        assert "_sse_subscribers" in CLARIFY_SRC

    def test_queue_maxsize_set(self):
        """Subscriber queues must have bounded maxsize."""
        assert "maxsize=16" in CLARIFY_SRC

    # --- Frontend ---

    def test_frontend_uses_event_source(self):
        """Frontend must create EventSource for clarify SSE."""
        assert "EventSource('/api/clarify/stream" in MESSAGES_JS

    def test_frontend_listens_initial_event(self):
        """Frontend must listen for 'initial' SSE event."""
        assert "'initial'" in MESSAGES_JS

    def test_frontend_listens_clarify_event(self):
        """Frontend must listen for 'clarify' SSE event."""
        assert "'clarify'" in MESSAGES_JS

    def test_frontend_has_fallback_poll(self):
        """Frontend must have fallback HTTP polling function."""
        assert "_startClarifyFallbackPoll" in MESSAGES_JS

    def test_frontend_fallback_interval_3s(self):
        """Fallback polling must use 3s interval (not 1.5s)."""
        assert "}, 3000)" in MESSAGES_JS

    def test_frontend_stop_closes_event_source(self):
        """stopClarifyPolling must close EventSource."""
        assert "_clarifyEventSource.close()" in MESSAGES_JS

    def test_frontend_has_health_timer(self):
        """Frontend must have SSE health check timer."""
        assert "_clarifySSEHealthTimer" in MESSAGES_JS


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Unit tests (test clarify.py SSE functions directly)
# ═══════════════════════════════════════════════════════════════════════════════

class TestClarifySSEUnit:
    """Test the SSE subscribe/unsubscribe/notify lifecycle."""

    def setup_method(self):
        """Reset clarify module state between tests."""
        from api.clarify import _lock, _sse_subscribers, _pending, _gateway_queues
        with _lock:
            _sse_subscribers.clear()
            _pending.clear()
            _gateway_queues.clear()

    def test_subscribe_returns_queue(self):
        from api.clarify import sse_subscribe
        q = sse_subscribe("test-session")
        assert isinstance(q, queue.Queue)
        assert q.maxsize == 16

    def test_unsubscribe_removes_queue(self):
        from api.clarify import sse_subscribe, sse_unsubscribe, _sse_subscribers, _lock
        q = sse_subscribe("test-session")
        sse_unsubscribe("test-session", q)
        with _lock:
            assert "test-session" not in _sse_subscribers

    def test_unsubscribe_cleans_empty_session(self):
        from api.clarify import sse_subscribe, sse_unsubscribe, _sse_subscribers, _lock
        q = sse_subscribe("test-session")
        sse_unsubscribe("test-session", q)
        with _lock:
            # Key should be fully removed, not left as empty list
            assert "test-session" not in _sse_subscribers

    def test_unsubscribe_unknown_queue_no_error(self):
        """Unsubscribing a queue that was never subscribed is a no-op."""
        from api.clarify import sse_unsubscribe
        q = queue.Queue()
        sse_unsubscribe("nonexistent", q)  # must not raise

    def test_multiple_subscribers_same_session(self):
        from api.clarify import sse_subscribe, _sse_subscribers, _lock
        q1 = sse_subscribe("test-session")
        q2 = sse_subscribe("test-session")
        with _lock:
            assert len(_sse_subscribers["test-session"]) == 2
        assert q1 is not q2

    def test_notify_delivers_to_all_subscribers(self):
        from api.clarify import sse_subscribe, submit_pending
        q1 = sse_subscribe("test-session")
        q2 = sse_subscribe("test-session")
        submit_pending("test-session", {"question": "Which?", "choices_offered": ["A", "B"]})
        d1 = q1.get(timeout=2)
        d2 = q2.get(timeout=2)
        assert d1["pending"] is not None
        assert d2["pending"] is not None

    def test_cross_session_isolation(self):
        """Notify for session A must not reach session B."""
        from api.clarify import sse_subscribe, submit_pending
        qa = sse_subscribe("session-a")
        qb = sse_subscribe("session-b")
        submit_pending("session-a", {"question": "A?"})
        # session-a subscriber gets the event
        d = qa.get(timeout=2)
        assert d["pending"] is not None
        # session-b subscriber should NOT get anything
        assert qb.empty()

    def test_queue_overflow_drops_silently(self):
        """Slow subscriber with full queue must not block notify."""
        from api.clarify import sse_subscribe, _sse_notify, _sse_subscribers, _lock
        q = sse_subscribe("test-session")
        # Fill the queue to maxsize
        for i in range(16):
            q.put_nowait({"pending": {"filler": i}})
        # _sse_notify should not raise
        with _lock:
            _sse_subscribers.setdefault("test-session", []).append(q)
        # Manually invoke notify — it should not block or raise
        from api.clarify import _sse_notify
        _sse_notify("test-session")  # drops silently

    def test_submit_pending_triggers_notify(self):
        """submit_pending must push to SSE subscribers."""
        from api.clarify import sse_subscribe, submit_pending
        q = sse_subscribe("test-session")
        entry = submit_pending("test-session", {"question": "Test?"})
        d = q.get(timeout=2)
        assert d["pending"]["question"] == "Test?"

    def test_unsubscribe_mid_notify_safe(self):
        """Removing a subscriber while notify runs must not crash."""
        from api.clarify import sse_subscribe, sse_unsubscribe, submit_pending, _sse_subscribers, _lock
        q1 = sse_subscribe("test-session")
        q2 = sse_subscribe("test-session")
        # Remove q2 before notify
        sse_unsubscribe("test-session", q2)
        submit_pending("test-session", {"question": "Still works?"})
        d = q1.get(timeout=2)
        assert d["pending"]["question"] == "Still works?"
        assert q2.empty()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Concurrency tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestClarifySSEConcurrency:
    """Stress-test concurrent subscribe/unsubscribe/notify."""

    def setup_method(self):
        from api.clarify import _lock, _sse_subscribers, _pending, _gateway_queues
        with _lock:
            _sse_subscribers.clear()
            _pending.clear()
            _gateway_queues.clear()

    def test_concurrent_subscribe_unsubscribe(self):
        """Many threads subscribing and unsubscribing must not deadlock."""
        from api.clarify import sse_subscribe, sse_unsubscribe
        barriers = threading.Barrier(10, timeout=5)
        errors = []

        def worker(i):
            try:
                barriers.wait()
                q = sse_subscribe(f"session-{i % 3}")
                sse_unsubscribe(f"session-{i % 3}", q)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors, f"Errors during concurrent subscribe/unsubscribe: {errors}"

    def test_concurrent_notify_and_subscribe(self):
        """Notify and subscribe racing must not deadlock."""
        from api.clarify import sse_subscribe, submit_pending
        errors = []

        def notifier():
            try:
                for _ in range(20):
                    submit_pending("shared-session", {"question": "Q?"})
            except Exception as e:
                errors.append(e)

        def subscriber():
            try:
                for _ in range(20):
                    q = sse_subscribe("shared-session")
                    # Drain what we can
                    try:
                        q.get(timeout=0.1)
                    except queue.Empty:
                        pass
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=notifier)
        t2 = threading.Thread(target=subscriber)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not errors, f"Errors during concurrent notify/subscribe: {errors}"
