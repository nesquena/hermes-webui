"""Regression tests for #6709 — closing a preview must reconcile browse-state
visibility (tree OR empty-state placeholder) and preserve the tree's scroll.

Round 2 (`9a85e328`) made `clearPreview()` defer browse-state visibility to
`renderFileTree()`. Round 3 (`5aee86e0`) closed the close-→-reopen branch by
always re-rendering after clearing the preview path.

Round 4 (this revision) responds to the exact-head gate:
  1. Current master renamed `_visibleWorkspaceEntries` → `_workspaceEntriesForRender`
     and added `_noteWorkspaceBirthtimeSupport`; the Node harness preambles below
     are refreshed to match (the behavior assertions are unchanged).
  2. `renderFileTree()` now runs while `#fileTree` is hidden (preview open), and a
     hidden container reports `scrollTop=0` and ignores writes — so closing a
     preview reset a long tree to the top. `openFile()` snapshots the last
     readable scroll position before hiding the tree, and the renderer restores
     from that snapshot (never from the zeroed live read) until the browse tree
     is visible again.

Two coverage layers:
  - Node-VM behavioral tests drive the REAL `renderFileTree()` / `clearPreview()`
    / `openFile()` bodies through the lifecycle with a fake DOM that emulates the
    two real-browser behaviors this contract depends on (hidden read = 0, hidden
    write ignored — verified against Chromium in the PR evidence).
  - Playwright tests run the same lifecycle in a real Chromium against the
    isolated test server, proving the browser-side contract end to end.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _read(rel: str) -> str:
    with open(REPO_ROOT / rel, encoding="utf-8") as f:
        return f.read()


def _extract_render_file_tree() -> str:
    src = _read("static/ui.js")
    start = src.find("function renderFileTree(){")
    assert start >= 0, "renderFileTree not found in static/ui.js"
    # Find the end: the function ends at the matching closing brace at column 0
    # (the function body uses 2-space indent; the closing brace of the function
    # is the first line with exactly "}" after the start).
    body_start = src.find("{", start)
    depth = 0
    i = body_start
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError("could not find renderFileTree closing brace")


def _extract_clear_preview() -> str:
    src = _read("static/boot.js")
    start = src.find("function clearPreview(opts={}){")
    assert start >= 0, "clearPreview not found in static/boot.js"
    # The default-param `opts={}` contains a brace pair BEFORE the body —
    # find the body brace as the FIRST `{` that opens a statement after the
    # parameter list closes. Simplest robust approach: locate the closing
    # paren of the parameter list, then the `{` that follows it.
    params_close = src.find(")", start)
    body_open = src.find("{", params_close)
    assert body_open > start, "clearPreview body brace not found"
    depth = 0
    i = body_open
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError("could not find clearPreview closing brace")


def _extract_open_file() -> str:
    src = _read("static/workspace.js")
    start = src.find("async function openFile(path, opts={}){")
    assert start >= 0, "openFile not found in static/workspace.js"
    params_close = src.find(")", start)
    body_open = src.find("{", params_close)
    assert body_open > start, "openFile body brace not found"
    depth = 0
    i = body_open
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError("could not find openFile closing brace")


def _run_node(js: str) -> subprocess.CompletedProcess:
    assert NODE, "node is required"
    return subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )


# Shared fake DOM + globals for the Node-VM harnesses. The fake #fileTree
# emulates the two real-browser behaviors this contract depends on: a
# display:none container reports scrollTop=0 and ignores scrollTop writes
# (both verified against Chromium in the PR evidence).
_NODE_PREAMBLE = r"""
const store = {};
const fileTreeBox = {
  id: 'fileTree', style: {}, innerHTML: '',
  _scrollTop: 0,
  get scrollTop(){ return this.style.display === 'none' ? 0 : this._scrollTop; },
  set scrollTop(v){ if(this.style.display !== 'none'){ this._scrollTop = Math.max(0, Number(v) || 0); } },
  appendChild(){}, remove(){}, setAttribute(){}, getAttribute(){ return null; }, querySelector(){ return null; },
  classList: {add(){}, remove(){}, toggle(){}, contains(){ return false; }},
};
store.fileTree = fileTreeBox;
function $id(id){
  if(id === 'fileTree') return fileTreeBox;
  if(store[id]) return store[id];
  const el = {
    id, style: {}, classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}},
    innerHTML:'', textContent:'', scrollTop:0, appendChild(){}, remove(){},
    setAttribute(){}, getAttribute(){return null;}, querySelector(){return null;},
  };
  store[id] = el;
  return el;
}
const $ = $id;
const S = {session:{workspace:'/ws'}, entries: null, currentDir:'.', _dirCache:{}};
let _previewCurrentPath='', _previewCurrentMode='', _previewDirty=false;
let _workspacePanelMode='preview';
// #6709 gate round 7: declared here because the real openWorkspacePanel() /
// closeWorkspacePanel() bodies record and read it.
let _workspacePanelRetainedMode=null;
function t(k){ return k; }
function _workspaceEntriesForRender(entries){ return Array.isArray(entries)?entries:[]; }
function _noteWorkspaceBirthtimeSupport(){}
function _syncWorkspaceBirthtimeSupportScope(){}
function _renderTreeItems(box, items){ box.innerHTML='items:'+items.length; }
function closeWorkspacePanel(){ _workspacePanelMode='closed'; }
function openWorkspacePanel(mode){ _workspacePanelMode=mode; }
function syncWorkspacePanelUI(){}
function _hasWorkspacePreviewVisible(){ return !!_previewCurrentPath; }

"""

# Stubs for the extracted openFile() body — only the text-preview path runs in
# these harnesses; the other branches are stubbed to keep the real body driving.
_OPEN_FILE_STUBS = r"""
const DOWNLOAD_EXTS = new Set(['.doc','.zip']);
const IMAGE_EXTS = new Set(['.png']);
const AUDIO_EXTS = new Set(['.mp3']);
const VIDEO_EXTS = new Set(['.mp4']);
const PDF_EXTS = new Set(['.pdf']);
const MD_EXTS = new Set(['.md']);
const HTML_EXTS = new Set(['.html']);
function fileExt(p){ const i=p.lastIndexOf('.'); return i>=0?p.slice(i).toLowerCase():''; }
let _previewServerEditable = null;
let _previewSaveRoute = '/api/file/save';
let _previewOfficeFormat = '';
let _previewPreviewKind = '';
function renderFileBreadcrumb(){}
function showPreview(){}
function _workspaceRouteForPath(){ return '/api/file/read'; }
async function api(){ return {content:'#6709 scroll harness content'}; }
function renderCodePreviewContent(){}
function setStatus(){}
function showToast(){}
function downloadFile(){}
function _workspaceEscapeGrantForPath(){ return null; }
function _clearWorkspaceEscapeGrant(){}

"""


def _lifecycle_harness(entries_json: str, expect_empty_after_close: bool) -> str:
    """Build a Node script that drives the REAL renderFileTree + clearPreview
    bodies through the preview-open → background-refresh → preview-close
    lifecycle with a minimal fake DOM."""
    render_ft = _extract_render_file_tree()
    clear_pv = _extract_clear_preview()
    lifecycle = r"""
// ── Lifecycle: open preview → background refresh empties dir → close preview
S.entries = __ENTRIES__;
_previewCurrentPath = '/ws/file.txt';
_previewCurrentMode = 'code';
// background refresh while preview open (loadDir → renderFileTree)
renderFileTree();
const duringPreview = {
  treeDisplay: store.fileTree.style.display,
  emptyDisplay: store.wsEmptyState.style.display,
};
// close the preview (keep panel open → browse mode)
clearPreview({keepPanelOpen:true});
const afterClose = {
  treeDisplay: store.fileTree.style.display,
  emptyDisplay: store.wsEmptyState.style.display,
  panelMode: _workspacePanelMode,
};
const result = {duringPreview, afterClose};
console.log('LIFECYCLE ' + JSON.stringify(result));
"""
    # entries_json is a JSON array literal; inject as JS directly
    lifecycle = lifecycle.replace("__ENTRIES__", entries_json)
    # This harness deliberately keeps the preamble's panel-mode stubs: it asserts
    # the renderFileTree/clearPreview tree-vs-empty-state contract, not the panel
    # ownership contract (see _close_panel_reopen_harness for that).
    return _NODE_PREAMBLE + render_ft + "\n" + clear_pv + "\n" + lifecycle


def _close_panel_reopen_harness(entries_json: str) -> str:
    """Build a Node script that drives clearPreview({keepPanelOpen:false})
    (closes the panel entirely) and then reopens it in browse mode, verifying
    the tree or empty-state placeholder is visible after reopen.

    The Greptile bot identified this gap: when closing the panel (not just
    the preview), the original code skipped renderFileTree() because it
    assumed the next explicit open would render fresh state — but
    openWorkspacePanel('browse') does not call renderFileTree(), leaving both
    the tree and empty-state placeholder hidden on reopen.

    #6709 gate round 7: this harness used to keep the preamble's single-line
    `openWorkspacePanel` / `closeWorkspacePanel` / `_hasWorkspacePreviewVisible`
    stubs. `openWorkspacePanel(mode){ _workspacePanelMode=mode; }` satisfied the
    "reopen → browse" assertion unconditionally, so the real
    openWorkspacePanel() normalization could regress without this test noticing —
    a false green. The real bodies are spliced in now, which is what makes the
    assertion load-bearing.
    """
    pre = _NODE_PREAMBLE
    for _name in (
        "closeWorkspacePanel",
        "openWorkspacePanel",
        "syncWorkspacePanelUI",
        "_hasWorkspacePreviewVisible",
    ):
        pre = re.sub(r"^function %s\([^)]*\)\{[^\n]*\}\n" % _name, "", pre, flags=re.M)
    fns = "\n".join(
        _extract_boot_function(n)
        for n in (
            "_setWorkspacePanelMode",
            "openWorkspacePanel",
            "closeWorkspacePanel",
            "_hasWorkspacePreviewVisible",
            "syncWorkspacePanelUI",
        )
    )
    render_ft = _extract_render_file_tree()
    clear_pv = _extract_clear_preview()
    lifecycle = r"""
