"""#7778 round-7: the COUPLED session A -> B -> A regression.

The maintainer's 2026-11-17 / 2026-11-19 status replies (review
5575243351) keep one item open:

    Commit the coupled changed-grammar session refusal and actual render
    rebuild/highlight regression; retain observed same-grammar restoration
    and cache-before-highlight controls.

The earlier rounds split that ask across files, and each split left a hole
the re-gate could see through:

  * ``tests/test_issue7778_grammar_gate_behaviour.py`` drives the A -> B -> A
    sequence, but its "rebuild" is manufactured — the driver sets
    ``out.rebuildRan = true`` itself and builds the expected token spans with
    ``.replace(...)`` literals, so no production rebuild or highlight code
    runs and breaking them changes nothing the test observes;
  * ``tests/test_issue7778_real_render_path.py`` drives the REAL
    ``renderMessages()``, but only through the same-grammar fast path — it
    never advances the grammar, so the refusal and the rebuild that follows
    it are not part of that driver;
  * ``tests/test_issue7778_rebuild_tail_cache_order.py`` drives the real
    rebuild-tail region for the cache-before-highlight order, but not the
    snapshot write / refusal that decides whether that region runs at all.

This module closes the gap with ONE coupled driver that executes the real
production code end to end, in the reviewer's order:

  1. **Real snapshot writer.** The REAL ``renderMessages()`` renders session
     A under the small Prism grammar (core + markup). The REAL snapshot
     producer block (``if(_html&&_html.length<300_000){...}``, sliced with
     its control flow) publishes A's entry, and the observed entry carries
     ``prismSig``. The fence is tokenized by the REAL ``highlightCode()``
     with real Prism — tag tokens only, no keyword spans yet.
  2. **Changed grammar.** Session B loads the javascript and css grammars;
     ``_prismGrammarSignature()`` moves.
  3. **Changed-grammar refusal + actual rebuild/highlight.** Return to A with
     message count, window key and render signature unchanged. The REAL
     fast-path gate refuses the stale snapshot, so the REAL rebuild path
     runs: the transcript is re-rendered, the REAL rebuild tail applies the
     (now-stale) per-block cache entry — skipped, because its signature is
     older — and the REAL sync ``highlightCode()`` re-tokenizes the fence
     with real Prism. The observed DOM carries keyword, selector and property
     spans, the block is stamped, and the producer re-published the entry
     under the advanced grammar. Refusal is observed on STATE (the producer
     rewrote the entry; the stale snapshot's marker never reached the DOM),
     never on a source grep.
  4. **Retained control — observed same-grammar restoration.** A snapshot
     published under the CURRENT grammar state is restored by the REAL fast
     path: the published HTML (with its own marker) lands in the transcript
     DOM, the entry object is NOT rewritten (the fast path returned before
     the producer), and the restored fence is re-highlighted synchronously.
  5. **Retained control — cache-before-highlight.** A warm per-block cache
     entry under the current grammar is applied by the REAL rebuild tail
     BEFORE the sync highlight: the block's final HTML is the cached HTML
     (marker included) and ``Prism.highlightElement`` was never called on it.

Only scaffolding that is orthogonal to this PR is stubbed: the markdown
renderer seam (``_getCachedRender``) emits one HTML fence, and row/turn
plumbing (timestamps, action buttons, compaction cards, worklog/tool
rebuild) is inert for a single settled assistant message. Everything in
#7778's scope — the fast-path gate, the snapshot producer, the rebuild tail
order, the per-block cache, ``highlightCode`` and the grammar signature —
is production code sliced or extracted verbatim out of ``static/ui.js``.

Mutation matrix this module is built to fail (verified on a scratch copy):

  * remove ``&&cached.prismSig===prismSigNow`` from the gate -> the stale
    snapshot is restored: the marker appears and the entry is not rewritten;
  * delete the producer's ``prismSig`` write -> the published entry has no
    prismSig, so the unchanged-grammar control can never restore;
  * replace ``highlightCode()`` with a no-op -> no tokens anywhere;
  * make ``renderMessages()`` return immediately -> nothing is published;
  * remove or reorder the rebuild tail's cache pass -> the warm-block
    control loses its cached HTML to a second Prism pass.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
FIXTURES = Path(__file__).parent / "fixtures" / "prism"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not UI_JS_PATH.exists() or not FIXTURES.exists(),
    reason="node, static/ui.js or the Prism fixtures are unavailable",
)

# The fence under test: an HTML fence whose body embeds a script and a style
# block, so the javascript and css grammars are the ones that matter. The
# source text (not the escaped markup) is what Prism tokenizes.
_RAW_FENCE = (
    "<script>const a = 1;</script>\n"
    "<style>.b { color: red; }</style>"
)

# Distinct markers so each observation is attributable to one fixture:
#   STALE  - rides the snapshot published under the OLD grammar; if the gate
#            ever accepts it, this marker shows up in the transcript DOM.
#   FRESH  - rides the snapshot published under the CURRENT grammar; its
#            presence proves the fast path restored instead of rebuilding.
#   CACHE  - rides the per-block cache entry; its presence proves the cache
#            pass ran before the sync highlight (and that no second Prism
#            pass overwrote it).
_STALE_MARKER = "7778-stale-snapshot"
_FRESH_MARKER = "7778-fresh-snapshot"
_CACHE_MARKER = "cache-marker-7778"

_DRIVER_SRC = r"""
// Coupled A -> B -> A regression for #7778 (maintainer re-gate 2026-11-17/19).
//
// Drives the REAL renderMessages() end to end. Every field in the output is
// either a measured DOM state, a measured cache/snapshot state, or a measured
// grammar signature - never a restatement of a production expression and
// never a driver-set "rebuildRan" flag.
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
const prismDir = process.argv[3];
const RAW_FENCE = process.argv[4];

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = uiSrc.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = uiSrc.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < uiSrc.length) {
    if (uiSrc[i] === '{') depth++;
    else if (uiSrc[i] === '}') depth--;
    i++;
  }
  return uiSrc.slice(start, i);
}
// Slices a whole production BLOCK (with its control flow) out of ui.js.
function sliceBlock(marker) {
  const s = uiSrc.indexOf(marker);
  if (s < 0) throw new Error('block not found: ' + marker);
  let i = uiSrc.indexOf('{', s), depth = 1; i++;
  while (depth > 0 && i < uiSrc.length) {
    if (uiSrc[i] === '{') depth++;
    else if (uiSrc[i] === '}') depth--;
    i++;
  }
  return uiSrc.slice(s, i);
}
// Slices one top-level `const NAME=...;` declaration line out of ui.js.
function sliceDecl(keyword) {
  const idx = uiSrc.indexOf(keyword);
  if (idx < 0) throw new Error('declaration not found: ' + keyword);
  const end = uiSrc.indexOf('\n', idx);
  if (end < 0) throw new Error('declaration end not found: ' + keyword);
  return uiSrc.slice(idx, end);
}

