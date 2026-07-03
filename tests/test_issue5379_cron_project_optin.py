"""Regression tests for #5379: gate 'Cron Jobs' project auto-creation behind
project opt-in.

ensure_cron_project() used to unconditionally mint and persist a 'Cron Jobs'
project the first time any code path saw a cron session, even on installs
that never created a project of their own. These tests pin the fix: creation
is now gated on whether the active profile already has at least one real
(non-system) project; lookup, renamed-root alias resolution, and legacy
untagged back-tagging are all unaffected. ensure_cron_project() itself is
never mocked here — only PROJECTS_FILE and profile-resolution helpers are.
"""

import json
import sqlite3
import threading
import time

import pytest


def _make_cron_state_db(path, *, cron_count=1):
    """state.db with a batch of cron-sourced sessions (mirrors #4842's helper)."""
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
    for i in range(cron_count):
        sid = f"cron_job{i:04d}_{int(now) + i}"
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model,"
            " started_at, message_count, parent_session_id, ended_at, end_reason)"
            " VALUES (?, 'cron', 'cron', 'Cron run', 'test-model', ?, 1, NULL, NULL, NULL)",
            (sid, now + i),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, 'user', 'cron task', ?)",
            (f"cron_msg_{i:04d}", sid, now + i),
        )
    conn.commit()
    conn.close()


def _write_projects(path, projects):
    path.write_text(json.dumps(projects), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_projects(tmp_path, monkeypatch):
    """Point PROJECTS_FILE at a fresh tmp_path file; reset profile-alias caches."""
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_CRON_PROJECT_LOCK", threading.Lock())
    monkeypatch.setattr(models, "_WEBHOOK_PROJECT_LOCK", threading.Lock())
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.setattr(profiles, "_active_profile", "default")
    profiles._invalidate_root_profile_cache()
    yield projects_file
    profiles._invalidate_root_profile_cache()


def test_sidebar_scan_zero_user_projects_skips_cron_project_creation(tmp_path):
    """Reproduction anchor: listing the sidebar with a cron row must not
    create or persist a Cron Jobs project when no user project exists."""
    import api.models as models

    projects_file = tmp_path / "projects.json"
    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=1)

    rows = models._load_cli_sessions_uncached(
        tmp_path, db, _cli_profile=None, include_claude_code=False,
    )

    cron_rows = [r for r in rows if r["source_tag"] == "cron"]
    assert len(cron_rows) == 1
    assert cron_rows[0]["project_id"] is None
    assert not projects_file.exists(), (
        "gate must not persist a Cron Jobs project when the active profile "
        "has zero user-created projects"
    )


def test_recovery_endpoint_zero_user_projects_skips_cron_project_creation(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "cron_recover_0001"
    cron_meta = {
        "session_id": sid,
        "title": "Cron run",
        "model": "test-model",
        "created_at": 10.0,
        "updated_at": 20.0,
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "is_cli_session": True,
        "read_only": False,
    }
    messages = [{"role": "user", "content": "run"}, {"role": "assistant", "content": "done"}]

    class FakeSession:
        def __init__(self):
            self.project_id = None

        def save(self, touch_updated_at=False):
            pass

        def compact(self):
            return {"session_id": sid, "title": "Cron run"}

    fake = FakeSession()

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid, profile=None: messages)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [cron_meta])
    monkeypatch.setattr(routes, "import_cli_session", lambda *a, **k: fake)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_queue_generated_title_for_imported_session", lambda *a, **k: None)

    response = routes._handle_session_import_cli(object(), {"session_id": sid})

    assert response["imported"] is True
    assert fake.project_id is None
    assert not (tmp_path / "projects.json").exists()


def test_sidebar_scan_with_user_project_still_autoassigns_cron(tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    _write_projects(projects_file, [
        {"project_id": "user-proj-1", "name": "My Website", "profile": "default", "created_at": 1.0},
    ])
    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=1)

    rows = models._load_cli_sessions_uncached(
        tmp_path, db, _cli_profile=None, include_claude_code=False,
    )

    cron_rows = [r for r in rows if r["source_tag"] == "cron"]
    assert len(cron_rows) == 1
    assert cron_rows[0]["project_id"] is not None

    saved = json.loads(projects_file.read_text())
    cron_saved = [p for p in saved if p["name"] == "Cron Jobs"]
    assert len(cron_saved) == 1


def test_recovery_endpoint_with_user_project_still_autoassigns_cron(monkeypatch, tmp_path):
    import api.routes as routes

    projects_file = tmp_path / "projects.json"
    _write_projects(projects_file, [
        {"project_id": "user-proj-1", "name": "My Website", "profile": "default", "created_at": 1.0},
    ])

    sid = "cron_recover_0002"
    cron_meta = {
        "session_id": sid,
        "title": "Cron run",
        "model": "test-model",
        "created_at": 10.0,
        "updated_at": 20.0,
        "source_tag": "cron",
        "raw_source": "cron",
        "session_source": "cron",
        "source_label": "Cron",
        "is_cli_session": True,
        "read_only": False,
    }
    messages = [{"role": "user", "content": "run"}, {"role": "assistant", "content": "done"}]

    class FakeSession:
        def __init__(self):
            self.project_id = None

        def save(self, touch_updated_at=False):
            pass

        def compact(self):
            return {"session_id": sid, "title": "Cron run"}

    fake = FakeSession()

    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, _sid: None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid, profile=None: messages)
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [cron_meta])
    monkeypatch.setattr(routes, "import_cli_session", lambda *a, **k: fake)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_queue_generated_title_for_imported_session", lambda *a, **k: None)

    response = routes._handle_session_import_cli(object(), {"session_id": sid})

    assert response["imported"] is True
    assert fake.project_id is not None
    saved = json.loads(projects_file.read_text())
    assert any(p["name"] == "Cron Jobs" for p in saved)