// ── Lifecycle: preview → close panel → reopen
const pa = $id('previewArea');
const pset = new Set();
pa.classList = {add(c){pset.add(c);}, remove(c){pset.delete(c);}, contains(c){return pset.has(c);}};
pa.classList.add('visible');           // openFile() sets this alongside the path
S.entries = __ENTRIES__;
_previewCurrentPath = '/ws/file.txt';
_previewCurrentMode = 'code';
// background refresh while preview open
renderFileTree();
const duringPreview = {
  treeDisplay: store.fileTree.style.display,
  emptyDisplay: store.wsEmptyState.style.display,
};
// close panel (not just preview — keepPanelOpen=false)
clearPreview({keepPanelOpen:false});
const afterClosePanel = {
  treeDisplay: store.fileTree.style.display,
  emptyDisplay: store.wsEmptyState.style.display,
  panelMode: _workspacePanelMode,
};
// reopen panel in browse mode
openWorkspacePanel('browse');
const afterReopen = {
  treeDisplay: store.fileTree.style.display,
  emptyDisplay: store.wsEmptyState.style.display,
  panelMode: _workspacePanelMode,
};
const result = {duringPreview, afterClosePanel, afterReopen};
console.log('LIFECYCLE ' + JSON.stringify(result));
"""
    lifecycle = lifecycle.replace("__ENTRIES__", entries_json)
    return (
        pre
        + _PANEL_MODE_SHIM
        + "\n"
        + render_ft
        + "\n"
        + clear_pv
        + "\n"
        + fns
        + "\n"
        + lifecycle
    )


# ── Scroll lifecycle harness (#6709 gate: scroll/read-position regression) ────
#
# Drives the REAL openFile() + renderFileTree() + clearPreview() bodies through
# the same preview lifecycle and asserts the browse tree keeps its reading
# position: the snapshot is captured before the tree is hidden, survives a
# background refresh and a preview-close render, and is consumed once the
# visible browse tree is back.

_SCROLL_VARIANTS = {
    # Ordinary close: open → background refresh → close (keep panel open).
    "ordinary": {
        "extra": "const extra = null;",
        "close": "clearPreview({keepPanelOpen:true});",
        "post": "",
    },
    # File-to-file switch: the second openFile() runs with the tree already
    # hidden and must not clobber the snapshot with a zeroed live read.
    "switch": {
        "extra": (
            "await openFile('file-031.txt');\n"
            "renderFileTree();\n"
            "const extra = {snapshot: S._wsBrowseScrollTop, scope: S._wsBrowseScrollScope};"
        ),
        "close": "clearPreview({keepPanelOpen:true});",
        "post": "",
    },
    # Empty refresh: a refresh that empties the directory while the preview is
    # open must still reconcile the placeholder + drop the snapshot cleanly.
    "empty": {
        "extra": "S.entries = [];\nrenderFileTree();\nconst extra = null;",
        "close": "clearPreview({keepPanelOpen:true});",
        "post": "",
    },
    # Whole-panel close → reopen: the close-path render runs while the panel is
    # collapsing; reopening must reveal the tree at the preserved position.
    "panelclose": {
        "extra": "const extra = null;",
        "close": "_workspacePanelMode = 'preview';\nclearPreview();",
        "post": (
            "openWorkspacePanel('browse');\n"
            "postReopen = {display: fileTreeBox.style.display, "
            "scrollTop: fileTreeBox.scrollTop, panelMode: _workspacePanelMode};"
        ),
    },
}


def _scroll_lifecycle_harness(entries_json: str, variant: str) -> str:
    steps = _SCROLL_VARIANTS[variant]
    render_ft = _extract_render_file_tree()
    clear_pv = _extract_clear_preview()
    open_file = _extract_open_file()
    lifecycle = r"""