// ── Real Prism, evaluated BEFORE any window binding (TDZ) ──────────────────
// NOTE: `window` stays a property of globalThis for the whole driver. A
// lexical `const window` later in this scope would put the identifier in its
// temporal dead zone and Prism's own `typeof window` read would throw.
global.window = { _prismPending: [] };
for (const f of ['prism-core.min.js', 'prism-markup.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
global.Prism = global.Prism || global.window.Prism;

// ── Instrument highlightElement so a second Prism pass is observable ───────
const prismCalls = [];
const realHighlightElement = Prism.highlightElement;
Prism.highlightElement = function (element, async, callback) {
  prismCalls.push(element);
  return realHighlightElement.call(this, element, async, callback);
};

// ── Minimal DOM: parses/serializes the shapes the renderer and Prism write ─
const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link']);
function decodeEntities(s) {
  return String(s).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}
function encodeEntities(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function makeTextNode(text) {
  return { nodeName: '#text', _text: String(text), get textContent() { return this._text; } };
}
function serializeNode(node) {
  if (node.nodeName === '#text') return encodeEntities(node._text);
  const tag = String(node.nodeName).toLowerCase();
  let attrs = '';
  if (node._className) attrs += ' class="' + node._className + '"';
  for (const k of Object.keys(node._attrs)) attrs += ' ' + k + '="' + encodeEntities(node._attrs[k]) + '"';
  if (VOID_TAGS.has(tag)) return '<' + tag + attrs + '>';
  return '<' + tag + attrs + '>' + node._children.map(serializeNode).join('') + '</' + tag + '>';
}
function appendText(parent, raw) { if (raw) parent.appendChild(makeTextNode(decodeEntities(raw))); }
function parseElement(html, start, parent) {
  const gt = html.indexOf('>', start);
  if (gt < 0) return html.length;
  let tagText = html.slice(start + 1, gt);
  let selfClose = false;
  if (tagText.endsWith('/')) { selfClose = true; tagText = tagText.slice(0, -1); }
  const nm = /^([a-zA-Z][a-zA-Z0-9-]*)/.exec(tagText);
  if (!nm) { appendText(parent, html.slice(start, gt + 1)); return gt + 1; }
  const el = makeEl(nm[1]);
  const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"/g;
  let am;
  while ((am = attrRe.exec(tagText))) {
    if (am[1] === 'class') el._className = decodeEntities(am[2]);
    else el._attrs[am[1]] = decodeEntities(am[2]);
  }
  parent.appendChild(el);
  const name = nm[1].toLowerCase();
  if (selfClose || VOID_TAGS.has(name)) return gt + 1;
  let i = gt + 1;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { appendText(el, html.slice(i)); return html.length; }
    if (lt > i) appendText(el, html.slice(i, lt));
    if (html.startsWith('</', lt)) {
      const closeGt = html.indexOf('>', lt);
      return closeGt < 0 ? html.length : closeGt + 1;
    }
    if (html.startsWith('<!--', lt)) { const e = html.indexOf('-->', lt); i = e < 0 ? html.length : e + 3; continue; }
    i = parseElement(html, lt, el);
  }
  return html.length;
}
function parseInto(html, parent) {
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { appendText(parent, html.slice(i)); return; }
    if (lt > i) appendText(parent, html.slice(i, lt));
    if (html.startsWith('<!--', lt)) { const e = html.indexOf('-->', lt); i = e < 0 ? html.length : e + 3; continue; }
    i = parseElement(html, lt, parent);
  }
}
function parseCompound(s) {
  const c = { tag: null, classes: [], attrs: [], nots: [], id: null };
  const re = /([a-zA-Z][a-zA-Z0-9-]*)|\.([a-zA-Z0-9_-]+)|#([A-Za-z][\w-]*)|\[([\w-]+)(?:="([^"]*)")?\]|:not\(\[([\w-]+)(?:="([^"]*)")?\]\)/g;
  let m;
  while ((m = re.exec(s))) {
    if (m[1]) c.tag = m[1].toUpperCase();
    else if (m[2]) c.classes.push(m[2]);
    else if (m[3]) c.id = m[3];
    else if (m[4]) c.attrs.push({ name: m[4], value: m[5] });
    else if (m[6]) c.nots.push({ name: m[6], value: m[7] });
  }
  return c;
}
function matchesCompound(el, c) {
  if (!el || el.nodeName === '#text') return false;
  if (c.tag && el.nodeName !== c.tag) return false;
  const classes = el._className ? el._className.split(/\s+/) : [];
  for (const cls of c.classes) if (!classes.includes(cls)) return false;
  for (const a of c.attrs) {
    if (!el.hasAttribute(a.name)) return false;
    if (a.value !== undefined && el.getAttribute(a.name) !== a.value) return false;
  }
  for (const a of c.nots) {
    if (el.hasAttribute(a.name) && (a.value === undefined || el.getAttribute(a.name) === a.value)) return false;
  }
  if (c.id && el.getAttribute('id') !== c.id) return false;
  return true;
}
function matchesSelector(el, sel) {
  const compounds = String(sel).trim().split(/\s+/).map(parseCompound);
  if (!compounds.length) return false;
  if (!matchesCompound(el, compounds[compounds.length - 1])) return false;
  let i = compounds.length - 2;
  let cur = el.parentElement;
  while (i >= 0) {
    if (!cur) return false;
    if (matchesCompound(cur, compounds[i])) i--;
    cur = cur.parentElement;
  }
  return i < 0;
}
function queryAll(root, sel) {
  const out = [];
  const sels = String(sel).split(',').map(s => s.trim()).filter(Boolean);
  const walk = (el) => {
    for (const c of el._children) {
      if (c.nodeName !== '#text' && sels.some(s => matchesSelector(c, s))) out.push(c);
      if (c._children) walk(c);
    }
  };
  if (root._children) walk(root);
  return out;
}
function makeEl(tag) {
  const el = {
    nodeName: String(tag).toUpperCase(),
    tagName: String(tag).toUpperCase(),
    _className: '',
    _attrs: Object.create(null),
    _children: [],
    parentElement: null,
    style: {},
    get className() { return this._className; },
    set className(v) { this._className = String(v); },
    get children() { return this._children.filter(c => c.nodeName !== '#text'); },
    get firstElementChild() { const c = this.children; return c.length ? c[0] : null; },
    get textContent() { return this._children.map(c => (c.nodeName === '#text' ? c._text : c.textContent)).join(''); },
    set textContent(v) { this._children = [makeTextNode(v)]; },
    get innerHTML() { return this._children.map(serializeNode).join(''); },
    set innerHTML(v) { this._children = []; parseInto(String(v), this); },
    get dataset() { return this._dataset || (this._dataset = makeDataset(this)); },
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n); },
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null; },
    setAttribute(n, v) { this._attrs[n] = String(v); },
    removeAttribute(n) { delete this._attrs[n]; },
    appendChild(c) { c.parentElement = this; this._children.push(c); return c; },
    insertBefore(c, ref) {
      const i = this._children.indexOf(ref);
      if (i < 0) this._children.push(c); else this._children.splice(i, 0, c);
      c.parentElement = this; return c;
    },
    insertAdjacentHTML(pos, html) { if (String(pos).toLowerCase() === 'beforeend') parseInto(String(html), this); },
    remove() {
      if (this.parentElement) {
        const i = this.parentElement._children.indexOf(this);
        if (i >= 0) this.parentElement._children.splice(i, 1);
        this.parentElement = null;
      }
    },
    closest(sel) {
      let cur = this;
      while (cur) { if (cur.nodeName !== '#text' && matchesSelector(cur, sel)) return cur; cur = cur.parentElement; }
      return null;
    },
    querySelector(sel) { const all = this.querySelectorAll(sel); return all.length ? all[0] : null; },
    querySelectorAll(sel) { return queryAll(this, sel); },
    focus() { this._focused = true; },
    addEventListener() {},
  };
  el.classList = {
    add(c) { const parts = el._className ? el._className.split(/\s+/) : []; if (!parts.includes(c)) { parts.push(c); el._className = parts.join(' '); } },
    remove(c) { el._className = (el._className ? el._className.split(/\s+/) : []).filter(x => x && x !== c).join(' '); },
    contains(c) { return (el._className ? el._className.split(/\s+/) : []).includes(c); },
    toggle(c) { if (this.contains(c)) this.remove(c); else this.add(c); },
  };
  return el;
}
function makeDataset(el) {
  const toAttr = (k) => 'data-' + String(k).replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
  return new Proxy({}, {
    get(t, p) { return typeof p === 'string' ? el._attrs[toAttr(p)] : undefined; },
    set(t, p, v) { if (typeof p === 'string') el._attrs[toAttr(p)] = String(v); return true; },
    has(t, p) { return typeof p === 'string' && Object.prototype.hasOwnProperty.call(el._attrs, toAttr(p)); },
    deleteProperty(t, p) { if (typeof p === 'string') delete el._attrs[toAttr(p)]; return true; },
  });
}
global.document = {
  createElement(tag) { return makeEl(tag); },
  getElementById() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
};

