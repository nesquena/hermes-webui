"""#5763 round 12 (greptile P2): a stale mutation response must still refresh
the workspace list, while its form/detail side effects stay fenced.

The round-11 _workspaceUiGen guard rejected the WHOLE late response when the
user opened a detail or a form while remove/add/create/rename was in flight.
The server-side mutation already happened, so dropping the catalog update left
the picker stale (a removed workspace stayed clickable, a created one missing)
until a manual reload. The guard must fence only the form/detail side effects
(reopen detail, clear form snapshot, toast); the list refresh lands even when
the user moved on.

Round 13 (omp review) added the orthogonal recency gate: responses can complete
out of order, so a late response's snapshot may predate a newer list that has
already been published. Round 14 (omp review F1) corrected the gate's ordering
source: client send order proves nothing about snapshot freshness (the GET
endpoint reads without the mutation lock), so the SERVER stamps every
snapshot with a monotonic catalog revision and _publishWorkspaceList accepts
a list only if its rev is not older than the newest rev applied. The
generation guard fences navigation; the revision gate fences recency.

These tests pin the NEW contract and supersede the round-11 assertions that
required the catalog to stay untouched on a stale response.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _extract_fn_opt(src: str, header: str) -> str:
    """Same as _extract_fn but returns '' when the header is absent — lets the
    harness compile against pre-fix product code (the fallback gate defined
    in the preamble is then simply never called and the test fails red)."""
    return _extract_fn(src, header) if header in src else ""


# Fallback recency gate for pre-fix sources; the real _publishWorkspaceList
# extracted from panels.js (a hoisted function declaration) overrides it.
_GATE_FALLBACK = """
let _wsListPublishedRev = -1, _wsListPublishedBoot = null;
function _publishWorkspaceList(rev, list, boot, profile){ if (profile && typeof S !== 'undefined' && S.activeProfile && profile !== S.activeProfile) return false; if (typeof rev !== 'number' || !isFinite(rev)) { _workspaceList = list; return true; } boot = (boot === undefined) ? null : boot; if (boot !== _wsListPublishedBoot) { _wsListPublishedBoot = boot; _wsListPublishedRev = rev; _workspaceList = list; return true; } if (rev < _wsListPublishedRev) return false; _wsListPublishedRev = rev; _workspaceList = list; return true; }
"""


def _harness(src: str, body: str, extra_fns: list[str] | None = None) -> str:
    fns = [
        _extract_fn(src, "async function saveWorkspaceForm(){"),
        _extract_fn(src, "async function deleteCurrentWorkspace(){"),
        _extract_fn(src, "async function loadWorkspaceList(){"),
        _extract_fn(src, "function openWorkspaceCreate(){"),
        _extract_fn(src, "function openWorkspaceDetail("),
        _extract_fn_opt(src, "function _publishWorkspaceList("),
    ] + (extra_fns or [])
    return textwrap.dedent("""
        const assert = require('assert');
        let _currentWorkspaceDetail = null, _workspaceMode = 'empty', _workspaceList = [];
        let _workspacePreFormDetail = null;
        let _workspaceUiGen = 0;
        let _currentPanel = 'workspaces';
        const S = {activeProfile: null};
        __GATE__
        const deferred = () => { let r; const p = new Promise(res => r = res); p.resolve = r; return p; };
        let pendingApi = [];
        const api = (url, opts) => { const d = deferred(); pendingApi.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null, d}); return d; };
        let opened = null, rendered = 0, detailRenders = 0, toasts = 0;
        const renderWorkspacesPanel = (list) => { rendered++; lastRenderedList = list; };
        let lastRenderedList = null;
        const showToast = () => { toasts++; };
        const setStatus = () => {};
        const switchPanel = () => {};
        const _currentPanelGuard = () => {};
        const _closeMobileSidebarAfterPanelSelection = () => {};
        const closeWorkspacePathSuggestions = () => {};
        const _wireWorkspaceFormPathSuggestions = () => {};
        const _renderWorkspaceForm = () => { _workspaceMode = 'create'; };
        const _renderWorkspaceDetail = (ws) => { detailRenders++; _currentWorkspaceDetail = ws; _workspaceMode = 'read'; };
        const _clearWorkspaceDetail = () => { _workspaceUiGen++; _currentWorkspaceDetail = null; _workspaceMode = 'empty'; };
        const _setWorkspaceHeaderButtons = () => {};
        const syncWorkspaceDisplays = () => {};
        const esc = (s) => String(s);
        const t = (k) => k;
        const A = {path: '/srv/a', workspace_kind: '', project_id: '', name: 'A'};
        const B = {path: '/srv/b', workspace_kind: '', project_id: '', name: 'B'};
        const showConfirmDialog = async () => true;
        let switched = [];
        const switchToWorkspace = async (p, n) => { switched.push(p); };
        const showPromptDialog = async () => '/tmp/newdir';
        const els = {workspaceFormName: {value: 'New Name'}, workspaceFormPath: {value: '/srv/www'},
                     workspaceFormError: {style: {}, textContent: ''}, workspaceFormAsProject: {checked: true}};
        const $ = (id) => els[id] || null;
        const document = {querySelectorAll: () => []};
        const tick = () => new Promise(r => setImmediate(r));
        __FNS__
        (async () => {
          __BODY__
        })().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
    """).replace("__GATE__", _GATE_FALLBACK).replace("__FNS__", "\n".join(fns)).replace("__BODY__", textwrap.dedent(body).strip())


def _run_node(script: str) -> None:
    res = subprocess.run(["node", "-e", script], cwd=REPO_ROOT, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr or res.stdout


# ── remove: late response refreshes the list, form/detail side effects fenced ──

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_remove_refreshes_list_but_not_form_state():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'read';
          _workspaceList = [A, B];
          const run = deleteCurrentWorkspace();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/remove');
          // User switches to B's detail while the remove is in flight.
          openWorkspaceDetail(B);
          const renderedAfter = rendered;
          const detailBefore = detailRenders;
          const toastsBefore = toasts;
          pendingApi[0].d.resolve({workspaces: [B], rev: 1});
          await run;
          assert.deepStrictEqual(_workspaceList, [B], 'late remove must still refresh the catalog');
          assert.strictEqual(rendered, renderedAfter + 1, 'late remove must re-render the list');
          assert.strictEqual(detailRenders, detailBefore, 'late remove must not touch B\\'s detail');
          assert.strictEqual(_currentWorkspaceDetail, B, 'late remove must not clear the newer detail');
          assert.strictEqual(toasts, toastsBefore, 'late remove must not toast');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_remove_over_new_form_refreshes_list_without_reopening_detail():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'read';
          _workspaceList = [A, B];
          const run = deleteCurrentWorkspace();
          await tick();
          // User opens a fresh create form while the remove is in flight.
          openWorkspaceCreate();
          const renderedAfter = rendered;
          pendingApi[0].d.resolve({workspaces: [B], rev: 1});
          await run;
          assert.deepStrictEqual(_workspaceList, [B], 'late remove must still refresh the catalog');
          assert.strictEqual(rendered, renderedAfter + 1, 'late remove must re-render the list');
          assert.strictEqual(_workspaceMode, 'create', 'late remove must not pull the view out of the new form');
          assert.strictEqual(detailRenders, 0, 'late remove must not render a detail over the form');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_fresh_remove_still_clears_detail_and_toasts():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'read';
          _workspaceList = [A, B];
          const run = deleteCurrentWorkspace();
          await tick();
          pendingApi[0].d.resolve({workspaces: [B], rev: 1});
          await run;
          assert.deepStrictEqual(_workspaceList, [B]);
          assert.strictEqual(_currentWorkspaceDetail, null, 'fresh remove must clear the removed detail');
          assert.strictEqual(toasts, 1, 'fresh remove must toast');
    """)
    _run_node(script)


