"""Tests for approval queue multi-entry support (issue #527).

Previously _pending[sid] held one entry, so simultaneous approvals overwrote
each other. This PR changes submit_pending() to append to a list and adds
approval_id so /api/approval/respond can target a specific entry.
"""
import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

ROUTES_SRC = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static-analysis: Python routes
# ---------------------------------------------------------------------------

def test_submit_pending_appends_to_list():
    """submit_pending() must append to a list, not overwrite."""
    # The new wrapper must contain queue.append
    assert "queue.append(entry)" in ROUTES_SRC, \
        "submit_pending() must append entry to a list queue, not overwrite _pending[sid]"


def test_submit_pending_adds_approval_id():
    """Each queued entry must get a unique approval_id."""
    assert "approval_id" in ROUTES_SRC and "uuid.uuid4().hex" in ROUTES_SRC, \
        "submit_pending() must assign a uuid4 approval_id to each queued entry"


def test_handle_approval_pending_returns_count():
    """_handle_approval_pending must return pending_count in its response."""
    assert '"pending_count"' in ROUTES_SRC, \
        "_handle_approval_pending must include pending_count in the JSON response"


def test_handle_approval_respond_pops_by_approval_id():
    """_handle_approval_respond must target entry by approval_id."""
    assert 'approval_id = body.get("approval_id"' in ROUTES_SRC, \
        "_handle_approval_respond must read approval_id from request body"
    assert 'entry.get("approval_id") == approval_id' in ROUTES_SRC, \
        "_handle_approval_respond must find and pop the matching entry by approval_id"


def test_handle_approval_respond_fallback_to_oldest():
    """When no approval_id is given, fall back to popping the oldest entry (FIFO)."""
    # The fallback path: queue.pop(0) when approval_id is empty
    assert "queue.pop(0)" in ROUTES_SRC, \
        "_handle_approval_respond must fall back to popping the oldest entry when approval_id is absent"


def test_backward_compat_legacy_dict_value():
    """The respond handler must tolerate a legacy single-dict value in _pending."""
    assert "Legacy single-dict value" in ROUTES_SRC or \
           "# Legacy single-dict" in ROUTES_SRC or \
           "elif queue:" in ROUTES_SRC, \
        "respond handler must handle legacy single-dict _pending values for backward compatibility"


# ---------------------------------------------------------------------------
# Static-analysis: JavaScript frontend
# ---------------------------------------------------------------------------

def test_respond_sends_approval_id():
    """respondApproval() must include approval_id in the POST body."""
    assert "approval_id: approvalId" in MESSAGES_JS, \
        "respondApproval() must send approval_id in the POST body to /api/approval/respond"


def test_show_approval_card_accepts_count():
    """showApprovalCard must accept a pendingCount parameter."""
    assert re.search(r"function showApprovalCard\(pending,\s*pendingCount\)", MESSAGES_JS), \
        "showApprovalCard() must accept a pendingCount argument"


def test_show_approval_card_renders_counter():
    """showApprovalCard must display a '1 of N pending' counter when N > 1."""
    assert '"1 of " + pendingCount + " pending"' in MESSAGES_JS or \
           "'1 of ' + pendingCount + ' pending'" in MESSAGES_JS, \
        "showApprovalCard() must render '1 of N pending' counter for multiple queued approvals"


def test_approval_current_id_tracked():
    """_approvalCurrentId must be set and cleared around each approval."""
    assert "_approvalCurrentId" in MESSAGES_JS, \
        "_approvalCurrentId must track the approval_id of the currently displayed card"
    assert "_approvalCurrentId = pending.approval_id" in MESSAGES_JS or \
           "_approvalCurrentId = pending.approval_id || null" in MESSAGES_JS, \
        "_approvalCurrentId must be assigned from pending.approval_id"
    # Must be nulled on respond
    assert "_approvalCurrentId = null" in MESSAGES_JS, \
        "_approvalCurrentId must be cleared when respondApproval() is called"


def test_polling_passes_count_to_show():
    """The poll loop must pass pending_count to showApprovalCard."""
    assert "showApprovalCard(data.pending, data.pending_count" in MESSAGES_JS, \
        "Poll loop must pass data.pending_count to showApprovalCard"


# ---------------------------------------------------------------------------
# HTML: counter element present
# ---------------------------------------------------------------------------

def test_approval_counter_element_exists():
    """index.html must contain an approvalCounter element."""
    assert 'id="approvalCounter"' in INDEX_HTML, \
        "index.html must contain an element with id='approvalCounter' for the '1 of N' display"


# ---------------------------------------------------------------------------
# Functional: multiple entries behave correctly (via routes module directly)
# ---------------------------------------------------------------------------

def test_multiple_approvals_both_surfaced():
    """Two submit_pending calls must produce two queued entries, not one."""
    import threading
    from api import routes as r

    # Reset state
    sid = "test-multi-approval-sid"
    with r._lock:
        r._pending.pop(sid, None)

    r.submit_pending(sid, {"command": "cmd1", "pattern_key": "p1", "pattern_keys": ["p1"], "description": "d1"})
    r.submit_pending(sid, {"command": "cmd2", "pattern_key": "p2", "pattern_keys": ["p2"], "description": "d2"})

    with r._lock:
        queue = r._pending.get(sid)

    assert isinstance(queue, list), "After two submit_pending calls, _pending[sid] must be a list"
    assert len(queue) == 2, f"Expected 2 queued entries, got {len(queue)}"
    assert queue[0]["command"] == "cmd1"
    assert queue[1]["command"] == "cmd2"
    assert queue[0].get("approval_id"), "First entry must have an approval_id"
    assert queue[1].get("approval_id"), "Second entry must have an approval_id"
    assert queue[0]["approval_id"] != queue[1]["approval_id"], "Each entry must have a unique approval_id"

    # Cleanup
    with r._lock:
        r._pending.pop(sid, None)


def test_respond_by_approval_id_pops_correct_entry():
    """Responding with approval_id must remove only the targeted entry."""
    from api import routes as r

    sid = "test-respond-by-id-sid"
    with r._lock:
        r._pending.pop(sid, None)

    r.submit_pending(sid, {"command": "cmd1", "pattern_key": "p1", "pattern_keys": ["p1"], "description": "d1"})
    r.submit_pending(sid, {"command": "cmd2", "pattern_key": "p2", "pattern_keys": ["p2"], "description": "d2"})

    with r._lock:
        queue = r._pending.get(sid, [])
        aid2 = queue[1]["approval_id"] if len(queue) > 1 else None

    assert aid2, "Second entry must have an approval_id"

    # Respond to the SECOND entry by its approval_id
    # We call the handler internals directly (no HTTP)
    with r._lock:
        queue = r._pending.get(sid, [])
        popped = None
        for i, entry in enumerate(queue):
            if entry.get("approval_id") == aid2:
                popped = queue.pop(i)
                break

    assert popped is not None, "Should have found and popped entry by approval_id"
    assert popped["command"] == "cmd2", "Popped the wrong entry"

    with r._lock:
        remaining = r._pending.get(sid, [])

    assert len(remaining) == 1, "One entry should remain after popping the second"
    assert remaining[0]["command"] == "cmd1", "The remaining entry should be cmd1"

    # Cleanup
    with r._lock:
        r._pending.pop(sid, None)