(async()=>{
S.entries = __ENTRIES__;
renderFileTree();
// the reader scrolls a long tree
fileTreeBox.scrollTop = 600;
const before = {display: fileTreeBox.style.display, scrollTop: fileTreeBox.scrollTop};
// open a preview (hides the tree)
await openFile('file-030.txt');
const open = {
  display: fileTreeBox.style.display,
  scrollTop: fileTreeBox.scrollTop,
  snapshot: S._wsBrowseScrollTop,
};
// background refresh while the preview is open (loadDir → renderFileTree)
renderFileTree();
const refresh = {display: fileTreeBox.style.display, snapshot: S._wsBrowseScrollTop};
__EXTRA__
let postReopen = null;
__CLOSE__
const close = {
  display: fileTreeBox.style.display,
  scrollTop: fileTreeBox.scrollTop,
  snapshot: S._wsBrowseScrollTop,
  panelMode: _workspacePanelMode,
  emptyDisplay: store.wsEmptyState ? store.wsEmptyState.style.display : null,
};
__POST__
console.log('SCROLL ' + JSON.stringify({before: before, open: open, refresh: refresh,
  extra: extra, close: close, postReopen: postReopen}));
})().catch(function(e){ console.error(e && e.stack ? e.stack : String(e)); process.exit(1); });
"""
    lifecycle = lifecycle.replace("__ENTRIES__", entries_json)
    lifecycle = lifecycle.replace("__EXTRA__", steps["extra"])
    lifecycle = lifecycle.replace("__CLOSE__", steps["close"])
    lifecycle = lifecycle.replace("__POST__", steps["post"])
    return (
        _NODE_PREAMBLE + render_ft + "\n" + clear_pv + "\n" + _OPEN_FILE_STUBS
        + open_file + "\n" + lifecycle
    )


def _scroll_entries() -> str:
    return json.dumps(
        [
            {"name": f"file-{i:03d}.txt", "path": f"file-{i:03d}.txt", "type": "file"}
            for i in range(80)
        ]
    )


def _run_scroll(variant: str) -> dict:
    js = _scroll_lifecycle_harness(_scroll_entries(), variant)
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "SCROLL" in proc.stdout, proc.stdout
    payload = proc.stdout.split("SCROLL ", 1)[1].strip()
    return json.loads(payload)


# ── Behavioral lifecycle tests (real function bodies, Node VM) ──────────────


def test_empty_refresh_placeholder_hidden_during_preview_visible_after_close():
    """The maintainer-requested regression: a background refresh that empties
    the directory while a preview is open must leave the empty-state
    placeholder hidden during the preview and VISIBLE after closing it."""
    js = _lifecycle_harness("[]", expect_empty_after_close=True)
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "LIFECYCLE" in proc.stdout, proc.stdout
    # parse the JSON after LIFECYCLE
    payload = proc.stdout.split("LIFECYCLE ", 1)[1].strip()
    data = json.loads(payload)
    during = data["duringPreview"]
    after = data["afterClose"]
    # during preview: tree hidden AND empty-state hidden
    assert during["treeDisplay"] == "none", during
    assert during["emptyDisplay"] == "none", during
    # after close: empty-state visible (the regression), panel in browse mode
    assert after["emptyDisplay"] == "flex", after
    assert after["panelMode"] == "browse", after


def test_nonempty_refresh_tree_hidden_during_preview_visible_after_close():
    """The round-1 behavior must be preserved: non-empty refresh keeps the
    tree rebuilt while hidden, and closing the preview reveals it."""
    js = _lifecycle_harness(
        '[{"name":"a.txt","type":"file","path":"/ws/a.txt"}]',
        expect_empty_after_close=False,
    )
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "LIFECYCLE" in proc.stdout, proc.stdout
    payload = proc.stdout.split("LIFECYCLE ", 1)[1].strip()
    data = json.loads(payload)
    during = data["duringPreview"]
    after = data["afterClose"]
    assert during["treeDisplay"] == "none", during
    assert after["treeDisplay"] == "", after  # restored (renderer default)
    assert after["panelMode"] == "browse", after


def test_close_panel_reopen_shows_tree_or_empty_state_nonempty():
    """Greptile finding: closing the panel entirely (keepPanelOpen=false)
    must not leave the tree hidden for a later reopen. After close + reopen
    in browse mode, the non-empty tree must be visible."""
    js = _close_panel_reopen_harness(
        '[{"name":"a.txt","type":"file","path":"/ws/a.txt"}]'
    )
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "LIFECYCLE" in proc.stdout, proc.stdout
    payload = proc.stdout.split("LIFECYCLE ", 1)[1].strip()
    data = json.loads(payload)
    during = data["duringPreview"]
    after_close = data["afterClosePanel"]
    after_reopen = data["afterReopen"]
    # during preview: tree hidden
    assert during["treeDisplay"] == "none", during
    # after closing the panel: panel is closed; the renderer may have already
    # revealed the tree (always-call fix) or kept it hidden behind the panel —
    # the invariant that matters is the REOPEN state
    assert after_close["panelMode"] == "closed", after_close
    # after reopen: tree must be visible — the Greptile regression
    assert after_reopen["treeDisplay"] == "", after_reopen
    assert after_reopen["panelMode"] == "browse", after_reopen


def test_close_panel_reopen_shows_empty_state():
    """Greptile finding, empty-directory variant: after closing the panel
    over a preview of an emptied directory and reopening, the empty-state
    placeholder must be visible (not a blank panel)."""
    js = _close_panel_reopen_harness("[]")
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "LIFECYCLE" in proc.stdout, proc.stdout
    payload = proc.stdout.split("LIFECYCLE ", 1)[1].strip()
    data = json.loads(payload)
    during = data["duringPreview"]
    after_close = data["afterClosePanel"]
    after_reopen = data["afterReopen"]
    # during preview: empty-state hidden
    assert during["emptyDisplay"] == "none", during
    assert after_close["panelMode"] == "closed", after_close
    # after reopen: empty-state placeholder visible
    assert after_reopen["emptyDisplay"] == "flex", after_reopen
    assert after_reopen["panelMode"] == "browse", after_reopen


# ── Scroll/read-position regression (#6709 gate blocker, round 4) ────────────


def test_ordinary_close_preserves_tree_scroll():
    """Closing a preview must not reset a long tree to the top: the snapshot is
    captured before the tree is hidden (live hidden reads are 0), a background
    refresh keeps it, and the close-path render restores the position."""
    data = _run_scroll("ordinary")
    before = data["before"]
    opened = data["open"]
    refresh = data["refresh"]
    close = data["close"]
    # the reader's position took effect before the preview opened
    assert before["scrollTop"] == 600, before
    # preview open: tree hidden, live read is 0 — the snapshot must hold 600
    assert opened["display"] == "none", opened
    assert opened["scrollTop"] == 0, opened
    assert opened["snapshot"] == 600, opened
    # background refresh while hidden: still hidden, snapshot intact
    assert refresh["display"] == "none", refresh
    assert refresh["snapshot"] == 600, refresh
    # after close: tree visible again at the reader's position; snapshot consumed
    assert close["display"] == "", close
    assert close["scrollTop"] == 600, close
    assert close["snapshot"] is None, close


def test_file_to_file_switch_keeps_tree_scroll_snapshot():
    """Switching from one preview to another happens with the tree already
    hidden; the second openFile() must not overwrite the snapshot with a
    hidden (zero) read, or closing would still reset the tree."""
    data = _run_scroll("switch")
    extra = data["extra"]
    close = data["close"]
    assert extra["snapshot"] == 600, extra
    assert close["display"] == "", close
    assert close["scrollTop"] == 600, close
    assert close["snapshot"] is None, close


def test_file_to_file_switch_keeps_the_scroll_scope_too():
    """Greptile P1: a second preview opened while the tree is ALREADY hidden must keep
    the offset *and* its browse identity. Clearing the scope there made the next
    renderFileTree() see a mismatch and drop the offset, so closing the second preview
    returned a long tree to the top instead of the position captured before the FIRST
    preview opened."""
    data = _run_scroll("switch")
    extra = data["extra"]
    close = data["close"]
    assert extra["snapshot"] == 600, extra
    # the scope survived the second openFile() — a file-to-file switch happens on the
    # same browse surface, so its identity must not be cleared
    assert extra["scope"] is not None, (
        f"the file-to-file switch dropped the browse scope, so the next render would "
        f"treat the surviving offset as a mismatch and reset a long tree to the top "
        f"(Greptile P1): {extra}"
    )
    assert extra["scope"].get("dir") == ".", extra
    # …and the reader's position still survives the close
    assert close["scrollTop"] == 600, close
    assert close["snapshot"] is None, close


def test_empty_refresh_after_preview_close_reconciles_without_stale_snapshot():
    """A refresh that empties the directory while the preview is open must
    still show the placeholder after close and must not strand the snapshot."""
    data = _run_scroll("empty")
    close = data["close"]
    assert close["emptyDisplay"] == "flex", close
    assert close["snapshot"] is None, close


def test_panel_close_reopen_preserves_tree_scroll():
    """Closing the whole panel over a preview and reopening it in browse mode
    must reveal the tree at the reader's position (mobile drawer + desktop
    collapsed-panel path)."""
    data = _run_scroll("panelclose")
    close = data["close"]
    reopened = data["postReopen"]
    assert close["panelMode"] == "closed", close
    assert close["scrollTop"] == 600, close
    assert reopened["panelMode"] == "browse", reopened
    assert reopened["display"] == "", reopened
    assert reopened["scrollTop"] == 600, reopened


# ── Browser-level lifecycle proof (real Chromium, isolated test server) ──────
#
# The Node harnesses above emulate the hidden-container contract; these tests
# hold the real browser to it: a long tree keeps its scroll position across
# open → background refresh → close, and across whole-panel close → reopen,
# on desktop and on the mobile drawer layout.

from tests._pytest_port import BASE  # noqa: E402  (needs conftest env published)

_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _require_playwright():
    pw = pytest.importorskip("playwright.sync_api")
    return pw


def _open_browser_page(browser, width, height):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.add_init_script("localStorage.setItem('hermes-webui-workspace-panel','open')")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof S !== 'undefined' && S._bootReady === true", timeout=15000
    )
    page.wait_for_function("() => typeof renderFileTree === 'function'", timeout=15000)
    return context, page


_DRIVE_LIFECYCLE_JS = r"""
async (panelCloseFlow) => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const ft = document.getElementById('fileTree');
  S.session = {session_id: 'browser-6709', workspace: '/tmp/6709-browser-ws'};
  S.currentDir = '.';
  openWorkspacePanel('browse');
  S.entries = Array.from({length: 120}, (_, i) => {
    const name = 'file-' + String(i).padStart(3, '0') + '.txt';
    return {name: name, path: name, type: 'file', mtime_ns: 1000 + i};
  });
  window.api = async () => ({content: '6709 browser harness content'});
  renderFileTree();
  const scrollable = ft.scrollHeight > ft.clientHeight;
  ft.scrollTop = 600;
  const before = {took: ft.scrollTop, scrollable: scrollable, display: ft.style.display};
  await openFile('file-060.txt');
  const open = {display: ft.style.display, read: ft.scrollTop};
  // background refresh while the preview is open (loadDir -> renderFileTree)
  renderFileTree();
  const refresh = {display: ft.style.display};
  let close;
  let reopened = null;
  if (panelCloseFlow) {
    // panel was opened from 'closed' as 'preview'; default close closes it
    closeWorkspacePanel();
    ensureWorkspacePreviewVisible();
    clearPreview();
    close = {display: ft.style.display, scrollTop: ft.scrollTop, mode: _workspacePanelMode};
    openWorkspacePanel('browse');
    reopened = {display: ft.style.display, scrollTop: ft.scrollTop, mode: _workspacePanelMode};
  } else {
    clearPreview({keepPanelOpen: true});
    close = {display: ft.style.display, scrollTop: ft.scrollTop, mode: _workspacePanelMode};
  }
  return {before: before, open: open, refresh: refresh, close: close, reopened: reopened};
}
"""


@pytest.mark.parametrize(
    "width,height,label", [(1280, 800, "desktop"), (480, 800, "mobile")]
)
def test_browser_preview_close_preserves_tree_scroll(width, height, label):
    pw = _require_playwright()
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            context, page = _open_browser_page(browser, width, height)
            try:
                data = page.evaluate(_DRIVE_LIFECYCLE_JS, False)
            finally:
                context.close()
        finally:
            browser.close()
    before = data["before"]
    opened = data["open"]
    refresh = data["refresh"]
    close = data["close"]
    assert before["scrollable"], f"[{label}] tree not scrollable: {before}"
    assert before["took"] == 600, f"[{label}] scroll set-up failed: {before}"
    assert opened["display"] == "none", f"[{label}] preview did not hide the tree: {opened}"
    assert refresh["display"] == "none", f"[{label}] refresh re-revealed the tree: {refresh}"
    assert close["display"] == "", f"[{label}] tree not revealed after close: {close}"
    assert close["scrollTop"] == 600, f"[{label}] scroll position lost on close: {close}"


@pytest.mark.parametrize(
    "width,height,label", [(1280, 800, "desktop"), (480, 800, "mobile")]
)
def test_browser_panel_close_reopen_preserves_tree_scroll(width, height, label):
    pw = _require_playwright()
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            context, page = _open_browser_page(browser, width, height)
            try:
                data = page.evaluate(_DRIVE_LIFECYCLE_JS, True)
            finally:
                context.close()
        finally:
            browser.close()
    close = data["close"]
    reopened = data["reopened"]
    assert close["mode"] == "closed", f"[{label}] panel did not close: {close}"
    assert close["scrollTop"] == 600, f"[{label}] scroll lost on panel close: {close}"
    assert reopened["mode"] == "browse", f"[{label}] panel did not reopen: {reopened}"
    assert reopened["display"] == "", f"[{label}] tree hidden after reopen: {reopened}"
    assert reopened["scrollTop"] == 600, f"[{label}] scroll lost on reopen: {reopened}"


# ── Source-shape lock ────────────────────────────────────────────────────────


def test_clear_preview_defers_to_render_file_tree():
    """clearPreview() must call renderFileTree() after clearing the preview
    path (not just toggle fileTree display), so the renderer owns the
    tree-vs-empty-state contract."""
    src = _read("static/boot.js")
    block_start = src.find("function clearPreview(opts={}){")
    block_end = src.find("$('btnClearPreview').onclick", block_start)
    assert block_start >= 0 and block_end > block_start
    block = src[block_start:block_end]
    assert "renderFileTree" in block, (
        "clearPreview must call renderFileTree() to reconcile browse-state "
        "visibility after the preview closes (empty-directory edge case)"
    )
    # The old display-only restore must be gone — it left the empty-state
    # placeholder hidden after closing a preview over an emptied directory.
    assert "ft.style.display" not in block, (
        "clearPreview must not restore display directly; renderFileTree() "
        "owns the tree/empty-state contract"
    )


# ── Greptile round 5 (P1): close paths that bypass clearPreview() ────────────
#
# The settings "workspace panel open" toggle (panels.js) and the mobile
# outside-tap drawer close call closeWorkspacePanel() WITHOUT clearPreview().
# `_previewCurrentPath` therefore stays set, and renderFileTree() keeps BOTH
# #fileTree and #wsEmptyState hidden while a preview path is set (`previewOpen`).
# Reopening through openWorkspacePanel('browse') then leaves the panel in browse
# mode with neither browse surface visible — a blank Files panel.
#
# Invariant under test: a panel that is open must show something — and if it
# resolves to browse mode, one of the browse surfaces (tree or empty-state)
# must be visible.

def _extract_boot_function(name: str) -> str:
    from tests.js_source_extract import extract_function

    return extract_function(_read("static/boot.js"), name)


_PANEL_MODE_SHIM = r"""
var document={documentElement:{dataset:{}}};
var localStorage={setItem:function(){},getItem:function(){return null;}};
function _workspacePanelEls(){
  const layout={classList:{toggle(){},add(){},remove(){}}};
  const panel={classList:{toggle(){},add(){},remove(){},contains(){return false;}}};
  const btn={classList:{toggle(){}},setAttribute(){},set disabled(v){},get disabled(){return false;}};
  return {layout:layout,panel:panel,toggleBtn:btn,edgeToggleBtn:btn,collapseBtn:btn};
}
function _isCompactWorkspaceViewport(){ return false; }
function _uiText(k,d){ return d||k; }
function _setButtonTooltip(){}
"""

_PANEL_MODE_DRIVE = r"""
const previewArea = $id('previewArea');
const previewClasses = new Set();
previewArea.classList = {
  add(c){ previewClasses.add(c); },
  remove(c){ previewClasses.delete(c); },
  contains(c){ return previewClasses.has(c); },
};
S.entries = __ENTRIES__;
S.session = {session_id:'s1', workspace:'/ws'};
S.currentDir = '.';
// open a preview exactly the way openFile() does
previewClasses.add('visible');
_previewCurrentPath = '/ws/file.txt';
_previewCurrentMode = 'code';
_workspacePanelMode = 'preview';
renderFileTree();
const during = {tree: store.fileTree.style.display, empty: store.wsEmptyState.style.display,
                preview: _hasWorkspacePreviewVisible(), mode: _workspacePanelMode};
