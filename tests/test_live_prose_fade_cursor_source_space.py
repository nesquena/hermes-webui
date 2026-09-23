"""Browserless regression: live prose fade rows must not split words across blocks.

Context
-------
Transparent-stream progress rows are built incrementally by
``_anchorProseIncrementalNode`` (static/messages.js), which streams the source
text into the vendored streaming-markdown parser. That parser always holds the
most recently written character back in its pending buffer, so the rendered
``.msg-body`` text lags the source text by one character while the row is live.

``_refreshTransparentFadeProseRow`` (static/ui.js) resumes appending from the
``data-stream-fade-text`` cursor, and used to fall back to ``body.textContent``
when that attribute was missing -- which is exactly the case for incrementally
built rows. The fallback mixed two different coordinate spaces (rendered text
vs source text), so the computed delta started one character early, in the
middle of a word, and was appended as a *sibling* of the parser's ``<p>``
element instead of inside it. A ``<p>`` is a block box, so the tail rendered on
its own line and the word was visually torn in half ("Ces deu" / "x fichiers").

These tests drive the real vendored parser to build the live row exactly like
production does, then run the real reconciler functions over it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

# Streamed source text, split exactly like the SSE token deltas that produced
# the original report.
FIRST_DELTA = "Ces"
SECOND_DELTA = " deux fichiers passent."
FULL_TEXT = FIRST_DELTA + SECOND_DELTA


def _run_node_module(script):
    assert NODE, "node is required for DOM-executed live prose render tests"
    env = os.environ.copy()
    env["UI_JS_PATH"] = str(ROOT / "static" / "ui.js")
    env["SMD_PATH"] = str(ROOT / "static" / "vendor" / "smd.min.js")
    env["MESSAGES_JS_PATH"] = str(ROOT / "static" / "messages.js")
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# A DOM stub faithful enough for this contract: textContent is *computed* from
# the node tree (the production code reads it back), and block-vs-inline
# structure is preserved so a torn word is observable.
_HARNESS = r"""
import { readFileSync } from 'node:fs';
const src = readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFuncFrom(source, name){
  const marker = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = source.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = source.indexOf('{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < source.length){
    if(source[i] === '{') depth += 1;
    else if(source[i] === '}') depth -= 1;
    i += 1;
  }
  return source.slice(start, i);
}
function extractFunc(name){ return extractFuncFrom(src, name); }
const BLOCK_TAGS = new Set(['P','DIV','H1','H2','H3','H4','H5','H6','BLOCKQUOTE','LI','UL','OL','PRE','TABLE']);
class TextNode {
  constructor(t){ this.nodeType = 3; this.tagName = '#text'; this._t = String(t); this.parentNode = null; this.childNodes = []; }
  get textContent(){ return this._t; }
  set textContent(v){ this._t = String(v); }
  cloneNode(){ return new TextNode(this._t); }
}
function extractConst(name){
  const marker = new RegExp('const\\s+' + name + '\\s*=');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('[', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    if(src[i] === '[') depth += 1;
    else if(src[i] === ']') depth -= 1;
    i += 1;
  }
  const end = src.indexOf(';', i);
  return src.slice(start, end + 1);
}
class FakeElement {
  constructor(tag='div'){
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.style = { setProperty(){}, removeProperty(){} };
    this.parentNode = null;
    this.type = '';
    this.disabled = false;
    this._classes = new Set();
    this._listeners = Object.create(null);
    const self = this;
    this.classList = {
      add(...n){ n.forEach(x=>self._classes.add(x)); },
      remove(...n){ n.forEach(x=>self._classes.delete(x)); },
      contains(n){ return self._classes.has(n); },
      toggle(n, force){
        if(force === true){ self._classes.add(n); return true; }
        if(force === false){ self._classes.delete(n); return false; }
        if(self._classes.has(n)){ self._classes.delete(n); return false; }
        self._classes.add(n); return true;
      },
    };
  }
  get children(){ return this.childNodes.filter(n=>n.nodeType === 1); }
  get parentElement(){ return this.parentNode; }
  get lastChild(){ return this.childNodes[this.childNodes.length - 1] || null; }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(v){ this._classes = new Set(String(v ?? '').trim().split(/\s+/).filter(Boolean)); }
  get textContent(){ return this.childNodes.map(n=>n.textContent).join(''); }
  set textContent(v){
    this.childNodes = [];
    const s = String(v ?? '');
    if(s) this.appendChild(new TextNode(s));
  }
  appendChild(n){
    if(n && n._isFragment){ n.childNodes.slice().forEach(c=>this.appendChild(c)); return n; }
    if(n.parentNode){ const i = n.parentNode.childNodes.indexOf(n); if(i >= 0) n.parentNode.childNodes.splice(i, 1); }
    n.parentNode = this; this.childNodes.push(n); return n;
  }
  insertBefore(n, ref){
    if(n && n._isFragment){ n.childNodes.slice().forEach(c=>this.insertBefore(c, ref)); return n; }
    if(n && n.parentNode){ const i = n.parentNode.childNodes.indexOf(n); if(i >= 0) n.parentNode.childNodes.splice(i, 1); }
    if(n) n.parentNode = this;
    if(!ref){ this.childNodes.push(n); return n; }
    const i = this.childNodes.indexOf(ref);
    if(i < 0) this.childNodes.push(n);
    else this.childNodes.splice(i, 0, n);
    return n;
  }
  replaceChild(n, old){
    const i = this.childNodes.indexOf(old);
    if(i < 0) return old;
    if(n && n.parentNode){ const j = n.parentNode.childNodes.indexOf(n); if(j >= 0) n.parentNode.childNodes.splice(j, 1); }
    if(old) old.parentNode = null;
    if(n) n.parentNode = this;
    this.childNodes[i] = n;
    return old;
  }
  remove(){
    if(!this.parentNode) return;
    const i = this.parentNode.childNodes.indexOf(this);
    if(i >= 0) this.parentNode.childNodes.splice(i, 1);
    this.parentNode = null;
  }
  setAttribute(name, value){ this.attributes[String(name)] = String(value ?? ''); }
  getAttribute(name){ const k = String(name); return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(name){ delete this.attributes[String(name)]; }
  getAttributeNames(){ return Object.keys(this.attributes); }
  addEventListener(type, fn){
    const key = String(type || '');
    if(!this._listeners[key]) this._listeners[key] = [];
    this._listeners[key].push(fn);
  }
  dispatchEvent(event){
    const type = event && event.type ? String(event.type) : '';
    const list = this._listeners[type] || [];
    for(const fn of list) fn(event);
    return true;
  }
  cloneNode(deep){
    const copy = new FakeElement(this.tagName);
    copy._classes = new Set(this._classes);
    for(const k of Object.keys(this.attributes)) copy.attributes[k] = this.attributes[k];
    for(const k of Object.keys(this.dataset)) copy.dataset[k] = this.dataset[k];
    if(deep) this.childNodes.forEach(c=>copy.appendChild(c.cloneNode(true)));
    return copy;
  }
  querySelector(sel){
    const options = String(sel || '').split(',').map(s => s.trim()).filter(Boolean);
    const matches = (el, option)=>{
      const classes = [];
      const re = /\.([A-Za-z0-9_-]+)/g;
      let m;
      while((m = re.exec(option))) classes.push(m[1]);
      if(!classes.length) return false;
      return classes.every(cls => el._classes && el._classes.has(cls));
    };
    const walk = (node)=>{
      for(const c of node.childNodes){
        if(c.nodeType !== 1) continue;
        if(options.some(opt => matches(c, opt))) return c;
        const hit = walk(c);
        if(hit) return hit;
      }
      return null;
    };
    return walk(this);
  }
  querySelectorAll(){ return []; }
}
class Fragment extends FakeElement {
  constructor(){ super('#fragment'); this._isFragment = true; }
}
globalThis.document = {
  createElement:(t)=>new FakeElement(t),
  createTextNode:(t)=>new TextNode(t),
  createDocumentFragment:()=>new Fragment(),
};
globalThis.window = { matchMedia: ()=>({ matches: false }) };

// Report whether any word is torn across a block boundary inside .msg-body,
// i.e. a block child whose text ends mid-word immediately followed by a
// sibling whose text starts mid-word.
function midWordBlockSplit(body){
  const kids = body.childNodes;
  for(let i = 0; i < kids.length - 1; i++){
    const cur = kids[i];
    const next = kids[i + 1];
    const isBlock = cur.nodeType === 1 && BLOCK_TAGS.has(cur.tagName);
    if(!isBlock) continue;
    const before = cur.textContent;
    const after = next.textContent;
    if(/\S$/.test(before) && /^\S/.test(after)){
      return { split: true, before: before.slice(-12), after: after.slice(0, 12) };
    }
  }
  return { split: false, before: '', after: '' };
}

// Non-whitespace text stranded at the root of .msg-body after a block element.
function strandedAfterBlock(body){
  let seenBlock = false, stranded = '';
  for(const kid of body.childNodes){
    if(kid.nodeType === 1 && BLOCK_TAGS.has(kid.tagName)){ seenBlock = true; continue; }
    if(seenBlock) stranded += kid.textContent;
  }
  return stranded.trim();
}
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_prose_row_without_cursor_keeps_words_intact():
    """A live row built incrementally must never be torn mid-word on refresh."""
    script = (
        _HARNESS
        + """
const smd = await import(process.env.SMD_PATH);
(0, eval)(extractConst('_TRANSPARENT_FADE_BLOCK_TAGS'));
(0, eval)(extractFunc('_transparentFadeAppendTarget'));
(0, eval)(extractFunc('_bindTransparentFadeCleanup'));
(0, eval)(extractFunc('_appendTransparentFadeText'));
(0, eval)(extractFunc('_transparentLiveRowAttributePairs'));
(0, eval)(extractFunc('_transparentLiveRowInteractiveState'));
(0, eval)(extractFunc('_rehydrateTransparentLiveRow'));
(0, eval)(extractFunc('_refreshTransparentFadeProseRow'));

const FIRST = %s;
const SECOND = %s;
const FULL = FIRST + SECOND;

// Live row exactly as _anchorProseIncrementalNode builds it: the real parser
// streams the source in, and is deliberately NOT ended (the turn is live).
const existing = new FakeElement('div');
existing.className = 'assistant-segment transparent-event-row';
existing.setAttribute('data-anchor-row-role', 'prose');
existing.setAttribute('data-anchor-row-id', 'live-prose-row');
existing.setAttribute('data-anchor-source-event-type', 'process_prose');
const body = new FakeElement('div');
body.className = 'msg-body stream-fade-active';
existing.appendChild(body);
const parser = smd.parser(smd.default_renderer(body));
smd.parser_write(parser, FIRST);
smd.parser_write(parser, SECOND.slice(0, 4));
const renderedBefore = body.textContent;
const sourceBefore = FIRST + SECOND.slice(0, 4);

// Candidate carrying the full source text for this row.
const candidate = new FakeElement('div');
candidate.className = 'assistant-segment transparent-event-row';
candidate.setAttribute('data-anchor-row-role', 'prose');
candidate.setAttribute('data-anchor-row-id', 'live-prose-row');
candidate.setAttribute('data-anchor-source-event-type', 'process_prose');
candidate.dataset.rawText = FULL;
const candidateBody = new FakeElement('div');
candidateBody.className = 'msg-body stream-fade-active';
candidateBody.textContent = FULL;
candidate.appendChild(candidateBody);

_refreshTransparentFadeProseRow(existing, candidate, null);

const refreshedBody = existing.querySelector('.msg-body');
const split = midWordBlockSplit(refreshedBody);
process.stdout.write(JSON.stringify({
  renderedLagsSource: renderedBefore.length < sourceBefore.length,
  renderedBefore,
  sourceBefore,
  finalText: refreshedBody.textContent,
  expectedText: FULL,
  midWordSplit: split.split,
  splitBefore: split.before,
  splitAfter: split.after,
  strandedAfterBlock: strandedAfterBlock(refreshedBody),
  cursor: existing.getAttribute('data-stream-fade-text'),
}));
"""
        % (json.dumps(FIRST_DELTA), json.dumps(SECOND_DELTA))
    )
    data = _run_node_module(script)

