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
            pinned INTEGER,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
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
    for session in sessions:
        conn.execute(
            """
            INSERT INTO sessions (
                id, title, model, message_count, started_at, source, pinned,
                parent_session_id, ended_at, end_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["id"], session["title"], "gpt-x", 1,
                session["started_at"], "desktop", session.get("pinned", 0),
                session.get("parent_session_id"), session.get("ended_at"),
                session.get("end_reason"),
            ),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role) VALUES (?, ?, ?)",
            (session["id"], session["last_activity"], "user"),
        )
    conn.commit()
    conn.close()


def _session(session_id, started_at, *, pinned=False, parent_session_id=None,
             ended_at=None, end_reason=None, last_activity=None):
    return {
        "id": session_id,
        "title": session_id,
        "started_at": started_at,
        "pinned": int(pinned),
        "parent_session_id": parent_session_id,
        "ended_at": ended_at,
        "end_reason": end_reason,
        "last_activity": last_activity if last_activity is not None else started_at,
    }


def test_pinned_agent_session_survives_the_recent_session_limit(tmp_path):
    """An older pinned Hermes Agent session remains available beside recent rows."""
    db_path = tmp_path / "state.db"
    recent = [_session(f"recent-{i}", 100.0 + i) for i in range(6)]
    _make_state_db(db_path, recent + [_session("older-pinned", 1.0, pinned=True)])

    rows = read_importable_agent_session_rows(
        db_path,
        limit=3,
        exclude_sources=("webui",),
    )

    by_id = {row["id"]: row for row in rows}
    assert "older-pinned" in by_id
    assert by_id["older-pinned"]["pinned"] is True


def test_pinned_continuation_beyond_candidate_oversample_hydrates_canonical_tip(tmp_path):
    """A pin on an old compressed root must import the newer unpinned tip.

    The 25 newer rows put both continuation segments outside the ordinary
    ``limit * 8`` candidate window. The explicit root pin must hydrate its
    bounded continuation lineage instead of returning a stale root transcript.
    """
    db_path = tmp_path / "state.db"
    filler = [_session(f"recent-{index}", 1_000.0 + index) for index in range(25)]
    root = _session(
        "pinned-root", 1.0, pinned=True, ended_at=2.0, end_reason="compression",
    )
    tip = _session(
        "unpinned-tip", 3.0, parent_session_id="pinned-root", last_activity=4.0,
    )
    _make_state_db(db_path, filler + [root, tip])

    rows = read_importable_agent_session_rows(db_path, limit=3, exclude_sources=("webui",))

    by_id = {row["id"]: row for row in rows}
    assert "pinned-root" not in by_id
    assert by_id["unpinned-tip"]["pinned"] is True
    assert by_id["unpinned-tip"]["_lineage_root_id"] == "pinned-root"
    assert by_id["unpinned-tip"]["_lineage_tip_id"] == "unpinned-tip"


def test_pinned_middle_continuation_marks_the_canonical_tip_pinned(tmp_path):
    """A pin on a middle compression segment applies to its visible lineage."""
    db_path = tmp_path / "state.db"
    root = _session("root", 1.0, ended_at=2.0, end_reason="compression")
    middle = _session(
        "middle",
        3.0,
        pinned=True,
        parent_session_id="root",
        ended_at=4.0,
        end_reason="compression",
    )
    tip = _session("tip", 5.0, parent_session_id="middle", last_activity=6.0)
    _make_state_db(db_path, [root, middle, tip])

    rows = read_importable_agent_session_rows(db_path, limit=3, exclude_sources=("webui",))

    assert [row["id"] for row in rows] == ["tip"]
    assert rows[0]["pinned"] is True


def test_sidebar_cap_retains_agent_pinned_rows_without_exposing_them_as_webui_pins():
    """An old Agent pin survives the route cap but remains internally owned."""
    from api.routes import _cap_recent_cli_sessions

    recent = [
        {"session_id": f"recent-{index}", "is_cli_session": True, "pinned": False}
        for index in range(20)
    ]
    agent_pinned = {
        "session_id": "agent-pinned", "is_cli_session": True,
        "pinned": False, "_agent_pinned": True,
    }
    old_unpinned = {"session_id": "old-unpinned", "is_cli_session": True, "pinned": False}

    capped = _cap_recent_cli_sessions(recent + [agent_pinned, old_unpinned])

    assert [row["session_id"] for row in capped] == [
        *(f"recent-{index}" for index in range(20)), "agent-pinned",
    ]
    assert next(row for row in capped if row["session_id"] == "agent-pinned")["pinned"] is False