// settings toggle / mobile outside-tap: close the panel WITHOUT clearPreview()
closeWorkspacePanel();
const afterClose = {tree: store.fileTree.style.display, empty: store.wsEmptyState.style.display,
                    preview: _hasWorkspacePreviewVisible(), mode: _workspacePanelMode};
// reopen the panel the way the settings toggle does
openWorkspacePanel('browse');
const afterReopen = {tree: store.fileTree.style.display, empty: store.wsEmptyState.style.display,
                     preview: _hasWorkspacePreviewVisible(), mode: _workspacePanelMode,
                     previewPath: _previewCurrentPath};
console.log('REOPEN ' + JSON.stringify({during, afterClose, afterReopen}));
"""


def _close_then_reopen_harness(entries_json: str) -> str:
    """Drive the REAL boot.js panel-mode functions through the settings/mobile
    close → reopen path, with a #previewArea whose `.visible` class behaves like
    the browser's."""
    pre = _NODE_PREAMBLE
    # The preamble's simplified panel stubs are replaced by the real bodies.
    for name in (
        "closeWorkspacePanel",
        "openWorkspacePanel",
        "syncWorkspacePanelUI",
        "_hasWorkspacePreviewVisible",
    ):
        pre = re.sub(r"^function %s\([^)]*\)\{[^\n]*\}\n" % name, "", pre, flags=re.M)
    fns = "\n".join(
        _extract_boot_function(n)
        for n in (
            "_setWorkspacePanelMode",
            "openWorkspacePanel",
            "closeWorkspacePanel",
            "_hasWorkspacePreviewVisible",
            "syncWorkspacePanelUI",
        )
    )
    drive = _PANEL_MODE_DRIVE.replace("__ENTRIES__", entries_json)
    return (
        pre
        + _PANEL_MODE_SHIM
        + "\n"
        + _extract_render_file_tree()
        + "\n"
        + _extract_clear_preview()
        + "\n"
        + fns
        + "\n"
        + drive
    )


def _assert_panel_not_blank(after: dict, label: str):
    assert after["mode"] != "closed", (label, after)
    # An open panel must show something.
    assert (
        after["tree"] != "none" or after["empty"] != "none" or after["preview"]
    ), (label, after)
    # A panel that resolved to browse mode must show a browse surface.
    if after["mode"] == "browse":
        assert after["tree"] == "" or after["empty"] == "flex", (label, after)


def test_settings_close_reopen_does_not_leave_blank_panel():
    """Non-empty directory: close via the settings/mobile path (no clearPreview),
    reopen in browse mode — the panel must not be blank."""
    js = _close_then_reopen_harness(
        '[{"name":"a.txt","type":"file","path":"/ws/a.txt"}]'
    )
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "REOPEN" in proc.stdout, proc.stdout
    data = json.loads(proc.stdout.split("REOPEN ", 1)[1].strip())
    # the preview really was open (guards against a harness that no-ops)
    assert data["during"]["tree"] == "none", data
    assert data["during"]["preview"] is True, data
    _assert_panel_not_blank(data["afterReopen"], "nonempty")


def test_settings_close_reopen_does_not_leave_blank_panel_empty_dir():
    """Empty-directory variant: the placeholder must not stay suppressed."""
    js = _close_then_reopen_harness("[]")
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "REOPEN" in proc.stdout, proc.stdout
    data = json.loads(proc.stdout.split("REOPEN ", 1)[1].strip())
    assert data["during"]["empty"] == "none", data
    _assert_panel_not_blank(data["afterReopen"], "empty")


# ── Dirty-preview lifecycle (gate round 6) ────────────────────────────────────
#
# The exact-head gate found that the round-5 fix (closeWorkspacePanel() calling
# clearPreview({keepPanelOpen:true})) introduced a data-loss regression: an
# ordinary presentation-only panel collapse — composer Files toggle, Settings
# workspace-panel toggle, mobile outside-tap drawer close — silently discarded an
# unsaved Edit draft. These tests drive the REAL panel-mode functions through
# each of those three entry points with a dirty preview and assert the draft
# survives, the reopen restores preview mode (not a blank browse pane), and only
# the explicit preview-close action clears the preview.

_DIRTY_PREVIEW_SHIM = r"""
var document={documentElement:{dataset:{}},querySelector:function(){return null;}};
var localStorage={setItem:function(){},getItem:function(){return null;}};
function _isCompactWorkspaceViewport(){ return globalThis.__compact===true; }
function _workspacePanelEls(){
  const layout={classList:{toggle(){},add(){},remove(){}}};
  const panel={classList:{toggle(){},add(){},remove(){},contains(){return false;}}};
  const btn={classList:{toggle(){}},setAttribute(){},set disabled(v){},get disabled(){return false;}};
  return {layout:layout,panel:panel,toggleBtn:btn,edgeToggleBtn:btn,collapseBtn:btn};
}
function _uiText(k,d){ return d||k; }
function _setButtonTooltip(){}
function renderBreadcrumb(){}
function _state(){
  const editArea = $id('previewEditArea');
  return {
    path: _previewCurrentPath,
    dirty: _previewDirty,
    mode: _workspacePanelMode,
    editorDisplay: editArea.style.display,
    draft: editArea.value,
    previewVisible: $id('previewArea').classList.contains('visible'),
    tree: store.fileTree.style.display,
  };
}
"""

# The three ordinary presentation-only collapse entry points the gate named.
_DIRTY_CLOSE_PATHS = {
    # Composer Files toggle: index.html onclick="toggleWorkspacePanel()".
    "composer": "toggleWorkspacePanel();",
    # Settings workspace-panel toggle: panels.js onchange → toggleWorkspacePanel(false).
    "settings": "if(_workspacePanelMode!=='closed') toggleWorkspacePanel(false);",
    # Mobile outside-tap drawer close on #mainChat.
    "mobile": "globalThis.__compact=true; closeMobileWorkspacePanelFromChat({target:{}});",
}

# The one path that IS supposed to tear the preview down.
_DIRTY_EXPLICIT_CLOSE = "handleWorkspaceClose();"


def _dirty_preview_harness(entries_json: str, close_js: str) -> str:
    """Drive the REAL boot.js panel-mode functions with a dirty, open edit

    draft and then run ``close_js`` (one of the collapse entry points)."""
    pre = _NODE_PREAMBLE
    for name in (
        "closeWorkspacePanel",
        "openWorkspacePanel",
        "syncWorkspacePanelUI",
        "_hasWorkspacePreviewVisible",
    ):
        pre = re.sub(r"^function %s\([^)]*\)\{[^\n]*\}\n" % name, "", pre, flags=re.M)
    fns = "\n".join(
        _extract_boot_function(n)
        for n in (
            "_setWorkspacePanelMode",
            "openWorkspacePanel",
            "closeWorkspacePanel",
            "_hasWorkspacePreviewVisible",
            "syncWorkspacePanelUI",
            "toggleWorkspacePanel",
            "closeMobileWorkspacePanelFromChat",
            "handleWorkspaceClose",
        )
    )
    drive = r"""
const previewArea = $id('previewArea');
const previewClasses = new Set(['visible']);
previewArea.classList = {
  add(c){ previewClasses.add(c); },
  remove(c){ previewClasses.delete(c); },
  contains(c){ return previewClasses.has(c); },
};
const editArea = $id('previewEditArea');
editArea.style.display = '';          // Edit mode is open
editArea.value = 'UNSAVED-DRAFT';
S.entries = __ENTRIES__;
S.session = {session_id:'s1', workspace:'/ws'};
S.currentDir = '.';
_previewCurrentPath = 'draft.txt';
_previewCurrentMode = 'code';
_previewDirty = true;
_workspacePanelMode = 'preview';
renderFileTree();
const before = _state();
__CLOSE__
const afterClose = _state();
// #6709 (gate certification): this comment used to claim "every reopen path funnels
// through here", which is FALSE — resize/reflow and session-load syncs reopen through
// syncWorkspacePanelState(). openWorkspacePanel() is the funnel only for the explicit
// toggles (composer, Settings, mobile); syncWorkspacePanelState() is covered by its own
// test below, which drives the real production entry.
openWorkspacePanel('browse');
const afterReopen = _state();
console.log('DIRTY ' + JSON.stringify({before, afterClose, afterReopen}));
""".replace("__ENTRIES__", entries_json).replace("__CLOSE__", close_js)
    return (
        pre
        + _DIRTY_PREVIEW_SHIM
        + "\n"
        + _extract_render_file_tree()
        + "\n"
        + _extract_clear_preview()
        + "\n"
        + fns
        + "\n"
        + drive
    )


def _run_dirty(close_js: str, entries_json: str = '[{"name":"other.txt","type":"file","path":"/ws/other.txt"}]') -> dict:
    js = _dirty_preview_harness(entries_json, close_js)
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    assert "DIRTY" in proc.stdout, proc.stdout
    return json.loads(proc.stdout.split("DIRTY ", 1)[1].strip())


