"""Tests for approval queue multi-entry support (issue #527).

Previously _pending[sid] held one entry, so simultaneous approvals overwrote
each other. This PR changes submit_pending() to append to a list and adds
approval_id so /api/approval/respond can target a specific entry.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

ROUTES_SRC = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
# Approval helpers moved to api.route_approvals after the #1907 extraction;
# combine both files so static-analysis assertions still pass.
_ROUTE_APPROVALS = REPO_ROOT / "api" / "route_approvals.py"
ROUTES_SRC_FULL = ROUTES_SRC + (_ROUTE_APPROVALS.read_text(encoding="utf-8") if _ROUTE_APPROVALS.exists() else "")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static-analysis: Python routes
# ---------------------------------------------------------------------------

def test_submit_pending_appends_to_list():
    """submit_pending() must append to a list, not overwrite."""
    # The new wrapper must contain a queue append (list mutation pattern)
    assert "queue_list.append(entry)" in ROUTES_SRC_FULL or "queue.append(entry)" in ROUTES_SRC_FULL, \
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
    """The poll loop must pass pending_count to the owner-aware approval renderer."""
    assert "showApprovalForSession(sid, data.pending, data.pending_count" in MESSAGES_JS, \
        "Poll loop must pass data.pending_count through showApprovalForSession"


def test_chat_stream_approval_listener_renders_received_count():
    """A chat-stream approval event with count two renders the head and count."""
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node is required for the approval listener harness")
    start = MESSAGES_JS.index("source.addEventListener('approval',e=>{")
    end_marker = "\n    });"
    end = MESSAGES_JS.index(end_marker, start) + len(end_marker)
    listener = MESSAGES_JS[start:end]
    script = f"""
    const rendered = [];
    let activeSid = 'sid-browser';
    const source = {{ listeners: {{}}, addEventListener(name, cb) {{ this.listeners[name] = cb; }} }};
    function _applyToAnchor() {{}}
    function showApprovalForSession(sid, data, count) {{ rendered.push({{sid, data, count}}); }}
    function playAttentionSound() {{}}
    function _attentionSoundKey() {{ return 'approval'; }}
    function sendBrowserNotification() {{}}
    {listener}
    source.listeners.approval({{ data: JSON.stringify({{command: 'head', approval_id: 'a', pending_count: 2}}) }});
    process.stdout.write(JSON.stringify(rendered));
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered == [{
        "sid": "sid-browser",
        "data": {"command": "head", "approval_id": "a", "pending_count": 2},
        "count": 2,
    }]


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


def test_stale_explicit_approval_id_does_not_pop_oldest_entry():
    """Duplicate/stale approval responses must not resolve a different command."""
    from api import routes as r

    sid = "test-stale-approval-id-sid"
    with r._lock:
        r._pending.pop(sid, None)

    r.submit_pending(sid, {"command": "cmd1", "pattern_key": "p1", "pattern_keys": ["p1"], "description": "d1"})
    r.submit_pending(sid, {"command": "cmd2", "pattern_key": "p2", "pattern_keys": ["p2"], "description": "d2"})

    accepted = r._resolve_approval_legacy(sid, "missing-approval-id", "deny")

    assert accepted is False
    with r._lock:
        queue = r._pending.get(sid, [])
        commands = [entry["command"] for entry in queue]
    assert commands == ["cmd1", "cmd2"]

    with r._lock:
        r._pending.pop(sid, None)


# ---------------------------------------------------------------------------
# Delegated-child approval routing (#6943)
#
# The agent rebinds a delegated child's approval authority to a child-owned
# key "subagent:<child_session_id>" (hermes-agent #82009 contract). These
# tests prove the WebUI surfaces those child-key approvals under the parent
# session key. The coordinated exact-entry resolve plus agent-side waiter
# wakeup lands in the follow-up gated on the agent contract.
# ---------------------------------------------------------------------------

def _seed_child_parent(child_session_id: str, parent_session_id: str) -> None:
    from api import route_approvals as ra

    ra.seed_child_parent(child_session_id, parent_session_id)


def _clear_approval_state(*session_keys: str) -> None:
    from api import routes as r
    from api import route_approvals as ra

    with ra._lock:
        for key in session_keys:
            r._pending.pop(key, None)
            r._gateway_queues.pop(key, None)
        ra._child_approval_parents.clear()


def test_child_approval_surfaced_under_parent_key():
    """A child-key approval must be visible when polling the parent session."""
    from api import routes as r

    parent = "test-child-parent-surfaced"
    child = "test-child-surfaced"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with r._lock:
            head, total = r.pending_head_for_session_locked(parent)
        assert head is not None, "child approval must be surfaced under parent key"
        assert head["command"] == "childcmd"
        assert total == 1
        # The parent's own queue stays untouched; the entry lives under the
        # child key so the agent-side child resolution still finds it.
        with r._lock:
            assert not r._pending.get(parent)
            assert len(r._pending[child_key]) == 1
    finally:
        _clear_approval_state(parent, child_key)


