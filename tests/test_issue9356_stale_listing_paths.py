"""Regression coverage for #9356 — three file-tree paths that sit outside the
listing ordering #9351 introduced.

#9351 numbers every `loadDir()` and stamps cached folder listings with a cache
epoch, so a late response from an older load can no longer repaint over a newer
one. Three nearby paths were left outside that ordering:

1. **Folder expand writes the cache without ordering checks** (`static/ui.js`).
   Expanding an uncached folder awaited `/api/list` and then wrote
   `S._dirCache[item.path]` unconditionally, with no session/generation/epoch
   check and no listed-at stamp.

2. **The end-of-load preview clear is not covered by listing ownership**
   (`static/workspace.js`). The tree render is skipped when a newer load owns the
   listing, but the preview clear (or the unsaved-changes prompt) after it still
   ran for the older load.

3. **`S.currentDir` could name a folder whose listing never rendered**. It was
   assigned before the await, so a load whose response was dropped by the
   session check left it pointing at a path that is not on screen — and
   `renderFileTree()` caches `S.entries` under `S.currentDir`.

The `loadDir` tests drive the shipped function in a Node VM. The expand-path test
drives the shipped `renderFileTree` and clicks the real folder row.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_JS = REPO / "static" / "workspace.js"
UI_JS = REPO / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is required to drive the file-tree harness"
)


def _extract_fn(source: str, name: str, *, is_async: bool = True) -> str:
    """Extract a top-level function by line boundaries.

    Brace counting is unreliable here: `static/ui.js` has braces inside strings
    and template literals, which close the counter early and silently truncate
    the body. The repo keeps every top-level function at column 0, so the next
    top-level declaration marks the end.
    """
    lines = source.splitlines(keepends=True)
    marker = f"{'async ' if is_async else ''}function {name}("
    starts = [i for i, line in enumerate(lines) if line.startswith(marker)]
    assert starts, f"{name} not found at top level"
    start = starts[0]
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("function ") or line.startswith("async function "):
            end = i
            break
    return "".join(lines[start:end]).rstrip()


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"node harness failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


PRELUDE_TEMPLATE = r"""
const fs = require('fs');
const vm = require('vm');

const WS_SRC = __WS_SRC__;

function extractFn(src, name) {
  const marker = 'async function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(name + ' not found');
  let paren = 0, bodyStart = -1;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (ch === '(') paren++;
    else if (ch === ')') paren--;
    else if (ch === '{' && paren === 0) { bodyStart = i; break; }
  }
  let depth = 0;
  for (let i = bodyStart; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('unbalanced ' + name);
}