// ── Module state the real renderMessages() reads ───────────────────────────
// (`window` stays on globalThis - see the TDZ note above.)
let _lastMessageRenderAt = 0;
let _messageUserUnpinned = false;
let _sessionHtmlCacheSid = null;
let _messageVirtualWindowKey = 'stale';
let _messageRenderWindowSid = 'sid-Z';
const _loadingSessionId = null;
const performance = { now: () => 1000 };
const INFLIGHT = {};
const S = {
  session: { session_id: 'sid-A' },
  messages: [{ role: 'assistant', content: RAW_FENCE }],
  busy: false,
  toolCalls: [],
};
const _sessionHtmlCache = new Map();
const _codeHighlightCache = new Map();
let _settleKeepOpenCacheStash = null;
const _recycleStash = new Map();
let _msgNodeRecycleEnabled = false;
let _prismCompleteHookRegistered = false;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
// The cache constants are SLICED out of ui.js, not restated here, so a
// production change to the token regex or the entry cap follows the driver.
// (`const` declarations inside eval() do not leak to the enclosing scope,
// so the slice is re-declared as `var` — same value, same source.)
eval('var ' + sliceDecl('const _CODE_HIGHLIGHT_TOKEN_RE').replace(/^const\s+/, ''));
eval('var ' + sliceDecl('const _CODE_HIGHLIGHT_CACHE_MAX').replace(/^const\s+/, ''));
eval('var ' + sliceDecl('const _ERR_MSG_RE').replace(/^const\s+/, ''));

