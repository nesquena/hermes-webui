"""Regression tests for /api/sessions lineage metadata used by sidebar collapse."""

import sqlite3
import time

import pytest

import api.models as models
from api.models import SESSIONS, STREAMS, Session, all_sessions


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    SESSIONS.clear()
    STREAMS.clear()
    yield state_db
    SESSIONS.clear()
    STREAMS.clear()


def _ensure_state_db(path):
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
        """
    )
    return conn


def _insert_state_row(conn, sid, *, title=None, parent=None, ended_at=None, end_reason=None, started_at=None, source='webui', session_source=None):
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
        VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 2, ?, ?, ?)
        """,
        (sid, source, session_source, title or sid, started_at or time.time(), parent, ended_at, end_reason),
    )
    conn.commit()


def _save_webui_session(sid, *, title, updated_at):
    session = Session(
        session_id=sid,
        title=title,
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        updated_at=updated_at,
    )
    session.save(touch_updated_at=False)
    return session


def test_all_sessions_exposes_state_db_lineage_metadata_for_webui_json_sessions(_isolate):
    """PR #1358 can only collapse rows when /api/sessions exposes lineage keys."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_root", title="Hermes WebUI", updated_at=t0)
        _save_webui_session("lineage_api_tip", title="Hermes WebUI #2", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_tip",
            parent="lineage_api_root",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        assert rows["lineage_api_tip"].get("parent_session_id") == "lineage_api_root"
        assert rows["lineage_api_tip"].get("_lineage_root_id") == "lineage_api_root"
        assert rows["lineage_api_tip"].get("_compression_segment_count") == 2
        assert "_lineage_root_id" not in rows["lineage_api_root"]
    finally:
        conn.close()


def test_all_sessions_keeps_explicit_forks_out_of_state_db_lineage_metadata(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_root", title="Visible root", updated_at=t0)
        _save_webui_session("lineage_api_fork", title="Explicit fork", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_fork",
            parent="lineage_api_root",
            started_at=t0 + 6,
            session_source="fork",
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        fork = rows["lineage_api_fork"]
        assert fork.get("parent_session_id") == "lineage_api_root"
        assert fork.get("relationship_type") == "child_session"
        assert fork.get("parent_title") == "lineage_api_root"
        assert fork.get("_parent_lineage_root_id") == "lineage_api_root"
        assert "_lineage_root_id" not in fork
        assert "_compression_segment_count" not in fork
    finally:
        conn.close()


def test_non_compression_state_db_parent_does_not_create_sidebar_lineage(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_plain_parent", title="Parent", updated_at=t0)
        _save_webui_session("lineage_api_plain_child", title="Child", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_plain_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="user_stop",
        )
        _insert_state_row(
            conn,
            "lineage_api_plain_child",
            parent="lineage_api_plain_parent",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        # Non-continuation parents should remain visible child-session links,
        # not compression lineage. The frontend must nest them under the parent
        # without collapsing sibling child sessions into one lineage row.
        child = rows["lineage_api_plain_child"]
        assert child.get("parent_session_id") == "lineage_api_plain_parent"
        assert child.get("relationship_type") == "child_session"
        assert child.get("parent_title") == "lineage_api_plain_parent"
        assert child.get("_parent_lineage_root_id") == "lineage_api_plain_parent"
        assert "_lineage_root_id" not in child
    finally:
        conn.close()



def test_child_of_hidden_compression_segment_exposes_parent_lineage_root(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_root", title="Visible root", updated_at=t0)
        _save_webui_session("lineage_api_tip", title="Visible tip", updated_at=t0 + 10)
        _save_webui_session("lineage_api_subtask", title="Subtask", updated_at=t0 + 20)
        _insert_state_row(
            conn,
            "lineage_api_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_tip",
            parent="lineage_api_root",
            started_at=t0 + 6,
            ended_at=t0 + 15,
            end_reason="user_stop",
        )
        _insert_state_row(
            conn,
            "lineage_api_subtask",
            parent="lineage_api_tip",
            started_at=t0 + 12,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        child = rows["lineage_api_subtask"]
        assert child.get("relationship_type") == "child_session"
        assert child.get("parent_session_id") == "lineage_api_tip"
        assert child.get("_parent_lineage_root_id") == "lineage_api_root"
        assert "_lineage_root_id" not in child
    finally:
        conn.close()



def test_cli_close_parent_preserves_cross_surface_continuation_lineage(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_cli_parent", title="Hermes WebUI #8", updated_at=t0)
        _save_webui_session("lineage_api_webui_child", title="Hermes WebUI #8", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_cli_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="cli_close",
        )
        _insert_state_row(
            conn,
            "lineage_api_webui_child",
            parent="lineage_api_cli_parent",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        assert rows["lineage_api_webui_child"].get("parent_session_id") == "lineage_api_cli_parent"
        assert rows["lineage_api_webui_child"].get("_lineage_root_id") == "lineage_api_cli_parent"
    finally:
        conn.close()


def test_cross_surface_child_session_metadata_marks_orphan_top_level_candidate(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_telegram_parent", title="Telegram parent", updated_at=t0)
        _save_webui_session("lineage_api_webui_tip", title="WebUI tip", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_telegram_parent",
            source="telegram",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_webui_tip",
            source="webui",
            parent="lineage_api_telegram_parent",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}
        tip = rows["lineage_api_webui_tip"]

        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_source") == "telegram"
        assert tip.get("_cross_surface_child_session") is True
    finally:
        conn.close()


def test_generic_webui_title_gets_read_only_state_db_display_title(_isolate):
    """Sidebar rows can display the fresher state.db title without mutating JSON."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_stale_title", title="Hermes WebUI #8", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_stale_title",
            title="Hermes WebUI #177",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions()}["lineage_api_stale_title"]

        assert row["title"] == "Hermes WebUI #8"
        assert row["display_title"] == "Hermes WebUI #177"
        assert row["_state_db_title"] == "Hermes WebUI #177"
    finally:
        conn.close()


def test_state_db_display_title_does_not_override_custom_json_title(_isolate):
    """Manual/custom JSON titles stay authoritative even when state.db differs."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_custom_title", title="Customer escalation notes", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_custom_title",
            title="Hermes WebUI #177",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions()}["lineage_api_custom_title"]

        assert row["title"] == "Customer escalation notes"
        assert "display_title" not in row
        assert "_state_db_title" not in row
    finally:
        conn.close()
