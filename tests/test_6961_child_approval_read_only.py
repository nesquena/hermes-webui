"""Regression tests for #6961 round-3 re-gate (read-only end-to-end child approvals).

The r3 review found three MUST-FIXes:
1. (CORE) The surfaced child card was actionable and routed into the PARENT's
   legacy FIFO resolver — production child entries have no approval_id, so a
   click signalled the parent's approval while the child stayed pending.
   Fix: child projections carry a non-empty read-only sentinel approval_id and
   the legacy resolver rejects it.
2. (SILENT) Cross-profile leak: _pending/_gateway_queues are process-global,
   so an identical child id across two profiles could surface profile A's
   pending command under profile B's parent. Fix: entries are bound to the
   enqueuing profile's state-db and the projection filters by that identity.
3. (SILENT) Malformed model_config fell through to the wrong physical parent.
   Fix: only the authoritative (parsed/absent) config may take the
   physical-parent fallback; malformed JSON fails closed.
"""
import json
import pathlib
import re
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

# Import order matters: `api.routes` loads `api.config`, which appends the
# discovered hermes-agent dir to sys.path (api/config.py). Importing it FIRST
# means `api.route_approvals` binds the REAL `tools.approval` module
# (`_pending`/`_lock`/`_gateway_queues` shared in-process) instead of the
# no-agent stub fallback — the same binding the production server gets, and
# the one the raw-producer regressions below must exercise.
from api import routes as r  # noqa: E402
from api import route_approvals as ra  # noqa: E402

_SENTINEL = "__read_only_child__:"


def _clear(*session_keys: str) -> None:
    with ra._lock:
        for key in session_keys:
            r._pending.pop(key, None)
            r._gateway_queues.pop(key, None)
        ra._child_approval_parents.clear()


def _seed(parent: str, child: str) -> str:
    child_key = f"subagent:{child}"
    _clear(parent, child_key)
    ra.seed_child_parent(child, parent)
    return child_key


# ---------------------------------------------------------------------------
# MUST-FIX 1 (CORE) — read-only sentinel projection
# ---------------------------------------------------------------------------

def test_child_projection_carries_read_only_sentinel():
    """The projected child head must be inert: sentinel id + read_only flag."""
    parent = "test-6961-sentinel-parent"
    child = "test-6961-sentinel-child"
    child_key = _seed(parent, child)
    try:
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert head is not None
        assert head["command"] == "childcmd"
        assert head["read_only"] is True, "child projection must be flagged read-only"
        assert str(head.get("approval_id") or "").startswith(_SENTINEL), (
            "child projection must carry the non-empty read-only sentinel id, "
            "never the raw null/absent production approval_id"
        )
        assert total == 1
        # The underlying child entry keeps its own identity under the child key.
        with ra._lock:
            raw = r._pending[child_key][0]
        assert not str(raw.get("approval_id") or "").startswith(_SENTINEL)
    finally:
        _clear(parent, child_key)


def test_legacy_resolver_rejects_read_only_sentinel():
    """A sentinel approval_id must never resolve through the parent resolver."""
    parent = "test-6961-resolver-parent"
    child = "test-6961-resolver-child"
    child_key = _seed(parent, child)
    try:
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with ra._lock:
            head, _total = ra.pending_head_for_session_locked(parent)
        assert head is not None
        sentinel_id = str(head["approval_id"])
        # Attempting to answer the surfaced child through the parent's legacy
        # FIFO resolver must fail closed (return False, nothing resolved).
        resolved = r._resolve_approval_legacy(parent, sentinel_id, "once")
        assert resolved is False
        # The child entry stays pending untouched.
        with ra._lock:
            assert len(r._pending[child_key]) == 1
    finally:
        _clear(parent, child_key)


