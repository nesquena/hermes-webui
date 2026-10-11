"""Re-gate round 2 (review of head a3d0acbe) — must-fix regressions.

1. Plain re-add after removing a project must be visible and must not be
   refused as a duplicate: hiding is driven by persisted mirror PROVENANCE
   (``project_mirror``), never by name-matching an archived project.
2. Remote (SSH/Docker) workspace paths must never be collapsed by the host
   ``realpath()`` — comparison keys are profile-aware and keep remote POSIX
   paths as written, even with a host symlink of the same spelling.
3. Kill switch must reject BEFORE any DB preflight or mkdir.
4. Rename of an unknown path returns 404 even when no writer is reachable.
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
    """See tests/test_issue5763_projects_db_bridge.py — pre-import the lazily
    imported hermes_state_wal so a prior test's sys.path restore cannot break
    pdb.connect() mid-suite (re-gate should-fix 5)."""
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


# ── Finding 1: plain re-add after remove must work ──────────────────────────


@requires_manager
def test_plain_readd_after_archive_is_visible(tmp_path, monkeypatch):
    """Create (box ticked) → remove (archived) → add with the box UNTICKED:
    the add must return 200 AND the row must be in the response (and every
    later GET). Previously the row was persisted but hidden by name-match
    against the archived project, and a second add said 'already in list'."""
    from api.routes import _handle_workspace_add, _handle_workspace_create_project, _handle_workspace_remove

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    target = tmp_path / "srv" / "proj"
    target.mkdir(parents=True)
    _make_projects_db(tmp_path, [])

    state = {"wss": []}

    def load_ws(profile=None):
        return [dict(w) for w in state["wss"]]

    def save_ws(wss, profile=None):
        state["wss"] = [dict(w) for w in wss]

    with patch("api.routes.load_workspaces", side_effect=load_ws), \
         patch("api.routes.save_workspaces", side_effect=save_ws):
        # 1) create with the box ticked → mirror row + DB project
        h1 = _make_handler()
        _handle_workspace_create_project(h1, {"path": str(target), "name": "Proj", "create": True})
        assert h1.send_response.call_args[0][0] == 200
        assert any(w.get("project_mirror") for w in state["wss"]), \
            f"mirror provenance must be stamped: {state['wss']}"

        # 2) remove → local row gone, DB project archived
        h2 = _make_handler()
        _handle_workspace_remove(h2, {"path": str(target)})
        assert h2.send_response.call_args[0][0] == 200
        assert state["wss"] == []

        # 3) plain add (box UNTICKED) → 200 and the row is VISIBLE
        h3 = _make_handler()
        _handle_workspace_add(h3, {"path": str(target)})
        assert h3.send_response.call_args[0][0] == 200, _response(h3)
        resp = _response(h3)
        assert any(w["path"] == str(target) for w in resp.get("workspaces", [])), \
            f"re-added workspace must be visible: {resp}"
        assert not any(w.get("project_mirror") for w in state["wss"]), \
            "a plain add must not carry mirror provenance"

        # 4) a second add is a genuine duplicate now (row is visible)
        h4 = _make_handler()
        _handle_workspace_add(h4, {"path": str(target)})
        assert h4.send_response.call_args[0][0] == 400


def test_plain_readd_same_name_as_archived_survives_merge(tmp_path):
    """Merge-level: a local row WITHOUT provenance at an archived project's
    path — even under the archived project's exact name — must stay visible."""
    from api.projects_bridge import merge_hermes_projects

    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Shared Name", "folders": ["/srv/a"], "archived": 1},
    ])
    local = [{"path": "/srv/a", "name": "Shared Name"}]
    assert merge_hermes_projects(local, profile_home=tmp_path) == local


def test_marked_mirror_of_archived_project_hidden(tmp_path):
    """The mirror row created by a project registration IS hidden when the
    shared project is archived — provenance is the trigger."""
    from api.projects_bridge import merge_hermes_projects

    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Shared Name", "folders": ["/srv/a"], "archived": 1},
    ])
    merged = merge_hermes_projects(
        [{"path": "/srv/a", "name": "Shared Name", "project_mirror": True}],
        profile_home=tmp_path,
    )
    assert merged == []


def test_mirror_provenance_survives_load_save_roundtrip(tmp_path, monkeypatch):
    """workspaces.json persistence must not strip project_mirror (the hiding
    contract depends on it surviving a reload)."""
    from api.workspace import _clean_workspace_list

    monkeypatch.setattr("api.workspace._home_path", lambda: tmp_path / "pinning-not-home")
    p = tmp_path / "srv"
    p.mkdir(parents=True)
    rows = [{"path": str(p), "name": "Proj", "project_mirror": True}]
    cleaned = _clean_workspace_list(rows)
    assert cleaned and cleaned[0].get("project_mirror") is True
    plain = [{"path": str(p), "name": "Proj"}]
    assert "project_mirror" not in _clean_workspace_list(plain)[0]


# ── Finding 2: remote paths must not be collapsed by host realpath ──────────


