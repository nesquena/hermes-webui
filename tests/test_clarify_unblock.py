"""Tests for clarify prompt unblocking and HTTP endpoints."""

import json
import uuid
import urllib.request
import urllib.error
import urllib.parse

import pytest

from tests._pytest_port import BASE

try:
    from api.clarify import (
        register_gateway_notify,
        unregister_gateway_notify,
        resolve_clarify,
        clear_pending,
        _gateway_queues,
        _gateway_notify_cbs,
        _lock,
        _ClarifyEntry,
        submit_pending,
    )
    CLARIFY_AVAILABLE = True
except ImportError:
    CLARIFY_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not CLARIFY_AVAILABLE,
    reason="api.clarify not available in this environment",
)


def get(path):
    url = BASE + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


class TestClarifyUnblocking:
    """Unit tests for clarify queue resolution."""

    def test_resolve_clarify_sets_event(self):
        sid = f"unit-clarify-{uuid.uuid4().hex[:8]}"
        entry = _ClarifyEntry({"question": "Pick one", "choices_offered": ["a", "b"]})
        with _lock:
            _gateway_queues.setdefault(sid, []).append(entry)

        resolved = resolve_clarify(sid, "a", resolve_all=False)
        assert resolved == 1
        assert entry.event.is_set()
        assert entry.result == "a"

    def test_register_and_fire_notify_cb(self):
        sid = f"unit-notify-{uuid.uuid4().hex[:8]}"
        fired = []
        register_gateway_notify(sid, lambda d: fired.append(d))

        with _lock:
            cb = _gateway_notify_cbs.get(sid)
        assert cb is not None

        data = {"question": "What now?", "choices_offered": ["x", "y"]}
        cb(data)
        assert fired == [data]

        unregister_gateway_notify(sid)

    def test_clear_pending_unblocks_waiters(self):
        sid = f"unit-clear-{uuid.uuid4().hex[:8]}"
        entry = _ClarifyEntry({"question": "Wait", "choices_offered": []})
        with _lock:
            _gateway_queues.setdefault(sid, []).append(entry)

        cleared = clear_pending(sid)
        assert cleared == 1
        assert entry.event.is_set()
        with _lock:
            assert sid not in _gateway_queues

    def test_submit_pending_registers_entry(self):
        sid = f"unit-submit-{uuid.uuid4().hex[:8]}"
        data = {"question": "Pick", "choices_offered": ["one", "two"], "session_id": sid}
        entry = submit_pending(sid, data)
        assert entry.data["question"] == data["question"]
        assert entry.data["choices_offered"] == data["choices_offered"]
        assert entry.data["session_id"] == data["session_id"]
        with _lock:
            assert sid in _gateway_queues

        clear_pending(sid)

    def test_submit_pending_adds_timeout_metadata(self):
        sid = f"unit-timeout-{uuid.uuid4().hex[:8]}"
        entry = submit_pending(sid, {"question": "Wait", "choices_offered": []})

        assert isinstance(entry.data["requested_at"], (int, float))
        assert entry.data["timeout_seconds"] == 120
        assert entry.data["expires_at"] == pytest.approx(
            entry.data["requested_at"] + 120,
            abs=0.1,
        )

        clear_pending(sid)


class TestClarifyModuleExports:
    def test_register_gateway_notify_exported(self):
        import api.clarify as ap
        assert hasattr(ap, "register_gateway_notify")

    def test_unregister_gateway_notify_exported(self):
        import api.clarify as ap
        assert hasattr(ap, "unregister_gateway_notify")

    def test_resolve_clarify_exported(self):
        import api.clarify as ap
        assert hasattr(ap, "resolve_clarify")

    def test_clarify_entry_exported(self):
        import api.clarify as ap
        assert hasattr(ap, "_ClarifyEntry")


class TestClarifyHTTPEndpoints:
    """Regression tests for /api/clarify/respond against the live test server."""

    def test_respond_returns_ok_no_pending(self):
        sid = f"http-no-pending-{uuid.uuid4().hex[:8]}"
        result, status = post("/api/clarify/respond", {
            "session_id": sid,
            "response": "Use option A",
        })
        assert status == 200
        assert result["ok"] is True

    def test_respond_requires_session_id(self):
        result, status = post("/api/clarify/respond", {"response": "Hello"})
        assert status == 400

    def test_respond_requires_response(self):
        sid = f"http-no-response-{uuid.uuid4().hex[:8]}"
        result, status = post("/api/clarify/respond", {"session_id": sid})
        assert status == 400

    def test_respond_clears_injected_pending(self):
        sid = f"http-clear-{uuid.uuid4().hex[:8]}"
        question = urllib.parse.quote("Pick the better option")
        choices = urllib.parse.quote("A")
        inject = get(
            f"/api/clarify/inject_test?session_id={urllib.parse.quote(sid)}"
            f"&question={question}&choices={choices}"
        )
        assert inject["ok"] is True

        data = get(f"/api/clarify/pending?session_id={urllib.parse.quote(sid)}")
        assert data["pending"] is not None

        result, status = post("/api/clarify/respond", {
            "session_id": sid,
            "response": "B",
        })
        assert status == 200
        assert result["ok"] is True

        data2 = get(f"/api/clarify/pending?session_id={urllib.parse.quote(sid)}")
        assert data2["pending"] is None
