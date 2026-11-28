"""Follow-ups to deep audit round 5 (#5763).

A1 (greptile P1 on b70acf20): the subprocess-lane F4 cross-check in
``_ARCHIVE_PROJECT_PROG`` / ``_RENAME_PROJECT_PROG`` type-sniffed rows with
``isinstance(row, tuple)``. A populated ``primary_path`` is a ``str`` and
``hermes_cli.projects_db.connect`` returns ``sqlite3.Row`` rows (not tuples),
so every legitimate id-lane archive/rename returned ``not-found`` whenever
hermes_cli was not importable in-process: rename 404'd and remove reported
success without archiving (the project re-appeared on the next poll).
These tests force the subprocess lane (``_projects_db_module() -> None``)
against a stub agent checkout whose ``connect`` uses ``sqlite3.Row`` or plain
tuples, with ``primary_path`` set and with the folders-only fallback.

C2 (maintainer re-gate on 5349e116): a PATH-ONLY ``{path: P}`` remove (or
rename) replay
for a remote-spelled path whose saved row is already gone fell into the
inference lane and archived the native project at the identical spelling.
With no saved row and no identity, the server must answer 409
``ambiguous_target``, drop nothing, and leave the native project active —
for both SSH and Docker profiles.
"""
import json
import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BACKENDS = pytest.mark.parametrize("backend", ["ssh", "docker"], ids=["ssh", "docker"])


def _manager_available() -> bool:
    from api.projects_bridge import _agent_dir, _projects_db_module
    return _projects_db_module() is not None or _agent_dir() is not None


@pytest.fixture
def _requires_manager():
    if not _manager_available():
        pytest.skip("no Hermes Projects manager reachable (agent checkout absent)")


def requires_manager(fn):
    fn = pytest.mark.usefixtures("_requires_manager")(fn)
    return pytest.mark.projects_manager(fn)


@pytest.fixture(autouse=True)
def _agent_on_sys_path(monkeypatch):
    """conftest restores sys.path to a snapshot taken BEFORE api.config
    appended the agent dir, so when this file runs in isolation the in-process
    lane's lazy agent imports (e.g. hermes_state_wal) can fail depending on
    test order. Re-append it for the duration of each test (restored after)."""
    import sys
    from api.projects_bridge import _agent_dir
    agent = _agent_dir()
    if agent is not None and str(agent) not in sys.path:
        monkeypatch.setattr(sys, "path", [*sys.path, str(agent)])


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


