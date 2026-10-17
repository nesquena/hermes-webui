"""Round 7 (#5763): maintainer re-gate on b70acf20 + greptile on bbf2bbff.

1. Blocker 1: a refused/stale ``project_id`` remove (bridge says not-found)
   must cause ZERO local mutation unless the cleanup provably belongs to that
   retired owner: wrong-id/path pair and stale predecessor A -> successor B
   keep the saved rows, their order and provenance.
2. Blocker 2 (fixed in bbf2bbff): subprocess id lane against projects created
   by the NATIVE producer (``pdb.create_project`` -> TEXT primary_path).
3. Blocker 3: remote rows carry ``workspace_kind='remote'`` on every
   projection path (empty / missing / unreadable / disabled store); then a
   native project appears and the identical identity-free retry 409s.
4. Lifecycle gaps: add dedupes per target kind (remote vs native at one
   spelling are independent); reorder validates a native id BEFORE claiming a
   local row; delete/rename continuations keep the invocation identity
   across awaits (node behavioural tests, deferred responses).
"""
import json
import shutil
import sqlite3
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
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
    # conftest restores a sys.path snapshot taken before api.config appended
    # the agent dir; re-append it per test so lazy agent imports work.
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


def _code(h):
    return h.send_response.call_args[0][0]


def _response(h):
    return h._captured.get("body", {})


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


def _add_project(db: Path, pid: str, slug: str, name: str, folder: str, archived=0):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO projects (id, slug, name, created_at, archived) VALUES (?,?,?,?,?)",
                 (pid, slug, name, 5000, archived))
    conn.execute("INSERT INTO project_folders (project_id, path, is_primary, added_at) VALUES (?,?,1,6000)",
                 (pid, folder))
    conn.commit()
    conn.close()
    from api.projects_bridge import _cache
    _cache.clear()


def _db_row(db: Path, pid: str) -> dict | None:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT name, archived FROM projects WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()
    return {"name": row[0], "archived": row[1]} if row else None


class _Store:
    """Cold saved-JSON double: every load returns fresh copies of the saved
    list; every save replaces it (deep copies, so callers can't alias)."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.saves = 0

    def patches(self):
        def _save(w, profile=None):
            self.saves += 1
            self.rows = [dict(x) for x in w]
        return (
            patch("api.routes.load_workspaces",
                  side_effect=lambda profile=None: [dict(w) for w in self.rows]),
            patch("api.routes.save_workspaces", side_effect=_save),
        )


def _call(fn, body, store):
    h = _make_handler()
    a, b = store.patches()
    with a, b:
        fn(h, dict(body))
    return h


@pytest.fixture
def host_scenario(tmp_path, monkeypatch):
    """Plain host profile (no remote backend): projects A at Q, B at P."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("api.workspace._remote_terminal_cwd", lambda profile=None: None)
    dirs = {}
    for n in ("p", "q", "other"):
        d = tmp_path / n
        d.mkdir()
        dirs[n] = str(d)
    return {"home": home, **dirs}


# ── 1. Blocker 1: refused id -> zero local mutation ──────────────────────


def _bridge_refuses_id(monkeypatch):
    monkeypatch.setattr("api.projects_bridge.archive_hermes_project",
                        lambda *a, **k: {"archived": False, "reason": "not-found"})


def test_remove_wrong_id_path_pair_keeps_unrelated_saved_row(host_scenario, monkeypatch):
    # Route-level contract: the bridge refuses the id (not-found);
    # stubbed so this runs in CI without an agent checkout.
    _bridge_refuses_id(monkeypatch)
    from api.routes import _handle_workspace_remove

    s = host_scenario
    db = _make_projects_db(s["home"], [
        {"id": "A", "slug": "a", "name": "A", "folders": [s["q"]]},
    ])
    rows = [{"path": s["other"], "name": "Other"}, {"path": s["p"], "name": "P local"}]
    store = _Store(rows)
    h = _call(_handle_workspace_remove, {"path": s["p"], "project_id": "A"}, store)
    assert _code(h) == 404, _response(h)
    assert store.saves == 0 and store.rows == rows, f"no local mutation on refusal: {store.rows}"
    assert _db_row(db, "A")["archived"] == 0


