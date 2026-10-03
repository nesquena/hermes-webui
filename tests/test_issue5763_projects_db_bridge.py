"""Tests for issue #5763 read slice — projects.db -> workspace list bridge.

The bridge must be read-only and fail-safe: a present projects.db surfaces
Hermes Projects in the workspace list; a missing/corrupt DB leaves the
workspaces.json behavior exactly as before.
"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from api.projects_bridge import (
    load_hermes_project_workspaces,
    merge_hermes_projects,
    _cache,
)


def _make_projects_db(home: Path, projects: list[dict]) -> Path:
    """Create a minimal projects.db matching the upstream schema.

    Uses WAL journal mode to match production: upstream projects_db.open_db
    sets journal_mode=wal, and the bridge's cache-invalidation contract must
    be tested against that mode, not DELETE.
    """
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


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    _cache.clear()
    yield
    _cache.clear()


def test_no_db_returns_empty(tmp_path):
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []


def test_projects_surface_with_names(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "gufo", "name": "Gufo", "folders": ["/srv/gufo"]},
        {"id": "p2", "slug": "omp", "name": "Oh My Pi", "folders": ["/srv/omp"]},
    ])
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert [(e["path"], e["name"]) for e in entries] == [
        ("/srv/gufo", "Gufo"), ("/srv/omp", "Oh My Pi"),
    ]
    assert all(e["source"] == "hermes_project" for e in entries)


def test_archived_and_folderless_projects_skipped(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Archived", "folders": ["/srv/a"], "archived": 1},
        {"id": "p2", "slug": "b", "name": "Folderless", "folders": []},
        {"id": "p3", "slug": "c", "name": "Real", "folders": ["/srv/c"]},
    ])
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert [(e["path"], e["name"]) for e in entries] == [("/srv/c", "Real")]


def test_non_primary_folder_used_when_no_primary(tmp_path):
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "x", "name": "X", "folders": ["/srv/first", "/srv/second"]},
    ])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE project_folders SET is_primary = 0")
    conn.commit()
    conn.close()
    _cache.clear()
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert entries[0]["path"] == "/srv/first"


def test_corrupt_db_fails_safe(tmp_path):
    (tmp_path / "projects.db").write_text("not a database")
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []


def test_cache_invalidates_on_mtime(tmp_path):
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    assert len(load_hermes_project_workspaces(profile_home=tmp_path)) == 1
    # Add a project without clearing the cache; bump mtime explicitly since
    # same-second writes can leave st_mtime unchanged on coarse filesystems.
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO projects (id, slug, name, created_at) VALUES ('p2','b','B',1001)")
    conn.execute("INSERT INTO project_folders VALUES ('p2','/srv/b',NULL,1,2001)")
    conn.commit()
    conn.close()
    future = time.time() + 5
    import os
    os.utime(db, (future, future))
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert {(e["path"], e["name"]) for e in entries} == {
        ("/srv/a", "A"), ("/srv/b", "B"),
    }


def test_cache_invalidates_with_open_wal_writer(tmp_path):
    """Regression: production projects.db is WAL. A long-lived external writer
    (Desktop/gateway) commits into the -wal sidecar; the main DB mtime stays
    stale until a checkpoint. The cache key must fold in the WAL mtime or
    external creates are invisible indefinitely."""
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    assert len(load_hermes_project_workspaces(profile_home=tmp_path)) == 1  # populate cache
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    try:
        writer.execute("INSERT INTO projects (id, slug, name, created_at) VALUES ('p2','b','B',1001)")
        writer.execute("INSERT INTO project_folders VALUES ('p2','/srv/b',NULL,1,2001)")
        writer.commit()  # committed into -wal; main DB untouched, writer stays open
        entries = load_hermes_project_workspaces(profile_home=tmp_path)
        assert {(e["path"], e["name"]) for e in entries} == {
            ("/srv/a", "A"), ("/srv/b", "B"),
        }, "external WAL commit must invalidate the cache without a checkpoint"
    finally:
        writer.close()


def test_merge_renames_local_entry_and_appends_new(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "gufo", "name": "Gufo", "folders": ["/srv/gufo"]},
        {"id": "p2", "slug": "omp", "name": "Oh My Pi", "folders": ["/srv/omp"]},
    ])
    local = [
        {"path": "/home/user/workspace", "name": "Home"},
        {"path": "/srv/gufo", "name": "gufo-dir"},  # local label loses to project name
    ]
    merged = merge_hermes_projects(local, profile_home=tmp_path)
    assert [(e["path"], e["name"]) for e in merged] == [
        ("/home/user/workspace", "Home"),
        ("/srv/gufo", "Gufo"),
        ("/srv/omp", "Oh My Pi"),
    ]
    # Input list is never mutated
    assert local[1]["name"] == "gufo-dir"


def test_merge_without_db_is_identity(tmp_path):
    local = [{"path": "/srv/x", "name": "X"}]
    assert merge_hermes_projects(local, profile_home=tmp_path) == local


def test_env_kill_switch(tmp_path, monkeypatch):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "1")
    assert len(load_hermes_project_workspaces(profile_home=tmp_path)) == 1


# ── Write path: create_hermes_project ───────────────────────────────────────

from api.projects_bridge import create_hermes_project


def test_create_project_writes_db_and_visible_on_read(tmp_path):
    _make_projects_db(tmp_path, [])
    # Populate the read cache BEFORE create, so the assertion below actually
    # proves create invalidates it — on an empty cache the read passes on a
    # plain miss even if _invalidate_cache were deleted.
    assert load_hermes_project_workspaces(profile_home=tmp_path) == []
    result = create_hermes_project("/srv/newproj", "New Project", profile_home=tmp_path)
    assert result["created"] is True
    assert result["name"] == "New Project"
    assert result["id"].startswith("p_")
    # Immediately visible through the read bridge (cache invalidated by create)
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert ("/srv/newproj", "New Project") in {(e["path"], e["name"]) for e in entries}


def test_create_project_slug_and_primary(tmp_path):
    db = _make_projects_db(tmp_path, [])
    create_hermes_project("/srv/My Cool App/", "My Cool App", profile_home=tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, slug, primary_path FROM projects").fetchone()
    assert row["slug"] == "my-cool-app"
    assert row["primary_path"] == "/srv/My Cool App"  # trailing sep stripped
    folder = conn.execute(
        "SELECT path, is_primary FROM project_folders WHERE project_id = ?", (row["id"],)
    ).fetchone()
    assert folder["path"] == "/srv/My Cool App" and folder["is_primary"] == 1
    conn.close()


def test_create_project_duplicate_slug_gets_suffix(tmp_path):
    db = _make_projects_db(tmp_path, [])
    create_hermes_project("/srv/a", "Dup Name", profile_home=tmp_path)
    create_hermes_project("/srv/b", "Dup Name", profile_home=tmp_path)
    conn = sqlite3.connect(db)
    slugs = [r[0] for r in conn.execute("SELECT slug FROM projects ORDER BY slug")]
    conn.close()
    assert slugs == ["dup-name", "dup-name-2"]


def test_create_project_duplicate_path_raises(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "existing", "name": "Existing", "folders": ["/srv/x"]},
    ])
    with pytest.raises(ValueError, match="already belongs"):
        create_hermes_project("/srv/x", "Clash", profile_home=tmp_path)


def test_create_project_empty_name_raises(tmp_path):
    _make_projects_db(tmp_path, [])
    with pytest.raises(ValueError):
        create_hermes_project("/srv/x", "  ", profile_home=tmp_path)


def test_create_project_initializes_missing_db(tmp_path):
    # Fresh profile (no projects.db): the native manager initializes the DB +
    # schema on connect, so registration must succeed rather than reject.
    result = create_hermes_project("/srv/x", "X", profile_home=tmp_path)
    assert result["created"] is True
    assert (tmp_path / "projects.db").is_file()
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert ("/srv/x", "X") in {(e["path"], e["name"]) for e in entries}


def test_create_project_missing_db_parent_dir_fails_clean(tmp_path):
    # A profile home that cannot hold a DB (parent is a file) must surface as
    # RuntimeError, not create anything.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(RuntimeError):
        create_hermes_project("/srv/x", "X", profile_home=blocker / "sub")
    assert not (blocker / "sub").exists()


# ── Subprocess fallback: no local reimplementation of create_project ───────

AGENT_CHECKOUT = Path.home() / ".hermes" / "hermes-agent"


def test_create_project_subprocess_fallback(tmp_path, monkeypatch):
    """With hermes_cli un-importable in-process, creation must still go
    through upstream projects_db — via subprocess against the agent checkout."""
    if not (AGENT_CHECKOUT / "hermes_cli" / "projects_db.py").exists():
        pytest.skip("agent checkout with hermes_cli not present")
    db = _make_projects_db(tmp_path, [])
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    monkeypatch.setattr("api.projects_bridge._agent_dir", lambda: AGENT_CHECKOUT)
    result = create_hermes_project("/srv/subproc", "Subproc Project", profile_home=tmp_path)
    assert result["created"] is True
    assert result["slug"] == "subproc-project"
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT name, primary_path FROM projects").fetchone()
    conn.close()
    assert row == ("Subproc Project", "/srv/subproc")


def test_create_project_subprocess_fallback_duplicate(tmp_path, monkeypatch):
    if not (AGENT_CHECKOUT / "hermes_cli" / "projects_db.py").exists():
        pytest.skip("agent checkout with hermes_cli not present")
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "existing", "name": "Existing", "folders": ["/srv/x"]},
    ])
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    monkeypatch.setattr("api.projects_bridge._agent_dir", lambda: AGENT_CHECKOUT)
    # The duplicate-path rejection comes from upstream create_project itself,
    # surfaced through the subprocess payload.
    with pytest.raises(ValueError, match="already belongs"):
        create_hermes_project("/srv/x", "Clash", profile_home=tmp_path)


def test_create_project_subprocess_no_agent_dir(tmp_path, monkeypatch):
    _make_projects_db(tmp_path, [])
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    monkeypatch.setattr("api.projects_bridge._agent_dir", lambda: None)
    with pytest.raises(RuntimeError, match="cannot register project"):
        create_hermes_project("/srv/x", "X", profile_home=tmp_path)


# ── Write path: archive on workspace removal ─────────────────────────────────
#
# The read bridge made projects.db authoritative for the picker list, so
# removing only the local workspace let the entry reappear on the next poll.
# archive_hermes_project must soft-delete (archive) the owning DB project so
# the delete sticks, and must never raise — the local removal already landed.

from api.projects_bridge import archive_hermes_project


def test_archive_hides_project_from_listing(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "gufo", "name": "Gufo", "folders": ["/srv/gufo"]},
        {"id": "p2", "slug": "omp", "name": "Oh My Pi", "folders": ["/srv/omp"]},
    ])
    result = archive_hermes_project("/srv/gufo", profile_home=tmp_path)
    assert result.get("archived") is True
    assert result.get("id") == "p1"
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert [e["path"] for e in entries] == ["/srv/omp"]


def test_archive_is_idempotent(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    assert archive_hermes_project("/srv/a", profile_home=tmp_path).get("archived") is True
    # Second removal of the same path: already archived (find_by_primary_path
    # excludes archived rows) -> no-op, still never raises.
    second = archive_hermes_project("/srv/a", profile_home=tmp_path)
    assert second.get("archived") is False


def test_archive_unknown_path_is_noop(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    result = archive_hermes_project("/srv/nope", profile_home=tmp_path)
    assert result.get("archived") is False
    assert load_hermes_project_workspaces(profile_home=tmp_path) != []


def test_archive_without_db_is_noop(tmp_path):
    result = archive_hermes_project("/srv/a", profile_home=tmp_path)
    assert result == {"archived": False, "reason": "no-db"}


def test_query_handles_hash_in_db_path(tmp_path):
    """A profile home containing '#' must not silently disable the bridge:
    the raw f-string URI turned '#' into a fragment and the open failed."""
    home = tmp_path / "we#ird"
    home.mkdir()
    _make_projects_db(home, [{"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]}])
    entries = load_hermes_project_workspaces(profile_home=home)
    assert [(e["path"], e["name"]) for e in entries] == [("/srv/a", "A")]


# ── Write path: rename_hermes_project ───────────────────────────────────────

from api.projects_bridge import rename_hermes_project


def test_rename_updates_db_name(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Old", "folders": ["/srv/a"]},
    ])
    result = rename_hermes_project("/srv/a", "New Name", profile_home=tmp_path)
    assert result.get("renamed") is True
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert [(e["path"], e["name"]) for e in entries] == [("/srv/a", "New Name")]


def test_rename_unknown_path_is_noop(tmp_path):
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    assert rename_hermes_project("/srv/nope", "X", profile_home=tmp_path).get("renamed") is False
    entries = load_hermes_project_workspaces(profile_home=tmp_path)
    assert entries[0]["name"] == "A"


def test_rename_without_db_is_noop(tmp_path):
    assert rename_hermes_project("/srv/a", "X", profile_home=tmp_path) == {
        "renamed": False, "reason": "no-db",
    }


def test_archive_subprocess_fallback(tmp_path, monkeypatch):
    """With hermes_cli un-importable in-process, archiving must still go
    through upstream projects_db — via subprocess against the agent checkout."""
    if not (AGENT_CHECKOUT / "hermes_cli" / "projects_db.py").exists():
        pytest.skip("agent checkout with hermes_cli not present")
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "A", "folders": ["/srv/a"]},
    ])
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    monkeypatch.setattr("api.projects_bridge._agent_dir", lambda: AGENT_CHECKOUT)
    result = archive_hermes_project("/srv/a", profile_home=tmp_path)
    assert result.get("archived") is True
    conn = sqlite3.connect(db)
    archived = conn.execute("SELECT archived FROM projects WHERE id='p1'").fetchone()[0]
    conn.close()
    assert archived == 1
