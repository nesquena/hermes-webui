import io
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import api.agent_sessions as agent_sessions
import api.models as models
import api.profiles as profiles
import api.routes as routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class _RecordingCursor:
    def __init__(self, cursor, executed):
        self._cursor = cursor
        self._executed = executed

    def execute(self, sql, params=()):
        self._executed.append((sql, tuple(params)))
        return self._cursor.execute(sql, params)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RecordingConnection:
    def __init__(self, connection, executed):
        self._connection = connection
        self._executed = executed

    def cursor(self):
        return _RecordingCursor(self._connection.cursor(), self._executed)

    def close(self):
        return self._connection.close()

    def commit(self):
        return self._connection.commit()

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    def __getattr__(self, name):
        return getattr(self._connection, name)


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
    rows = [
        ("tui_session", "tui", "tui", "TUI Session", 10.0),
        ("cron_session", "cron", "cron", "Cron Session", 20.0),
        ("webui_session", "webui", "webui", "WebUI Session", 30.0),
    ]
    for sid, source, session_source, title, started_at in rows:
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, session_source, title, model, started_at, message_count,
             parent_session_id, ended_at, end_reason)
            VALUES (?, ?, ?, ?, 'test-model', ?, 1, NULL, NULL, NULL)
            """,
            (sid, source, session_source, title, started_at),
        )
        conn.execute(
            """
            INSERT INTO messages (id, session_id, role, content, timestamp)
            VALUES (?, ?, 'user', 'message', ?)
            """,
            (f"{sid}_msg", sid, started_at),
        )
    conn.commit()
    conn.close()


def test_read_importable_agent_session_rows_uses_parameterized_include_filter(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db)
    executed = []
    real_connect = agent_sessions.sqlite3.connect

    def recording_connect(*args, **kwargs):
        return _RecordingConnection(real_connect(*args, **kwargs), executed)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", recording_connect)

    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        limit=None,
        exclude_sources=None,
        include_sources=("tui", "cron"),
    )

    assert {row["id"] for row in rows} == {"tui_session", "cron_session"}
    select_calls = [
        (sql, params)
        for sql, params in executed
        if "FROM sessions s" in sql and "s.source IN (?, ?)" in sql
    ]
    assert select_calls, "Expected the projection SQL to use a parameterized IN clause"
    assert select_calls[-1][1][:2] == ("tui", "cron")


def test_get_cli_sessions_source_filter_uses_distinct_cache_key(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(models, "_CLI_SESSIONS_CACHE_TTL_SECONDS", 60.0, raising=False)
    models.clear_cli_sessions_cache()

    seen = []

    def fake_loader(_hermes_home, _db_path, _cli_profile, source_filter=None):
        seen.append(source_filter)
        return [{"session_id": f"session-{source_filter or 'all'}", "title": "cached"}]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", fake_loader)

    first = models.get_cli_sessions()
    filtered = models.get_cli_sessions(source_filter="tui")
    filtered_again = models.get_cli_sessions(source_filter="tui")

    assert seen == [None, "tui"]
    assert first[0]["session_id"] == "session-all"
    assert filtered[0]["session_id"] == "session-tui"
    assert filtered_again[0]["session_id"] == "session-tui"


def test_load_cli_sessions_uncached_pushes_specific_source_into_state_db_scan(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")
    calls = []
    claude_calls = []

    def fake_read_rows(_db_path, **kwargs):
        calls.append(kwargs)
        return [
            {
                "id": "tui_session",
                "title": "TUI Session",
                "model": "test-model",
                "source": "tui",
                "raw_source": "tui",
                "message_count": 2,
                "actual_message_count": 2,
                "actual_user_message_count": 1,
                "last_activity": 10.0,
                "started_at": 9.0,
            }
        ]

    monkeypatch.setattr(models, "get_claude_code_sessions", lambda: claude_calls.append(True) or [])
    monkeypatch.setattr(models, "read_importable_agent_session_rows", fake_read_rows)
    monkeypatch.setattr(models, "get_last_workspace", lambda: tmp_path)
    monkeypatch.setattr(models, "ensure_cron_project", lambda: "cron-project-id")
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    result = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None, source_filter="tui")

    assert claude_calls == []
    assert calls == [
        {
            "limit": models.CLI_VISIBLE_SESSION_LIMIT,
            "log": models.logger,
            "exclude_sources": None,
            "include_sources": ("tui",),
        }
    ]
    assert [row["source_tag"] for row in result] == ["tui"]


def test_cron_source_filter_uses_cron_rescue_limit(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")
    calls = []

    def fake_read_rows(_db_path, **kwargs):
        calls.append(kwargs)
        return [
            {
                "id": "cron_session",
                "title": "Cron Session",
                "model": "test-model",
                "source": "cron",
                "raw_source": "cron",
                "message_count": 1,
                "actual_message_count": 1,
                "actual_user_message_count": 1,
                "last_activity": 10.0,
                "started_at": 9.0,
            }
        ]

    monkeypatch.setattr(models, "read_importable_agent_session_rows", fake_read_rows)
    monkeypatch.setattr(models, "get_last_workspace", lambda: tmp_path)
    monkeypatch.setattr(models, "ensure_cron_project", lambda: "cron-project-id")
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    result = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None, source_filter="cron")

    assert calls == [
        {
            "limit": models.CRON_PROJECT_CHIP_LIMIT,
            "log": models.logger,
            "exclude_sources": None,
            "include_sources": ("cron",),
        }
    ]
    assert [row["source_tag"] for row in result] == ["cron"]


def test_api_sessions_passes_source_filter_only_on_sidebar_path(monkeypatch):
    captured = []

    def fake_get_cli_sessions(source_filter=None):
        captured.append(source_filter)
        return []

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(
        routes,
        "load_settings",
        lambda: {
            "show_cli_sessions": True,
            "agent_session_source_filter": "  TUI  ",
        },
    )
    monkeypatch.setattr(routes, "get_cli_sessions", fake_get_cli_sessions)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse("http://example.com/api/sessions"))

    assert handler.status == 200
    assert handler.json_body()["sessions"] == []
    assert captured == ["  TUI  "]


def test_non_sidebar_cli_session_callers_keep_default_get_cli_sessions_signature(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda: [{"session_id": "cli-session", "title": "CLI Session"}],
    )

    assert routes._lookup_cli_session_metadata("cli-session") == {
        "session_id": "cli-session",
        "title": "CLI Session",
    }