# ── create_project / plain add: same split ────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_create_project_refreshes_list_without_reopening_detail():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          _workspaceList = [A, B];
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/create_project');
          // User leaves the form and opens B's detail while create is pending.
          openWorkspaceDetail(B);
          const renderedAfter = rendered;
          const detailBefore = detailRenders;
          const toastsBefore = toasts;
          pendingApi[0].d.resolve({workspaces: [A, {path: '/srv/www', name: 'N'}],
                                   project: {path: '/srv/www'}});
          await run;
          assert.strictEqual(_workspaceList.length, 2, 'late create must still refresh the catalog');
          assert.strictEqual(rendered, renderedAfter + 1, 'late create must re-render the list');
          assert.strictEqual(_currentWorkspaceDetail, B, 'late create must not replace the newer detail');
          assert.strictEqual(detailRenders, detailBefore, 'late create must not re-render the newer detail');
          assert.strictEqual(toasts, toastsBefore, 'late create must not toast');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_plain_add_refreshes_list_without_reopening_detail():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = false;
          _workspaceList = [A, B];
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/add');
          openWorkspaceDetail(B);
          const renderedAfter = rendered;
          const detailBefore = detailRenders;
          const toastsBefore = toasts;
          pendingApi[0].d.resolve({workspaces: [A, {path: '/srv/www', name: 'N'}], path: '/srv/www'});
          await run;
          assert.strictEqual(_workspaceList.length, 2, 'late add must still refresh the catalog');
          assert.strictEqual(rendered, renderedAfter + 1, 'late add must re-render the list');
          assert.strictEqual(_currentWorkspaceDetail, B, 'late add must not replace the newer detail');
          assert.strictEqual(detailRenders, detailBefore, 'late add must not re-render a detail over B');
          assert.strictEqual(toasts, toastsBefore, 'late add must not toast');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_fresh_create_project_still_opens_detail_and_toasts():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          _workspaceList = [A];
          const run = saveWorkspaceForm();
          await tick();
          pendingApi[0].d.resolve({workspaces: [A, {path: '/srv/www', name: 'N'}],
                                   project: {path: '/srv/www'}});
          await run;
          assert.strictEqual(rendered, 1, 'untouched create must apply the response');
          assert.strictEqual(_currentWorkspaceDetail && _currentWorkspaceDetail.path, '/srv/www',
                             'fresh create must open the created detail');
          assert.strictEqual(toasts, 1, 'fresh create must toast');
    """)
    _run_node(script)


# ── rename: same class (round-11 guard landed on rename first; same split) ──

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_rename_refreshes_list_without_reopening_detail():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'edit';
          _workspaceList = [A, B];
          const run = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/rename');
          // User cancels the edit form and opens B while rename is in flight.
          openWorkspaceDetail(B);
          const renderedAfter = rendered;
          const detailBefore = detailRenders;
          const toastsBefore = toasts;
          pendingApi[0].d.resolve({ok: true});
          await tick();
          pendingApi[1].d.resolve({workspaces: [{...A, name: 'Renamed'}, B]});
          await run;
          assert.strictEqual(_workspaceList.length, 2, 'late rename must still refresh the catalog');
          assert.strictEqual(_workspaceList[0].name, 'Renamed', 'late rename must land the new name in the list');
          assert.strictEqual(rendered, renderedAfter + 1, 'late rename must re-render the list');
          assert.strictEqual(_currentWorkspaceDetail, B, 'late rename must not replace B\\'s detail');
          assert.strictEqual(detailRenders, detailBefore, 'late rename must not re-render a detail over B');
          assert.strictEqual(toasts, toastsBefore, 'late rename must not toast');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_fresh_rename_still_reopens_detail_and_toasts():
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'edit';
          _workspaceList = [A, B];
          const run = saveWorkspaceForm();
          await tick();
          pendingApi[0].d.resolve({ok: true});
          await tick();
          pendingApi[1].d.resolve({workspaces: [{...A, name: 'Renamed'}, B]});
          await run;
          assert.strictEqual(rendered, 1, 'untouched rename must apply the response');
          assert.strictEqual(toasts, 1, 'fresh rename must toast');
          assert.strictEqual(detailRenders >= 1, true, 'fresh rename must reopen the detail');
    """)
    _run_node(script)