def test_remove_stale_predecessor_keeps_successor_row_order_and_provenance(host_scenario, monkeypatch):
    # Route-level contract: the bridge refuses the id (not-found);
    # stubbed so this runs in CI without an agent checkout.
    _bridge_refuses_id(monkeypatch)
    from api.routes import _handle_workspace_remove

    s = host_scenario
    db = _make_projects_db(s["home"], [
        {"id": "A", "slug": "a", "name": "A", "folders": [s["p"]], "archived": 1},
        {"id": "B", "slug": "b", "name": "B", "folders": [s["p"]]},
    ])
    rows = [{"path": s["p"], "name": "B", "project_mirror": True},
            {"path": s["other"], "name": "Other"}]
    store = _Store(rows)
    h = _call(_handle_workspace_remove, {"path": s["p"], "project_id": "A"}, store)
    assert _code(h) == 404, _response(h)
    assert store.saves == 0 and store.rows == rows, \
        f"successor B's saved row/order/provenance must survive: {store.rows}"
    assert _db_row(db, "B")["archived"] == 0


def test_remove_retired_id_replay_does_not_save(host_scenario, monkeypatch):
    """Refused id → zero local save (re-gate 05:31). Mirror-flag cleanup is
    not same-owner proof, so even a retired-A replay must not mutate."""
    _bridge_refuses_id(monkeypatch)
    from api.routes import _handle_workspace_remove

    s = host_scenario
    _make_projects_db(s["home"], [
        {"id": "A", "slug": "a", "name": "A", "folders": [s["p"]], "archived": 1},
    ])
    rows = [{"path": s["p"], "name": "A", "project_mirror": True},
            {"path": s["other"], "name": "Other"}]
    store = _Store(rows)
    h = _call(_handle_workspace_remove, {"path": s["p"], "project_id": "A"}, store)
    assert _code(h) == 404, _response(h)
    assert store.saves == 0 and store.rows == rows

    store = _Store([{"path": s["p"], "name": "Mine"}])
    h = _call(_handle_workspace_remove, {"path": s["p"], "project_id": "A"}, store)
    assert _code(h) == 404, _response(h)
    assert store.saves == 0 and store.rows == [{"path": s["p"], "name": "Mine"}]


def test_remove_unknown_id_does_not_drop_unrelated_mirror(host_scenario, monkeypatch):
    _bridge_refuses_id(monkeypatch)
    from api.routes import _handle_workspace_remove

    s = host_scenario
    _make_projects_db(s["home"], [])
    rows = [{"path": s["p"], "name": "B", "project_mirror": True},
            {"path": s["other"], "name": "Other"}]
    store = _Store(rows)
    h = _call(_handle_workspace_remove, {"path": s["p"], "project_id": "NOPE"}, store)
    assert _code(h) == 404, _response(h)
    assert store.saves == 0 and store.rows == rows, \
        f"unknown id must not drop B's saved mirror: {store.rows}"


# ── 2. Blocker 2: native-producer projects through the subprocess lane ──