    # Precondition: the vendored parser really does lag the source text.
    assert data["renderedLagsSource"] is True, (
        "expected the streaming parser to hold a pending character, got "
        f"rendered={data['renderedBefore']!r} source={data['sourceBefore']!r}"
    )

    # The contract under test: no word may be torn across a block boundary.
    assert data["midWordSplit"] is False, (
        "live prose row was torn mid-word across a block boundary: "
        f"...{data['splitBefore']!r} | {data['splitAfter']!r}..."
    )
    assert data["strandedAfterBlock"] == "", (
        "text was appended outside the block element instead of inside it: "
        f"{data['strandedAfterBlock']!r}"
    )
    # And the visible text stays exactly the source text.
    assert data["finalText"] == data["expectedText"]
    assert data["cursor"] == FULL_TEXT


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_prose_row_with_cursor_still_appends_incrementally():
    """The existing incremental contract (cursor present) must be preserved."""
    script = (
        _HARNESS
        + """
(0, eval)(extractConst('_TRANSPARENT_FADE_BLOCK_TAGS'));
(0, eval)(extractFunc('_transparentFadeAppendTarget'));
(0, eval)(extractFunc('_bindTransparentFadeCleanup'));
(0, eval)(extractFunc('_appendTransparentFadeText'));
(0, eval)(extractFunc('_transparentLiveRowAttributePairs'));
(0, eval)(extractFunc('_transparentLiveRowInteractiveState'));
(0, eval)(extractFunc('_rehydrateTransparentLiveRow'));
(0, eval)(extractFunc('_refreshTransparentFadeProseRow'));

const existing = new FakeElement('div');
existing.className = 'assistant-segment transparent-event-row';
existing.setAttribute('data-anchor-row-role', 'prose');
existing.setAttribute('data-stream-fade-text', 'old ');
const body = new FakeElement('div');
body.className = 'msg-body stream-fade-active';
const oldSpan = new FakeElement('span');
oldSpan.className = 'stream-fade-word is-new';
oldSpan.textContent = 'old';
body.appendChild(oldSpan);
body.appendChild(new TextNode(' '));
existing.appendChild(body);

const candidate = new FakeElement('div');
candidate.className = 'assistant-segment transparent-event-row';
candidate.setAttribute('data-anchor-row-role', 'prose');
candidate.dataset.rawText = 'old new';
const candidateBody = new FakeElement('div');
candidateBody.className = 'msg-body stream-fade-active';
candidateBody.textContent = 'old new';
candidate.appendChild(candidateBody);

_refreshTransparentFadeProseRow(existing, candidate, null);
const refreshedBody = existing.querySelector('.msg-body');
const spans = refreshedBody.children.filter(c=>c._classes.has('stream-fade-word'));
process.stdout.write(JSON.stringify({
  text: refreshedBody.textContent,
  cursor: existing.getAttribute('data-stream-fade-text'),
  spanTexts: spans.map(s=>s.textContent),
  oldSpanPreserved: spans[0] === oldSpan,
}));
"""
    )
    data = _run_node_module(script)
    assert data["text"] == "old new"
    assert data["cursor"] == "old new"
    assert data["spanTexts"] == ["old", "new"]
    # Node identity of already-faded words must survive (scroll-anchor stability).
    assert data["oldSpanPreserved"] is True


# Shared eval preamble for the rebuild-branch tests: real ui.js reconciler
# functions plus the real messages.js mute helper, wired to window exactly the
# way production wires it (window.__streamFadeMuteRenderedPrefix).
_REBUILD_PRELUDE = """
const messagesSrc = readFileSync(process.env.MESSAGES_JS_PATH, 'utf8');
// Single combined eval: sloppy-mode indirect eval hoists function declarations
// to the global object but discards top-level `const` bindings after the call,
// so _TRANSPARENT_FADE_BLOCK_TAGS must be evaluated in the SAME eval as the
// functions that close over it (_transparentFadeAppendTarget).
(0, eval)([
  extractConst('_TRANSPARENT_FADE_BLOCK_TAGS'),
  extractFunc('_transparentFadeAppendTarget'),
  extractFunc('_bindTransparentFadeCleanup'),
  extractFunc('_appendTransparentFadeText'),
  extractFunc('_transparentLiveRowAttributePairs'),
  extractFunc('_transparentLiveRowInteractiveState'),
  extractFunc('_rehydrateTransparentLiveRow'),
  extractFunc('_refreshTransparentFadeProseRow'),
  extractFunc('_refreshTransparentLiveRow'),
].join('\\n'));
(0, eval)(extractFuncFrom(messagesSrc, '_streamFadeMuteRenderedPrefix'));
globalThis.window.__streamFadeMuteRenderedPrefix = globalThis._streamFadeMuteRenderedPrefix;
if(typeof window.__streamFadeMuteRenderedPrefix !== 'function'){
  throw new Error('messages.js must export _streamFadeMuteRenderedPrefix on window');
}
function makeRow(rawText){
  const row = new FakeElement('div');
  row.className = 'assistant-segment transparent-event-row';
  row.setAttribute('data-anchor-row-role', 'prose');
  row.setAttribute('data-anchor-row-id', 'live-prose-row');
  row.setAttribute('data-anchor-source-event-type', 'process_prose');
  if(rawText !== undefined) row.dataset.rawText = rawText;
  const rowBody = new FakeElement('div');
  rowBody.className = 'msg-body stream-fade-active';
  row.appendChild(rowBody);
  return row;
}
function collectFadeSpans(root){
  return collectFadeSpanNodes(root).map(c=>({ text: c.textContent, isNew: c._classes.has('is-new') }));
}
function collectFadeSpanNodes(root){
  const out = [];
  const walk = (node)=>{
    for(const c of node.childNodes || []){
      if(c.nodeType !== 1) continue;
      if(c._classes && c._classes.has('stream-fade-word')) out.push(c);
      walk(c);
    }
  };
  walk(root);
  return out;
}
function findTag(root, tag){
  for(const c of root.childNodes || []){
    if(c.nodeType !== 1) continue;
    if(c.tagName === tag) return c;
    const hit = findTag(c, tag);
    if(hit) return hit;
  }
  return null;
}
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_prose_rebuild_mutes_already_rendered_prefix():
    """#7082 review should-fix: the no-cursor rebuild must not re-animate the
    already-rendered prefix. Only genuinely-new tail words keep ``.is-new``."""
    script = (
        _HARNESS
        + _REBUILD_PRELUDE
        + """
const smd = await import(process.env.SMD_PATH);

const FIRST = %s;
const SECOND = %s;
const FULL = FIRST + SECOND;

// Existing row: incrementally built by the real parser, no cursor attribute.
const existing = makeRow(undefined);
delete existing.dataset.rawText;
const body = existing.querySelector('.msg-body');
const parser = smd.parser(smd.default_renderer(body));
smd.parser_write(parser, FIRST);
smd.parser_write(parser, SECOND.slice(0, 4));
const prevRendered = body.textContent;

// Candidate: the incremental fade node's parsed body — a <p> whose words are
// wrapped as fade spans, all `.is-new` (worst case: a rewind rebuild of the
// incremental node re-wrapped every word).
const candidate = makeRow(FULL);
const candidateBody = candidate.querySelector('.msg-body');
const p = new FakeElement('p');
candidateBody.appendChild(p);
const words = FULL.split(' ');
words.forEach((word, i)=>{
  const span = new FakeElement('span');
  span.className = 'stream-fade-word is-new';
  span.textContent = word;
  p.appendChild(span);
  if(i < words.length - 1) p.appendChild(new TextNode(' '));
});

_refreshTransparentFadeProseRow(existing, candidate, null);

const refreshedBody = existing.querySelector('.msg-body');
process.stdout.write(JSON.stringify({
  prevRendered,
  finalText: refreshedBody.textContent,
  spans: collectFadeSpans(refreshedBody),
  cursor: existing.getAttribute('data-stream-fade-text'),
  candidateBodyIntact: candidateBody.childNodes.length === 1 && candidateBody.childNodes[0] === p,
}));
"""
        % (json.dumps(FIRST_DELTA), json.dumps(SECOND_DELTA))
    )
    data = _run_node_module(script)

    assert data["finalText"] == FULL_TEXT
    assert data["cursor"] == FULL_TEXT
    spans = data["spans"]
    assert [s["text"] for s in spans] == FULL_TEXT.split(" ")
    # Words inside the previously-rendered prefix must NOT replay their fade …
    prev = data["prevRendered"]
    consumed = 0
    for span in spans:
        starts_in_prefix = consumed < len(prev)
        if starts_in_prefix:
            assert span["isNew"] is False, (
                f"already-rendered word {span['text']!r} would replay its fade "
                f"(prefix was {prev!r})"
            )
        consumed += len(span["text"]) + 1  # word + following space
    # … while genuinely-new tail words still animate.
    assert spans[-1]["isNew"] is True
    muted = [s for s in spans if not s["isNew"]]
    assert muted, "expected at least one muted already-rendered word"
    # The persistent incremental node (parser-owned DOM) must not be stolen.
    assert data["candidateBodyIntact"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_prose_rebuild_preserves_markdown_dom():
    """Greptile P1: the no-cursor rebuild must not flatten a live markdown row
    to literal source text — the parsed DOM (links, emphasis) must survive."""
    md_source = "Voici **un** [lien](https://example.com) utile pour tout le monde."
    script = (
        _HARNESS
        + _REBUILD_PRELUDE
        + """
const smd = await import(process.env.SMD_PATH);

const SOURCE = %s;

// Existing row: the parser has only seen a prefix of the source.
const existing = makeRow(undefined);
delete existing.dataset.rawText;
const body = existing.querySelector('.msg-body');
const parser = smd.parser(smd.default_renderer(body));
smd.parser_write(parser, SOURCE.slice(0, 10));
const prevRendered = body.textContent;

// Candidate: the incremental node fed the FULL source through the real
// streaming-markdown parser (still live, deliberately not ended). Production
// binds the parser on the body via _smdBindParserIdentity (messages.js), and
// the cursor fix reads the held-back tail from that binding.
const candidate = makeRow(SOURCE);
const candidateBody = candidate.querySelector('.msg-body');
const candidateParser = smd.parser(smd.default_renderer(candidateBody));
smd.parser_write(candidateParser, SOURCE);
candidateBody.__smdParser = candidateParser;
const candidateRendered = candidateBody.textContent;
const pendingTail = String(candidateParser.pending || '') + String(candidateParser.text || '');

const visible = _refreshTransparentFadeProseRow(existing, candidate, null);

const refreshedBody = visible.querySelector('.msg-body');
const link = findTag(refreshedBody, 'A');
const strong = findTag(refreshedBody, 'STRONG');
process.stdout.write(JSON.stringify({
  prevRendered,
  candidateRendered,
  pendingTail,
  finalText: refreshedBody.textContent,
  hasLink: !!link,
  linkText: link ? link.textContent : null,
  linkHref: link ? link.getAttribute('href') : null,
  hasStrong: !!strong,
  strongText: strong ? strong.textContent : null,
  cursor: visible.getAttribute('data-stream-fade-text'),
  adoptedParserRow: visible === candidate,
  sameParserBody: refreshedBody === candidateBody,
}));
"""
        % json.dumps(md_source)
    )
    data = _run_node_module(script)

    # The parsed markdown DOM survived the rebuild …
    assert data["hasLink"] is True, "rebuild dropped the parsed <a> element"
    assert data["linkText"] == "lien"
    assert data["linkHref"] == "https://example.com"
    assert data["hasStrong"] is True, "rebuild dropped the parsed <strong> element"
    assert data["strongText"] == "un"
    # … and no markdown syntax leaked into the visible text.
    assert "**" not in data["finalText"]
    assert "](" not in data["finalText"]
    assert data["finalText"] == data["candidateRendered"]
    # Parser-owned rows must not publish a source-space cursor. The next
    # refresh has to adopt the parsed DOM again; pending tails stay on the
    # parser until it flushes them.
    assert data["pendingTail"] == "."
    assert data["cursor"] is None
    assert data["adoptedParserRow"] is True
    assert data["sameParserBody"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_prose_rebuild_leaves_parser_pending_tail_on_parser():
    """Parser pending tails stay on the parser after a cursorless rebuild.

