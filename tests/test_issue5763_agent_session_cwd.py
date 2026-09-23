"""Focused state.db imported-session cwd projection coverage for issue #5763."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from api.agent_sessions import read_importable_agent_session_rows


def _create_state_db(path: Path, *, with_cwd: bool) -> sqlite3.Connection:
    cwd_column = ", cwd TEXT" if with_cwd else ""
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
            {cwd_column}
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    return conn


def _insert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    started_at: float,
    cwd: str | None = None,
    parent_session_id: str | None = None,
    ended_at: float | None = None,
    end_reason: str | None = None,
    message_at: float | None = None,
) -> None:
    columns = [
        "id",
        "source",
        "title",
        "model",
        "message_count",
        "started_at",
        "parent_session_id",
        "ended_at",
        "end_reason",
    ]
    values: list[object] = [
        session_id,
        "tui",
        session_id,
        "test-model",
        1,
        started_at,
        parent_session_id,
        ended_at,
        end_reason,
    ]
    if "cwd" in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}:
        columns.append("cwd")
        values.append(cwd)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO sessions ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, timestamp) VALUES (?, 'user', ?)",
        (session_id, message_at if message_at is not None else started_at + 1),
    )


def test_standalone_imported_session_preserves_persisted_cwd(tmp_path):
    db_path = tmp_path / "state.db"
    with _create_state_db(db_path, with_cwd=True) as conn:
        _insert_session(
            conn,
            session_id="standalone",
            started_at=100,
            cwd="/tmp/../persisted-project",
        )

    rows = read_importable_agent_session_rows(db_path, limit=None)

    assert len(rows) == 1
    assert "cwd" in rows[0]
    assert rows[0]["cwd"] == "/tmp/../persisted-project"


def test_older_schema_without_cwd_projects_none(tmp_path):
    db_path = tmp_path / "state.db"
    with _create_state_db(db_path, with_cwd=False) as conn:
        _insert_session(conn, session_id="legacy", started_at=100)

    rows = read_importable_agent_session_rows(db_path, limit=None)

    assert len(rows) == 1
    assert "cwd" in rows[0]
    assert rows[0]["cwd"] is None


def test_compression_chain_projects_selected_tip_cwd(tmp_path):
    db_path = tmp_path / "state.db"
    with _create_state_db(db_path, with_cwd=True) as conn:
        _insert_session(
            conn,
            session_id="head",
            started_at=100,
            ended_at=150,
            end_reason="compression",
            cwd="/old",
        )
        _insert_session(
            conn,
            session_id="tip",
            started_at=160,
            parent_session_id="head",
            cwd="/new",
        )

    rows = read_importable_agent_session_rows(db_path, limit=None)

    assert len(rows) == 1
    assert rows[0]["id"] == "tip"
    assert rows[0]["cwd"] == "/new"


def test_selected_tip_null_cwd_overwrites_stale_head_cwd(tmp_path):
    db_path = tmp_path / "state.db"
    with _create_state_db(db_path, with_cwd=True) as conn:
        _insert_session(
            conn,
            session_id="head",
            started_at=100,
            ended_at=150,
            end_reason="cli_close",
            cwd="/old",
        )
        _insert_session(
            conn,
            session_id="tip",
            started_at=160,
            parent_session_id="head",
            cwd=None,
        )

    rows = read_importable_agent_session_rows(db_path, limit=None)

    assert len(rows) == 1
    assert rows[0]["id"] == "tip"
    assert rows[0]["cwd"] is None


def test_branching_chain_projects_winning_deep_tip_cwd(tmp_path):
    db_path = tmp_path / "state.db"
    with _create_state_db(db_path, with_cwd=True) as conn:
        _insert_session(
            conn,
            session_id="head",
            started_at=100,
            ended_at=150,
            end_reason="compression",
            cwd="/head",
        )
        _insert_session(
            conn,
            session_id="deep-mid",
            started_at=160,
            parent_session_id="head",
            ended_at=170,
            end_reason="compression",
            cwd="/deep-mid",
        )
        _insert_session(
            conn,
            session_id="deep-tip",
            started_at=180,
            parent_session_id="deep-mid",
            cwd="/deep-tip",
            message_at=300,
        )
        _insert_session(
            conn,
            session_id="newer-direct-child",
            started_at=250,
            parent_session_id="head",
            cwd="/direct",
            message_at=251,
        )

    rows = read_importable_agent_session_rows(db_path, limit=None)

    assert len(rows) == 1
    assert rows[0]["id"] == "deep-tip"
    assert rows[0]["cwd"] == "/deep-tip"