def _response(handler):
    return handler._captured.get("body", {})


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
            "INSERT INTO projects (id, slug, name, primary_path, created_at, archived) "
            "VALUES (?,?,?,?,?,?)",
            (p["id"], p["slug"], p["name"], p.get("primary_path"), 1000 + i,
             p.get("archived", 0)),
        )
        for j, folder in enumerate(p.get("folders", [])):
            conn.execute(
                "INSERT INTO project_folders (project_id, path, is_primary, added_at) "
                "VALUES (?,?,?,?)",
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


# ── A1: subprocess id lane with primary_path / sqlite3.Row ──────────────

_STUB_PDB = textwrap.dedent(
    """
    import sqlite3
    ROW_FACTORY = {row_factory!r}

    def connect(db_path=None):
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        if ROW_FACTORY == "row":
            conn.row_factory = sqlite3.Row  # what hermes_cli.projects_db uses
        return conn

    def list_projects(conn, include_archived=False):
        return []
    """
)


@pytest.fixture(params=["row", "tuple"], ids=["sqlite3.Row", "tuple"])
def subprocess_lane(request, tmp_path, monkeypatch):
    """Force the subprocess lane against a stub agent checkout (no real
    hermes-agent needed), with Row or tuple row factories."""
    agent = tmp_path / "stub-agent"
    (agent / "hermes_cli").mkdir(parents=True)
    (agent / "hermes_cli" / "__init__.py").write_text("")
    (agent / "hermes_cli" / "projects_db.py").write_text(
        _STUB_PDB.format(row_factory=request.param))
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    monkeypatch.setattr("api.projects_bridge._agent_dir", lambda: agent)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    own = tmp_path / "own"
    own.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    return {"home": home, "own": str(own), "other": str(other)}


def _project(s, layout):
    if layout == "primary_path":
        return {"id": "p1", "slug": "own", "name": "Own", "primary_path": s["own"],
                "folders": [s["own"]]}
    return {"id": "p1", "slug": "own", "name": "Own", "folders": [s["own"]]}


LAYOUTS = pytest.mark.parametrize("layout", ["primary_path", "folders_only"])


@LAYOUTS
def test_subprocess_archive_by_id_matching_path(subprocess_lane, layout):
    from api.projects_bridge import archive_hermes_project

    s = subprocess_lane
    db = _make_projects_db(s["home"], [_project(s, layout)])
    res = archive_hermes_project(s["own"], project_id="p1")
    assert res.get("archived") is True, res
    assert _db_row(db, "p1")["archived"] == 1


@LAYOUTS
def test_subprocess_rename_by_id_matching_path(subprocess_lane, layout):
    from api.projects_bridge import rename_hermes_project

    s = subprocess_lane
    db = _make_projects_db(s["home"], [_project(s, layout)])
    res = rename_hermes_project(s["own"], "Renamed", project_id="p1")
    assert res.get("renamed") is True, res
    assert _db_row(db, "p1")["name"] == "Renamed"


@LAYOUTS
def test_subprocess_archive_by_id_mismatched_path_refused(subprocess_lane, layout):
    from api.projects_bridge import archive_hermes_project

    s = subprocess_lane
    db = _make_projects_db(s["home"], [_project(s, layout)])
    res = archive_hermes_project(s["other"], project_id="p1")
    assert res.get("archived") is False and res.get("reason") == "not-found", res
    assert _db_row(db, "p1")["archived"] == 0


@LAYOUTS
def test_subprocess_rename_by_id_mismatched_path_refused(subprocess_lane, layout):
    from api.projects_bridge import rename_hermes_project

    s = subprocess_lane
    db = _make_projects_db(s["home"], [_project(s, layout)])
    res = rename_hermes_project(s["other"], "Hijacked", project_id="p1")
    assert res.get("renamed") is False and res.get("reason") == "not-found", res
    assert _db_row(db, "p1")["name"] == "Own"


@requires_manager
@LAYOUTS
def test_real_agent_subprocess_lane_archive_and_rename_by_id(tmp_path, monkeypatch, layout):
    """Same contract against the real hermes_cli checkout (when present)."""
    from api.projects_bridge import _agent_dir, archive_hermes_project, rename_hermes_project

    if _agent_dir() is None:
        pytest.skip("agent checkout absent (subprocess lane needs it)")
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    own = tmp_path / "own"
    own.mkdir()
    s = {"own": str(own)}
    db = _make_projects_db(home, [_project(s, layout)])
    assert rename_hermes_project(str(own), "Renamed", project_id="p1").get("renamed") is True
    assert archive_hermes_project(str(own), project_id="p1").get("archived") is True
    assert _db_row(db, "p1") == {"name": "Renamed", "archived": 1}


# ── C2: path-only replay of a stale remote remove ───────────────────────


@pytest.fixture
def equal_spelling_scenario(tmp_path, monkeypatch):
    """Saved remote workspace and a native project at the IDENTICAL spelling
    (plain host dir, no symlink); backend from real profile config."""
    remote_cwd = tmp_path / "srv"
    remote_cwd.mkdir()
    same = remote_cwd / "www"
    same.mkdir()
    same_str = str(same)
    backend = ["ssh"]
    monkeypatch.setattr("api.config.get_config_for_profile_home",
                        lambda profile_home: {"terminal": {"backend": backend[0],
                                                           "cwd": same_str}})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    db = _make_projects_db(home, [
        {"id": "p1", "slug": "hostproj", "name": "HostProj", "folders": [same_str]},
    ])
    local = tmp_path / "local"
    local.mkdir()
    return {"same": same_str, "home": home, "db": db, "backend": backend,
            "local": str(local)}


def _forbid_shared_writer(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("shared-store writer must not be called")
    monkeypatch.setattr("api.projects_bridge.archive_hermes_project", _boom)
    monkeypatch.setattr("api.projects_bridge.rename_hermes_project", _boom)


def _state_store(state):
    return (
        patch("api.routes.load_workspaces",
              side_effect=lambda profile=None: [dict(w) for w in state["wss"]]),
        patch("api.routes.save_workspaces",
              side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])),
    )