@pytest.mark.parametrize("label", sorted(_DIRTY_CLOSE_PATHS))
def test_panel_collapse_preserves_unsaved_draft(label):
    """Composer / Settings / mobile collapse must not touch the draft.

    The gate reproduced this at head 811561ca: close ended at
    {path:'', dirty:false, confirmCalls:0} with the textarea bytes orphaned."""
    data = _run_dirty(_DIRTY_CLOSE_PATHS[label])
    # precondition: the draft really was live and the preview really was open
    assert data["before"]["path"] == "draft.txt", data
    assert data["before"]["dirty"] is True, data
    assert data["before"]["draft"] == "UNSAVED-DRAFT", data
    assert data["before"]["editorDisplay"] != "none", data
    assert data["before"]["previewVisible"] is True, data
    # the panel collapsed …
    assert data["afterClose"]["mode"] == "closed", (label, data)
    # … but the preview state survived the collapse untouched
    assert data["afterClose"]["path"] == "draft.txt", (label, data)
    assert data["afterClose"]["dirty"] is True, (label, data)
    assert data["afterClose"]["draft"] == "UNSAVED-DRAFT", (label, data)
    assert data["afterClose"]["editorDisplay"] != "none", (label, data)
    assert data["afterClose"]["previewVisible"] is True, (label, data)


@pytest.mark.parametrize("label", sorted(_DIRTY_CLOSE_PATHS))
def test_panel_reopen_restores_preview_not_blank_browse(label):
    """Reopen after a collapse must restore the retained preview, not expose a

    browse pane whose tree and empty-state are still suppressed by the preview
    path (the blank Files pane the round-5 teardown was trying to avoid)."""
    data = _run_dirty(_DIRTY_CLOSE_PATHS[label])
    after = data["afterReopen"]
    # normalised back to preview: the retained preview is what the user sees
    assert after["mode"] == "preview", (label, data)
    assert after["path"] == "draft.txt", (label, data)
    assert after["dirty"] is True, (label, data)
    assert after["draft"] == "UNSAVED-DRAFT", (label, data)
    assert after["previewVisible"] is True, (label, data)
    # and the pane is never a blank browse surface
    assert after["tree"] == "none", (label, data)


@pytest.mark.parametrize("label", sorted(_DIRTY_CLOSE_PATHS))
def test_collapse_then_explicit_close_still_clears_preview(label):
    """Only the explicit preview-close action tears the preview down — and it

    must still reconcile browse state so the tree is visible (round-4/round-5
    behaviour, which the gate said looks converged)."""
    js = _dirty_preview_harness(
        '[{"name":"other.txt","type":"file","path":"/ws/other.txt"}]',
        _DIRTY_CLOSE_PATHS[label] + "\n" + _DIRTY_EXPLICIT_CLOSE,
    )
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.split("DIRTY ", 1)[1].strip())
    after = data["afterClose"]
    assert after["path"] == "", (label, data)
    assert after["dirty"] is False, (label, data)
    assert after["previewVisible"] is False, (label, data)
    # browse surfaces reconciled: the tree is back (non-empty directory)
    assert after["tree"] == "", (label, data)


# ── Panel ownership across an ordinary collapse (#6709, gate round 7) ─────────
#
# Gate comment 5773362769 reproduced this at head 2000f7cc:
#   candidate: browse -> open file (browse) -> collapse (closed) -> reopen (preview) -> X (closed)
#   control:   browse -> open file (browse) -> collapse (closed) -> reopen (browse)  -> X (browse)
#
# A preview reached from a manually-opened Files tree is OWNED by `browse`. The
# unconditional `if(mode==='browse'&&_hasWorkspacePreviewVisible()) mode='preview'`
# turned every retained preview into `preview`, so the explicit X took
# clearPreview()'s `closePanelAfter` branch and tore down the whole drawer instead
# of revealing the still-open Files tree.
#
# These harnesses drive the REAL panel-mode functions and start from `closed`
# with no mode seeded in — that seeding is precisely what masked the regression
# in the older fixtures, which asserted the desired sequence against a
# single-line `openWorkspacePanel` stub and so could never observe it.

_OWNERSHIP_SHIM = r"""
var document={documentElement:{dataset:{}}};
var localStorage={setItem:function(){},getItem:function(){return null;}};
function _workspacePanelEls(){
  const layout={classList:{toggle(){},add(){},remove(){}}};
  const panel={classList:{toggle(){},add(){},remove(){},contains(){return false;}}};
  const btn={classList:{toggle(){}},setAttribute(){},set disabled(v){},get disabled(){return false;}};
  return {layout:layout,panel:panel,toggleBtn:btn,edgeToggleBtn:btn,collapseBtn:btn};
}
function _isCompactWorkspaceViewport(){ return __COMPACT__; }
function _uiText(k,d){ return d||k; }
function _setButtonTooltip(){}
"""

# The harness preamble is shared with fixtures that stub the panel-mode functions,
# so these harnesses build the real ones instead.
_OWNERSHIP_PREAMBLE = r"""
const store = {};
const fileTreeBox = {
  id:'fileTree', style:{}, innerHTML:'', _scrollTop:0,
  get scrollTop(){ return this.style.display==='none' ? 0 : this._scrollTop; },
  set scrollTop(v){ if(this.style.display!=='none'){ this._scrollTop=Math.max(0,Number(v)||0); } },
  appendChild(){}, remove(){}, setAttribute(){}, getAttribute(){return null;},
  querySelector(){return null;},
  classList:{add(){},remove(){},toggle(){},contains(){return false;}},
};
store.fileTree = fileTreeBox;
function $id(id){
  if(id==='fileTree') return fileTreeBox;
  if(store[id]) return store[id];
  const el = {id, style:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}},
    innerHTML:'', textContent:'', scrollTop:0, appendChild(){}, remove(){},
    setAttribute(){}, getAttribute(){return null;}, querySelector(){return null;}};
  store[id]=el; return el;
}
const $ = $id;
const S = {session:{session_id:'s1', workspace:'/ws'}, entries:null, currentDir:'.', _dirCache:{}};
let _previewCurrentPath='', _previewCurrentMode='', _previewDirty=false;
let _workspacePanelMode='closed';
let _workspacePanelRetainedMode=null;
function t(k){ return k; }
function renderBreadcrumb(){}
function _syncWorkspaceBirthtimeSupportScope(){}
function _noteWorkspaceBirthtimeSupport(){}
function _saveExpandedDirs(){}
function _workspaceEntriesForRender(entries){ return Array.isArray(entries)?entries:[]; }
function _renderTreeItems(box, items){ box.innerHTML='items:'+items.length; }
"""

_OWNERSHIP_DRIVE = r"""
const pa = $id('previewArea');
const pset = new Set();
pa.classList = {add(c){pset.add(c);}, remove(c){pset.delete(c);}, contains(c){return pset.has(c);}};
S.entries = [{name:'a.txt', path:'a.txt', type:'file'},
             {name:'b.txt', path:'b.txt', type:'file'}];
S.session = {session_id:'s1', workspace:'/ws'};
S.currentDir = '.';

const results = [];
function snap(label){
  results.push({label,
    mode: _workspacePanelMode,
    retained: _workspacePanelRetainedMode,
    preview: _hasWorkspacePreviewVisible(),
    tree: store.fileTree.style.display || '(shown)',
    empty: $id('wsEmptyState').style.display || '(hidden)',
    // the blank panel the contract must never produce: neither surface visible
    blank: (store.fileTree.style.display === 'none' && !_hasWorkspacePreviewVisible()),
  });
}
function reset(){
  _workspacePanelMode='closed';
  _workspacePanelRetainedMode=null;
  _previewCurrentPath=''; _previewCurrentMode=''; _previewDirty=false;
  pset.clear();
  store.fileTree.style.display='';
  $id('wsEmptyState').style.display='none';
}
// exactly what openFile() does to these globals when a tree row is clicked:
// it shows the preview and hides the tree but does NOT touch the panel mode
function openFileLike(path){
  pa.classList.add('visible');
  store.fileTree.style.display = 'none';
  _previewCurrentPath = path;
  _previewCurrentMode = 'code';
}

function driveBrowse(){
  reset();
  openWorkspacePanel('browse');      snap('open');
  openFileLike('/ws/a.txt');         snap('open_file');
  closeWorkspacePanel();             snap('collapse');
  openWorkspacePanel('browse');      snap('reopen');
  handleWorkspaceClose();            snap('explicit_close');
}
function drivePreview(){
  reset();
  ensureWorkspacePreviewVisible();   snap('open');
  openFileLike('/ws/a.txt');         snap('open_file');
  closeWorkspacePanel();             snap('collapse');
  openWorkspacePanel('browse');      snap('reopen');
  handleWorkspaceClose();            snap('explicit_close');
}
function driveNoPreview(){
  reset();
  openWorkspacePanel('browse');      snap('open');
  closeWorkspacePanel();             snap('collapse');
  openWorkspacePanel('browse');      snap('reopen');
}
function driveToggle(){
  // the composer Files button: onclick="toggleWorkspacePanel()" on
  // #btnWorkspacePanelToggle, which passes `preview` whenever a preview is visible
  reset();
  toggleWorkspacePanel(true);        snap('open');
  openFileLike('/ws/a.txt');         snap('open_file');
  toggleWorkspacePanel(false);       snap('collapse');
  toggleWorkspacePanel(true);        snap('reopen');
  handleWorkspaceClose();            snap('explicit_close');
}
function driveEmptyDir(){
  reset();
  S.entries = [];
  openWorkspacePanel('browse');      snap('open');
  openFileLike('/ws/a.txt');         snap('open_file');
  closeWorkspacePanel();             snap('collapse');
  openWorkspacePanel('browse');      snap('reopen');
  handleWorkspaceClose();            snap('explicit_close');
}
({browse:driveBrowse, preview:drivePreview, toggle:driveToggle,
  no_preview:driveNoPreview, empty_dir:driveEmptyDir})[__SCENARIO__]();

console.log('OWNERSHIP ' + JSON.stringify(results));
"""


