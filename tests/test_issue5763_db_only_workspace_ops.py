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



def _manager_available() -> bool:
    """A Hermes Projects manager is reachable (in-process import or agent
    checkout for the subprocess fallback). CI's tests.yml installs pip
    packages only — no agent checkout — so write-path tests must skip there
    (re-gate should-fix 5)."""
    from api.projects_bridge import _agent_dir, _projects_db_module
    return _projects_db_module() is not None or _agent_dir() is not None


requires_manager = pytest.mark.usefixtures("_requires_manager")


@pytest.fixture
def _requires_manager():
    if not _manager_available():
        pytest.skip("no Hermes Projects manager reachable (agent checkout absent)")


@pytest.fixture(autouse=True)
def _preimport_lazy_state_wal():
    """Upstream ``open_db`` imports ``hermes_state_wal`` lazily at connect()
    time; conftest's per-test sys.path restore can drop the agent dir
    between tests, so the lazy import fails mid-suite (passes alone, fails
    in a multi-file run). Pre-import it here — adding the agent dir to
    sys.path ourselves when a prior test stripped it — so the module cache
    answers the lazy import (re-gate should-fix 5). conftest restores
    sys.path after the test, so the append does not leak."""
    import sys
    try:
        import api.config  # noqa: F401  (normally appends the agent dir)
        from api.projects_bridge import _agent_dir
        d = _agent_dir()
        # A prior test's sys.path restore can strip the agent dir entirely —
        # re-add it for THIS test (conftest restores sys.path afterwards, so
        # the append never leaks) so hermes_cli AND its lazy hermes_state_wal
        # import both keep resolving mid-suite.
        if d is not None and str(d) not in sys.path:
            sys.path.append(str(d))
        import hermes_state_wal  # noqa: F401
    except Exception:
        pass
    yield

@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    _cache.clear()
    yield
    _cache.clear()


@pytest.fixture()
def db_only(tmp_path, monkeypatch):
    """A projects.db in a fake profile home + a DB-only project dir outside home.

    ``_home_path`` is pinned to an empty dir under tmp_path so the candidate
    is genuinely OUTSIDE home: pytest's tmp_path lives under $TMPDIR, which
    on some hosts (e.g. TMPDIR under $HOME) would otherwise make rule (A)
    ("under home is trusted") return the candidate and silently bypass the
    (B)/(B2) trust branches these tests exist to cover.
    """
    monkeypatch.setattr("api.workspace._home_path", lambda: tmp_path / "pinning-not-home")
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
    # Pin home (see db_only fixture): under a $TMPDIR inside $HOME, rule (A)
    # would trust tmp_path and the rejection would never be exercised.
    monkeypatch.setattr("api.workspace._home_path", lambda: tmp_path / "pinning-not-home")
    stranger = tmp_path / "srv" / "not-a-project"
    stranger.mkdir(parents=True)
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    _make_projects_db(tmp_path, [])
    with pytest.raises(ValueError):
        resolve_trusted_workspace(str(stranger))


def test_trust_rejects_archived_project(tmp_path, monkeypatch):
    monkeypatch.setattr("api.workspace._home_path", lambda: tmp_path / "pinning-not-home")
    gone = tmp_path / "srv" / "archived-project"
    gone.mkdir(parents=True)
    _make_projects_db(tmp_path, [
        {"id": "p_a", "slug": "arch", "name": "Arch", "folders": [str(gone)], "archived": 1},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    with pytest.raises(ValueError):
        resolve_trusted_workspace(str(gone))


# ── Fix 4: rename works on DB-only entries ─────────────────────────────────


@requires_manager
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


@requires_manager
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
         patch("api.routes.load_workspaces", return_value=[]), \
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


# ── Re-gate finding 1: remove must return the merged post-mutation view ────


@requires_manager
def test_remove_db_only_keeps_surviving_neighbor_in_response(tmp_path, monkeypatch):
    from api.routes import _handle_workspace_remove
    a = tmp_path / "srv" / "proj-a"
    b = tmp_path / "srv" / "proj-b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _make_projects_db(tmp_path, [
        {"id": "p_a", "slug": "a", "name": "A", "folders": [str(a)]},
        {"id": "p_b", "slug": "b", "name": "B", "folders": [str(b)]},
    ])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", wss)):
        _handle_workspace_remove(handler, {"path": str(a)})
    handler.send_response.assert_called_once_with(200)
    resp = _response(handler)
    paths = [w["path"] for w in resp["workspaces"]]
    # Survivor present in the mutation response...
    assert str(b) in paths
    assert str(a) not in paths
    # ...no invented local duplicate for the removed DB-only entry...
    assert saved.get("wss") == []
    # ...and the next GET agrees.
    from api.projects_bridge import load_hermes_project_workspaces
    get_paths = {e["path"] for e in load_hermes_project_workspaces(profile_home=tmp_path)}
    assert get_paths == {str(b)}