# --- omp review findings (round 13): recency gate on list publishes ---
# A response's list snapshot is taken when the server finishes that request,
# so two in-flight requests can complete out of order. The generation guard
# fences navigation; recency needs its own gate: a response may publish its
# list (and re-render) only if no newer request has already published one.


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_remove_snapshot_cannot_clobber_newer_add_list():
    """Remove is slow; meanwhile the create form's add completes and publishes
    [B, C]. The late remove response carries a pre-C snapshot and must not
    erase C nor re-render (a render would clear C's freshly-opened detail)."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'read';
          _workspaceList = [A, B];
          const rm = deleteCurrentWorkspace();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/remove');
          openWorkspaceCreate();
          els.workspaceFormAsProject.checked = false;
          const add = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[1].url, '/api/workspaces/add');
          pendingApi[1].d.resolve({workspaces: [B, {path: '/srv/www', name: 'C'}], path: '/srv/www', rev: 2});
          await add;
          assert.strictEqual(_workspaceList.length, 2, 'add response must publish');
          const renderedAfter = rendered;
          pendingApi[0].d.resolve({workspaces: [B], rev: 1});
          await rm;
          assert.strictEqual(_workspaceList.length, 2, 'stale remove snapshot must not erase the newer add');
          assert.strictEqual(rendered, renderedAfter, 'stale remove must not re-render (no detail clear)');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_in_order_responses_still_publish():
    """Reverse completion order: remove lands first, add lands after — both
    must publish, newest last."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'read';
          _workspaceList = [A, B];
          const rm = deleteCurrentWorkspace();
          await tick();
          openWorkspaceCreate();
          els.workspaceFormAsProject.checked = false;
          const add = saveWorkspaceForm();
          await tick();
          pendingApi[0].d.resolve({workspaces: [B], rev: 1});
          await rm;
          assert.strictEqual(_workspaceList.length, 1, 'remove response must publish');
          pendingApi[1].d.resolve({workspaces: [B, {path: '/srv/www', name: 'C'}], path: '/srv/www', rev: 2});
          await add;
          assert.strictEqual(_workspaceList.length, 2, 'newer add response must publish after older remove');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_rename_get_cannot_clobber_newer_add_list():
    """Rename POST landed, its refresh GET is in flight; a create-form add
    completes first and publishes [A, B, C]. The rename GET snapshot predates
    C and must not erase it."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _currentWorkspaceDetail = A;
          _workspaceMode = 'edit';
          _workspaceList = [A, B];
          const ren = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/rename');
          pendingApi[0].d.resolve({ok: true});
          await tick();
          assert.strictEqual(pendingApi[1].url, '/api/workspaces');
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = false;
          const add = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[2].url, '/api/workspaces/add');
          pendingApi[2].d.resolve({workspaces: [A, B, {path: '/srv/www', name: 'C'}], path: '/srv/www', rev: 2});
          await add;
          const renderedAfter = rendered;
          pendingApi[1].d.resolve({workspaces: [A, B], rev: 1});
          await ren;
          assert.strictEqual(_workspaceList.length, 3, 'stale rename GET must not erase the newer add');
          assert.strictEqual(rendered, renderedAfter, 'stale rename GET must not re-render');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_background_load_cannot_clobber_newer_mutation_list():
    """A panel load GET is in flight when a create-form add completes and
    publishes [B, C]; the late load snapshot predates C and must not erase
    it. loadWorkspaceList shares the same recency gate."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = false;
          _workspaceList = [A, B];
          const ld = loadWorkspaceList();
          await tick();
          const add = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces');
          assert.strictEqual(pendingApi[1].url, '/api/workspaces/add');
          pendingApi[1].d.resolve({workspaces: [B, {path: '/srv/www', name: 'C'}], path: '/srv/www', rev: 2});
          await add;
          pendingApi[0].d.resolve({workspaces: [A, B], rev: 1});
          await ld;
          assert.strictEqual(_workspaceList.some(w => w.name === 'C'), true, 'stale load snapshot must not erase the newer add');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_add_snapshot_cannot_resurrect_deleted_row():
    """Create-form add is slow; meanwhile the user deletes row B and that
    response lands. The late add snapshot still contains B and must not
    resurrect it nor re-render."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = false;
          _workspaceList = [A, B];
          const add = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/add');
          _currentWorkspaceDetail = B;
          _workspaceMode = 'read';
          const rm = deleteCurrentWorkspace();
          await tick();
          assert.strictEqual(pendingApi[1].url, '/api/workspaces/remove');
          pendingApi[1].d.resolve({workspaces: [A], rev: 2});
          await rm;
          assert.strictEqual(_workspaceList.length, 1, 'remove response must publish');
          const renderedAfter = rendered;
          pendingApi[0].d.resolve({workspaces: [A, B], path: '/srv/www', rev: 1});
          await add;
          assert.strictEqual(_workspaceList.some(w => w.path === '/srv/b'), false, 'stale add snapshot must not resurrect the deleted row');
          assert.strictEqual(rendered, renderedAfter, 'stale add must not re-render');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stale_create_project_snapshot_cannot_resurrect_deleted_row():
    """create_project waits seconds on the subprocess lane; a delete that
    lands first must not be undone by the late project response."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = true;
          _workspaceList = [A, B];
          const proj = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/create_project');
          _currentWorkspaceDetail = B;
          _workspaceMode = 'read';
          const rm = deleteCurrentWorkspace();
          await tick();
          pendingApi[1].d.resolve({workspaces: [A], rev: 2});
          await rm;
          const renderedAfter = rendered;
          pendingApi[0].d.resolve({workspaces: [A, B], project: {path: '/srv/www'}, rev: 1});
          await proj;
          assert.strictEqual(_workspaceList.some(w => w.path === '/srv/b'), false, 'stale create_project snapshot must not resurrect the deleted row');
          assert.strictEqual(rendered, renderedAfter, 'stale create_project must not re-render');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_slow_mutation_publishes_over_later_sent_stale_get():
    """omp review F1: create_project commits slowly on the subprocess lane;
    a GET sent AFTER it completes first with a pre-commit snapshot (rev 1).
    Send order proves nothing — the GET's revision is older, so the create's
    rev-2 snapshot must win even though the GET landed first. (A client
    send-sequence gate would wrongly reject the create here.)"""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = true;
          _workspaceList = [A, B];
          const proj = saveWorkspaceForm();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/create_project');
          const ld = loadWorkspaceList();
          await tick();
          assert.strictEqual(pendingApi[1].url, '/api/workspaces');
          pendingApi[1].d.resolve({workspaces: [A, B], rev: 1});
          await ld;
          pendingApi[0].d.resolve({workspaces: [A, B, {path: '/srv/www', name: 'P'}], project: {path: '/srv/www'}, rev: 2});
          await proj;
          assert.strictEqual(_workspaceList.some(w => w.name === 'P'), true, 'the committed create (rev 2) must publish over the later-sent stale GET (rev 1)');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_panel_render_uses_gated_cache():
    """omp review F2: loadWorkspacesPanel must render the gated cache, not
    the raw response — a rev-rejected snapshot must not reach the DOM either
    (a render would re-show the deleted row and clear its detail)."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(
        src,
        """
          els.workspacesPanel = {innerHTML: ''};
          _workspaceList = [A, B];
          const ld = loadWorkspacesPanel();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces');
          _currentWorkspaceDetail = B;
          _workspaceMode = 'read';
          const rm = deleteCurrentWorkspace();
          await tick();
          pendingApi[1].d.resolve({workspaces: [A], rev: 2});
          await rm;
          pendingApi[0].d.resolve({workspaces: [A, B], rev: 1});
          await ld;
          assert.deepStrictEqual(lastRenderedList, [A], 'panel render must use the gated cache, not the stale response');
        """,
        extra_fns=[_extract_fn(src, "async function loadWorkspacesPanel(){")],
    )
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_server_restart_boot_token_resets_gate():
    """omp review r14 P1-1: a server restart resets _CATALOG_REV to 0, so
    every new-process response would lose a `rev < published` comparison
    forever and freeze the picker. Responses carry a per-process boot token;
    a changed token resets the gate and the response publishes."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          _workspaceMode = 'create';
          els.workspaceFormAsProject.checked = false;
          _workspaceList = [A, B];
          const add = saveWorkspaceForm();
          await tick();
          pendingApi[0].d.resolve({workspaces: [A, B, {path: '/srv/www', name: 'C'}], path: '/srv/www', rev: 5, boot: 'x'});
          await add;
          const ld = loadWorkspaceList();
          await tick();
          pendingApi[1].d.resolve({workspaces: [A], rev: 1, boot: 'y'});
          await ld;
          assert.deepStrictEqual(_workspaceList, [A], 'a new boot token must reset the gate (server restart)');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wrong_profile_response_is_dropped():
    """omp review r14 P2-3: the revision counter is process-global, so a
    stale old-profile GET finishing after a profile switch can carry an
    equal-or-higher rev and paint the wrong profile's list. The GET echoes
    the profile it served; a mismatch against S.activeProfile is dropped."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          S.activeProfile = 'mine';
          _workspaceList = [A];
          const ld = loadWorkspaceList();
          await tick();
          pendingApi[0].d.resolve({workspaces: [A, B], rev: 3, profile: 'other'});
          await ld;
          assert.deepStrictEqual(_workspaceList, [A], 'a response from another profile must not publish');
          const ld2 = loadWorkspaceList();
          await tick();
          pendingApi[1].d.resolve({workspaces: [A, B], rev: 4, profile: 'mine'});
          await ld2;
          assert.strictEqual(_workspaceList.length, 2, 'the matching-profile response publishes normally');
    """)
    _run_node(script)


