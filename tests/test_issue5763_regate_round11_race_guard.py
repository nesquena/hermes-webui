"""Re-gate round 11 (#5763): greptile P1 + P2 on 0ae97a70.

P1 (backend): ``_handle_workspace_create_project``'s load→modify→save of
workspaces.json ran under ``WORKSPACE_MUTATION_LOCK`` only, while
``/api/projects/bind`` rewrites the same file under ``_PROJECTS_CATALOG_LOCK``
(its auto-registration lane, ``_resolve_ws_list``). The two lock sets do not
intersect, so a concurrent bind registration could be last-writer-wins erased
by create's stale save. The fix holds ``_PROJECTS_CATALOG_LOCK`` across the
LOCAL read/modify/save only — the subprocess project-create above it must not
hold the catalog lock.

P2 (frontend): the create_project branch of ``saveWorkspaceForm`` applied its
(deferrable, seconds-long) response unconditionally — a late response cleared
a newer form's snapshot, re-rendered the panel and opened the created
project's detail over the user's newer selection. The fix captures
``_workspaceUiGen`` around the request like the rename branch already does
(create and plain-add branches alike).
"""
import json
import shutil
import subprocess
import textwrap
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _manager_available() -> bool:
    from api.projects_bridge import _agent_dir, _projects_db_module
    return _projects_db_module() is not None or _agent_dir() is not None


@pytest.fixture(autouse=True)
def _agent_on_sys_path(monkeypatch):
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


@pytest.mark.projects_manager
def test_create_project_local_save_serializes_with_catalog_lock(tmp_path, monkeypatch):
    """A bind-style concurrent save must survive create_project's save.

    The bind writer is the same load→append→save-under-``_PROJECTS_CATALOG_LOCK``
    pair ``/api/projects/bind``'s ``_resolve_ws_list`` performs. Create's read
    window is widened deterministically; without the catalog lock around
    create's local block, bind's registration is erased (lost update).
    """
    if not _manager_available():
        pytest.skip("no Hermes Projects manager reachable (agent checkout absent)")
    from api.routes import _handle_workspace_create_project
    from api.models import PROJECTS_CATALOG_LOCK

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: home)
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.delenv("HERMES_WEBUI_PROJECTS_DB_SYNC", raising=False)

    ws_new = tmp_path / "proj-new"
    ws_new.mkdir()
    bind_ws = tmp_path / "proj-bind"
    bind_ws.mkdir()

    disk = {"wss": [{"path": str(tmp_path / "a"), "name": "A"}]}
    create_read = threading.Event()

    def load_spy(profile=None):
        result = [dict(w) for w in disk["wss"]]
        if not create_read.is_set():
            create_read.set()
            # Widen the read→save window so the interleaving is deterministic
            # in BOTH worlds: without the fix bind slips inside it; with the
            # fix this sleep simply runs under the catalog lock.
            threading.Event().wait(0.15)
        return result

    def save_spy(w, profile=None):
        disk["wss"] = [dict(x) for x in w]

    def bind_writer():
        with PROJECTS_CATALOG_LOCK:
            wss = [dict(w) for w in disk["wss"]]
            wss.append({"path": str(bind_ws), "name": "B"})
            disk["wss"] = wss

    h = _make_handler()

    def run_create():
        with patch("api.routes.load_workspaces", side_effect=load_spy), \
             patch("api.routes.save_workspaces", side_effect=save_spy):
            _handle_workspace_create_project(
                h, {"path": str(ws_new), "name": "N", "create": True})

    t = threading.Thread(target=run_create)
    t.start()
    assert create_read.wait(5.0), "create handler never read the workspace list"
    bind_writer()
    t.join(10)
    assert not t.is_alive(), "create handler hung"
    assert _code(h) == 200, _response(h)
    paths = [w["path"] for w in disk["wss"]]
    assert str(ws_new) in paths, f"created row missing: {paths}"
    assert str(bind_ws) in paths, f"concurrent bind registration lost: {paths}"


# ── P2: frontend stale create/add response guard ─────────────────────────

def _extract_fn(src: str, header: str) -> str:
    start = src.find(header)
    assert start >= 0, f"{header!r} not found in static/panels.js"
    i = src.find("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces for {header!r}")