def _ownership_harness(scenario: str, compact: bool = False) -> str:
    """Drive the REAL panel-mode lifecycle from a closed panel (no mode seeded)."""
    # clearPreview(opts={}) has a default parameter, so it comes from the
    # paren-aware extractor: _extract_boot_function() delegates to
    # tests.js_source_extract.extract_function(), which brace-matches from the
    # FIRST `{` — the default-value brace — and returns the signature only, which
    # is a syntax error in the harness rather than a failing assertion.
    fns = "\n".join(
        [_extract_boot_function(n) for n in (
            "_hasWorkspacePreviewVisible",
            "_setWorkspacePanelMode",
            "syncWorkspacePanelUI",
            "openWorkspacePanel",
            "closeWorkspacePanel",
            "handleWorkspaceClose",
            "ensureWorkspacePreviewVisible",
            "toggleWorkspacePanel",
        )]
        + [_extract_clear_preview()]
    )
    drive = _OWNERSHIP_DRIVE.replace("__SCENARIO__", repr(scenario))
    return (
        _OWNERSHIP_PREAMBLE
        + _OWNERSHIP_SHIM.replace("__COMPACT__", "true" if compact else "false")
        + "\n"
        + _extract_render_file_tree()
        + "\n"
        + fns
        + "\n"
        + drive
    )


def _run_ownership(scenario: str, compact: bool = False) -> list:
    proc = _run_node(_ownership_harness(scenario, compact))
    assert proc.returncode == 0, proc.stderr
    assert "OWNERSHIP" in proc.stdout, proc.stdout
    return json.loads(proc.stdout.split("OWNERSHIP ", 1)[1].strip())


def _modes(steps: list) -> list:
    return [step["mode"] for step in steps]


def _boot_code_lines(name: str) -> str:
    """Extracted boot.js function with `//` comment lines stripped.

    The doc comments deliberately quote the patterns the code must NOT contain
    (the blanket normalization, the old clearPreview() teardown), so assertions
    that search the raw body would match their own explanation.
    """
    body = _extract_boot_function(name)
    return "\n".join(
        line for line in body.split("\n") if not line.strip().startswith("//")
    )


def test_browse_owned_preview_reopens_as_browse():
    """The gate's exact sequence: a preview opened from a manually-opened Files

    tree must come back as `browse`, not `preview` — otherwise the explicit X is
    a drawer-close instead of a return to the tree."""
    steps = _run_ownership("browse")
    assert _modes(steps) == ["browse", "browse", "closed", "browse", "browse"], steps
    # the retained owner is what makes the reopen correct
    assert steps[2]["retained"] == "browse", steps[2]
    assert steps[3]["retained"] == "browse", steps[3]


def test_browse_owned_explicit_close_reveals_the_files_tree():
    """The regression the gate reproduced: after collapse → reopen, the explicit

    preview X must leave the panel OPEN on the Files tree, not close the drawer."""
    steps = _run_ownership("browse")
    final = steps[-1]
    assert final["mode"] == "browse", final
    assert final["preview"] is False, final
    assert final["tree"] != "none", final
    assert final["blank"] is False, final


def test_preview_owned_drawer_reopens_as_preview_and_closes_on_explicit_close():
    """A preview that itself owns the drawer (artifact reveal with the panel

    closed) keeps its own contract: reopen as `preview`, explicit X closes."""
    steps = _run_ownership("preview")
    assert _modes(steps) == ["preview", "preview", "closed", "preview", "closed"], steps
    assert steps[2]["retained"] == "preview", steps[2]


def test_composer_files_toggle_preserves_browse_ownership():
    """Greptile P1 on the ownership fix (22 Sep): the composer Files toggle calls

    toggleWorkspacePanel(), which passes `preview` whenever a preview is visible
    (`_hasWorkspacePreviewVisible()?'preview':'browse'`). That bypassed the
    browse-only owner restoration, so this entry point put the panel back in
    `preview` and the explicit X closed the drawer. The recorded owner is now
    authoritative in openWorkspacePanel(), so every entry point agrees."""
    steps = _run_ownership("toggle")
    assert _modes(steps) == ["browse", "browse", "closed", "browse", "browse"], steps
    final = steps[-1]
    assert final["preview"] is False, final
    assert final["tree"] != "none", final
    assert final["blank"] is False, final


def test_collapse_without_preview_reopens_as_browse():
    """No retained preview means nothing to restore — an ordinary browse."""
    steps = _run_ownership("no_preview")
    assert _modes(steps) == ["browse", "closed", "browse"], steps
    assert steps[1]["retained"] is None, steps[1]
    assert steps[2]["tree"] != "none", steps[2]


def test_empty_directory_ownership_sequence_still_reconciles_the_empty_state():
    """Ownership must not regress the empty-state reconciliation the gate called

    converged: closing the preview must reveal the placeholder, not a blank pane."""
    steps = _run_ownership("empty_dir")
    assert _modes(steps) == ["browse", "browse", "closed", "browse", "browse"], steps
    final = steps[-1]
    assert final["empty"] == "flex", final
    assert final["blank"] is False, final


@pytest.mark.parametrize(
    "scenario", ["browse", "preview", "toggle", "no_preview", "empty_dir"]
)
def test_no_ownership_sequence_leaves_a_blank_panel(scenario):
    """No step of any ownership sequence may hide BOTH the tree and the preview."""
    steps = _run_ownership(scenario)
    blank = [s for s in steps if s["blank"]]
    assert not blank, (scenario, blank)


@pytest.mark.parametrize("compact", [False, True], ids=["desktop", "compact"])
def test_ownership_survives_every_viewport(compact):
    """The collapse path is shared by the composer toggle, the Settings toggle and

    the mobile outside-tap close, so ownership must be recordable and restorable
    at both viewport widths — not only the compact one."""
    steps = _run_ownership("browse", compact=compact)
    assert _modes(steps) == ["browse", "browse", "closed", "browse", "browse"], steps
    assert steps[-1]["tree"] != "none", steps[-1]


def test_ordinary_collapse_still_preserves_the_unsaved_draft():
    """Round-6 contract, re-asserted alongside ownership: an ordinary collapse is

    presentation-only, so the dirty flag, path and `.visible` all survive it."""
    js = _ownership_harness("browse")
    js = js.replace(
        "_previewDirty=false;", "_previewDirty=false;", 1
    )
    # mark the preview dirty before the collapse and report it after
    js = js.replace(
        "  openFileLike('/ws/a.txt');         snap('open_file');",
        "  openFileLike('/ws/a.txt'); _previewDirty=true; snap('open_file');",
        1,
    )
    js = js.replace(
        "    blank: (store.fileTree.style.display === 'none' && !_hasWorkspacePreviewVisible()),",
        "    blank: (store.fileTree.style.display === 'none' && !_hasWorkspacePreviewVisible()),\n"
        "    dirty: _previewDirty,\n    path: _previewCurrentPath,",
    )
    proc = _run_node(js)
    assert proc.returncode == 0, proc.stderr
    steps = json.loads(proc.stdout.split("OWNERSHIP ", 1)[1].strip())
    collapsed = steps[2]
    assert collapsed["mode"] == "closed", collapsed
    assert collapsed["dirty"] is True, collapsed
    assert collapsed["path"] == "/ws/a.txt", collapsed
    assert collapsed["preview"] is True, collapsed


def test_open_workspace_panel_no_longer_normalizes_every_retained_preview():
    """Code contract: the unconditional browse→preview conversion must be gone, and

    the decision must key off the recorded owner instead."""
    body = _boot_code_lines("openWorkspacePanel")
    assert (
        "if(mode==='browse'&&_hasWorkspacePreviewVisible()) mode='preview';" not in body
    ), "the blanket browse→preview normalization is back"
    # the owner must be authoritative regardless of the requested mode, not only
    # when the caller asked for `browse` — that browse-only gate is what let the
    # composer Files toggle through
    assert (
        "if(mode==='browse'&&_hasWorkspacePreviewVisible()&&_workspacePanelRetainedMode"
        not in body.replace(" ", "")
    ), "the owner restoration must not be gated on the requested mode"
    assert "mode=_workspacePanelRetainedMode;" in body.replace(" ", ""), (
        "openWorkspacePanel() must make the recorded owner authoritative"
    )


def test_close_workspace_panel_records_the_retained_owner():
    """Code contract: the ordinary collapse must record WHO owned the retained

    preview, and stay presentation-only while doing it."""
    body = _boot_code_lines("closeWorkspacePanel")
    assert (
        "_workspacePanelRetainedMode=_hasWorkspacePreviewVisible()?_workspacePanelMode:null;"
        in body.replace(" ", "")
    ), "closeWorkspacePanel() must record the retained owner"
    assert "clearPreview(" not in body, (
        "the ordinary collapse must stay presentation-only"
    )


# ── Real-browser ownership drive (#6709 gate round 7) ────────────────────────
# The gate reproduced its finding "on a real 129-file workspace against candidate
# and clean current master", so the ownership contract is asserted here against
# the real booted app too — real openFile(), real panel-mode functions, and a
# real click on the #btnClearPreview X — not only the Node harness.

_DRIVE_OWNERSHIP_JS = r"""
async (previewOwned) => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const ft = document.getElementById('fileTree');
  S.session = {session_id: 'browser-6709-ownership', workspace: '/tmp/6709-browser-ws'};
  S.currentDir = '.';
  S.entries = Array.from({length: 12}, (_, i) => {
    const name = 'file-' + String(i).padStart(3, '0') + '.txt';
    return {name: name, path: name, type: 'file', mtime_ns: 1000 + i};
  });
  window.api = async () => ({content: '6709 ownership harness content'});
  const snap = (label) => ({
    label: label,
    mode: _workspacePanelMode,
    preview: _hasWorkspacePreviewVisible(),
    tree: ft.style.display === 'none' ? 'none' : '(shown)',
    blank: ft.style.display === 'none' && !_hasWorkspacePreviewVisible(),
  });
  const steps = [];
  _setWorkspacePanelMode('closed');
  if (previewOwned) {
    ensureWorkspacePreviewVisible();          steps.push(snap('open'));
  } else {
    openWorkspacePanel('browse');             steps.push(snap('open'));
  }
  await openFile('file-000.txt');             steps.push(snap('open_file'));
  closeWorkspacePanel();                      steps.push(snap('collapse'));
  openWorkspacePanel('browse');               steps.push(snap('reopen'));
  document.getElementById('btnClearPreview').click();   // the real explicit X
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  steps.push(snap('explicit_close'));
  return steps;
}
"""


