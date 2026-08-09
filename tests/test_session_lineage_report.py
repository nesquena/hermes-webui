"""Read-only session lineage report endpoint tests."""

import json
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

import api.agent_sessions as agent_sessions
import api.models as models
import api.profiles as profiles
import api.routes as routes


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
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    return conn


def _insert_state_row(conn, sid, *, parent=None, ended_at=None, end_reason=None, started_at=None, source="webui", session_source=None):
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
        VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 2, ?, ?, ?)
        """,
        (sid, source, session_source, sid.replace("_", " "), started_at or time.time(), parent, ended_at, end_reason),
    )
    conn.commit()


def _insert_message(conn, sid, *, timestamp=None, role="user"):
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, 'hello', ?)",
        (f"msg_{sid}_{role}", sid, role, timestamp or time.time()),
    )
    conn.commit()


def test_lineage_report_returns_bounded_read_only_tip_and_hidden_segments(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_mid", parent="lineage_report_root", started_at=t0 + 6, ended_at=t0 + 12, end_reason="cli_close")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_mid", started_at=t0 + 13)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip")

        assert report["mutation"] is False
        assert report["session_id"] == "lineage_report_tip"
        assert report["lineage_key"] == "lineage_report_root"
        assert report["tip_session_id"] == "lineage_report_tip"
        assert report["total_segments"] == 3
        assert report["materialized_segments"] == 3
        assert [s["session_id"] for s in report["segments"]] == [
            "lineage_report_tip",
            "lineage_report_mid",
            "lineage_report_root",
        ]
        assert [s["role"] for s in report["segments"]] == ["tip", "hidden_segment", "hidden_segment"]
        assert report["children"] == []
        assert report["manual_review"] is False
        assert "archive_candidates" not in report
        assert "delete_candidates" not in report
    finally:
        conn.close()


def test_lineage_report_keeps_cross_surface_parent_out_of_hidden_segments(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(
            conn,
            "lineage_report_telegram_parent",
            source="telegram",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_report_webui_tip",
            source="webui",
            parent="lineage_report_telegram_parent",
            started_at=t0 + 6,
        )

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_webui_tip")

        assert report["lineage_key"] == "lineage_report_webui_tip"
        assert report["total_segments"] == 1
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_webui_tip"]
        assert report["segments"][0]["role"] == "tip"
        assert report["children"] == []
    finally:
        conn.close()


def test_lineage_report_keeps_explicit_forks_out_of_hidden_segments(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(
            conn,
            "lineage_report_fork",
            parent="lineage_report_root",
            started_at=t0 + 6,
            session_source="fork",
        )

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_fork")

        assert report["lineage_key"] == "lineage_report_fork"
        assert report["tip_session_id"] == "lineage_report_fork"
        assert report["total_segments"] == 1
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_fork"]
        assert report["segments"][0]["role"] == "tip"
        assert report["children"] == []
        assert report["manual_review"] is False
    finally:
        conn.close()


def test_importable_agent_projection_keeps_explicit_forks_out_of_compression_lineage(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(
            conn,
            "lineage_report_fork",
            parent="lineage_report_root",
            started_at=t0 + 6,
            session_source="fork",
        )
        _insert_message(conn, "lineage_report_fork", timestamp=t0 + 7)

        rows = agent_sessions.read_importable_agent_session_rows(tmp_path / "state.db", exclude_sources=())

        assert [row["id"] for row in rows] == ["lineage_report_fork"]
        fork = rows[0]
        assert fork.get("relationship_type") == "child_session"
        assert fork.get("parent_session_id") == "lineage_report_root"
        assert fork.get("_parent_lineage_root_id") == "lineage_report_root"
        assert "_lineage_root_id" not in fork
        assert "_compression_segment_count" not in fork
    finally:
        conn.close()


def test_lineage_report_surfaces_non_continuation_children_without_mutation(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_root", started_at=t0 + 6, ended_at=t0 + 15, end_reason="user_stop")
        _insert_state_row(conn, "lineage_report_child", parent="lineage_report_tip", started_at=t0 + 8)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip")

        assert report["lineage_key"] == "lineage_report_root"
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_tip", "lineage_report_root"]
        assert report["children"] == [
            {
                "session_id": "lineage_report_child",
                "role": "child_session",
                "title": "lineage report child",
                "source": "webui",
                "started_at": t0 + 8,
                "updated_at": t0 + 8,
                "end_reason": None,
                "active": True,
                "archived": False,
            }
        ]
        assert report["mutation"] is False
    finally:
        conn.close()


def test_lineage_report_marks_bounded_parent_walk_for_manual_review(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_mid", parent="lineage_report_root", started_at=t0 + 6, ended_at=t0 + 12, end_reason="compression")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_mid", started_at=t0 + 13)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip", max_hops=1)

        assert report["mutation"] is False
        assert report["manual_review"] is True
        assert [s["session_id"] for s in report["segments"]] == ["lineage_report_tip", "lineage_report_mid"]
        assert report["total_segments"] == 2
    finally:
        conn.close()


def test_lineage_report_endpoint_is_read_only_and_uses_active_state_db(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 100
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, "lineage_report_tip", parent="lineage_report_root", started_at=t0 + 6)
        captured = {}

        def fake_j(handler, data, status=200, **_kwargs):
            captured["status"] = status
            captured["data"] = data
            return data

        handler = SimpleNamespace()
        parsed = urlparse("/api/session/lineage/report?session_id=lineage_report_tip")
        with patch.object(routes, "_active_state_db_path", return_value=tmp_path / "state.db"), patch.object(routes, "j", side_effect=fake_j):
            routes.handle_get(handler, parsed)

        assert captured["status"] == 200
        assert captured["data"]["mutation"] is False
        assert captured["data"]["lineage_key"] == "lineage_report_root"
        assert captured["data"]["total_segments"] == 2
    finally:
        conn.close()


def test_lineage_report_endpoint_selects_requested_profile_state_db(tmp_path):
    active_db = tmp_path / "active.db"
    profile_db = tmp_path / "work.db"
    active = _ensure_state_db(active_db)
    profile = _ensure_state_db(profile_db)
    try:
        _insert_state_row(active, "same-id", started_at=time.time() - 10)
        _insert_state_row(profile, "same-id", started_at=time.time() - 100)
        captured = {}
        requested_profiles = []

        def fake_j(handler, data, status=200, **_kwargs):
            captured["status"] = status
            captured["data"] = data
            return data

        handler = SimpleNamespace()
        parsed = urlparse("/api/session/lineage/report?session_id=same-id&profile=work")
        def profile_state_db_path(*, profile, fallback_to_active):
            requested_profiles.append(profile)
            assert fallback_to_active is False
            return profile_db

        with patch.object(routes, "_active_state_db_path", return_value=active_db), \
            patch.object(routes, "_agent_state_db_path", side_effect=profile_state_db_path), \
            patch.object(routes, "j", side_effect=fake_j):
            routes.handle_get(handler, parsed)

        assert captured["status"] == 200
        assert requested_profiles == ["work"]
        assert captured["data"]["session_id"] == "same-id"
        assert captured["data"]["tip_session_id"] == "same-id"
    finally:
        active.close()
        profile.close()


def test_requested_profile_lineage_report_fails_closed_without_profile_state_db(tmp_path):
    active_db = tmp_path / "active" / "state.db"
    active_db.parent.mkdir()
    active_db.touch()
    missing_profile_home = tmp_path / "missing-profile"

    with patch.object(models, "_get_profile_home", return_value=missing_profile_home), \
        patch.object(models, "_active_state_db_path", return_value=active_db):
        assert models._agent_state_db_path(profile="missing", fallback_to_active=False) is None

    captured = {}
    requested_profiles = []

    def fake_bad(handler, message, status=400):
        captured["status"] = status
        captured["message"] = message
        return {"error": message}

    def missing_profile_state_db_path(*, profile, fallback_to_active):
        requested_profiles.append((profile, fallback_to_active))
        return None

    handler = SimpleNamespace()
    parsed = urlparse("/api/session/lineage/report?session_id=same-id&profile=missing")
    with patch.object(routes, "_agent_state_db_path", side_effect=missing_profile_state_db_path), \
        patch.object(routes, "bad", side_effect=fake_bad), \
        patch.object(routes, "read_session_lineage_report") as read_report:
        routes.handle_get(handler, parsed)

    assert requested_profiles == [("missing", False)]
    assert captured == {"status": 404, "message": "Session not found"}
    read_report.assert_not_called()


def test_foreign_isolated_profile_rejects_before_pinned_home_resolution(monkeypatch, tmp_path):
    pinned_db = tmp_path / "pinned" / "state.db"
    pinned_db.parent.mkdir()
    pinned_db.touch()
    resolve_calls = []

    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "profile-a")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: resolve_calls.append(profile) or pinned_db)

    assert models._agent_state_db_path(profile="profile-b", fallback_to_active=False) is None
    assert resolve_calls == []


def test_lineage_report_endpoint_rejects_invalid_profile_before_db_resolution():
    captured = {}

    def fake_bad(handler, message, status=400):
        captured["status"] = status
        captured["message"] = message
        return {"error": message}

    handler = SimpleNamespace()
    parsed = urlparse("/api/session/lineage/report?session_id=same-id&profile=../outside")
    with patch.object(routes, "_agent_state_db_path") as resolve_db, \
        patch.object(routes, "bad", side_effect=fake_bad), \
        patch.object(routes, "read_session_lineage_report") as read_report:
        routes.handle_get(handler, parsed)

    assert captured == {"status": 400, "message": "invalid profile"}
    resolve_db.assert_not_called()
    read_report.assert_not_called()


def test_lineage_report_endpoint_returns_404_for_unknown_session(tmp_path):
    conn = _ensure_state_db(tmp_path / "state.db")
    conn.close()
    captured = {}

    def fake_bad(handler, message, status=400):
        captured["status"] = status
        captured["message"] = message
        return {"error": message}

    handler = SimpleNamespace()
    parsed = urlparse("/api/session/lineage/report?session_id=missing_lineage_report_session")
    with patch.object(routes, "_active_state_db_path", return_value=tmp_path / "state.db"), patch.object(routes, "bad", side_effect=fake_bad):
        routes.handle_get(handler, parsed)

    assert captured == {"status": 404, "message": "Session not found"}


def test_lineage_report_preserves_child_order_for_each_segment_parent(tmp_path):
    """Behavioural coverage for batched child fetch: started_at DESC per parent."""
    conn = _ensure_state_db(tmp_path / "state.db")
    t0 = time.time() - 200
    try:
        _insert_state_row(conn, "lineage_report_root", started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(
            conn,
            "lineage_report_tip",
            parent="lineage_report_root",
            started_at=t0 + 6,
            ended_at=t0 + 15,
            end_reason="user_stop",
        )
        _insert_state_row(conn, "lineage_report_child_old", parent="lineage_report_tip", started_at=t0 + 8)
        _insert_state_row(conn, "lineage_report_child_new", parent="lineage_report_tip", started_at=t0 + 20)

        report = agent_sessions.read_session_lineage_report(tmp_path / "state.db", "lineage_report_tip")

        assert [child["session_id"] for child in report["children"]] == [
            "lineage_report_child_new",
            "lineage_report_child_old",
        ]
    finally:
        conn.close()
