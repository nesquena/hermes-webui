"""DB-only native picker rows must be admitted to checkpoint list (#5763).

Maintainer 05:31: detail of a DB-only native project hits /api/rollback/list
and gets HTTP 400 "Workspace not in configured list" because
rollback._resolve_workspace only allowlists load_workspaces(). Share the
picker's validated local/native inventory; keep remote isolation and refuse
unregistered paths.
"""
import os
import sqlite3
from pathlib import Path

import pytest


def _make_projects_db(home: Path, projects: list[dict]) -> Path:
    db = home / "projects.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT, icon TEXT, color TEXT, board_slug,
            primary_path TEXT, created_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE project_folders (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
            added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path)
        );
        """
    )
    for i, p in enumerate(projects):
        conn.execute(
            "INSERT INTO projects (id, slug, name, created_at, archived) VALUES (?,?,?,?,?)",
            (p["id"], p["slug"], p["name"], 1000 + i, p.get("archived", 0)),
        )
        for j, folder in enumerate(p.get("folders", [])):
            conn.execute(
                "INSERT INTO project_folders (project_id, path, is_primary, added_at) VALUES (?,?,?,?)",
                (p["id"], folder, 1 if j == 0 else 0, 2000 + j),
            )
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    from api.projects_bridge import _cache
    _cache.clear()
    yield
    _cache.clear()


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    native = tmp_path / "native"
    native.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    stray = tmp_path / "stray"
    stray.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("api.workspace._remote_terminal_cwd", lambda profile=None: None)
    monkeypatch.setattr("api.workspace.load_workspaces", lambda profile=None: [])
    return {"home": home, "native": native, "local": local, "stray": stray}


def test_resolve_workspace_accepts_db_only_native(host):
    from api.rollback import _resolve_workspace

    _make_projects_db(host["home"], [
        {"id": "N1", "slug": "n1", "name": "Native", "folders": [str(host["native"])]},
    ])
    got = _resolve_workspace(str(host["native"]))
    assert os.path.realpath(got) == os.path.realpath(host["native"])


def test_resolve_workspace_accepts_persisted_local(host, monkeypatch):
    from api.rollback import _resolve_workspace

    monkeypatch.setattr(
        "api.workspace.load_workspaces",
        lambda profile=None: [{"path": str(host["local"]), "name": "Local"}],
    )
    got = _resolve_workspace(str(host["local"]))
    assert os.path.realpath(got) == os.path.realpath(host["local"])


def test_resolve_workspace_rejects_unregistered_sibling(host):
    from api.rollback import _resolve_workspace

    _make_projects_db(host["home"], [
        {"id": "N1", "slug": "n1", "name": "Native", "folders": [str(host["native"])]},
    ])
    with pytest.raises(ValueError, match="not in configured list"):
        _resolve_workspace(str(host["stray"]))


def test_resolve_workspace_rejects_remote_literal(host, monkeypatch):
    from api.rollback import _resolve_workspace

    remote = host["local"]
    monkeypatch.setattr("api.workspace._remote_terminal_cwd", lambda profile=None: str(remote))
    monkeypatch.setattr(
        "api.workspace.load_workspaces",
        lambda profile=None: [{"path": str(remote), "name": "RemoteWWW"}],
    )
    with pytest.raises(ValueError, match="not in configured list"):
        _resolve_workspace(str(remote))
