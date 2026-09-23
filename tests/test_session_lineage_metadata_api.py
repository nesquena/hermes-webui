"""Regression tests for /api/sessions lineage metadata used by sidebar collapse."""

import json
import sqlite3
import time

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS, STREAMS, Session, all_sessions


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    state_db = tmp_path / "state.db"
    index_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "_start_session_index_rebuild_thread", lambda: None)

    def uncached_persisted_session_ids():
        return frozenset(
            p.stem
            for p in models.SESSION_DIR.glob("*.json")
            if not p.name.startswith("_")
        )

    monkeypatch.setattr(models, "_persisted_session_ids_snapshot", uncached_persisted_session_ids)
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
            end_reason TEXT,
            model_config TEXT
        );
        """
    )
    return conn


def _ensure_messages_table(conn):
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        )
        """
    )


def _insert_state_message(conn, sid, *, role, content, timestamp):
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (sid, role, content, timestamp),
    )
    conn.commit()


def _insert_state_row(conn, sid, *, title=None, parent=None, ended_at=None, end_reason=None, started_at=None, source='webui', session_source=None, model_config=None):
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason, model_config)
        VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 2, ?, ?, ?, ?)
        """,
        (sid, source, session_source, title or sid, started_at or time.time(), parent, ended_at, end_reason, model_config),
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
        assert child.get("_parent_lineage_tip_id") == "lineage_api_tip"
        serialized = routes._sidebar_session_response_item(child, redact_enabled=False)
        assert serialized.get("_parent_lineage_tip_id") == "lineage_api_tip"
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


def test_state_db_webui_source_overrides_stale_cli_json_metadata(_isolate):
    """State-db WebUI mirrors should clear stale CLI source fields in sidebar rows."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        session = Session(
            session_id="lineage_api_stale_cli_source",
            title="WebUI Chatnachrichten verschwinden nach Neustart #9",
            messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
            updated_at=t0,
            is_cli_session=True,
            source_tag="cli",
            raw_source="cli",
            session_source="cli",
            source_label="CLI",
        )
        session.save(touch_updated_at=False)
        _insert_state_row(
            conn,
            "lineage_api_stale_cli_source",
            source="webui",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions()}["lineage_api_stale_cli_source"]

        assert row["source_tag"] == "webui"
        assert row["raw_source"] == "webui"
        assert row["session_source"] == "webui"
        assert row["source_label"] == "WebUI"
        assert row["is_cli_session"] is False
    finally:
        conn.close()


