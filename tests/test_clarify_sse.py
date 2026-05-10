"""Tests for clarify SSE long-connection (mirrors test_approval_sse.py structure).

Covers:
  - Static analysis of backend and frontend code
  - Unit tests for subscribe/unsubscribe/notify lifecycle
  - Concurrency safety of subscriber registry
"""

import os
import queue
import threading
import textwrap

import pytest

# ── Paths ────────────────────────────────────────────────────────────────────
_ROUTES = os.path.join(os.path.dirname(__file__), "..", "api", "routes.py")
_CLARIFY = os.path.join(os.path.dirname(__file__), "..", "api", "clarify.py")
_MESSAGES = os.path.join(os.path.dirname(__file__), "..", "static", "messages.js")


def _read(path):
    with open(path) as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Static analysis — verify code structure without importing the server
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("marker", [
    "_clarify_sse_subscribers",
    "def sse_subscribe",
    "def sse_unsubscribe",
    "_clarify_sse_notify",
])
class TestClarifySSEBackendMarkers:
    def test_clarify_module_has_marker(self, marker):
        src = _read(_CLARIFY)
        assert marker in src, f"api/clarify.py missing: {marker}"


class TestClarifySSEBackendCode:
    def test_submit_pending_calls_notify(self):
        src = _read(_CLARIFY)
        assert "_clarify_sse_notify(" in src, (
            "submit_pending should call _clarify_sse_notify inside _lock"
        )

    def test_resolve_clarify_calls_notify(self):
        src = _read(_CLARIFY)
        # Count occurrences — resolve should notify on both branches
        assert src.count("_clarify_sse_notify(") >= 2, (
            "resolve_clarify should call _clarify_sse_notify for both queue-has-more and empty cases"
        )


class TestClarifySSERoutesCode:
    def test_route_registered(self):
        src = _read(_ROUTES)
        assert '"/api/clarify/stream"' in src, "Missing /api/clarify/stream route"

    def test_handler_function_exists(self):
        src = _read(_ROUTES)
        assert "def _handle_clarify_sse_stream(" in src

    def test_imports_sse_subscribe(self):
        src = _read(_ROUTES)
        assert "clarify_sse_subscribe" in src

    def test_imports_sse_unsubscribe(self):
        src = _read(_ROUTES)
        assert "clarify_sse_unsubscribe" in src


class TestClarifySSEFrontendCode:
    @pytest.fixture(autouse=True)
    def _load_js(self):
        self.js = _read(_MESSAGES)

    def test_uses_event_source(self):
        assert "new EventSource" in self.js
        assert "api/clarify/stream" in self.js
        assert "EventSource('/api/clarify/stream" not in self.js

    def test_frontend_listens_initial_event(self):
        assert "'initial'" in self.js or '"initial"' in self.js

    def test_frontend_listens_clarify_event(self):
        assert "'clarify'" in self.js or '"clarify"' in self.js

    def test_frontend_has_fallback_poll(self):
        assert "_startClarifyFallbackPoll" in self.js or "clarifyFallbackTimer" in self.js

    def test_frontend_fallback_interval_3s(self):
        # Fallback poll interval should be 3000ms
        assert "3000" in self.js

    def test_frontend_stop_closes_event_source(self):
        assert "_clarifyEventSource" in self.js
        assert ".close()" in self.js

    def test_frontend_has_health_timer(self):
        assert "_clarifyHealthTimer" in self.js


# ══════════════════════════════════════════════════════════════════════════════
# 2. Unit tests — import clarify module directly
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture()
def clarify_mod():
    """Import api.clarify fresh (module-level state is shared)."""
    from api import clarify
    return clarify


@pytest.fixture(autouse=True)
def _cleanup_subscribers(clarify_mod):
    """Clear SSE subscribers between tests to avoid leakage."""
    yield
    clarify_mod._clarify_sse_subscribers.clear()


