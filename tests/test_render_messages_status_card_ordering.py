"""Behavior-level DOM regression tests for renderMessages() status-card ordering.

PR #6579 / issue #6577: the tool-limit (and /status) status card must render
BELOW the assistant's final prose — `msg-body`, then `status-card`, then
`msg-foot` — never card-before-body, and never with an empty body/footer pair
or duplicated tool/status rows.

These tests drive the REAL production `renderMessages()` extracted from
static/ui.js inside a FakeDOM Node harness (FakeElement + a small HTML parser
for insertAdjacentHTML) and assert on the resulting DOM tree — NOT on source
strings — covering the four behavioral cases the maintainer review requested:

1. non-empty final prose + _statusCard  -> one body and one card, body before
   card, footer last;
2. null / empty / whitespace-only content + _statusCard -> card only, no empty
   body/footer nodes;
3. array and transparent ordered-content forms -> final prose before the card,
   no duplicated tool/status rows;
4. repeated settled render -> no duplicated body/card pair.

Mutation notes (each fails on the pre-fix code):
- inserting statusHtml before the body breaks the segment-order assertions;
- inserting the body when content is empty/whitespace breaks `bodies == 0`;
- skipping the wipe (`inner.innerHTML=''`) breaks the repeated-render counts;
- duplicating the statusHtml insertion breaks the `cards == 1` counts.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _ui_js() -> str:
    return UI_JS_PATH.read_text(encoding="utf-8")


def _run_node(source: str) -> str:
    """Run a Node script from the repo root; return the last non-empty stdout line."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
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
        raise AssertionError(
            f"node behavior check failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _extract_func_script(js: str) -> str:
    """Brace-matching function extractor (skips strings/templates/regex/comments),
    embedded as JS so the harness can eval() the real production functions."""
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


def _harness_prelude() -> str:
    """FakeDOM + collaborator stubs + eval of the real production functions.

    The FakeElement implements a working `insertAdjacentHTML` (small HTML
    parser) and an `innerHTML` setter that clears children on `''`, so
    renderMessages' real wipe-and-rebuild (`inner.innerHTML=''`) is modeled and
    the resulting DOM tree can be queried for ordering assertions.

    `_legacySettledFallbackHasToolMetadata` is stubbed to false so the legacy
    settled-tool fallback rebuild (a separate mechanism, covered by
    test_anchor_fallback_ownership.py) cannot inject extra tool rows that would
    mask a duplication in the ordered render path under test.
    """
    return r"""
class FakeClassList {
  constructor(el){ this.el = el; }
  _set(){ return new Set(String(this.el.className || '').split(/\s+/).filter(Boolean)); }
  contains(name){ return this._set().has(name); }
  add(...names){ const set = this._set(); names.forEach((name) => set.add(name)); this.el.className = Array.from(set).join(' '); }
  remove(...names){ const set = this._set(); names.forEach((name) => set.delete(name)); this.el.className = Array.from(set).join(' '); }
}
function dataKey(name){
  return String(name).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}
function matchesSimple(el, selector){
  if (!selector || !el || el.nodeType === 3) return false;
  const negated = [];
  const baseSelector = selector.replace(/:not\(([^()]*)\)/g, (_, inner) => {
    negated.push(String(inner || '').trim());
    return '';
  }).trim();
  if (negated.some((inner) => inner && matchesSimple(el, inner))) return false;
  if (!baseSelector) return true;
  const classMatches = [...baseSelector.matchAll(/\.([A-Za-z0-9_-]+)/g)].map((m) => m[1]);
  if (classMatches.some((name) => !el.classList.contains(name))) return false;
  const attrMatches = [...baseSelector.matchAll(/\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\]/g)];
  for (const match of attrMatches) {
    const value = el.getAttribute(match[1]);
    if (value === null) return false;
    if (match[2] !== undefined && String(value) !== String(match[2])) return false;
  }
  const idMatch = baseSelector.match(/#([A-Za-z0-9_-]+)/);
  if (idMatch && el.id !== idMatch[1]) return false;
  const tagMatch = baseSelector.match(/^[A-Za-z][A-Za-z0-9_-]*/);
  if (tagMatch && el.tagName.toLowerCase() !== tagMatch[0].toLowerCase()) return false;
  return true;
}
function matchesSelector(el, selector){
  return String(selector || '').split(',').some((part) => matchesSimple(el, part.trim()));
}
const VOID_TAGS = new Set(['br', 'img', 'input', 'hr', 'meta', 'link']);
function parseHTML(html){
  // Minimal HTML parser sufficient for the markup renderMessages emits in the
  // status-card branches: nested <div>/<span> with class/data-*/title attrs,
  // text nodes, entities. Text is returned as {nodeType:3} nodes.
  const root = { children: [] };
  const stack = [root];
  let i = 0;
  const len = html.length;
  const textBuf = [];
  const flushText = () => {
    const text = textBuf.join('');
    textBuf.length = 0;
    if (text) stack[stack.length - 1].children.push({ nodeType: 3, textContent: text });
  };
  while (i < len) {
    const c = html[i];
    if (c === '<') {
      if (html.startsWith('<!--', i)) {
        const end = html.indexOf('-->', i + 4);
        i = end < 0 ? len : end + 3;
        continue;
      }
      const closing = html.startsWith('</', i);
      const tagStart = closing ? i + 2 : i + 1;
      const tagMatch = /^[a-zA-Z][a-zA-Z0-9-]*/.exec(html.slice(tagStart));
      if (tagMatch) {
        flushText();
        const tag = tagMatch[0].toLowerCase();
        let j = tagStart + tagMatch[0].length;
        const attrs = {};
        while (j < len) {
          while (j < len && /\s/.test(html[j])) j++;
          if (html[j] === '>' || html[j] === '/' || html[j] === undefined) break;
          const attrMatch = /^([a-zA-Z_:][a-zA-Z0-9_:.-]*)/.exec(html.slice(j));
          if (!attrMatch) { j++; continue; }
          const attrName = attrMatch[1];
          j += attrMatch[0].length;
          while (j < len && /\s/.test(html[j])) j++;
          let attrValue = '';
          if (html[j] === '=') {
            j++;
            while (j < len && /\s/.test(html[j])) j++;
            if (html[j] === '"' || html[j] === "'") {
              const quote = html[j];
              j++;
              const end = html.indexOf(quote, j);
              attrValue = end < 0 ? html.slice(j) : html.slice(j, end);
              j = end < 0 ? len : end + 1;
            } else {
              const bare = /^[^\s>]*/.exec(html.slice(j));
              attrValue = bare ? bare[0] : '';
              j += attrValue.length;
            }
          }
          attrs[attrName.toLowerCase()] = attrValue;
        }
        if (closing) {
          stack.pop();
          while (j < len && html[j] !== '>') j++;
          if (html[j] === '>') j++;
          i = j;
          continue;
        }
        const el = new FakeElement(tag);
        for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
        while (j < len && html[j] !== '>') j++;
        const selfClosing = html[j - 1] === '/' || VOID_TAGS.has(tag);
        if (html[j] === '>') j++;
        stack[stack.length - 1].children.push(el);
        if (!selfClosing) stack.push(el);
        i = j;
        continue;
      }
      textBuf.push('<');
      i++;
      continue;
    }
    if (c === '&') {
      const entMatch = /^&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);/.exec(html.slice(i));
      if (entMatch) {
        const ent = entMatch[1];
        let decoded;
        if (ent[0] === '#') {
          const num = ent[1] === 'x' || ent[1] === 'X' ? parseInt(ent.slice(2), 16) : parseInt(ent.slice(1), 10);
          decoded = String.fromCodePoint(num);
        } else {
          decoded = ({ amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' })[ent] || entMatch[0];
        }
        textBuf.push(decoded);
        i += entMatch[0].length;
        continue;
      }
    }
    textBuf.push(c);
    i++;
  }
  flushText();
  return root.children;
}
class FakeElement {
  constructor(tag = 'div'){
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.attributes = {};
    this.className = '';
    this.id = '';
    this.hidden = false;
    this._innerHtmlString = '';
    this._ownText = '';
    this.style = {};
    this.classList = new FakeClassList(this);
  }
  get textContent(){
    let out = this._ownText || '';
    for (const child of this.children) {
      out += child.nodeType === 3 ? child.textContent : (child.textContent || '');
    }
    return out;
  }
  set textContent(value){ this._ownText = String(value); }
  appendChild(child){ child.parentElement = this; this.children.push(child); return child; }
  insertBefore(child, ref){
    child.parentElement = this;
    const idx = this.children.indexOf(ref);
    if (idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }
  remove(){
    if (!this.parentElement) return;
    const idx = this.parentElement.children.indexOf(this);
    if (idx >= 0) this.parentElement.children.splice(idx, 1);
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
  closest(selector){
    let node = this;
    while (node) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
  querySelectorAll(selector){
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (child.nodeType === 3) continue;
        if (matchesSelector(child, selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  get firstElementChild(){ return this.children.find((c) => c.nodeType !== 3) || null; }
  get nextElementSibling(){
    if (!this.parentElement) return null;
    const idx = this.parentElement.children.indexOf(this);
    return idx < 0 ? null : (this.parentElement.children[idx + 1] || null);
  }
  insertAdjacentHTML(position, html){
    const nodes = parseHTML(html);
    if (position === 'beforeend') { for (const n of nodes) this.appendChild(n); return; }
    if (position === 'afterbegin') { for (let k = nodes.length - 1; k >= 0; k--) this.insertBefore(nodes[k], this.children[0] || null); return; }
    if (position === 'beforebegin' && this.parentElement) { for (const n of nodes) this.parentElement.insertBefore(n, this); return; }
    if (position === 'afterend' && this.parentElement) {
      for (const n of nodes) {
        const idx = this.parentElement.children.indexOf(this);
        this.parentElement.insertBefore(n, this.parentElement.children[idx + 1] || null);
      }
      return;
    }
    for (const n of nodes) this.appendChild(n);
  }
  get innerHTML(){ return this._innerHtmlString; }
  set innerHTML(value){
    this._innerHtmlString = String(value);
    // Model the browser wipe: `inner.innerHTML=''` detaches all children.
    if (String(value) === '') { this.children = []; this._ownText = ''; }
  }
}
const elements = {
  msgInner: new FakeElement('div'),
  emptyState: new FakeElement('div'),
};
global.window = {};
global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => elements[id] || null,
};
global.performance = { now: () => 1 };
global.requestAnimationFrame = (fn) => fn();
global.setTimeout = (fn) => fn();
function $(id){ return elements[id] || null; }

// --- controllable mode flags ---
let transparentStream = false;
let compactWorklog = false;
function isTransparentStream(){ return transparentStream; }
function isCompactWorklogMode(){ return compactWorklog; }
function isSimplifiedToolCalling(){ return true; }
function t(key){ return key; }
function li(){ return ''; }
function esc(value){ return String(value == null ? '' : value); }

// --- renderMessages collaborators (stubs, same set as the sibling
// test_anchor_fallback_ownership.py harness) ---
let S;
const INFLIGHT = {};
let _loadingSessionId = null;
let _messageRenderWindowSid = null;
let _messageUserUnpinned = false;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _sessionHtmlCacheSid = null;
let _messagesTruncated = false;
let _oldestIdx = 0;
const _sessionHtmlCache = new Map();
const _recycleStash = new Map();
const _transparentTurnCollapsedStates = {};
const _msgNodeRecycleEnabled = false;
const _recycleResetAttrs = [];
const _ERR_MSG_RE = /__never__/;
function _captureMessageScrollSnapshot(){ return null; }
function _resetMessageRenderWindow(sid){ _messageRenderWindowSid = sid; }
function _latestPreservedCompressionTaskListMessages(){ return []; }
function _getVisibleMessagesWithIdx(){ return S.messages.map((m, rawIdx) => (m && m.role !== 'tool') ? { m, rawIdx } : null).filter(Boolean); }
function _messageVirtualKeepTailCount(){ return 100; }
function _currentMessageVirtualWindow(vis){ return { virtualized: false, start: 0, end: vis.length, topPad: 0, bottomPad: 0, total: vis.length, tailStart: vis.length }; }
function _messageVirtualWindowKeyFor(){ return 'all'; }
function _messageRenderCacheSignature(){ return 'sig'; }
function _compressionStateForCurrentSession(){ return null; }
function clearCompressionUi(){}
function _handoffStateForCurrentSession(){ return null; }
function _captureWorklogDetailDisclosureState(){ return null; }
function _latestCompressionReferenceMessage(){ return { message: null, rawIdx: -1 }; }
function _shouldShowSettledCompressionReference(){ return false; }
function _applySessionNavigationPrefs(){}
function _messageVirtualSpacer(){ return new FakeElement('div'); }
function _compressionAnchorIndex(){ return null; }
function _assistantTurnFinalVisibleContentMap(){ return new Map(); }
function _assistantTurnVisibleContentMap(){ return new Map(); }
function _isPreservedCompressionTaskListMessage(){ return false; }
function _preservedCompressionTaskListCardsHtml(){ return ''; }
function _isContextCompactionMessage(){ return false; }
function _createAssistantTurn(){
  const turn = new FakeElement('div');
  turn.className = 'assistant-turn';
  const blocks = new FakeElement('div');
  blocks.className = 'assistant-turn-blocks';
  turn.appendChild(blocks);
  return turn;
}
function _assistantTurnBlocks(turn){ return turn ? turn.querySelector('.assistant-turn-blocks') : null; }
function _setLatestAssistantTurnLandmark(){}
function _assistantRoleHtml(){ return ''; }
function _userMessageDomId(rawIdx){ return `user-${rawIdx}`; }
function _messageSessionIndexForRawIdx(rawIdx){ return rawIdx; }
function _messageViewportAnchorKeyForMessage(){ return 'k'; }
function _stripAttachedFilesMarkerForDisplay(value){ return String(value || ''); }
function _stripWorkspaceDisplayPrefix(value){ return String(value || ''); }
function _stripLeadingAssistantThinkingMarkup(value){ return String(value || ''); }
function _getCachedRender(value){ return String(value || ''); }
function _formatInServerTz(){ return ''; }
function _formatMessageFooterTimestamp(){ return ''; }
function _questionJumpButtonHtml(){ return ''; }
function _formatTurnTps(){ return ''; }
function isTpsDisplayEnabled(){ return false; }
function _renderAttachmentHtml(){ return ''; }
function _isMarkerOnlyAssistantCompressionMessage(){ return false; }
function _isAssistantEmptyPlaceholderContent(){ return false; }
function _assistantTurnAnchorSettledFinalAnswer(){ return null; }
function _worklogReasoningTextFromMessage(){ return ''; }
function _assistantMessageBelongsInWorklog(){ return false; }
function _assistantThinkingBelongsInWorklog(){ return false; }
function _assistantReasoningPayloadText(){ return ''; }
function _collectHandoffSummaryStates(){ return []; }
function _insertCompressionLikeNode(){}
function _handoffCardsNode(){ return null; }
function renderCompressionUi(){}
function _assistantToolAnchorIdxForMessage(messages, rawIdx){ return rawIdx; }
function _cliToolResultSnippet(value){ return String(value || ''); }
function _cliPatchSnippetFromArgs(){ return ''; }
function _cliToolCardSnippet(value){ return String(value || ''); }
function _cliToolCardHasDiffSnippet(){ return false; }
function _toolArgsSnapshot(args){ return args || {}; }
function _worklogReasonHtmlFromAnchor(){ return ''; }
function _normalizeThinkingEchoCompare(value){ return String(value || ''); }
function _toolWorklogListEl(group){ return group; }
function ensureActivityGroup(parent, opts){
  const group = new FakeElement('div');
  group.className = 'tool-worklog-group tool-call-group agent-activity-group';
  group.setAttribute('data-legacy-fallback-owner', '1');
  const anchor = opts && opts.anchor;
  if (parent && anchor && anchor.parentElement === parent) parent.insertBefore(group, anchor);
  else if (parent) parent.appendChild(group);
  return group;
}
function _appendWorklogStep(){}
function _syncToolCallGroupSummary(){}
function _restoreWorklogDetailDisclosureState(){}
function _wireTransparentTurnToggle(){}
function _applyTransparentRowFading(){}
function _transparentTurnMetaMessage(){ return null; }
function _formatFirstToken(){ return ''; }
function _fmtTokens(){ return ''; }
function _renderTransparentTurnFooter(){}
function _scrollAfterMessageRender(){}
function _maybeRecoverVirtualizedBlankViewport(){ return false; }
function _updateMessageVirtualMeasurements(){}
function postProcessRenderedMessages(){}
function _postProcessWithAnchorSuppression(){}
function _formatGatewayModelLabel(){ return ''; }
function _gatewayRoutingFailoverText(){ return ''; }
function _gatewayModelWarningText(){ return ''; }
function _usedModelTurnChipLabel(){ return ''; }
function _formatTurnDuration(){ return ''; }
function _renderSettledAnchorSceneForMessage(){ return false; }

// --- status-card / transparent-ordered collaborators ---
// Isolate the ordered render path under test from the separate legacy settled
// tool fallback rebuild (covered by test_anchor_fallback_ownership.py).
function _legacySettledFallbackHasToolMetadata(){ return false; }
function buildToolCard(tc){ return { tid: (tc && tc.tid) || '', name: (tc && tc.name) || 'tool' }; }
function _transparentToolStatus(){ return ''; }
function _decorateTransparentEventRow(row, opts){
  const el = new FakeElement('div');
  el.className = 'tool-card-row';
  el.setAttribute('data-tool-id', (row && row.tid) || '');
  el.setAttribute('data-event-type', 'tool');
  return el;
}

// --- REAL production functions extracted from static/ui.js ---
eval(extractFunc('_timestampSeconds'));
eval(extractFunc('_firstValidTimestampSeconds'));
eval(extractFunc('_collectToolResultSnippetsByTid'));
eval(extractFunc('_statusCardHtml'));
eval(extractFunc('_transparentStreamOrderedParts'));
eval(extractFunc('_transparentOrderedDisplayText'));
eval(extractFunc('_transparentOrderedToolCall'));
eval(extractFunc('renderMessages'));

// --- DOM summary helper ---
function summarize(label){
  const inner = elements.msgInner;
  const segs = inner.querySelectorAll('.assistant-segment');
  const kind = (n) => n.className.split(/\s+/).find((c) => c === 'msg-body' || c === 'status-card' || c === 'msg-foot');
  const segOrder = segs.map((seg) => seg.querySelectorAll('.msg-body, .status-card, .msg-foot').map(kind));
  const cardSeg = segs.find((seg) => seg.querySelector('.status-card'));
  const bodyEl = inner.querySelector('.msg-body');
  const titleEl = inner.querySelector('.status-card-title');
  return {
    label,
    bodies: inner.querySelectorAll('.msg-body').length,
    cards: inner.querySelectorAll('.status-card').length,
    feet: inner.querySelectorAll('.msg-foot').length,
    toolRows: inner.querySelectorAll('.tool-card-row').length,
    segOrder,
    cardSegmentOrder: cardSeg ? cardSeg.querySelectorAll('.msg-body, .status-card, .msg-foot').map(kind) : null,
    cardSegBodyText: cardSeg && cardSeg.querySelector('.msg-body') ? String(cardSeg.querySelector('.msg-body').textContent) : null,
    cardSegCards: cardSeg ? cardSeg.querySelectorAll('.status-card').length : 0,
    bodyText: bodyEl ? String(bodyEl.textContent) : null,
    cardTitleText: titleEl ? String(titleEl.textContent) : null,
  };
}
"""


def _status_card_message(content, *, session_id, content_text=None):
    """Build an assistant message with the given content and a status card."""
    return (
        "{role:'assistant',content:" + json.dumps(content) + ","
        "_statusCard:{title:'Tool iteration limit reached'}}"
    )


def _run_scenario(scenario_js: str) -> dict:
    source = _extract_func_script(_ui_js()) + _harness_prelude() + scenario_js
    return json.loads(_run_node(source))


def test_prose_with_status_card_renders_body_before_card_footer_last():
    """Non-empty final prose + _statusCard: exactly one body and one card, body
    BEFORE the card, footer last — and the card's own title renders."""
    result = _run_scenario(
        """
S={session:{session_id:'sA'},messages:[
  {role:'user',content:'run'},
  {role:'assistant',content:'Final answer text.',_statusCard:{title:'Tool iteration limit reached'}}
],busy:false,toolCalls:[]};
transparentStream=false; compactWorklog=false;
renderMessages();
console.log(JSON.stringify(summarize('prose')));
"""
    )
    assert result["bodies"] == 1, f"expected exactly one msg-body, got {result['bodies']}"
    assert result["cards"] == 1, f"expected exactly one status-card, got {result['cards']}"
    assert result["feet"] == 1, f"expected exactly one msg-foot, got {result['feet']}"
    assert result["cardSegmentOrder"] == ["msg-body", "status-card", "msg-foot"], (
        "body must render BEFORE the status card, footer last; "
        f"got {result['cardSegmentOrder']}"
    )
    assert result["cardSegCards"] == 1, "the card must appear exactly once in its segment"
    assert result["bodyText"] == "Final answer text."
    assert result["cardTitleText"] == "Tool iteration limit reached"


def test_status_card_with_empty_content_renders_card_only():
    """null / empty / whitespace-only content + _statusCard: the card renders
    alone — no empty msg-body and no footer (no dead whitespace below it)."""
    result = _run_scenario(
        """
const contents=[null,'','   '];
const results=[];
for(const content of contents){
  elements.msgInner=new FakeElement('div');
  S={session:{session_id:'sB'},messages:[
    {role:'user',content:'run'},
    {role:'assistant',content:content,_statusCard:{title:'Tool iteration limit reached'}}
  ],busy:false,toolCalls:[]};
  transparentStream=false; compactWorklog=false;
  renderMessages();
  results.push(summarize('content='+JSON.stringify(content)));
}
console.log(JSON.stringify(results));
"""
    )
    assert len(result) == 3, f"expected 3 content variants, got {len(result)}"
    for case in result:
        assert case["cards"] == 1, f"{case['label']}: expected the status card, got {case['cards']}"
        assert case["bodies"] == 0, (
            f"{case['label']}: empty content must not render an empty msg-body; "
            f"got {case['bodies']}"
        )
        assert case["feet"] == 0, (
            f"{case['label']}: card-only render must not emit a footer; got {case['feet']}"
        )
        assert case["cardSegmentOrder"] == ["status-card"], (
            f"{case['label']}: card-only segment must contain only the card; "
            f"got {case['cardSegmentOrder']}"
        )


def test_array_and_transparent_forms_keep_prose_before_card_without_duplication():
    """(a) Array content form (text parts joined, non-transparent): the joined
    prose renders before the card. (b) Transparent ordered-content form (text +
    tool_use parts): final prose before the card, exactly one tool row and one
    status card — no duplicated tool/status rows."""
    result = _run_scenario(
        """
const results=[];
// (a) array content form: content is an array of text parts (joined by
// renderMessages' own array normalization), non-transparent stream.
elements.msgInner=new FakeElement('div');
S={session:{session_id:'sC1'},messages:[
  {role:'user',content:'run'},
  {role:'assistant',content:[{type:'text',text:'Part one'},{type:'text',text:'Part two'}],_statusCard:{title:'Tool iteration limit reached'}}
],busy:false,toolCalls:[]};
transparentStream=false; compactWorklog=false;
renderMessages();
results.push(summarize('array'));

// (b) transparent ordered-content form: text + tool_use parts with a status
// card on the turn-final assistant message.
elements.msgInner=new FakeElement('div');
S={session:{session_id:'sC2'},messages:[
  {role:'user',content:'run'},
  {role:'assistant',content:[
    {type:'text',text:'Step one'},
    {type:'tool_use',id:'toolu_1',name:'terminal',input:{cmd:'ls'}},
    {type:'text',text:'Final prose'}
  ],_statusCard:{title:'Tool iteration limit reached'}}
],busy:false,toolCalls:[]};
transparentStream=true; compactWorklog=false;
renderMessages();
results.push(summarize('transparent'));
console.log(JSON.stringify(results));
"""
    )
    array_case, transparent_case = result

    # (a) array form — single joined body before the card, footer last.
    assert array_case["bodies"] == 1, f"array form: expected one body, got {array_case['bodies']}"
    assert array_case["cards"] == 1, f"array form: expected one card, got {array_case['cards']}"
    assert array_case["feet"] == 1, f"array form: expected one footer, got {array_case['feet']}"
    assert array_case["cardSegmentOrder"] == ["msg-body", "status-card", "msg-foot"], (
        f"array form: body before card, footer last; got {array_case['cardSegmentOrder']}"
    )
    assert array_case["bodyText"] == "Part one\nPart two", (
        "array form: joined prose must render as the single body; "
        f"got {array_case['bodyText']!r}"
    )

    # (b) transparent form — tool row rendered once by the ordered loop, final
    # prose before the card, footer last, and the status card exactly once.
    assert transparent_case["bodies"] == 2, (
        f"transparent form: two text parts -> two bodies; got {transparent_case['bodies']}"
    )
    assert transparent_case["toolRows"] == 1, (
        "transparent form: the tool_use part must render exactly one tool row; "
        f"got {transparent_case['toolRows']}"
    )
    assert transparent_case["cards"] == 1, (
        "transparent form: the status card must render exactly once; "
        f"got {transparent_case['cards']}"
    )
    assert transparent_case["feet"] == 1, (
        f"transparent form: exactly one footer; got {transparent_case['feet']}"
    )
    assert transparent_case["cardSegmentOrder"] == ["msg-body", "status-card", "msg-foot"], (
        "transparent form: final prose before the card, footer last; "
        f"got {transparent_case['cardSegmentOrder']}"
    )
    assert transparent_case["cardSegBodyText"] == "Final prose", (
        "transparent form: the card must sit in the LAST text segment's prose; "
        f"got {transparent_case['cardSegBodyText']!r}"
    )
    assert transparent_case["cardSegCards"] == 1


def test_repeated_settled_render_does_not_duplicate_body_card_pair():
    """Repeated settled render: renderMessages wipes (#msgInner.innerHTML='')
    and rebuilds, so a second render must not leave a duplicated body/card/foot
    pair behind."""
    result = _run_scenario(
        """
elements.msgInner=new FakeElement('div');
S={session:{session_id:'sD'},messages:[
  {role:'user',content:'run'},
  {role:'assistant',content:'Settled answer.',_statusCard:{title:'Tool iteration limit reached'}}
],busy:false,toolCalls:[]};
transparentStream=false; compactWorklog=false;
renderMessages();
renderMessages();
console.log(JSON.stringify(summarize('repeated')));
"""
    )
    assert result["bodies"] == 1, (
        "repeated render must not duplicate the body; got "
        f"{result['bodies']}"
    )
    assert result["cards"] == 1, (
        "repeated render must not duplicate the status card; got "
        f"{result['cards']}"
    )
    assert result["feet"] == 1, (
        f"repeated render must not duplicate the footer; got {result['feet']}"
    )
    assert result["cardSegmentOrder"] == ["msg-body", "status-card", "msg-foot"], (
        f"repeated render must keep body-before-card; got {result['cardSegmentOrder']}"
    )