def test_sessions_route_keeps_state_db_webui_row_with_stale_cli_json_when_cli_hidden(_isolate, monkeypatch):
    """The hot route must apply state.db source correction before CLI filtering."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        session = Session(
            session_id="lineage_api_route_stale_cli_source",
            title="WebUI Chatnachrichten verschwinden nach Neustart #9",
            messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
            updated_at=t0,
            is_cli_session=True,
            source_tag="cli",
            raw_source="cli",
            session_source="cli",
            source_label="CLI",
        )
        session.save(touch_updated_at=False)
        _insert_state_row(
            conn,
            "lineage_api_route_stale_cli_source",
            source="webui",
            started_at=t0,
        )

        monkeypatch.setattr(routes, "all_sessions", models.all_sessions)
        monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", models._enrich_sidebar_lineage_metadata)
        monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False)

        payload = routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            include_archived=False,
        )

        rows = {row["session_id"]: row for row in payload["sessions"]}
        row = rows["lineage_api_route_stale_cli_source"]
        assert row["source_tag"] == "webui"
        assert row["raw_source"] == "webui"
        assert row["session_source"] == "webui"
        assert row["source_label"] == "WebUI"
        assert row["is_cli_session"] is False
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


def test_generic_subagent_title_gets_goal_display_title(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_goal", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_goal",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_goal",
            role="user",
            content="Find the root cause of the failing sidebar test",
            timestamp=t0 + 1,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_goal"]

        assert row["title"] == "Subagent Session"
        assert row["display_title"] == "Find the root cause of the failing sidebar test"
    finally:
        conn.close()


def test_custom_subagent_title_stays_authoritative(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_custom", title="Investigate auth", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_custom",
            title="Investigate auth",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_custom",
            role="user",
            content="A different goal",
            timestamp=t0 + 1,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_custom"]

        assert row["title"] == "Investigate auth"
        assert "display_title" not in row
    finally:
        conn.close()


def test_generic_subagent_title_falls_back_without_first_user_message(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_empty", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_empty",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_empty",
            role="assistant",
            content="Only assistant output",
            timestamp=t0 + 1,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_empty"]

        assert row["title"] == "Subagent Session"
        assert "display_title" not in row
    finally:
        conn.close()


def test_generic_subagent_title_skips_null_first_user_message(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_null_first", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_null_first",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_null_first",
            role="user",
            content=None,
            timestamp=t0 + 1,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_null_first",
            role="user",
            content="Recover the next usable delegated title",
            timestamp=t0 + 2,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_null_first"]

        assert row["title"] == "Subagent Session"
        assert row["display_title"] == "Recover the next usable delegated title"
    finally:
        conn.close()


def test_generic_subagent_title_respects_sidebar_override_cap(_isolate, monkeypatch):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    older = time.time() - 200
    newer = time.time() - 100
    try:
        monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "1")
        _save_webui_session("lineage_api_subagent_old", title="Subagent Session", updated_at=older)
        _save_webui_session("lineage_api_subagent_new", title="Subagent Session", updated_at=newer)
        _insert_state_row(
            conn,
            "lineage_api_subagent_old",
            title="Subagent Session",
            source="subagent",
            started_at=older,
        )
        _insert_state_row(
            conn,
            "lineage_api_subagent_new",
            title="Subagent Session",
            source="subagent",
            started_at=newer,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_old",
            role="user",
            content="Older delegated title",
            timestamp=older + 1,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_new",
            role="user",
            content="Newest delegated title",
            timestamp=newer + 1,
        )

        rows = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}

        assert rows["lineage_api_subagent_new"]["display_title"] == "Newest delegated title"
        assert "display_title" not in rows["lineage_api_subagent_old"]
    finally:
        conn.close()
def test_generic_subagent_title_falls_back_without_messages_table(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_no_messages", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_no_messages",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_no_messages"]

        assert row["title"] == "Subagent Session"
        assert "display_title" not in row
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


def test_sessions_route_preserves_visible_child_lineage_when_archived_parent_filtered(_isolate, monkeypatch):
    """Default /api/sessions omits archived rows but keeps their lineage metadata.

    The route builds the hot sidebar payload with archived rows filtered out by
    default. A visible continuation child still needs lineage metadata from its
    archived parent so the client can collapse/display the logical conversation
    correctly.
    """
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        archived_parent = _save_webui_session(
            "lineage_api_archived_parent",
            title="Hermes WebUI",
            updated_at=t0,
        )
        archived_parent.archived = True
        archived_parent.save(touch_updated_at=False)
        _save_webui_session(
            "lineage_api_visible_tip",
            title="Hermes WebUI #2",
            updated_at=t0 + 10,
        )
        _insert_state_row(
            conn,
            "lineage_api_archived_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_visible_tip",
            parent="lineage_api_archived_parent",
            started_at=t0 + 6,
        )

        monkeypatch.setattr(routes, "all_sessions", models.all_sessions)
        monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", models._enrich_sidebar_lineage_metadata)
        monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False)

        default_payload = routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            include_archived=False,
        )

        assert [row["session_id"] for row in default_payload["sessions"]] == ["lineage_api_visible_tip"]
        assert default_payload["archived_count"] == 1
        tip = default_payload["sessions"][0]
        assert tip.get("parent_session_id") == "lineage_api_archived_parent"
        assert tip.get("_lineage_root_id") == "lineage_api_archived_parent"
        assert tip.get("_compression_segment_count") == 2

        archived_payload = routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            include_archived=True,
        )
        assert [row["session_id"] for row in archived_payload["sessions"]] == [
            "lineage_api_visible_tip",
            "lineage_api_archived_parent",
        ]
    finally:
        conn.close()


def test_compression_continuation_tolerates_sub_second_timestamp_overlap(_isolate):
    """#6931: a compression continuation recorded a few ms BEFORE the parent's
    ended_at (write-order race) is still one lineage in /api/sessions."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_race_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_race_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_race_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        # child.started_at lands 0.1s BEFORE parent.ended_at — the #6931 race.
        _insert_state_row(
            conn,
            "lineage_race_tip",
            parent="lineage_race_root",
            started_at=t0 + 5 - 0.1,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_race_tip"]
        assert tip.get("parent_session_id") == "lineage_race_root"
        assert tip.get("_lineage_root_id") == "lineage_race_root"
        assert tip.get("_lineage_tip_id") == "lineage_race_tip"
        assert tip.get("_compression_segment_count") == 2
        assert tip.get("relationship_type") != "child_session"
    finally:
        conn.close()


def test_materially_overlapping_compression_child_stays_child_session(_isolate):
    """#6931: a child that started long before the compression parent ended
    (beyond tolerance, no matching title) must remain a separate child."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_overlap_root", title="Root title", updated_at=t0)
        _save_webui_session("lineage_overlap_child", title="Child title", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_overlap_root",
            started_at=t0,
            ended_at=t0 + 60,
            end_reason="compression",
        )
        # child started 30s before the parent ended — a genuine concurrent child.
        _insert_state_row(
            conn,
            "lineage_overlap_child",
            parent="lineage_overlap_root",
            started_at=t0 + 30,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        child = rows["lineage_overlap_child"]
        assert child.get("relationship_type") == "child_session"
        assert child.get("parent_session_id") == "lineage_overlap_root"
        assert "_lineage_root_id" not in child
        assert "_compression_segment_count" not in child
    finally:
        conn.close()


def test_same_title_independent_child_outside_tolerance_stays_visible(_isolate):
    """#7021 re-gate: a same-title independent child started well outside the
    tolerance window must NOT collapse on the title match — titles are
    user-controlled and non-unique, so the only continuation evidence is the
    bounded early-side timestamp window."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_title_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_title_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_title_root",
            title="Shared conversation",
            started_at=t0,
            ended_at=t0 + 60,
            end_reason="compression",
        )
        # Same title, but started 50s before the parent ended — far outside any
        # handoff race window. Must remain a visible child session.
        _insert_state_row(
            conn,
            "lineage_title_tip",
            title="Shared conversation",
            parent="lineage_title_root",
            started_at=t0 + 10,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_title_tip"]
        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_session_id") == "lineage_title_root"
        assert "_lineage_root_id" not in tip
        assert "_compression_segment_count" not in tip
    finally:
        conn.close()


def test_model_config_branched_from_child_stays_visible_within_tolerance(_isolate):
    """#7021 re-gate: an explicit Agent branch is marked in
    model_config._branched_from (not session_source). Even when it starts
    inside the 2s tolerance window of the parent's compression, the real
    marker must keep it visible instead of collapsing it into the lineage."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_branch_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_branch_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_branch_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        # The reviewer's adversarial probe: an Agent branch starting 1.5s
        # BEFORE the parent's compression ended_at. No session_source — the
        # fork identity lives in model_config._branched_from.
        _insert_state_row(
            conn,
            "lineage_branch_tip",
            parent="lineage_branch_root",
            started_at=t0 + 5 - 1.5,
            model_config=json.dumps({"_branched_from": "lineage_branch_root"}),
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_branch_tip"]
        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_session_id") == "lineage_branch_root"
        assert "_lineage_root_id" not in tip
        assert "_compression_segment_count" not in tip
    finally:
        conn.close()


def test_model_config_delegate_from_child_stays_visible_within_tolerance(_isolate):
    """#7021 re-gate: delegate/subagent runs are marked in
    model_config._delegate_from. A delegate child starting inside the
    tolerance window of the parent's compression must stay visible."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_delegate_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_delegate_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_delegate_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_delegate_tip",
            parent="lineage_delegate_root",
            started_at=t0 + 5 - 1.0,
            model_config=json.dumps({"_delegate_from": "lineage_delegate_root"}),
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_delegate_tip"]
        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_session_id") == "lineage_delegate_root"
        assert "_lineage_root_id" not in tip
        assert "_compression_segment_count" not in tip
    finally:
        conn.close()


def test_continuation_classification_timestamp_tolerance_and_guards():
    """#6931/#7021 focused unit coverage of _is_continuation_session: bounded
    tolerance, model_config branch markers, and preserved fork/cross-source/
    end_reason guards."""
    from api.agent_sessions import _is_continuation_session

    def make_parent(**over):
        row = {
            'id': 'parent-1',
            'source': 'webui',
            'end_reason': 'compression',
            'ended_at': 1000.0,
            'title': 'Shared conversation',
        }
        row.update(over)
        return row

    def make_child(**over):
        row = {
            'id': 'child-1',
            'source': 'webui',
            'started_at': 1000.05,
            'title': 'Shared conversation',
        }
        row.update(over)
        return row

    # Normal non-overlapping ordering: continuation.
    assert _is_continuation_session(make_parent(), make_child())
    # Sub-second overlap (observed -0.06..-0.07s in #6931): continuation.
    assert _is_continuation_session(make_parent(), make_child(started_at=999.95))
    # Overlap inside the 2s tolerance: continuation.
    assert _is_continuation_session(make_parent(), make_child(started_at=998.5))
    # Overlap beyond tolerance: separate child, even with an exact title match
    # (titles are user-controlled and non-unique — no title fallback).
    assert not _is_continuation_session(make_parent(), make_child(started_at=950.0))
    assert not _is_continuation_session(
        make_parent(), make_child(started_at=950.0, title='Another conversation')
    )
    # model_config._branched_from pointing at the parent: never a
    # continuation, regardless of timing.
    assert not _is_continuation_session(
        make_parent(),
        make_child(
            started_at=999.95,
            model_config=json.dumps({'_branched_from': 'parent-1'}),
        ),
    )
    assert not _is_continuation_session(
        make_parent(),
        make_child(
            started_at=998.5,
            model_config={'_branched_from': 'parent-1'},
        ),
    )
    # model_config._delegate_from pointing at the parent: never a
    # continuation, regardless of timing.
    assert not _is_continuation_session(
        make_parent(),
        make_child(
            started_at=999.95,
            model_config=json.dumps({'_delegate_from': 'parent-1'}),
        ),
    )
    # A marker pointing at a DIFFERENT session does not disqualify: compression
    # continuations inherit the rotated agent's model_config verbatim, so a
    # delegate's continuation still carries the delegate's own marker.
    assert _is_continuation_session(
        make_parent(),
        make_child(
            started_at=999.95,
            model_config=json.dumps({'_delegate_from': 'some-other-session'}),
        ),
    )
    # Unparsable model_config degrades to no markers (no crash, no match).
    assert _is_continuation_session(
        make_parent(),
        make_child(started_at=999.95, model_config='not-json'),
    )
    assert not _is_continuation_session(
        make_parent(),
        make_child(started_at=950.0, model_config='not-json'),
    )
    # Fork guard holds regardless of timing.
    assert not _is_continuation_session(
        make_parent(), make_child(started_at=999.95, session_source='fork')
    )
    # Cross-source guard holds regardless of timing.
    assert not _is_continuation_session(
        make_parent(), make_child(started_at=999.95, source='telegram')
    )
    # Non-compression/cli_close parents never continue.
    assert not _is_continuation_session(
        make_parent(end_reason='user_stop'), make_child(started_at=999.95)
    )
    # Missing/unparsable boundary timestamps degrade to False (no crash).
    assert not _is_continuation_session(make_parent(ended_at='not-a-number'), make_child())
    assert not _is_continuation_session(make_parent(), make_child(started_at='not-a-number'))


def test_state_db_stitch_keeps_branched_child_out_of_parent_transcript(_isolate):
    """#7021 r2: the open/import transcript stitcher (get_state_db_session_messages
    with stitch_continuations=True) must apply the same model_config branch-marker
    guard as the listing classifier. A child whose model_config._branched_from /
    _delegate_from points at the parent must NOT have its messages stitched into
    the parent transcript even when it starts inside the tolerance window.
    """
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        # Compression parent whose child starts INSIDE the 2s tolerance window.
        _insert_state_row(
            conn,
            "stitch_branch_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_message(
            conn, "stitch_branch_parent", role="user", content="parent turn", timestamp=t0 + 1
        )
        # Explicit Agent branch: fork identity lives in model_config, not
        # session_source. Starting 1.5s before the parent's ended_at — inside
        # the tolerance — it must still stay out of the parent's transcript.
        _insert_state_row(
            conn,
            "stitch_branch_child",
            parent="stitch_branch_parent",
            started_at=t0 + 5 - 1.5,
            model_config=json.dumps({"_branched_from": "stitch_branch_parent"}),
        )
        _insert_state_message(
            conn, "stitch_branch_child", role="user", content="branch turn", timestamp=t0 + 6
        )

        msgs = models.get_state_db_session_messages(
            "stitch_branch_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "branch turn" in contents
        assert "parent turn" not in contents

        # Control: a genuine compression continuation (no branch marker) starting
        # inside the same window IS stitched into the parent transcript.
        _insert_state_row(
            conn,
            "stitch_continuation_child",
            parent="stitch_branch_parent",
            started_at=t0 + 5 - 0.5,
        )
        _insert_state_message(
            conn,
            "stitch_continuation_child",
            role="user",
            content="continuation turn",
            timestamp=t0 + 7,
        )
        msgs = models.get_state_db_session_messages(
            "stitch_continuation_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "continuation turn" in contents
        assert "parent turn" in contents
    finally:
        conn.close()


def test_state_db_stitch_keeps_delegate_child_out_of_parent_transcript(_isolate):
    """#7021 r2: same guard for model_config._delegate_from (subagent runs) in
    the open/import transcript stitcher."""
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _insert_state_row(
            conn,
            "stitch_delegate_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_message(
            conn, "stitch_delegate_parent", role="user", content="parent turn", timestamp=t0 + 1
        )
        _insert_state_row(
            conn,
            "stitch_delegate_child",
            parent="stitch_delegate_parent",
            started_at=t0 + 5 - 1.0,
            model_config=json.dumps({"_delegate_from": "stitch_delegate_parent"}),
        )
        _insert_state_message(
            conn, "stitch_delegate_child", role="user", content="delegate turn", timestamp=t0 + 6
        )

        msgs = models.get_state_db_session_messages(
            "stitch_delegate_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "delegate turn" in contents
        assert "parent turn" not in contents
    finally:
        conn.close()


def test_state_db_stitch_old_schema_without_identity_columns_still_stitches(_isolate):
    """#7021 r2: older state.db schemas lacking session_source/model_config degrade
    to NULL and keep the pre-fix behavior — a genuine compression continuation
    within the tolerance is still stitched, without crashing."""
    conn = sqlite3.connect(str(_isolate))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        """
    )
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, title, started_at, message_count, parent_session_id, ended_at, end_reason)
            VALUES (?, ?, ?, ?, 2, ?, ?, ?)
            """,
            (
                "stitch_old_parent",
                "webui",
                "stitch_old_parent",
                t0,
                None,
                t0 + 5,
                "compression",
            ),
        )
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, title, started_at, message_count, parent_session_id, ended_at, end_reason)
            VALUES (?, ?, ?, ?, 2, ?, NULL, NULL)
            """,
            ("stitch_old_child", "webui", "stitch_old_child", t0 + 4.5, "stitch_old_parent"),
        )
        conn.commit()
        _insert_state_message(
            conn, "stitch_old_parent", role="user", content="parent turn", timestamp=t0 + 1
        )
        _insert_state_message(
            conn, "stitch_old_child", role="user", content="old child turn", timestamp=t0 + 6
        )

        msgs = models.get_state_db_session_messages(
            "stitch_old_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "old child turn" in contents
        assert "parent turn" in contents
    finally:
        conn.close()
