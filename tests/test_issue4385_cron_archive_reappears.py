"""Regression coverage for #4385: archived cron sessions reappearing."""

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

    class ArchivedSidecar:
        title = "Cron Session"
        archived = True

    monkeypatch.setattr(
        models.Session,
        "load_metadata_only",
        staticmethod(lambda candidate: ArchivedSidecar() if candidate == sid else None),
    )
    monkeypatch.setattr(models, "ensure_cron_project", lambda: "cron-project")

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


def test_archived_cron_sidecar_suppresses_raw_unarchived_cron_row(monkeypatch):
    """An archived cron sidecar should keep the raw state.db cron row hidden."""
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

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=True,
    )

    rows = payload["sessions"]
    matching = [row for row in rows if row["session_id"] == sid]
    assert len(matching) == 1
    assert matching[0]["archived"] is True
    assert matching[0]["is_cli_session"] is False
