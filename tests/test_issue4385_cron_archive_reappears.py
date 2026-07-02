"""Regression coverage for archived state.db-projected sessions reappearing."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch


def test_cron_rows_are_not_cli_even_with_stale_cli_flag():
    """A stale sidecar flag must not turn a cron row into an external CLI row."""
    from api.agent_sessions import is_cli_session_row

    row = {
        "session_id": "cron_job123_20260618",
        "title": "Cron Session",
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "is_cli_session": True,
    }

    assert is_cli_session_row(row) is False


def test_materializing_cron_session_preserves_non_cli_identity(monkeypatch):
    """Cron materialization must not stamp the sidecar as CLI-imported."""
    import api.routes as routes

    sid = "cron_job123_20260618"
    cron_meta = {
        "session_id": sid,
        "title": "Cron Session",
        "model": "test-model",
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "read_only": False,
        "profile": "default",
    }

    class FakeSession:
        def __init__(self):
            self.session_id = sid
            self.title = "Cron Session"
            self.profile = "default"
            self.model = "test-model"
            self.archived = False
            self.is_cli_session = False
            self.source_tag = None
            self.raw_source = None
            self.session_source = None
            self.source_label = None
            self.read_only = False

        def save(self, *args, **kwargs):
            pass

    def fake_import_cli_session(*args, **kwargs):
        return FakeSession()

    with (
        patch.object(routes, "get_session", side_effect=KeyError(sid)),
        patch.object(routes, "_lookup_cli_session_metadata", return_value=cron_meta),
        patch.object(
            routes,
            "get_cli_session_messages",
            return_value=[
                {"role": "user", "content": "run"},
                {"role": "assistant", "content": "done"},
            ],
        ),
        patch.object(routes, "import_cli_session", side_effect=fake_import_cli_session),
    ):
        session = routes._get_or_materialize_session(sid)

    assert session.session_source == "cron"
    assert session.source_tag == "cron"
    assert session.is_cli_session is False


def test_cron_state_projection_preserves_archived_sidecar(monkeypatch, tmp_path):
    """A hidden archived sidecar must still mark the state.db cron projection archived."""
    import api.models as models

    sid = "cron_job123_20260618"
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                model TEXT,
                message_count INTEGER,
                started_at REAL,
                source TEXT,
                session_source TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sessions (
                id, title, model, message_count, started_at, source, session_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, "Cron Session", "test-model", 1, 20, "cron", "cron"),
        )

    # Write a real archived sidecar JSON under a patched SESSION_DIR so the
    # projection's sidecar read exercises the production path (the #4842 perf
    # fix stat-gates on SESSION_DIR/{sid}.json before consulting metadata, so a
    # mock of load_metadata_only without a real file would never be reached —
    # which mirrors production, where archived metadata only exists when the
    # file does).
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / f"{sid}.json").write_text(
        '{"session_id": "%s", "title": "Cron Session", "created_at": 1.0,'
        ' "updated_at": 2.0, "archived": true, "messages": []}' % sid,
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "ensure_cron_project", lambda: "cron-project")
    models.clear_sidecar_metadata_cache()

    rows = models._load_cli_sessions_uncached(
        tmp_path,
        db_path,
        "default",
        source_filter="cron",
        include_claude_code=False,
    )

    assert len(rows) == 1
    assert rows[0]["session_id"] == sid
    assert rows[0]["archived"] is True


def test_webhook_state_projection_preserves_archived_sidecar(monkeypatch, tmp_path):
    """Archived webhook sidecars must not reappear as unarchived state.db rows."""
    import api.models as models

    sid = "webhook_archive_20260618"
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                model TEXT,
                message_count INTEGER,
                started_at REAL,
                source TEXT,
                user_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sessions (
                id, title, model, message_count, started_at, source, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                None,
                "test-model",
                2,
                20,
                "webhook",
                "webhook:read-later",
            ),
        )
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp)
            VALUES (?, 'user', 'payload', 21)
            """,
            (sid,),
        )
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp)
            VALUES (?, 'assistant', 'done', 22)
            """,
            (sid,),
        )

    # Real archived sidecar JSON under a patched SESSION_DIR (see the cron test
    # above — the #4842 stat-gate requires the file to exist, matching prod).
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / f"{sid}.json").write_text(
        '{"session_id": "%s", "title": "Webhook Session", "created_at": 1.0,'
        ' "updated_at": 2.0, "archived": true, "messages": []}' % sid,
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.clear_sidecar_metadata_cache()

    rows = models._load_cli_sessions_uncached(
        tmp_path,
        db_path,
        "default",
        include_claude_code=False,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == sid
    assert row["title"] == "Webhook Session"
    assert row["source_tag"] == "webhook"
    assert row["raw_source"] == "webhook"
    assert row["session_source"] == "webhook"
    assert row["source_label"] == "Webhook"
    assert row["project_id"]
    assert row["is_cli_session"] is False
    assert row["archived"] is True


def test_archived_webhook_projection_reaches_sidebar_payload(monkeypatch):
    """Archived webhook projections are counted by default and fetched on demand."""
    import api.routes as routes

    sid = "webhook_archive_20260618"
    raw_webhook_row = {
        "session_id": sid,
        "title": "Webhook Session",
        "profile": "default",
        "updated_at": 22,
        "last_message_at": 22,
        "message_count": 2,
        "user_message_count": 1,
        "archived": True,
        "source_tag": "webhook",
        "raw_source": "webhook",
        "session_source": "webhook",
        "source_label": "Webhook",
        "project_id": "webhook-project",
        "is_cli_session": False,
    }

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [raw_webhook_row])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False)

    default_payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    assert [row for row in default_payload["sessions"] if row["session_id"] == sid] == []
    assert default_payload["archived_count"] == 1
    assert default_payload["archived_webui_count"] == 1

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=True,
    )

    rows = payload["sessions"]
    matching = [row for row in rows if row["session_id"] == sid]
    assert len(matching) == 1
    assert matching[0]["archived"] is True
    assert matching[0]["source_tag"] == "webhook"
    assert matching[0]["default_hidden"] is True
    assert matching[0]["is_cli_session"] is False


def test_archived_cron_sidecar_suppresses_raw_unarchived_cron_row(monkeypatch):
    """Archived cron sidecars stay out of the hot list but win on archive fetch."""
    import api.routes as routes

    sid = "cron_job123_20260618"
    archived_sidecar = {
        "session_id": sid,
        "title": "Cron Session",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 1,
        "user_message_count": 1,
        "archived": True,
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "is_cli_session": True,
    }
    raw_cron_row = {
        "session_id": sid,
        "title": "Cron Session",
        "profile": "default",
        "updated_at": 20,
        "last_message_at": 20,
        "message_count": 1,
        "user_message_count": 1,
        "archived": False,
        "project_id": "cron-project",
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "is_cli_session": False,
    }

    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: [archived_sidecar])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [raw_cron_row])
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False)

    default_payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=True,
    )
    assert [row for row in default_payload["sessions"] if row["session_id"] == sid] == []
    assert default_payload["archived_count"] == 1
    assert default_payload["archived_webui_count"] == 1
    assert default_payload["archived_cli_count"] == 0

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=True,
        include_archived=True,
    )

    rows = payload["sessions"]
    matching = [row for row in rows if row["session_id"] == sid]
    assert len(matching) == 1
    assert matching[0]["archived"] is True
    assert matching[0]["is_cli_session"] is False
