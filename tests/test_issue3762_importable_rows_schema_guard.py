"""Regression tests for #3762 — minimal state.db schemas must not hide all sessions.

`read_importable_agent_session_rows()` built its projection SQL with
unconditional references to the `messages` table and `messages.timestamp`
(`MAX(m.timestamp)`, `COUNT(m.id)`, the `LEFT JOIN messages`, and the
`ORDER BY MAX(m.timestamp)`). On a `state.db` that has no `messages` table — or
a `messages` table without a `timestamp` column — that query raised
`sqlite3.OperationalError`, which `get_cli_sessions()` catches and turns into an
empty list, so EVERY imported/CLI/agent session silently vanished from the
sidebar. These tests build minimal schemas directly and assert the rows still
come back instead of the query exploding.
"""
import sqlite3

import pytest

from api.agent_sessions import read_importable_agent_session_rows


def _make_db(path, *, with_messages_table, with_timestamp, with_session_id=True,
             sessions=None, messages=None):
    """Build a minimal state.db.

    sessions: list of (id, title, model, message_count, started_at, source, session_source)
    messages: list of (session_id, role[, timestamp]) — inserted only when the
              messages table is created with the matching columns.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
            started_at REAL, source TEXT, session_source TEXT
        )
        """
    )
    for r in (sessions or []):
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, session_source) "
            "VALUES (?,?,?,?,?,?,?)",
            r,
        )
    if with_messages_table:
        cols = ["id INTEGER PRIMARY KEY"]
        if with_session_id:
            cols.append("session_id TEXT")
        cols.append("role TEXT")
        if with_timestamp:
            cols.append("timestamp REAL")
        conn.execute(f"CREATE TABLE messages ({', '.join(cols)})")
        for m in (messages or []):
            if with_session_id and with_timestamp:
                conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)", m)
            elif with_session_id:
                conn.execute("INSERT INTO messages (session_id, role) VALUES (?,?)", m[:2])
    conn.commit()
    conn.close()


def test_full_schema_returns_rows_baseline(tmp_path):
    db = tmp_path / "state.db"
    _make_db(
        db, with_messages_table=True, with_timestamp=True,
        sessions=[("cli-1", "Hello", "gpt", 3, 1000.0, "cli", "cli")],
        messages=[("cli-1", "user", 1001.0), ("cli-1", "assistant", 1002.0)],
    )
    out = read_importable_agent_session_rows(db, exclude_sources=None)
    assert "cli-1" in {r["id"] for r in out}


def test_messages_table_without_timestamp_does_not_hide_sessions(tmp_path):
    """The exact #3762 repro: messages table exists but has no timestamp column.

    On master this raised sqlite3.OperationalError on MAX(m.timestamp) and the
    whole list collapsed to empty. The COUNT join still works, so the session
    must surface with its real message count and a NULL last_activity.
    """
    db = tmp_path / "state.db"
    _make_db(
        db, with_messages_table=True, with_timestamp=False,
        sessions=[("cli-1", "Hello", "gpt", 2, 1000.0, "cli", "cli")],
        messages=[("cli-1", "user"), ("cli-1", "assistant")],
    )
    out = read_importable_agent_session_rows(db, exclude_sources=None)
    assert {r["id"] for r in out} == {"cli-1"}
    assert out[0].get("last_activity") is None


def test_no_messages_table_does_not_hide_sessions(tmp_path):
    """state.db with a sessions table but NO messages table at all.

    With no join possible we fall back to the denormalized s.message_count, so
    non-empty sessions still surface.
    """
    db = tmp_path / "state.db"
    _make_db(
        db, with_messages_table=False, with_timestamp=False,
        sessions=[
            ("cli-1", "First", "gpt", 2, 1000.0, "cli", "cli"),
            ("cli-2", "Second", "gpt", 7, 2000.0, "cli", "cli"),
        ],
    )
    out = read_importable_agent_session_rows(db, exclude_sources=None)
    assert {r["id"] for r in out} == {"cli-1", "cli-2"}


def test_messages_table_without_session_id_does_not_hide_sessions(tmp_path):
    """A messages table that can't be joined (no session_id) degrades to s.message_count."""
    db = tmp_path / "state.db"
    _make_db(
        db, with_messages_table=True, with_timestamp=True, with_session_id=False,
        sessions=[("cli-1", "Hello", "gpt", 4, 1000.0, "cli", "cli")],
    )
    out = read_importable_agent_session_rows(db, exclude_sources=None)
    assert {r["id"] for r in out} == {"cli-1"}


@pytest.mark.parametrize("with_timestamp", [True, False])
def test_limit_path_also_guarded(tmp_path, with_timestamp):
    """The bounded (limit set) candidate-CTE branch must be column-aware too."""
    db = tmp_path / "state.db"
    sessions = [(f"cli-{i}", f"T{i}", "gpt", 2, 1000.0 + i, "cli", "cli") for i in range(5)]
    messages = []
    for i in range(5):
        if with_timestamp:
            messages += [(f"cli-{i}", "user", 1000.0 + i), (f"cli-{i}", "assistant", 1000.5 + i)]
        else:
            messages += [(f"cli-{i}", "user"), (f"cli-{i}", "assistant")]
    _make_db(db, with_messages_table=True, with_timestamp=with_timestamp,
             sessions=sessions, messages=messages)
    out = read_importable_agent_session_rows(db, limit=3, exclude_sources=None)
    assert len(out) == 3
