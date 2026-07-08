from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest


class _FakeFolder:
    def __init__(self, path: str):
        self.path = path
        self.label = None
        self.is_primary = True
        self.added_at = 7

    def to_dict(self):
        return {
            "path": self.path,
            "label": self.label,
            "is_primary": self.is_primary,
            "added_at": self.added_at,
        }


def _install_fake_projects_db(monkeypatch, *, projects, list_error: Exception | None = None):
    module = types.ModuleType("hermes_cli.projects_db")

    def _connect():
        raise AssertionError("read-only adapter must not call projects_db.connect()")

    def _list_projects(conn):
        if list_error is not None:
            raise list_error
        assert isinstance(conn, sqlite3.Connection)
        return projects

    module.connect = _connect
    module.list_projects = _list_projects

    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    package.projects_db = module

    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.projects_db", module)
    return module


def _profile_home(tmp_path: Path, name: str) -> Path:
    if name in ("", "default"):
        return tmp_path / "root"
    return tmp_path / "profiles" / name


def test_projects_db_adapter_maps_slug_and_profile(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    fake_projects = [
        types.SimpleNamespace(
            slug="team-project",
            name="Team Project",
            color="#123456",
            created_at=123,
            primary_path="/workspace/team",
            folders=[_FakeFolder("/workspace/team")],
            archived=False,
        )
    ]
    _install_fake_projects_db(monkeypatch, projects=fake_projects)
    db_file = _profile_home(tmp_path, "work") / "projects.db"
    db_file.parent.mkdir(parents=True)
    db_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "work", raising=False)
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)

    rows = adapter.load_projects_from_db()

    assert rows == [
        {
            "project_id": "team-project",
            "name": "Team Project",
            "color": "#123456",
            "profile": "work",
            "created_at": 123,
            "primary_path": "/workspace/team",
            "folders": [
                {
                    "path": "/workspace/team",
                    "label": None,
                    "is_primary": True,
                    "added_at": 7,
                }
            ],
        }
    ]


def test_projects_db_adapter_reads_selected_profile_home(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    fake_projects = [
        types.SimpleNamespace(
            slug="profile-project",
            name="Profile Project",
            color=None,
            archived=False,
        )
    ]
    _install_fake_projects_db(monkeypatch, projects=fake_projects)
    root_db = _profile_home(tmp_path, "default") / "projects.db"
    work_db = _profile_home(tmp_path, "work") / "projects.db"
    root_db.parent.mkdir(parents=True)
    work_db.parent.mkdir(parents=True)
    root_db.write_text("", encoding="utf-8")
    work_db.write_text("", encoding="utf-8")
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default", raising=False)
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)

    rows = adapter.load_projects_from_db(profile_name="work")

    assert rows == [
        {
            "project_id": "profile-project",
            "name": "Profile Project",
            "color": None,
            "profile": "work",
        }
    ]


def test_projects_db_adapter_uses_read_only_sqlite_uri(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    fake_projects = [
        types.SimpleNamespace(slug="db", name="DB", color=None, archived=False)
    ]
    _install_fake_projects_db(monkeypatch, projects=fake_projects)
    db_file = _profile_home(tmp_path, "work") / "projects.db"
    db_file.parent.mkdir(parents=True)
    db_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)
    real_connect = sqlite3.connect
    seen = {}

    def _connect(database, *args, **kwargs):
        seen["database"] = database
        seen["uri"] = kwargs.get("uri")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(adapter.sqlite3, "connect", _connect)

    assert adapter.load_projects_from_db(profile_name="work")
    assert seen["uri"] is True
    assert seen["database"].startswith("file:///")
    assert seen["database"].endswith("?mode=ro&immutable=1")


def test_projects_db_adapter_uses_wal_aware_read_only_uri(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    fake_projects = [
        types.SimpleNamespace(slug="db", name="DB", color=None, archived=False)
    ]
    _install_fake_projects_db(monkeypatch, projects=fake_projects)
    db_file = _profile_home(tmp_path, "work") / "projects.db"
    db_file.parent.mkdir(parents=True)
    db_file.write_text("", encoding="utf-8")
    db_file.with_name("projects.db-wal").write_text("", encoding="utf-8")
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)
    real_connect = sqlite3.connect
    seen = {}

    def _connect(database, *args, **kwargs):
        seen["database"] = database
        seen["uri"] = kwargs.get("uri")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(adapter.sqlite3, "connect", _connect)

    assert adapter.load_projects_from_db(profile_name="work")
    assert seen["uri"] is True
    assert seen["database"].startswith("file:///")
    assert seen["database"].endswith("?mode=ro")