def test_preexisting_tagged_cron_project_resolves_when_zero_user_projects(tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    _write_projects(projects_file, [
        {"project_id": "existing-cron", "name": "Cron Jobs", "profile": "default", "color": "#6366f1", "created_at": 1.0},
    ])
    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=1)

    rows = models._load_cli_sessions_uncached(
        tmp_path, db, _cli_profile=None, include_claude_code=False,
    )

    cron_rows = [r for r in rows if r["source_tag"] == "cron"]
    assert cron_rows[0]["project_id"] == "existing-cron"
    saved = json.loads(projects_file.read_text())
    assert len(saved) == 1
    assert saved[0]["project_id"] == "existing-cron"


def test_legacy_untagged_cron_project_back_tagged_when_zero_user_projects(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles

    # A non-default active profile is required to exercise the back-tag loop:
    # with 'default' active, an untagged row's implicit profile and the active
    # profile both alias to the root profile (_is_root_profile('default') is
    # unconditionally True), so the FIRST (alias-match) lookup loop would
    # already resolve it without ever reaching the back-tag loop below it.
    # Mirrors the active-profile choice in the proven
    # test_issue1614_project_profile_filtering.py::test_ensure_cron_project_back_tags_legacy_untagged.
    monkeypatch.setattr(profiles, "_active_profile", "haku")
    profiles._invalidate_root_profile_cache()

    projects_file = tmp_path / "projects.json"
    _write_projects(projects_file, [
        {"project_id": "legacy-cron", "name": "Cron Jobs", "color": "#6366f1", "created_at": 1.0},
    ])
    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=1)

    rows = models._load_cli_sessions_uncached(
        tmp_path, db, _cli_profile=None, include_claude_code=False,
    )

    cron_rows = [r for r in rows if r["source_tag"] == "cron"]
    assert cron_rows[0]["project_id"] == "legacy-cron"
    saved = json.loads(projects_file.read_text())
    assert saved[0]["profile"] == "haku", (
        "legacy untagged project must still be back-tagged even when the "
        "create branch is gated off"
    )


def test_renamed_root_alias_cron_project_resolves_when_zero_user_projects(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    _write_projects(projects_file, [
        {"project_id": "root-alias-cron", "name": "Cron Jobs", "profile": "default", "color": "#6366f1", "created_at": 1.0},
    ])
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [
        {"name": "kinni", "is_default": True, "path": str(tmp_path)},
    ])
    monkeypatch.setattr(profiles, "_active_profile", "kinni")
    profiles._invalidate_root_profile_cache()

    db = tmp_path / "state.db"
    _make_cron_state_db(db, cron_count=1)

    rows = models._load_cli_sessions_uncached(
        tmp_path, db, _cli_profile=None, include_claude_code=False,
    )

    cron_rows = [r for r in rows if r["source_tag"] == "cron"]
    assert cron_rows[0]["project_id"] == "root-alias-cron"


def test_webhook_project_still_auto_created_when_zero_user_projects(tmp_path):
    """Adjacent-condition row: the ungated webhook path must still
    auto-create its dedicated project even in the same zero-user-project
    scan where the cron path is now suppressed."""
    import api.models as models

    projects_file = tmp_path / "projects.json"
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
                started_at REAL, source TEXT, user_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions (id, title, model, message_count, started_at, source, user_id)"
            " VALUES ('webhook_route_1', NULL, 'test-model', 1, 20, 'webhook', 'webhook:read-later')"
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES ('webhook_route_1', 'user', 'payload', 21)"
        )

    rows = models._load_cli_sessions_uncached(
        tmp_path, db_path, "default", include_claude_code=False,
    )

    webhook_rows = [r for r in rows if r["source_tag"] == "webhook"]
    assert len(webhook_rows) == 1
    assert webhook_rows[0]["project_id"] is not None

    saved = json.loads(projects_file.read_text())
    assert any(p["name"] == "Webhooks" for p in saved)


def test_profile_has_user_projects_ignores_system_named_projects(tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"

    _write_projects(projects_file, [])
    assert models._profile_has_user_projects() is False

    _write_projects(projects_file, [
        {"project_id": "c1", "name": "Cron Jobs", "profile": "default", "created_at": 1.0},
        {"project_id": "w1", "name": "Webhooks", "profile": "default", "created_at": 1.0},
    ])
    assert models._profile_has_user_projects() is False, (
        "system-named projects don't count as opting in"
    )

    _write_projects(projects_file, [
        {"project_id": "a1", "name": "Cron Jobs Archive", "profile": "default", "created_at": 1.0},
    ])
    assert models._profile_has_user_projects() is True, (
        "only exact reserved names are excluded; similar names are user projects"
    )

    _write_projects(projects_file, [
        {"project_id": "c1", "name": "Cron Jobs", "profile": "default", "created_at": 1.0},
        {"project_id": "u1", "name": "My Real Project", "profile": "default", "created_at": 2.0},
    ])
    assert models._profile_has_user_projects() is True


def test_ensure_cron_project_default_create_true_ignores_gate(tmp_path):
    """A direct call with no `create` argument must keep today's
    unconditional-create behavior, even with zero user projects — only the
    two gated callers opt in."""
    import api.models as models

    projects_file = tmp_path / "projects.json"
    pid = models.ensure_cron_project()

    saved = json.loads(projects_file.read_text())
    assert any(p["project_id"] == pid and p["name"] == "Cron Jobs" for p in saved)
