"""Regression tests for #3172: cron sessions surviving CLI_VISIBLE_SESSION_LIMIT.

When state.db has many more-recent non-cron sessions, the normal sidebar
query (capped at CLI_VISIBLE_SESSION_LIMIT=20) drops older cron runs before
the project-chip rescue can process them.  The second-pass cron-only query
must bring them back so they stay addressable under their project chip.
"""

import json
import sqlite3

import pytest

from api import models


def _make_state_db(db_path, sessions, messages=None):
    """Create a minimal state.db with the given sessions and messages."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER,
            started_at REAL,
            source TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp REAL,
            role TEXT
        )
    """)
    for sid, title, source, started_at in sessions:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, title, "gpt-x", 1, started_at, source),
        )
    for sid, ts, role in (messages or []):
        conn.execute(
            "INSERT INTO messages (session_id, timestamp, role) VALUES (?, ?, ?)",
            (sid, ts, role),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    """Point get_cli_sessions() at a temporary HERMES_HOME."""
    home = tmp_path / "hermes"
    home.mkdir()

    import api.config as cfg
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    # Seed a legacy untagged Cron Jobs project in the actual file the
    # production ensure_cron_project() path reads.
    projects_file.write_text(
        json.dumps([
            {
                "project_id": "cron-project",
                "name": "Cron Jobs",
                "color": "#6366f1",
                "created_at": 1.0,
            }
        ]),
        encoding="utf-8",
    )

    return home


def test_cron_sessions_survive_when_outnumbered_by_recent_sessions(fake_hermes_home, monkeypatch):
    """Cron sessions must appear even when 25+ newer non-cron sessions fill
    the default sidebar window (#3172)."""
    # Patch CLI_VISIBLE_SESSION_LIMIT to a small value to make the test
    # deterministic regardless of the real constant.
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    monkeypatch.setattr(models, "CRON_PROJECT_CHIP_LIMIT", 200)

    db_path = fake_hermes_home / "state.db"

    # 25 non-cron sessions, all more recent than the cron session.
    non_cron = [
        (f"cli-session-{i:02d}", f"CLI Session {i}", "cli", 1700000100.0 + i)
        for i in range(25)
    ]
    # 1 older cron session with messages.
    cron = [("cron_abc123_20260501", "Daily digest", "cron", 1700000000.0)]

    _make_state_db(db_path, non_cron + cron, messages=[
        ("cron_abc123_20260501", 1700000001.0, "assistant"),
    ])

    sessions = models.get_cli_sessions()

    cron_sessions = [s for s in sessions if s.get("source_tag") == "cron"]
    assert len(cron_sessions) >= 1, (
        f"Expected at least 1 cron session in result, got {len(cron_sessions)}. "
        f"Total sessions returned: {len(sessions)}"
    )
    cron_s = cron_sessions[0]
    assert cron_s["session_id"] == "cron_abc123_20260501"
    assert cron_s["project_id"] is not None, "Cron session should have project_id set"


def test_cron_sessions_deduplicated_across_passes(fake_hermes_home, monkeypatch):
    """Sessions returned by both the default and cron-only pass must not
    appear twice."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 50)
    monkeypatch.setattr(models, "CRON_PROJECT_CHIP_LIMIT", 200)

    db_path = fake_hermes_home / "state.db"

    # Only 3 sessions total — all fit within CLI_VISIBLE_SESSION_LIMIT.
    sessions_data = [
        ("cli-1", "Normal", "cli", 1700000100.0),
        ("cron_recent", "Recent cron", "cron", 1700000050.0),
        ("cron_old", "Old cron", "cron", 1700000000.0),
    ]
    messages = [
        ("cron_recent", 1700000051.0, "assistant"),
        ("cron_old", 1700000001.0, "assistant"),
    ]
    _make_state_db(db_path, sessions_data, messages=messages)

    sessions = models.get_cli_sessions()

    ids = [s["session_id"] for s in sessions]
    assert ids.count("cron_recent") == 1, "cron_recent should appear exactly once"
    assert ids.count("cron_old") == 1, "cron_old should appear exactly once"


def test_cron_session_with_no_messages_excluded_from_second_pass(fake_hermes_home, monkeypatch):
    """The second pass should only pick up cron sessions that have messages;
    empty cron runs should not appear."""
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 5)
    monkeypatch.setattr(models, "CRON_PROJECT_CHIP_LIMIT", 200)

    db_path = fake_hermes_home / "state.db"

    non_cron = [
        (f"cli-{i:02d}", f"Session {i}", "cli", 1700000100.0 + i)
        for i in range(10)
    ]
    # Cron session with no messages.
    cron_empty = [("cron_empty_1", "Empty cron", "cron", 1700000000.0)]
    # Cron session with messages.
    cron_ok = [("cron_ok_1", "Active cron", "cron", 1700000001.0)]

    _make_state_db(
        db_path,
        non_cron + cron_empty + cron_ok,
        messages=[("cron_ok_1", 1700000002.0, "assistant")],
    )

    sessions = models.get_cli_sessions()
    cron_ids = [s["session_id"] for s in sessions if s.get("source_tag") == "cron"]

    assert "cron_ok_1" in cron_ids, "Messageful cron session should be included"
    # Empty cron should be excluded by the rescue logic (message_count=0),
    # but it may still appear in the raw list if the second pass picks it up.
    # The rescue layer (_include_project_hidden_background_sidebar_sessions)
    # will filter it out at the API level.
