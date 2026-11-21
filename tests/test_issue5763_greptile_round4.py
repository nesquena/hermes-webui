"""Greptile round 4 (review of head 200f6d4a) — four regressions.

1. P1 remove: a folder added locally FIRST and registered in projects.db
   LATER has no ``project_mirror`` provenance, yet the projection stamps its
   picker row with the ``project_id`` the request carries. The native lane
   archived the shared project but ``drop_mirror=True`` kept the plain local
   row, so the removed workspace re-listed as a local workspace despite the
   200. The lane must drop the plain local row too (a same-spelling remote
   literal sibling still survives — round 3 contract).
2. P1 reorder: the safety net suppressed unmentioned rows by PATH key while
   the main loop dedupes by full identity. A stale tab submitting only the
   native identity object silently deleted a same-spelling remote row.
   Suppression must use the same identity granularity.
3. P2 rename: the ``workspace_kind="remote"`` lane ran before
   ``merge_hermes_projects`` was imported in ``_handle_workspace_rename``;
   the NameError was swallowed by ``except Exception`` and the response
   silently dropped every DB-only project.
4. P2 frontend: the detail highlight used object reference equality (lost
   after any reorder/rename re-render, which returns fresh dicts) and the
   reopen path selected rows by path alone (wrong sibling for same-spelling
   remote/native rows). Both must use the identity triple.
"""
import json
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

    def end_headers():
        pass

    h.send_response.side_effect = send_response
    h.end_headers.side_effect = end_headers

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


@pytest.fixture
def remote_profile(tmp_path, monkeypatch):
    """Remote-terminal profile (terminal.cwd on the target host) with a host
    dir at the SAME spelling the DB owns — the same-spelling remote/native
    scenario (round 3)."""
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


# ── Finding 1 (P1): remove by carried project_id must drop a PLAIN local row ─