const inner = makeEl('DIV');
const container = makeEl('DIV');
container.scrollTop = 0; container.clientHeight = 600; container.scrollHeight = 20000;
container.getBoundingClientRect = () => ({ top: 0, bottom: 600 });
function $(id) {
  if (id === 'msgInner') return inner;
  if (id === 'emptyState') return { style: {} };
  if (id === 'messages') return container;
  return null;
}

// ── Stubs: render-path scaffolding orthogonal to #7778 ─────────────────────
function _resetMessageVirtualMeasurementBurst() {}
function _resetMessageRenderWindow() {}
function _latestPreservedCompressionTaskListMessages() { return []; }
function _compressionStateForCurrentSession() { return null; }
function _clearCompressionElapsedTimer() {}
function _setCompressionSessionLock() {}
function _captureWorklogDetailDisclosureState() { return null; }
function _handoffStateForCurrentSession() { return null; }
function _getVisibleMessagesWithIdx() { return [{ rawIdx: 0, m: S.messages[0] }]; }
function _currentMessageVirtualWindow() {
  return { virtualized: false, start: 0, end: 1, topPad: 0, bottomPad: 0, total: 1, tailStart: 1 };
}
function _messageVirtualKeepTailCount() { return 0; }
function _messageVirtualWindowKeyFor() { return 'w1'; }
function _messageRenderCacheSignature() { return 'sig-1'; }
function _rehydrateTransparentStreamDom() {}
function _rehydrateDeferredWorklogsFromCache() {}
function _wireMessageWindowLoadEarlierButton() {}
function _updateMessageVirtualMeasurements() {}
function _captureMessageRecoveryOwnership() { return { programmatic: false, jumpOwner: false }; }
function _scrollAfterMessageRender() {}
function _maybeRecoverVirtualizedBlankViewport() { return false; }
function _postProcessWithAnchorSuppression() {}
function requestAnimationFrame() {}
function addCopyButtons() {}
function initTreeViews() {}
function isTpsDisplayEnabled() { return false; }
function t(k) { return k; }
function li() { return ''; }
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function _rawTurnBoundaryByRawIdx() { return new Map(); }
function _assistantTurnFinalVisibleContentMap() { return new Map(); }
function _assistantTurnVisibleContentMap() { return new Map(); }
function _latestCompressionReferenceMessage() { return { message: null, rawIdx: -1 }; }
function msgContent() { return ''; }
function _compactionSummarySegment() { return null; }
function _loadedCompactionMarkerRawIdxs() { return []; }
function _shouldShowSettledCompressionReference() { return false; }
function _selectCompactionCardPlacements() { return { preWindowMarkers: [], inlineMarkers: [], taskOwner: null }; }
function _isPreservedCompressionTaskListMessage() { return false; }
function _compressionAnchorIndex() { return null; }
function _transparentStreamOrderedParts() { return null; }
function _assistantTurnAnchorSettledFinalAnswer() { return null; }
function _isMarkerOnlyAssistantCompressionMessage() { return false; }
function _stripAttachedFilesMarkerForDisplay(v) { return v; }
function _stripWorkspaceDisplayPrefix(v) { return v; }
function _isAssistantEmptyPlaceholderContent() { return false; }
function isCompactWorklogMode() { return false; }
function isTransparentStream() { return false; }
function isSimplifiedToolCalling() { return false; }
function _assistantReasoningPayloadText() { return ''; }
function _isContextCompactionMessage() { return false; }
function _legacySettledFallbackHasToolMetadata() { return false; }
function _collectHandoffSummaryStates() { return []; }
function _messageSessionIndexForRawIdx() { return 0; }
function _formatMessageFooterTimestamp() { return ''; }
function _messageVirtualSpacer() { return makeEl('DIV'); }
function _assistantRoleHtml() { return ''; }
// The markdown-renderer seam: what the smd renderer emits for one html fence
// (escaped source inside <pre><code>). Everything AFTER this seam - the
// rebuild tail, the cache, highlightCode, the producer - is production code.
function _getCachedRender(text, isUser) {
  if (isUser) return '<p>' + escHtml(text) + '</p>';
  return '<pre class="language-html"><code class="language-html">' + escHtml(RAW_FENCE) + '</code></pre>';
}