@pytest.fixture()
def remote_profile(tmp_path, monkeypatch):
    """A remote-terminal profile: terminal.cwd lives on the target host.
    The HOST filesystem has alias -> real symlinks at the SAME spelling
    UNDER the remote cwd (greptile P2: the symlink must exist at the tested
    paths, otherwise host realpath() is identity there and the tests pass
    even with the old host-based comparison)."""
    remote_cwd = tmp_path / "remote"
    remote_cwd.mkdir()
    real = remote_cwd / "real"
    real.mkdir()
    alias = remote_cwd / "alias"
    alias.symlink_to(real)
    monkeypatch.setattr("api.workspace._remote_terminal_cwd",
                        lambda profile=None: str(remote_cwd))
    return {"cwd": remote_cwd, "alias": alias, "real": real}


def test_path_key_keeps_remote_paths_unresolved(remote_profile):
    from api.projects_bridge import path_key

    alias = str(remote_profile["alias"])
    real = str(remote_profile["real"])
    # No profile: host comparison deliberately folds the host symlink
    # (realpath alias -> real) — two spellings of one HOST directory.
    assert path_key(alias) == path_key(real)
    # Profile-aware: remote paths are keyed as written; the host symlink
    # must NOT collapse two distinct target-side directories.
    assert path_key(alias, profile="default") != path_key(real, profile="default")
    assert path_key(alias, profile="default") == alias.rstrip("/")


def test_remove_remote_alias_keeps_remote_real(remote_profile, monkeypatch):
    """Removing the remote workspace 'alias' must not remove the saved
    workspace 'real' — even with Projects sync disabled and a host symlink
    alias -> real at the same spelling."""
    from api.routes import _handle_workspace_remove

    monkeypatch.setattr("api.profiles.get_active_hermes_home",
                        lambda: remote_profile["cwd"] / "home")
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    alias, real = str(remote_profile["alias"]), str(remote_profile["real"])
    wss = [{"path": alias, "name": "Alias"}, {"path": real, "name": "Real"}]
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[dict(w) for w in wss]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, **kw: saved.setdefault("wss", [dict(x) for x in w])):
        _handle_workspace_remove(handler, {"path": alias})
    assert handler.send_response.call_args[0][0] == 200
    paths = [w["path"] for w in saved["wss"]]
    assert real in paths, f"remote sibling must survive: {saved}"
    assert alias not in paths


def test_add_remote_alias_succeeds_with_host_symlink(remote_profile, monkeypatch):
    """Adding remote 'alias' must succeed (validate keeps remote paths
    un-stat'ed) and must not be refused as a duplicate of host 'real'."""
    from api.routes import _handle_workspace_add

    monkeypatch.setattr("api.profiles.get_active_hermes_home",
                        lambda: remote_profile["cwd"] / "home")
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    alias, real = str(remote_profile["alias"]), str(remote_profile["real"])
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[{"path": real, "name": "Real"}]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, **kw: saved.setdefault("wss", list(w))):
        _handle_workspace_add(handler, {"path": alias})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert any(w["path"] == alias for w in saved["wss"])


@requires_manager
@requires_manager
def test_host_owned_path_under_remote_cwd_still_archives(tmp_path, monkeypatch):
    """Deep-audit 2b: projects.db is HOST-local. A host DB project whose path
    string falls under a remote profile's terminal.cwd is still host-owned —
    remove must archive it (host-keyed), not treat remoteness as proof of
    non-ownership (that gate silently reverted removals: the next GET
    re-appended the un-archived project)."""
    from api.routes import _handle_workspace_remove

    cwd = tmp_path / "remote"
    cwd.mkdir()
    app = cwd / "app"
    app.mkdir()
    monkeypatch.setattr("api.workspace._remote_terminal_cwd", lambda profile=None: str(cwd))
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "Host Proj", "folders": [str(app)]},
    ])
    saved = {}
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[{"path": str(app), "name": "Host Proj", "project_mirror": True}]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, **kw: saved.setdefault("wss", list(w))):
        _handle_workspace_remove(handler, {"path": str(app)})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    conn = sqlite3.connect(tmp_path / "projects.db")
    try:
        assert conn.execute("SELECT archived FROM projects WHERE id='p1'").fetchone()[0] == 1, \
            "host-owned project under a remote cwd must archive on remove"
    finally:
        conn.close()


@requires_manager
def test_merge_no_duplicate_rows_for_host_project_under_remote_cwd(tmp_path, monkeypatch):
    """Deep-audit 2a + re-gate must-fix: with a remote profile, a NATIVE row
    (mirror provenance) and the DB project for the SAME directory must merge
    into one row even when the host realpath differs from the lexical spelling
    (host symlink under the remote cwd). Provenance is what makes the row a
    genuine local/native entry eligible for host canonicalization; a plain
    row under a remote cwd is a remote literal and keeps lexical identity
    (see test_merge_remote_alias_keeps_label_and_native_row)."""
    from api.projects_bridge import merge_hermes_projects

    cwd = tmp_path / "remote"
    real = cwd / "real"
    real.mkdir(parents=True)
    alias = cwd / "alias"
    alias.symlink_to(real)
    monkeypatch.setattr("api.workspace._remote_terminal_cwd", lambda profile=None: str(cwd))
    # DB stores the RESOLVED host path; the local row carries the alias
    # spelling WITH mirror provenance. Host-keyed comparison must fold them
    # to one entry.
    _make_projects_db(tmp_path, [
        {"id": "p1", "slug": "a", "name": "HostProj", "folders": [str(real)]},
    ])
    merged = merge_hermes_projects(
        [{"path": str(alias), "name": "alias", "project_mirror": True}],
        profile_home=tmp_path, profile="default")
    same = [w for w in merged if w["path"] in (str(alias), str(real))]
    assert len(same) == 1, f"one directory must list once: {merged}"
    assert same[0]["name"] == "HostProj"  # DB name override applied