def test_projects_db_adapter_reads_live_wal_rows(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    db_file = _profile_home(tmp_path, "work") / "projects.db"
    db_file.parent.mkdir(parents=True)
    writer = sqlite3.connect(db_file)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE projects (slug TEXT, name TEXT, color TEXT)")
        writer.execute(
            "INSERT INTO projects (slug, name, color) VALUES (?, ?, ?)",
            ("wal-project", "WAL Project", "#123456"),
        )
        writer.commit()
        assert db_file.with_name("projects.db-wal").exists()

        module = types.ModuleType("hermes_cli.projects_db")

        def _connect():
            raise AssertionError("read-only adapter must not call projects_db.connect()")

        def _list_projects(conn):
            return [
                types.SimpleNamespace(
                    slug=row["slug"],
                    name=row["name"],
                    color=row["color"],
                    archived=False,
                )
                for row in conn.execute("SELECT slug, name, color FROM projects")
            ]

        module.connect = _connect
        module.list_projects = _list_projects
        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        package.projects_db = module
        monkeypatch.setitem(sys.modules, "hermes_cli", package)
        monkeypatch.setitem(sys.modules, "hermes_cli.projects_db", module)
        monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)

        rows = adapter.load_projects_from_db(profile_name="work")
    finally:
        writer.close()

    assert rows == [
        {
            "project_id": "wal-project",
            "name": "WAL Project",
            "color": "#123456",
            "profile": "work",
        }
    ]


def test_projects_db_adapter_retries_without_immutable_when_wal_appears(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    fake_projects = [
        types.SimpleNamespace(slug="db", name="DB", color=None, archived=False)
    ]
    _install_fake_projects_db(monkeypatch, projects=fake_projects)
    db_file = _profile_home(tmp_path, "work") / "projects.db"
    db_file.parent.mkdir(parents=True)
    db_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)
    real_connect = sqlite3.connect
    seen = []

    def _connect(database, *args, **kwargs):
        seen.append(database)
        if database.endswith("?mode=ro&immutable=1"):
            db_file.with_name("projects.db-wal").write_text("", encoding="utf-8")
            raise sqlite3.OperationalError("no such table: projects")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(adapter.sqlite3, "connect", _connect)

    assert adapter.load_projects_from_db(profile_name="work")
    assert seen[0].endswith("?mode=ro&immutable=1")
    assert seen[1].endswith("?mode=ro")


def test_projects_db_adapter_read_does_not_create_sqlite_sidecars(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    fake_projects = [
        types.SimpleNamespace(slug="db", name="DB", color=None, archived=False)
    ]
    _install_fake_projects_db(monkeypatch, projects=fake_projects)
    db_file = _profile_home(tmp_path, "work") / "projects.db"
    db_file.parent.mkdir(parents=True)
    setup_conn = sqlite3.connect(db_file)
    setup_conn.execute("PRAGMA journal_mode=WAL")
    setup_conn.close()
    db_file.with_name("projects.db-wal").unlink(missing_ok=True)
    db_file.with_name("projects.db-shm").unlink(missing_ok=True)
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)

    rows = adapter.load_projects_from_db(profile_name="work")

    assert rows == [{"project_id": "db", "name": "DB", "color": None, "profile": "work"}]
    assert not db_file.with_name("projects.db-wal").exists()
    assert not db_file.with_name("projects.db-shm").exists()


def test_load_projects_uses_db_rows_when_projects_json_is_absent_for_read_only_callers(monkeypatch, tmp_path):
    import api.models as models

    monkeypatch.setattr(models, "PROJECTS_FILE", tmp_path / "missing-projects.json", raising=False)
    monkeypatch.setattr("api.projects_db_adapter.load_projects_from_db", lambda **_kwargs: [{"project_id": "db", "name": "DB", "color": None, "profile": "work"}], raising=False)

    rows = models.load_projects(include_db=True)

    assert rows == [{"project_id": "db", "name": "DB", "color": None, "profile": "work"}]


