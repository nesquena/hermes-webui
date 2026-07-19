"""Regression tests for the #4771 follow-up: a LOCAL (in-process agent)
approval must NOT be refused with the gateway "active run is unavailable" 409.

Background
----------
#4771 ("keep gateway approval failures actionable") added an explicit 409
relay-failure response in ``_handle_approval_respond`` for the case where a
*gateway* approval is mirrored into WebUI polling state but its gateway run is
gone (no ``_STREAM_RUN_IDS`` entry). That keeps the approval card actionable on
a gateway-backed deployment instead of silently failing.

The regression: on the DEFAULT local in-process backend, every guarded command
parks an ``_ApprovalEntry`` in ``tools.approval._gateway_queues`` (via
``_await_gateway_decision``), and the local streaming path mirrors it into
``_pending`` with ``_GATEWAY_MIRROR_FLAG`` set — but there is no gateway run and
no ``_STREAM_RUN_IDS`` entry, by design. ``_gateway_pending_approval_without_run_id``
therefore returned True for that purely local approval, and the handler 409'd
("Gateway approval could not be relayed because the active run is unavailable"),
refusing to resolve an approval that resolves perfectly well locally. Users on
the local backend hit this on *every* approval click.

The fix gates the 409 on the WebUI actually running the gateway chat backend
(``webui_gateway_chat_enabled``), so local approvals fall through to the local
resolution path while the legitimate #4771 gateway behaviour is preserved.

These tests drive the real ``_handle_approval_respond`` handler and assert:
  - LOCAL backend  -> 200, approval resolved, agent thread unblocked (the fix)
  - GATEWAY backend -> 409 preserved when a mirrored approval has no run (#4771)
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest

# Importing an api module first injects the hermes-agent dir onto sys.path
# (api.config._AGENT_DIR), which is what makes `tools.approval` importable.
# Import order matters: tools.* will not resolve until api.config has run.
from api import routes
from api import models
from api.gateway_chat import _STREAM_RUN_IDS

try:
    import tools.approval as ta
    from api import route_approvals as ra
    APPROVAL_AVAILABLE = True
except ImportError:
    ta = None
    ra = None
    APPROVAL_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not APPROVAL_AVAILABLE,
    reason="tools.approval not available in this environment",
)


class _FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler that captures the JSON body."""

    def __init__(self):
        self.status = None
        self.headers_sent = {}
        self._body = b""
        self.client_address = ("127.0.0.1", 0)
        self.headers = {}

        class _W:
            def __init__(self, outer):
                self.outer = outer

            def write(self, b):
                self.outer._body += b

        self.wfile = _W(self)

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers_sent[k] = v

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self._body.decode("utf-8"))


def _register_session(sid: str):
    """Put a real Session (no active stream / no gateway run) into the cache."""
    s = models.Session(session_id=sid, title="approval-regression")
    s.active_stream_id = None
    with models.LOCK:
        models.SESSIONS[sid] = s
    return s


def _seed_local_pending_approval(sid: str):
    """Reproduce what the local in-process agent path does on a guarded command.

    The agent parks an ``_ApprovalEntry`` in ``_gateway_queues`` and the local
    streaming notify callback mirrors it into ``_pending`` via
    ``submit_gateway_pending_mirror`` (tagging it with ``_GATEWAY_MIRROR_FLAG``).
    Returns (entry, approval_id).
    """
    with ta._lock:
        ta._gateway_queues.pop(sid, None)
        ta._pending.pop(sid, None)
    entry = ta._ApprovalEntry({
        "command": "rm -rf /tmp/x",
        "description": "Dangerous command",
        "pattern_key": "dangerous_command",
        "pattern_keys": ["dangerous_command"],
    })
    with ta._lock:
        ta._gateway_queues.setdefault(sid, []).append(entry)
    ra.submit_gateway_pending_mirror(sid, entry.data)
    with ta._lock:
        q = ta._pending.get(sid)
    head = q[0] if isinstance(q, list) and q else q
    assert isinstance(head, dict)
    # Sanity: the mirror is tagged exactly as the regression requires.
    assert head.get(ra._GATEWAY_MIRROR_FLAG) is True
    return entry, head.get("approval_id")