def test_remove_local_only_response_unchanged(tmp_path, monkeypatch):
    # Local-only workspace: merged projection equals the local list (no DB
    # entries), so existing UI behavior is preserved.
    from api.routes import _handle_workspace_remove
    _make_projects_db(tmp_path, [])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    local = {"path": "/home/user/keep", "name": "Keep"}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[local, {"path": "/home/user/gone", "name": "Gone"}]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: wss):
        _handle_workspace_remove(handler, {"path": "/home/user/gone"})
    resp = _response(handler)
    assert [w["path"] for w in resp["workspaces"]] == ["/home/user/keep"]


# ── Re-gate finding 2: unavailable shared writer must not report success ───


def _no_writer(monkeypatch):
    # Read bridge stays intact (raw sqlite); only the native write manager and
    # the subprocess fallback disappear — the reviewer's exact scenario.
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    monkeypatch.setattr("api.projects_bridge._agent_dir", lambda: None)


def test_rename_shared_backed_fails_when_writer_unavailable(db_only, monkeypatch):
    from api.routes import _handle_workspace_rename
    _no_writer(monkeypatch)
    local_mirror = {"path": str(db_only), "name": "DB Only"}
    save_calls = []
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[dict(local_mirror)]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: save_calls.append(wss)):
        _handle_workspace_rename(handler, {"path": str(db_only), "name": "Should Not Stick"})
    handler.send_response.assert_called_once_with(500)
    # Local state untouched: nothing saved, native row unchanged.
    assert save_calls == []
    conn = sqlite3.connect(db_only.parent.parent / "projects.db")
    try:
        assert conn.execute("SELECT name FROM projects WHERE id='p_db1'").fetchone()[0] == "DB Only"
    finally:
        conn.close()
    # Next GET still shows the authoritative native name.
    from api.projects_bridge import load_hermes_project_workspaces
    names = {e["path"]: e["name"] for e in load_hermes_project_workspaces(profile_home=db_only.parent.parent)}
    assert names[str(db_only)] == "DB Only"


def test_remove_shared_backed_fails_when_writer_unavailable(db_only, monkeypatch):
    from api.routes import _handle_workspace_remove
    _no_writer(monkeypatch)
    local_mirror = {"path": str(db_only), "name": "DB Only"}
    save_calls = []
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[dict(local_mirror)]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: save_calls.append(wss)):
        _handle_workspace_remove(handler, {"path": str(db_only)})
    handler.send_response.assert_called_once_with(400)
    assert save_calls == []
    # The native project is still live: next GET restores it.
    from api.projects_bridge import load_hermes_project_workspaces
    paths = {e["path"] for e in load_hermes_project_workspaces(profile_home=db_only.parent.parent)}
    assert str(db_only) in paths


def test_rename_local_only_still_works_without_writer(tmp_path, monkeypatch):
    # Legitimate local-only ops must NOT fail when the writer is unavailable.
    from api.routes import _handle_workspace_rename
    _make_projects_db(tmp_path, [])
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _no_writer(monkeypatch)
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[{"path": "/home/user/x", "name": "X"}]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: wss):
        _handle_workspace_rename(handler, {"path": "/home/user/x", "name": "Renamed Local"})
    handler.send_response.assert_called_once_with(200)
    resp = _response(handler)
    assert next(w for w in resp["workspaces"] if w["path"] == "/home/user/x")["name"] == "Renamed Local"


