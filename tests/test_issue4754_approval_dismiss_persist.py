"""Tests for #4754: approval card dismissal persists across tab switches and restarts.

Uses the node-driver (static source extraction) pattern — no browser required.
"""
import json
import re
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
    # Locate the guard after the function start
    guard = "_isApprovalDismissed(pending.approval_id)"
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
    assert "if(pending&&pending.approval_id&&_isApprovalDismissed(pending.approval_id))return;" in compact


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
    assert "_markApprovalDismissed(_approvalCurrentId)" in body


def test_dismiss_approval_card_hides_card():
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("functiondismissApprovalCard(")
    assert func_start != -1
    body_end = compact.find("}", func_start)
    body = compact[func_start:body_end + 1]
    assert "hideApprovalCard(true)" in body


# ---------------------------------------------------------------------------
# respondApproval prunes dismissed set
# ---------------------------------------------------------------------------

def test_respond_approval_unmarks_dismissed():
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("asyncfunctionrespondApproval(")
    assert func_start != -1
    # Find the closing brace of the function (scan for matching })
    assert "_unmarkApprovalDismissed(approvalId)" in compact[func_start:], \
        "_unmarkApprovalDismissed(approvalId) must be called inside respondApproval"


def test_respond_approval_unmarks_before_clear():
    # _unmarkApprovalDismissed must come before _approvalCurrentId is set to null
    # so approvalId still holds the right value
    compact = _compact(MESSAGES_JS)
    func_start = compact.find("asyncfunctionrespondApproval(")
    assert func_start != -1
    unmark_idx = compact.find("_unmarkApprovalDismissed(approvalId)", func_start)
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
    # _unmarkApprovalDismissed must appear within the branch (before _hideApprovalCardIfOwner)
    nearby = compact[branch_start:branch_start + 300]
    assert "_unmarkApprovalDismissed(_approvalCurrentId)" in nearby, \
        "no-pending poll branch must unmark dismissed when server clears the approval"


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