def test_load_projects_default_keeps_db_rows_out_of_json_mutation_path(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "missing-projects.json"
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr("api.projects_db_adapter.load_projects_from_db", lambda **_kwargs: [{"project_id": "db", "name": "DB", "color": None, "profile": "work"}], raising=False)

    projects = models.load_projects()
    projects.append({"project_id": "json", "name": "JSON", "profile": "default"})
    models.save_projects(projects)

    assert json.loads(projects_file.read_text(encoding="utf-8")) == [{"project_id": "json", "name": "JSON", "profile": "default"}]


def test_load_projects_default_keeps_existing_json_authoritative_over_db_rows(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps([{"project_id": "json", "name": "JSON", "profile": "default"}]), encoding="utf-8")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr("api.projects_db_adapter.load_projects_from_db", lambda **_kwargs: [{"project_id": "db", "name": "DB", "color": None, "profile": "work"}], raising=False)
    monkeypatch.setattr(models, "_projects_migrated", True, raising=False)

    rows = models.load_projects(_migrate=False)

    assert rows == [{"project_id": "json", "name": "JSON", "profile": "default"}]


def test_load_projects_read_only_merges_db_rows_when_json_exists(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps([{"project_id": "json", "name": "JSON", "profile": "default"}]), encoding="utf-8")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr("api.projects_db_adapter.load_projects_from_db", lambda **_kwargs: [
        {"project_id": "json", "name": "DB Shadow", "color": None, "profile": "work"},
        {"project_id": "db", "name": "DB", "color": None, "profile": "work"},
    ], raising=False)
    monkeypatch.setattr(models, "_projects_migrated", True, raising=False)

    rows = models.load_projects(_migrate=False, include_db=True)

    assert rows == [
        {"project_id": "json", "name": "JSON", "profile": "default"},
        {"project_id": "json", "name": "DB Shadow", "color": None, "profile": "work"},
        {"project_id": "db", "name": "DB", "color": None, "profile": "work"},
    ]


def test_load_projects_falls_back_to_json_when_db_unavailable(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps([{"project_id": "json", "name": "JSON", "profile": "default"}]), encoding="utf-8")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr("api.projects_db_adapter.load_projects_from_db", lambda **_kwargs: None, raising=False)
    monkeypatch.setattr(models, "_projects_migrated", True, raising=False)

    rows = models.load_projects(_migrate=False)

    assert rows == [{"project_id": "json", "name": "JSON", "profile": "default"}]


def test_load_projects_falls_back_to_json_when_db_has_no_rows(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps([{"project_id": "json", "name": "JSON", "profile": "default"}]), encoding="utf-8")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr("api.projects_db_adapter.load_projects_from_db", lambda **_kwargs: None, raising=False)
    monkeypatch.setattr(models, "_projects_migrated", True, raising=False)

    rows = models.load_projects(_migrate=False)

    assert rows == [{"project_id": "json", "name": "JSON", "profile": "default"}]


def test_load_projects_backfills_legacy_profile_rows_when_json_fallback_runs(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "session_index.json"
    projects_file.write_text(
        json.dumps([{"project_id": "legacy-project", "name": "Legacy Project"}]),
        encoding="utf-8",
    )
    index_file.write_text(
        json.dumps([{"project_id": "legacy-project", "profile": "work"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr("api.projects_db_adapter.load_projects_from_db", lambda **_kwargs: None, raising=False)
    monkeypatch.setattr(models, "_projects_migrated", False, raising=False)

    rows = models.load_projects()

    assert rows == [{"project_id": "legacy-project", "name": "Legacy Project", "profile": "work"}]
    assert json.loads(projects_file.read_text(encoding="utf-8")) == rows


def test_save_projects_remains_json_backed(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)

    models.save_projects([{"project_id": "json", "name": "JSON", "profile": "default"}])

    assert json.loads(projects_file.read_text(encoding="utf-8")) == [
        {"project_id": "json", "name": "JSON", "profile": "default"}
    ]


def test_load_projects_passes_profile_name_to_adapter(monkeypatch, tmp_path):
    import api.models as models

    calls = []
    monkeypatch.setattr(models, "PROJECTS_FILE", tmp_path / "missing-projects.json", raising=False)
    monkeypatch.setattr(
        "api.projects_db_adapter.load_projects_from_db",
        lambda **kwargs: calls.append(kwargs) or [],
        raising=False,
    )

    assert models.load_projects(include_db=True, profile_name="work") == []
    assert calls == [{"profile_name": "work"}]


def test_load_projects_for_profiles_merges_db_rows_by_profile(monkeypatch, tmp_path):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps(
            [
                {
                    "project_id": "shared",
                    "name": "JSON Work",
                    "profile": "work",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file, raising=False)
    monkeypatch.setattr(models, "_projects_migrated", True, raising=False)

    def _fake_db_projects(**kwargs):
        profile = kwargs["profile_name"]
        return [
            {"project_id": "shared", "name": f"{profile} Shared", "profile": profile},
            {"project_id": f"{profile}-db", "name": f"{profile} DB", "profile": profile},
        ]

    monkeypatch.setattr(
        "api.projects_db_adapter.load_projects_from_db",
        _fake_db_projects,
        raising=False,
    )

    rows = models.load_projects_for_profiles(["work", "research"])

    assert rows == [
        {"project_id": "shared", "name": "JSON Work", "profile": "work"},
        {"project_id": "work-db", "name": "work DB", "profile": "work"},
        {"project_id": "shared", "name": "research Shared", "profile": "research"},
        {"project_id": "research-db", "name": "research DB", "profile": "research"},
    ]


def test_project_identity_matches_requires_profile_and_project_id():
    import api.models as models

    assert models.project_identity_matches(
        {"project_id": "shared", "profile": "work"},
        "shared",
        "work",
    )
    assert not models.project_identity_matches(
        {"project_id": "shared", "profile": "default"},
        "shared",
        "work",
    )
    assert not models.project_identity_matches(
        {"project_id": "other", "profile": "work"},
        "shared",
        "work",
    )


def test_routes_pass_active_and_session_profiles_to_project_reads(monkeypatch):
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    calls = []
    all_profile_calls = []

    def _fake_load_projects(**kwargs):
        calls.append(kwargs)
        profile = kwargs.get("profile_name") or "default"
        return [
            {
                "project_id": f"{profile}-project",
                "name": "Wrong Profile Project",
                "profile": "default",
            },
            {
                "project_id": f"{profile}-project",
                "name": "Project",
                "profile": profile,
            }
        ]

    monkeypatch.setattr(routes, "load_projects", _fake_load_projects)
    monkeypatch.setattr(
        profiles,
        "get_active_profile_name",
        lambda: "work",
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, **_kwargs: payload,
        raising=False,
    )

    projects_payload = routes.handle_get(object(), urlparse("/api/projects"))

    assert projects_payload["projects"][0]["project_id"] == "work-project"
    assert calls == [{"include_db": True, "profile_name": "work"}]

    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "work"}, {"name": "research"}],
    )
    monkeypatch.setattr(
        models,
        "load_projects_for_profiles",
        lambda names: all_profile_calls.append(list(names)) or [
            {"project_id": "work-project", "name": "Project", "profile": "work"},
            {"project_id": "research-project", "name": "Project", "profile": "research"},
        ],
    )

    all_payload = routes.handle_get(object(), urlparse("/api/projects?all_profiles=1"))

    assert [p["project_id"] for p in all_payload["projects"]] == [
        "work-project",
        "research-project",
    ]
    assert all_profile_calls == [["work", "research"]]

    session = types.SimpleNamespace(
        profile="research",
        project_id=None,
        session_id="sid",
        save=lambda: None,
        compact=lambda: {"session_id": "sid", "project_id": "research-project"},
    )
    lock = types.SimpleNamespace(
        acquire=lambda timeout=None: True,
        release=lambda: None,
    )
    calls.clear()
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "sid", "project_id": "research-project"})
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "active")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)

    move_payload = routes.handle_post(object(), urlparse("/api/session/move"))

    assert move_payload["ok"] is True
    assert session.project_id == "research-project"
    assert calls == [{"include_db": True, "profile_name": "research"}]


def test_routes_project_rename_and_delete_use_profile_identity(monkeypatch):
    import api.routes as routes

    saved = []
    projects = [
        {"project_id": "shared", "name": "Default", "profile": "default"},
        {"project_id": "shared", "name": "Work", "profile": "work"},
    ]

    monkeypatch.setattr(routes, "load_projects", lambda: list(projects))
    monkeypatch.setattr(routes, "save_projects", lambda rows: saved.append(list(rows)))
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload, raising=False)

    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"project_id": "shared", "name": "Renamed"},
    )
    rename_payload = routes.handle_post(object(), urlparse("/api/projects/rename"))

    assert rename_payload["project"] == {"project_id": "shared", "name": "Renamed", "profile": "work"}
    assert saved[-1] == [
        {"project_id": "shared", "name": "Default", "profile": "default"},
        {"project_id": "shared", "name": "Renamed", "profile": "work"},
    ]

    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"project_id": "shared"},
    )
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", Path("missing-index.json"))
    delete_payload = routes.handle_post(object(), urlparse("/api/projects/delete"))

    assert delete_payload["ok"] is True
    assert saved[-1] == [
        {"project_id": "shared", "name": "Default", "profile": "default"},
    ]