// ── The REAL production helpers and the REAL renderer ──────────────────────
eval(extractFunc('_codeHighlightCacheKey'));
eval(extractFunc('_writeCodeHighlightCache'));
eval(extractFunc('_applyCachedCodeHighlights'));
eval(extractFunc('_prismGrammarSignature'));
eval(extractFunc('_maybeRegisterPrismCompleteHook'));
eval(extractFunc('highlightCode'));
eval(extractFunc('_createAssistantTurn'));
eval(extractFunc('_assistantTurnBlocks'));
eval(extractFunc('_setLatestAssistantTurnLandmark'));
eval(extractFunc('_insertSegmentBlock'));
eval(extractFunc('_messageViewportAnchorKeyForMessage'));
eval(extractFunc('_questionJumpButtonHtml'));
eval(extractFunc('_insertCompactionCardNodes'));
eval(extractFunc('_insertPreservedCompressionTaskFallback'));
eval(extractFunc('renderCompressionUi'));
eval(extractFunc('_restoreWorklogDetailDisclosureState'));
eval(extractFunc('_formatGatewayModelLabel'));
eval(extractFunc('_usedModelTurnChipLabel'));
eval(extractFunc('_gatewayRoutingFailoverText'));
eval(extractFunc('_gatewayModelWarningText'));
eval(extractFunc('_formatTurnDuration'));
eval(extractFunc('renderMessages'));

