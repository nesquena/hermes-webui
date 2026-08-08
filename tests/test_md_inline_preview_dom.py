"""
Behavior-level (non-source-string) regression for the deferred-fetch inline
Markdown preview (PR #6277 re-gate).

Runs the REAL `loadMarkdownInline()` (and the REAL `_postProcessMdInlineSubtree()`
helper) from static/ui.js inside a FakeDOM with a stubbed `fetch`, and asserts
on observable behavior:

1. When the fetch resolves, the rendered Markdown subtree is inserted AND the
   bounded post-processors run on that new `.md-inline-content` subtree.
2. Both the preview (fetch) URL and the download link URL retain the same
   encoded `session_id`; the query is omitted entirely when no session exists.
3. The size-cap path falls back to a download link (with session grant) and
   does NOT run post-processors.
4. The fetch-error path falls back to a download link too.

The sanitizer boundary is pinned behaviorally: the real production function is
extracted, so if anyone bypasses `renderMd()` the `renderMdCalls` assertion
fails.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
UI_JS_PATH = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

MD_SAMPLE = "# Title\n\n```mermaid\nflowchart LR\nA-->B\n```\n\n```python\nprint(1)\n```\n"


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    # Hardened brace matcher (skips strings/templates/regex/comments) — same
    # extractor as the sibling #5744 / renderMessages suites.
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  let str = null;
  let inLine = false;
  let inBlock = false;
  let inRegex = false;
  let prev = '';
  while (depth > 0 && i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i++; } i++; continue; }
    if (str) {
      if (c === '\\') { i += 2; continue; }
      if (c === str) str = null;
      i++; continue;
    }
    if (inRegex) {
      if (c === '\\') { i += 2; continue; }
      if (c === '/') inRegex = false;
      i++; continue;
    }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; i++; continue; }
    if (c === '/' && !'})]0123456789'.includes(prev) && !/[A-Za-z_$]/.test(prev)) {
      inRegex = true; i++; continue;
    }
    if (c === '{') depth++;
    else if (c === '}') depth--;
    if (c.trim()) prev = c;
    i++;
  }
  return src.slice(start, i);
}
"""
    return prelude + body