def test_mcp_list_and_move_use_active_profile_db_rows(monkeypatch):
    pytest.importorskip("mcp", reason="mcp package not installed")
    import mcp_server

    calls = []

    def _fake_load_projects(**kwargs):
        calls.append(kwargs)
        profile = kwargs.get("profile_name") or "default"
        return [
            {
                "project_id": f"{profile}-project",
                "name": "Wrong Profile Project",
                "profile": "default",
            },
            {
                "project_id": f"{profile}-project",
                "name": "Project",
                "profile": profile,
            }
        ]

    monkeypatch.setattr(mcp_server, "_active_profile", lambda: "work")
    monkeypatch.setattr(mcp_server, "load_projects", _fake_load_projects)
    monkeypatch.setattr(
        mcp_server,
        "_load_index",
        lambda: [
            {"project_id": "work-project", "profile": "default"},
            {"project_id": "work-project", "profile": "work"},
        ],
    )

    listed = asyncio.run(mcp_server.handle_list_projects({}))
    listed_payload = json.loads(listed[0].text)

    assert listed_payload == [
        {
            "project_id": "work-project",
            "name": "Project",
            "profile": "work",
            "session_count": 1,
        }
    ]
    assert calls == [{"include_db": True, "profile_name": "work"}]

    calls.clear()
    posted = []
    monkeypatch.setattr(
        mcp_server,
        "_api_post",
        lambda endpoint, body: posted.append((endpoint, body)) or {
            "session": {"title": "Moved"}
        },
    )

    moved = asyncio.run(
        mcp_server.handle_move_session(
            {"session_id": "sid", "project_id": "work-project"}
        )
    )
    moved_payload = json.loads(moved[0].text)

    assert moved_payload["ok"] is True
    assert calls == [{"include_db": True, "profile_name": "work"}]
    assert posted == [
        ("/api/session/move", {"session_id": "sid", "project_id": "work-project"})
    ]


