"""
Tests for fix/approval-stuck-thinking:
Verify that /api/approval/respond correctly unblocks gateway approval queues
and that the approval module exports the symbols streaming.py and routes.py
need to prevent the UI getting stuck in "Thinking…" during dangerous commands.
"""

import json
import threading
import uuid
import urllib.request
import urllib.error
import urllib.parse

import pytest

# Import approval internals — shared module-level state within this process.
# The HTTP tests use the test server (port 8788, separate process).
# The unit tests operate directly on the module.
try:
    from tools.approval import (
        register_gateway_notify,
        unregister_gateway_notify,
        resolve_gateway_approval,
        _gateway_queues,
        _gateway_notify_cbs,
        _lock,
        _ApprovalEntry,
        submit_pending,
    )
    # has_pending and pop_pending were removed from tools.approval when the
    # agent renamed has_pending -> has_blocking_approval (gateway queue check)
    # and removed the polling-mode pop_pending. Routes now check _pending
    # directly. These symbols are no longer part of the public API.
    APPROVAL_AVAILABLE = True
except ImportError:
    APPROVAL_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not APPROVAL_AVAILABLE,
    reason="tools.approval not available in this environment"
)

from tests._pytest_port import BASE


def get(path):
    url = BASE + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── Unit tests (in-process, no HTTP server needed) ──────────────────────────

class TestGatewayApprovalUnblocking:
    """Unit tests for the gateway queue unblocking mechanism."""

    def test_resolve_gateway_approval_sets_event(self):
        """resolve_gateway_approval() must set the entry's event and store the result."""
        sid = f"unit-resolve-{uuid.uuid4().hex[:8]}"
        data = {"command": "rm -rf /tmp/x", "description": "recursive delete"}
        entry = _ApprovalEntry(data)
        with _lock:
            _gateway_queues.setdefault(sid, []).append(entry)

        resolved = resolve_gateway_approval(sid, "once", resolve_all=False)
        assert resolved == 1
        assert entry.event.is_set()
        assert entry.result == "once"

        # Queue should be cleaned up
        with _lock:
            assert sid not in _gateway_queues

    def test_resolve_gateway_approval_deny(self):
        """Deny choice is propagated correctly."""
        sid = f"unit-deny-{uuid.uuid4().hex[:8]}"
        entry = _ApprovalEntry({"command": "pkill -9 x", "description": "force kill"})
        with _lock:
            _gateway_queues.setdefault(sid, []).append(entry)

        resolve_gateway_approval(sid, "deny")
        assert entry.result == "deny"

    def test_resolve_gateway_approval_no_queue_is_harmless(self):
        """resolve_gateway_approval with no queue entry returns 0, no crash."""
        sid = f"unit-no-queue-{uuid.uuid4().hex[:8]}"
        result = resolve_gateway_approval(sid, "once")
        assert result == 0

    def test_resolve_all_unblocks_multiple_entries(self):
        """resolve_all=True unblocks every pending entry in the queue."""
        sid = f"unit-resolve-all-{uuid.uuid4().hex[:8]}"
        entries = [_ApprovalEntry({"command": f"cmd{i}"}) for i in range(3)]
        with _lock:
            _gateway_queues[sid] = list(entries)

        resolved = resolve_gateway_approval(sid, "session", resolve_all=True)
        assert resolved == 3
        for e in entries:
            assert e.event.is_set()
            assert e.result == "session"

    def test_register_and_fire_notify_cb(self):
        """register_gateway_notify stores the cb; calling it delivers approval data."""
        sid = f"unit-notify-{uuid.uuid4().hex[:8]}"
        fired = []
        register_gateway_notify(sid, lambda d: fired.append(d))

        with _lock:
            cb = _gateway_notify_cbs.get(sid)
        assert cb is not None

        data = {"command": "test", "description": "test"}
        cb(data)
        assert fired == [data]

        unregister_gateway_notify(sid)

    def test_unregister_clears_cb_and_signals_entries(self):
        """unregister_gateway_notify removes cb and unblocks any queued entries."""
        sid = f"unit-unreg-{uuid.uuid4().hex[:8]}"
        register_gateway_notify(sid, lambda d: None)

        entry = _ApprovalEntry({"command": "x"})
        with _lock:
            _gateway_queues.setdefault(sid, []).append(entry)

        unregister_gateway_notify(sid)

        assert entry.event.is_set(), "unregister should signal blocked entries"
        with _lock:
            assert sid not in _gateway_notify_cbs
            assert sid not in _gateway_queues

    def test_streaming_approval_integration(self):
        """
        End-to-end unit simulation of the streaming.py fix:
        1. streaming.py registers notify_cb
        2. check_all_command_guards fires notify_cb (pushing approval SSE)
        3. User responds — resolve_gateway_approval unblocks agent thread
        4. Agent thread sees choice and continues
        """
        sid = f"unit-e2e-{uuid.uuid4().hex[:8]}"
        approval_events_sent = []

        # Step 1: streaming.py registers the notify callback
        def _approval_notify_cb(approval_data):
            approval_events_sent.append(approval_data)  # would be put('approval', ...)
        register_gateway_notify(sid, _approval_notify_cb)

        # Step 2: check_all_command_guards fires the callback and queues an entry
        approval_data = {
            "command": "rm -rf /tmp/test",
            "pattern_key": "recursive delete",
            "pattern_keys": ["recursive delete"],
            "description": "recursive delete",
        }
        entry = _ApprovalEntry(approval_data)
        with _lock:
            _gateway_queues.setdefault(sid, []).append(entry)
        # notify_cb fires synchronously (gateway notifies user)
        with _lock:
            cb = _gateway_notify_cbs.get(sid)
        cb(approval_data)

        assert len(approval_events_sent) == 1, "approval SSE event should have been queued"

        # Step 3: user responds via /api/approval/respond → resolve_gateway_approval
        resolved = resolve_gateway_approval(sid, "once")
        assert resolved == 1

        # Step 4: agent thread is unblocked with the correct choice
        assert entry.event.is_set()
        assert entry.result == "once"

        # Cleanup
        unregister_gateway_notify(sid)


