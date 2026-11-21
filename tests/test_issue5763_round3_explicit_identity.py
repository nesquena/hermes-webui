"""Re-gate round 3 (head 5349e116): explicit target identity for same-spelling
remote/native targets and stale remote-remove replays.

Two independent targets may share a path SPELLING: a saved REMOTE workspace
(e.g. /srv/www on an SSH or Docker target) and a native projects.db project
whose primary folder is a host directory at the identical string (no symlink
involved). Path-only selection cannot tell them apart, so the projection now
carries explicit identity — ``workspace_kind='remote'`` on remote rows and
``project_id`` on native rows — and rename/remove/reorder act on the carried
identity instead of re-inferring provenance after the row vanishes.

Covered per backend (SSH and Docker, driven through real profile config so
``_is_remote_terminal_backend`` runs for both):

1. Projection: same-spelling remote row + native project => TWO rows
   (remote keeps its label + kind; native carries its project_id).
2. Stale remote-remove replay: request 1 ({path}, kind=remote) removes the
   saved remote row and leaves the native project unarchived; the IDENTICAL
   replay must 404 and must NOT archive the native project (round 2's lane
   flipped remote_literal false once the row was gone and archived it).
3. Same-spelling native rename via carried project_id renames exactly the
   native project; a replay with the same id after the project is gone 404s
   without touching a successor registered at the same spelling.
4. Reorder with identity objects keeps both same-spelling rows and never
   materializes a mirror for a stale remote claim.
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


BACKENDS = pytest.mark.parametrize("backend", ["ssh", "docker"], ids=["ssh", "docker"])


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
def equal_spelling_scenario(tmp_path, monkeypatch):
    """Remote cwd root == tmp_path; a host dir at the IDENTICAL spelling the
    DB owns (plain dir, no symlink); saved remote workspace at that spelling.
    Backend comes from real profile config so SSH and Docker both exercise
    the production remote-classification path."""
    remote_cwd = tmp_path / "srv"
    remote_cwd.mkdir()
    same = remote_cwd / "www"
    same.mkdir()  # plain host dir at the same spelling — no symlink
    same_str = str(same)

    def _config_for_profile_home(profile_home):
        return {"terminal": {"backend": _backend[0], "cwd": same_str}}

    _backend = ["ssh"]
    monkeypatch.setattr("api.config.get_config_for_profile_home", _config_for_profile_home)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    db = _make_projects_db(home, [
        {"id": "p1", "slug": "hostproj", "name": "HostProj", "folders": [same_str]},
    ])
    return {"same": same_str, "home": home, "db": db, "backend": _backend}


def _use_backend(s, backend):
    s["backend"][0] = backend


def _state_store(state):
    return (
        patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]),
        patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])),
    )


# ── 1. Projection: two rows for two independent same-spelling targets ──────


@requires_manager
@BACKENDS
def test_projection_same_spelling_keeps_two_rows(equal_spelling_scenario, backend):
    from api.projects_bridge import merge_hermes_projects

    s = equal_spelling_scenario
    _use_backend(s, backend)
    merged = merge_hermes_projects(
        [{"path": s["same"], "name": "RemoteWWW"}],
        profile_home=s["home"], profile="default")
    rows = [w for w in merged if w["path"] == s["same"]]
    assert len(rows) == 2, f"remote workspace and native project are separate targets: {merged}"
    remote_row, native_row = rows
    assert remote_row["name"] == "RemoteWWW" and remote_row.get("workspace_kind") == "remote"
    assert native_row["name"] == "HostProj" and native_row.get("source") == "hermes_project"
    assert native_row.get("project_id") == "p1"


# ── 2. Stale remote-remove replay must not archive the native project ──────


@requires_manager
@BACKENDS
def test_stale_remote_remove_replay_does_not_archive_native(equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_remove

    s = equal_spelling_scenario
    _use_backend(s, backend)
    state = {"wss": [{"path": s["same"], "name": "RemoteWWW"}]}
    body = {"path": s["same"], "workspace_kind": "remote"}

    h1 = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_remove(h1, dict(body))
    assert h1.send_response.call_args[0][0] == 200, _response(h1)
    assert state["wss"] == [], "request 1 removes the saved remote row"
    assert _db_row(s["db"], "p1")["archived"] == 0, "native project untouched by request 1"

    # Identical replay: the remote row is gone. Round 2 flipped
    # remote_literal=false here (no local row + lexical DB membership) and
    # ARCHIVED the native project with a 200. Explicit kind must fail closed.
    h2 = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_remove(h2, dict(body))
    assert h2.send_response.call_args[0][0] == 404, \
        f"stale remote claim must 404, not archive a successor: {_response(h2)}"
    assert _db_row(s["db"], "p1")["archived"] == 0, \
        "stale remote-remove replay must never archive the native project"


# ── 3. Native rename via carried project_id; stale id replay 404s ──────────


@requires_manager
@BACKENDS
def test_native_rename_by_project_id_and_stale_replay(equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_rename

    s = equal_spelling_scenario
    _use_backend(s, backend)
    # The native picker row (no local row; DB-only lane) carries project_id.
    state = {"wss": []}
    h1 = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_rename(h1, {"path": s["same"], "name": "Renamed Native",
                                      "project_id": "p1"})
    assert h1.send_response.call_args[0][0] == 200, _response(h1)
    assert _db_row(s["db"], "p1")["name"] == "Renamed Native"

    # Archive the project, then replay the identical request: the carried id
    # is gone -> 404, and a NEW project registered at the same spelling must
    # not be renamed by the stale request.
    conn = sqlite3.connect(s["db"])
    conn.execute("UPDATE projects SET archived = 1 WHERE id='p1'")
    conn.execute("INSERT INTO projects (id, slug, name, created_at, archived) VALUES ('p2','successor','Successor',3000,0)")
    conn.execute("INSERT INTO project_folders (project_id, path, is_primary, added_at) VALUES ('p2',?,1,4000)", (s["same"],))
    conn.commit()
    conn.close()
    from api.projects_bridge import _cache
    _cache.clear()

    h2 = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_rename(h2, {"path": s["same"], "name": "Stale Rename",
                                      "project_id": "p1"})
    assert h2.send_response.call_args[0][0] == 404, \
        f"stale project_id must 404 instead of renaming the successor: {_response(h2)}"
    assert _db_row(s["db"], "p2")["name"] == "Successor", \
        "stale rename must not touch a successor project at the same spelling"


# ── 4. Reorder with identity objects: both rows persist, no materialization ─


@requires_manager
@BACKENDS
def test_reorder_identity_objects_keep_both_rows(equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_reorder

    s = equal_spelling_scenario
    _use_backend(s, backend)
    state = {"wss": [{"path": s["same"], "name": "RemoteWWW"}]}
    handler = _make_handler()
    paths = [
        {"path": s["same"], "workspace_kind": "remote", "project_id": ""},
        {"path": s["same"], "workspace_kind": "", "project_id": "p1"},
    ]
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_reorder(handler, {"paths": paths})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    # The remote row persists as-is; the native row materializes as a mirror
    # of p1 (dragged DB-only row) — two entries, neither dropped.
    assert len(state["wss"]) == 2, f"both same-spelling targets persist: {state['wss']}"
    assert state["wss"][0] == {"path": s["same"], "name": "RemoteWWW"}
    assert state["wss"][1].get("project_mirror") is True and state["wss"][1]["name"] == "HostProj"


@requires_manager
@BACKENDS
def test_reorder_stale_remote_claim_materializes_nothing(equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_reorder

    s = equal_spelling_scenario
    _use_backend(s, backend)
    state = {"wss": []}  # remote row already removed
    handler = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_reorder(
            handler, {"paths": [{"path": s["same"], "workspace_kind": "remote"}]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert state["wss"] == [], \
        f"a stale remote drag must not materialize the coinciding native row: {state['wss']}"


# ── 5. Identity-scoped local mutation: sibling row at same spelling survives ─


@requires_manager
@BACKENDS
def test_native_remove_identity_does_not_delete_sibling_remote_row(equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_remove

    s = equal_spelling_scenario
    _use_backend(s, backend)
    # Both rows exist locally: saved remote row + materialized native mirror.
    state = {"wss": [
        {"path": s["same"], "name": "RemoteWWW"},
        {"path": s["same"], "name": "HostProj", "project_mirror": True},
    ]}
    h = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_remove(h, {"path": s["same"], "project_id": "p1"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    survivors = [w for w in state["wss"] if not w.get("project_mirror")]
    assert len(survivors) == 1 and survivors[0]["name"] == "RemoteWWW", \
        f"native-id remove must keep the sibling remote row: {state['wss']}"
    assert _db_row(s["db"], "p1")["archived"] == 1


@requires_manager
@BACKENDS
def test_native_rename_identity_does_not_rename_sibling_remote_row(equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_rename

    s = equal_spelling_scenario
    _use_backend(s, backend)
    state = {"wss": [
        {"path": s["same"], "name": "RemoteWWW"},
        {"path": s["same"], "name": "HostProj", "project_mirror": True},
    ]}
    h = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_rename(h, {"path": s["same"], "name": "Renamed",
                                     "project_id": "p1"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    remote_rows = [w for w in state["wss"] if not w.get("project_mirror")]
    assert remote_rows and remote_rows[0]["name"] == "RemoteWWW", \
        f"native-id rename must leave the sibling remote row's label alone: {state['wss']}"
    assert _db_row(s["db"], "p1")["name"] == "Renamed"


@requires_manager
@BACKENDS
def test_remote_remove_keeps_sibling_native_mirror(equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_remove

    s = equal_spelling_scenario
    _use_backend(s, backend)
    state = {"wss": [
        {"path": s["same"], "name": "RemoteWWW"},
        {"path": s["same"], "name": "HostProj", "project_mirror": True},
    ]}
    h = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_remove(h, {"path": s["same"], "workspace_kind": "remote"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    survivors = [w for w in state["wss"] if w.get("project_mirror")]
    assert len(survivors) == 1 and survivors[0]["name"] == "HostProj", \
        f"remote-kind remove must keep the sibling native mirror: {state['wss']}"
    assert _db_row(s["db"], "p1")["archived"] == 0


# ── 6. Audit round (omp -p): reorder provenance findings F1–F4 ─────────────


@requires_manager
def test_second_drag_after_mirror_keeps_remote_row(tmp_path, monkeypatch):
    """F1: remote row + materialized native mirror at one spelling; a second
    drag of the remote row must not let first-match local_hit land on the
    mirror and the safety net silently delete the remote row."""
    from api.routes import _handle_workspace_reorder
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _make_projects_db(home, [
        {"id": "p1", "slug": "hostproj", "name": "HostProj", "folders": ["/srv/www"]},
    ])
    from api.projects_bridge import _cache
    _cache.clear()
    state = {"wss": [
        {"path": "/srv/www", "name": "HostProj", "project_mirror": True},
        {"path": "/srv/www", "name": "RemoteWWW"},
    ]}
    handler = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_reorder(handler, {"paths": [
            {"path": "/srv/www", "workspace_kind": "remote", "project_id": ""},
            {"path": "/srv/www", "workspace_kind": "", "project_id": "p1"},
        ]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert len(state["wss"]) == 2, f"second drag must keep both rows: {state['wss']}"
    assert state["wss"][0] == {"path": "/srv/www", "name": "RemoteWWW"}
    assert state["wss"][1].get("project_mirror") is True


@requires_manager
def test_stale_project_id_reorder_materializes_nothing(tmp_path, monkeypatch):
    """F4: replay a reorder carrying an archived project id after a successor
    registered at the same path — the id lane must fail closed, not fall
    back to path selection and mirror the successor."""
    from api.routes import _handle_workspace_reorder
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _make_projects_db(home, [
        {"id": "p1", "slug": "old", "name": "Old", "folders": ["/srv/www"], "archived": 1},
        {"id": "p2", "slug": "succ", "name": "Succ", "folders": ["/srv/www"]},
    ])
    from api.projects_bridge import _cache
    _cache.clear()
    state = {"wss": []}
    handler = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_reorder(handler, {"paths": [
            {"path": "/srv/www", "workspace_kind": "", "project_id": "p1"}]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert state["wss"] == [], \
        f"stale project_id drag must not materialize the successor: {state['wss']}"


@requires_manager
def test_drag_materializes_project_the_row_showed(tmp_path, monkeypatch):
    """F2: two DB projects behind one realpath (symlink spellings). The
    projection shows the FIRST entry (bridge first-wins); dragging that row
    must materialize the SAME project, not the last-wins dict pick."""
    from api.routes import _handle_workspace_reorder
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _make_projects_db(home, [
        {"id": "p_a", "slug": "a", "name": "ProjA", "folders": [str(real)]},
        {"id": "p_b", "slug": "b", "name": "ProjB", "folders": [str(link)]},
    ])
    from api.projects_bridge import _cache, merge_hermes_projects, path_key
    _cache.clear()
    merged = merge_hermes_projects([], profile_home=home, profile="default")
    row = next(w for w in merged if path_key(w["path"]) == path_key(str(real)))
    assert row["project_id"] == "p_a", f"projection first-wins: {row}"
    state = {"wss": []}
    handler = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_reorder(handler, {"paths": [
            {"path": row["path"], "workspace_kind": "", "project_id": row["project_id"]}]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert len(state["wss"]) == 1 and state["wss"][0]["name"] == "ProjA", \
        f"drag must materialize the project the row showed: {state['wss']}"


@requires_manager
def test_drag_merged_local_row_keeps_local_name(tmp_path, monkeypatch):
    """F3: a plain local row merged with a DB project at the same path (not
    remote) projects as ONE row carrying project_id. Dragging it must keep
    the local row as-is — not flip provenance to mirror and lose the local
    name."""
    from api.routes import _handle_workspace_reorder
    d = tmp_path / "proj"
    d.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _make_projects_db(home, [
        {"id": "p1", "slug": "dbproj", "name": "DBName", "folders": [str(d)]},
    ])
    from api.projects_bridge import _cache
    _cache.clear()
    state = {"wss": [{"path": str(d), "name": "LocalName"}]}
    handler = _make_handler()
    with _state_store(state)[0], _state_store(state)[1]:
        _handle_workspace_reorder(handler, {"paths": [
            {"path": str(d), "workspace_kind": "", "project_id": "p1"}]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert state["wss"] == [{"path": str(d), "name": "LocalName"}], \
        f"merged local row must persist unchanged: {state['wss']}"