class TestClarifySSEUnit:
    def test_subscribe_returns_queue(self, clarify_mod):
        q = clarify_mod.sse_subscribe("s1")
        assert isinstance(q, queue.Queue)
        assert q.maxsize == 16

    def test_unsubscribe_removes_queue(self, clarify_mod):
        q = clarify_mod.sse_subscribe("s1")
        clarify_mod.sse_unsubscribe("s1", q)
        assert "s1" not in clarify_mod._clarify_sse_subscribers

    def test_unsubscribe_cleans_empty_session(self, clarify_mod):
        q = clarify_mod.sse_subscribe("s1")
        clarify_mod.sse_unsubscribe("s1", q)
        assert "s1" not in clarify_mod._clarify_sse_subscribers

    def test_unsubscribe_unknown_queue_no_error(self, clarify_mod):
        q = queue.Queue()
        clarify_mod.sse_unsubscribe("s1", q)  # should not raise

    def test_multiple_subscribers_same_session(self, clarify_mod):
        q1 = clarify_mod.sse_subscribe("s1")
        q2 = clarify_mod.sse_subscribe("s1")
        assert len(clarify_mod._clarify_sse_subscribers["s1"]) == 2
        clarify_mod.sse_unsubscribe("s1", q1)
        assert len(clarify_mod._clarify_sse_subscribers["s1"]) == 1

    def test_notify_delivers_to_all_subscribers(self, clarify_mod):
        q1 = clarify_mod.sse_subscribe("s1")
        q2 = clarify_mod.sse_subscribe("s1")
        clarify_mod._clarify_sse_notify("s1", {"question": "test?"}, 1)
        assert q1.get(timeout=1)["pending"]["question"] == "test?"
        assert q2.get(timeout=1)["pending"]["question"] == "test?"

    def test_cross_session_isolation(self, clarify_mod):
        q1 = clarify_mod.sse_subscribe("s1")
        q2 = clarify_mod.sse_subscribe("s2")
        clarify_mod._clarify_sse_notify("s1", {"question": "q1"}, 1)
        assert q1.get(timeout=1)["pending"]["question"] == "q1"
        assert q2.empty()

    def test_queue_overflow_drops_silently(self, clarify_mod):
        q = clarify_mod.sse_subscribe("s1")
        for i in range(20):  # maxsize=16
            clarify_mod._clarify_sse_notify("s1", {"q": i}, i + 1)
        # Should not raise; some messages dropped
        count = 0
        while not q.empty():
            q.get_nowait()
            count += 1
        assert count <= 16

    def test_submit_pending_triggers_notify(self, clarify_mod):
        q = clarify_mod.sse_subscribe("s1")
        clarify_mod.submit_pending("s1", {"question": "hello?", "choices_offered": []})
        payload = q.get(timeout=1)
        assert payload["pending"] is not None
        assert payload["pending"]["question"] == "hello?"
        assert payload["pending_count"] == 1

    def test_unsubscribe_mid_notify_safe(self, clarify_mod):
        q1 = clarify_mod.sse_subscribe("s1")
        q2 = clarify_mod.sse_subscribe("s1")
        clarify_mod.sse_unsubscribe("s1", q2)
        clarify_mod._clarify_sse_notify("s1", {"question": "safe?"}, 1)
        assert q1.get(timeout=1)["pending"]["question"] == "safe?"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Concurrency tests
# ══════════════════════════════════════════════════════════════════════════════
class TestClarifySSEConcurrency:
    def test_concurrent_subscribe_unsubscribe(self, clarify_mod):
        errors = []

        def worker():
            try:
                for _ in range(50):
                    q = clarify_mod.sse_subscribe("s1")
                    clarify_mod.sse_unsubscribe("s1", q)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors

    def test_concurrent_notify_and_subscribe(self, clarify_mod):
        received = []
        lock = threading.Lock()
        q = clarify_mod.sse_subscribe("s1")

        def notifier():
            for i in range(20):
                clarify_mod._clarify_sse_notify("s1", {"q": i}, i + 1)

        def subscriber():
            for _ in range(20):
                q2 = clarify_mod.sse_subscribe("s1")
                clarify_mod.sse_unsubscribe("s1", q2)

        t1 = threading.Thread(target=notifier)
        t2 = threading.Thread(target=subscriber)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        # Just verify no crash — some events may have been dropped
        while not q.empty():
            payload = q.get_nowait()
            with lock:
                received.append(payload)
        # Should have received at least some events
        assert len(received) > 0
