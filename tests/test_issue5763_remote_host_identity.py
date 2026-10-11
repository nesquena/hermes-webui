"""Re-gate must-fix (head 9a121710): remote-to-host project identity must not
cross the bridge boundary.

Setup for every test: SSH profile whose ``terminal.cwd`` is ``remote/``; a
saved REMOTE workspace spelled ``remote/alias``; a HOST symlink at that exact
spelling (``remote/alias -> remote/real``); an UNRELATED native projects.db
project at ``remote/real``. The host ``realpath()`` coincidence must never
give the remote literal native ownership:

- rename of the remote alias renames ONLY the local row (host project name
  untouched);
- remove of the remote alias removes ONLY the local row (host project NOT
  archived);
- the merged projection keeps the remote alias label AND the separate native
  row (no label replacement, no dropped row);
- reorder of the remote alias persists the remote row, not a materialized
  copy of the host project.

The already-green local/native actions (host-owned mirror rows under a remote
cwd still archive/rename via provenance) are retained in
tests/test_issue5763_regate_round2.py (deep-audit 2a/2b).
"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _manager_available() -> bool:
    from api.projects_bridge import _agent_dir, _projects_db_module
    return _projects_db_module() is not None or _agent_dir() is not None


requires_manager = pytest.mark.usefixtures("_requires_manager")


@pytest.fixture
def _requires_manager():
    if not _manager_available():
        pytest.skip("no Hermes Projects manager reachable (agent checkout absent)")


@pytest.fixture(autouse=True)
def _preimport_lazy_state_wal():
    """See tests/test_issue5763_projects_db_bridge.py — keep the lazily
    imported hermes_state_wal resolvable mid-suite (re-gate should-fix 5)."""
    import sys
    try:
        import api.config  # noqa: F401  (normally appends the agent dir)
        from api.projects_bridge import _agent_dir
        d = _agent_dir()
        if d is not None and str(d) not in sys.path:
            sys.path.append(str(d))
        import hermes_state_wal  # noqa: F401
    except Exception:
        pass
    yield


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


def _db_state(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT name, archived FROM projects WHERE id='p1'").fetchone()
    finally:
        conn.close()
    return {"name": row[0], "archived": row[1]}


@pytest.fixture
def remote_alias_scenario(tmp_path, monkeypatch):
    """SSH profile cwd=remote/, saved remote workspace 'remote/alias', host
    symlink remote/alias -> remote/real, unrelated native project at real."""
    remote_cwd = tmp_path / "remote"
    remote_cwd.mkdir()
    real = remote_cwd / "real"
    real.mkdir()
    alias = remote_cwd / "alias"
    alias.symlink_to(real)
    monkeypatch.setattr("api.workspace._remote_terminal_cwd",
                        lambda profile=None: str(remote_cwd))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    db = _make_projects_db(home, [
        {"id": "p1", "slug": "hostproj", "name": "HostProj", "folders": [str(real)]},
    ])
    return {"cwd": remote_cwd, "alias": alias, "real": real, "home": home, "db": db,
            "alias_str": str(alias), "real_str": str(real)}


@requires_manager
def test_rename_remote_alias_does_not_rename_host_project(remote_alias_scenario, monkeypatch):
    """Reviewer assertion 1: renaming the remote alias workspace returned 200
    and renamed the HOST project. It must rename only the local row's label;
    the native projects.db row keeps its name."""
    from api.routes import _handle_workspace_rename

    s = remote_alias_scenario
    state = {"wss": [{"path": s["alias_str"], "name": "Alias"}]}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_rename(handler, {"path": s["alias_str"], "name": "Renamed Remote"})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert _db_state(s["db"])["name"] == "HostProj", \
        "renaming a remote alias workspace must not rename the host project"
    assert state["wss"][0]["name"] == "Renamed Remote", \
        "the remote row's own label must still rename locally"


@requires_manager
def test_remove_remote_alias_does_not_archive_host_project(remote_alias_scenario, monkeypatch):
    """Reviewer assertion 2: removing the remote alias workspace returned 200
    and ARCHIVED the host project. It must remove only the local row."""
    from api.routes import _handle_workspace_remove

    s = remote_alias_scenario
    state = {"wss": [{"path": s["alias_str"], "name": "Alias"}]}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_remove(handler, {"path": s["alias_str"]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert _db_state(s["db"])["archived"] == 0, \
        "removing a remote alias workspace must not archive the host project"
    assert state["wss"] == [], "the remote row itself must be removed"


@requires_manager
def test_merge_remote_alias_keeps_label_and_native_row(remote_alias_scenario):
    """Reviewer assertion 3 (merged projection): with real SSH/Docker profile
    YAML the merge replaced the remote alias label with the host project name
    and DROPPED the separate native row. The projection must keep the alias
    row's own label AND append the native row as its own entry."""
    from api.projects_bridge import merge_hermes_projects

    s = remote_alias_scenario
    merged = merge_hermes_projects(
        [{"path": s["alias_str"], "name": "Alias"}],
        profile_home=s["home"], profile="default")
    alias_rows = [w for w in merged if w["path"] == s["alias_str"]]
    real_rows = [w for w in merged if w["path"] == s["real_str"]]
    assert len(alias_rows) == 1 and len(real_rows) == 1, \
        f"remote alias and native row must both appear separately: {merged}"
    assert alias_rows[0]["name"] == "Alias", \
        f"remote label must not be replaced by the host project name: {merged}"
    assert real_rows[0]["name"] == "HostProj"
    assert real_rows[0].get("source") == "hermes_project"