@BACKENDS
def test_path_only_remote_remove_replay_does_not_archive_native(equal_spelling_scenario, backend, monkeypatch):
    # No manager needed (greptile P2 on bbf2bbff): neither request may reach
    # the shared-store writer, so make it fail loudly if called — this runs
    # in CI, which has no agent checkout.
    _forbid_shared_writer(monkeypatch)
    from api.projects_bridge import is_remote_workspace_path
    from api.routes import _handle_workspace_remove

    s = equal_spelling_scenario
    s["backend"][0] = backend
    assert is_remote_workspace_path(s["same"], profile="default"), "scenario must be remote-spelled"
    other = {"path": s["local"], "name": "Unrelated"}
    state = {"wss": [{"path": s["same"], "name": "RemoteWWW"}, dict(other)]}
    body = {"path": s["same"]}  # legacy path-only payload, no identity

    h1 = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_remove(h1, dict(body))
    assert h1.send_response.call_args[0][0] == 200, _response(h1)
    assert state["wss"] == [other], "request 1 removes only the saved remote row"
    assert _db_row(s["db"], "p1")["archived"] == 0, "request 1 must not archive the native project"

    # Identical replay after the row is gone: must not infer native origin
    # from the spelling and archive p1.
    h2 = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_remove(h2, dict(body))
    assert h2.send_response.call_args[0][0] == 409, \
        f"path-only replay with no saved row must 409: {_response(h2)}"
    assert _response(h2).get("code") == "ambiguous_target", _response(h2)
    assert "project_id" in _response(h2).get("error", ""), _response(h2)
    assert state["wss"] == [other], "a 409 must not drop or rewrite any saved row"
    assert _db_row(s["db"], "p1") == {"name": "HostProj", "archived": 0}, \
        "native project must remain active after a path-only remote replay"


