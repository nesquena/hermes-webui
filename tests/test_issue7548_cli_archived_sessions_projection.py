"""Regression test for issue #7548:
CLI-archived cron/webhook/kanban sessions reappear from state.db projection (no sidecar case).
"""

import sqlite3
from pathlib import Path
from api.agent_sessions import read_importable_agent_session_rows
from api.models import get_cli_sessions, _state_projection_sidecar_metadata


def _create_state_db(path: Path, rows: list[tuple]):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER,
            started_at REAL,
            source TEXT,
            session_source TEXT,
            archived INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            timestamp REAL
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, session_source, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            r,
        )
        # insert a dummy user message so actual_message_count > 0
        conn.execute(
            "INSERT INTO messages (session_id, role, timestamp) VALUES (?, 'user', ?)",
            (r[0], r[4]),
        )
    conn.commit()
    conn.close()


def test_cli_archived_session_without_sidecar_retains_archived_state(tmp_path, monkeypatch):
    """When a session is marked archived in state.db and has no WebUI sidecar,
    the projection must preserve archived=True and not reset it to False."""
    db_path = tmp_path / "state.db"
    session_rows = [
        ("cron-active-1", "Cron Active", "model", 1, 1000.0, "cron", "cron", 0),
        ("cron-archived-1", "Cron Archived", "model", 1, 1000.0, "cron", "cron", 1),
    ]
    _create_state_db(db_path, session_rows)

    # Test read_importable_agent_session_rows directly
    projected = read_importable_agent_session_rows(
        db_path,
        limit=200,
        exclude_sources=None,
        include_sources=("cron",),
    )
    by_id = {r["id"]: r for r in projected}
    assert "cron-active-1" in by_id
    assert "cron-archived-1" in by_id
    assert bool(by_id["cron-active-1"].get("archived")) is False
    assert bool(by_id["cron-archived-1"].get("archived")) is True

    # Test _state_projection_sidecar_metadata returns archived: None when no sidecar exists
    meta = _state_projection_sidecar_metadata("cron-archived-1")
    assert meta.get("archived") is None

    # Test get_cli_sessions() projection
    from api.models import clear_cli_sessions_cache
    clear_cli_sessions_cache()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    try:
        import api.profiles  # noqa: F401  # makes the monkeypatch target below importable
        monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: str(tmp_path))
    except Exception:
        pass
    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)
    monkeypatch.setattr("api.models.SESSION_DIR", tmp_path / "webui_sessions")
    (tmp_path / "webui_sessions").mkdir(parents=True, exist_ok=True)

    cli_sessions = get_cli_sessions(source_filter="cron")
    cli_by_id = {s["session_id"]: s for s in cli_sessions}
    assert "cron-active-1" in cli_by_id
    assert "cron-archived-1" in cli_by_id
    assert cli_by_id["cron-active-1"]["archived"] is False
    assert cli_by_id["cron-archived-1"]["archived"] is True
