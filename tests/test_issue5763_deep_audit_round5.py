"""Deep audit round 5 (omp + OCR delegation review of the merged feature).

1. F4 (bridge): the explicit-id lane archived/renamed by ``project_id``
   alone, ignoring the request path. A crafted/stale POST pairing an
   unrelated path with a victim project_id archived/renamed the victim.
   The id must ALSO own the requested path (canonicalized), else not-found.
2. O1 (reorder): a legacy path-only entry and an identity entry at the same
   spelling resolved to the SAME row and both appended it — a permanent
   duplicate in workspaces.json (remove/rename act on only the first copy).
3. O2 (reorder): a legacy path-only entry appended only the FIRST row at its
   spelling while the key-granular safety net suppressed EVERY row at that
   key, silently deleting a same-spelling sibling (remote literal next to a
   native mirror — a state the merge projection itself creates).
   Both fixed by deduping on the resolved ROW OBJECT (materialized rows on
   their DB-entry content key).
4. F3 (workspace.py): save_workspaces wrote in place (torn read -> load
   falls back to [Home] -> next mutation persists the fallback). Now an
   atomic temp+os.replace write; the five mutation routes serialize on
   WORKSPACE_MUTATION_LOCK so a bridge-widened load->save window can no
   longer last-writer-wins a concurrent mutation.
"""
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _manager_available() -> bool:
    from api.projects_bridge import _agent_dir, _projects_db_module
    return _projects_db_module() is not None or _agent_dir() is not None


def requires_manager(fn):
    fn = pytest.mark.usefixtures("_requires_manager")(fn)
    return pytest.mark.projects_manager(fn)


@pytest.fixture
def _requires_manager():
    if not _manager_available():
        pytest.skip("no Hermes Projects manager reachable (agent checkout absent)")


@pytest.fixture(autouse=True)
def _clear_bridge_cache():
    from api.projects_bridge import _cache
    _cache.clear()
    yield
    _cache.clear()


def _make_handler():
    h = MagicMock()
    h._headers_buffer = []
    h.wfile = MagicMock()
    captured = {}

    def send_response(code):
        captured["code"] = code

    h.send_response.side_effect = send_response
    h.end_headers.side_effect = lambda: None

    def wfile_write(data):
        captured["body"] = json.loads(data.decode("utf-8"))

    h.wfile.write.side_effect = wfile_write
    h._captured = captured
    return h


def _response(handler):
    return handler._captured.get("body", {})


def _make_projects_db(home: Path, projects: list[dict]) -> Path:
    db = home / "projects.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
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


def _db_row(db: Path, pid: str) -> dict | None:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT name, archived FROM projects WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()
    return {"name": row[0], "archived": row[1]} if row else None


# ── F4: explicit-id lane must cross-check the request path ──────────────


@requires_manager
def test_archive_id_mismatched_path_refused(tmp_path, monkeypatch):
    """(path, project_id) pair where the id owns a DIFFERENT directory:
    archive must refuse (not-found) and leave the victim untouched."""
    from api.projects_bridge import archive_hermes_project

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    db = _make_projects_db(tmp_path, [
        {"id": "v1", "slug": "victim", "name": "Victim", "folders": [str(victim_dir)]},
    ])
    res = archive_hermes_project(str(unrelated), project_id="v1")
    assert res.get("archived") is False
    assert res.get("reason") == "not-found"
    assert _db_row(db, "v1")["archived"] == 0, "victim project must stay active"


@requires_manager
def test_rename_id_mismatched_path_refused(tmp_path, monkeypatch):
    from api.projects_bridge import rename_hermes_project

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    db = _make_projects_db(tmp_path, [
        {"id": "v1", "slug": "victim", "name": "Victim", "folders": [str(victim_dir)]},
    ])
    res = rename_hermes_project(str(unrelated), "Hijacked", project_id="v1")
    assert res.get("renamed") is False
    assert res.get("reason") == "not-found"
    assert _db_row(db, "v1")["name"] == "Victim"


@requires_manager
def test_archive_id_matching_path_still_works(tmp_path, monkeypatch):
    """The legitimate lane is unchanged: id + its own path archives fine."""
    from api.projects_bridge import archive_hermes_project

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    target = tmp_path / "own"
    target.mkdir()
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "own", "name": "Own", "folders": [str(target)]},
    ])
    res = archive_hermes_project(str(target), project_id="p1")
    assert res.get("archived") is True, res
    assert _db_row(db, "p1")["archived"] == 1


# ── O1/O2: reorder dedupe by resolved row object ────────────────────────


@pytest.fixture
def remote_profile(tmp_path, monkeypatch):
    remote_cwd = tmp_path / "srv"
    remote_cwd.mkdir()
    same = remote_cwd / "www"
    same.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.workspace._remote_terminal_cwd",
                        lambda profile=None: str(remote_cwd))
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    return {"cwd": remote_cwd, "same": str(same), "home": home}


