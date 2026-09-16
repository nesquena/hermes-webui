"""#6224 frontend regression: the inline Markdown preview lifecycle.

`loadMarkdownInline()` fetches a `.md` artifact through `/api/media` and swaps
its loader node for a rendered preview. Four properties decide whether that
preview is actually usable:

1. every URL the loader builds — the fetch URL and the download link on the
   success, oversize and error paths — carries the `session_id` grant (an
   out-of-root artifact is served only while the session itself emitted a
   `MEDIA:` token for it) and the message-level `&snap=<digest>` stamp (so a
   historical preview shows the bytes the message emitted, not mutated ones);
2. the resolved preview is inserted as a *node* and then goes through the same
   post-processing pipeline as every other rendered message. The fetch settles
   after the synchronous `postProcessRenderedMessages()` pass already ran, so
   without an explicit second pass the inserted subtree keeps inert code
   blocks, no copy buttons, un-rendered mermaid/katex and un-hydrated nested
   media;
3. a fetch that settles against a detached node (the transcript re-rendered)
   or after a session switch must not paint into the DOM that another render
   now owns;
4. a self-referential `.md` → `.md` reference chain must stop instead of
   recursing.

These tests execute the real `loadMarkdownInline()`,
`postProcessRenderedMessages()`, `_mediaSnapQuery()` and `_mediaSessionQuery()`
extracted from `static/ui.js` under node with a minimal DOM double; only the
browser surface (fetch, renderMd, i18n, the individual enhancers) is stubbed,
and the stubs record what the loader asked them to do. The suffix matrix in the
last test evaluates the real declarations from `static/ui.js` and
`static/workspace.js`. Same node-harness style as
tests/test_5306_subagent_sidebar_flicker.py.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
WORKSPACE_JS = REPO_ROOT / "static" / "workspace.js"
NODE = shutil.which("node")

# The one deliberate Markdown compatibility set shared by api/config.py
# (MD_EXTS + MIME_MAP), static/workspace.js (MD_EXTS) and static/ui.js
# (_MD_EXTS).
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd", ".mkdn")
NON_MARKDOWN_SUFFIX = ".txt"

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Snippet spliced in front of the harness: evaluates the real suffix
# declarations, so the matrix reflects static/ui.js + static/workspace.js
# instead of a re-typed copy.
_MATRIX_SNIPPET = r"""
var MATRIX = {};
function REPORT_MATRIX(){
  const mods = {};
  eval(SOURCES.mdExtsLine + '\nmods.ui = _MD_EXTS;');
  eval(SOURCES.workspaceMdExtsLine + '\nmods.workspace = MD_EXTS;');
  scenario.suffixes.forEach(ext => {
    MATRIX[ext] = {
      ui_inline_regex: mods.ui.test('notes' + ext),
      workspace_set: mods.workspace.has(ext),
    };
  });
}
"""

_HARNESS = r"""
const scenario = JSON.parse(process.env.SCENARIO_MD);
const SOURCES = scenario.sources || {};