def test_uijs_add_as_workspace_publishes_through_gate():
    """omp review r14 P2-2: promptNewFolder's add-as-workspace branch wrote
    _workspaceList directly, resurrecting rows a newer publish had removed
    and poisoning the cache the gate protects."""
    src = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    assert "_publishWorkspaceList(data.rev,data.workspaces||[],data.boot,data.profile)" in src, \
        "add-as-workspace must publish through the rev+profile gate"
    assert "_workspaceList=data.workspaces||_workspaceList||[]" not in src


def test_workspace_dropdowns_render_gated_cache():
    """omp review r14 P3-2: the sidebar/composer dropdowns rendered the raw
    loadWorkspaceList response, so a rev-rejected snapshot still reached the
    DOM. They must render the gated cache."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "renderWorkspaceDropdownInto(dd, data.workspaces" not in src
    assert src.count("renderWorkspaceDropdownInto(dd, _workspaceList") >= 2


# ── r15 P2 / P3: profile echo on mutations; rejected snapshot must not
#    supply the promptWorkspacePath switch target ──

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_gate_rejects_profile_mismatch():
    """r15 P2: the rev counter is process-global, so a stale old-profile
    mutation response is always rev-acceptable after a profile switch.
    The gate must drop payloads whose profile mismatches S.activeProfile."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          S.activeProfile = 'mine';
          assert.strictEqual(_publishWorkspaceList(10, [A], 'b1', 'other'), false,
            'wrong-profile payload must be rejected');
          assert.strictEqual(_workspaceList.length, 0, 'rejected payload must not publish');
          assert.strictEqual(_publishWorkspaceList(10, [A], 'b1', 'mine'), true,
            'matching profile must publish');
          assert.strictEqual(_publishWorkspaceList(11, [A, B], 'b1', null), true,
            'missing profile must publish (old-server compat)');
          assert.strictEqual(_publishWorkspaceList(12, [B], 'b1', undefined), true,
            'undefined profile must publish');
    """)
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_old_profile_add_response_does_not_switch():
    """r15 P2 integration: user on profile A submits an add, switches to B,
    then A's response lands — rev-acceptable but must not publish or switch."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          S.activeProfile = 'mine';
          S.session = {workspace: '/x'};
          const run = promptWorkspacePath();
          await tick();
          assert.strictEqual(pendingApi[0].url, '/api/workspaces/add');
          pendingApi[0].d.resolve({ok: true, path: '/tmp/newdir',
            workspaces: [{path: '/tmp/newdir', name: 'newdir'}],
            rev: 1, boot: 'b1', profile: 'other'});
          await run;
          assert.strictEqual(switched.length, 0,
            'old-profile add must not switch to its row');
          assert.strictEqual(_workspaceList.length, 0,
            'old-profile add must not publish into the new profile');
    """, extra_fns=[_extract_fn_opt(src, "async function promptWorkspacePath(){")])
    _run_node(script)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_rejected_snapshot_must_not_supply_switch_target():
    """r15 P3-1: when the add response is rev-rejected, the newer published
    list postdates the add commit; if the row is absent there it was deleted
    — the fallback must NOT resurrect it from the rejected snapshot."""
    src = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    script = _harness(src, """
          S.session = {workspace: '/x'};
          // A newer list already published (rev 5) without the new row:
          // the row was deleted after this add committed.
          assert.strictEqual(_publishWorkspaceList(5, [A], 'b1', null), true);
          const run = promptWorkspacePath();
          await tick();
          pendingApi[0].d.resolve({ok: true, path: '/tmp/newdir',
            workspaces: [A, {path: '/tmp/newdir', name: 'newdir'}],
            rev: 4, boot: 'b1', profile: null});
          await run;
          assert.strictEqual(switched.length, 0,
            'a rev-rejected snapshot must not supply the switch target');
    """, extra_fns=[_extract_fn_opt(src, "async function promptWorkspacePath(){")])
    _run_node(script)