@pytest.mark.parametrize(
    "width,height,label", [(1280, 800, "desktop"), (480, 800, "mobile")]
)
def test_browser_browse_owned_preview_returns_to_the_files_tree(width, height, label):
    """Gate round 7, real browser: a file opened from a manually-opened Files tree

    must survive collapse → reopen as `browse`, and the real #btnClearPreview X must
    reveal the still-open tree instead of closing the drawer."""
    pw = _require_playwright()
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context, page = _open_browser_page(browser, width, height)
            try:
                steps = page.evaluate(_DRIVE_OWNERSHIP_JS, False)
            finally:
                context.close()
        finally:
            browser.close()
    modes = [s["mode"] for s in steps]
    assert modes == ["browse", "browse", "closed", "browse", "browse"], (label, steps)
    final = steps[-1]
    assert final["preview"] is False, (label, steps)
    assert final["tree"] != "none", (label, steps)
    assert not any(s["blank"] for s in steps), (label, steps)


@pytest.mark.parametrize(
    "width,height,label", [(1280, 800, "desktop"), (480, 800, "mobile")]
)
def test_browser_preview_owned_drawer_still_closes_on_the_x(width, height, label):
    """The other side of the contract, real browser: a drawer that a preview owns

    must still reopen as `preview` and be closed by the real X."""
    pw = _require_playwright()
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context, page = _open_browser_page(browser, width, height)
            try:
                steps = page.evaluate(_DRIVE_OWNERSHIP_JS, True)
            finally:
                context.close()
        finally:
            browser.close()
    modes = [s["mode"] for s in steps]
    assert modes == ["preview", "preview", "closed", "preview", "closed"], (label, steps)
    assert not any(s["blank"] for s in steps), (label, steps)


_DRIVE_TOGGLE_JS = r"""
async () => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  const ft = document.getElementById('fileTree');
  S.session = {session_id: 'browser-6709-toggle', workspace: '/tmp/6709-browser-ws'};
  S.currentDir = '.';
  S.entries = Array.from({length: 12}, (_, i) => {
    const name = 'file-' + String(i).padStart(3, '0') + '.txt';
    return {name: name, path: name, type: 'file', mtime_ns: 1000 + i};
  });
  window.api = async () => ({content: '6709 toggle harness content'});
  const snap = (label) => ({
    label: label,
    mode: _workspacePanelMode,
    preview: _hasWorkspacePreviewVisible(),
    tree: ft.style.display === 'none' ? 'none' : '(shown)',
    blank: ft.style.display === 'none' && !_hasWorkspacePreviewVisible(),
  });
  // the real composer Files button: onclick="toggleWorkspacePanel()"
  const toggle = document.getElementById('btnWorkspacePanelToggle');
  const steps = [];
  _setWorkspacePanelMode('closed');
  toggle.click();                           steps.push(snap('toggle_open'));
  await openFile('file-000.txt');           steps.push(snap('open_file'));
  toggle.click();                           steps.push(snap('toggle_close'));
  toggle.click();                           steps.push(snap('toggle_reopen'));
  document.getElementById('btnClearPreview').click();   // the real explicit X
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  steps.push(snap('explicit_close'));
  return steps;
}
"""


@pytest.mark.parametrize(
    "width,height,label", [(1280, 800, "desktop"), (480, 800, "mobile")]
)
def test_browser_composer_files_toggle_keeps_browse_ownership(width, height, label):
    """Greptile P1 on the ownership fix, real browser: clicking the actual

    #btnWorkspacePanelToggle (onclick="toggleWorkspacePanel()") must not lose the
    browse ownership, and the real X must leave the Files tree open."""
    pw = _require_playwright()
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context, page = _open_browser_page(browser, width, height)
            try:
                steps = page.evaluate(_DRIVE_TOGGLE_JS)
            finally:
                context.close()
        finally:
            browser.close()
    modes = [s["mode"] for s in steps]
    assert modes == ["browse", "browse", "closed", "browse", "browse"], (label, steps)
    final = steps[-1]
    assert final["preview"] is False, (label, steps)
    assert final["tree"] != "none", (label, steps)
    assert not any(s["blank"] for s in steps), (label, steps)

# ── Gate certification (23 Sep): the three findings at head 9bddce71 ─────────
#
# B1 — a stale `_workspacePanelRetainedMode` leaked into a later preview: the value is
# written only by closeWorkspacePanel() and read only by openWorkspacePanel(), so a
# preview torn down by ANOTHER path (clearPreview() during a directory refresh or
# session load, or a new preview starting while the panel is closed) left `browse`
# behind, and the next X revealed the tree instead of closing the drawer.
# B2/B3 — a bare scroll offset was restored onto a different directory/session.
# Test-honesty — the preview-owned cases ran through ensureWorkspacePreviewVisible(),
# which has no production callers, and the reopen comment claimed a nonexistent funnel.

_GATE_PRELUDE = r"""
const store = {};
const fileTreeBox = {
  id:'fileTree', style:{}, _html:'', _scrollTop:0,
  // #5657, verified against Chromium: wiping innerHTML detaches every row, collapsing
  // scrollHeight so the browser CLAMPS scrollTop to 0. Modelling that here is what makes
  // the scroll assertions below meaningful — without it a hidden render would leave a
  // stale offset installed and every reader would look correct.
  get innerHTML(){ return this._html; },
  set innerHTML(v){ this._html=v; this._scrollTop=0; },
  get scrollTop(){ return this.style.display==='none' ? 0 : this._scrollTop; },
  set scrollTop(v){ if(this.style.display!=='none'){ this._scrollTop=Math.max(0,Number(v)||0); } },
  appendChild(){}, remove(){}, setAttribute(){}, getAttribute(){return null;}, querySelector(){return null;},
  classList:{add(){}, remove(){}, toggle(){}, contains(){return false;}},
};
store.fileTree = fileTreeBox;
function $id(id){
  if(id==='fileTree') return fileTreeBox;
  if(store[id]) return store[id];
  const el = { id, style:{}, classList:{add(){}, remove(){}, toggle(){}, contains(){return false;}},
    innerHTML:'', textContent:'', scrollTop:0, appendChild(){}, remove(){},
    setAttribute(){}, getAttribute(){return null;}, querySelector(){return null;} };
  store[id]=el; return el;
}
const $ = $id;
const S = {session:{session_id:'s1', workspace:'/ws'}, entries:null, currentDir:'.', _dirCache:{}};
let _previewCurrentPath='', _previewCurrentMode='', _previewDirty=false;
let _workspacePanelMode='closed';
let _workspacePanelRetainedMode=null;
function t(k){ return k; }
function _workspaceEntriesForRender(entries){ return Array.isArray(entries)?entries:[]; }
function _noteWorkspaceBirthtimeSupport(){}
function _syncWorkspaceBirthtimeSupportScope(){}
function _renderTreeItems(box, items){ box.innerHTML='items:'+items.length; }
function syncWorkspacePanelUI(){}
function _hasWorkspacePreviewVisible(){ return !!_previewCurrentPath; }
// Real `_setWorkspacePanelMode()` reads the DOM through this seam; a null layout makes
// it return early, so the harness supplies minimal elements to let the real body run.
function _workspacePanelEls(){ return {layout: $id('wsLayout'), panel: $id('wsPanel')}; }
function _isCompactWorkspaceViewport(){ return false; }
const localStorage = {setItem(){}, getItem(){ return null; }};
const document = { documentElement: { dataset: {} } };
"""


def _gate_harness(driver: str) -> str:
    ui = _read("static/ui.js")
    boot = _read("static/boot.js")
    parts = [
        _GATE_PRELUDE,
        _extract_render_file_tree(),
        # real closePreview / openWorkspacePanel / closeWorkspacePanel / syncWorkspacePanelState
        _extract_fn(boot, "function clearPreview(opts={}){"),
        _extract_fn(boot, "function openWorkspacePanel(mode='browse'){"),
        _extract_fn(boot, "function closeWorkspacePanel(){"),
        _extract_fn(boot, "function _setWorkspacePanelMode(mode){"),
        _extract_fn(boot, "function syncWorkspacePanelState(){"),
        _extract_fn(boot, "function toggleWorkspacePanel(force){"),
        # the reader helper lives in ui.js (openFile writes the scope inline) + openFile
        _extract_fn(ui, "function _wsBrowseScrollScopeMatchesLiveModel(){"),
        _OPEN_FILE_STUBS,
        _extract_open_file(),
        driver,
    ]
    return "\n".join(parts)


def _extract_fn(src: str, marker: str) -> str:
    start = src.find(marker)
    assert start >= 0, f"not found: {marker}"
    # The body brace is the FIRST `{` after the parameter list CLOSES — not the first
    # one after the marker, because a default param like `opts={}` (see clearPreview)
    # carries a brace pair of its own and would truncate the body to `opts={}`.
    params_close = src.find(")", start)
    assert params_close > start, f"no parameter list for {marker}"
    depth = 0
    i = src.find("{", params_close)
    assert i > start, f"no body brace for {marker}"
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"no closing brace for {marker}")


_B1_DRIVER = r"""
(async () => {
  // (a) browse-owned collapse, then a plain reopen through the REAL composer toggle:
  // the recorded owner must still be honoured (the round-7 contract).
  _previewCurrentPath='A.txt'; _workspacePanelMode='browse';
  closeWorkspacePanel();
  const recorded = _workspacePanelRetainedMode;
  toggleWorkspacePanel(true);
  const modeOnPlainReopen = _workspacePanelMode;

  // (b) browse-owned collapse, then a directory refresh tears the preview down while
  // the panel is closed (loadDir('.')), then a DIFFERENT preview starts (chat
  // `#workspace=` link → openArtifactPath → openFile) and the user reopens.
  _workspacePanelMode='browse'; _previewCurrentPath='A.txt';
  closeWorkspacePanel();
  clearPreview({keepPanelOpen:true});
  const retainedAfterTeardown = _workspacePanelRetainedMode;
  await openFile('B.txt');
  toggleWorkspacePanel(true);
  const modeOnReopen = _workspacePanelMode;
  console.log(JSON.stringify({ recorded, modeOnPlainReopen, retainedAfterTeardown, modeOnReopen }));
})();
"""


