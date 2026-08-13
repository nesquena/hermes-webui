"""Regression tests for #6709 round 2 — closing a preview must reconcile
browse-state visibility (tree OR empty-state placeholder).

Round 1 fix (`f3ae94b6`) kept the hidden tree rebuilt while a preview is
open, but a maintainer review found an empty-directory edge case: when a
background refresh empties the current directory while a preview is open,
`renderFileTree()` correctly hides BOTH the tree and `#wsEmptyState` for the
preview state — yet `clearPreview()` only restored `#fileTree.style.display`
and never re-showed the empty-state placeholder. Closing the preview then
revealed a blank panel.

The fix defers to the renderer: `clearPreview()` calls `renderFileTree()`
after clearing `_previewCurrentPath`, so the renderer owns the tree-vs-empty
contract (placeholder hidden during preview, visible after close).

These tests drive the REAL extracted function bodies through a Node VM with a
minimal fake DOM, covering both the empty and non-empty refresh cases.
"""
import re
import shutil
import subprocess
from pathlib import Path

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


def _run_node(js: str) -> subprocess.CompletedProcess:
    assert NODE, "node is required"
    return subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )


def _lifecycle_harness(entries_json: str, expect_empty_after_close: bool) -> str:
    """Build a Node script that drives the REAL renderFileTree + clearPreview
    bodies through the preview-open → background-refresh → preview-close
    lifecycle with a minimal fake DOM."""
    render_ft = _extract_render_file_tree()
    clear_pv = _extract_clear_preview()
    preamble = r"""
const store = {};
function $id(id){
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
function _visibleWorkspaceEntries(entries){ return Array.isArray(entries)?entries:[]; }
function _renderTreeItems(box, items){ box.innerHTML='items:'+items.length; }
function closeWorkspacePanel(){ _workspacePanelMode='closed'; }
function openWorkspacePanel(mode){ _workspacePanelMode=mode; }
function syncWorkspacePanelUI(){}
function _hasWorkspacePreviewVisible(){ return !!_previewCurrentPath; }

"""
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
    return preamble + render_ft + "\n" + clear_pv + "\n" + lifecycle


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
    import json

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
    import json

    data = json.loads(payload)
    during = data["duringPreview"]
    after = data["afterClose"]
    assert during["treeDisplay"] == "none", during
    assert after["treeDisplay"] == "", after  # restored (renderer default)
    assert after["panelMode"] == "browse", after


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