def test_frontend_disables_controls_for_read_only_card():
    """messages.js must disable every approval control for sentinel cards."""
    src = pathlib.Path(REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert "_READ_ONLY_APPROVAL_PREFIX" in src
    assert "_setApprovalControlsDisabled(" in src
    # The read-only branch must disable controls unconditionally.
    assert "readOnly || responding" in src, (
        "read-only cards must disable every approval control"
    )
    # respondApproval must refuse sentinel ids outright.
    assert "_READ_ONLY_APPROVAL_PREFIX) === 0" in src


# ---------------------------------------------------------------------------
# MUST-FIX 2 (SILENT) — cross-profile provenance filter
# ---------------------------------------------------------------------------

def test_cross_profile_child_entry_not_projected(monkeypatch):
    """A child entry parked by profile A must never surface under profile B."""
    parent = "test-6961-prov-parent"
    child = "test-6961-prov-child"
    child_key = _seed(parent, child)
    try:
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "profileA-db")
        r.submit_pending(
            child_key,
            {"command": "acmd", "pattern_key": "ap", "pattern_keys": ["ap"], "description": "ad"},
        )
        # Same process, profile B active: the entry must not be projected.
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "profileB-db")
        with ra._lock:
            head_b, total_b = ra.pending_head_for_session_locked(parent)
        assert head_b is None and total_b == 0, (
            "cross-profile child entry must be filtered from the projection"
        )
        # Back on profile A the entry is visible again.
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "profileA-db")
        with ra._lock:
            head_a, total_a = ra.pending_head_for_session_locked(parent)
        assert head_a is not None and total_a == 1
    finally:
        monkeypatch.setattr(ra, "_child_provenance_current", ra._child_provenance_current.__wrapped__ if hasattr(ra._child_provenance_current, "__wrapped__") else ra._child_provenance_current)
        _clear(parent, child_key)


# ---------------------------------------------------------------------------
# MUST-FIX 3 (SILENT) — malformed model_config fails closed
# ---------------------------------------------------------------------------

def test_malformed_model_config_fails_closed(tmp_path, monkeypatch):
    """Malformed model_config must never fall through to the physical parent."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, model_config TEXT, source TEXT)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("malformed-child", "physical-parent", "{not-valid-json", "subagent"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(ra, "_child_parent_cache_key", lambda child: ("test-malformed-db", child))
    from api import models as api_models
    monkeypatch.setattr(api_models, "_active_state_db_path", lambda: str(db_path))
    with ra._lock:
        ra._child_approval_parents.clear()
    try:
        parent = ra._child_parent_session_id("malformed-child")
        assert parent is None, (
            "malformed model_config must fail closed (no physical-parent fallback)"
        )
    finally:
        with ra._lock:
            ra._child_approval_parents.clear()


def test_valid_delegate_from_still_resolves(tmp_path, monkeypatch):
    """A well-formed _delegate_from must still win over the physical parent."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, model_config TEXT, source TEXT)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            ("good-child", "physical-parent", json.dumps({"_delegate_from": "logical-parent"}), "subagent"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(ra, "_child_parent_cache_key", lambda child: ("test-good-db", child))
    from api import models as api_models
    monkeypatch.setattr(api_models, "_active_state_db_path", lambda: str(db_path))
    with ra._lock:
        ra._child_approval_parents.clear()
    try:
        parent = ra._child_parent_session_id("good-child")
        assert parent == "logical-parent"
    finally:
        with ra._lock:
            ra._child_approval_parents.clear()


# ---------------------------------------------------------------------------
# MUST-FIX 2 (r4) — explicit-empty/null lineage fails open → key presence
# ---------------------------------------------------------------------------

def _lineage_row(db_path, child_id, raw_config, source, physical_parent):
    """Insert one lineage-matrix row into a fresh state.db."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, model_config TEXT, source TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)",
            (child_id, physical_parent, raw_config, source),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "raw_config,source,physical_parent,expected",
    [
        # Genuinely absent marker + legacy subagent signal → physical parent.
        (None, "subagent", "physical-parent", "physical-parent"),
        (json.dumps({"other": 1}), "subagent", "physical-parent", "physical-parent"),
        # Present non-empty string marker wins.
        (json.dumps({"_delegate_from": "logical-parent"}), "subagent", "physical-parent", "logical-parent"),
        # Explicitly present EMPTY marker: authoritative fail-closed, never the
        # physical-parent fallback (#6961 r4 #2).
        (json.dumps({"_delegate_from": ""}), "subagent", "physical-parent", None),
        # Explicitly present NULL marker: fail closed.
        (json.dumps({"_delegate_from": None}), "subagent", "physical-parent", None),
        # Explicitly present non-string marker: fail closed.
        (json.dumps({"_delegate_from": 123}), "subagent", "physical-parent", None),
        (json.dumps({"_delegate_from": []}), "subagent", "physical-parent", None),
        # Malformed / non-dict config: fail closed (no physical-parent fallback).
        ("{not-valid-json", "subagent", "physical-parent", None),
        (json.dumps([1, 2]), "subagent", "physical-parent", None),
        # Absent marker + non-subagent source: no parent.
        (None, "api_server", "physical-parent", None),
    ],
)
def test_lineage_matrix_fails_closed(tmp_path, monkeypatch, raw_config, source, physical_parent, expected):
    """_delegate_from key PRESENCE must be distinguished from absence.

    An explicitly present empty/null/non-string marker declares the child's
    lineage and must never fall through to the physical-parent fallback; only
    a genuinely absent marker may take the legacy `source='subagent'` path.
    """
    db_path = tmp_path / "state.db"
    child_id = f"lineage-{abs(hash((str(raw_config), source)))}-{len(list(tmp_path.iterdir()))}"
    _lineage_row(db_path, child_id, raw_config, source, physical_parent)
    monkeypatch.setattr(ra, "_child_parent_cache_key", lambda child: ("test-lineage-db", child))
    from api import models as api_models
    monkeypatch.setattr(api_models, "_active_state_db_path", lambda: str(db_path))
    with ra._lock:
        ra._child_approval_parents.clear()
    try:
        parent = ra._child_parent_session_id(child_id)
        assert parent == expected, (
            f"raw_config={raw_config!r} source={source!r} -> {parent!r}, expected {expected!r}"
        )
    finally:
        with ra._lock:
            ra._child_approval_parents.clear()


# ---------------------------------------------------------------------------
# MUST-FIX 1 (r4) — real raw-producer provenance + raw SSE relay
# ---------------------------------------------------------------------------

def _raw_tools_approval():
    """The installed Agent's raw `tools.approval` module, or skip."""
    return pytest.importorskip("tools.approval")