# ── Symbol existence tests ───────────────────────────────────────────────────

class TestApprovalModuleExports:
    """Verify the module exports all symbols that streaming.py and routes.py need."""

    def test_register_gateway_notify_exported(self):
        import tools.approval as ap
        assert hasattr(ap, "register_gateway_notify"), \
            "tools.approval must export register_gateway_notify"

    def test_unregister_gateway_notify_exported(self):
        import tools.approval as ap
        assert hasattr(ap, "unregister_gateway_notify"), \
            "tools.approval must export unregister_gateway_notify"

    def test_resolve_gateway_approval_exported(self):
        import tools.approval as ap
        assert hasattr(ap, "resolve_gateway_approval"), \
            "tools.approval must export resolve_gateway_approval"

    def test_approval_entry_exported(self):
        import tools.approval as ap
        assert hasattr(ap, "_ApprovalEntry"), \
            "tools.approval must export _ApprovalEntry"


# ── HTTP regression tests (test server, port 8788) ───────────────────────────

class TestApprovalHTTPEndpoints:
    """
    Regression tests for /api/approval/respond against the live test server.
    These verify that the HTTP layer behaves correctly — they don't rely on
    in-process module state shared with the server subprocess.
    """

    def test_respond_returns_ok_no_pending(self):
        """respond with no pending entry returns ok (no crash, no 500)."""
        sid = f"http-no-pending-{uuid.uuid4().hex[:8]}"
        result, status = post("/api/approval/respond", {
            "session_id": sid,
            "choice": "deny",
        })
        assert status == 200
        assert result["ok"] is True

    def test_respond_clears_injected_pending(self):
        """Inject a pending entry, respond, verify it's cleared."""
        sid = f"http-clear-{uuid.uuid4().hex[:8]}"
        cmd = "rm -rf /tmp/testdir"

        inject = get(f"/api/approval/inject_test?session_id={urllib.parse.quote(sid)}"
                     f"&pattern_key=recursive+delete&command={urllib.parse.quote(cmd)}")
        assert inject["ok"] is True

        data = get(f"/api/approval/pending?session_id={urllib.parse.quote(sid)}")
        assert data["pending"] is not None

        result, status = post("/api/approval/respond", {
            "session_id": sid,
            "choice": "deny",
        })
        assert status == 200
        assert result["ok"] is True

        data2 = get(f"/api/approval/pending?session_id={urllib.parse.quote(sid)}")
        assert data2["pending"] is None, "pending should be cleared after respond"

    def test_respond_rejects_invalid_choice(self):
        """respond with an unknown choice returns 400."""
        result, status = post("/api/approval/respond", {
            "session_id": "some-session",
            "choice": "INVALID",
        })
        assert status == 400

    def test_respond_requires_session_id(self):
        """respond without session_id returns 400."""
        result, status = post("/api/approval/respond", {"choice": "deny"})
        assert status == 400

    def test_respond_session_choice_clears_pending(self):
        """Inject pending, respond with 'session', verify cleared."""
        sid = f"http-session-{uuid.uuid4().hex[:8]}"
        inject = get(f"/api/approval/inject_test?session_id={urllib.parse.quote(sid)}"
                     f"&pattern_key=force+kill+processes&command=pkill+-9+something")
        assert inject["ok"] is True

        result, status = post("/api/approval/respond", {
            "session_id": sid,
            "choice": "session",
        })
        assert status == 200
        assert result["choice"] == "session"

        data = get(f"/api/approval/pending?session_id={urllib.parse.quote(sid)}")
        assert data["pending"] is None
