"""Read-only session lineage report endpoint tests."""

import json
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

import api.agent_sessions as agent_sessions
import api.routes as routes


def _ensure_state_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
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


def _insert_state_row(conn, sid, *, parent=None, ended_at=None, end_reason=None, started_at=None, source="webui"):
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason)
        VALUES (?, ?, ?, 'openai/gpt-5', ?, 2, ?, ?, ?)
        """,
        (sid, source, sid.replace("_", " "), started_at or time.time(), parent, ended_at, end_reason),
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