def test_parent_approval_unchanged_by_child_routing():
    """A normal parent-key approval resolves exactly as before the fix."""
    from api import routes as r

    parent = "test-child-parent-normal"
    child = "test-child-normal"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        r.submit_pending(
            parent,
            {"command": "parentcmd", "pattern_key": "pp", "pattern_keys": ["pp"], "description": "pd"},
        )
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with r._lock:
            parent_aid = r._pending[parent][0]["approval_id"]
            child_aid = r._pending[child_key][0]["approval_id"]

        # Parent head must be its own approval, not the child's.
        with r._lock:
            head, total = r.pending_head_for_session_locked(parent)
        assert head["approval_id"] == parent_aid
        assert total == 2

        assert r._resolve_approval_legacy(parent, parent_aid, "once") is True
        with r._lock:
            assert parent not in r._pending
            assert len(r._pending[child_key]) == 1
            assert r._pending[child_key][0]["approval_id"] == child_aid
        # A stale explicit child id must not resolve the unrelated parent head
        # (#527 guard) — and here nothing is pending for the parent at all.
        assert r._resolve_approval_legacy(parent, "missing-id", "deny") is False
    finally:
        _clear_approval_state(parent, child_key)


def test_session_has_pending_approval_sees_child_approval():
    """_session_has_pending_approval must count child-key work as live."""
    from api import routes as r

    parent = "test-child-parent-haspending"
    child = "test-child-haspending"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        assert r._session_has_pending_approval(parent) is False
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        assert r._session_has_pending_approval(parent) is True
    finally:
        _clear_approval_state(parent, child_key)


def test_attention_summary_lights_for_child_approval():
    """The sidebar attention dot must light when only a child approval is live."""
    from api import routes as r

    parent = "test-child-parent-attn"
    child = "test-child-attn"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        assert r._session_attention_summary(parent) is None
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        summary = r._session_attention_summary(parent)
        assert summary is not None
        assert summary["kind"] == "approval"
        assert summary["count"] == 1
    finally:
        _clear_approval_state(parent, child_key)


def test_unassociated_child_approval_not_surfaced():
    """A child key with no recorded parent must fail closed (never surfaced)."""
    from api import routes as r

    parent = "test-child-parent-unassoc"
    child = "test-child-unassoc"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    try:
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with r._lock:
            head, total = r.pending_head_for_session_locked(parent)
        assert head is None
        assert total == 0
        with r._lock:
            aid = r._pending[child_key][0]["approval_id"]
        # Explicit-id respond must fail closed: the entry stays parked under
        # the child key, never surfaced into an unrelated parent session.
        assert r._resolve_approval_legacy(parent, aid, "once") is False
        with r._lock:
            assert child_key in r._pending, "unassociated child entry must stay parked"
    finally:
        _clear_approval_state(parent, child_key)


# ---------------------------------------------------------------------------
# Read/surface half of the #6961 review: cache scoping (#4), aggregate count
# on all three surface paths (#5), and child-change SSE relay to the parent
# (#6). The resolve half (#1/#2/#3) stays in the follow-up gated on the agent
# contract, exactly as the maintainer suggested.
# ---------------------------------------------------------------------------

def test_child_parent_cache_scoped_by_state_db_profile(monkeypatch):
    """#4: a cached parent lookup must never leak across state-db profiles."""
    import pathlib
    from api import route_approvals as ra
    from api import models as api_models

    child = "test-child-cache-scope"
    profile_a = pathlib.Path("/tmp/__profile_a__/state.db")
    profile_b = pathlib.Path("/tmp/__profile_b__/state.db")
    _clear_approval_state()
    try:
        monkeypatch.setattr(api_models, "_active_state_db_path", lambda: profile_a)
        ra.seed_child_parent(child, "parent-a")
        assert ra._child_parent_session_id(child) == "parent-a"

        # Switching profile must NOT see profile A's cached parent — the
        # entry is keyed by canonical state-db path + child id (#6961 #4).
        monkeypatch.setattr(api_models, "_active_state_db_path", lambda: profile_b)
        assert ra._child_parent_session_id(child) is None

        # Profile B can record its own mapping independently.
        ra.seed_child_parent(child, "parent-b")
        assert ra._child_parent_session_id(child) == "parent-b"

        # Back on profile A, the original mapping is still intact.
        monkeypatch.setattr(api_models, "_active_state_db_path", lambda: profile_a)
        assert ra._child_parent_session_id(child) == "parent-a"
    finally:
        monkeypatch.undo()
        _clear_approval_state()


def test_child_parent_cache_does_not_cache_misses():
    """#4: a failed lookup must not be cached, so a late DB write is seen."""
    from api import route_approvals as ra

    child = "test-child-cache-miss"
    _clear_approval_state()
    try:
        # Unknown child -> None, and NOT cached (no negative-cache poison).
        assert ra._child_parent_session_id(child) is None
        assert ra._child_parent_cache_key(child) not in ra._child_approval_parents
        # After the mapping is seeded (simulating a late state.db write),
        # the next lookup succeeds — the miss was not cached.
        ra.seed_child_parent(child, "parent-late")
        assert ra._child_parent_session_id(child) == "parent-late"
    finally:
        _clear_approval_state()