@requires_manager
def test_subprocess_lane_native_created_project_archive_and_rename(tmp_path, monkeypatch):
    from api.projects_bridge import _agent_dir, archive_hermes_project, rename_hermes_project

    agent = _agent_dir()
    if agent is None:
        pytest.skip("agent checkout absent (subprocess lane needs it)")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    own = tmp_path / "own"
    own.mkdir()
    # Create through the native producer itself (TEXT primary_path).
    import sys as _sys
    out = subprocess.run(
        [_sys.executable, "-I", "-c", textwrap.dedent(f"""
            import sys; sys.path.insert(0, {str(agent)!r})
            from pathlib import Path
            from hermes_cli import projects_db as pdb
            conn = pdb.connect(db_path=Path({str(home / 'projects.db')!r}))
            print(pdb.create_project(conn, name='Native', primary_path={str(own)!r}))
            conn.commit(); conn.close()
        """)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    pid = out.stdout.strip().splitlines()[-1]
    conn = sqlite3.connect(home / "projects.db")
    assert conn.execute("SELECT primary_path FROM projects WHERE id=?", (pid,)).fetchone()[0]
    conn.close()
    monkeypatch.setattr("api.projects_bridge._projects_db_module", lambda: None)
    assert rename_hermes_project(str(own), "Renamed", project_id=pid).get("renamed") is True
    assert rename_hermes_project(str(tmp_path), "X", project_id=pid).get("reason") == "not-found"
    assert archive_hermes_project(str(own), project_id=pid).get("archived") is True
    assert archive_hermes_project(str(own), project_id="unknown").get("reason") == "not-found"
    assert _db_row(home / "projects.db", pid) == {"name": "Renamed", "archived": 1}


# ── 3. Blocker 3: remote kind on every projection path ───────────────────


@pytest.fixture
def remote_scenario(tmp_path, monkeypatch):
    remote_cwd = tmp_path / "srv"
    remote_cwd.mkdir()
    same = remote_cwd / "www"
    same.mkdir()
    same_str = str(same)
    backend = ["ssh"]
    monkeypatch.setattr("api.config.get_config_for_profile_home",
                        lambda profile_home: {"terminal": {"backend": backend[0], "cwd": same_str}})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    return {"same": same_str, "home": home, "backend": backend, "tmp": tmp_path}


def _no_writer(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("shared-store writer must not be called")
    monkeypatch.setattr("api.projects_bridge.archive_hermes_project", _boom)
    monkeypatch.setattr("api.projects_bridge.rename_hermes_project", _boom)


@BACKENDS
@pytest.mark.parametrize("snapshot", ["missing", "empty", "unreadable", "disabled"])
def test_remote_kind_stamped_without_native_state_then_retry_409(
        remote_scenario, backend, snapshot, monkeypatch):
    from api.projects_bridge import merge_hermes_projects
    from api.routes import _handle_workspace_remove

    s = remote_scenario
    s["backend"][0] = backend
    db = s["home"] / "projects.db"
    if snapshot == "empty":
        _make_projects_db(s["home"], [])
    elif snapshot == "unreadable":
        db.write_bytes(b"not a sqlite database at all" * 64)
    elif snapshot == "disabled":
        _make_projects_db(s["home"], [])
        monkeypatch.setenv("HERMES_WEBUI_PROJECTS_DB_SYNC", "0")
    rows = [{"path": s["same"], "name": "RemoteWWW"}]
    merged = merge_hermes_projects(rows, profile_home=s["home"], profile="default")
    assert merged[0].get("workspace_kind") == "remote", f"{snapshot}: {merged}"

    # A same-spelling native project appears after that snapshot.
    if snapshot in ("missing", "unreadable"):
        db.unlink(missing_ok=True)
        _make_projects_db(s["home"], [])
    monkeypatch.delenv("HERMES_WEBUI_PROJECTS_DB_SYNC", raising=False)
    _add_project(db, "N1", "native", "Native", s["same"])
    _no_writer(monkeypatch)

    store = _Store(rows)
    h1 = _call(_handle_workspace_remove, {"path": s["same"], "workspace_kind": "remote"}, store)
    assert _code(h1) == 200, _response(h1)
    assert store.rows == []
    # The client's network-error retry re-sends the identical body; with the
    # stamped kind that is a stale remote claim -> 404, and an identity-free
    # body is 409 — neither touches the native project.
    h2 = _call(_handle_workspace_remove, {"path": s["same"], "workspace_kind": "remote"}, store)
    assert _code(h2) == 404, _response(h2)
    h3 = _call(_handle_workspace_remove, {"path": s["same"]}, store)
    assert _code(h3) == 409 and _response(h3).get("code") == "ambiguous_target", _response(h3)
    assert _db_row(db, "N1") == {"name": "Native", "archived": 0}


# ── 4a. Add dedupes per target kind ──────────────────────────────────────


@BACKENDS
def test_add_remote_row_beside_same_spelling_native_project(remote_scenario, backend, monkeypatch):
    from api.routes import _handle_workspace_add

    s = remote_scenario
    s["backend"][0] = backend
    db = _make_projects_db(s["home"], [])
    _add_project(db, "N1", "native", "Native", s["same"])
    monkeypatch.setattr("api.routes.validate_workspace_to_add",
                        lambda path, profile=None: Path(path))
    store = _Store([])
    h = _call(_handle_workspace_add, {"path": s["same"], "name": "RemoteWWW"}, store)
    assert _code(h) == 200, _response(h)
    assert store.rows == [{"path": s["same"], "name": "RemoteWWW"}]
    rows = [w for w in _response(h)["workspaces"] if w["path"] == s["same"]]
    assert {w.get("workspace_kind", "") for w in rows} == {"remote", ""}, rows
    # A second remote add at the spelling IS a duplicate.
    h2 = _call(_handle_workspace_add, {"path": s["same"], "name": "Again"}, store)
    assert _code(h2) == 400 and "already" in _response(h2).get("error", ""), _response(h2)


def test_add_local_duplicate_still_refused(host_scenario):
    from api.routes import _handle_workspace_add

    s = host_scenario
    store = _Store([{"path": s["p"], "name": "P"}])
    h = _call(_handle_workspace_add, {"path": s["p"]}, store)
    assert _code(h) == 400 and "already" in _response(h).get("error", ""), _response(h)


# ── 4b. Reorder validates native id before claiming a local row ──────────


def test_reorder_stale_native_id_claims_no_position(host_scenario):
    from api.routes import _handle_workspace_reorder

    s = host_scenario
    _make_projects_db(s["home"], [
        {"id": "A", "slug": "a", "name": "A", "folders": [s["p"]], "archived": 1},
        {"id": "B", "slug": "b", "name": "B", "folders": [s["p"]]},
    ])
    rows = [{"path": s["other"], "name": "Other"},
            {"path": s["p"], "name": "B", "project_mirror": True}]
    store = _Store(rows)
    h = _call(_handle_workspace_reorder, {"paths": [
        {"path": s["p"], "project_id": "A", "workspace_kind": ""},
        s["other"],
    ]}, store)
    assert _code(h) == 200, _response(h)
    assert [w["path"] for w in store.rows] == [s["other"], s["p"]], \
        f"stale id A must not move successor B's row: {store.rows}"
    assert store.rows[1].get("project_mirror") is True


def test_reorder_active_id_wrong_path_does_not_move_other_row(host_scenario):
    """An active native id paired with someone else's path must not claim
    that saved row. Archive/rename already pair id+path; reorder must too."""
    from api.routes import _handle_workspace_reorder

    s = host_scenario
    _make_projects_db(s["home"], [
        {"id": "A", "slug": "a", "name": "A", "folders": [s["q"]]},
        {"id": "B", "slug": "b", "name": "B", "folders": [s["p"]]},
    ])
    rows = [{"path": s["p"], "name": "B", "project_mirror": True},
            {"path": s["other"], "name": "Other"}]
    store = _Store(rows)
    h = _call(_handle_workspace_reorder, {"paths": [
        {"path": s["p"], "project_id": "A", "workspace_kind": ""},
        s["other"],
    ]}, store)
    assert _code(h) == 200, _response(h)
    assert [w["path"] for w in store.rows] == [s["other"], s["p"]], \
        f"active id A must not move B's row at a different path: {store.rows}"
    assert store.rows[1].get("project_mirror") is True
    assert store.rows[1]["name"] == "B"


# ── 4c. Frontend continuations keep the invocation identity ──────────────


def _extract_fn(src: str, header: str) -> str:
    i = src.index(header)
    j = src.index("\n}\n", i)
    return src[i:j + 2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_delete_and_rename_keep_invocation_identity():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    fns = _extract_fn(src, "async function deleteCurrentWorkspace(){") + "\n" + \
        _extract_fn(src, "async function saveWorkspaceForm(){")
    script = textwrap.dedent("""
        const assert = require('assert');
        let _currentWorkspaceDetail = null, _workspaceMode = 'edit', _workspaceList = [];
        let _workspacePreFormDetail = null;
        let _workspaceUiGen = 0;
        const calls = [];
        const deferred = () => { let r; const p = new Promise(res => r = res); p.resolve = r; return p; };
        let pendingConfirm = null, pendingApi = [];
        const showConfirmDialog = () => (pendingConfirm = deferred());
        const api = (url, opts) => { const d = deferred(); pendingApi.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null, d}); return d; };
        let cleared = 0, opened = null;
        const _clearWorkspaceDetail = () => { cleared++; };
        const renderWorkspacesPanel = () => {}, showToast = () => {}, setStatus = (m) => calls.push(m);
        const openWorkspaceDetail = (w) => { opened = w; };
        const t = (k) => k;
        const els = {workspaceFormName: {value: 'New Name'}, workspaceFormPath: {value: '/srv/www'},
                     workspaceFormError: {style: {}, textContent: ''}, workspaceFormAsProject: {checked: false}};
        const $ = (id) => els[id] || null;
        const tick = () => new Promise(r => setImmediate(r));
        __FNS__
        const A = {path: '/srv/www', workspace_kind: 'remote', project_id: '', name: 'Remote'};
        const B = {path: '/srv/www', workspace_kind: '', project_id: 'N1', name: 'Native'};
        (async () => {
          // delete: selection changes while confirm AND request are pending
          _currentWorkspaceDetail = A;
          const run = deleteCurrentWorkspace();
          await tick();
          _currentWorkspaceDetail = B;
          pendingConfirm.resolve(true);
          await tick();
          assert.deepStrictEqual(pendingApi[0].body, {path: '/srv/www', workspace_kind: 'remote'});
          pendingApi[0].d.resolve({workspaces: []});
          await run;
          assert.strictEqual(cleared, 0, 'must not clear a detail that now shows another row');
          // delete: same row still shown -> cleared
          pendingApi = []; _currentWorkspaceDetail = A;
          const run2 = deleteCurrentWorkspace(); await tick();
          pendingConfirm.resolve(true); await tick();
          pendingApi[0].d.resolve({workspaces: []}); await run2;
          assert.strictEqual(cleared, 1);
          // rename: selection changes while the rename request is pending
          pendingApi = []; opened = null; _currentWorkspaceDetail = B;
          const run3 = saveWorkspaceForm(); await tick();
          assert.deepStrictEqual(pendingApi[0].body, {path: '/srv/www', name: 'New Name', project_id: 'N1'});
          _currentWorkspaceDetail = A;
          pendingApi[0].d.resolve({ok: true}); await tick();
          pendingApi[1].d.resolve({workspaces: [A, B]}); await run3;
          assert.strictEqual(opened, null, 'must not reopen the renamed row over the newly selected one');
          // same-selection positive: still on B → reopen B
          pendingApi = []; opened = null; _currentWorkspaceDetail = B; _workspaceMode = 'edit';
          const run4 = saveWorkspaceForm(); await tick();
          pendingApi[0].d.resolve({ok: true}); await tick();
          pendingApi[1].d.resolve({workspaces: [A, B]}); await run4;
          assert.deepStrictEqual(opened, {path: '/srv/www', workspace_kind: '', project_id: 'N1'},
            'same selection must still reopen the renamed row');
        })().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
    """).replace("__FNS__", fns)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_add_sends_name_and_does_not_rename_native_sibling():
    """Add beside an existing native row must send name on add and must not
    follow up with a path-first rename that carries the native project_id."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    fns = _extract_fn(src, "async function saveWorkspaceForm(){")
    script = textwrap.dedent("""
        const assert = require('assert');
        let _currentWorkspaceDetail = null, _workspaceMode = 'create', _workspaceList = [];
        let _workspacePreFormDetail = null;
        let _workspaceUiGen = 0;
        const deferred = () => { let r; const p = new Promise(res => r = res); p.resolve = r; return p; };
        let pendingApi = [];
        const api = (url, opts) => { const d = deferred(); pendingApi.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null, d}); return d; };
        let opened = null;
        const renderWorkspacesPanel = () => {}, showToast = () => {};
        const openWorkspaceDetail = (w) => { opened = w; };
        const t = (k) => k;
        const els = {workspaceFormName: {value: 'RemoteWWW'}, workspaceFormPath: {value: '/srv/www'},
                     workspaceFormError: {style: {}, textContent: ''}, workspaceFormAsProject: {checked: false}};
        const $ = (id) => els[id] || null;
        const tick = () => new Promise(r => setImmediate(r));
        __FNS__
        const native = {path: '/srv/www', workspace_kind: '', project_id: 'N1', name: 'Native'};
        const remote = {path: '/srv/www', workspace_kind: 'remote', project_id: '', name: 'RemoteWWW'};
        (async () => {
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/add');
          assert.deepStrictEqual(pendingApi[0].body, {path: '/srv/www', name: 'RemoteWWW'});
          pendingApi[0].d.resolve({ok: true, path: '/srv/www', workspaces: [native, remote]});
          await run;
          assert.strictEqual(pendingApi.length, 1, 'must not follow up with rename; name already went on add');
          assert.deepStrictEqual(opened, remote, 'must open the added remote row, not the native sibling');
        })().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
    """).replace("__FNS__", fns)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_open_detail_absent_identity_does_not_open():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    fns = _extract_fn(src, "function openWorkspaceDetail(pathOrWs, el){")
    script = textwrap.dedent("""
        const assert = require('assert');
        let _workspaceList = [{path: '/p', project_id: 'B', workspace_kind: '', name: 'B'}];
        let _workspaceUiGen = 0;
        let rendered = null, active = 0;
        const document = { querySelectorAll: () => [] };
        const _renderWorkspaceDetail = (ws) => { rendered = ws; };
        const _closeMobileSidebarAfterPanelSelection = () => {};
        let _workspacePreFormDetail = null;
        __FNS__
        openWorkspaceDetail({path: '/p', project_id: 'A', workspace_kind: ''});
        assert.strictEqual(rendered, null, 'must not open a ghost detail for an identity not in the list');
        openWorkspaceDetail(_workspaceList[0]);
        assert.strictEqual(rendered, _workspaceList[0]);
    """).replace("__FNS__", fns)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_stale_rename_get_does_not_publish_over_new_selection():
    """A's refresh GET already in flight, then switch to B: late A response
    must not replace B's catalog, form snapshot, or render."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    fns = _extract_fn(src, "async function saveWorkspaceForm(){")
    script = textwrap.dedent("""
        const assert = require('assert');
        let _currentWorkspaceDetail = null, _workspaceMode = 'edit', _workspaceList = [];
        let _workspacePreFormDetail = null;
        let _workspaceUiGen = 0;
        const deferred = () => { let r; const p = new Promise(res => r = res); p.resolve = r; return p; };
        let pendingApi = [];
        const api = (url, opts) => { const d = deferred(); pendingApi.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null, d}); return d; };
        let opened = null, rendered = 0;
        const renderWorkspacesPanel = () => { rendered++; };
        const showToast = () => {};
        const openWorkspaceDetail = (w) => { opened = w; };
        const t = (k) => k;
        const els = {workspaceFormName: {value: 'New Name'}, workspaceFormPath: {value: '/srv/www'},
                     workspaceFormError: {style: {}, textContent: ''}, workspaceFormAsProject: {checked: false}};
        const $ = (id) => els[id] || null;
        const tick = () => new Promise(r => setImmediate(r));
        __FNS__
        const A = {path: '/srv/www', workspace_kind: 'remote', project_id: '', name: 'Remote'};
        const B = {path: '/srv/www', workspace_kind: '', project_id: 'N1', name: 'Native'};
        (async () => {
          _currentWorkspaceDetail = A;
          _workspaceList = [A];
          _workspacePreFormDetail = { ...A };
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/rename');
          pendingApi[0].d.resolve({ok: true});
          await tick();
          assert.strictEqual(pendingApi[1].url, '/api/workspaces');
          _workspaceUiGen++;
          _currentWorkspaceDetail = B;
          _workspaceList = [B];
          _workspacePreFormDetail = { ...B };
          const renderedAfterSwitch = rendered;
          pendingApi[1].d.resolve({workspaces: [A]});
          await run;
          assert.deepStrictEqual(_workspaceList, [B], 'late A catalog must not replace B');
          assert.deepStrictEqual(_workspacePreFormDetail, B, 'must not clear B form snapshot');
          assert.strictEqual(rendered, renderedAfterSwitch, 'must not re-render from stale A GET');
          assert.strictEqual(opened, null, 'must not reopen A');
        })().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
    """).replace("__FNS__", fns)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


def _workspace_form_harness(src: str, body: str) -> str:
    fns = "\n".join([
        _extract_fn(src, "async function saveWorkspaceForm(){"),
        _extract_fn(src, "function openWorkspaceCreate(){"),
        _extract_fn(src, "function editCurrentWorkspace(){"),
        _extract_fn(src, "function cancelWorkspaceForm(){"),
    ])
    return textwrap.dedent("""
        const assert = require('assert');
        let _currentWorkspaceDetail = null, _workspaceMode = 'edit', _workspaceList = [];
        let _workspacePreFormDetail = null;
        let _workspaceUiGen = 0;
        let _currentPanel = 'workspaces';
        const deferred = () => { let r; const p = new Promise(res => r = res); p.resolve = r; return p; };
        let pendingApi = [];
        const api = (url, opts) => { const d = deferred(); pendingApi.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null, d}); return d; };
        let opened = null, rendered = 0;
        const renderWorkspacesPanel = () => { rendered++; };
        const showToast = () => {};
        const openWorkspaceDetail = (w) => { opened = w; };
        const switchPanel = () => {};
        const _closeMobileSidebarAfterPanelSelection = () => {};
        const closeWorkspacePathSuggestions = () => {};
        const _renderWorkspaceForm = () => {};
        const _renderWorkspaceDetail = (ws) => { _currentWorkspaceDetail = ws; _workspaceMode = 'read'; };
        const _clearWorkspaceDetail = () => { _workspaceUiGen++; _currentWorkspaceDetail = null; _workspaceMode = 'empty'; };
        const t = (k) => k;
        const els = {workspaceFormName: {value: 'New Name'}, workspaceFormPath: {value: '/srv/www'},
                     workspaceFormError: {style: {}, textContent: ''}, workspaceFormAsProject: {checked: false}};
        const $ = (id) => els[id] || null;
        const tick = () => new Promise(r => setImmediate(r));
        __FNS__
        const A = {path: '/srv/www', workspace_kind: 'remote', project_id: '', name: 'Remote'};
        (async () => {
          __BODY__
        })().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
    """).replace("__FNS__", fns).replace("__BODY__", textwrap.dedent(body).strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_stale_rename_get_does_not_clobber_new_space_form():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _workspace_form_harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceList = [A];
          _workspacePreFormDetail = { ...A };
          const run = saveWorkspaceForm();
          await tick();
          pendingApi[0].d.resolve({ok: true});
          await tick();
          assert.strictEqual(pendingApi[1].url, '/api/workspaces');
          openWorkspaceCreate();
          assert.strictEqual(_workspaceMode, 'create');
          const snap = _workspacePreFormDetail;
          const renderedAfter = rendered;
          pendingApi[1].d.resolve({workspaces: [A]});
          await run;
          assert.strictEqual(_workspaceMode, 'create', 'late A GET must not switch create → read');
          assert.deepStrictEqual(_workspacePreFormDetail, snap, 'must keep the newer form snapshot');
          assert.strictEqual(rendered, renderedAfter, 'must not re-render from stale A GET');
          assert.strictEqual(opened, null);
    """)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_stale_rename_get_does_not_clobber_reopened_edit_form():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _workspace_form_harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceList = [A];
          _workspacePreFormDetail = { ...A };
          const run = saveWorkspaceForm();
          await tick();
          pendingApi[0].d.resolve({ok: true});
          await tick();
          assert.strictEqual(pendingApi[1].url, '/api/workspaces');
          cancelWorkspaceForm();
          _currentWorkspaceDetail = A;
          editCurrentWorkspace();
          assert.strictEqual(_workspaceMode, 'edit');
          const snap = _workspacePreFormDetail;
          const renderedAfter = rendered;
          pendingApi[1].d.resolve({workspaces: [A]});
          await run;
          assert.strictEqual(_workspaceMode, 'edit', 'late A GET must not switch edit → read');
          assert.deepStrictEqual(_workspacePreFormDetail, snap, 'must keep the newer edit snapshot');
          assert.strictEqual(rendered, renderedAfter);
          assert.strictEqual(opened, null);
    """)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout
