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
    """
    render_ft = _extract_render_file_tree()
    clear_pv = _extract_clear_preview()
    lifecycle = r"""
// ── Lifecycle: preview → close panel → reopen
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
    return _NODE_PREAMBLE + render_ft + "\n" + clear_pv + "\n" + lifecycle


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
            "const extra = {snapshot: S._wsBrowseScrollTop};"
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
openWorkspacePanel('browse');         // every reopen path funnels through here
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
