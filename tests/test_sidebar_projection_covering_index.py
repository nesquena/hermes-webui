"""Regression coverage for the sidebar projection's missing covering index.

``read_importable_agent_session_rows()`` aggregates ``m.role`` in the sidebar
projection::

    COUNT(CASE WHEN LOWER(m.role) = 'user' THEN 1 END)

alongside ``MAX(m.timestamp)``. Hermes Agent ships ``idx_messages_session
(session_id, timestamp)`` and ``idx_messages_session_id (session_id, id)``, and
NEITHER contains ``role``. SQLite therefore drove the LEFT JOIN through
``idx_messages_session_id`` and fetched every matching ``messages`` row from the
table to read ``role``/``timestamp``, so the join scaled with total message
volume instead of the bounded candidate window.

Measured on a 1.4 GB state.db (1327 sessions / 175789 messages) the projection
took 6.0-8.7s, of which the role aggregate was ~95%: dropping only that
expression took the same query to 0.49s. Priming
``idx_messages_session_ts_role (session_id, timestamp, role)`` makes the join a
COVERING INDEX scan (6.0s -> 0.35s; 443ms -> 23ms on a 970 MB db).

These tests pin the fix: the index is primed on a writable db, the projection
plans as a covering-index scan, results are unchanged by its presence, and a
read-only db still degrades gracefully instead of raising.
"""

import pathlib
import sqlite3

from api.agent_sessions import read_importable_agent_session_rows
from tests._sqlite_helpers import writes_blocked

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

COVERING_INDEX = "idx_messages_session_ts_role"


def _make_state_db(path, *, sessions=6, messages_per_session=25):
    """A state.db shaped like Hermes Agent's, with the agent's own indexes."""
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
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        -- The two indexes Hermes Agent actually ships. Neither covers `role`.
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        CREATE INDEX idx_messages_session_id ON messages(session_id, id);
        """
    )
    now = 1_787_000_000.0
    for s in range(sessions):
        sid = f"2026082{s}_120000_aaaa{s:02d}"
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count)"
            " VALUES (?,?,?,?,?,?)",
            (sid, "cli", f"Session {s}", "claude-sonnet-5", now + s,
             messages_per_session),
        )
        for m in range(messages_per_session):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp)"
                " VALUES (?,?,?,?)",
                (sid, "user" if m % 2 == 0 else "assistant", f"m{m}",
                 now + s + m),
            )
    conn.commit()
    conn.close()


def _index_names(path):
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute("PRAGMA index_list(messages)")}
    finally:
        conn.close()


def test_covering_index_is_primed_on_writable_db(tmp_path):
    """The projection self-heals the covering index, like the #3887 prime."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    assert COVERING_INDEX not in _index_names(db)

    read_importable_agent_session_rows(db)

    assert COVERING_INDEX in _index_names(db), (
        "sidebar projection should prime the (session_id, timestamp, role) "
        "covering index so the role aggregate stops fetching table rows"
    )


def test_projection_plans_as_covering_index_scan(tmp_path):
    """The messages join must not fall back to a table fetch for `role`."""
    db = tmp_path / "state.db"
    _make_state_db(db)
    read_importable_agent_session_rows(db)  # prime

    conn = sqlite3.connect(str(db))
    try:
        plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT s.id, COUNT(m.id),
                   COUNT(CASE WHEN LOWER(m.role) = 'user' THEN 1 END),
                   MAX(m.timestamp)
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            """
        ).fetchall()
    finally:
        conn.close()

    detail = " | ".join(str(row[-1]) for row in plan)
    assert COVERING_INDEX in detail, f"expected covering index in plan: {detail}"
    assert "COVERING INDEX" in detail.upper(), (
        f"role aggregate still fetches table rows: {detail}"
    )


def test_index_presence_does_not_change_rows(tmp_path):
    """An index changes the plan, never the data."""
    without = tmp_path / "without.db"
    with_idx = tmp_path / "with.db"
    _make_state_db(without)
    _make_state_db(with_idx)

    conn = sqlite3.connect(str(with_idx))
    conn.execute(
        f"CREATE INDEX {COVERING_INDEX} ON messages(session_id, timestamp, role)"
    )
    conn.commit()
    conn.close()

    # The baseline must stay genuinely unindexed: without the write block its
    # own projection call primes the covering index first, and the comparison
    # degenerates into indexed-vs-indexed.
    with writes_blocked():
        baseline = read_importable_agent_session_rows(without)
    assert COVERING_INDEX not in _index_names(without), (
        "baseline fixture must remain unindexed for the comparison to mean "
        "anything"
    )

    primed = read_importable_agent_session_rows(with_idx)
    assert COVERING_INDEX in _index_names(with_idx)

    assert baseline == primed, "covering index must not alter projected rows"
    assert baseline, "fixture should project at least one row"
    for row in baseline:
        assert row["actual_user_message_count"] > 0, (
            "role aggregate should still count user messages"
        )


def test_read_only_db_degrades_gracefully(tmp_path):
    """A failed prime keeps the old plan instead of raising, on every CI user."""
    db = tmp_path / "state.db"
    reference = tmp_path / "reference.db"
    _make_state_db(db)
    _make_state_db(reference)

    expected = read_importable_agent_session_rows(reference)
    assert COVERING_INDEX in _index_names(reference), (
        "writable control must prime the index"
    )

    with writes_blocked():
        rows = read_importable_agent_session_rows(db)

    assert rows, "projection must still return rows when the prime fails"
    assert COVERING_INDEX not in _index_names(db), (
        "the prime must not have landed — otherwise the caught sqlite3.Error "
        "branch was never exercised"
    )
    assert rows == expected, (
        "the unindexed plan must project the same rows as the indexed one"
    )
    for row in rows:
        assert row["actual_user_message_count"] > 0