// ── The REAL snapshot producer, sliced as a whole BLOCK ────────────────────
const PRODUCER_BLOCK = sliceBlock('if(_html&&_html.length<300_000){');
eval(
  'function produceSnapshot(sid,_html,msgCount,renderWindowKey,cachedRenderSignature){'
  + PRODUCER_BLOCK + '\n}'
);

// ── The REAL fast-path condition, lifted verbatim out of renderMessages ────
const FAST_PATH_SRC = uiSrc.slice(
  uiSrc.indexOf('if(cached&&cached.msgCount===msgCount'),
  uiSrc.indexOf('inner.innerHTML=cached.html;')
);
const fastPathAccepts = new Function(
  'cached', 'msgCount', 'renderWindowKey', 'renderSignature', 'prismSigNow',
  'return ' + FAST_PATH_SRC.replace(/^if\(/, '(').replace(/\)\s*\{\s*$/, ')') + ';'
);
// The same condition with ONLY the grammar clause removed - the negative
// control that proves the observed refusal is attributable to that clause.
const GATE_NO_GRAMMAR_SRC = FAST_PATH_SRC.replace('&&cached.prismSig===prismSigNow', '');
const fastPathAcceptsNoGrammar = new Function(
  'cached', 'msgCount', 'renderWindowKey', 'renderSignature', 'prismSigNow',
  'return ' + GATE_NO_GRAMMAR_SRC.replace(/^if\(/, '(').replace(/\)\s*\{\s*$/, ')') + ';'
);

// ── Measurement helpers ────────────────────────────────────────────────────
function codeBlock() { const all = inner.querySelectorAll('pre code'); return all.length ? all[0] : null; }
function tokenStats(html) {
  return {
    tokens: (html.match(/<span\b[^>]*\bclass="[^"]*\btoken\b/g) || []).length,
    keyword: (html.match(/<span\b[^>]*\bclass="[^"]*\btoken\s+keyword\b/g) || []).length,
    selector: (html.match(/<span\b[^>]*\bclass="[^"]*\btoken\s+selector\b/g) || []).length,
    property: (html.match(/<span\b[^>]*\bclass="[^"]*\btoken\s+property\b/g) || []).length,
  };
}
function blockReport(block) {
  if (!block) return null;
  const pre = block.parentElement;
  return {
    found: true,
    marked: block.dataset.highlighted === '1',
    stats: tokenStats(block.innerHTML),
    preTabindex: pre && pre.hasAttribute('tabindex') ? pre.getAttribute('tabindex') : null,
    codeClass: String(block.className),
    cacheSig: (() => {
      const e = _codeHighlightCache.get(_codeHighlightCacheKey(block));
      return e ? e.sig : null;
    })(),
  };
}
const out = {};

// ── Phase 1: session A renders under the SMALL grammar ─────────────────────
S.session.session_id = 'sid-A';
_sessionHtmlCacheSid = 'sid-Z';
renderMessages({});
const sigSmall = _prismGrammarSignature();
const published1 = _sessionHtmlCache.get('sid-A');
out.phase1 = {
  sigSmall,
  published: !!published1,
  publishedKeys: published1 ? Object.keys(published1).sort().join(',') : '',
  publishedPrismSig: published1 ? published1.prismSig : null,
  publishedHtmlLength: published1 ? published1.html.length : 0,
  block: blockReport(codeBlock()),
};
const phase1Html = published1 ? published1.html : '';

// Phase 1b: re-publish A's snapshot through the REAL producer block with the
// stale marker appended. The producer is real; the marker only makes the
// restore observable in the DOM (a marked old snapshot is the deliberate
// compatibility fixture the 2026-10-12 review allows).
produceSnapshot('sid-A', phase1Html + '<div id="7778-stale-snapshot"></div>', S.messages.length, 'w1', null);
out.staleMarkerPublished = _sessionHtmlCache.get('sid-A').html.indexOf('7778-stale-snapshot') >= 0;