def test_mcp_project_rename_and_delete_use_profile_identity(monkeypatch):
    pytest.importorskip("mcp", reason="mcp package not installed")
    import mcp_server

    saved = []
    projects = [
        {"project_id": "shared", "name": "Default", "profile": "default"},
        {"project_id": "shared", "name": "Work", "profile": "work"},
    ]

    monkeypatch.setattr(mcp_server, "_active_profile", lambda: "work")
    monkeypatch.setattr(mcp_server, "load_projects", lambda: list(projects))
    monkeypatch.setattr(mcp_server, "save_projects", lambda rows: saved.append(list(rows)))
    monkeypatch.setattr(mcp_server, "SESSION_DIR", Path("missing-sessions"))

    renamed = asyncio.run(
        mcp_server.handle_rename_project(
            {"project_id": "shared", "name": "Renamed"}
        )
    )
    renamed_payload = json.loads(renamed[0].text)

    assert renamed_payload == {"project_id": "shared", "name": "Renamed", "profile": "work"}
    assert saved[-1] == [
        {"project_id": "shared", "name": "Default", "profile": "default"},
        {"project_id": "shared", "name": "Renamed", "profile": "work"},
    ]

    deleted = asyncio.run(mcp_server.handle_delete_project({"project_id": "shared"}))
    deleted_payload = json.loads(deleted[0].text)

    assert deleted_payload["ok"] is True
    assert saved[-1] == [
        {"project_id": "shared", "name": "Default", "profile": "default"},
    ]


def test_load_projects_handles_missing_db_and_read_errors(monkeypatch, tmp_path):
    import api.projects_db_adapter as adapter

    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda name: _profile_home(tmp_path, name), raising=False)
    _install_fake_projects_db(monkeypatch, projects=[])
    missing_home = _profile_home(tmp_path, "work")
    rows = adapter.load_projects_from_db(profile_name="work")
    assert rows is None
    assert not missing_home.exists()

    fake_db = missing_home / "projects.db"
    fake_db.parent.mkdir(parents=True)
    fake_db.write_text("", encoding="utf-8")
    _install_fake_projects_db(monkeypatch, projects=[])
    assert adapter.load_projects_from_db(profile_name="work") == []

    _install_fake_projects_db(
        monkeypatch,
        projects=[],
        list_error=sqlite3.OperationalError("missing table"),
    )
    assert adapter.load_projects_from_db(profile_name="work") is None