@requires_manager
@BACKENDS
def test_db_only_native_under_remote_cwd_still_removable_by_project_id(
        equal_spelling_scenario, backend):
    """The picker's DB-only native row carries project_id; that lane still
    archives it (greptile P1 operability) even though path-only now 409s."""
    from api.routes import _handle_workspace_remove

    s = equal_spelling_scenario
    s["backend"][0] = backend
    state = {"wss": []}
    h = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_remove(h, {"path": s["same"], "project_id": "p1"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    assert _db_row(s["db"], "p1")["archived"] == 1


@requires_manager
@BACKENDS
def test_path_only_remove_of_host_native_project_still_archives(equal_spelling_scenario, backend):
    """A non-remote host path keeps the legacy path-only lane: a DB-only
    native project outside the remote cwd is archived by path alone."""
    from api.routes import _handle_workspace_remove

    s = equal_spelling_scenario
    s["backend"][0] = backend
    conn = sqlite3.connect(s["db"])
    conn.execute("INSERT INTO projects (id, slug, name, created_at, archived) "
                 "VALUES ('p2','localproj','LocalProj',3000,0)")
    conn.execute("INSERT INTO project_folders (project_id, path, is_primary, added_at) "
                 "VALUES ('p2',?,1,4000)", (s["local"],))
    conn.commit()
    conn.close()
    state = {"wss": [{"path": s["local"], "name": "LocalProj"}]}
    h = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_remove(h, {"path": s["local"]})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    assert _db_row(s["db"], "p2")["archived"] == 1
    assert _db_row(s["db"], "p1")["archived"] == 0
    assert state["wss"] == []


@BACKENDS
def test_path_only_remote_rename_replay_does_not_rename_native(equal_spelling_scenario, backend, monkeypatch):
    # No manager needed (greptile P2 on bbf2bbff): neither request may reach
    # the shared-store writer, so make it fail loudly if called — this runs
    # in CI, which has no agent checkout.
    _forbid_shared_writer(monkeypatch)
    from api.projects_bridge import is_remote_workspace_path
    from api.routes import _handle_workspace_remove, _handle_workspace_rename

    s = equal_spelling_scenario
    s["backend"][0] = backend
    assert is_remote_workspace_path(s["same"], profile="default"), "scenario must be remote-spelled"
    other = {"path": s["local"], "name": "Unrelated"}
    state = {"wss": [{"path": s["same"], "name": "RemoteWWW"}, dict(other)]}

    # Path-only rename while the remote row is saved: labels only that row.
    h0 = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_rename(h0, {"path": s["same"], "name": "RemoteRenamed"})
    assert h0.send_response.call_args[0][0] == 200, _response(h0)
    assert state["wss"][0]["name"] == "RemoteRenamed"
    assert _db_row(s["db"], "p1")["name"] == "HostProj"

    # The remote row goes away (removed through its identity lane).
    h1 = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_remove(h1, {"path": s["same"], "workspace_kind": "remote"})
    assert h1.send_response.call_args[0][0] == 200, _response(h1)
    assert state["wss"] == [other]

    # Replayed path-only rename: must not rename the same-spelling native
    # project by inference, and must not touch any saved row.
    h2 = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_rename(h2, {"path": s["same"], "name": "Hijacked"})
    assert h2.send_response.call_args[0][0] == 409, _response(h2)
    assert _response(h2).get("code") == "ambiguous_target", _response(h2)
    assert "project_id" in _response(h2).get("error", ""), _response(h2)
    assert _db_row(s["db"], "p1") == {"name": "HostProj", "archived": 0}
    assert state["wss"] == [other], "a 409 must not drop or rewrite any saved row"


@requires_manager
@BACKENDS
def test_db_only_native_under_remote_cwd_still_renamable_by_project_id(
        equal_spelling_scenario, backend):
    from api.routes import _handle_workspace_rename

    s = equal_spelling_scenario
    s["backend"][0] = backend
    state = {"wss": []}
    h = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_rename(h, {"path": s["same"], "name": "Renamed Native",
                                     "project_id": "p1"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    assert _db_row(s["db"], "p1")["name"] == "Renamed Native"


@requires_manager
@BACKENDS
def test_path_only_rename_of_host_db_only_project_still_works(equal_spelling_scenario, backend):
    """Non-remote host path with no saved row keeps the legacy DB-only lane."""
    from api.routes import _handle_workspace_rename

    s = equal_spelling_scenario
    s["backend"][0] = backend
    conn = sqlite3.connect(s["db"])
    conn.execute("INSERT INTO projects (id, slug, name, created_at, archived) "
                 "VALUES ('p2','localproj','LocalProj',3000,0)")
    conn.execute("INSERT INTO project_folders (project_id, path, is_primary, added_at) "
                 "VALUES ('p2',?,1,4000)", (s["local"],))
    conn.commit()
    conn.close()
    state = {"wss": []}
    h = _make_handler()
    p1, p2 = _state_store(state)
    with p1, p2:
        _handle_workspace_rename(h, {"path": s["local"], "name": "LocalRenamed"})
    assert h.send_response.call_args[0][0] == 200, _response(h)
    assert _db_row(s["db"], "p2")["name"] == "LocalRenamed"
    assert _db_row(s["db"], "p1")["name"] == "HostProj"
