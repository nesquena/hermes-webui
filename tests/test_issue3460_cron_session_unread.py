"""Regression coverage for #3460 cron session unread badges."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def test_cron_recent_returns_latest_session_id_for_job(monkeypatch, tmp_path):
    import api.routes as routes

    db_path = tmp_path / "state.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                started_at REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions(id, source, started_at) VALUES (?, ?, ?)",
            ("cron_job3460_20260610_060000", "cron", 100.0),
        )
        conn.execute(
            "INSERT INTO sessions(id, source, started_at) VALUES (?, ?, ?)",
            ("cron_job3460_20260610_070000", "cron", 200.0),
        )
        conn.execute(
            "INSERT INTO sessions(id, source, started_at) VALUES (?, ?, ?)",
            ("telegram_job3460_20260610_080000", "telegram", 300.0),
        )

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.list_jobs = lambda include_disabled=True: [
        {
            "id": "job3460",
            "name": "Morning Briefing",
            "last_run_at": 250,
            "last_status": "success",
        }
    ]
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db_path)
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_recent(handler, SimpleNamespace(query="since=0"))

    body = _payload(handler)
    assert handler.status == 200
    assert body["completions"][0]["job_id"] == "job3460"
    assert body["completions"][0]["session_id"] == "cron_job3460_20260610_070000"


def test_sessions_helper_marks_background_completion_with_existing_unread_marker():
    assert "function _markSessionCompletionUnreadIfBackground(" in SESSIONS_JS
    helper_start = SESSIONS_JS.find("function _markSessionCompletionUnreadIfBackground(")
    helper_end = SESSIONS_JS.find("function _clearSessionCompletionUnread(", helper_start)
    assert helper_start != -1 and helper_end != -1
    helper_block = SESSIONS_JS[helper_start:helper_end]

    assert "_isSessionActivelyViewedForList(sid)" in helper_block
    assert "_setSessionViewedCount(sid, count);" in helper_block
    assert "_markSessionCompletionUnread(sid, count);" in helper_block
    assert "renderSessionListFromCache()" in helper_block


def test_cron_polling_bridges_recent_completion_to_sidebar_unread_helper():
    start = PANELS_JS.find("function startCronPolling()")
    end = PANELS_JS.find("function updateCronBadge()", start)
    assert start != -1 and end != -1
    body = PANELS_JS[start:end]

    assert "_cronNewJobIds.add(String(c.job_id))" in body
    assert "if(c.session_id && typeof _markSessionCompletionUnreadIfBackground === 'function')" in body
    assert "_markSessionCompletionUnreadIfBackground(c.session_id);" in body