@requires_manager
def test_reorder_remote_alias_persists_remote_row_not_host_project(remote_alias_scenario, monkeypatch):
    """Reviewer must-fix covers reorder's DB-owner selection: the saved
    remote alias (local row, no mirror provenance) must keep its own row and
    label when dragged — never collapse onto the coincidentally-matching
    host project."""
    from api.routes import _handle_workspace_reorder

    s = remote_alias_scenario
    state = {"wss": [{"path": s["alias_str"], "name": "Alias"}]}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_reorder(handler, {"paths": [s["alias_str"]]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert not any(w.get("project_mirror") for w in state["wss"]), \
        f"remote row must not gain native provenance from a realpath coincidence: {state['wss']}"
    alias_rows = [w for w in state["wss"] if w["path"] == s["alias_str"]]
    assert len(alias_rows) == 1 and alias_rows[0]["name"] == "Alias", \
        f"reorder must not overwrite the remote label with the host project name: {state['wss']}"


# ── greptile P1 (head c4ef87b5): DB-only shared projects under a remote cwd
# must stay operable from the picker. A path with NO local row can only be in
# the picker because it came FROM projects.db (Desktop/CLI-created) — it is a
# native entry even when its spelling falls under a remote terminal.cwd.


@requires_manager
def test_rename_db_only_project_under_remote_cwd_renames_shared_store(remote_alias_scenario):
    from api.routes import _handle_workspace_rename

    s = remote_alias_scenario
    state = {"wss": []}  # no local row: the picker row came from the DB
    handler = _make_handler()
    with patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_rename(handler, {"path": s["real_str"], "name": "Renamed Shared"})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert _db_state(s["db"])["name"] == "Renamed Shared", \
        "a DB-only shared project under a remote cwd must remain renamable (greptile P1)"


@requires_manager
def test_remove_db_only_project_under_remote_cwd_archives(remote_alias_scenario):
    from api.routes import _handle_workspace_remove

    s = remote_alias_scenario
    state = {"wss": []}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_remove(handler, {"path": s["real_str"]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert _db_state(s["db"])["archived"] == 1, \
        "removing a DB-only shared project under a remote cwd must archive it (greptile P1)"


@requires_manager
def test_reorder_db_only_project_under_remote_cwd_persists(remote_alias_scenario):
    from api.routes import _handle_workspace_reorder

    s = remote_alias_scenario
    state = {"wss": []}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_reorder(handler, {"paths": [s["real_str"]]})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    rows = [w for w in state["wss"] if w["path"] == s["real_str"]]
    assert len(rows) == 1 and rows[0].get("project_mirror"), \
        f"dragged DB-only row must materialize with mirror provenance: {state['wss']}"