@requires_manager
def test_remove_by_project_id_drops_plain_local_row(tmp_path, monkeypatch):
    """Added locally first, registered in projects.db later: the row has no
    mirror provenance but the picker carries its project_id. Remove must
    archive the project AND remove the local row (greptile P1: the row
    survived and the workspace re-listed after a 200)."""
    from api.routes import _handle_workspace_remove

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    target = tmp_path / "srv" / "proj"
    target.mkdir(parents=True)
    db = _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "late", "name": "LateReg", "folders": [str(target)]},
    ])
    state = {"wss": [{"path": str(target), "name": "LocalName"}]}
    h = _make_handler()
    with patch("api.routes.load_workspaces",
               side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces",
               side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_remove(h, {"path": str(target), "project_id": "p1"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    assert state["wss"] == [], \
        f"plain local row carried by project_id must be removed: {state['wss']}"
    assert _db_row(db, "p1")["archived"] == 1


@requires_manager
def test_remove_by_project_id_keeps_remote_sibling(remote_profile):
    """Round 3 contract preserved by the fix: a remote literal at the same
    spelling is a DIFFERENT target and must survive a native remove."""
    from api.routes import _handle_workspace_remove

    same = remote_profile["same"]
    _make_projects_db(remote_profile["home"], [
        {"id": "p1", "slug": "hostproj", "name": "HostProj", "folders": [same]},
    ])
    state = {"wss": [{"path": same, "name": "RemoteWWW"}]}
    h = _make_handler()
    with patch("api.routes.load_workspaces",
               side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces",
               side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_remove(h, {"path": same, "project_id": "p1"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    assert [w["path"] for w in state["wss"]] == [same], \
        f"remote sibling must survive a native remove: {state['wss']}"


# ── Finding 2 (P1): reorder safety net must not delete a same-spelling sibling


@requires_manager
def test_reorder_native_identity_keeps_unmentioned_remote_sibling(remote_profile):
    """A stale tab drags only the native row (identity object) while another
    tab had added a remote row at the identical spelling. The safety net must
    NOT suppress the unmentioned remote row (greptile P1: path-key
    suppression silently deleted it from workspaces.json)."""
    from api.routes import _handle_workspace_reorder

    same = remote_profile["same"]
    _make_projects_db(remote_profile["home"], [
        {"id": "p1", "slug": "hostproj", "name": "HostProj", "folders": [same]},
    ])
    state = {"wss": [
        {"path": same, "name": "RemoteWWW"},
        {"path": same, "name": "HostProj", "project_mirror": True},
    ]}
    h = _make_handler()
    with patch("api.routes.load_workspaces",
               side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces",
               side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_reorder(h, {"paths": [{"path": same, "project_id": "p1"}]})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    rows = [w for w in state["wss"] if w["path"] == same]
    assert len(rows) == 2, \
        f"both same-spelling rows must persist: {state['wss']}"
    assert any(not w.get("project_mirror") for w in rows), \
        f"unmentioned remote row must survive: {state['wss']}"


def test_reorder_legacy_path_keeps_all_rows_at_spelling(remote_profile):
    """Superseded by deep-audit round 5 (O2): the old per-path first-wins
    suppression silently DELETED same-spelling sibling rows on an
    all-strings payload (a 200 that destroyed picker state). A legacy
    path-only entry cannot disambiguate which sibling it means, so it must
    claim only the first row's position and every row at that spelling
    persists (safety net preserves the sibling)."""
    from api.routes import _handle_workspace_reorder

    same = remote_profile["same"]
    state = {"wss": [
        {"path": same, "name": "RemoteWWW"},
        {"path": same, "name": "Other", "project_mirror": True},
    ]}
    h = _make_handler()
    with patch("api.routes.load_workspaces",
               side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces",
               side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_reorder(h, {"paths": [same]})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    same_rows = [w for w in state["wss"] if w["path"] == same]
    assert len(same_rows) == 2, \
        f"legacy path entry must not delete same-spelling siblings: {state['wss']}"
    assert same_rows[0]["name"] == "RemoteWWW", \
        f"first-wins position preserved: {state['wss']}"


# ── Finding 3 (P2): remote rename response must be the MERGED view ──────────


@requires_manager
def test_rename_remote_lane_response_includes_db_only_projects(remote_profile):
    """The remote lane ran before merge_hermes_projects was imported in the
    rename handler; the swallowed NameError returned raw local rows, dropping
    every DB-only project from the response (greptile P2)."""
    from api.routes import _handle_workspace_rename

    same = remote_profile["same"]
    dbonly = remote_profile["home"] / "dbonly"
    dbonly.mkdir()
    _make_projects_db(remote_profile["home"], [
        {"id": "p9", "slug": "dbonly", "name": "DbOnly", "folders": [str(dbonly)]},
    ])
    state = {"wss": [{"path": same, "name": "RemoteWWW"}]}
    h = _make_handler()
    with patch("api.routes.load_workspaces",
               side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces",
               side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_rename(h, {"path": same, "name": "Renamed",
                                     "workspace_kind": "remote"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    resp_paths = [w["path"] for w in _response(h).get("workspaces", [])]
    assert str(dbonly) in resp_paths, \
        f"response must be the merged view (DB-only project present): {resp_paths}"
    renamed = next(w for w in _response(h)["workspaces"] if w["path"] == same)
    assert renamed["name"] == "Renamed" and renamed.get("workspace_kind") == "remote"


# ── Finding 4 (P2): detail highlight uses the identity triple ───────────────


def test_workspace_detail_highlight_uses_identity_triple():
    """Static guard: the row highlight must compare path + project_id +
    workspace_kind (freshly-serialized response dicts never match by
    reference), and the reopen selector must not match rows by path alone."""
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "static" / "panels.js").read_text()

    assert "_currentWorkspaceDetail === w) row.classList.add" not in src, \
        "row highlight must not rely on object reference equality alone"
    loop = src[src.index("function renderWorkspacesPanel"):]
    hl = loop[:loop.index("Drag-and-drop reorder")]
    assert "w.path === _cd.path" in hl and "w.project_id" in hl and "w.workspace_kind" in hl, \
        "row highlight must compare the identity triple"

    detail = src[src.index("function openWorkspaceDetail"):]
    detail = detail[:detail.index("function _clearWorkspaceDetail")]
    assert 'data-path="${CSS.escape(ws.path)}"' not in detail, \
        "reopen selector must not match rows by path alone"
    assert "r.dataset.projectId" in detail and "r.dataset.workspaceKind" in detail, \
        "reopen selector must compare the identity triple via row datasets"