def _fakedom_prelude() -> str:
    return r"""
// ── Minimal FakeDOM (models the browser subset loadMarkdownInline touches) ──
const VOID = new Set(['br','img','hr','input','meta','link']);
function dataKey(name) {
  return String(name).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}
class FakeClassList {
  constructor(el){ this._el = el; }
  contains(name){ return this._el.className.split(/\s+/).includes(name); }
  add(name){ if(!this.contains(name)) this._el.className = (this._el.className + ' ' + name).trim(); }
  remove(name){ this._el.className = this._el.className.split(/\s+/).filter(c => c && c !== name).join(' '); }
}
function parseAttrs(attrStr, el){
  const re = /([A-Za-z_:][A-Za-z0-9_.:-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
  let m;
  while ((m = re.exec(attrStr))) {
    const name = m[1];
    const value = m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4] : '';
    el.setAttribute(name, value);
  }
}
function parseHTMLInto(html, parent){
  const re = /<(\/?)([A-Za-z][A-Za-z0-9]*)((?:\s+[^<>]*?)?)(\/?)>/g;
  const stack = [parent];
  let last = 0;
  let m;
  while ((m = re.exec(html))) {
    const text = html.slice(last, m.index);
    if (text && stack.length) stack[stack.length - 1]._text += text;
    last = m.index + m[0].length;
    const closing = m[1], tag = m[2], attrs = m[3], selfClose = m[4];
    if (closing) { if (stack.length > 1) stack.pop(); }
    else {
      const el = new FakeElement(tag);
      parseAttrs(attrs, el);
      stack[stack.length - 1].appendChild(el);
      if (!selfClose && !VOID.has(tag)) stack.push(el);
    }
  }
  const tail = html.slice(last);
  if (tail && stack.length) stack[stack.length - 1]._text += tail;
}
function matchesSimple(el, selector){
  if (!el || el.nodeType === 3) return false;
  const negated = [];
  const base = String(selector || '').replace(/:not\(([^()]*)\)/g, (_, inner) => {
    negated.push(String(inner || '').trim());
    return '';
  }).trim();
  if (negated.some(inner => inner && matchesSimple(el, inner))) return false;
  if (!base) return true;
  const classMatches = [...base.matchAll(/\.([A-Za-z0-9_-]+)/g)].map(m => m[1]);
  if (classMatches.some(name => !el.classList.contains(name))) return false;
  const attrMatches = [...base.matchAll(/\[([^=\]]+)(?:=["']?([^"'^\]]+)["']?)?\]/g)];
  for (const match of attrMatches) {
    const got = el.getAttribute(match[1]);
    if (got === null) return false;
    if (match[2] !== undefined && String(got) !== String(match[2])) return false;
  }
  const idMatch = base.match(/#([A-Za-z0-9_-]+)/);
  if (idMatch && el.id !== idMatch[1]) return false;
  const tagMatch = base.match(/^[A-Za-z][A-Za-z0-9_-]*/);
  if (tagMatch && el.tagName.toLowerCase() !== tagMatch[0].toLowerCase()) return false;
  return true;
}
function matchesSelector(el, selector){
  return String(selector || '').split(',').some(part => {
    part = part.trim();
    const pieces = part.split(/\s+/);
    if (pieces.length === 1) return matchesSimple(el, pieces[0]);
    // descendant chain: last piece matches el, earlier pieces match ancestors in order
    const chain = [];
    let node = el;
    while (node) { chain.push(node); node = node.parentElement; }
    let pi = pieces.length - 1;
    for (let i = 0; i < chain.length && pi >= 0; i++) {
      if (matchesSimple(chain[i], pieces[pi])) pi--;
    }
    return pi < 0;
  });
}
class FakeElement {
  constructor(tag){
    this.tagName = String(tag).toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.attributes = {};
    this.className = '';
    this.id = '';
    this._text = '';
    this.style = {};
    this.classList = new FakeClassList(this);
  }
  get textContent(){ return this.children.length ? this.children.map(c => c.textContent).join('') : this._text; }
  set textContent(v){ this._text = String(v); }
  get firstElementChild(){ return this.children[0] || null; }
  appendChild(child){ child.parentElement = this; this.children.push(child); return child; }
  set innerHTML(html){
    this.children = [];
    this._text = '';
    parseHTMLInto(String(html), this);
  }
  get innerHTML(){
    return this.children.map(c => '<' + c.tagName.toLowerCase() + '>' + c.innerHTML + '</' + c.tagName.toLowerCase() + '>').join('') + this._text;
  }
  set outerHTML(html){
    const parent = this.parentElement;
    if (!parent) return;
    const tmp = new FakeElement('div');
    tmp.innerHTML = String(html);
    const nodes = tmp.children.slice();
    const idx = parent.children.indexOf(this);
    parent.children.splice(idx, 1, ...nodes);
    for (const n of nodes) n.parentElement = parent;
    this.parentElement = null;
  }
  replaceWith(node){
    const parent = this.parentElement;
    if (!parent) return;
    const idx = parent.children.indexOf(this);
    parent.children.splice(idx, 1, node);
    node.parentElement = parent;
    this.parentElement = null;
  }
  setAttribute(name, value){
    this.attributes[name] = String(value);
    if (name === 'id') this.id = String(value);
    if (name === 'class') this.className = String(value);
    if (name.startsWith('data-')) this.dataset[dataKey(name)] = String(value);
  }
  getAttribute(name){
    if (name === 'id') return this.id || null;
    if (name === 'class') return this.className || null;
    if (name.startsWith('data-')) {
      const value = this.dataset[dataKey(name)];
      return value === undefined ? null : String(value);
    }
    return this.attributes[name] === undefined ? null : this.attributes[name];
  }
  removeAttribute(name){
    delete this.attributes[name];
    if (name.startsWith('data-')) delete this.dataset[dataKey(name)];
  }
  matches(selector){ return matchesSelector(this, selector); }
  querySelectorAll(selector){
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
}

// ── Globals + collaborator stubs the real loadMarkdownInline needs ──────────
const postProcCalls = [];
const renderMdCalls = [];
const fetchCalls = [];
let S = { session: { session_id: 'sess-123' } };
global.document = { createElement: (tag) => new FakeElement(tag) };
function esc(value){ return String(value == null ? '' : value); }
function t(key){ return key; }
function renderMd(txt){
  renderMdCalls.push(txt);
  // Mimics renderMd() output: elements the post-processors act on.
  return '<div class="mermaid-block" data-mermaid-id="m1">graph TD; A-->B</div>' +
         '<pre><code class="language-js">const x = 1</code></pre>';
}
function _spy(name){ return (root) => postProcCalls.push({ name: name, root: root }); }
const highlightCode = _spy('highlightCode');
const addCopyButtons = _spy('addCopyButtons');
const loadDiffInline = _spy('loadDiffInline');
const loadCsvInline = _spy('loadCsvInline');
const loadExcalidrawInline = _spy('loadExcalidrawInline');
const loadPdfInline = _spy('loadPdfInline');
const loadHtmlInline = _spy('loadHtmlInline');
const renderMermaidBlocks = _spy('renderMermaidBlocks');
const renderKatexBlocks = _spy('renderKatexBlocks');
const initTreeViews = _spy('initTreeViews');
"""


