"""Sidebar origin tags for cron jobs and delegated subagents.

Cron/subagent sessions can flood the merged CLI session sidebar. The API should
surface compact origin metadata (cron job name, skills, parent agent) and the
client should include those tags in filtering/rendering.
"""
import json
import sqlite3
from pathlib import Path

import api.models as models


ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _make_state_db(path, rows):
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
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp REAL
        )
        """
    )
    for idx, row in enumerate(rows):
        conn.execute(
            """
            INSERT INTO sessions (
                id, title, model, message_count, started_at, source,
                parent_session_id, ended_at, end_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row.get("title"),
                row.get("model", "gpt-x"),
                row.get("message_count", 1),
                row.get("started_at", 1700000000.0 + idx),
                row.get("source", "cli"),
                row.get("parent_session_id"),
                row.get("ended_at"),
                row.get("end_reason"),
            ),
        )
        conn.execute(
            "INSERT INTO messages (session_id, timestamp) VALUES (?, ?)",
            (row["id"], 1700000001.0 + idx),
        )
    conn.commit()
    conn.close()


def _write_jobs_json(hermes_home, jobs):
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _patch_hermes_home(monkeypatch, home):
    import importlib
    import sys

    import api

    # Some profile-isolation tests deliberately reload api.profiles and can
    # leave the parent package attribute and sys.modules entry pointing at
    # different module objects. Patch both so get_cli_sessions() resolves the
    # fake home regardless of which import cache path Python takes next.
    modules = []
    for candidate in (
        importlib.import_module("api.profiles"),
        sys.modules.get("api.profiles"),
        getattr(api, "profiles", None),
    ):
        if candidate is not None and candidate not in modules:
            modules.append(candidate)

    monkeypatch.setenv("HERMES_HOME", str(home))
    for profiles in modules:
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home, raising=False)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None, raising=False)


def test_cron_sessions_include_job_and_skill_origin_tags(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _patch_hermes_home(monkeypatch, hermes_home)
    _write_jobs_json(
        hermes_home,
        [{"id": "job123", "name": "Nightly WebUI Scout", "skills": ["hermes-webui-customization", "dogfood"]}],
    )
    _make_state_db(
        hermes_home / "state.db",
        [{"id": "cron_job123_20260504_010203", "title": None, "source": "cron"}],
    )

    sessions = models.get_cli_sessions()

    assert len(sessions) == 1
    session = sessions[0]
    assert session["title"] == "Nightly WebUI Scout"
    assert session["origin_label"] == "Cron: Nightly WebUI Scout"
    assert session["origin_tags"] == ["Cron", "hermes-webui-customization", "dogfood"]
    assert "Nightly WebUI Scout" in session["origin_filter_text"]


def test_subagent_sessions_include_parent_agent_origin_tags(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _patch_hermes_home(monkeypatch, hermes_home)
    _make_state_db(
        hermes_home / "state.db",
        [
            {"id": "parent_cli_001", "title": "Maintainer agent", "source": "cli"},
            {
                "id": "child_cli_001",
                "title": "Investigate failing test",
                "source": "cli",
                "parent_session_id": "parent_cli_001",
            },
        ],
    )

    sessions = models.get_cli_sessions()
    child = next(s for s in sessions if s["session_id"] == "child_cli_001")

    assert child["relationship_type"] == "child_session"
    assert child["origin_tags"] == ["Subagent"]
    assert child["origin_label"] == "Subagent: Maintainer agent"
    assert "Maintainer agent" in child["origin_filter_text"]


def test_cron_subagents_inherit_cron_job_skill_tags(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _patch_hermes_home(monkeypatch, hermes_home)
    _write_jobs_json(
        hermes_home,
        [{"id": "job123", "name": "Nightly WebUI Scout", "skills": ["dogfood"]}],
    )
    _make_state_db(
        hermes_home / "state.db",
        [
            {"id": "cron_job123_20260504_010203", "title": None, "source": "cron"},
            {
                "id": "subagent_from_cron_001",
                "title": "Check issue funnel",
                "source": "cron",
                "parent_session_id": "cron_job123_20260504_010203",
            },
        ],
    )

    sessions = models.get_cli_sessions()
    child = next(s for s in sessions if s["session_id"] == "subagent_from_cron_001")

    assert child["origin_tags"] == ["Subagent", "Cron", "dogfood"]
    assert child["origin_label"] == "Subagent from Cron: Nightly WebUI Scout"
    assert "Nightly WebUI Scout" in child["origin_filter_text"]


def test_sidebar_search_uses_origin_terms():
    assert "function _sessionOriginTerms(s)" in SESSIONS_JS
    assert "function _sessionMatchesQuery(s,q)" in SESSIONS_JS
    assert "_sessionMatchesQuery(s,q)" in SESSIONS_JS
    assert "origin_filter_text" in SESSIONS_JS


def test_sidebar_renders_origin_chips():
    assert "session-origin-chip" in SESSIONS_JS
    assert "_sessionOriginTagsForDisplay" in SESSIONS_JS
    assert ".session-origin-chip" in STYLE_CSS