// ── minimal DOM double ────────────────────────────────────────────────────
// Only the surface loadMarkdownInline()/postProcessRenderedMessages() touch:
// dataset/attributes, innerHTML -> child nodes, replaceWith(), parentNode and
// isConnected (detachment). Nested preview nodes are materialised from the
// markup the stubbed renderMd() returns, so the recursion guard is exercised
// against real markup instead of a hand-built tree.
class FNode {
  constructor(className){
    this.className = className || '';
    this.attrs = {};
    this.dataset = {};
    this.children = [];
    this.parentNode = null;
    this._connected = true;
    this._html = '';
  }
  get isConnected(){ return this._connected; }
  setAttribute(k, v){
    this.attrs[k] = String(v);
    if(k === 'class') this.className = String(v);
    if(k.indexOf('data-') === 0){
      this.dataset[k.slice(5).replace(/-([a-z])/g, (m, c) => c.toUpperCase())] = String(v);
    }
  }
  getAttribute(k){ return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  set innerHTML(html){ this._html = String(html); this.children = parseNodes(this._html, this); }
  get innerHTML(){ return this._html; }
  set outerHTML(html){
    this._html = String(html);
    if(this.parentNode){
      const repl = new FNode('');
      repl._html = this._html;
      repl.parentNode = this.parentNode;
      const idx = this.parentNode.children.indexOf(this);
      if(idx >= 0) this.parentNode.children[idx] = repl; else this.parentNode.children.push(repl);
      this.parentNode = null;
    }
  }
  get outerHTML(){ return this._html; }
  appendChild(child){ child.parentNode = this; this.children.push(child); return child; }
  replaceWith(node){
    const parent = this.parentNode;
    node.parentNode = parent;
    if(parent){
      const idx = parent.children.indexOf(this);
      if(idx >= 0) parent.children[idx] = node; else parent.children.push(node);
    }
    this.parentNode = null;
  }
  detach(){
    if(this.parentNode){
      const idx = this.parentNode.children.indexOf(this);
      if(idx >= 0) this.parentNode.children.splice(idx, 1);
    }
    this.parentNode = null;
    this._connected = false;
  }
  descendants(out){
    out = out || [];
    (this.children || []).forEach(child => { out.push(child); child.descendants(out); });
    return out;
  }
  querySelectorAll(sel){
    const m = /^\.([a-z0-9-]+)(?::not\(\[([a-z0-9-]+)\]\))?$/.exec(sel);
    if(!m) throw new Error('unsupported selector: ' + sel);
    const cls = m[1], notAttr = m[2];
    return this.descendants().filter(node => {
      const has = node.className && node.className.split(/\s+/).indexOf(cls) >= 0;
      return has && (!notAttr || node.getAttribute(notAttr) == null);
    });
  }
}

function parseNodes(html, parent){
  const out = [];
  const tagRe = /<div class="([^"]*)"([^>]*)>/g;
  let tag;
  while((tag = tagRe.exec(String(html))) !== null){
    const node = new FNode(tag[1]);
    node._html = tag[0];
    node.parentNode = parent || null;
    const attrRe = /data-([a-z-]+)="([^"]*)"/g;
    let attr;
    while((attr = attrRe.exec(tag[2])) !== null){
      node.setAttribute('data-' + attr[1], attr[2]);
    }
    out.push(node);
  }
  if(!out.length && html){
    const text = new FNode('');
    text._html = String(html);
    text.parentNode = parent || null;
    out.push(text);
  }
  return out;
}

// document.createElement() is the only other DOM entry point the loader uses:
// it materialises the resolved preview as a node before insertion.
globalThis.document = {
  createElement: () => new FNode(''),
};

// ── the real source, evaluated at top level so its declarations become
// script-scope bindings the harness can call (no re-implementation).
eval(SOURCES.postProcess || '');
eval(SOURCES.snapQuery || '');
eval(SOURCES.sessionQuery || '');
eval(SOURCES.depth || '');
eval(SOURCES.stale || '');
eval(SOURCES.dropNested || '');
// MD_INLINE_MAX_DEPTH is a const: it has to share one eval scope with the
// loader that closes over it.
eval((SOURCES.maxDepth || '') + '\n' + (SOURCES.loader || ''));

// ── scenario state ────────────────────────────────────────────────────────
let renderCalls = 0;
let insertedNode = null;
const record = {fetches: [], enhancerCalls: []};

const root = new FNode('msgInner');
const loaderNode = new FNode('md-inline-load');
root.appendChild(loaderNode);
loaderNode.setAttribute('data-path', scenario.path);
if(scenario.snap) loaderNode.setAttribute('data-snap', scenario.snap);
const originalReplaceWith = loaderNode.replaceWith.bind(loaderNode);
loaderNode.replaceWith = node => { insertedNode = node; originalReplaceWith(node); };