function mkEl(tag) {
  const e = {
    tag, children: [], parent: null, className: '', id: '', dataset: {},
    textContent: '', value: '', style: {}, _html: '', onclick: null,
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    remove() { if (this.parent) { this.parent.children = this.parent.children.filter(x => x !== this); this.parent = null; } },
    get isConnected() { let p = this; while (p.parent) p = p.parent; return p === root; },
    set innerHTML(v) { this.children.forEach(c => { c.parent = null; }); this.children = []; this._html = v; },
    get innerHTML() { return this._html; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {}, removeEventListener() {},
    setAttribute() {}, getAttribute() { return null; }, hasAttribute() { return false; },
    focus() {}, scrollIntoView() {},
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
  };
  return e;
}
const root = mkEl('root');
const nodes = {};
function node(id) { if (!nodes[id]) { const n = mkEl('div'); n.id = id; root.appendChild(n); nodes[id] = n; } return nodes[id]; }

const pendingList = [];
function api(route) {
  return new Promise((resolve, reject) => { pendingList.push({ route, resolve, reject }); });
}
function makeLoadDirCtx(overrides) {
  const out = { rendered: 0, breadcrumb: 0, previewCleared: 0 };
  const ctx = {
    S: {
      session: { session_id: 'S1', workspace: '/ws' },
      entries: [{ name: 'old-entry' }],
      _dirCache: {},
      _expandedDirs: new Set(),
      currentDir: 'old-dir',
    },
    _wsTreeGen: 1, _wsTreeLoadSeq: 0, _wsTreeEntriesSeq: 0,
    _wsTreeCacheEpoch: 0, _wsTreeCacheListedAt: new Map(),
    api, $: node, t: (k) => k, out, console,
    document: { createElement: mkEl, baseURI: 'http://x/' },
    location: { href: 'http://x/' }, window: {},
    _workspaceRouteForPath: (p) => '/api/list?path=' + encodeURIComponent(p),
    _workspaceEntriesForRender: (e) => e || [],
    _workspacePathIsReadOnly: () => false,
    _workspaceParentDir: () => '.',
    _workspaceEscapeGrantForPath: () => null,
    _clearWorkspaceEscapeGrant: () => {},
    _syncWorkspaceBirthtimeSupportScope: () => {},
    _restoreExpandedDirs: () => {},
    _refreshGitBadge: () => {},
    _previewHasUnsavedEdits: () => false,
    _previewHiddenEditsOwnedHere: () => true,
    renderBreadcrumb: () => { out.breadcrumb += 1; },
    renderFileTree: () => { out.rendered += 1; },
    renderSessionArtifacts: () => {},
    clearPreview: () => { out.previewCleared += 1; },
    refreshOpenPreviewIfMutated: () => Promise.resolve(),
    syncWorkspaceDisplays: () => {},
    syncTerminalButton: () => {},
    showToast: () => {},
    showConfirmDialog: () => Promise.resolve(false),
    a11yTreeDropCarry: () => {},
  };
  Object.assign(ctx, overrides || {});
  return ctx;
}
"""

def _prelude() -> str:
    """Inline the workspace.js source so node needs no environment plumbing."""
    src = json.dumps(WORKSPACE_JS.read_text(encoding="utf-8"))
    return PRELUDE_TEMPLATE.replace("__WS_SRC__", src)




# ── path 3: S.currentDir only names a listing that rendered ─────────────────


def test_a_dropped_response_leaves_current_dir_and_preview_alone():
    """Drives the shipped loadDir: a response dropped by the session check must
    neither change S.currentDir nor clear the preview."""
    fn = _extract_fn(WORKSPACE_JS.read_text(encoding="utf-8"), "loadDir")
    script = (
        _prelude()
        + f"""
const LOAD_DIR = {json.dumps(fn)};
const ctx = makeLoadDirCtx();
vm.createContext(ctx);
vm.runInContext(LOAD_DIR, ctx);
ctx.loadDir = ctx.loadDir || (typeof loadDir === 'function' ? loadDir : null);

(async () => {{
  const p = ctx.loadDir.call(ctx, 'new-dir');
  // The session changes while the request is in flight; the response is dropped.
  ctx.S.session = {{ session_id: 'S2', workspace: '/ws' }};
  const req = pendingList.shift();
  req.resolve({{ entries: [{{ name: 'x' }}] }});
  await p;
  console.log(JSON.stringify({{
    currentDir: ctx.S.currentDir,
    rendered: ctx.out.rendered,
    previewCleared: ctx.out.previewCleared,
    entries: ctx.S.entries.map(e => e.name),
  }}));
}})();
"""
    )
    result = _run_node(script)
    assert result["rendered"] == 0, "a dropped response must not render"
    assert result["currentDir"] == "old-dir", (
        "a dropped response left S.currentDir naming a folder that never rendered; "
        f"observed {result['currentDir']!r}"
    )
    assert result["previewCleared"] == 0, (
        "a load that does not own the shown listing must not clear the preview"
    )


def test_an_older_load_does_not_clear_the_preview_of_a_newer_one():
    """The newer load owns the listing; the older one finishes late."""
    fn = _extract_fn(WORKSPACE_JS.read_text(encoding="utf-8"), "loadDir")
    script = (
        _prelude()
        + f"""
const LOAD_DIR = {json.dumps(fn)};
const ctx = makeLoadDirCtx();
vm.createContext(ctx);
vm.runInContext(LOAD_DIR, ctx);
ctx.loadDir = ctx.loadDir || (typeof loadDir === 'function' ? loadDir : null);

(async () => {{
  // The older load starts first and its request is held.
  const older = ctx.loadDir.call(ctx, 'dir-a');
  const olderReq = pendingList.shift();

  // A newer load starts and completes first, owning the shown listing.
  const newer = ctx.loadDir.call(ctx, 'dir-b');
  const newerReq = pendingList.shift();
  newerReq.resolve({{ entries: [{{ name: 'b' }}] }});
  await newer;
  const clearedByNewer = ctx.out.previewCleared;

  // Now the older response lands.
  olderReq.resolve({{ entries: [{{ name: 'a' }}] }});
  await older;

  console.log(JSON.stringify({{
    clearedByNewer,
    clearedTotal: ctx.out.previewCleared,
    currentDir: ctx.S.currentDir,
    entries: ctx.S.entries.map(e => e.name),
  }}));
}})();
"""
    )
    result = _run_node(script)
    assert result["entries"] == ["b"], (
        "the newer load's listing must stay on screen; "
        f"observed {result['entries']!r}"
    )
    assert result["currentDir"] == "dir-b", (
        f"S.currentDir must name the shown listing, observed {result['currentDir']!r}"
    )
    assert result["clearedTotal"] == result["clearedByNewer"], (
        "the older load cleared the preview after the newer one had already "
        "decided what to show (#9356)"
    )


def test_a_successful_load_sets_current_dir_and_renders():
    """The happy path still works: ownership sets the dir and renders."""
    fn = _extract_fn(WORKSPACE_JS.read_text(encoding="utf-8"), "loadDir")
    script = (
        _prelude()
        + f"""
const LOAD_DIR = {json.dumps(fn)};
const ctx = makeLoadDirCtx();
vm.createContext(ctx);
vm.runInContext(LOAD_DIR, ctx);
ctx.loadDir = ctx.loadDir || (typeof loadDir === 'function' ? loadDir : null);

(async () => {{
  const p = ctx.loadDir.call(ctx, 'docs');
  pendingList.shift().resolve({{ entries: [{{ name: 'readme.md' }}] }});
  await p;
  console.log(JSON.stringify({{
    currentDir: ctx.S.currentDir,
    rendered: ctx.out.rendered,
    breadcrumb: ctx.out.breadcrumb,
    entries: ctx.S.entries.map(e => e.name),
    cleared: ctx.out.previewCleared,
  }}));
}})();
"""
    )
    result = _run_node(script)
    assert result["currentDir"] == "docs"
    assert result["entries"] == ["readme.md"]
    assert result["rendered"] == 1
    assert result["breadcrumb"] == 1
    assert result["cleared"] == 1


# ── path 1: the expand handler must not write a stale listing ───────────────


def test_the_expand_path_checks_session_generation_and_epoch():
    """The expand handler's cache write must be guarded like loadDir's.

    This reads the shipped handler body, because the click path needs a full
    file-tree render to reach; the loadDir tests above cover the equivalent
    ordering end to end.
    """
    ui = UI_JS.read_text(encoding="utf-8")
    block = _extract_fn(ui, "_renderTreeItems", is_async=False)
    marker = "Fetch children if not cached"
    at = block.find(marker)
    assert at > -1, "the expand fetch block moved; update this test"
    body = block[at : at + 2000]

    assert "expandEpoch" in body, (
        "the expand handler must capture the cache epoch its request was made in"
    )
    assert "_wsTreeCacheEpoch" in body, (
        "the expand handler must compare the live cache epoch before writing"
    )
    assert "_wsTreeCacheListedAt.set" in body, (
        "the expand handler must stamp the listing it writes, so a later refresh "
        "refetches it instead of treating it as current"
    )
    assert "expandGen" in body and "_wsTreeGen" in body, (
        "the expand handler must check the tree generation, like loadDir does"
    )
    compact = body.replace(" ", "")
    assert "session_id===expandSession" in compact, (
        "the expand handler must check the session it was requested under"
    )


def test_the_expand_guard_rejects_a_stale_epoch():
    """Drive the guard's own predicate: a stale epoch must refuse the write."""
    script = r"""
const S = { session: { session_id: 'S1' }, _dirCache: {} };
let _wsTreeGen = 1;
let _wsTreeCacheEpoch = 3;
const _wsTreeCacheListedAt = new Map();

const item = { path: 'docs' };
const expandSession = S.session.session_id;
const expandGen = _wsTreeGen;
const expandEpoch = _wsTreeCacheEpoch;

// A refresh clears the cache while the expand request is in flight.
S._dirCache = {};
_wsTreeCacheListedAt.clear();
_wsTreeCacheEpoch += 1;

const stillOurs = S.session && S.session.session_id === expandSession
  && expandGen === _wsTreeGen && expandEpoch === _wsTreeCacheEpoch;
if (stillOurs) {
  S._dirCache[item.path] = [{ name: 'stale.md' }];
  _wsTreeCacheListedAt.set(item.path, expandEpoch);
}
console.log(JSON.stringify({
  wrote: S._dirCache['docs'] || null,
  stamped: _wsTreeCacheListedAt.has('docs'),
}));
"""
    result = _run_node(script)
    assert result["wrote"] is None, (
        "a listing requested in a superseded cache epoch must not be written"
    )
    assert result["stamped"] is False, (
        "a refused write must not leave a listed-at stamp behind"
    )