def test_a_stale_retained_owner_cannot_leak_into_a_later_preview():
    """B1: after a preview is torn down outside closeWorkspacePanel(), the next reopen
    must be an ordinary browse — not the stale `browse` owner of a dead preview."""
    proc = _run_node(_gate_harness(_B1_DRIVER))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["recorded"] == "browse", f"precondition: a collapse with a preview retains its owner: {out}"
    assert out["modeOnPlainReopen"] == "browse", (
        f"precondition: the round-7 contract must still hold — a plain reopen honours the "
        f"browse owner: {out}"
    )
    assert out["retainedAfterTeardown"] is None, (
        f"a teardown outside closeWorkspacePanel() left the owner installed, so the next "
        f"preview would reopen as `browse` and its X would reveal the tree instead of "
        f"closing the drawer (gate B1): {out}"
    )
    assert out["modeOnReopen"] == "preview", (
        f"the reopen must restore the NEW preview's own ownership (`preview`), whose X "
        f"closes the drawer (gate B1): {out}"
    )


_B23_DRIVER = r"""
(async () => {
  // scroll a 120-entry tree to 600, then open a file (snapshot taken while visible)
  S.entries = Array.from({length:120}, (_,i)=>({name:'f'+i+'.txt', type:'file', path:'/ws/f'+i+'.txt'}));
  S.currentDir='.';
  renderFileTree();
  fileTreeBox._scrollTop = 600;
  await openFile('f1.txt');
  const snapped = { top:S._wsBrowseScrollTop, scope:S._wsBrowseScrollScope };
  // B2: the tree is now a DIFFERENT directory. Render while still hidden (what a
  // directory refresh does), then reveal — the position the reader sees is the live one.
  S.currentDir='sub';
  renderFileTree();
  const snapClearedOnDirChange = S._wsBrowseScrollTop==null;
  _previewCurrentPath='';            // preview closed → tree becomes visible again
  fileTreeBox.style.display='';
  renderFileTree();
  const afterDir = { live:fileTreeBox.scrollTop, snapCleared:snapClearedOnDirChange };
  // B3: same for a different session/workspace
  S.currentDir='.';
  S._wsBrowseScrollTop = snapped.top;
  S._wsBrowseScrollScope = snapped.scope;
  fileTreeBox._scrollTop = 600;
  S.session = {session_id:'s2', workspace:'/other'};
  renderFileTree();
  fileTreeBox.style.display='';
  renderFileTree();
  const afterSession = { live:fileTreeBox.scrollTop, snapCleared:S._wsBrowseScrollTop==null };
  console.log(JSON.stringify({ snapped, afterDir, afterSession }));
})();
"""


def test_a_scroll_snapshot_is_never_restored_onto_another_directory_or_session():
    """B2/B3: the snapshot is scoped by session + workspace + directory."""
    proc = _run_node(_gate_harness(_B23_DRIVER))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    snap = out["snapped"]
    assert snap["top"] == 600, f"precondition: a visible tree lends its offset: {out}"
    assert snap["scope"] == {"sessionId": "s1", "workspace": "/ws", "dir": "."}, (
        f"precondition: the snapshot must carry the browse identity: {out}"
    )
    assert out["afterDir"]["snapCleared"] is True, (
        f"a snapshot from another directory survived the directory change (gate B2): {out}"
    )
    # B2: a different directory must not inherit the offset
    assert out["afterDir"]["live"] == 0, (
        f"the new directory's tree was revealed at the old directory's scroll position, "
        f"so its top entries start out of view (gate B2): {out}"
    )
    # B3: a different session must not inherit it either
    assert out["afterSession"]["snapCleared"] is True, (
        f"a snapshot from another session/workspace survived into this render (gate "
        f"B3): {out}"
    )


_SYNC_DRIVER = r"""
(() => {
  // A DELIBERATE collapse (browse-owned), then an ordinary resize / reflow / session-load
  // sync. Reopening is an explicit user action, so the sync must leave the panel closed
  // and merely re-sync the chrome — otherwise dragging the window (or, on a phone, the
  // keyboard/URL-bar reflow) brings back a panel the user just dismissed.
  _previewCurrentPath='A.txt'; _workspacePanelMode='browse';
  closeWorkspacePanel();
  const retained = _workspacePanelRetainedMode;
  syncWorkspacePanelState();
  const afterBrowseCollapse = _workspacePanelMode;
  // ...and the explicit reopen still restores the recorded owner
  openWorkspacePanel('preview');
  const afterExplicitReopen = _workspacePanelMode;

  // A preview-owned deliberate collapse behaves the same way.
  _workspacePanelMode='closed'; _workspacePanelRetainedMode='preview';
  syncWorkspacePanelState();
  const afterPreviewCollapse = _workspacePanelMode;

  // With NO recorded owner there is no deliberate collapse to respect, so the
  // historical behaviour (reopen as preview) still applies.
  _workspacePanelMode='closed'; _workspacePanelRetainedMode=null;
  syncWorkspacePanelState();
  const afterNone = _workspacePanelMode;
  console.log(JSON.stringify({ retained, afterBrowseCollapse, afterExplicitReopen,
                               afterPreviewCollapse, afterNone }));
})();
"""


def test_a_sync_does_not_undo_a_deliberate_collapse():
    """Re-gate: a resize / reflow / session-load sync must not reopen a panel the user
    deliberately collapsed — reopening stays an explicit action. Reproduced on master
    too, but this PR makes "collapsed with a kept preview" a deliberate state."""
    proc = _run_node(_gate_harness(_SYNC_DRIVER))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["retained"] == "browse", (
        f"precondition: the collapse must record an owner, which is what marks it as "
        f"deliberate: {out}"
    )
    assert out["afterBrowseCollapse"] == "closed", (
        f"a resize/reflow sync reopened a deliberately collapsed panel; on a phone the "
        f"drawer would come back over the chat as soon as the keyboard appears "
        f"(re-gate): {out}"
    )
    # …while an explicit reopen still restores the recorded owner
    assert out["afterExplicitReopen"] == "browse", (
        f"the explicit reopen must still honour the recorded owner: {out}"
    )
    assert out["afterPreviewCollapse"] == "closed", (
        f"a preview-owned deliberate collapse must also survive a sync: {out}"
    )
    assert out["afterNone"] == "preview", (
        f"with no recorded owner the historical reopen behaviour must still apply: {out}"
    )

# ── Greptile P1: a background refresh of the SAME preview is not a new preview ──
#
# The turn-complete refresh (refreshOpenPreviewIfMutated) and the markdown re-render both
# call openFile(_previewCurrentPath, …) while the panel is collapsed. That path IS the
# preview the collapse retained, so retiring its ownership there left a later sync free to
# reopen the deliberately collapsed drawer as preview-owned — the X then closed it instead
# of returning to the tree.

_SAME_PATH_REFRESH_DRIVER = r"""
(async () => {
  // browse-owned preview, then a deliberate collapse (records the owner)
  _previewCurrentPath='A.txt'; _workspacePanelMode='browse';
  _workspacePanelRetainedMode=null;
  closeWorkspacePanel();
  const retainedAfterCollapse = _workspacePanelRetainedMode;

  // the turn-complete refresh re-opens the SAME path while the panel is still collapsed
  await openFile('A.txt', {bustCache:true});
  const retainedAfterSameRefresh = _workspacePanelRetainedMode;
  // …so a sync must still treat the collapse as deliberate and leave it closed
  syncWorkspacePanelState();
  const modeAfterSync = _workspacePanelMode;
  // …and the explicit reopen still restores the browse owner
  openWorkspacePanel('preview');
  const modeAfterExplicitReopen = _workspacePanelMode;

  // control: a DIFFERENT file while collapsed DOES supersede the retained owner
  _workspacePanelMode='browse'; _previewCurrentPath='A.txt';
  closeWorkspacePanel();
  const retainedBeforeOther = _workspacePanelRetainedMode;
  await openFile('B.txt');
  const retainedAfterOther = _workspacePanelRetainedMode;
  const modeAfterOtherSync = (syncWorkspacePanelState(), _workspacePanelMode);
  console.log(JSON.stringify({ retainedAfterCollapse, retainedAfterSameRefresh, modeAfterSync,
                               modeAfterExplicitReopen, retainedBeforeOther, retainedAfterOther,
                               modeAfterOtherSync }));
})();
"""


def test_a_background_refresh_of_the_same_preview_keeps_the_retained_owner():
    """Greptile P1: re-opening the retained path is not a new preview."""
    proc = _run_node(_gate_harness(_SAME_PATH_REFRESH_DRIVER))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["retainedAfterCollapse"] == "browse", (
        f"precondition: the collapse must record the browse owner: {out}"
    )
    assert out["retainedAfterSameRefresh"] == "browse", (
        f"a background refresh of the SAME preview discarded the recorded ownership, so a "
        f"later sync would reopen the deliberately collapsed drawer as preview-owned and "
        f"its X would close the drawer instead of returning to the tree (Greptile P1): {out}"
    )
    assert out["modeAfterSync"] == "closed", (
        f"the collapse must still count as deliberate after the refresh: {out}"
    )
    assert out["modeAfterExplicitReopen"] == "browse", (
        f"the explicit reopen must still restore the recorded owner: {out}"
    )
    # control: a different file while collapsed does supersede the owner
    assert out["retainedBeforeOther"] == "browse", out
    assert out["retainedAfterOther"] is None, (
        f"a DIFFERENT file must still retire the previous preview's ownership, otherwise "
        f"the next open would restore `browse` for a preview nobody reached from the "
        f"tree: {out}"
    )
    assert out["modeAfterOtherSync"] == "preview", (
        f"with no retained owner the historical reopen applies: {out}"
    )