// ── Phase 2: visit B, which loads the javascript and css grammars ──────────
for (const f of ['prism-clike.min.js', 'prism-javascript.min.js', 'prism-css.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
const sigLarge = _prismGrammarSignature();
out.sigLarge = sigLarge;
out.signatureMovedOn = sigLarge !== sigSmall;

// ── Phase 3: return to A, count/window/signature unchanged ─────────────────
const entryBefore3 = _sessionHtmlCache.get('sid-A');
_sessionHtmlCacheSid = 'sid-B';
renderMessages({});
const entryAfter3 = _sessionHtmlCache.get('sid-A');
out.phase3 = {
  // The fast path returns BEFORE the producer, so a rewritten entry is
  // direct evidence the stale snapshot was refused and the rebuild ran.
  entryRewritten: entryAfter3 !== entryBefore3,
  entryPrismSig: entryAfter3 ? entryAfter3.prismSig : null,
  staleMarkerPresent: inner.innerHTML.indexOf('7778-stale-snapshot') >= 0,
  block: blockReport(codeBlock()),
};
out.attribution = {
  gateRefuses: !fastPathAccepts(entryBefore3, entryBefore3.msgCount, entryBefore3.renderWindowKey, entryBefore3.signature, sigLarge),
  gateWithoutGrammarClauseAccepts: fastPathAcceptsNoGrammar(entryBefore3, entryBefore3.msgCount, entryBefore3.renderWindowKey, entryBefore3.signature, sigLarge),
};

// ── Control 1: an UNCHANGED grammar still takes the fast path ──────────────
produceSnapshot('sid-A', phase1Html + '<div id="7778-fresh-snapshot"></div>', S.messages.length, 'w1', null);
const entryBeforeC1 = _sessionHtmlCache.get('sid-A');
_sessionHtmlCacheSid = 'sid-B';
renderMessages({});
out.control1 = {
  freshMarkerPresent: inner.innerHTML.indexOf('7778-fresh-snapshot') >= 0,
  entryUnchanged: _sessionHtmlCache.get('sid-A') === entryBeforeC1,
  block: blockReport(codeBlock()),
};

// ── Control 2: a warm block is restored from cache BEFORE the sync highlight ─
const warmBlock = codeBlock();
_codeHighlightCache.set(_codeHighlightCacheKey(warmBlock), {
  html: (warmBlock ? warmBlock.innerHTML : '') + '<span class="token cache-marker-7778"></span>',
  sig: sigLarge,
});
_sessionHtmlCache.delete('sid-A');
_sessionHtmlCacheSid = 'sid-B';
prismCalls.length = 0;
renderMessages({});
const finalBlock = codeBlock();
out.control2 = {
  blockFound: !!finalBlock,
  cacheMarkerPresent: finalBlock ? finalBlock.innerHTML.indexOf('cache-marker-7778') >= 0 : false,
  prismCallsOnBlock: prismCalls.filter(c => c === finalBlock).length,
  block: blockReport(finalBlock),
};

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("code_paint_7778_coupled") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return p


def _run(driver_path) -> dict:
    result = subprocess.run(
        [NODE, str(driver_path), str(UI_JS_PATH), str(FIXTURES), _RAW_FENCE],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_the_real_render_publishes_the_snapshot_under_the_small_grammar(driver_path):
    """Phase 1: the real renderer runs, the real producer publishes, and the
    fence is tokenized by the real highlightCode with real Prism."""
    out = _run(driver_path)
    p1 = out["phase1"]
    assert p1["published"] is True, (
        "the real renderMessages() published no session snapshot"
    )
    assert p1["publishedKeys"] == "html,msgCount,prismSig,renderWindowKey,signature", (
        f"published entry keys are {p1['publishedKeys']!r} — the producer must "
        "record the Prism grammar signature alongside the snapshot"
    )
    assert p1["publishedPrismSig"] == p1["sigSmall"], (
        "the published snapshot's prismSig does not equal the grammar "
        f"signature at publish time ({p1['publishedPrismSig']!r} != {p1['sigSmall']!r})"
    )
    assert p1["publishedHtmlLength"] > 0, "the published snapshot HTML is empty"
    block = p1["block"]
    assert block and block["found"] is True, "the rendered transcript has no code block"
    assert block["stats"]["tokens"] > 0, (
        "the fence was not tokenized under the html grammar — highlightCode "
        "or Prism did not run on the rendered block"
    )
    assert block["stats"]["keyword"] == 0, (
        "the fence already carries keyword tokens under the small grammar — "
        "the javascript grammar must not be loaded yet, or the test is not "
        "discriminating"
    )
    assert block["marked"] is True, "the rendered block is not stamped data-highlighted='1'"
    assert block["preTabindex"] == "0", (
        "real Prism.highlightElement sets tabindex=\"0\" on the <pre>; the "
        "rendered block's pre is missing it"
    )
    assert block["cacheSig"] == p1["sigSmall"], (
        "the per-block cache entry was not written under the small grammar "
        f"signature (sig={block['cacheSig']!r})"
    )


def test_returning_after_the_grammar_advanced_refuses_and_rebuilds_with_real_tokens(driver_path):
    """Phase 3: the changed-grammar refusal, the actual rebuild, and the
    resulting DOM tokens — all observed, none manufactured."""
    out = _run(driver_path)
    assert out["signatureMovedOn"] is True, (
        f"loading the javascript/css grammars did not move the signature "
        f"({out['phase1']['sigSmall']} -> {out['sigLarge']}); the gate is vacuous"
    )
    assert out["staleMarkerPublished"] is True, (
        "the stale-marker fixture did not land in the published snapshot"
    )
    p3 = out["phase3"]
    assert p3["entryRewritten"] is True, (
        "the session entry was NOT rewritten by the return render — the fast "
        "path returned early, so the stale snapshot was restored instead of "
        "refused and rebuilt"
    )
    assert p3["entryPrismSig"] == out["sigLarge"], (
        "the rebuild did not re-publish the snapshot under the advanced "
        f"grammar (prismSig={p3['entryPrismSig']!r}, expected {out['sigLarge']!r})"
    )
    assert p3["staleMarkerPresent"] is False, (
        "the stale snapshot's marker reached the transcript DOM — the "
        "changed-grammar snapshot was restored instead of refused"
    )
    block = p3["block"]
    assert block and block["found"] is True, "the rebuilt transcript has no code block"
    assert block["marked"] is True, "the rebuilt block is not stamped data-highlighted='1'"
    stats = block["stats"]
    assert stats["keyword"] > 0, (
        "the rebuilt fence carries no keyword spans — the actual rebuild / "
        "highlight path did not re-tokenize the embedded script with the "
        "javascript grammar"
    )
    assert stats["selector"] > 0, "the rebuilt fence has no selector spans"
    assert stats["property"] > 0, "the rebuilt fence has no property spans"
    assert stats["tokens"] > out["phase1"]["block"]["stats"]["tokens"], (
        f"the rebuild produced no more tokens than the stale fence "
        f"({out['phase1']['block']['stats']['tokens']} -> {stats['tokens']})"
    )
    assert block["cacheSig"] == out["sigLarge"], (
        "the per-block cache entry was not refreshed under the advanced "
        f"grammar (sig={block['cacheSig']!r})"
    )


def test_the_refusal_is_attributable_to_the_grammar_gate(driver_path):
    """Negative control on the REAL condition: with the grammar clause the
    gate refuses the stale entry; with ONLY that clause removed it accepts.

    The condition is sliced verbatim out of renderMessages, so the mutated
    variant is the shipped expression minus the comparison under test.
    """
    out = _run(driver_path)
    attribution = out["attribution"]
    assert attribution["gateRefuses"] is True, (
        "the real fast-path condition did not refuse the stale snapshot under "
        "the advanced grammar"
    )
    assert attribution["gateWithoutGrammarClauseAccepts"] is True, (
        "removing only the prismSig comparison did not change the gate's "
        "result — the refusal observation above is not attributable to the "
        "grammar gate"
    )


def test_an_unchanged_grammar_still_restores_the_published_snapshot(driver_path):
    """Control 1: the same-grammar fast path still restores (no #7752
    synchronous-flash regression), observed on the restored DOM."""
    out = _run(driver_path)
    c1 = out["control1"]
    assert c1["freshMarkerPresent"] is True, (
        "the snapshot published under the CURRENT grammar was not restored — "
        "the marker never reached the transcript DOM, so every return to a "
        "session would rebuild"
    )
    assert c1["entryUnchanged"] is True, (
        "the session entry was rewritten on an unchanged-grammar return — the "
        "fast path did not return early, so the synchronous restore is gone"
    )
    block = c1["block"]
    assert block and block["found"] is True, "the restored transcript has no code block"
    assert block["stats"]["keyword"] > 0, (
        "the restored fence was not re-highlighted by the fast path's "
        "synchronous highlightCode under the advanced grammar"
    )


def test_a_warm_block_is_restored_from_cache_before_the_sync_highlight(driver_path):
    """Control 2: the per-block cache pass runs BEFORE the sync highlight, so
    a warm entry is restored without a second Prism pass."""
    out = _run(driver_path)
    c2 = out["control2"]
    assert c2["blockFound"] is True, "the rebuilt transcript has no code block"
    assert c2["cacheMarkerPresent"] is True, (
        "the rebuilt block does not carry the cached HTML — the per-block "
        "cache was not applied, so the sync highlight ran first and stamped "
        "data-highlighted='1' before the cache pass could restore the block"
    )
    assert c2["prismCallsOnBlock"] == 0, (
        f"Prism.highlightElement ran on the cached block {c2['prismCallsOnBlock']} "
        "time(s) — the cache hit did not avoid a second Prism pass, which "
        "means the sync highlight ran before the cache pass"
    )
    assert c2["block"] and c2["block"]["marked"] is True, (
        "the cache-restored block is not stamped data-highlighted='1'"
    )
