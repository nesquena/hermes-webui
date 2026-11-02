"""Re-gate round 10 (#5763): a path-only mirror action must not clobber a
same-spelling remote sibling (greptile P1 on 0bf8feb8).

Setup: SSH/Docker profile whose remote cwd coincides with a host directory; a
remote literal R and a native mirror M saved at the SAME spelling; a DB
project at that host path. With sync on, M carries its project_id and every
lane is correct. With sync off (HERMES_WEBUI_PROJECTS_DB_SYNC=0) the picker
row for M is path-only — indistinguishable from R by path alone. Remote
literals ALWAYS carry workspace_kind=remote in every projection, so an
identity-free request can only come from the mirror:

- remove M  -> drop ONLY the mirror row (R survives; both orders);
- rename M  -> rename the mirror, never R (both orders);
- reorder M -> the identity object claims the mirror row, not R.

Sync-ON variants pin the shared-store consequence: the sibling branch must
route the DB archive/rename (remote_literal=False), not skip it — otherwise
the "removed" mirror re-appears on the next GET because projects.db
re-appends its still-active project.
"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


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
    h.send_response.side_effect = lambda code: captured.__setitem__("code", code)
    h.end_headers.side_effect = lambda: None
    h.wfile.write.side_effect = lambda data: captured.__setitem__(
        "body", json.loads(data.decode("utf-8")))
    h._captured = captured
    return h


def _code(h):
    return h.send_response.call_args[0][0]


def _response(h):
    return h._captured.get("body", {})


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


def _db_state(db: Path, pid: str = "p1") -> dict:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT name, archived FROM projects WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()
    return {"name": row[0], "archived": row[1]}


def _sibling_rows(same: str) -> list[dict]:
    return [
        {"path": same, "name": "RemoteWWW", "workspace_kind": "remote"},
        {"path": same, "name": "Mirror", "project_mirror": True},
    ]


@pytest.fixture
def sibling_pair(tmp_path, monkeypatch):
    """Remote cwd == host dir spelling; rows R (remote literal) and M (mirror)
    saved at the same spelling. Kill switch ON (sync off)."""
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
    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    return {"same": str(same), "rows": _sibling_rows(str(same))}


@pytest.fixture
def sibling_pair_sync_on(tmp_path, monkeypatch):
    """Same pair with sync ON and projects.db owning the spelling — the
    shared-store lane (archive/rename in the DB) is live."""
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
    monkeypatch.delenv("HERMES_WEBUI_PROJECTS_DB_SYNC", raising=False)
    db = _make_projects_db(home, [
        {"id": "p1", "slug": "wwwproj", "name": "WWWProj", "folders": [str(same)]},
    ])
    return {"same": str(same), "rows": _sibling_rows(str(same)), "db": db}


def _state_store(state):
    return (
        patch("api.routes.load_workspaces",
              side_effect=lambda profile=None: [dict(w) for w in state["wss"]]),
        patch("api.routes.save_workspaces",
              side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])),
    )


@pytest.mark.parametrize("remote_first", [True, False])
def test_remove_mirror_keeps_remote_sibling(sibling_pair, remote_first):
    """Path-only remove of the mirror must drop ONLY the mirror row; the
    remote sibling at the same spelling survives (greptile P1: both rows
    were deleted and the handler returned 200)."""
    from api.routes import _handle_workspace_remove

    rows = list(sibling_pair["rows"])
    if not remote_first:
        rows.reverse()
    state = {"wss": rows}
    h = _make_handler()
    ld, sv = _state_store(state)
    with ld, sv:
        _handle_workspace_remove(h, {"path": sibling_pair["same"]})
    assert _code(h) == 200, _response(h)
    survivors = [w for w in state["wss"] if w["path"] == sibling_pair["same"]]
    assert len(survivors) == 1, f"exactly the remote sibling must survive: {state['wss']}"
    assert survivors[0].get("workspace_kind") == "remote", \
        f"the mirror must be the row removed: {state['wss']}"
    # GAP C: the merged response projection must also show the survivor —
    # a merge-lane regression that hides R from `workspaces` must fail here.
    body_rows = _response(h).get("workspaces", [])
    assert any(w.get("path") == sibling_pair["same"] and w.get("workspace_kind") == "remote"
               for w in body_rows), f"response must still list the remote sibling: {body_rows}"


@pytest.mark.parametrize("remote_first", [True, False])
def test_rename_mirror_never_renames_remote_sibling(sibling_pair, remote_first):
    """Path-only rename of the mirror renames the MIRROR row; with R saved
    first the old code renamed R instead (greptile P1)."""
    from api.routes import _handle_workspace_rename

    rows = list(sibling_pair["rows"])
    if not remote_first:
        rows.reverse()
    state = {"wss": rows}
    h = _make_handler()
    ld, sv = _state_store(state)
    with ld, sv:
        _handle_workspace_rename(h, {"path": sibling_pair["same"], "name": "M2"})
    assert _code(h) == 200, _response(h)
    at_key = [w for w in state["wss"] if w["path"] == sibling_pair["same"]]
    renamed = [w for w in at_key if w.get("name") == "M2"]
    assert len(renamed) == 1, f"exactly one row renamed: {state['wss']}"
    assert renamed[0].get("project_mirror") is True, \
        f"the mirror must be the row renamed: {state['wss']}"
    remote = [w for w in at_key if w.get("workspace_kind") == "remote"]
    assert remote and remote[0]["name"] == "RemoteWWW", \
        f"remote sibling label untouched: {state['wss']}"


def test_reorder_identity_object_claims_mirror(sibling_pair):
    """An identity object (dict without kind/id — the kill-switch picker row)
    at a sibling spelling must claim the MIRROR row, not the remote literal.
    A bare legacy string keeps first-wins (round-5 O2 pin)."""
    from api.routes import _handle_workspace_reorder

    same = sibling_pair["same"]
    other = sibling_pair["rows"][0]
    extra = {"path": str(sibling_pair["rows"][0]["path"]) + "-B", "name": "B"}
    state = {"wss": [dict(other), {"path": same, "name": "Mirror", "project_mirror": True}, extra]}
    h = _make_handler()
    ld, sv = _state_store(state)
    with ld, sv:
        _handle_workspace_reorder(h, {"paths": [{"path": same}, extra, other]})
    assert _code(h) == 200, _response(h)
    first = state["wss"][0]
    assert first.get("project_mirror") is True, \
        f"identity object must reorder the mirror row: {state['wss']}"


# ── GAP A (mutation-proven): sync-ON shared-store consequence ───────────────
# Under the kill switch db_paths is always empty, so the archive/rename DB
# lane is unreachable there. These tests run with sync ON: the sibling branch
# must route the DB write (remote_literal=False). Without the override the
# mirror row is dropped but the DB project stays active and the next GET
# re-appends the "removed" workspace.


def _requires_manager(fn):
    from api.projects_bridge import _agent_dir, _projects_db_module
    return pytest.mark.skipif(
        not (_projects_db_module() is not None or _agent_dir() is not None),
        reason="no Hermes Projects manager reachable (agent checkout absent)")(fn)


@_requires_manager
@pytest.mark.parametrize("remote_first", [True, False])
def test_sync_on_remove_mirror_archives_db_project(sibling_pair_sync_on, remote_first):
    """Path-only remove of the mirror with sync ON: DB project archived AND
    only the mirror row dropped (remote sibling survives)."""
    from api.routes import _handle_workspace_remove

    rows = list(sibling_pair_sync_on["rows"])
    if not remote_first:
        rows.reverse()
    state = {"wss": rows}
    h = _make_handler()
    ld, sv = _state_store(state)
    with ld, sv:
        _handle_workspace_remove(h, {"path": sibling_pair_sync_on["same"]})
    assert _code(h) == 200, _response(h)
    assert _db_state(sibling_pair_sync_on["db"])["archived"] == 1, \
        "removing the mirror must archive the shared project (else the next GET re-appends it)"
    survivors = [w for w in state["wss"] if w["path"] == sibling_pair_sync_on["same"]]
    assert len(survivors) == 1 and survivors[0].get("workspace_kind") == "remote", \
        f"only the mirror row dropped: {state['wss']}"


@_requires_manager
@pytest.mark.parametrize("remote_first", [True, False])
def test_sync_on_rename_mirror_renames_db_project(sibling_pair_sync_on, remote_first):
    """Path-only rename of the mirror with sync ON: DB project renamed AND
    the remote sibling's local label untouched."""
    from api.routes import _handle_workspace_rename

    rows = list(sibling_pair_sync_on["rows"])
    if not remote_first:
        rows.reverse()
    state = {"wss": rows}
    h = _make_handler()
    ld, sv = _state_store(state)
    with ld, sv:
        _handle_workspace_rename(h, {"path": sibling_pair_sync_on["same"], "name": "M2"})
    assert _code(h) == 200, _response(h)
    assert _db_state(sibling_pair_sync_on["db"])["name"] == "M2", \
        "renaming the mirror must rename the shared project (DB is authoritative for its name)"
    at_key = [w for w in state["wss"] if w["path"] == sibling_pair_sync_on["same"]]
    remote = [w for w in at_key if w.get("workspace_kind") == "remote"]
    assert remote and remote[0]["name"] == "RemoteWWW", \
        f"remote sibling label untouched: {state['wss']}"