def _scenario(fetch_impl: str, session_id: str = "sess-123", path: str = "/tmp/notes.md") -> str:
    return (
        "eval(extractFunc('_postProcessMdInlineSubtree'));\n"
        "eval(extractFunc('loadMarkdownInline'));\n"
        "const SESSION_ID = " + json.dumps(session_id) + ";\n"
        "S = { session: { session_id: SESSION_ID } };\n"
        "global.fetch = " + fetch_impl + ";\n"
        "(async () => {\n"
        "  const container = new FakeElement('div');\n"
        "  container.innerHTML = '<div class=\"md-inline-load\" data-path=\"" + path + "\"></div>';\n"
        "  loadMarkdownInline(container);\n"
        "  await new Promise(r => setTimeout(r, 20));\n"
        "  const wrap = container.querySelector('.md-inline-wrap');\n"
        "  const contentEl = container.querySelector('.md-inline-content');\n"
        "  const headerLink = container.querySelector('.md-inline-header .msg-media-link');\n"
        "  const fallback = container.querySelector('.md-inline-fallback');\n"
        "  const mermaidInside = contentEl ? contentEl.querySelector('.mermaid-block') : null;\n"
        "  console.log(JSON.stringify({\n"
        "    wrapFound: !!wrap,\n"
        "    contentFound: !!contentEl,\n"
        "    fallbackFound: !!fallback,\n"
        "    fetchUrl: fetchCalls[0] || null,\n"
        "    downloadHref: headerLink ? headerLink.getAttribute('href') : (fallback ? fallback.querySelector('.msg-media-link').getAttribute('href') : null),\n"
        "    renderMdCalls: renderMdCalls.length,\n"
        "    renderMdText: renderMdCalls[0] || null,\n"
        "    postProcNames: postProcCalls.map(c => c.name),\n"
        "    postProcOnContent: postProcCalls.length > 0 && postProcCalls.every(c => c.root === contentEl),\n"
        "    mermaidInside: !!mermaidInside,\n"
        "  }));\n"
        "})().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });\n"
    )


def _harness(fetch_impl: str, session_id: str = "sess-123", path: str = "/tmp/notes.md") -> dict:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = (
        _extract_func_script(js)
        + _fakedom_prelude()
        + _scenario(fetch_impl, session_id=session_id, path=path)
    )
    out = _run_node(source)
    return json.loads(out.splitlines()[-1])


