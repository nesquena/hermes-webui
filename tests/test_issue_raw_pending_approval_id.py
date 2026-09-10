"""Raw id-less local approval entries in ``_pending`` must be actionable.

Regression for the Sep 2026 live incident. When a guarded tool needs approval
and no gateway notifier is registered for the session (``unregister_gateway_
notify`` fires at turn end; a queued wakeup/continuation then hits a guard),
the agent-side fallback ``tools/approval.py::_pending_result`` writes a raw
dict — ``{command, description, pattern_key, pattern_keys}`` and NOTHING else,
no ``approval_id``, no ``request_id`` — directly into the shared
``tools.approval._pending[session_key]`` dict that the WebUI imports.

Before the fix:
- ``GET /api/approval/pending`` served that raw dict verbatim (reconcile passes
  non-mirror entries through untouched) with NO ``approval_id``, so the
  frontend owner-capture (``_captureApprovalResponseOwner``) bailed and every
  button on the approval card was a no-op;
- the frontend dismiss (X) only hid the card locally and the 1.5s poll
  re-rendered it forever — the card could neither be approved nor dismissed;
- there is no waiter thread behind this shape (``_pending_result`` returns the
  STOP message immediately), so draining the entry is purely UI hygiene — but
  leaving it forever means the session shows a permanent phantom card.

After the fix (mint at the reconcile chokepoint):
- id-less, run-less, non-mirror ``_pending`` entries get a stable minted
  ``approval_id`` (persisted in the stored entry dict, so it is stable across
  polls and dismissals persist);
- an exact-id response pops the entry and reports ``ok``;
- a stale/different explicit id still fails closed (#527 guard preserved);
- Skip-all (yolo release) drains the entry instead of dead-ending.

The sibling shape — an orphaned ``_gateway_queues`` producer entry — is already
covered upstream (minting ``gwlocal:<token>`` in the producer-tokenization loop,
commit 5826d76d); the tests in ``test_gateway_approval_legacy_path.py`` and this
file's history document it. This file covers the raw-``_pending`` shape, which
no existing test exercised.
"""

from __future__ import annotations

import json
import re
import uuid
from unittest.mock import patch

import pytest

from api import models
from api import route_approvals as ra
from api import routes

try:
    import tools.approval as ta
    APPROVAL_AVAILABLE = True
except ImportError:
    ta = None
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


def _register_session(sid: str, active_stream_id: str | None = None):
    s = models.Session(session_id=sid, title="approval-raw-pending-orphan")
    s.active_stream_id = active_stream_id
    with models.LOCK:
        models.SESSIONS[sid] = s
    return s


def _seed_raw_pending(sid: str, command: str = "rm -rf /tmp/qa-probe-home",
                      key: str = "recursive delete") -> dict:
    """Seed ``_pending[sid]`` with the exact ``_pending_result`` shape.

    Mirrors agent-side tools/approval.py::_pending_result when no gateway
    notifier is registered: a raw dict with no approval_id, no request_id,
    no run_id, written directly into the shared _pending dict.
    """
    pending = {
        "command": command,
        "pattern_key": key,
        "pattern_keys": [key],
        "description": "recursive delete",
    }
    with ta._lock:
        ta._pending[sid] = pending
        ta._gateway_queues.pop(sid, None)
    return pending


def _cleanup(sid: str):
    with ta._lock:
        ta._pending.pop(sid, None)
        ta._gateway_queues.pop(sid, None)
    with models.LOCK:
        models.SESSIONS.pop(sid, None)
    try:
        ta.disable_session_yolo(sid)
    except Exception:
        pass


def _poll_pending(sid: str) -> dict:
    h = _FakeHandler()
    routes._handle_approval_pending(h, type("P", (), {"query": f"session_id={sid}"})())
    assert h.status == 200, f"pending poll failed: {h.status} {h.json()!r}"
    return h.json()


def _respond(sid: str, body: dict):
    h = _FakeHandler()
    with patch("api.gateway_chat.webui_gateway_chat_enabled", return_value=True):
        routes._handle_approval_respond(h, body)
    return h


MINTED_ID_RE = re.compile(r"^gwlocal-mirrorless:[0-9a-f]{32}$")