def test_raw_producer_child_enqueue_gets_provenance_and_surfaces():
    """The REAL Agent `tools.approval.submit_pending()` must stamp canonical
    provenance on child-key entries so the projector surfaces them.

    The r3 projector filters child entries by `_child_provenance`; the raw
    producer never called the WebUI wrapper, so the real child row was
    filtered instead of surfaced. The import-time boundary hook must make the
    raw path equivalent to the wrapper path (#6961 r4 #1).
    """
    ta = _raw_tools_approval()
    parent = "test-6961-raw-parent"
    child = "test-6961-raw-child"
    child_key = _seed(parent, child)
    try:
        ta.submit_pending(
            child_key,
            {"command": "rawchildcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        # The raw entry itself now carries the enqueuing profile's provenance.
        with ra._lock:
            raw = ra._pending[child_key]
        assert isinstance(raw, dict), "raw producer stores a legacy single dict"
        assert str(raw.get("_child_provenance") or "").strip(), (
            "raw child enqueue must stamp _child_provenance (found empty)"
        )
        # And the projector now surfaces it instead of filtering it out.
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert total == 1
        assert head is not None and head["command"] == "rawchildcmd"
        assert head["read_only"] is True
        assert str(head.get("approval_id") or "").startswith(_SENTINEL)
    finally:
        _clear(parent, child_key)


def test_raw_producer_relays_parent_sse_subscriber():
    """A raw Agent child enqueue must push the aggregate to the parent SSE
    subscriber — the relay was previously only wired into the WebUI wrapper
    path (#6961 r4 #3)."""
    ta = _raw_tools_approval()
    parent = "test-6961-raw-sse-parent"
    child = "test-6961-raw-sse-child"
    child_key = _seed(parent, child)
    q = ra._approval_sse_subscribe(parent)
    try:
        ta.submit_pending(
            child_key,
            {"command": "rawssechild", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        payload = q.get(timeout=2)
        assert payload["pending_count"] == 1
        assert payload["pending"]["command"] == "rawssechild"
        assert str(payload["pending"].get("approval_id") or "").startswith(_SENTINEL)
    finally:
        ra._approval_sse_unsubscribe(parent, q)
        _clear(parent, child_key)


def test_raw_producer_two_profile_same_child_id(monkeypatch):
    """Raw enqueue under profile A must never surface under profile B, even
    with the same child id (process-global queue, raw producer path)."""
    ta = _raw_tools_approval()
    parent = "test-6961-raw-prov-parent"
    child = "test-6961-raw-prov-child"
    child_key = _seed(parent, child)
    try:
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "rawProfileA-db")
        ta.submit_pending(
            child_key,
            {"command": "rawacmd", "pattern_key": "ap", "pattern_keys": ["ap"], "description": "ad"},
        )
        # Same process, profile B active: filtered from the projection.
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "rawProfileB-db")
        with ra._lock:
            head_b, total_b = ra.pending_head_for_session_locked(parent)
        assert head_b is None and total_b == 0, (
            "raw cross-profile child entry must be filtered from the projection"
        )
        # Back on profile A the raw entry is visible again.
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "rawProfileA-db")
        with ra._lock:
            head_a, total_a = ra.pending_head_for_session_locked(parent)
        assert head_a is not None and total_a == 1
    finally:
        _clear(parent, child_key)


# ---------------------------------------------------------------------------
# MUST-FIX 1 (r4) — sentinel with a SIMULTANEOUS parent approval
# ---------------------------------------------------------------------------

def test_sentinel_does_not_resolve_simultaneous_parent_approval():
    """Answering a surfaced child card (sentinel id) while the parent has its
    OWN pending approval must resolve nothing — the parent approval stays
    intact and the child stays pending (#6961 r4 #4)."""
    parent = "test-6961-simul-parent"
    child = "test-6961-simul-child"
    child_key = _seed(parent, child)
    try:
        r.submit_pending(
            parent,
            {"command": "parentcmd", "pattern_key": "pp", "pattern_keys": ["pp"], "description": "pd"},
        )
        r.submit_pending(
            child_key,
            {"command": "childcmd", "pattern_key": "cp", "pattern_keys": ["cp"], "description": "cd"},
        )
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert total == 2, "parent-own + child must both be visible"
        assert head["command"] == "parentcmd"
        sentinel_id = f"{_SENTINEL}{child_key}"

        # Legacy resolver: sentinel must fail closed and resolve NOTHING.
        resolved = r._resolve_approval_legacy(parent, sentinel_id, "once")
        assert resolved is False
        with ra._lock:
            assert len(r._pending[parent]) == 1, "parent approval must stay pending"
            assert len(r._pending[child_key]) == 1, "child entry must stay pending"

        # HTTP respond handler: sentinel rejected before any resolver side
        # effect (409), parent approval still untouched.
        import io
        captured_status = {}
        handler = type("H", (), {
            "wfile": io.BytesIO(),
            "send_response": lambda self, s: captured_status.__setitem__("status", s),
            "send_header": lambda self, k, v: None,
            "end_headers": lambda self: None,
        })()
        r._handle_approval_respond(
            handler,
            {"session_id": parent, "choice": "once", "approval_id": sentinel_id},
        )
        assert captured_status.get("status") == 409, (
            "respond with a sentinel approval_id must be rejected with 409"
        )
        response_body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        assert response_body.get("ok") is False
        with ra._lock:
            assert len(r._pending[parent]) == 1, (
                "respond with sentinel must not consume the parent approval"
            )
    finally:
        _clear(parent, child_key)


# ---------------------------------------------------------------------------
# MUST-FIX 4 (r4) — full-control frontend regressions (Skip all / YOLO)
# ---------------------------------------------------------------------------

def test_frontend_disables_skip_all_for_read_only_card():
    """Every card action must be inert on read-only cards — including Skip all
    / YOLO, which previously stayed wired to toggleYoloFromApproval() and
    mutated the parent session (#6961 r4 #4)."""
    src = pathlib.Path(REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    # The disabled set must now include the Skip all / YOLO control.
    assert '"approvalSkipAll"' in src, (
        "_setApprovalControlsDisabled must reference the Skip all button"
    )
    assert "skipAll.disabled = !!disabled" in src, (
        "the Skip all button must be disabled together with the other controls"
    )
    # toggleYoloFromApproval must refuse read-only sentinel cards outright.
    assert "async function toggleYoloFromApproval()" in src
    func_start = src.index("async function toggleYoloFromApproval()")
    toggle_body = src[func_start:func_start + 700]
    assert "_READ_ONLY_APPROVAL_PREFIX) === 0" in toggle_body, (
        "toggleYoloFromApproval must early-return on read-only sentinel cards"
    )
    assert "return;" in toggle_body.split("const sid = S.session")[0], (
        "the sentinel guard must return BEFORE the /api/session/yolo call"
    )
    # Belt-and-braces: respondApproval also still refuses the sentinel.
    assert src.count("_READ_ONLY_APPROVAL_PREFIX) === 0") >= 2


# ---------------------------------------------------------------------------
# Round-5 regressions — unknown provenance fails closed + gateway initial
# pending relay (#6961 r5)
# ---------------------------------------------------------------------------

def test_unknown_provenance_fails_closed_when_resolution_empty(monkeypatch):
    """state-db resolution failing on BOTH sides (entry_prov == current_prov
    == "") must fail closed: equality of two empty strings never authorizes a
    child projection (#6961 r5 #2)."""
    ta = _raw_tools_approval()
    parent = "test-6961-empty-prov-parent"
    child = "test-6961-empty-prov-child"
    child_key = _seed(parent, child)
    try:
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "")
        ta.submit_pending(
            child_key,
            {"command": "noprovcmd", "pattern_key": "np", "pattern_keys": ["np"], "description": "nd"},
        )
        # The raw entry really was stamped empty (resolution failed at enqueue).
        with ra._lock:
            raw = ra._pending[child_key]
        assert isinstance(raw, dict)
        assert not str(raw.get("_child_provenance") or "").strip(), (
            "raw enqueue with failed resolution must stamp empty provenance"
        )
        # Empty == empty must NOT authorize the projection.
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert head is None and total == 0, (
            "unknown provenance (empty on both sides) must fail closed"
        )
    finally:
        _clear(parent, child_key)


def test_provenance_requires_both_sides_non_empty(monkeypatch):
    """A child entry is only projected when BOTH the entry and the current
    provenance are non-empty AND equal — an empty stamp on either side fails
    closed even when the other side is known (#6961 r5 #2)."""
    parent = "test-6961-both-side-parent"
    child = "test-6961-both-side-child"
    child_key = _seed(parent, child)
    try:
        # Entry has no provenance at all, current is known -> filtered.
        with ra._lock:
            ra._pending[child_key] = [
                {"command": "stale", "pattern_key": "s", "pattern_keys": ["s"], "description": "sd"}
            ]
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "known-db")
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert head is None and total == 0, (
            "empty entry provenance must fail closed even with a known current side"
        )
        # Entry is known, current resolution returns empty -> filtered.
        with ra._lock:
            ra._pending[child_key] = [
                {"command": "stale", "pattern_key": "s", "pattern_keys": ["s"], "description": "sd",
                 "_child_provenance": "known-db"}
            ]
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "")
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert head is None and total == 0, (
            "empty current provenance (state-db resolution failure) must fail closed"
        )
        # Sanity: both known and equal still authorizes.
        with ra._lock:
            ra._pending[child_key] = [
                {"command": "stale", "pattern_key": "s", "pattern_keys": ["s"], "description": "sd",
                 "_child_provenance": "known-db"}
            ]
        monkeypatch.setattr(ra, "_child_provenance_current", lambda: "known-db")
        with ra._lock:
            head, total = ra.pending_head_for_session_locked(parent)
        assert head is not None and total == 1
    finally:
        _clear(parent, child_key)