OK_FETCH = (
    "(url) => { fetchCalls.push(String(url)); "
    "return Promise.resolve({ ok: true, text: () => Promise.resolve("
    + json.dumps(MD_SAMPLE)
    + ") }); }"
)

LARGE_FETCH = (
    "(url) => { fetchCalls.push(String(url)); "
    "return Promise.resolve({ ok: true, text: () => Promise.resolve('x'.repeat(300000)) }); }"
)

ERROR_FETCH = "(url) => { fetchCalls.push(String(url)); return Promise.reject(new Error('network')); }"


def test_fetch_resolves_and_post_processes_inserted_subtree():
    """Deferred-fetch regression: once the fetch resolves, the sanitized
    renderMd() output lands in .md-inline-content AND every bounded
    post-processor runs on that exact subtree (code-copy, MEDIA loaders,
    Mermaid, KaTeX, tree views)."""
    m = _harness(OK_FETCH)
    assert m["wrapFound"] is True, "md-inline-wrap must be inserted after fetch"
    assert m["contentFound"] is True, ".md-inline-content subtree must exist"
    assert m["renderMdCalls"] == 1, "content must render through renderMd() (sanitizer boundary)"
    assert m["renderMdText"] == MD_SAMPLE, "renderMd must receive the fetched text"
    assert m["mermaidInside"] is True, "renderMd output must live inside the inserted subtree"
    for proc in [
        "highlightCode", "addCopyButtons", "loadDiffInline", "loadCsvInline",
        "loadExcalidrawInline", "loadPdfInline", "loadHtmlInline",
        "renderMermaidBlocks", "renderKatexBlocks", "initTreeViews",
    ]:
        assert proc in m["postProcNames"], f"{proc} must run on the inserted subtree"
    assert m["postProcOnContent"] is True, (
        "every post-processor must receive the inserted .md-inline-content element; "
        f"got {m['postProcNames']}"
    )


def test_preview_and_download_urls_keep_session_id_and_drop_it_without_session():
    """The preview (fetch) URL and the download link URL must retain the same
    encoded session_id; when no session exists the query is omitted entirely."""
    m = _harness(OK_FETCH, session_id="sess-123")
    assert m["fetchUrl"] == "api/media?path=%2Ftmp%2Fnotes.md&session_id=sess-123", m["fetchUrl"]
    assert m["downloadHref"] == (
        "api/media?path=%2Ftmp%2Fnotes.md&session_id=sess-123&download=1"
    ), m["downloadHref"]

    m2 = _harness(OK_FETCH, session_id="")
    assert m2["fetchUrl"] == "api/media?path=%2Ftmp%2Fnotes.md", (
        "no session_id query may be emitted when there is no session; got "
        + str(m2["fetchUrl"])
    )
    assert m2["downloadHref"] == "api/media?path=%2Ftmp%2Fnotes.md&download=1", m2["downloadHref"]


def test_oversized_markdown_falls_back_to_download_link_without_post_processing():
    """The size-cap path must degrade to a download link (still session-granted)
    and must NOT run post-processors on anything."""
    m = _harness(LARGE_FETCH, session_id="sess-123")
    assert m["fallbackFound"] is True, "oversized Markdown must show the fallback"
    assert m["wrapFound"] is False, "oversized Markdown must not insert a preview wrap"
    assert m["downloadHref"] == (
        "api/media?path=%2Ftmp%2Fnotes.md&session_id=sess-123&download=1"
    ), m["downloadHref"]
    assert m["postProcNames"] == [], "no post-processing may run on the size-cap path"


def test_fetch_error_falls_back_to_download_link():
    """A failed fetch must degrade to a download link, not a broken spinner."""
    m = _harness(ERROR_FETCH, session_id="sess-123")
    assert m["fallbackFound"] is True, "fetch error must show the fallback"
    assert m["downloadHref"] == (
        "api/media?path=%2Ftmp%2Fnotes.md&session_id=sess-123&download=1"
    ), m["downloadHref"]
    assert m["renderMdCalls"] == 0, "renderMd must not run on the error path"