def _form_harness(src: str, body: str) -> str:
    fns = "\n".join([
        _extract_fn(src, "async function saveWorkspaceForm(){"),
        _extract_fn(src, "function openWorkspaceCreate(){"),
    ])
    return textwrap.dedent("""
        const assert = require('assert');
        let _currentWorkspaceDetail = null, _workspaceMode = 'create', _workspaceList = [];
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
        const _wireWorkspaceFormPathSuggestions = () => {};
        const _renderWorkspaceForm = () => {};
        const _renderWorkspaceDetail = (ws) => { _currentWorkspaceDetail = ws; _workspaceMode = 'read'; };
        const _clearWorkspaceDetail = () => { _workspaceUiGen++; _currentWorkspaceDetail = null; _workspaceMode = 'empty'; };
        const esc = (s) => String(s);
        const t = (k) => k;
        const els = {workspaceFormName: {value: 'New Name'}, workspaceFormPath: {value: '/srv/www'},
                     workspaceFormError: {style: {}, textContent: ''}, workspaceFormAsProject: {checked: true}};
        const $ = (id) => els[id] || null;
        const tick = () => new Promise(r => setImmediate(r));
        __FNS__
        const A = {path: '/srv/other', workspace_kind: '', project_id: '', name: 'A'};
        (async () => {
          __BODY__
        })().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
    """).replace("__FNS__", fns).replace("__BODY__", textwrap.dedent(body).strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_stale_create_project_response_does_not_clobber_new_form():
    """Late create_project response must not clobber a newer create form."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _form_harness(src, """
          _workspaceMode = 'create';
          _workspaceList = [A];
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/create_project');
          // User leaves the form and opens a fresh create form while the
          // create request (subprocess lane: seconds) is still pending.
          openWorkspaceCreate();
          const listBefore = _workspaceList;
          const renderedAfter = rendered;
          pendingApi[0].d.resolve({workspaces: [A, {path: '/srv/www', name: 'N'}],
                                   project: {path: '/srv/www'}});
          await run;
          assert.strictEqual(rendered, renderedAfter, 'late create response must not re-render the panel');
          assert.strictEqual(opened, null, 'late create response must not open the created detail over the new form');
          assert.strictEqual(_workspaceList, listBefore, 'late create response must not replace the catalog');
    """)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_create_project_response_still_applies_when_form_untouched():
    """Positive control: with no interruption the create response still lands."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _form_harness(src, """
          _workspaceMode = 'create';
          _workspaceList = [A];
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/create_project');
          pendingApi[0].d.resolve({workspaces: [A, {path: '/srv/www', name: 'N'}],
                                   project: {path: '/srv/www'}});
          await run;
          assert.strictEqual(rendered, 1, 'untouched form must still apply the response');
          assert.deepStrictEqual(opened && opened.path, '/srv/www');
    """)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_stale_plain_add_response_does_not_clobber_new_form():
    """Same class as the create branch: a late plain-add response must not
    clobber a newer form either (sibling sweep of the same guard)."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _form_harness(src, """
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = false;
          _workspaceList = [A];
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/add');
          openWorkspaceCreate();
          const listBefore = _workspaceList;
          const renderedAfter = rendered;
          pendingApi[0].d.resolve({workspaces: [A, {path: '/srv/www', name: 'N'}], path: '/srv/www'});
          await run;
          assert.strictEqual(rendered, renderedAfter, 'late add response must not re-render the panel');
          assert.strictEqual(opened, null, 'late add response must not open a detail over the new form');
          assert.strictEqual(_workspaceList, listBefore, 'late add response must not replace the catalog');
    """)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


# ── P2 siblings (omp independent review): delete/activate same-class guards ──

def _action_harness(src: str, body: str) -> str:
    fns = "\n".join([
        _extract_fn(src, "async function activateCurrentWorkspace(){"),
        _extract_fn(src, "async function deleteCurrentWorkspace(){"),
        _extract_fn(src, "function openWorkspaceCreate(){"),
    ])
    return textwrap.dedent("""
        const assert = require('assert');
        let _currentWorkspaceDetail = null, _workspaceMode = 'empty', _workspaceList = [];
        let _workspacePreFormDetail = null;
        let _workspaceUiGen = 0;
        let _currentPanel = 'workspaces';
        const deferred = () => { let r; const p = new Promise(res => r = res); p.resolve = r; return p; };
        let pendingApi = [];
        const api = (url, opts) => { const d = deferred(); pendingApi.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null, d}); return d; };
        let rendered = 0, detailRenders = 0;
        const renderWorkspacesPanel = () => { rendered++; };
        const showToast = () => {};
        const setStatus = () => {};
        const switchPanel = () => {};
        const _closeMobileSidebarAfterPanelSelection = () => {};
        const _renderWorkspaceForm = () => {};
        const _renderWorkspaceDetail = (ws) => { detailRenders++; _currentWorkspaceDetail = ws; _workspaceMode = 'read'; };
        const _clearWorkspaceDetail = () => { _workspaceUiGen++; _currentWorkspaceDetail = null; _workspaceMode = 'empty'; };
        const _setWorkspaceHeaderButtons = () => {};
        const esc = (s) => String(s);
        const t = (k) => k;
        const A = {path: '/srv/a', workspace_kind: '', project_id: '', name: 'A'};
        const B = {path: '/srv/b', workspace_kind: '', project_id: '', name: 'B'};
        let switchD = null;
        const switchToWorkspace = () => { switchD = deferred(); return switchD; };
        const showConfirmDialog = async () => true;
        const $ = () => null;
        const tick = () => new Promise(r => setImmediate(r));
        __FNS__
        (async () => {
          __BODY__
        })().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
    """).replace("__FNS__", fns).replace("__BODY__", textwrap.dedent(body).strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_stale_delete_response_does_not_clobber_newer_list():
    """Late remove response must not overwrite _workspaceList with its
    pre-interruption snapshot or re-render over a newer form."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _action_harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'read';
          _workspaceList = [A, B];
          const run = deleteCurrentWorkspace();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/remove');
          // User opens a fresh create form while the remove is in flight;
          // meanwhile a newer list (B still present, C added) arrives.
          openWorkspaceCreate();
          const listBefore = _workspaceList;
          const renderedAfter = rendered;
          pendingApi[0].d.resolve({workspaces: [B]});
          await run;
          assert.strictEqual(_workspaceList, listBefore, 'late remove response must not replace the catalog');
          assert.strictEqual(rendered, renderedAfter, 'late remove response must not re-render the panel');
    """)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_frontend_stale_activate_does_not_render_detail_over_new_form():
    """Late activate continuation must not re-render the old detail on top
    of a form the user opened while the switch was in flight."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _action_harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'read';
          const run = activateCurrentWorkspace();
          await tick();
          openWorkspaceCreate();
          assert.strictEqual(_workspaceMode, 'create');
          const detailBefore = detailRenders;
          switchD.resolve();
          await run;
          assert.strictEqual(detailRenders, detailBefore, 'late activate must not re-render detail over the new form');
          assert.strictEqual(_workspaceMode, 'create', 'late activate must not pull the view back to detail mode');
    """)
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout
