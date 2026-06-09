"""Regression tests for #3887 — defensive index prime for the sidebar scan.

The sidebar's CLI-session scan (``read_importable_agent_session_rows``) orders
candidate sessions by a correlated ``MAX(mx.timestamp)`` subquery over the
``messages`` table. That is fast only when the agent's standard
``idx_messages_session ON messages(session_id, timestamp)`` index exists. A
state.db that lost its migrations (older hermes-agent, or a hand-rebuilt /
reimported db) has no such index and the scan degrades to a full ``messages``
scan per candidate session — stalling ``/api/sessions`` for seconds on every
refresh.

These tests assert the intent (the index is primed when missing so the listing
self-heals) and the cross-cell isolation (the prime is a no-op when the index
already exists, is skipped when the schema lacks the columns, and degrades
silently on a read-only db without ever failing the listing).
"""
import os
import sqlite3
import stat

import pytest

import api.agent_sessions as agent_sessions


def _full_schema_db(path):
    """A state.db with the columns the scan reads, but NO messages index."""
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE sessions(
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT, session_source TEXT,
            parent_session_id TEXT, ended_at REAL, end_reason TEXT,
            user_id TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT,
            session_key TEXT, origin_chat_id TEXT, origin_user_id TEXT,
            platform TEXT)"""
    )
    cur.execute(
        """CREATE TABLE messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL)"""
    )
    for i in range(3):
        sid = f"sess{i}"
        cur.execute(
            "INSERT INTO sessions(id, title, model, message_count, started_at, "
            "source, session_source) VALUES (?,?,?,?,?,?,?)",
            (sid, f"Session {i}", "m", 2, 1000.0 + i, "cli", "cli"),
        )
        cur.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) "
            "VALUES (?,?,?,?)",
            (sid, "user", "hi", 1001.0 + i),
        )
        cur.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) "
            "VALUES (?,?,?,?)",
            (sid, "assistant", "yo", 1002.0 + i),
        )
    conn.commit()
    conn.close()


def _messages_indexes(path):
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='messages'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_prime_creates_missing_index(tmp_path):
    """A db missing idx_messages_session gets it primed on the first scan."""
    db = tmp_path / "state.db"
    _full_schema_db(db)
    assert "idx_messages_session" not in _messages_indexes(db)

    rows = agent_sessions.read_importable_agent_session_rows(
        db, limit=20, exclude_sources=None
    )

    # Listing still returns the sessions ...
    assert {r["id"] for r in rows} == {"sess0", "sess1", "sess2"}
    # ... and the index now exists on (session_id, timestamp).
    assert "idx_messages_session" in _messages_indexes(db)
    conn = sqlite3.connect(str(db))
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_messages_session'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "session_id" in sql and "timestamp" in sql


def test_prime_is_noop_when_index_exists(tmp_path):
    """When the agent already created the index, the prime must not duplicate
    or error — the existing index is reused untouched."""
    db = tmp_path / "state.db"
    _full_schema_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE INDEX idx_messages_session ON messages(session_id, timestamp)"
    )
    conn.commit()
    conn.close()
    before = _messages_indexes(db)

    rows = agent_sessions.read_importable_agent_session_rows(
        db, limit=20, exclude_sources=None
    )

    assert {r["id"] for r in rows} == {"sess0", "sess1", "sess2"}
    # Index set is unchanged — no duplicate, no error.
    assert _messages_indexes(db) == before
    assert "idx_messages_session" in _messages_indexes(db)


def test_prime_skipped_without_timestamp_column(tmp_path):
    """A minimal messages schema (no timestamp column) must not attempt the
    index and must not crash the listing."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE sessions(
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT)"""
    )
    cur.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT)"
    )
    cur.execute(
        "INSERT INTO sessions(id, title, model, message_count, started_at, "
        "source) VALUES ('s1','T','m',1,1000.0,'cli')"
    )
    cur.execute(
        "INSERT INTO messages(id, session_id, role, content) "
        "VALUES (1,'s1','user','hi')"
    )
    conn.commit()
    conn.close()

    rows = agent_sessions.read_importable_agent_session_rows(
        db, limit=20, exclude_sources=None
    )

    # Listing degrades gracefully via the denormalized counts (still surfaces).
    assert {r["id"] for r in rows} == {"s1"}
    # The timestamp-less schema must NOT have an index primed on it.
    assert "idx_messages_session" not in _messages_indexes(db)


def test_prime_degrades_on_readonly_db(tmp_path):
    """A read-only db (can't create the index) must not raise — the listing
    still returns, just without the perf benefit."""
    # Root bypasses POSIX permission bits, so chmod 0444 doesn't make the file
    # read-only for root and the prime would succeed — validating the wrong path
    # and giving false confidence on root-run CI (greptile). Skip under root; the
    # production handler's `except sqlite3.Error: pass` covers read-only/locked/
    # corrupted/older-schema regardless, and the other cases pin that contract.
    if hasattr(os, "getuid") and os.getuid() == 0:
        pytest.skip("chmod-based read-only test is a no-op under root")
    db = tmp_path / "state.db"
    _full_schema_db(db)
    os.chmod(db, stat.S_IREAD)
    try:
        rows = agent_sessions.read_importable_agent_session_rows(
            db, limit=20, exclude_sources=None
        )
        assert {r["id"] for r in rows} == {"sess0", "sess1", "sess2"}
    finally:
        # Restore write so tmp_path cleanup can remove it.
        os.chmod(db, stat.S_IWRITE | stat.S_IREAD)
