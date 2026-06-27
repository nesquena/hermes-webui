"""Regression tests for the #4948 LOCAL-backend variant (Jamie/.666 report):
a STALE approval card click must clear gracefully, not dead-end on
"Approval response not accepted." with a stuck card.

Background
----------
#4771 changed the frontend `respondApproval` from fire-and-forget to checking
`result.ok` and surfacing a toast on failure. That exposed a pre-existing
local-backend edge: when a guarded command's approval card is still on screen
but its stream has ended (user cancel / fork / provider error / completion
while pending), the agent's gateway entry is dropped and reconcile purges the
mirrored `_pending` entry. A click then sends the held `approval_id`, which
`_resolve_approval_legacy` cannot match -> returns False -> the handler
returned a bare `{ok: false}` (no `error`) -> the frontend showed
"Approval response not accepted." with a STUCK card. (Reported by Jamie on
v0.51.666 and b3nw on Discord; the local-backend sibling of the gateway-side
#4948.)

The fix: when the local resolution returns False AND the session has no live
pending approval at all, treat the click as a benign stale-card clear and
return `{ok: True, stale_cleared: True}` so the UI clears the orphan card.
The #527 protective guard is preserved: a stale explicit-id click made WHILE a
DIFFERENT approval is still live must stay `ok: false` so it can never resolve
the wrong command.
"""
from __future__ import annotations

import json
import threading
import uuid
from unittest.mock import patch

import pytest

from api import routes
from api import models

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
    def __init__(self):
        self.status = None
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
        pass

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self._body.decode("utf-8"))


def _register_session(sid: str):
    s = models.Session(session_id=sid, title="approval-4948-local")
    s.active_stream_id = None
    with models.LOCK:
        models.SESSIONS[sid] = s
    return s


def _park_local_approval(sid: str, command: str = "rm -rf /tmp/x", key: str = "dangerous_command"):
    """Wire the notify callback exactly like api/streaming.py and park a
    guarded command on a background thread (it blocks awaiting approval).

    Returns the rendered approval_id (what the frontend card holds), taken from
    /api/approval/pending — the same surface the real UI reads.
    """
    with ta._lock:
        ta._gateway_queues.pop(sid, None)
        ta._pending.pop(sid, None)

    def _cb(approval_data):
        ra.submit_gateway_pending_mirror(sid, approval_data)

    ta.register_gateway_notify(sid, _cb)

    def _agent():
        ad = {
            "command": command,
            "pattern_key": key,
            "pattern_keys": [key],
            "description": "Dangerous command",
        }
        ta._await_gateway_decision(sid, ta._gateway_notify_cbs.get(sid), ad, surface="gateway")

    th = threading.Thread(target=_agent, daemon=True)
    th.start()
    th.join(timeout=1.5)  # still blocked; the queue is seeded

    h = _FakeHandler()
    routes._handle_approval_pending(h, type("P", (), {"query": f"session_id={sid}"})())
    return (h.json().get("pending") or {}).get("approval_id")


def _end_stream_drop_entry(sid: str):
    """Simulate the stream ending while the card is pending: the agent's
    gateway entry is dropped (cancel/fork/error/completion) and reconcile
    purges the orphaned _pending mirror — exactly what _drop_entry +
    _cleanup_gateway_pending_mirror do."""
    with ta._lock:
        ta._gateway_queues.pop(sid, None)
    with ra._lock:
        ra.reconcile_gateway_pending_mirror_locked(sid)


def _respond(sid: str, approval_id: str, choice: str = "once"):
    h = _FakeHandler()
    with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=False):
        routes._handle_approval_respond(
            h, {"session_id": sid, "choice": choice, "approval_id": approval_id}
        )
    return h


def _cleanup(sid: str):
    with ta._lock:
        ta._gateway_queues.pop(sid, None)
        ta._pending.pop(sid, None)
    with models.LOCK:
        models.SESSIONS.pop(sid, None)


def test_stale_card_click_clears_not_dead_ends():
    """The Jamie/.666 bug: clicking a card whose approval is gone returns a
    benign cleared result, NOT a bare ok:false (which the UI rendered as
    'Approval response not accepted.' with a stuck card)."""
    sid = f"stale-local-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        card_id = _park_local_approval(sid)
        assert card_id, "precondition: a pending approval card should be rendered"
        _end_stream_drop_entry(sid)
        # Nothing is pending now.
        assert routes._session_has_pending_approval(sid) is False
        resp = _respond(sid, card_id)
        body = resp.json()
        assert resp.status == 200, f"expected 200, got {resp.status}: {body}"
        assert body.get("ok") is True, f"stale card must clear gracefully: {body}"
        assert body.get("stale_cleared") is True
        # Crucially: no bare ok:false-without-error (the stuck-card symptom).
    finally:
        _cleanup(sid)


def test_fresh_local_approval_still_resolves():
    """A normal, live local approval still resolves (200/ok) and is NOT
    mislabeled stale_cleared."""
    sid = f"fresh-local-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        card_id = _park_local_approval(sid)
        assert card_id
        assert routes._session_has_pending_approval(sid) is True
        resp = _respond(sid, card_id, choice="once")
        body = resp.json()
        assert resp.status == 200, f"{body}"
        assert body.get("ok") is True
        assert not body.get("stale_cleared"), "a live approval must not be tagged stale_cleared"
    finally:
        _cleanup(sid)


def test_stale_id_while_different_approval_live_still_blocked():
    """#527 guard preserved: a stale explicit-id click made WHILE a different
    approval is live must NOT resolve the live one (stays ok:false) and must
    leave the live approval pending."""
    sid = f"guard-local-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        live_id = _park_local_approval(sid, command="rm -rf /tmp/B", key="dangerous_B")
        assert live_id
        stale_id = uuid.uuid4().hex  # a long-gone approval A
        resp = _respond(sid, stale_id, choice="once")
        body = resp.json()
        assert body.get("ok") is False, f"stale id must not resolve while B is live: {body}"
        assert not body.get("stale_cleared")
        # B is still pending and unchanged.
        assert routes._session_has_pending_approval(sid) is True
        h = _FakeHandler()
        routes._handle_approval_pending(h, type("P", (), {"query": f"session_id={sid}"})())
        assert (h.json().get("pending") or {}).get("approval_id") == live_id
    finally:
        _cleanup(sid)


def test_session_has_pending_approval_predicate():
    """Unit: the predicate the benign-clear hinges on reports live vs empty."""
    sid = f"pred-{uuid.uuid4().hex[:8]}"
    _register_session(sid)
    try:
        assert routes._session_has_pending_approval(sid) is False
        _park_local_approval(sid)
        assert routes._session_has_pending_approval(sid) is True
        _end_stream_drop_entry(sid)
        assert routes._session_has_pending_approval(sid) is False
    finally:
        _cleanup(sid)