const MD_MAX_SIZE = scenario.maxSize;
const S = scenario.sessionId ? {session: {session_id: scenario.sessionId}} : {session: null};
function esc(s){ return String(s); }
function t(key){
  const dict = {
    md_loading: 'Carregando',
    md_download: 'Baixar',
    md_too_large: 'Arquivo grande demais para pré-visualizar',
    md_error: 'Erro ao carregar markdown',
  };
  return Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : key;
}
function renderMd(text){
  renderCalls += 1;
  const nested = scenario.nested
    ? '<div class="md-inline-load" data-path="' + scenario.nestedPath + '">nested</div>'
    : '';
  return '<p class="rendered">' + text + '</p>' + nested;
}
function fetch(url){
  record.fetches.push(url);
  if(record.fetches.length > 12) throw new Error('runaway markdown recursion');
  if(scenario.network === 'reject') return Promise.reject(new Error('network down'));
  return Promise.resolve({
    ok: scenario.ok !== false,
    status: scenario.status || 200,
    text: async () => scenario.text,
  });
}
// The individual enhancers are browser-side; recording stubs stand in for them
// so the pipeline dispatch (membership + scope) is what gets asserted.
['highlightCode', 'addCopyButtons', 'loadDiffInline', 'loadCsvInline',
 'loadExcalidrawInline', 'loadPdfInline', 'loadHtmlInline', 'renderMermaidBlocks',
 'renderKatexBlocks', 'initTreeViews'].forEach(name => {
  globalThis[name] = container => { record.enhancerCalls.push({name: name, scope: container}); };
});

function collectHtml(node){
  let out = node._html || '';
  (node.children || []).forEach(child => { out += '\n' + collectHtml(child); });
  return out;
}

const watchdog = setTimeout(() => {
  console.log(JSON.stringify({error: 'harness watchdog: the loader never settled'}));
  process.exit(0);
}, 10000);

function report(){
  clearTimeout(watchdog);
  console.log(JSON.stringify({
    fetches: record.fetches,
    enhancerCalls: record.enhancerCalls.map(c => ({name: c.name, scopeIsInserted: c.scope === insertedNode})),
    enhancerNames: record.enhancerCalls.map(c => c.name),
    renderCalls: renderCalls,
    loaderHtml: loaderNode.outerHTML,
    insertedHtml: insertedNode ? collectHtml(insertedNode) : '',
    rootChildren: root.children.map(c => c.className),
    matrix: (typeof MATRIX === 'undefined') ? null : MATRIX,
  }));
}

