"""Hermes Desktop state.db sessions must appear in Hermex's sidebar."""

import sqlite3
import subprocess
import time
import unittest.mock
from pathlib import Path

import api.models as models
from api.agent_sessions import (
    is_cli_session_row,
    is_cli_session_row_visible,
    normalize_agent_session_source,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_desktop_normalizes_to_interactive_family():
    meta = normalize_agent_session_source("desktop")
    assert meta["session_source"] == "cli"
    assert meta["raw_source"] == "desktop"
    assert meta["source_label"] == "Desktop"


def test_raw_desktop_row_is_interactive():
    assert is_cli_session_row({"id": "desktop-session", "source": "desktop"}) is True


def test_ended_desktop_conversation_with_user_turn_stays_visible():
    row = {
        "id": "desktop-ended",
        "source": "desktop",
        "title": "Desktop Session",
        "message_count": 4,
        "actual_message_count": 4,
        "actual_user_message_count": 1,
        "ended_at": 1_751_000_000.0,
        "end_reason": "client_disconnect",
    }
    normalized = {**row, **normalize_agent_session_source("desktop")}
    assert is_cli_session_row(normalized) is True
    assert is_cli_session_row_visible(normalized) is True


def test_desktop_rows_without_user_turns_stay_hidden():
    for message_count in (0, 3):
        row = {
            "id": f"desktop-no-user-{message_count}",
            "source": "desktop",
            "title": "Desktop Session",
            "message_count": message_count,
            "actual_message_count": message_count,
            "actual_user_message_count": 0,
            "ended_at": 1_751_000_000.0,
            "end_reason": "client_disconnect",
        }
        normalized = {**row, **normalize_agent_session_source("desktop")}
        assert is_cli_session_row(normalized) is True
        assert is_cli_session_row_visible(normalized) is False


def _make_state_db(path: Path) -> None:
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
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    started = time.time()
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count,
         parent_session_id, ended_at, end_reason)
        VALUES (?, 'desktop', NULL, ?, 'openai-codex/gpt-5.6-sol', ?, 3, NULL, NULL, NULL)
        """,
        (
            "20260820_140625_693cd9",
            "Troubleshoot system issues after rough morning",
            started,
        ),
    )
    for index, (role, content) in enumerate(
        (("user", "hello"), ("assistant", "hi"), ("user", "follow-up"))
    ):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                f"desktop-message-{index}",
                "20260820_140625_693cd9",
                role,
                content,
                started + index / 10,
            ),
        )
    conn.commit()
    conn.close()


def test_desktop_session_appears_in_state_db_projection(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    with (
        unittest.mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        unittest.mock.patch("api.models.ensure_cron_project", return_value="cron-project-id"),
    ):
        results = models._load_cli_sessions_uncached(tmp_path, db, None)

    row = next(
        (item for item in results if item["session_id"] == "20260820_140625_693cd9"),
        None,
    )
    assert row is not None, "Desktop session should appear in the sidebar projection"
    assert row["is_cli_session"] is True
    assert row["session_source"] == "cli"
    assert row["source_label"] == "Desktop"


def _make_desktop_stub_db(path: Path, *, include_role: bool) -> None:
    conn = sqlite3.connect(str(path))
    role_column = ", role TEXT" if include_role else ""
    conn.executescript(
        f"""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT{role_column},
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    started = time.time()
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, title, model, started_at, message_count)
        VALUES ('desktop-stub', 'desktop', 'Connection stub', 'test-model', ?, 2)
        """,
        (started,),
    )
    if include_role:
        conn.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, 'desktop-stub', ?, ?, ?)",
            [
                ("stub-assistant", "assistant", "hello", started),
                ("stub-tool", "tool", "result", started + 0.1),
            ],
        )
    else:
        conn.executemany(
            "INSERT INTO messages (id, session_id, content, timestamp) VALUES (?, 'desktop-stub', ?, ?)",
            [
                ("stub-message-1", "assistant-shaped legacy row", started),
                ("stub-message-2", "tool-shaped legacy row", started + 0.1),
            ],
        )
    conn.commit()
    conn.close()


def _load_desktop_stub(tmp_path: Path, *, include_role: bool) -> list[dict]:
    db = tmp_path / "state.db"
    _make_desktop_stub_db(db, include_role=include_role)
    with (
        unittest.mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        unittest.mock.patch("api.models.ensure_cron_project", return_value="cron-project-id"),
    ):
        return models._load_cli_sessions_uncached(tmp_path, db, None)


def test_role_aware_state_db_hides_desktop_stub_without_user_turn(tmp_path):
    results = _load_desktop_stub(tmp_path, include_role=True)
    assert not any(item["session_id"] == "desktop-stub" for item in results)


def test_legacy_state_db_without_message_roles_fails_closed_for_desktop(tmp_path):
    results = _load_desktop_stub(tmp_path, include_role=False)
    assert not any(item["session_id"] == "desktop-stub" for item in results)


def test_legacy_state_db_without_messages_table_fails_closed_for_desktop(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        """
    )
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, title, model, started_at, message_count)
        VALUES ('desktop-stub', 'desktop', 'Connection stub', 'test-model', ?, 2)
        """,
        (time.time(),),
    )
    conn.commit()
    conn.close()

    with (
        unittest.mock.patch("api.models.get_claude_code_sessions", return_value=[]),
        unittest.mock.patch("api.models.ensure_cron_project", return_value="cron-project-id"),
    ):
        results = models._load_cli_sessions_uncached(tmp_path, db, None)

    assert not any(item["session_id"] == "desktop-stub" for item in results)


def test_sessions_js_classifies_raw_desktop_payload_as_interactive():
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.index("function _sourceKeyForSession")
    end = src.index("function _sessionSourceLabel", start)
    block = src[start:end]
    script = f"""
function _isMessagingSession() {{ return false; }}
{block}
if (!_isCliSession({{ source: 'desktop' }})) {{
  throw new Error('raw Desktop session should be interactive');
}}
if (!_isCliSession({{ raw_source: 'desktop' }})) {{
  throw new Error('raw_source Desktop session should be interactive');
}}
if (_isCliSession({{ source: 'telegram', is_cli_session: false }})) {{
  throw new Error('messaging session should not be interactive');
}}
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
