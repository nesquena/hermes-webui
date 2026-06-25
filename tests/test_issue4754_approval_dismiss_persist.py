"""Tests for #4754: approval card dismissal persists across tab switches and restarts.

Uses the node-driver (static source extraction) pattern — no browser required.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


# ---------------------------------------------------------------------------
# localStorage helper presence
# ---------------------------------------------------------------------------

def test_dismissed_approvals_key_defined():
    assert "_DISMISSED_APPROVALS_KEY" in MESSAGES_JS
    assert "hermes_dismissed_approvals" in MESSAGES_JS


def test_get_dismissed_approvals_defined():
    assert "function _getDismissedApprovals(" in MESSAGES_JS


def test_is_approval_dismissed_defined():
    assert "function _isApprovalDismissed(" in MESSAGES_JS


def test_mark_approval_dismissed_defined():
    assert "function _markApprovalDismissed(" in MESSAGES_JS


def test_unmark_approval_dismissed_defined():
    assert "function _unmarkApprovalDismissed(" in MESSAGES_JS


# ---------------------------------------------------------------------------
# 100-entry cap: _markApprovalDismissed must call .slice(-100)
# ---------------------------------------------------------------------------

def test_dismissed_set_capped_at_100():
    compact = _compact(MESSAGES_JS)
    assert ".slice(-100)" in compact, "_markApprovalDismissed must cap the set at 100"


# ---------------------------------------------------------------------------
# Guard in showApprovalCard
# ---------------------------------------------------------------------------

def test_guard_in_show_approval_card():
    compact = _compact(MESSAGES_JS)
    # Guard must appear inside showApprovalCard, after _rememberApprovalPending
    func_start = compact.find("functionshowApprovalCard(")
    assert func_start != -1
    # Locate the guard after the function start (dismissals are namespaced by
    # session, so the guard passes sid + approval_id).
    guard = "_isApprovalDismissed(sid,pending.approval_id)"
    guard_idx = compact.find(guard, func_start)
    assert guard_idx != -1, "guard _isApprovalDismissed must appear in showApprovalCard"
    # _rememberApprovalPending must appear before the guard
    remember = "_rememberApprovalPending("
    remember_idx = compact.find(remember, func_start)
    assert remember_idx != -1
    assert remember_idx < guard_idx, "guard must come after _rememberApprovalPending"


def test_guard_returns_early():
    # The guard must be a return statement
    compact = _compact(MESSAGES_JS)
    assert "if(pending&&pending.approval_id&&_isApprovalDismissed(sid,pending.approval_id))return;" in compact


# ---------------------------------------------------------------------------
# dismissApprovalCard function
# ---------------------------------------------------------------------------

def test_dismiss_approval_card_defined():
    assert "function dismissApprovalCard(" in MESSAGES_JS


def test_dismiss_approval_card_marks_dismissed():
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("functiondismissApprovalCard(")
    assert func_start != -1
    body_end = compact.find("}", func_start)
    body = compact[func_start:body_end + 1]
    assert "_markApprovalDismissed(sid,_approvalCurrentId)" in body


def test_dismiss_approval_card_hides_card():
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("functiondismissApprovalCard(")
    assert func_start != -1
    body_end = compact.find("}", func_start)
    body = compact[func_start:body_end + 1]
    assert "hideApprovalCard(true)" in body


def test_dismiss_approval_card_clears_pending_attention():
    # dismissApprovalCard must call _clearApprovalPendingForSession so the tab
    # indicator stops blinking after dismiss. Without this, _rememberApprovalPending
    # reinsertes the pending entry on every poll tick and the indicator stays lit.
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("functiondismissApprovalCard(")
    assert func_start != -1
    body_end = compact.find("}", func_start)
    body = compact[func_start:body_end + 1]
    assert "_clearApprovalPendingForSession(sid)" in body, (
        "dismissApprovalCard must call _clearApprovalPendingForSession(sid)"
    )


def test_dismiss_approval_card_captures_sid_before_hide():
    # The session ID must be captured before hideApprovalCard(true) nulls out
    # _approvalSessionId so the clear call receives the correct ID.
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("functiondismissApprovalCard(")
    assert func_start != -1
    body_end = compact.find("}", func_start)
    body = compact[func_start:body_end + 1]
    sid_pos = body.find("constsid=_approvalSessionId")
    hide_pos = body.find("hideApprovalCard(true)")
    assert sid_pos != -1, "sid must be captured via const sid=_approvalSessionId"
    assert sid_pos < hide_pos, "sid must be captured before hideApprovalCard is called"


# ---------------------------------------------------------------------------
# respondApproval prunes dismissed set
# ---------------------------------------------------------------------------

def test_respond_approval_unmarks_dismissed():
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("asyncfunctionrespondApproval(")
    assert func_start != -1
    # Find the closing brace of the function (scan for matching })
    assert "_unmarkApprovalDismissed(sid,approvalId)" in compact[func_start:], \
        "_unmarkApprovalDismissed(sid,approvalId) must be called inside respondApproval"


def test_respond_approval_unmarks_before_clear():
    # _unmarkApprovalDismissed must come before _approvalCurrentId is set to null
    # so approvalId still holds the right value
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("asyncfunctionrespondApproval(")
    assert func_start != -1
    unmark_idx = compact.find("_unmarkApprovalDismissed(sid,approvalId)", func_start)
    clear_idx = compact.find("_approvalCurrentId=null;", func_start)
    assert unmark_idx != -1
    assert clear_idx != -1
    assert unmark_idx < clear_idx, \
        "_unmarkApprovalDismissed must be called before _approvalCurrentId is cleared"


# ---------------------------------------------------------------------------
# No-pending poll branch prunes dismissed set
# ---------------------------------------------------------------------------

def test_no_pending_branch_unmarks_dismissed():
    compact = _compact(MESSAGES_JS)
    # Anchor on the poll-specific else-if branch that checks mismatched session
    branch_marker = "elseif(!_approvalPollingSessionMissingOrMismatched(sid)){"
    branch_start = compact.find(branch_marker)
    assert branch_start != -1, "no-pending poll else-if branch must exist"
    nearby = compact[branch_start:branch_start + 400]
    # Must use session-scoped lookup, not the global _approvalCurrentId
    assert "_approvalPendingBySession.get(sid)" in nearby, \
        "no-pending poll branch must read session-scoped pending via _approvalPendingBySession.get(sid)"
    assert "_unmarkApprovalDismissed(sid,_resolvedId)" in nearby, \
        "no-pending poll branch must unmark the session-scoped resolved ID"
    # Must NOT use the global _approvalCurrentId to unmark (that caused the cross-session bug)
    assert "_unmarkApprovalDismissed(sid,_approvalCurrentId)" not in nearby, \
        "no-pending poll branch must not unmark via the global _approvalCurrentId"


def test_no_pending_branch_reads_pending_before_clear():
    # The session-scoped entry must be read BEFORE _clearApprovalPendingForSession erases it.
    compact = _compact(MESSAGES_JS)
    branch_marker = "elseif(!_approvalPollingSessionMissingOrMismatched(sid)){"
    branch_start = compact.find(branch_marker)
    assert branch_start != -1
    nearby = compact[branch_start:branch_start + 400]
    get_idx = nearby.find("_approvalPendingBySession.get(sid)")
    clear_idx = nearby.find("_clearApprovalPendingForSession(sid)")
    assert get_idx != -1, "_approvalPendingBySession.get(sid) must appear in branch"
    assert clear_idx != -1, "_clearApprovalPendingForSession(sid) must appear in branch"
    assert get_idx < clear_idx, \
        "_approvalPendingBySession.get(sid) must come before _clearApprovalPendingForSession"


def test_cross_session_dismiss_not_cleared_by_other_session_poll():
    """Polling session B with no pending must not un-dismiss session A's dismissed approval.

    Simulates the node-driver scenario:
    - session A has approval X in _approvalPendingBySession (it was dismissed)
    - _approvalCurrentId still holds X from before a session switch
    - polling session B returns no-pending
    - the branch must NOT unmark X because _approvalPendingBySession.get(sid_B) is empty
    """
    compact = _compact(MESSAGES_JS)
    branch_marker = "elseif(!_approvalPollingSessionMissingOrMismatched(sid)){"
    branch_start = compact.find(branch_marker)
    assert branch_start != -1
    nearby = compact[branch_start:branch_start + 400]

    # The resolved ID must come from the session map, not the global
    assert "_approvalPendingBySession.get(sid)" in nearby, \
        "resolved ID source must be session-scoped, not the global _approvalCurrentId"

    # The guard must be conditional on the resolved ID being truthy so that when
    # the session being polled has no pending entry, _unmarkApprovalDismissed is skipped.
    assert "if(_resolvedId)" in nearby, \
        "unmark must be gated on _resolvedId so it fires only for the resolved session"


# ---------------------------------------------------------------------------
# index.html: dismiss button present
# ---------------------------------------------------------------------------

def test_dismiss_button_in_html():
    assert 'class="approval-dismiss"' in INDEX_HTML


def test_dismiss_button_onclick():
    assert 'onclick="dismissApprovalCard()"' in INDEX_HTML


def test_dismiss_button_aria_label():
    assert 'aria-label="Dismiss approval"' in INDEX_HTML


def test_dismiss_button_near_collapse_button():
    # Dismiss button must appear after collapse button in document order
    collapse_idx = INDEX_HTML.find('id="approvalCollapse"')
    dismiss_idx = INDEX_HTML.find('class="approval-dismiss"')
    assert collapse_idx != -1
    assert dismiss_idx != -1
    assert dismiss_idx > collapse_idx, "dismiss button must follow collapse button"


# ---------------------------------------------------------------------------
# CSS: .approval-dismiss styled
# ---------------------------------------------------------------------------

def test_approval_dismiss_css_defined():
    assert ".approval-dismiss" in STYLE_CSS


# ---------------------------------------------------------------------------
# Functional: session-namespaced dismissal (the cross-session collision fix)
# ---------------------------------------------------------------------------

import json
import shutil
import subprocess

NODE = shutil.which("node")


def _extract_fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"{name} body not closed")


def test_same_approval_id_in_two_sessions_does_not_collide():
    """The bug Codex found: dismissing approval_id 'X' in session A must NOT hide
    a still-pending approval_id 'X' in session B (gateway/run sources can reuse
    externally-supplied IDs across sessions). Drives the real helper functions in
    node with a mocked localStorage."""
    if NODE is None:
        import pytest
        pytest.skip("node not available")
    helpers = "\n".join(
        _extract_fn(MESSAGES_JS, n)
        for n in ("_approvalDismissKey", "_getDismissedApprovals",
                  "_isApprovalDismissed", "_markApprovalDismissed", "_unmarkApprovalDismissed")
    )
    script = (
        "const _store = {};\n"
        "const localStorage = {\n"
        "  getItem: k => (k in _store ? _store[k] : null),\n"
        "  setItem: (k, v) => { _store[k] = String(v); },\n"
        "};\n"
        "const _DISMISSED_APPROVALS_KEY = 'hermes_dismissed_approvals';\n"
        + helpers +
        "\n"
        "// Dismiss approval 'X' in session A.\n"
        "_markApprovalDismissed('sessionA', 'X');\n"
        "const out = {\n"
        "  a_dismissed: _isApprovalDismissed('sessionA', 'X'),\n"
        "  b_not_dismissed: _isApprovalDismissed('sessionB', 'X'),\n"
        "  diff_id_same_session_not_dismissed: _isApprovalDismissed('sessionA', 'Y'),\n"
        "};\n"
        "// Responding in session B (unmark) must not clear session A's dismissal.\n"
        "_unmarkApprovalDismissed('sessionB', 'X');\n"
        "out.a_still_dismissed_after_b_unmark = _isApprovalDismissed('sessionA', 'X');\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True, timeout=15)
    out = json.loads(result.stdout)
    assert out["a_dismissed"] is True, "session A's own dismissal must register"
    assert out["b_not_dismissed"] is False, (
        "CROSS-SESSION COLLISION: dismissing approval_id 'X' in session A must NOT "
        "hide the same approval_id 'X' in session B"
    )
    assert out["diff_id_same_session_not_dismissed"] is False
    assert out["a_still_dismissed_after_b_unmark"] is True, (
        "un-dismissing 'X' in session B must not clear session A's dismissal of 'X'"
    )
