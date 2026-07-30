"""Regression tests for the sidebar lineage cleanup.

Covers:
- WebUI compression rotations start the successor a fraction of a second
  BEFORE closing the predecessor (observed -0.1s to -1.0s overlap), so the
  strict ``child.started_at >= parent.ended_at`` check misclassified every
  rotation as a generic ``child_session`` and the sidebar showed each
  compression segment as a separate conversation.
- Visible lineage roots now carry a self-referenced ``_lineage_root_id`` so
  the client can collapse the whole chain (and re-attach forks) even when
  intermediate segments are hidden from the payload.
- Manual forks whose parent link exists only in the WebUI session file
  (state.db mirror keeps ``parent_session_id`` NULL) are re-attached under
  the visible lineage root instead of being promoted to orphan top-level
  rows.
"""

import sqlite3
import time


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            started_at REAL NOT NULL,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT,
            source TEXT DEFAULT 'webui',
            archived INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 1
        );
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        """
    )
    return conn


def _insert(conn, sid, *, title=None, parent=None, started=None, ended=None, end_reason=None):
    started = started if started is not None else time.time()
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, parent_session_id, ended_at, end_reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, title or sid, started, parent, ended, end_reason),
    )


# ---------------------------------------------------------------------------
# _is_continuation_session: bounded overlap tolerance for compression rotations
# ---------------------------------------------------------------------------

def test_compression_overlap_within_bound_is_continuation():
    from api.agent_sessions import _is_continuation_session

    parent = {"source": "webui", "end_reason": "compression", "ended_at": 1000.0}
    child = {"source": "webui", "started_at": 999.0}  # successor started 1s early
    assert _is_continuation_session(parent, child) is True


def test_compression_overlap_beyond_bound_is_not_continuation():
    from api.agent_sessions import _is_continuation_session

    parent = {"source": "webui", "end_reason": "compression", "ended_at": 1000.0}
    child = {"source": "webui", "started_at": 700.0}  # 300s early: unrelated
    assert _is_continuation_session(parent, child) is False


def test_non_compression_end_reason_stays_strict():
    from api.agent_sessions import _is_continuation_session

    parent = {"source": "webui", "end_reason": "cli_close", "ended_at": 1000.0}
    child = {"source": "webui", "started_at": 999.0}
    assert _is_continuation_session(parent, child) is False
    child_ok = {"source": "webui", "started_at": 1000.5}
    assert _is_continuation_session(parent, child_ok) is True


def test_fork_child_is_never_continuation():
    from api.agent_sessions import _is_continuation_session

    parent = {"source": "webui", "end_reason": "compression", "ended_at": 1000.0}
    fork = {"source": "webui", "started_at": 999.0, "session_source": "fork"}
    assert _is_continuation_session(parent, fork) is False


# ---------------------------------------------------------------------------
# read_session_lineage_metadata: overlap chains collapse, self-referenced roots
# ---------------------------------------------------------------------------

def test_overlap_rotation_chain_resolves_single_lineage_root(tmp_path):
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = _make_db(db)
    t0 = time.time() - 200
    # root -> seg1 -> seg2 (tip); each successor starts 0.5s before the
    # predecessor closes, matching real WebUI compression rotations.
    _insert(conn, "root", started=t0, ended=t0 + 10, end_reason="compression")
    _insert(conn, "seg1", parent="root", started=t0 + 9.5, ended=t0 + 20, end_reason="compression")
    _insert(conn, "seg2", parent="seg1", started=t0 + 19.5)
    conn.commit()
    conn.close()

    result = read_session_lineage_metadata(db, ["root", "seg1", "seg2"])

    assert result["seg1"]["_lineage_root_id"] == "root"
    assert result["seg2"]["_lineage_root_id"] == "root"
    assert result["seg2"]["_lineage_tip_id"] == "seg2"
    assert result["seg2"]["_compression_segment_count"] == 3
    # Visible root carries a self-referenced lineage key so the client can
    # collapse the whole chain even with intermediate segments hidden.
    assert result["root"]["_lineage_root_id"] == "root"
    assert result["root"]["_lineage_tip_id"] == "seg2"
    assert result["root"]["_compression_segment_count"] == 3


def test_root_without_continuation_descendants_has_no_self_reference(tmp_path):
    from api.agent_sessions import read_session_lineage_metadata

    db = tmp_path / "state.db"
    conn = _make_db(db)
    t0 = time.time() - 100
    _insert(conn, "plain_root", started=t0)
    _insert(conn, "manual_child", parent="plain_root", started=t0 + 5)
    conn.commit()
    conn.close()

    result = read_session_lineage_metadata(db, ["plain_root", "manual_child"])

    # A plain root with only a non-continuation child must not be marked as a
    # lineage root (nothing to collapse); the child keeps its generic link.
    root_entry = result.get("plain_root", {})
    assert "_lineage_root_id" not in root_entry
    assert result["manual_child"]["relationship_type"] == "child_session"


# ---------------------------------------------------------------------------
# _enrich_sidebar_orphan_file_parent_links: file-only fork parent links
# ---------------------------------------------------------------------------

def test_orphan_fork_reattached_to_visible_lineage_root(tmp_path, monkeypatch):
    from api import models

    db = tmp_path / "state.db"
    conn = _make_db(db)
    t0 = time.time() - 100
    _insert(conn, "root_conv", started=t0, ended=t0 + 10, end_reason="compression")
    _insert(conn, "root_conv_tip", parent="root_conv", started=t0 + 9.5)
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    sessions = [
        # Fork whose parent link exists only in its WebUI session file; the
        # state.db mirror keeps parent_session_id NULL, so the lineage pass
        # returned nothing for it (metadata keyed only by state.db ids).
        {"session_id": "fork1", "title": "root_conv (fork)", "parent_session_id": "root_conv_tip"},
        {"session_id": "root_conv", "title": "root_conv", "_lineage_root_id": "root_conv"},
    ]
    models._enrich_sidebar_orphan_file_parent_links(sessions, {})

    fork = sessions[0]
    assert fork["relationship_type"] == "child_session"
    assert fork["_parent_lineage_root_id"] == "root_conv"
    assert fork["_parent_lineage_tip_id"] == "root_conv_tip"
    # The visible root row is untouched.
    assert "relationship_type" not in sessions[1]


def test_orphan_with_unknown_parent_left_untouched(tmp_path, monkeypatch):
    from api import models

    db = tmp_path / "state.db"
    conn = _make_db(db)
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    sessions = [{"session_id": "fork_x", "title": "x", "parent_session_id": "missing_everywhere"}]
    models._enrich_sidebar_orphan_file_parent_links(sessions, {})

    assert sessions[0] == {"session_id": "fork_x", "title": "x", "parent_session_id": "missing_everywhere"}


def test_session_already_resolved_by_state_db_not_modified(tmp_path, monkeypatch):
    from api import models

    db = tmp_path / "state.db"
    conn = _make_db(db)
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)

    resolved = {
        "session_id": "seg1",
        "title": "s",
        "parent_session_id": "p",
        "relationship_type": "child_session",
        "_parent_lineage_root_id": "p",
    }
    sessions = [dict(resolved)]
    models._enrich_sidebar_orphan_file_parent_links(sessions, {})

    assert sessions[0] == resolved
