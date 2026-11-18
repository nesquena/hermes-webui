"""Regression tests for #9102 — compressed reset successors keep their marker.

A Gateway reset (``session_reset``) starts a new visible conversation, but the
Agent still records ``parent_session_id`` for lineage. ``read_session_lineage_metadata``
already labels that successor ``relationship_type='reset_successor'`` so the
sidebar does not draw the "Forked from ..." affordance.

Context compression then rotates the session id again (``A -> B -> B2``). The
compressed tip is not a direct child of the reset successor, so the
reset-successor rule never matched it and the tip came back with only
``_lineage_root_id`` / ``_compression_segment_count``. The sidebar then treated
the tip as a parent-linked branch (``_showsForkOrBranchIndicator``) and the
reset conversation went back to showing "Forked from ...".

These tests pin the marker on the compressed tip, that the continuation
grouping keys stay untouched, and the controls that must NOT gain the marker:
ordinary compression with no reset above it, an uncompressed reset successor,
and a session with no parent link.
"""
import json
import sqlite3

import pytest

import api.agent_sessions as agent_sessions
from tests.test_session_lineage_collapse import render_sidebar_rows


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            session_key TEXT,
            model_config TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        """
    )
    return conn


def _insert(
    conn,
    sid,
    *,
    parent=None,
    source="webui",
    started_at=0.0,
    ended_at=None,
    end_reason=None,
    session_key=None,
    model_config=None,
):
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count,
         parent_session_id, session_key, model_config, ended_at, end_reason)
        VALUES (?, ?, NULL, ?, 'openai/gpt-5', ?, 2, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            source,
            sid,
            started_at,
            parent,
            session_key,
            json.dumps(model_config) if isinstance(model_config, dict) else model_config,
            ended_at,
            end_reason,
        ),
    )
    conn.commit()


def _sidebar_rows(metadata, session_ids, titles=None):
    """Render the production sidebar grouping over lineage-enriched API rows."""
    rows = []
    for sid in session_ids:
        row = {"session_id": sid, "title": (titles or {}).get(sid, sid)}
        row.update(metadata.get(sid) or {})
        rows.append(row)
    return render_sidebar_rows(rows, rows, include_indicators=True)


@pytest.mark.parametrize(
    "source,parent_end_reason,parent_ended_at",
    [
        # Gateway messaging reset: the compatibility rule (source + chat key).
        ("telegram", "session_reset", 200.0),
        # Canonical ``_reset_from`` marker on a non-messaging surface.
        ("webui", "session_switch", 200.0),
    ],
)
def test_compressed_reset_successor_tip_keeps_reset_marker(
    tmp_path, source, parent_end_reason, parent_ended_at
):
    db = tmp_path / "state.db"
    conn = _make_db(db)
    try:
        _insert(
            conn, "A", source=source, started_at=100.0,
            ended_at=parent_ended_at, end_reason=parent_end_reason,
            session_key="dm-1",
        )
        # Reset successor: new conversation, durable parent link.
        _insert(
            conn, "B", parent="A", source=source, started_at=210.0,
            ended_at=300.0, end_reason="compression", session_key="dm-1",
            model_config={"_reset_from": "A"},
        )
        # Context compression rotates the id; model_config is inherited verbatim.
        _insert(
            conn, "B2", parent="B", source=source, started_at=310.0,
            session_key="dm-1", model_config={"_reset_from": "A"},
        )
    finally:
        conn.close()

    metadata = agent_sessions.read_session_lineage_metadata(db, {"A", "B", "B2"})
    tip = metadata["B2"]

    assert tip.get("relationship_type") == "reset_successor", (
        "a compressed tip of a reset successor must stay a reset successor, "
        "otherwise the sidebar draws 'Forked from ...' for it"
    )
    # Grouping keys must keep collapsing the chain onto the reset root.
    assert tip.get("_lineage_root_id") == "B"
    assert tip.get("_lineage_tip_id") == "B2"
    assert tip.get("_compression_segment_count") == 2
    assert metadata["B"].get("relationship_type") == "reset_successor"
    assert metadata["B"].get("_lineage_root_id") == "B"

    rendered = {row["session_id"]: row for row in _sidebar_rows(metadata, ["A", "B", "B2"])}
    shown = rendered.get("B2")
    assert shown is not None, "the compressed tip must stay the visible row"
    assert shown.get("_lineage_key") == "B"
    assert shown.get("_lineage_collapsed_count") == 2
    assert shown["_test_branch_indicator"] is False, (
        "the sidebar must not draw 'Forked from ...' for a compressed reset tip"
    )


def test_compressed_continuation_without_reset_keeps_no_marker(tmp_path):
    """Control (a): plain compression must not be relabelled as a reset."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    try:
        _insert(conn, "root", started_at=100.0, ended_at=200.0, end_reason="compression")
        # Root carries an inherited marker naming a grandparent, not its parent.
        _insert(
            conn, "tip", parent="root", started_at=210.0,
            model_config={"_reset_from": "grandparent"},
        )
    finally:
        conn.close()

    metadata = agent_sessions.read_session_lineage_metadata(db, {"root", "tip"})
    entry = metadata["tip"]

    assert "relationship_type" not in entry
    assert entry.get("_lineage_root_id") == "root"
    assert entry.get("_lineage_tip_id") == "tip"
    assert entry.get("_compression_segment_count") == 2

    rendered = {row["session_id"]: row for row in _sidebar_rows(metadata, ["root", "tip"])}
    shown = rendered.get("tip")
    assert shown is not None
    assert shown.get("relationship_type") != "reset_successor"
    # The continuation grouping metadata is what the sidebar reads here; it must
    # survive untouched by the reset-marker rule.
    assert shown.get("_lineage_root_id") == "root"
    assert shown.get("_compression_segment_count") == 2


def test_uncompressed_reset_successor_still_marked(tmp_path):
    """Control (b): the existing direct reset successor rule is unchanged."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    try:
        _insert(conn, "reset-parent", started_at=1.0, ended_at=10.0, end_reason="session_switch")
        _insert(
            conn, "reset", parent="reset-parent", started_at=11.0,
            model_config={"_reset_from": "reset-parent"},
        )
    finally:
        conn.close()

    metadata = agent_sessions.read_session_lineage_metadata(db, {"reset-parent", "reset"})
    entry = metadata["reset"]

    assert entry.get("relationship_type") == "reset_successor"
    assert entry.get("_lineage_root_id") == "reset"
    assert "_compression_segment_count" not in entry

    rendered = {row["session_id"]: row for row in _sidebar_rows(metadata, ["reset-parent", "reset"])}
    assert rendered["reset"]["_test_branch_indicator"] is False


def test_session_without_parent_does_not_raise(tmp_path):
    """Control (c): a top-level row and an unknown id stay silent."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    try:
        _insert(conn, "solo", started_at=100.0)
    finally:
        conn.close()

    metadata = agent_sessions.read_session_lineage_metadata(db, {"solo", "missing"})

    assert "missing" not in metadata
    entry = metadata["solo"]
    assert "relationship_type" not in entry
    assert "parent_session_id" not in entry
    assert "_lineage_root_id" not in entry
    assert "_compression_segment_count" not in entry
