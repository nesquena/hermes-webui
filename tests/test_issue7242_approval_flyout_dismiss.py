"""Tests for #7242: approval flyout durable dismissal and lifecycle settlement.

Covers the four-point contract from the issue:

1. Every rendered approval carries a stable actionable approval_id (idless
   legacy entries are normalized server-side and the card degrades to an
   explicit unresolved state when identity is still missing).
2. X durably dismisses the card (best-effort server deny + local dismissal
   that the pending poll honors).
3. Dismissal resolves the matching server-side pending entry.
4. Terminal runs (completed / failed / cancelled / 60s BLOCKED timeout) settle
   the approval/control-boundary state so no stale pending head re-opens the
   flyout.

Backend assertions exercise the real api.route_approvals functions directly
(no server boot required); frontend assertions use the node-driver static
source extraction pattern used across the suite.
"""
import uuid

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTE_APPROVALS_SRC = (ROOT / "api" / "route_approvals.py").read_text(encoding="utf-8")

# Import api.config FIRST: its module-level code appends the agent dir to
# sys.path (api/config.py "Inject agent dir into sys.path"), which makes
# `tools.approval` importable so route_approvals binds to the REAL shared
# state. Importing api.route_approvals before that would silently bind it to
# the ImportError fallback stubs (private _pending/_gateway_queues dicts),
# breaking module-identity assumptions for every later in-process test.
import api.config  # noqa: F401  (side effect: sys.path += agent dir)

from api.route_approvals import (
    _GATEWAY_MIRROR_FLAG,
    _GATEWAY_MIRROR_RETAINED,
    _pending,
    reconcile_gateway_pending_mirror_locked,
    retire_gateway_pending_mirror,
    submit_gateway_pending_mirror,
)


def _compact(text: str) -> str:
    return "".join(text.split())


def _fn_body(compact: str, fn_name: str) -> str:
    """Extract a function body with brace matching (nested blocks safe)."""
    start = compact.find("function" + fn_name + "(")
    assert start != -1, f"function {fn_name}( not found"
    brace = compact.find("{", start)
    assert brace != -1
    depth = 0
    for i in range(brace, len(compact)):
        ch = compact[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return compact[start:i + 1]
    raise AssertionError(f"unbalanced braces in function {fn_name}")


def _cleanup_sid(sid: str) -> None:
    _pending.pop(sid, None)


# ── Backend: idless normalization (contract 1) ────────────────────────────

def test_reconcile_mints_approval_id_for_idless_entry():
    """A legacy idless entry in the shared queue must gain a stable id."""
    sid = "test-7242-idless-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {_GATEWAY_MIRROR_FLAG: True, "run_id": "", "description": "tirith mass deletion"},
        ]
        head, total, changed = reconcile_gateway_pending_mirror_locked(sid)
        assert head is not None
        assert str(head.get("approval_id") or "").strip(), "reconcile must mint approval_id"
        assert total == 1
        assert changed is True
        assert str(_pending[sid][0]["approval_id"] or "").strip()
    finally:
        _cleanup_sid(sid)


def test_reconcile_mints_id_only_once():
    """Repeated reconciles must keep the minted id stable (no churn)."""
    sid = "test-7242-stable-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {_GATEWAY_MIRROR_FLAG: True, "run_id": "", "description": "x"},
        ]
        reconcile_gateway_pending_mirror_locked(sid)
        first_id = _pending[sid][0]["approval_id"]
        head, total, changed = reconcile_gateway_pending_mirror_locked(sid)
        assert head["approval_id"] == first_id
        assert changed is False, "id must be stable across reconciles"
    finally:
        _cleanup_sid(sid)


def test_submit_gateway_pending_mirror_mints_id_for_orphan():
    """An orphan mirror (no run_id, no request_id, no live producer) must
    still reach the client with an actionable approval_id."""
    sid = "test-7242-orphan-" + uuid.uuid4().hex[:8]
    try:
        head, total = submit_gateway_pending_mirror(
            sid, {"description": "legacy no-identity approval"}
        )
        assert head is not None
        assert str(head.get("approval_id") or "").strip(), (
            "submit_gateway_pending_mirror must mint approval_id for orphan mirrors"
        )
        assert total == 1
    finally:
        _cleanup_sid(sid)


# ── Backend: terminal settlement (contract 4) ─────────────────────────────

