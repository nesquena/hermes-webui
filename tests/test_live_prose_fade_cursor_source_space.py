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
function extractFunc(name){
  const marker = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}') depth -= 1;
    i += 1;
  }
  return src.slice(start, i);
}
const BLOCK_TAGS = new Set(['P','DIV','H1','H2','H3','H4','H5','H6','BLOCKQUOTE','LI','UL','OL','PRE','TABLE']);
class TextNode {
  constructor(t){ this.nodeType = 3; this.tagName = '#text'; this._t = String(t); this.parentNode = null; this.childNodes = []; }
  get textContent(){ return this._t; }
  set textContent(v){ this._t = String(v); }
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
  setAttribute(name, value){ this.attributes[String(name)] = String(value ?? ''); }
  getAttribute(name){ const k = String(name); return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(name){ delete this.attributes[String(name)]; }
  getAttributeNames(){ return Object.keys(this.attributes); }
  addEventListener(){}
  querySelector(sel){
    const want = String(sel).split(',')[0].trim().replace(/^\./, '');
    const walk = (node)=>{
      for(const c of node.childNodes){
        if(c.nodeType !== 1) continue;
        if(c._classes.has(want)) return c;
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