def test_raw_pending_entry_surfaces_with_stable_actionable_id():
    """The id-less raw entry must be served with a minted id that is stable
    across polls (dismiss persistence keys on sid+approval_id)."""
    sid = f"raw-id-{uuid.uuid4().hex[:8]}"
    entry = _seed_raw_pending(sid)
    try:
        data = _poll_pending(sid)
        assert data["pending_count"] >= 1
        pending = data["pending"]
        assert pending is not None
        approval_id = pending.get("approval_id")
        assert approval_id, (
            "pending payload served a raw id-less local entry with no "
            "approval_id; the frontend cannot act on it (buttons no-op, "
            "dismiss cannot stick)"
        )
        assert MINTED_ID_RE.match(approval_id), approval_id

        # Stability: the second poll must return the SAME id — the mint must
        # persist in the stored entry, not re-mint per poll.
        data2 = _poll_pending(sid)
        assert data2["pending"]["approval_id"] == approval_id
        assert entry["approval_id"] == approval_id
    finally:
        _cleanup(sid)


def test_raw_pending_entry_respond_with_minted_id_pops_entry():
    """An exact-id response must pop the raw entry and report ok (no waiter
    exists behind this shape — draining is the correct resolution)."""
    sid = f"raw-resp-{uuid.uuid4().hex[:8]}"
    entry = _seed_raw_pending(sid)
    # The incident session had a LIVE run pointer; the stale stream-pointer
    # guard must not 409 the click before the legacy resolver pops the entry.
    _register_session(sid, active_stream_id="stream-raw-resp-live")
    from api.gateway_chat import _STREAM_RUN_IDS

    _STREAM_RUN_IDS["stream-raw-resp-live"] = "run-live-1"
    try:
        approval_id = _poll_pending(sid)["pending"]["approval_id"]
        assert approval_id

        h = _respond(sid, {"session_id": sid, "choice": "deny",
                           "approval_id": approval_id})
        body = h.json()
        assert h.status == 200, f"respond failed: {h.status} {body!r}"
        assert body.get("ok") is True, f"respond not accepted: {body!r}"

        # The entry must actually be gone — no more phantom card.
        data = _poll_pending(sid)
        assert data["pending"] is None
        assert data["pending_count"] == 0
    finally:
        _STREAM_RUN_IDS.pop("stream-raw-resp-live", None)
        _cleanup(sid)


def test_raw_pending_entry_stale_id_fails_closed():
    """#527 guard preserved: a different explicit id must not pop the live
    entry and must keep it surfaced with its stable id."""
    sid = f"raw-stale-{uuid.uuid4().hex[:8]}"
    entry = _seed_raw_pending(sid)
    _register_session(sid)
    try:
        approval_id = _poll_pending(sid)["pending"]["approval_id"]
        assert approval_id

        h = _respond(sid, {"session_id": sid, "choice": "once",
                           "approval_id": "stale-different-id"})
        body = h.json()
        assert body.get("ok") is not True, f"stale id must fail closed: {body!r}"

        # The live entry remains pending, unchanged, with its stable id.
        data = _poll_pending(sid)
        assert data["pending"] is not None
        assert data["pending"]["approval_id"] == approval_id
        assert entry["approval_id"] == approval_id
    finally:
        _cleanup(sid)


def test_raw_pending_entry_yolo_skip_all_drains():
    """Skip-all (yolo release) must drain the raw entry instead of leaving the
    phantom card, and report the yolo state truthfully."""
    sid = f"raw-yolo-{uuid.uuid4().hex[:8]}"
    entry = _seed_raw_pending(sid)
    _register_session(sid)
    try:
        approval_id = _poll_pending(sid)["pending"]["approval_id"]
        assert approval_id

        h = _respond(sid, {"session_id": sid, "choice": "once",
                           "approval_id": approval_id, "yolo": True})
        body = h.json()
        assert h.status == 200, f"yolo respond failed: {h.status} {body!r}"
        assert body.get("ok") is True, body
        assert body.get("yolo_enabled") is True
        assert _poll_pending(sid)["pending"] is None
    finally:
        # Restore: the yolo release enables session YOLO as a side effect.
        _cleanup(sid)