(async () => {
  try {
    if(scenario.mode === 'suffixMatrix'){
      REPORT_MATRIX();
      report();
      return;
    }
    loadMarkdownInline(root);
    if(scenario.detach) loaderNode.detach();
    if(scenario.switchSession) S.session = {session_id: scenario.sessionAfter};
    for(let i = 0; i < 40; i++) await new Promise(resolve => setTimeout(resolve, 0));
    report();
  } catch (e) {
    clearTimeout(watchdog);
    console.log(JSON.stringify({error: String((e && e.message) || e)}));
  }
})();
"""

_NODE_ENV_ALLOWLIST = ("PATH", "NODE_PATH")


def _node_env(**values) -> dict:
    """Minimal explicit environment for the node subprocess.

    The harness needs an interpreter lookup path and its scenario payload —
    nothing else. Copying the ambient environment would hand the child every
    secret the parent happens to hold.
    """
    env = {key: os.environ[key] for key in _NODE_ENV_ALLOWLIST if key in os.environ}
    env.update(values)
    return env


def _run_node(payload: dict) -> dict:
    harness = _MATRIX_SNIPPET + _HARNESS
    proc = subprocess.run(
        [NODE],
        input=harness,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        env=_node_env(SCENARIO_MD=json.dumps(payload)),
        timeout=60,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}\n{proc.stdout}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    return out


def _extract(name: str) -> str:
    from tests.js_source_extract import extract_function

    return extract_function(UI_JS.read_text(encoding="utf-8"), name)


def _extract_optional(name: str) -> str:
    try:
        return _extract(name)
    except AssertionError:
        return ""


def _extract_const(source: str, name: str) -> str:
    match = re.search(rf"^const {name}\s*=\s*[^;]+;", source, re.M)
    return match.group(0) if match else ""


def _sources() -> dict:
    ui_source = UI_JS.read_text(encoding="utf-8")
    return {
        "loader": _extract("loadMarkdownInline"),
        "postProcess": _extract("postProcessRenderedMessages"),
        "snapQuery": _extract("_mediaSnapQuery"),
        "sessionQuery": _extract("_mediaSessionQuery"),
        "depth": _extract_optional("_mdInlineDepth"),
        "stale": _extract_optional("_mdInlineStale"),
        "dropNested": _extract_optional("_mdInlineDropNestedLoaders"),
        "maxDepth": _extract_const(ui_source, "MD_INLINE_MAX_DEPTH"),
    }


def _run(
    text="short body",
    session_id="s-grant-123",
    ok=True,
    status=200,
    network="ok",
    max_size=4000,
    snap="",
    path="/opt/agent/work/notes.md",
    detach=False,
    switch_session=None,
    nested=False,
    nested_path="/opt/agent/work/inner.md",
) -> dict:
    sources = _sources()
    assert sources["postProcess"], "postProcessRenderedMessages() must exist in static/ui.js"
    payload = {
        "mode": "loader",
        "path": path,
        "text": text,
        "sessionId": session_id,
        "ok": ok,
        "status": status,
        "network": network,
        "maxSize": max_size,
        "snap": snap,
        "nested": nested,
        "nestedPath": nested_path,
        "detach": detach,
        "switchSession": bool(switch_session),
        "sessionAfter": switch_session,
        "sources": sources,
    }
    return _run_node(payload)


def _download_href(html: str) -> str:
    match = re.search(r'class="msg-media-link" href="([^"]+)"', html)
    assert match, f"no media download link in rendered HTML: {html!r}"
    return match.group(1)


# ── the session grant + snapshot must reach every URL ─────────────────────


def test_fetch_url_and_download_link_carry_the_session_grant():
    out = _run(session_id="s-grant-123")

    assert "path=%2Fopt%2Fagent%2Fwork%2Fnotes.md" in out["fetches"][0], out["fetches"]
    assert "session_id=s-grant-123" in out["fetches"][0], out["fetches"]

    href = _download_href(out["insertedHtml"])
    assert "download=1" in href, href
    assert "session_id=s-grant-123" in href, href
    assert out["renderCalls"] == 1, "a previewable file must be rendered, not fall back"
    assert "Baixar" in out["insertedHtml"], "the download label must come from i18n, not a literal"


def test_oversize_download_link_retains_the_session_grant():
    out = _run(text="x" * 64, session_id="s-grant-123", max_size=16)

    assert "session_id=s-grant-123" in out["fetches"][0], out["fetches"]
    assert out["renderCalls"] == 0, "an oversize file must not be rendered"
    href = _download_href(out["loaderHtml"])
    assert "download=1" in href, href
    assert "session_id=s-grant-123" in href, href
    assert "grande demais" in out["loaderHtml"], "the oversize state must be translated"


def test_error_download_link_retains_the_session_grant():
    out = _run(session_id="s-grant-123", ok=False, status=403)

    assert "session_id=s-grant-123" in out["fetches"][0], out["fetches"]
    href = _download_href(out["loaderHtml"])
    assert "download=1" in href, href
    assert "session_id=s-grant-123" in href, href
    assert "carregar markdown" in out["loaderHtml"], "the error state must be translated"


def test_network_failure_download_link_retains_the_session_grant():
    out = _run(session_id="s-grant-123", network="reject")

    assert "session_id=s-grant-123" in out["fetches"][0], out["fetches"]
    href = _download_href(out["loaderHtml"])
    assert "session_id=s-grant-123" in href, href
    assert "carregar markdown" in out["loaderHtml"]


def test_public_urls_omit_the_grant_without_an_active_session():
    out = _run(session_id=None)

    assert "session_id=" not in out["fetches"][0], out["fetches"]
    assert "session_id=" not in _download_href(out["insertedHtml"])


def test_snapshot_digest_reaches_the_fetch_and_every_download_url():
    digest = "a" * 64

    success = _run(snap=digest)
    assert f"&snap={digest}" in success["fetches"][0], success["fetches"]
    assert f"&snap={digest}" in _download_href(success["insertedHtml"]), success["insertedHtml"]

    oversize = _run(text="x" * 64, max_size=16, snap=digest)
    assert f"&snap={digest}" in oversize["fetches"][0], oversize["fetches"]
    assert f"&snap={digest}" in _download_href(oversize["loaderHtml"]), oversize["loaderHtml"]

    failed = _run(ok=False, status=500, snap=digest)
    assert f"&snap={digest}" in failed["fetches"][0], failed["fetches"]
    assert f"&snap={digest}" in _download_href(failed["loaderHtml"]), failed["loaderHtml"]

    offline = _run(network="reject", snap=digest)
    assert f"&snap={digest}" in offline["fetches"][0], offline["fetches"]
    assert f"&snap={digest}" in _download_href(offline["loaderHtml"]), offline["loaderHtml"]

    unstamped = _run(snap="not-a-digest")
    assert "&snap=" not in unstamped["fetches"][0], unstamped["fetches"]


# ── the inserted preview must go through the post-processing pipeline ──────


def test_inserted_preview_runs_the_full_post_processing_pipeline():
    out = _run(text="hello **world**")

    assert out["renderCalls"] == 1, out
    assert "hello **world**" in out["insertedHtml"], out["insertedHtml"]
    assert out["rootChildren"] == ["md-inline-wrap"], out["rootChildren"]
    assert out["enhancerNames"] == [
        "highlightCode", "addCopyButtons", "loadDiffInline", "loadCsvInline",
        "loadExcalidrawInline", "loadPdfInline", "loadHtmlInline",
        "renderMermaidBlocks", "renderKatexBlocks", "initTreeViews",
    ], out["enhancerNames"]
    assert all(call["scopeIsInserted"] for call in out["enhancerCalls"]), (
        "every enhancer must run against the node that was just inserted, "
        "not against a stale or detached subtree"
    )


def test_nested_markdown_reference_does_not_recurse():
    out = _run(text="outer", nested=True)

    # The preview renders and hydrates, the nested `.md` reference it emits is
    # resolved once, and then the chain stops instead of following itself.
    assert len(out["fetches"]) == 2, out["fetches"]
    assert "inner.md" in out["fetches"][1], out["fetches"]
    assert "session_id=s-grant-123" in out["fetches"][0], out["fetches"]
    assert "session_id=s-grant-123" in out["fetches"][1], out["fetches"]
    assert 'class="msg-media-link"' in out["insertedHtml"], out["insertedHtml"]
    assert "download=1" in out["insertedHtml"], out["insertedHtml"]


# ── stale targets must not be painted ─────────────────────────────────────


def test_detached_preview_node_is_not_rewritten():
    out = _run(text="late body", detach=True)

    assert len(out["fetches"]) == 1, "the fetch still had to happen"
    assert out["renderCalls"] == 0, "a detached preview must not be rendered"
    assert out["loaderHtml"] == "", "a detached node must not be rewritten"
    assert "late body" not in out["insertedHtml"], out["insertedHtml"]
    assert out["rootChildren"] == [], out["rootChildren"]
    assert out["enhancerCalls"] == [], "no enhancement may run for a dropped preview"


def test_session_switch_during_fetch_is_not_painted():
    out = _run(text="other session body", session_id="s-old", switch_session="s-new")

    assert len(out["fetches"]) == 1, out["fetches"]
    assert "session_id=s-old" in out["fetches"][0], out["fetches"]
    assert out["renderCalls"] == 0, "the previous session's artifact must not be painted"
    assert out["insertedHtml"] == "", out["insertedHtml"]
    assert out["enhancerCalls"] == [], out["enhancerCalls"]


# ── the suffix contract, evaluated from the real frontend declarations ─────


def test_frontend_markdown_detection_covers_the_shared_suffix_set():
    ui_source = UI_JS.read_text(encoding="utf-8")
    ws_source = WORKSPACE_JS.read_text(encoding="utf-8")
    ui_line = _extract_const(ui_source, "_MD_EXTS")
    ws_line = _extract_const(ws_source, "MD_EXTS")
    assert ui_line, "static/ui.js must declare _MD_EXTS"
    assert ws_line, "static/workspace.js must declare MD_EXTS"

    out = _run_node(
        {
            "mode": "suffixMatrix",
            "suffixes": list(MARKDOWN_SUFFIXES) + [NON_MARKDOWN_SUFFIX],
            "sources": {"mdExtsLine": ui_line, "workspaceMdExtsLine": ws_line},
        }
    )
    matrix = out["matrix"]

    for suffix in MARKDOWN_SUFFIXES:
        assert matrix[suffix]["ui_inline_regex"], f"chat inline detection misses {suffix}"
        assert matrix[suffix]["workspace_set"], f"workspace detection misses {suffix}"
    assert not matrix[NON_MARKDOWN_SUFFIX]["ui_inline_regex"], "*.txt is not Markdown"
    assert not matrix[NON_MARKDOWN_SUFFIX]["workspace_set"], "*.txt is not Markdown"
