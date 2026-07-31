"""Regression: project-assigned CLI sessions must survive the recent-session cap."""

import sqlite3

import pytest

from api import models, routes


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
            project_id TEXT
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
    for sid, started_at, project_id in sessions:
        conn.execute(
            "INSERT INTO sessions "
            "(id, title, model, message_count, started_at, source, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, sid, "gpt-x", 1, started_at, "cli", project_id),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role) VALUES (?, ?, ?)",
            (sid, started_at + 0.5, "user"),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()

    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)
    models.clear_cli_sessions_cache()
    yield home
    models.clear_cli_sessions_cache()


def test_project_assigned_cli_session_survives_recent_session_limit(
    fake_hermes_home, monkeypatch
):
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)

    newer_unassigned = [
        (f"recent-{index:02d}", 1700000100.0 + index, None)
        for index in range(25)
    ]
    older_assigned = [("older-assigned", 1700000000.0, "project-123")]
    _make_state_db(fake_hermes_home / "state.db", newer_unassigned + older_assigned)

    sessions = models.get_cli_sessions()
    by_id = {session["session_id"]: session for session in sessions}

    assert "older-assigned" in by_id
    assert by_id["older-assigned"]["project_id"] == "project-123"
    assert [session["session_id"] for session in sessions].count("older-assigned") == 1


@pytest.mark.parametrize(
    ("root_project_id", "tip_project_id"),
    [
        ("project-123", None),
        (None, "project-123"),
    ],
)
def test_project_assigned_compression_lineage_is_returned_once(
    fake_hermes_home,
    monkeypatch,
    root_project_id,
    tip_project_id,
):
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    db_path = fake_hermes_home / "state.db"
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
            project_id TEXT,
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
    rows = [
        (
            "lineage-root",
            1700000000.0,
            root_project_id,
            None,
            1700000001.0,
            "compression",
        ),
        (
            "lineage-tip",
            1700000200.0,
            tip_project_id,
            "lineage-root",
            None,
            None,
        ),
    ]
    # Keep the tip inside the normal five-row window while pushing the root
    # outside its 8x SQL candidate window. This reproduces the real two-pass
    # boundary: the normal pass sees only the tip and the project pass must
    # bring enough lineage context to avoid appending the root separately.
    rows.extend(
        (
            f"newer-{index}",
            1700000300.0 + index,
            None,
            None,
            None,
            None,
        )
        for index in range(4)
    )
    rows.extend(
        (
            f"middle-{index}",
            1700000100.0 + index,
            None,
            None,
            None,
            None,
        )
        for index in range(56)
    )
    for sid, started_at, project_id, parent_id, ended_at, end_reason in rows:
        conn.execute(
            "INSERT INTO sessions "
            "(id, title, model, message_count, started_at, source, project_id, "
            "parent_session_id, ended_at, end_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                sid,
                "gpt-x",
                1,
                started_at,
                "cli",
                project_id,
                parent_id,
                ended_at,
                end_reason,
            ),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role) VALUES (?, ?, ?)",
            (sid, started_at + 0.5, "user"),
        )
    conn.commit()
    conn.close()

    sessions = models.get_cli_sessions()
    lineage_rows = [
        session
        for session in sessions
        if session["session_id"] in {"lineage-root", "lineage-tip"}
    ]

    assert len(lineage_rows) == 1
    assert lineage_rows[0]["session_id"] in {"lineage-root", "lineage-tip"}
    assert lineage_rows[0]["project_id"] == "project-123"


def test_route_cap_preserves_project_assigned_overflow_as_hidden():
    recent = [
        {
            "session_id": f"recent-{index}",
            "is_cli_session": True,
            "project_id": None,
        }
        for index in range(20)
    ]
    assigned_overflow = {
        "session_id": "assigned-overflow",
        "is_cli_session": True,
        "project_id": "project-prime",
    }
    unassigned_overflow = {
        "session_id": "unassigned-overflow",
        "is_cli_session": True,
        "project_id": None,
    }

    kept = routes._cap_recent_cli_sessions(
        recent + [assigned_overflow, unassigned_overflow],
        cli_cap=20,
    )

    by_id = {row["session_id"]: row for row in kept}
    assert len(kept) == 21
    assert "unassigned-overflow" not in by_id
    assert by_id["assigned-overflow"]["default_hidden"] is True
    assert "default_hidden" not in assigned_overflow