def _state_store(state):
    return (
        patch("api.routes.load_workspaces",
              side_effect=lambda profile=None: [dict(w) for w in state["wss"]]),
        patch("api.routes.save_workspaces",
              side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])),
    )


@requires_manager
def test_reorder_mixed_legacy_and_identity_no_duplicate(remote_profile):
    """O1: legacy string + identity object at one spelling resolve to the
    SAME row; the payload must yield exactly one copy of it."""
    from api.routes import _handle_workspace_reorder

    same = remote_profile["same"]
    _make_projects_db(remote_profile["home"], [
        {"id": "p1", "slug": "host", "name": "HostProj", "folders": [same]},
    ])
    other = str(remote_profile["cwd"] / "other")
    Path(other).mkdir()
    state = {"wss": [{"path": same, "name": "Proj", "project_mirror": True,
                      "project_id": "p1"},
                     {"path": other, "name": "Other"}]}
    h = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_reorder(h, {"paths": [
            same,
            {"path": same, "project_id": "p1", "workspace_kind": ""},
            other,
        ]})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    saved = state["wss"]
    same_rows = [w for w in saved if w["path"] == same]
    assert len(same_rows) == 1, f"row double-appended: {saved}"
    assert {w["path"] for w in saved} == {same, other}


@requires_manager
def test_reorder_legacy_strings_keep_same_spelling_siblings(remote_profile):
    """O2: an all-strings payload (older client / API caller) with a remote
    literal and a native mirror at one spelling must keep BOTH rows."""
    from api.routes import _handle_workspace_reorder

    same = remote_profile["same"]
    _make_projects_db(remote_profile["home"], [
        {"id": "p1", "slug": "host", "name": "HostProj", "folders": [same]},
    ])
    state = {"wss": [
        {"path": same, "name": "RemoteWWW"},                       # remote literal
        {"path": same, "name": "HostProj", "project_mirror": True},  # native mirror
    ]}
    h = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_reorder(h, {"paths": [same]})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    saved = state["wss"]
    assert len(saved) == 2, f"sibling row silently dropped: {saved}"
    assert {w.get("project_mirror", False) for w in saved} == {False, True}


@requires_manager
def test_reorder_identity_only_keeps_remote_sibling(remote_profile):
    """Round-4 contract preserved by the row-object rewrite: dragging only
    the native identity object must not delete the same-spelling remote row."""
    from api.routes import _handle_workspace_reorder

    same = remote_profile["same"]
    _make_projects_db(remote_profile["home"], [
        {"id": "p1", "slug": "host", "name": "HostProj", "folders": [same]},
    ])
    state = {"wss": [
        {"path": same, "name": "RemoteWWW"},
        {"path": same, "name": "HostProj", "project_mirror": True},
    ]}
    h = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_reorder(h, {"paths": [
            {"path": same, "project_id": "p1", "workspace_kind": ""}]})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    assert len(state["wss"]) == 2, f"remote sibling dropped: {state['wss']}"


# ── F3: atomic save + mutation lock ─────────────────────────────────────


def test_save_workspaces_atomic_replace(tmp_path, monkeypatch):
    """save_workspaces must write via temp+os.replace so a concurrent reader
    never observes a torn file (and no tmp litter survives)."""
    import api.workspace as ws

    state_file = tmp_path / "workspaces.json"
    monkeypatch.setattr(ws, "_workspaces_file_for_profile", lambda profile=None: state_file)
    replaced: list = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", spy_replace)
    ws.save_workspaces([{"path": str(tmp_path), "name": "A"}])
    assert replaced and replaced[-1][1] == state_file, "save must go through os.replace"
    assert json.loads(state_file.read_text()) == [{"path": str(tmp_path), "name": "A"}]
    assert list(tmp_path.glob("workspaces.json.tmp-*")) == []


def test_mutation_lock_exists_and_is_reentrant():
    from api.workspace import WORKSPACE_MUTATION_LOCK
    # Reentrancy: acquiring twice on the same thread must not deadlock.
    with WORKSPACE_MUTATION_LOCK:
        with WORKSPACE_MUTATION_LOCK:
            pass


def test_mutation_routes_hold_lock():
    """The five workspace mutation routes must dispatch under
    WORKSPACE_MUTATION_LOCK (F3): all five appear in the guarded block."""
    import inspect
    import api.routes as routes
    src = inspect.getsource(routes)
    marker = "WORKSPACE_MUTATION_LOCK"
    assert marker in src
    # The guarded dispatch block: find the `with WORKSPACE_MUTATION_LOCK:`
    # and require all five handler calls inside it before the block ends.
    i = src.index("with WORKSPACE_MUTATION_LOCK:")
    block = src[i:i + 1200]
    for fn in ("_handle_workspace_add", "_handle_workspace_remove",
               "_handle_workspace_create_project", "_handle_workspace_rename",
               "_handle_workspace_reorder"):
        assert fn + "(" in block, f"{fn} not dispatched under the lock"
