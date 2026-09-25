"""System projects minted by the all-profiles sidebar scan must belong to the
profile whose state.db produced the rows, not the profile currently selected.

`get_cli_sessions(all_profiles=True)` walks every profile's state.db in one
request. When it reached a profile with webhook (or cron) sessions, the lazily
created "Webhooks" / "Cron Jobs" system project used to be tagged with
`get_active_profile_name()` — the UI-selected profile — so a webhook-only
profile leaked a "Webhooks" project onto whichever profile the user happened
to be viewing. Deleting the stray entry did not help: the next sidebar poll
re-minted it, and switching profiles minted another one per profile.
"""

import json
import sqlite3
import threading
import time

import pytest


def _make_state_db(path, *, source, count=1):
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
    now = time.time()
    for i in range(count):
        sid = f"{source}_{i:04d}_{int(now) + i}"
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model,"
            " started_at, message_count) VALUES (?, ?, ?, ?, 'test-model', ?, 1)",
            (sid, source, source, f"{source} run", now + i),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, 'user', 'payload', ?)",
            (f"{source}_msg_{i:04d}", sid, now + i),
        )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_projects(tmp_path, monkeypatch):
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_CRON_PROJECT_LOCK", threading.Lock())
    monkeypatch.setattr(models, "_WEBHOOK_PROJECT_LOCK", threading.Lock())
    monkeypatch.setattr(models, "get_last_workspace", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.setattr(profiles, "_active_profile", "default")
    profiles._invalidate_root_profile_cache()
    yield projects_file
    profiles._invalidate_root_profile_cache()


def _projects(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _scan(models, home, db, profile):
    return models._load_cli_sessions_uncached(
        home, db, profile,
        source_filter=None,
        visible_session_limit=None,
        cron_project_limit=None,
        webhook_project_limit=None,
        kanban_project_limit=None,
        include_claude_code=False,
    )


def test_webhook_project_is_tagged_to_scanned_profile_not_selected_one(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    hook_home = tmp_path / "profiles" / "hooks"
    hook_home.mkdir(parents=True)
    hook_db = hook_home / "state.db"
    _make_state_db(hook_db, source="webhook", count=3)

    # UI has `default` selected; the all-profiles walk reaches the hooks profile.
    monkeypatch.setattr(profiles, "_active_profile", "default")
    rows = _scan(models, hook_home, hook_db, "hooks")
    assert sum(1 for r in rows if r["source_tag"] == "webhook") == 3

    webhook_projects = [p for p in _projects(projects_file) if p["name"] == models.WEBHOOK_PROJECT_NAME]
    assert [p["profile"] for p in webhook_projects] == ["hooks"], (
        "Webhooks project must be tagged to the profile that owns the sessions"
    )
    assert all(r["project_id"] == webhook_projects[0]["project_id"]
               for r in rows if r["source_tag"] == "webhook")

    # Switching the selected profile and rescanning must reuse the same project
    # rather than minting one per selected profile.
    monkeypatch.setattr(profiles, "_active_profile", "deepseek")
    _scan(models, hook_home, hook_db, "hooks")
    webhook_projects = [p for p in _projects(projects_file) if p["name"] == models.WEBHOOK_PROJECT_NAME]
    assert [p["profile"] for p in webhook_projects] == ["hooks"]


def test_scanning_a_profile_without_webhook_rows_mints_nothing(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    home = tmp_path / "profiles" / "quiet"
    home.mkdir(parents=True)
    db = home / "state.db"
    _make_state_db(db, source="cli", count=2)

    monkeypatch.setattr(profiles, "_active_profile", "default")
    _scan(models, home, db, "quiet")
    _scan(models, home, db, "default")

    assert not [p for p in _projects(projects_file) if p["name"] == models.WEBHOOK_PROJECT_NAME]


def test_cron_project_gate_and_tag_follow_scanned_profile(tmp_path, monkeypatch):
    """The opt-in gate and the tag for Cron Jobs must both key on the scanned
    profile: a user project on the *selected* profile must not unlock cron
    project creation for a different scanned profile."""
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps([
        {"project_id": "u1", "name": "Mine", "profile": "default", "created_at": 1.0},
    ]), encoding="utf-8")

    cron_home = tmp_path / "profiles" / "jobs"
    cron_home.mkdir(parents=True)
    cron_db = cron_home / "state.db"
    _make_state_db(cron_db, source="cron", count=1)

    monkeypatch.setattr(profiles, "_active_profile", "default")
    rows = _scan(models, cron_home, cron_db, "jobs")
    cron_rows = [r for r in rows if r["source_tag"] == "cron"]
    assert len(cron_rows) == 1
    assert cron_rows[0]["project_id"] is None
    assert not [p for p in _projects(projects_file) if p["name"] == models.CRON_PROJECT_NAME]

    # Once the scanned profile itself opts in, its cron project is tagged to it.
    projects_file.write_text(json.dumps([
        {"project_id": "u1", "name": "Mine", "profile": "default", "created_at": 1.0},
        {"project_id": "u2", "name": "Theirs", "profile": "jobs", "created_at": 2.0},
    ]), encoding="utf-8")
    rows = _scan(models, cron_home, cron_db, "jobs")
    cron_projects = [p for p in _projects(projects_file) if p["name"] == models.CRON_PROJECT_NAME]
    assert [p["profile"] for p in cron_projects] == ["jobs"]
    assert rows[0]["project_id"] == cron_projects[0]["project_id"]