def test_retire_with_run_id_clears_plain_and_mirror_entries():
    """A terminated run must clear BOTH gateway mirrors and plain local
    pending entries bound to it (BLOCKED timeout leaves no pending head)."""
    sid = "test-7242-run-" + uuid.uuid4().hex[:8]
    run_id = "run-7242-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {"approval_id": "plain-1", "run_id": run_id, "description": "local"},
            {
                _GATEWAY_MIRROR_FLAG: True,
                "approval_id": "mirror-1",
                "run_id": run_id,
                "_gateway_mirror_token": "tok",
            },
            {"approval_id": "other-run", "run_id": "run-other", "description": "keep"},
        ]
        retired = retire_gateway_pending_mirror(sid, run_id=run_id)
        assert retired is True
        remaining = _pending.get(sid) or []
        remaining_ids = [e.get("approval_id") for e in remaining]
        assert "plain-1" not in remaining_ids, "plain run entry must be retired"
        assert "mirror-1" not in remaining_ids, "run mirror must be retired"
        assert "other-run" in remaining_ids, "unrelated run entries must survive"
    finally:
        _cleanup_sid(sid)


def test_retire_teardown_preserves_plain_entries_retires_no_run_mirrors():
    """Session teardown (no run_id) must retire no-run gateway mirrors but
    preserve plain local entries, whose embedded-agent producer may still be
    blocked on the approval."""
    sid = "test-7242-teardown-" + uuid.uuid4().hex[:8]
    try:
        _pending[sid] = [
            {"approval_id": "plain-1", "description": "local"},
            {
                _GATEWAY_MIRROR_FLAG: True,
                "approval_id": "mirror-1",
                "run_id": "r1",
                "_gateway_mirror_token": "tok1",
            },
            {_GATEWAY_MIRROR_RETAINED: True, _GATEWAY_MIRROR_FLAG: True, "approval_id": "ret-1"},
        ]
        retire_gateway_pending_mirror(sid)
        remaining = _pending.get(sid) or []
        remaining_ids = [e.get("approval_id") for e in remaining]
        assert "plain-1" in remaining_ids, "plain local entries must survive teardown"
        assert "mirror-1" not in remaining_ids, "run mirror must be retired"
        assert "ret-1" not in remaining_ids, "no-run mirror must be retired"
    finally:
        _cleanup_sid(sid)


# ── Frontend: durable dismiss (contracts 2 + 3) ───────────────────────────

def test_dismiss_approval_card_sends_deny_to_server():
    """X must resolve the server-side pending entry, not just hide locally."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert '"/api/approval/respond"' in body, "dismiss must POST to /api/approval/respond"
    assert "choice:\"deny\"" in body, "dismiss must send choice deny"


def test_dismiss_approval_card_keeps_local_dismissal_behavior():
    """The pre-existing local dismissal behavior must be preserved."""
    body = _fn_body(_compact(MESSAGES_JS), "dismissApprovalCard")
    assert "_markApprovalDismissed(sid,_approvalCurrentId)" in body
    assert "hideApprovalCard(true)" in body
    assert "_clearApprovalPendingForSession(sid)" in body


def test_poll_skips_dismissed_pending_head():
    """The fallback poll must not re-render a durably dismissed head."""
    compact = _compact(MESSAGES_JS)
    poll_start = compact.find("function_startApprovalFallbackPoll(")
    assert poll_start != -1
    poll_body_end = compact.find("functionstopApprovalPollingForSession(", poll_start)
    poll_body = compact[poll_start:poll_body_end]
    assert "_isApprovalDismissed(sid," in poll_body, (
        "poll must check the dismissal set before rendering the pending head"
    )
    dismiss_check = poll_body.find("_isApprovalDismissed(sid,")
    render_call = poll_body.find("showApprovalForSession(sid,data.pending")
    assert dismiss_check != -1 and render_call != -1
    assert dismiss_check < render_call, (
        "the dismissal check must gate the showApprovalForSession render"
    )


# ── Frontend: idless card degrades to unresolved state (contract 1) ───────

def test_show_approval_card_disables_controls_without_identity():
    """Without an actionable approval_id the card must render disabled rather
    than silently no-op on Allow/Deny."""
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("functionshowApprovalCard(")
    assert func_start != -1
    # Locate the idless guard after the responding-controls block.
    guard = "_setApprovalControlsDisabled(null,true)"
    assert guard in compact[func_start:], (
        "showApprovalCard must disable action controls when identity is missing"
    )
    responding_block = compact.find("_approvalResponseMatches(sid,_approvalCurrentId)", func_start)
    guard_idx = compact.find(guard, func_start)
    assert responding_block != -1 and guard_idx != -1
    assert responding_block < guard_idx, (
        "the idless guard must run after the normal responding-controls update"
    )