    A source-space resume cursor would make the next refresh append the
    held-back bytes as raw text and break later Markdown. The visible row
    must keep the adopted parsed DOM until the parser itself flushes.
    """
    md_source = "Voici **un** [lien](https://example.com) super."
    script = (
        _HARNESS
        + _REBUILD_PRELUDE
        + """
const smd = await import(process.env.SMD_PATH);

const SOURCE = %s;

const existing = makeRow(undefined);
delete existing.dataset.rawText;
const body = existing.querySelector('.msg-body');
const parser = smd.parser(smd.default_renderer(body));
smd.parser_write(parser, SOURCE.slice(0, 8));

const candidate = makeRow(SOURCE);
const candidateBody = candidate.querySelector('.msg-body');
const candidateParser = smd.parser(smd.default_renderer(candidateBody));
smd.parser_write(candidateParser, SOURCE);
candidateBody.__smdParser = candidateParser;
const pendingTail = String(candidateParser.pending || '') + String(candidateParser.text || '');
const candidateRendered = candidateBody.textContent;

const visible = _refreshTransparentFadeProseRow(existing, candidate, null);
const afterRebuildText = visible.querySelector('.msg-body').textContent;
const afterRebuildCursor = visible.getAttribute('data-stream-fade-text');

const sameVisible = _refreshTransparentFadeProseRow(visible, candidate, null);
const afterResumeBody = sameVisible.querySelector('.msg-body');
const afterResumeStrong = findTag(afterResumeBody, 'STRONG');
const afterResumeLink = findTag(afterResumeBody, 'A');

if(typeof smd.parser_end === 'function') smd.parser_end(candidateParser);
const settledVisible = _refreshTransparentFadeProseRow(sameVisible, candidate, null);
const afterEndBody = settledVisible.querySelector('.msg-body');

process.stdout.write(JSON.stringify({
  pendingTail,
  candidateRendered,
  afterRebuildText,
  afterRebuildCursor,
  afterResumeText: afterResumeBody.textContent,
  afterResumeCursor: sameVisible.getAttribute('data-stream-fade-text'),
  hasStrongAfterResume: !!afterResumeStrong,
  hasLinkAfterResume: !!afterResumeLink,
  literalStarsAfterResume: afterResumeBody.textContent.includes('**'),
  afterEndText: afterEndBody.textContent,
  literalStarsAfterEnd: afterEndBody.textContent.includes('**'),
  adoptedParserRow: visible === candidate,
  laterRefreshSameRow: sameVisible === candidate,
  settledSameRow: settledVisible === candidate,
  sameParserBody: afterResumeBody === candidateBody,
}));
"""
        % json.dumps(md_source)
    )
    data = _run_node_module(script)

    assert data["pendingTail"], "expected the streaming parser to hold a pending tail"
    assert data["pendingTail"] == "."
    assert data["afterRebuildText"] == data["candidateRendered"]
    assert not data["afterRebuildText"].endswith(data["pendingTail"])
    assert data["afterRebuildCursor"] is None
    # Same live parser: later refreshes must keep parsed structure. The
    # parser may flush its held-back "." into the candidate; that is still
    # parser-owned text, not a source-byte append.
    assert data["adoptedParserRow"] is True
    assert data["laterRefreshSameRow"] is True
    assert data["settledSameRow"] is True
    assert data["sameParserBody"] is True
    assert data["afterResumeCursor"] is None
    assert data["hasStrongAfterResume"] is True
    assert data["hasLinkAfterResume"] is True
    assert data["literalStarsAfterResume"] is False
    assert data["afterResumeText"].startswith("Voici un lien super")
    assert "**" not in data["afterResumeText"]
    assert "](" not in data["afterResumeText"]
    # After the parser flushes, the adopted DOM may grow, but still no
    # literal markdown syntax.
    assert data["literalStarsAfterEnd"] is False
    assert "super" in data["afterEndText"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_prose_keeps_parser_authority_across_markdown_close():
    """#7082 re-gate: two reconciliations across incomplete-to-complete Markdown
    must keep the parser as the only DOM authority.