def _cleanup(sid: str):
    with ta._lock:
        ta._gateway_queues.pop(sid, None)
        ta._pending.pop(sid, None)
    with models.LOCK:
        models.SESSIONS.pop(sid, None)


def test_local_mirrored_approval_resolves_not_409():
    """The b3nw bug: a local approval must resolve (200), not 409.

    Drives the real handler with the local backend (gateway chat disabled) and
    asserts the agent thread is unblocked with the chosen result.
    """
    sid = f"local-approval-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        entry, approval_id = _seed_local_pending_approval(sid)
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False):
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once", "approval_id": approval_id},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert resp.get("code") != "gateway_run_unavailable"
        # The parked agent thread must be released with the user's choice.
        assert entry.event.is_set(), "agent approval entry was not unblocked"
        assert entry.result == "once"
    finally:
        _cleanup(sid)


def test_local_mirrored_approval_deny_resolves():
    """Deny on a local approval also resolves locally (no 409)."""
    sid = f"local-approval-deny-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        entry, approval_id = _seed_local_pending_approval(sid)
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False):
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "deny", "approval_id": approval_id},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert entry.event.is_set()
        assert entry.result == "deny"
    finally:
        _cleanup(sid)


def test_local_mirrored_approval_with_gwrun_prefix_still_resolves():
    """A local approval id that happens to start with gwrun: stays local."""
    sid = f"local-approval-prefixed-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        entry, _approval_id = _seed_local_pending_approval(sid)
        with ta._lock:
            head = ta._pending[sid][0]
            head["approval_id"] = "gwrun:local-test"
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False):
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once", "approval_id": "gwrun:local-test"},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert resp.get("code") != "gateway_run_unavailable"
        assert entry.event.is_set()
        assert entry.result == "once"
    finally:
        _cleanup(sid)


def test_local_live_head_does_not_inherit_stale_remote_run():
    """A stale remote mirror cannot seize a live local approval with the same id."""
    sid = f"local-approval-collision-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        entry, approval_id = _seed_local_pending_approval(sid)
        with ta._lock:
            local_queue = ta._pending.get(sid)
            local_entries = list(local_queue) if isinstance(local_queue, list) else [local_queue]
            ta._pending[sid] = [{
                ra._GATEWAY_MIRROR_FLAG: True,
                "run_id": "remote-run",
                "approval_id": approval_id,
                "command": "stale-remote",
            }, *local_entries]
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval:
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once", "approval_id": approval_id},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert resp.get("relayed") is not True
        respond_approval.assert_not_called()
        assert entry.event.is_set()
        assert entry.result == "once"
    finally:
        _cleanup(sid)


def test_local_live_head_beats_active_remote_candidate_run():
    """A local approval still wins when an active remote run shares its id."""
    sid = f"local-approval-active-remote-{uuid.uuid4().hex[:8]}"
    session = _register_session(sid)
    stream_id = f"remote-stream-{uuid.uuid4().hex[:8]}"
    session.active_stream_id = stream_id
    _STREAM_RUN_IDS[stream_id] = "remote-run"
    try:
        entry, approval_id = _seed_local_pending_approval(sid)
        with ta._lock:
            local_queue = ta._pending.get(sid)
            local_entries = list(local_queue) if isinstance(local_queue, list) else [local_queue]
            ta._pending[sid] = [{
                ra._GATEWAY_MIRROR_FLAG: True,
                "run_id": "remote-run",
                "approval_id": approval_id,
                "command": "remote-collision",
            }, *local_entries]
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval:
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once", "approval_id": approval_id},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert resp.get("relayed") is not True
        respond_approval.assert_not_called()
        assert entry.event.is_set()
        assert entry.result == "once"
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        _cleanup(sid)


def test_local_plain_pending_exact_id_beats_remote_mirror():
    sid = f"local-plain-pending-id-{uuid.uuid4().hex[:8]}"
    session = _register_session(sid)
    stream_id = f"remote-stream-{uuid.uuid4().hex[:8]}"
    session.active_stream_id = stream_id
    _STREAM_RUN_IDS[stream_id] = "remote-run"
    try:
        with ta._lock:
            ta._pending[sid] = [
                {"approval_id": "shared-id", "command": "local"},
                {
                    ra._GATEWAY_MIRROR_FLAG: True,
                    "run_id": "remote-run",
                    "approval_id": "shared-id",
                    "command": "remote",
                },
            ]
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval, \
             patch.object(routes, "_resolve_approval_legacy", return_value=True) as resolve_legacy:
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once", "approval_id": "shared-id"},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert resp.get("relayed") is not True
        resolve_legacy.assert_called_once_with(sid, "shared-id", "once")
        respond_approval.assert_not_called()
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        _cleanup(sid)


