"""Regression coverage for pinned Hermes Agent sessions in the WebUI sidebar.

The sidebar uses a bounded recent-session projection for performance. A pinned
agent session is an explicit user discovery signal, so it must remain present
when newer sessions fill that ordinary recency window.
"""

from __future__ import annotations

import sqlite3

from api.agent_sessions import read_importable_agent_session_rows


def _make_state_db(db_path, sessions):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER,
            started_at REAL,
            source TEXT,
            pinned INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp REAL,
            role TEXT
        )
        """
    )
    for sid, title, started_at, pinned in sessions:
        conn.execute(
            """
            INSERT INTO sessions (id, title, model, message_count, started_at, source, pinned)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, title, "gpt-x", 1, started_at, "desktop", pinned),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role) VALUES (?, ?, ?)",
            (sid, started_at, "user"),
        )
    conn.commit()
    conn.close()


def test_pinned_agent_session_survives_the_recent_session_limit(tmp_path):
    """An older pinned Hermes Agent session remains available beside recent rows."""
    db_path = tmp_path / "state.db"
    recent = [
        (f"recent-{i}", f"Recent session {i}", 100.0 + i, 0)
        for i in range(6)
    ]
    pinned = ("older-pinned", "Pinned older session", 1.0, 1)
    _make_state_db(db_path, recent + [pinned])

    rows = read_importable_agent_session_rows(
        db_path,
        limit=3,
        exclude_sources=("webui",),
    )

    by_id = {row["id"]: row for row in rows}
    assert "older-pinned" in by_id
    assert by_id["older-pinned"]["pinned"] is True