    Cloning the parsed body and then appending later source bytes through
    ``_appendTransparentFadeText`` leaves literal ``**world**`` in the visible
    row while the live parser has already built ``<strong>world</strong>``.
    """
    first_source = "Hello **"
    full_source = "Hello **world** done "
    script = (
        _HARNESS
        + _REBUILD_PRELUDE
        + """
const smd = await import(process.env.SMD_PATH);

const FIRST = %s;
const FULL = %s;

function countSubstr(hay, needle){
  if(!needle) return 0;
  let n = 0, from = 0;
  while(true){
    const i = hay.indexOf(needle, from);
    if(i < 0) return n;
    n += 1;
    from = i + needle.length;
  }
}
function serialize(node){
  if(!node) return '';
  if(node.nodeType === 3) return node.textContent;
  const tag = String(node.tagName || '').toLowerCase();
  if(!tag || tag === '#fragment' || tag === 'div' && node._classes && node._classes.has('msg-body')){
    return (node.childNodes || []).map(serialize).join('');
  }
  const inner = (node.childNodes || []).map(serialize).join('');
  return '<' + tag + '>' + inner + '</' + tag + '>';
}

const existing = makeRow(undefined);
delete existing.dataset.rawText;

const candidate = makeRow(FIRST);
const candidateBody = candidate.querySelector('.msg-body');
const parser = smd.parser(smd.default_renderer(candidateBody));
smd.parser_write(parser, FIRST);
candidateBody.__smdParser = parser;

const visible = _refreshTransparentFadeProseRow(existing, candidate, null);

smd.parser_write(parser, FULL.slice(FIRST.length));
candidate.dataset.rawText = FULL;
const sameVisible = _refreshTransparentFadeProseRow(visible, candidate, null);

const liveAfterClose = sameVisible.querySelector('.msg-body');
const strongAfterClose = findTag(liveAfterClose, 'STRONG');
const htmlAfterClose = serialize(liveAfterClose);
const textAfterClose = liveAfterClose.textContent;
const candidateHtmlAfterClose = serialize(candidateBody);

if(typeof smd.parser_end === 'function') smd.parser_end(parser);
const settledVisible = _refreshTransparentFadeProseRow(sameVisible, candidate, null);
const liveAfterEnd = settledVisible.querySelector('.msg-body');
const strongAfterEnd = findTag(liveAfterEnd, 'STRONG');

process.stdout.write(JSON.stringify({
  htmlAfterClose,
  textAfterClose,
  candidateHtmlAfterClose,
  candidateTextAfterClose: candidateBody.textContent,
  hasStrongAfterClose: !!strongAfterClose,
  strongTextAfterClose: strongAfterClose ? strongAfterClose.textContent : null,
  helloCountAfterClose: countSubstr(textAfterClose, 'Hello'),
  worldCountAfterClose: countSubstr(textAfterClose, 'world'),
  doneCountAfterClose: countSubstr(textAfterClose, 'done'),
  literalStarsAfterClose: textAfterClose.includes('**'),
  htmlAfterEnd: serialize(liveAfterEnd),
  textAfterEnd: liveAfterEnd.textContent,
  hasStrongAfterEnd: !!strongAfterEnd,
  strongTextAfterEnd: strongAfterEnd ? strongAfterEnd.textContent : null,
  literalStarsAfterEnd: liveAfterEnd.textContent.includes('**'),
  helloCountAfterEnd: countSubstr(liveAfterEnd.textContent, 'Hello'),
  worldCountAfterEnd: countSubstr(liveAfterEnd.textContent, 'world'),
  doneCountAfterEnd: countSubstr(liveAfterEnd.textContent, 'done'),
  adoptedParserRow: visible === candidate,
  laterRefreshSameRow: sameVisible === candidate,
  settledSameRow: settledVisible === candidate,
  sameParserBody: liveAfterClose === candidateBody,
}));
"""
        % (json.dumps(first_source), json.dumps(full_source))
    )
    data = _run_node_module(script)

    assert data["hasStrongAfterClose"] is True, (
        "visible row lost parser emphasis after the second reconciliation: "
        f"html={data['htmlAfterClose']!r} text={data['textAfterClose']!r}"
    )
    assert data["strongTextAfterClose"] == "world"
    assert data["literalStarsAfterClose"] is False, (
        "literal ** leaked into the live row: "
        f"{data['textAfterClose']!r} html={data['htmlAfterClose']!r}"
    )
    assert data["helloCountAfterClose"] == 1
    assert data["worldCountAfterClose"] == 1
    assert data["doneCountAfterClose"] == 1
    assert "world" in data["textAfterClose"]
    assert "done" in data["textAfterClose"]

    assert data["hasStrongAfterEnd"] is True
    assert data["strongTextAfterEnd"] == "world"
    assert data["literalStarsAfterEnd"] is False
    assert data["helloCountAfterEnd"] == 1
    assert data["worldCountAfterEnd"] == 1
    assert data["doneCountAfterEnd"] == 1
    assert data["adoptedParserRow"] is True
    assert data["laterRefreshSameRow"] is True
    assert data["settledSameRow"] is True
    assert data["sameParserBody"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_prose_post_rewind_keeps_tail_word_identity_across_two_growths():
    """#7082 must-fix: after the separate-owner rewind transition, two later
    growth frames must keep the first genuinely-new tail span as the same
    node with ``.is-new`` until an explicit ``animationend``.
    """
    prefix_source = "Hello there"
    first_growth = "Hello there world"
    second_growth = "Hello there world more"
    script = (
        _HARNESS
        + _REBUILD_PRELUDE
        + """
const smd = await import(process.env.SMD_PATH);

const PREFIX = %s;
const FIRST = %s;
const SECOND = %s;

function wrapWords(parent, text){
  const words = String(text || '').split(' ');
  words.forEach((word, i)=>{
    const span = new FakeElement('span');
    span.className = 'stream-fade-word is-new';
    span.textContent = word;
    parent.appendChild(span);
    if(i < words.length - 1) parent.appendChild(new TextNode(' '));
  });
}

const host = new FakeElement('div');
const existing = makeRow(undefined);
delete existing.dataset.rawText;
const staleBody = existing.querySelector('.msg-body');
wrapWords(staleBody, PREFIX);
host.appendChild(existing);

const candidate = makeRow(PREFIX);
const candidateBody = candidate.querySelector('.msg-body');
const parser = smd.parser(smd.default_renderer(candidateBody));
smd.parser_write(parser, PREFIX);
candidateBody.__smdParser = parser;
const pendingAtAdopt = String(parser.pending || '') + String(parser.text || '');
// Keep the live parser identity, then rebuild fade spans so the mute + later
// growth can be observed as node identity. Do not parser_write after this:
// the default renderer would replace the word nodes we need to pin.
candidateBody.textContent = '';
const p = new FakeElement('p');
const strong = new FakeElement('strong');
const hello = new FakeElement('span');
hello.className = 'stream-fade-word is-new';
hello.textContent = 'Hello';
const there = new FakeElement('span');
there.className = 'stream-fade-word is-new';
there.textContent = 'there';
strong.appendChild(there);
p.appendChild(hello);
p.appendChild(new TextNode(' '));
p.appendChild(strong);
candidateBody.appendChild(p);

const visible = _refreshTransparentFadeProseRow(existing, candidate, null);
const afterAdoptNew = collectFadeSpanNodes(visible).filter(span => span._classes.has('is-new'));

candidate.dataset.rawText = FIRST;
const world = new FakeElement('span');
world.className = 'stream-fade-word is-new';
world.textContent = 'world';
p.appendChild(new TextNode(' '));
p.appendChild(world);
const afterFirstGrowth = _refreshTransparentLiveRow(visible, candidate, null);
const firstNewWasAnimated = world._classes.has('is-new');
const firstNewAfterFirstGrowth = world;

candidate.dataset.rawText = SECOND;
const more = new FakeElement('span');
more.className = 'stream-fade-word is-new';
more.textContent = 'more';
p.appendChild(new TextNode(' '));
p.appendChild(more);
const afterSecondGrowth = _refreshTransparentLiveRow(afterFirstGrowth, candidate, null);
const afterSecondSpans = collectFadeSpanNodes(afterSecondGrowth);
const afterSecondNew = afterSecondSpans.filter(span => span._classes.has('is-new'));
const secondNewSameIdentity = world === firstNewAfterFirstGrowth && afterSecondSpans.includes(world);
const secondNewIsNew = world._classes.has('is-new');

visible.querySelector('.msg-body').dispatchEvent({ type: 'animationend', target: world });
const afterCleanupNew = collectFadeSpanNodes(afterSecondGrowth).filter(span => span._classes.has('is-new'));

if(typeof smd.parser_end === 'function') smd.parser_end(parser);
const settled = _refreshTransparentLiveRow(afterSecondGrowth, candidate, null);
const settledBody = settled.querySelector('.msg-body');

process.stdout.write(JSON.stringify({
  adoptedParserRow: visible === candidate,
  firstGrowthSameRow: afterFirstGrowth === candidate,
  secondGrowthSameRow: afterSecondGrowth === candidate,
  sameParserBody: afterSecondGrowth.querySelector('.msg-body') === candidateBody,
  afterAdoptNewTexts: afterAdoptNew.map(span => span.textContent),
  firstNewText: firstNewAfterFirstGrowth.textContent,
  firstNewWasAnimated,
  firstNewStillConnected: !!(world.parentNode),
  secondNewSameIdentity,
  secondNewIsNew,
  afterSecondNewTexts: afterSecondNew.map(span => span.textContent),
  afterCleanupNewTexts: afterCleanupNew.map(span => span.textContent),
  hasStrong: !!findTag(afterSecondGrowth, 'STRONG'),
  strongText: findTag(afterSecondGrowth, 'STRONG') ? findTag(afterSecondGrowth, 'STRONG').textContent : null,
  literalStars: afterSecondGrowth.textContent.includes('**'),
  pendingAtAdopt,
  settledSameRow: settled === candidate,
  afterSecondText: afterSecondGrowth.querySelector('.msg-body').textContent,
  afterEndText: settledBody.textContent,
  cursorAfterAdopt: visible.getAttribute('data-stream-fade-text'),
  cursorAfterSecond: afterSecondGrowth.getAttribute('data-stream-fade-text'),
  oldRowDetached: existing.parentNode === null,
  adoptedRowParented: visible.parentNode === host,
  helloCount: (settledBody.textContent.match(/Hello/g) || []).length,
  worldCount: (settledBody.textContent.match(/world/g) || []).length,
  moreCount: (settledBody.textContent.match(/more/g) || []).length,
}));
"""
        % (json.dumps(prefix_source), json.dumps(first_growth), json.dumps(second_growth))
    )
    data = _run_node_module(script)

    assert data["adoptedParserRow"] is True
    assert data["firstGrowthSameRow"] is True
    assert data["secondGrowthSameRow"] is True
    assert data["sameParserBody"] is True
    assert data["afterAdoptNewTexts"] == []
    assert data["firstNewText"] == "world"
    assert data["firstNewWasAnimated"] is True
    assert data["firstNewStillConnected"] is True
    assert data["secondNewSameIdentity"] is True
    assert data["secondNewIsNew"] is True
    assert data["afterSecondNewTexts"] == ["world", "more"]
    assert data["afterCleanupNewTexts"] == ["more"]
    assert data["hasStrong"] is True
    assert data["strongText"] == "there"
    assert data["literalStars"] is False
    assert data["settledSameRow"] is True
    assert data["afterSecondText"] == second_growth
    assert data["afterEndText"] == second_growth
    assert data["cursorAfterAdopt"] is None
    assert data["cursorAfterSecond"] is None
    assert data["oldRowDetached"] is True
    assert data["adoptedRowParented"] is True
    assert data["helloCount"] == 1
    assert data["worldCount"] == 1
    assert data["moreCount"] == 1