def test_local_plain_pending_exact_id_beats_remote_first_queue_order():
    sid = f"local-plain-pending-remote-first-{uuid.uuid4().hex[:8]}"
    session = _register_session(sid)
    stream_id = f"remote-stream-{uuid.uuid4().hex[:8]}"
    session.active_stream_id = stream_id
    _STREAM_RUN_IDS[stream_id] = "remote-run"
    try:
        with ta._lock:
            ta._pending[sid] = [
                {
                    ra._GATEWAY_MIRROR_FLAG: True,
                    "run_id": "remote-run",
                    "approval_id": "shared-id",
                    "command": "remote",
                },
                {"approval_id": "shared-id", "command": "local"},
            ]
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval:
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once", "approval_id": "shared-id"},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert resp.get("relayed") is not True
        respond_approval.assert_not_called()
        with ta._lock:
            queue = ta._pending.get(sid) or []
            assert len(queue) == 1
            assert queue[0].get("command") == "remote"
            assert queue[0].get(ra._GATEWAY_MIRROR_FLAG) is True
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        _cleanup(sid)


def test_local_plain_pending_without_id_beats_active_remote_candidate_run():
    sid = f"local-plain-pending-no-id-{uuid.uuid4().hex[:8]}"
    session = _register_session(sid)
    stream_id = f"remote-stream-{uuid.uuid4().hex[:8]}"
    session.active_stream_id = stream_id
    _STREAM_RUN_IDS[stream_id] = "remote-run"
    try:
        with ta._lock:
            ta._pending[sid] = [{"command": "local"}]
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False), \
             patch("api.runner_client.HttpRunnerClient.respond_approval") as respond_approval, \
             patch.object(routes, "_resolve_approval_legacy", return_value=True) as resolve_legacy:
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once"},
            )
        resp = handler.json()
        assert handler.status == 200, f"expected 200, got {handler.status}: {resp}"
        assert resp.get("ok") is True
        assert resp.get("relayed") is not True
        resolve_legacy.assert_called_once_with(sid, "", "once")
        respond_approval.assert_not_called()
    finally:
        _STREAM_RUN_IDS.pop(stream_id, None)
        _cleanup(sid)


def test_gateway_mirrored_approval_without_run_still_409s():
    """#4771 behaviour preserved: on the gateway backend, a mirrored approval
    whose run is gone must still surface the actionable 409 so the card stays
    visible instead of silently failing."""
    sid = f"gw-approval-{uuid.uuid4().hex[:8]}"
    _register_session(sid)  # no active_stream_id -> no _STREAM_RUN_IDS mapping
    try:
        entry, approval_id = _seed_local_pending_approval(sid)
        handler = _FakeHandler()
        with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=True):
            routes._handle_approval_respond(
                handler,
                {"session_id": sid, "choice": "once", "approval_id": approval_id},
            )
        resp = handler.json()
        assert handler.status == 409, f"expected 409, got {handler.status}: {resp}"
        assert resp.get("ok") is False
        assert resp.get("relayed") is False
        assert resp.get("code") == "gateway_run_unavailable"
    finally:
        _cleanup(sid)


def test_gateway_pending_without_run_id_flag_detection():
    """Unit-level: the helper detects a mirrored (flagged) head correctly.

    This is the predicate the 409 hinges on; it must report True for a
    gateway-mirror entry regardless of backend (the backend gate lives in the
    handler, not the predicate).
    """
    sid = f"flag-detect-{uuid.uuid4().hex[:8]}"
    try:
        entry, approval_id = _seed_local_pending_approval(sid)
        assert routes._gateway_pending_approval_without_run_id(sid, approval_id) is True
        # A non-existent approval_id must not match.
        assert routes._gateway_pending_approval_without_run_id(sid, "no-such-id") is False
    finally:
        _cleanup(sid)
