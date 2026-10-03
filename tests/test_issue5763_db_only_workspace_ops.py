"""Route-level tests for #5763 follow-up review: DB-only projects (created in
projects.db by Desktop/CLI, absent from workspaces.json) appear in the picker
via the read bridge, so trust resolution, rename, and reorder must all handle
them. Each test uses a DB-only project OUTSIDE the user's home directory.
"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.projects_bridge import _cache
from api.workspace import resolve_trusted_workspace


def _make_projects_db(home: Path, projects: list[dict]) -> Path:
    db = home / "projects.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            description TEXT, icon TEXT, color TEXT, board_slug TEXT,
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


def _make_handler():
    h = MagicMock()
    h.wfile = MagicMock()
    return h


def _response(handler):
    body = b"".join(c.args[0] for c in handler.wfile.write.call_args_list)
    return json.loads(body)


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    _cache.clear()
    yield
    _cache.clear()


@pytest.fixture()
def db_only(tmp_path, monkeypatch):
    """A projects.db in a fake profile home + a DB-only project dir outside home.

    tmp_path is under /tmp (never under the user's home), so the DB-only path
    exercises the (B)/(B2) trust branches rather than the under-home shortcut.
    """
    outside = tmp_path / "srv" / "dbonly-project"
    outside.mkdir(parents=True)
    _make_projects_db(tmp_path, [
        {"id": "p_db1", "slug": "db-only", "name": "DB Only", "folders": [str(outside)]},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    return outside


# ── Fix 3: resolve_trusted_workspace accepts DB-only project paths ─────────


def test_trust_accepts_db_only_project_outside_home(db_only):
    assert resolve_trusted_workspace(str(db_only)) == db_only


def test_trust_still_rejects_unknown_path_outside_home(tmp_path, monkeypatch):
    stranger = tmp_path / "srv" / "not-a-project"
    stranger.mkdir(parents=True)
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    _make_projects_db(tmp_path, [])
    with pytest.raises(ValueError):
        resolve_trusted_workspace(str(stranger))


def test_trust_rejects_archived_project(tmp_path, monkeypatch):
    gone = tmp_path / "srv" / "archived-project"
    gone.mkdir(parents=True)
    _make_projects_db(tmp_path, [
        {"id": "p_a", "slug": "arch", "name": "Arch", "folders": [str(gone)], "archived": 1},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    with pytest.raises(ValueError):
        resolve_trusted_workspace(str(gone))


# ── Fix 4: rename works on DB-only entries ─────────────────────────────────


def test_rename_db_only_project_succeeds(db_only):
    from api.routes import _handle_workspace_rename
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: wss):
        _handle_workspace_rename(handler, {"path": str(db_only), "name": "Renamed DB"})
    handler.send_response.assert_called_once_with(200)
    resp = _response(handler)
    assert resp["ok"] is True
    entry = next(w for w in resp["workspaces"] if w["path"] == str(db_only))
    assert entry["name"] == "Renamed DB"
    # The DB itself carries the new name (authoritative for the path it owns).
    conn = sqlite3.connect(db_only.parent.parent / "projects.db")
    try:
        assert conn.execute("SELECT name FROM projects WHERE id='p_db1'").fetchone()[0] == "Renamed DB"
    finally:
        conn.close()


def test_rename_unknown_path_still_404(tmp_path, monkeypatch):
    from api.routes import _handle_workspace_rename
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _make_projects_db(tmp_path, [])
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: wss):
        _handle_workspace_rename(handler, {"path": "/no/such/path", "name": "X"})
    handler.send_response.assert_called_once_with(404)


# ── Fix 1: fresh-profile create_project route (no pre-existing projects.db) ─


def test_create_project_route_fresh_profile_succeeds(tmp_path, monkeypatch):
    # Reviewer blocker: default New Workspace flow on a profile that has never
    # run Desktop/CLI. With the box checked and NO projects.db, the native
    # manager must initialize the DB and complete the whole operation.
    from api.routes import _handle_workspace_create_project
    fresh_home = tmp_path / "profile"
    fresh_home.mkdir()
    target = tmp_path / "srv" / "brand-new"
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: fresh_home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", wss)):
        _handle_workspace_create_project(handler, {"path": str(target), "name": "Brand New", "create": True})
    handler.send_response.assert_called_once_with(200)
    resp = _response(handler)
    assert resp.get("ok") is True
    assert resp.get("project", {}).get("created") is True
    assert target.is_dir()
    assert (fresh_home / "projects.db").is_file()
    assert any(w["path"] == str(target) for w in saved.get("wss", []))


def test_create_project_route_no_manager_fails_before_side_effects(tmp_path, monkeypatch):
    # No native Projects manager reachable: the route must fail BEFORE mkdir
    # and before touching the local workspace list (no orphan directory).
    from api.routes import _handle_workspace_create_project
    fresh_home = tmp_path / "profile"
    fresh_home.mkdir()
    target = tmp_path / "srv" / "orphan-me"
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: fresh_home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    handler = _make_handler()
    with patch("api.projects_bridge.projects_write_supported", return_value=False), \
         patch("api.routes.load_workspaces", return_value=[]) as mock_load, \
         patch("api.routes.save_workspaces", side_effect=AssertionError("must not save")):
        _handle_workspace_create_project(handler, {"path": str(target), "name": "X", "create": True})
    handler.send_response.assert_called_once_with(400)
    assert not target.exists()


# ── Fix 5: reorder persists DB-only entries, returns merged ordered list ──


def test_reorder_with_db_only_first_persists_and_returns_it(db_only):
    from api.routes import _handle_workspace_reorder
    local = {"path": "/home/user/local", "name": "Local"}
    handler = _make_handler()
    saved = {}
    def _save(wss, **kw):
        saved["wss"] = wss
    with patch("api.routes.load_workspaces", return_value=[local]), \
         patch("api.routes.save_workspaces", side_effect=_save):
        _handle_workspace_reorder(handler, {"paths": [str(db_only), "/home/user/local"]})
    handler.send_response.assert_called_once_with(200)
    # Persisted: the DB-only entry got a local row in the given position.
    assert saved["wss"][0]["path"] == str(db_only)
    assert saved["wss"][1]["path"] == "/home/user/local"
    # Response: merged ordered list contains the DB-only entry (not dropped).
    resp = _response(handler)
    paths = [w["path"] for w in resp["workspaces"]]
    assert paths[0] == str(db_only)
    assert "/home/user/local" in paths


def test_reorder_omitting_db_only_entry_still_returns_it(db_only):
    from api.routes import _handle_workspace_reorder
    a = {"path": "/home/user/a", "name": "A"}
    b = {"path": "/home/user/b", "name": "B"}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[a, b]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: wss):
        _handle_workspace_reorder(handler, {"paths": ["/home/user/b", "/home/user/a"]})
    resp = _response(handler)
    paths = [w["path"] for w in resp["workspaces"]]
    # DB-only entry not mentioned in the request still appears (merged view).
    assert str(db_only) in paths