def test_gateway_child_enqueue_relays_initial_pending_to_parent_sse():
    """A child-key gateway enqueue must publish the parent's INITIAL aggregate
    while the worker is still blocked (entry parked), not only the removal
    after `_await_gateway_decision` returns (#6961 r5 #1)."""
    import threading

    ta = _raw_tools_approval()
    parent = "test-6961-gw-sse-parent"
    child = "test-6961-gw-sse-child"
    child_key = _seed(parent, child)
    q = ra._approval_sse_subscribe(parent)
    notified = []
    t = None
    try:
        def _worker():
            ta._await_gateway_decision(
                child_key,
                notified.append,
                {"command": "gwchild", "pattern_key": "gp", "pattern_keys": ["gp"], "description": "gd"},
                surface="gateway",
            )

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        # The worker parks the entry and invokes notify_cb BEFORE blocking; the
        # wrapped callback must already have relayed the parent's aggregate.
        payload = q.get(timeout=5)
        assert payload["pending_count"] == 1, (
            "parent must see the child pending while the gateway worker is blocked"
        )
        assert payload["pending"]["command"] == "gwchild"
        assert payload["pending"]["read_only"] is True
        assert str(payload["pending"].get("approval_id") or "").startswith(_SENTINEL)
        # The original notify_cb still fires, with the stamped data.
        assert notified and notified[0]["command"] == "gwchild"
        assert str(notified[0].get("_child_provenance") or "").strip(), (
            "gateway approval data must carry provenance"
        )
        # The worker is provably still blocked with the entry parked.
        with ra._lock:
            parked = list(ta._gateway_queues.get(child_key, []))
        assert len(parked) == 1, "worker must still be blocked with the entry parked"
        assert t.is_alive()

        # Resolve the entry: the worker unblocks, drops the entry, and the
        # retained finally relay publishes the removal to the parent.
        parked[0].result = "once"
        parked[0].event.set()
        t.join(timeout=5)
        assert not t.is_alive(), "resolving the gateway entry must unblock the worker"
        removal = q.get(timeout=2)
        assert removal["pending_count"] == 0 and removal["pending"] is None, (
            "resolving the gateway entry must relay the removal to the parent"
        )
    finally:
        if t is not None and t.is_alive():
            with ra._lock:
                for entry in ta._gateway_queues.get(child_key, []):
                    entry.event.set()
            t.join(timeout=2)
        ra._approval_sse_unsubscribe(parent, q)
        _clear(parent, child_key)