# ── Optional nit: corrupt DB must not orphan a new folder ──────────────────


def test_create_project_corrupt_db_fails_before_mkdir(tmp_path, monkeypatch):
    from api.routes import _handle_workspace_create_project
    fresh_home = tmp_path / "profile"
    fresh_home.mkdir()
    (fresh_home / "projects.db").write_bytes(b"this is not a sqlite database at all")
    target = tmp_path / "srv" / "never-made"
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: fresh_home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=AssertionError("must not save")):
        _handle_workspace_create_project(handler, {"path": str(target), "name": "X", "create": True})
    handler.send_response.assert_called_once_with(400)
    assert not target.exists()


# ── Greptile P1: failed ownership read must not prove local-only ───────────


def test_remove_fails_closed_when_ownership_unreadable(db_only):
    from api.routes import _handle_workspace_remove
    saved = {}
    handler = _make_handler()
    with patch("api.projects_bridge.load_project_state", return_value=([], set(), False)), \
         patch("api.routes.load_workspaces", return_value=[{"path": str(db_only), "name": "DB Only"}]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", wss)):
        _handle_workspace_remove(handler, {"path": str(db_only)})
    handler.send_response.assert_called_once_with(400)
    assert not saved, "local list must be untouched when ownership is unknown"


def test_rename_fails_closed_when_ownership_unreadable(db_only):
    from api.routes import _handle_workspace_rename
    saved = {}
    handler = _make_handler()
    with patch("api.projects_bridge.load_project_state", return_value=([], set(), False)), \
         patch("api.routes.load_workspaces", return_value=[{"path": str(db_only), "name": "DB Only"}]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", wss)):
        _handle_workspace_rename(handler, {"path": str(db_only), "name": "Sneaky"})
    handler.send_response.assert_called_once_with(500)
    assert not saved, "local list must be untouched when ownership is unknown"


# ── Re-gate finding 2: /api/workspaces/add returns the merged projection ───


def test_add_response_keeps_db_only_neighbor(db_only, tmp_path, monkeypatch):
    """A DB-only project (no local row) must survive in the add response:
    the picker renders this response directly, and the raw local list made
    DB-only neighbours vanish until the next poll."""
    from api.routes import _handle_workspace_add
    fresh = tmp_path / "srv" / "fresh-local"
    fresh.mkdir()
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=lambda wss, **kw: saved.setdefault("wss", wss)):
        _handle_workspace_add(handler, {"path": str(fresh)})
    handler.send_response.assert_called_once_with(200)
    resp = _response(handler)
    paths = [w["path"] for w in resp["workspaces"]]
    assert str(db_only) in paths, "DB-only neighbour dropped from add response"
    assert str(fresh) in paths
    # Only the fresh dir is persisted locally — no invented row for the DB-only one.
    assert [w["path"] for w in saved["wss"]] == [str(fresh)]
    # Server-normalized path for callers that typed '~/x' or a trailing slash.
    assert resp["path"] == str(fresh)


def test_add_rejects_duplicate_under_symlink_spelling(tmp_path, monkeypatch):
    """path_key canonicalization: a local row stored under a symlinked
    spelling must block an add of the same directory through the real path
    (remove/rename/reorder already compare via path_key; add must too).
    Note validate_workspace_to_add resolves the REQUEST path, so the
    divergence lives on the stored-row side."""
    from api.routes import _handle_workspace_add
    real = tmp_path / "srv" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "srv" / "link"
    link.symlink_to(real)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[{"path": str(link), "name": "Proj"}]), \
         patch("api.routes.save_workspaces", side_effect=AssertionError("must not save on duplicate")):
        _handle_workspace_add(handler, {"path": str(real)})
    handler.send_response.assert_called_once_with(400)
    body = _response(handler)
    assert "already in list" in body.get("error", "").lower()