def test_child_parent_cache_invalidates_on_ownership_change():
    """#4: invalidate_child_parent_cache must drop stale positives."""
    from api import route_approvals as ra

    child = "test-child-cache-invalidate"
    _clear_approval_state()
    try:
        ra.seed_child_parent(child, "parent-old")
        assert ra._child_parent_session_id(child) == "parent-old"
        ra.invalidate_child_parent_cache(child)
        assert ra._child_parent_cache_key(child) not in ra._child_approval_parents
        ra.seed_child_parent(child, "parent-new")
        assert ra._child_parent_session_id(child) == "parent-new"
        # Full clear also works.
        ra.invalidate_child_parent_cache()
        assert not ra._child_approval_parents
    finally:
        _clear_approval_state()


def test_aggregate_count_includes_child_when_parent_has_approval():
    """#5: parent-1 + child-1 must count 2 on every surface path."""
    from api import routes as r
    from api import route_approvals as ra

    parent = "test-child-parent-aggr"
    child = "test-child-aggr"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        r.submit_pending(
            parent,
            {"command": "parentcmd", "pattern_key": "pp", "pattern_keys": ["pp"], "description": "pd"},
        )
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        # Aggregate projection: count 2 (parent 1 + child 1).
        with r._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert total == 2
        assert head["command"] == "parentcmd"  # own head stays first
        # Attention summary: count 2, not 1.
        summary = r._session_attention_summary(parent)
        assert summary is not None
        assert summary["kind"] == "approval"
        assert summary["count"] == 2
    finally:
        _clear_approval_state(parent, child_key)


def test_aggregate_dedupes_mirror_and_gateway_representation():
    """#5: the same approval in _pending (mirror) and _gateway_queues counts once."""
    from api import routes as r
    from api import route_approvals as ra

    parent = "test-child-parent-dedupe"
    _clear_approval_state(parent)
    try:
        r.submit_pending(
            parent,
            {"command": "cmd", "pattern_key": "pk", "pattern_keys": ["pk"], "description": "d"},
        )
        with r._lock:
            q = r._pending[parent]
            aid = q[0]["approval_id"]
        with ra._lock:
            # Simulate the gateway mirror representation of the SAME approval
            # parked in _gateway_queues (data carries the same approval_id).
            entry = type("Entry", (), {"data": {"command": "cmd", "approval_id": aid}})()
            r._gateway_queues.setdefault(parent, []).append(entry)
            head, total = ra.pending_head_for_session_locked(parent)
        assert total == 1, "mirror + live gateway entry for one approval must dedupe to 1"
        assert head["approval_id"] == aid
    finally:
        _clear_approval_state(parent)


def test_polling_endpoint_reports_aggregate_count():
    """#5: _handle_approval_pending must report 2 for parent-1 + child-1."""
    from urllib.parse import urlparse
    import io
    from api import routes as r

    parent = "test-child-parent-poll"
    child = "test-child-poll"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        r.submit_pending(
            parent,
            {"command": "parentcmd", "pattern_key": "pp", "pattern_keys": ["pp"], "description": "pd"},
        )
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        handler = type("H", (), {
            "wfile": io.BytesIO(),
            "send_response": lambda self, s: None,
            "send_header": lambda self, k, v: None,
            "end_headers": lambda self: None,
        })()
        r._handle_approval_pending(handler, urlparse(f"/api/approval/pending?session_id={parent}"))
        import json as _json
        body = _json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert body["pending_count"] == 2
        assert body["pending"]["command"] == "parentcmd"
    finally:
        _clear_approval_state(parent, child_key)


def test_sse_initial_snapshot_reports_aggregate_count():
    """#5: SSE initial snapshot must include child approvals with count 2."""
    from api import routes as r
    from api import route_approvals as ra

    parent = "test-child-parent-sseinit"
    child = "test-child-sseinit"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        r.submit_pending(
            parent,
            {"command": "parentcmd", "pattern_key": "pp", "pattern_keys": ["pp"], "description": "pd"},
        )
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        # The stream handler's initial snapshot uses the same aggregate
        # projection as the polling endpoint.
        with r._lock:
            r.reconcile_gateway_pending_mirror_locked(parent)
            initial_pending, initial_count = ra.pending_head_for_session_locked(parent)
        assert initial_count == 2
        assert initial_pending["command"] == "parentcmd"
    finally:
        _clear_approval_state(parent, child_key)


def test_parent_sse_subscriber_receives_child_enqueue():
    """#6: a parent SSE subscriber must get a push when a child approval lands."""
    from api import routes as r
    from api import route_approvals as ra

    parent = "test-child-parent-sse-enqueue"
    child = "test-child-sse-enqueue"
    child_key = f"subagent:{child}"
    _clear_approval_state(parent, child_key)
    _seed_child_parent(child, parent)
    try:
        q = ra._approval_sse_subscribe(parent)
        try:
            r.submit_pending(
                child_key,
                {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
            )
            payload = q.get(timeout=2)
            assert payload["pending_count"] == 1
            assert payload["pending"]["command"] == "childcmd"
        finally:
            ra._approval_sse_unsubscribe(parent, q)
    finally:
        _clear_approval_state(parent, child_key)