@requires_manager
def test_create_project_stamps_existing_plain_row(tmp_path, monkeypatch):
    """Deep-audit finding 1: plain add first, then register the same folder as
    a project (box ticked) — the EXISTING local row must gain mirror
    provenance, or a later archive leaves an un-hidable mirror that also
    blocks re-add."""
    from api.routes import _handle_workspace_create_project

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    target = tmp_path / "srv" / "proj"
    target.mkdir(parents=True)
    _make_projects_db(tmp_path, [])
    state = {"wss": [{"path": str(target), "name": "proj"}]}  # plain row, no flag
    handler = _make_handler()
    with patch("api.routes.load_workspaces", side_effect=lambda profile=None: [dict(w) for w in state["wss"]]), \
         patch("api.routes.save_workspaces", side_effect=lambda w, profile=None: state.__setitem__("wss", [dict(x) for x in w])):
        _handle_workspace_create_project(handler, {"path": str(target), "name": "Proj", "create": True})
    assert handler.send_response.call_args[0][0] == 200, _response(handler)
    assert any(w.get("project_mirror") for w in state["wss"]), \
        f"existing row must gain provenance: {state['wss']}"


# ── Finding 4 (should-fix): kill switch must not write ──────────────────────


def test_kill_switch_rejects_before_db_or_mkdir(tmp_path, monkeypatch):
    """With HERMES_WEBUI_PROJECTS_DB_SYNC=0, create_project must return 400
    WITHOUT creating projects.db or the requested folder."""
    from api.routes import _handle_workspace_create_project

    monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    fresh_home = tmp_path / "profile"
    fresh_home.mkdir()
    target = tmp_path / "srv" / "never"
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: fresh_home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    handler = _make_handler()
    with patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=AssertionError("must not save")):
        _handle_workspace_create_project(handler, {"path": str(target), "name": "X", "create": True})
    assert handler.send_response.call_args[0][0] == 400
    assert not target.exists(), "kill switch must reject before mkdir"
    assert not (fresh_home / "projects.db").exists(), "kill switch must reject before DB preflight"


# ── Low: rename unknown path is 404 even without a reachable writer ─────────


def test_rename_unknown_path_404_without_writer(tmp_path, monkeypatch):
    """A DB exists (ownership readable) but no writer is reachable: an
    unknown path must be a clean 404, not a 500."""
    from api.routes import _handle_workspace_rename

    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    _make_projects_db(tmp_path, [])
    handler = _make_handler()
    with patch("api.projects_bridge.projects_write_supported", return_value=False), \
         patch("api.routes.load_workspaces", return_value=[]), \
         patch("api.routes.save_workspaces", side_effect=AssertionError("must not save")):
        _handle_workspace_rename(handler, {"path": "/no/such/path", "name": "X"})
    assert handler.send_response.call_args[0][0] == 404


def test_projects_db_openable_detects_malformed_body(tmp_path):
    """Greptile P2: `SELECT 1` validates only the file header — a DB with a
    valid header but a corrupt table page must still fail the probe, or the
    create route mkdirs before registration fails and leaves an orphan
    folder. The probe reads table rows (SELECT *), which forces the b-tree
    page read."""
    from api.projects_bridge import projects_db_openable

    home = tmp_path / "profile"
    home.mkdir()
    db = home / "projects.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
    for i in range(500):
        conn.execute("INSERT INTO projects VALUES (?, ?)", (f"r{i}", "x" * 200))
    conn.commit()
    conn.close()
    raw = bytearray(db.read_bytes())
    raw[4096] = 0x0F  # invalid page-type byte on page 2 (header stays valid)
    db.write_bytes(bytes(raw))
    assert projects_db_openable(profile_home=home) is False


def test_projects_db_openable_accepts_schemaless_db(tmp_path):
    """A valid DB without the projects schema yet is openable: the native
    manager initializes it on the real write (probe must not over-reject)."""
    from api.projects_bridge import projects_db_openable

    home = tmp_path / "profile"
    home.mkdir()
    sqlite3.connect(home / "projects.db").close()
    assert projects_db_openable(profile_home=home) is True


def test_projects_db_openable_does_not_create_db(tmp_path):
    """The preflight probe must be read-only: a fresh profile stays DB-free."""
    from api.projects_bridge import projects_db_openable

    fresh = tmp_path / "profile"
    fresh.mkdir()
    assert projects_db_openable(profile_home=fresh) is True
    assert not (fresh / "projects.db").exists()
