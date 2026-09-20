"""Render-level regression: stale compaction markers must not hide the current summary.

A session can carry historical ``[CONTEXT COMPACTION]`` markers in its loaded
transcript while ``session.compression_anchor_summary`` already describes a
newer compaction that no loaded marker references. ``renderMessages()`` must
still render that current summary as its own settled card (as ``master`` did)
and the preserved task cards must be attached to exactly one owner.

The scenario drives the real ``renderMessages()`` through the same Node
harness used by ``tests/test_anchor_fallback_ownership.py``; only the DOM
shim gains a minimal ``innerHTML`` → ``firstElementChild`` parser so the
compaction card nodes built by ``renderMessages()`` are real elements.
"""

from __future__ import annotations

import json
import textwrap

from tests.test_anchor_fallback_ownership import (
    _function_source,
    _run_node_script,
    _ui_js,
)


_REAL_FUNCTIONS = (
    "_transparentStreamOrderedParts",
    "_legacySettledFallbackHasToolMetadata",
    "_isContextCompactionText",
    "_isContextCompactionMessage",
    "_compactionSummarySegment",
    "_isPreservedCompressionTaskListMarkerText",
    "_isPreservedCompressionTaskListMessage",
    "_preservedCompressionTaskListPreview",
    "_latestTodoToolItems",
    "_hasActiveTodoItems",
    "_latestPreservedCompressionTaskListMessages",
    "_compressionStatusCardHtml",
    "_preservedCompressionTaskListCardHtml",
    "_preservedCompressionTaskListCardsHtml",
    "_latestCompressionReferenceMessage",
    "_shouldShowSettledCompressionReference",
    "_compactionDigestText",
    "_compactionCardPreview",
    "_compressionReferenceCardHtml",
    "_loadedCompactionMarkerRawIdxs",
    "_selectCompactionCardPlacements",
    "_insertCompactionCardNodes",
    "_insertPreservedCompressionTaskFallback",
    "_pinCompactionCardAtTop",
    "_pinSettledCompressionReferenceAtTop",
    "renderMessages",
)

_DOM_SHIM = r"""
class FakeClassList {
  constructor(el) { this.el = el; }
  _set() { return new Set(String(this.el.className || '').split(/\s+/).filter(Boolean)); }
  contains(name) { return this._set().has(name); }
  add(...names) {
    const set = this._set();
    names.forEach((name) => set.add(name));
    this.el.className = Array.from(set).join(' ');
  }
  remove(...names) {
    const set = this._set();
    names.forEach((name) => set.delete(name));
    this.el.className = Array.from(set).join(' ');
  }
  toggle(name) {
    const set = this._set();
    if (set.has(name)) set.delete(name); else set.add(name);
    this.el.className = Array.from(set).join(' ');
    return set.has(name);
  }
}
function unescapeHtml(value) {
  return String(value)
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}
// Minimal HTML → FakeElement tree parser. Only used when renderMessages()
// reads `firstElementChild` after assigning innerHTML (the compaction /
// reference / preserved-task card nodes). Text nodes are dropped; the
// assertions below rely on data-* attributes only.
function parseHtmlInto(parent, html) {
  const tagRe = /<(\/)?([A-Za-z][\w-]*)((?:\s+[\w:-]+(?:="[^"]*")?)*)\s*(\/)?>/g;
  const stack = [parent];
  let match;
  while ((match = tagRe.exec(html))) {
    if (match[1]) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new FakeElement(match[2]);
    const attrRe = /([\w:-]+)(?:="([^"]*)")?/g;
    let attr;
    while ((attr = attrRe.exec(match[3] || ''))) {
      el.setAttribute(attr[1], attr[2] === undefined ? '' : unescapeHtml(attr[2]));
    }
    stack[stack.length - 1].appendChild(el);
    if (!match[4]) stack.push(el);
  }
}
class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.attributes = {};
    this.className = '';
    this.id = '';
    this.hidden = false;
    this._innerHTML = '';
    this._pendingHtml = null;
    this.style = {};
    this.classList = new FakeClassList(this);
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._pendingHtml = this._innerHTML ? this._innerHTML : null;
  }
  get innerHTML() { return this._innerHTML; }
  get firstElementChild() {
    if (this._pendingHtml !== null) {
      const html = this._pendingHtml;
      this._pendingHtml = null;
      parseHtmlInto(this, html);
    }
    return this.children[0] || null;
  }
  appendChild(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, ref) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    const idx = this.children.indexOf(ref);
    if (idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }
  remove() {
    if (!this.parentElement) return;
    const idx = this.parentElement.children.indexOf(this);
    if (idx >= 0) this.parentElement.children.splice(idx, 1);
    this.parentElement = null;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'id') this.id = String(value);
    if (name === 'class') this.className = String(value);
    if (name.startsWith('data-')) this.dataset[dataKey(name)] = String(value);
  }
  getAttribute(name) {
    if (name === 'id') return this.id || null;
    if (name === 'class') return this.className || null;
    if (name.startsWith('data-')) {
      const value = this.dataset[dataKey(name)];
      return value === undefined ? null : String(value);
    }
    return this.attributes[name] === undefined ? null : this.attributes[name];
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name.startsWith('data-')) delete this.dataset[dataKey(name)];
  }
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
  querySelectorAll(selector) {
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
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  insertAdjacentHTML() {}
}
function dataKey(name) {
  return String(name).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}
function matchesSelector(el, selector) {
  return String(selector || '').split(',').some((part) => matchesSimple(el, part.trim()));
}
function matchesSimple(el, selector) {
  if (!selector) return false;
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
function $(id) { return elements[id] || null; }
function isTransparentStream() { return false; }
function isCompactWorklogMode() { return true; }
function isSimplifiedToolCalling() { return true; }
function t(key) { return key; }
function li() { return ''; }
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function msgContent(message) {
  if (Array.isArray(message.content)) {
    return message.content.filter((p) => p && p.type === 'text').map((p) => p.text || p.content || '').join('\n');
  }
  return String(message.content || '');
}
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
let virtualStart = 0;
const _sessionHtmlCache = new Map();
const _recycleStash = new Map();
const _msgNodeRecycleEnabled = false;
const _recycleResetAttrs = [];
const _ERR_MSG_RE = /__never__/;

function _captureMessageScrollSnapshot() { return null; }
function _resetMessageRenderWindow(sid) { _messageRenderWindowSid = sid; }
function _getVisibleMessagesWithIdx() {
  return S.messages.map((m, rawIdx) => (
    m && m.role !== 'tool' && !_isContextCompactionMessage(m) && !_isPreservedCompressionTaskListMessage(m)
  ) ? { m, rawIdx } : null).filter(Boolean);
}
function _messageVirtualKeepTailCount() { return 20; }
function _currentMessageVirtualWindow(vis) {
  const start = Math.min(virtualStart, vis.length);
  return {
    virtualized: start > 0,
    start,
    end: vis.length,
    topPad: start > 0 ? 120 * start : 0,
    bottomPad: 0,
    total: vis.length,
    tailStart: vis.length,
  };
}
function _messageVirtualWindowKeyFor() { return 'all'; }
function _messageRenderCacheSignature() { return 'sig'; }
function _compressionStateForCurrentSession() { return null; }
function clearCompressionUi() {}
function _handoffStateForCurrentSession() { return null; }
function _captureWorklogDetailDisclosureState() { return null; }
function _applySessionNavigationPrefs() {}
function _messageVirtualSpacer() { return new FakeElement('div'); }
function _wireMessageWindowLoadEarlierButton() {}
function _compressionAnchorIndex() { return null; }
function _assistantTurnFinalVisibleContentMap() { return new Map(); }
function _assistantTurnVisibleContentMap() { return new Map(); }
function _engineAwareCompressionCopy() {
  return { label: 'context_compaction_label', preview: 'reference_only_label' };
}
function _createAssistantTurn() {
  const turn = new FakeElement('div');
  turn.className = 'assistant-turn';
  const blocks = new FakeElement('div');
  blocks.className = 'assistant-turn-blocks';
  turn.appendChild(blocks);
  return turn;
}
function _assistantTurnBlocks(turn) { return turn ? turn.querySelector('.assistant-turn-blocks') : null; }
function _setLatestAssistantTurnLandmark() {}
function _assistantRoleHtml() { return ''; }
function _userMessageDomId(rawIdx) { return `user-${rawIdx}`; }
function _messageSessionIndexForRawIdx(rawIdx) { return rawIdx; }
function _messageViewportAnchorKeyForMessage() { return 'k'; }
function _stripAttachedFilesMarkerForDisplay(value) { return String(value || ''); }
function _stripWorkspaceDisplayPrefix(value) { return String(value || ''); }
function _stripLeadingAssistantThinkingMarkup(value) { return String(value || ''); }
function _getCachedRender(value) { return String(value || ''); }
function _formatInServerTz() { return ''; }
function _formatMessageFooterTimestamp() { return ''; }
function _questionJumpButtonHtml() { return ''; }
function _formatTurnTps() { return ''; }
function isTpsDisplayEnabled() { return false; }
function _renderAttachmentHtml() { return ''; }
function _isMarkerOnlyAssistantCompressionMessage() { return false; }
function _isAssistantEmptyPlaceholderContent() { return false; }
function _assistantTurnAnchorSettledFinalAnswer() { return null; }
function _worklogReasoningTextFromMessage() { return ''; }
function _assistantMessageBelongsInWorklog() { return false; }
function _assistantThinkingBelongsInWorklog() { return false; }
function _assistantReasoningPayloadText() { return ''; }
function _statusCardHtml() { return ''; }
function _collectHandoffSummaryStates() { return []; }
function _handoffCardsNode() { return null; }
function renderCompressionUi() {}
function _assistantToolAnchorIdxForMessage(messages, rawIdx) { return rawIdx; }
function _cliToolResultSnippet(value) { return String(value || ''); }
function _cliPatchSnippetFromArgs() { return ''; }
function _cliToolCardSnippet(value) { return String(value || ''); }
function _cliToolCardHasDiffSnippet() { return false; }
function _toolArgsSnapshot(args) { return args || {}; }
function _worklogReasonHtmlFromAnchor() { return ''; }
function _normalizeThinkingEchoCompare(value) { return String(value || ''); }
function _toolWorklogListEl(group) { return group; }
function ensureActivityGroup(parent) {
  const group = new FakeElement('div');
  if (parent) parent.appendChild(group);
  return group;
}
function _appendWorklogStep() {}
function _syncToolCallGroupSummary() {}
function _restoreWorklogDetailDisclosureState() {}
function _scrollAfterMessageRender() {}
function _maybeRecoverVirtualizedBlankViewport() { return false; }
function _updateMessageVirtualMeasurements() {}
function postProcessRenderedMessages() {}
function _postProcessWithAnchorSuppression() {}
function _formatGatewayModelLabel() { return ''; }
function _gatewayRoutingFailoverText() { return ''; }
function _gatewayModelWarningText() { return ''; }
function _usedModelTurnChipLabel() { return ''; }
function _formatTurnDuration() { return ''; }
function _renderSettledAnchorSceneForMessage() { return false; }
"""

_SCENARIO = r"""
const TOTAL = 220;
const STALE_MARKER_IDXS = [40, 100, 160];
const TASK_LIST_IDX = 161;
const CURRENT_TOKEN = 'CURRENT-SUMMARY-TOKEN-7124';
const TASK_MARKER = '[Your active task list was preserved across context compression]';

function staleMarker(rawIdx) {
  return '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below.\n'
    + '## Historical Task Snapshot\nUser asked: "stale request ' + rawIdx + '"\n'
    + '## Goal\nSTALE-MARKER-' + rawIdx + ' historical state\n--- END OF CONTEXT SUMMARY ---';
}
function buildMessages() {
  const messages = [];
  for (let i = 0; i < TOTAL; i++) {
    if (STALE_MARKER_IDXS.includes(i)) {
      messages.push({ role: 'assistant', content: staleMarker(i) });
    } else if (i === TASK_LIST_IDX) {
      messages.push({ role: 'user', content: TASK_MARKER + '\n- [ ] finish the compaction fix' });
    } else if (i % 2 === 0) {
      messages.push({ role: 'user', content: 'question ' + i });
    } else {
      messages.push({ role: 'assistant', content: 'answer ' + i });
    }
  }
  return messages;
}
function render(opts) {
  elements.msgInner = new FakeElement('div');
  _sessionHtmlCache.clear();
  _sessionHtmlCacheSid = null;
  virtualStart = opts.virtualStart || 0;
  _messagesTruncated = !!opts.truncated;
  _oldestIdx = opts.truncated ? 50 : 0;
  S = {
    session: {
      session_id: opts.sid,
      compression_anchor_summary: opts.summary,
    },
    messages: buildMessages(),
    toolCalls: [],
    busy: false,
  };
  renderMessages();
  const inner = elements.msgInner;
  const cards = inner.querySelectorAll('[data-compression-card]');
  const rawText = (node) => String(node.getAttribute('data-raw-text') || '');
  const summaryCards = cards.filter((node) => rawText(node).includes(CURRENT_TOKEN));
  const taskCards = cards.filter((node) => rawText(node).startsWith(TASK_MARKER));
  const owners = inner.querySelectorAll('[data-compaction-task-owner]');
  const ownerHasSummary = owners.length === 1
    && owners[0].querySelectorAll('[data-compression-card]').some((node) => rawText(node).includes(CURRENT_TOKEN));
  const ownerHasTask = owners.length === 1
    && owners[0].querySelectorAll('[data-compression-card]').some((node) => rawText(node).startsWith(TASK_MARKER));
  const placements = inner.querySelectorAll('[data-compaction-placement]');
  const summaryTopIdx = summaryCards.length
    ? inner.children.findIndex((child) => child.querySelectorAll('[data-compression-card]').some((node) => rawText(node).includes(CURRENT_TOKEN)))
    : -1;
  const loadOlderIdx = inner.children.findIndex((child) => child.id === 'loadOlderIndicator');
  return {
    renderedRows: inner.querySelectorAll('[data-msg-idx]').length,
    currentSummaryVisible: summaryCards.length > 0,
    currentSummaryCards: summaryCards.length,
    staleCards: placements.map((node) => node.getAttribute('data-compaction-placement')),
    staleCardTexts: placements.map((node) => {
      const card = node.querySelector('[data-compression-card]');
      return card ? (/STALE-MARKER-(\d+)/.exec(rawText(card))?.[1] || null) : null;
    }),
    taskCards: taskCards.length,
    taskOwners: owners.length,
    ownerHasSummary,
    ownerHasTask,
    standaloneFallbacks: inner.querySelectorAll('[data-compaction-task-fallback]').length,
    summaryTopIdx,
    loadOlderIdx,
  };
}

const NEWER_SUMMARY = 'Compacted context: ' + CURRENT_TOKEN + ' — newest state after the last compaction';
const fullWindow = render({ sid: 'full', summary: NEWER_SUMMARY });
// Visible index 170 maps to raw idx >= 174 (three markers and the task-list
// message are not renderable rows), so every stale marker is pre-window.
const virtualWindow = render({ sid: 'virtual', summary: NEWER_SUMMARY, virtualStart: 170 });
const truncatedTail = render({ sid: 'truncated', summary: NEWER_SUMMARY, truncated: true });
// Control: the current summary IS referenced by the latest loaded marker, so
// the marker card renders it and no second (fallback) card may appear.
const matchingSummary = render({ sid: 'matching', summary: 'STALE-MARKER-160 historical state' });
const matchingCards = elements.msgInner.querySelectorAll('[data-compression-card]')
  .filter((node) => String(node.getAttribute('data-raw-text') || '').includes('STALE-MARKER-160'));
console.log(JSON.stringify({
  fullWindow,
  virtualWindow,
  truncatedTail,
  matchingSummary: {
    ...matchingSummary,
    latestMarkerCards: matchingCards.length,
  },
}));
"""


def _scenario_script() -> str:
    js = _ui_js()
    evals = "\n".join(
        f"eval({json.dumps(_function_source(js, name))});" for name in _REAL_FUNCTIONS
    )
    return textwrap.dedent(_DOM_SHIM) + "\n" + evals + "\n" + textwrap.dedent(_SCENARIO)


def test_stale_markers_do_not_suppress_current_summary_and_task_owner_is_unique():
    result = json.loads(_run_node_script(_scenario_script()))

    full = result["fullWindow"]
    assert full["renderedRows"] > 200
    assert full["currentSummaryVisible"] is True
    assert full["currentSummaryCards"] == 1
    assert full["staleCards"] == ["inline", "inline", "inline"]
    assert full["staleCardTexts"] == ["40", "100", "160"]
    assert full["taskCards"] == 1
    assert full["taskOwners"] == 1
    assert full["ownerHasSummary"] is True
    assert full["ownerHasTask"] is True
    assert full["standaloneFallbacks"] == 0

    virtual = result["virtualWindow"]
    assert virtual["currentSummaryVisible"] is True
    assert virtual["currentSummaryCards"] == 1
    assert virtual["staleCards"] == ["pre-window", "pre-window", "pre-window"]
    assert virtual["staleCardTexts"] == ["40", "100", "160"]
    assert virtual["taskCards"] == 1
    assert virtual["taskOwners"] == 1
    assert virtual["ownerHasSummary"] is True
    assert virtual["standaloneFallbacks"] == 0

    truncated = result["truncatedTail"]
    assert truncated["currentSummaryVisible"] is True
    assert truncated["currentSummaryCards"] == 1
    assert truncated["loadOlderIdx"] >= 0
    # The settled current-summary card is pinned directly under the
    # load-earlier affordance, before the stale marker cards and rows.
    assert truncated["summaryTopIdx"] == truncated["loadOlderIdx"] + 1
    assert truncated["taskCards"] == 1
    assert truncated["taskOwners"] == 1
    assert truncated["ownerHasSummary"] is True
    assert truncated["standaloneFallbacks"] == 0

    matching = result["matchingSummary"]
    assert matching["currentSummaryVisible"] is False
    assert matching["latestMarkerCards"] == 1
    assert matching["staleCards"] == ["inline", "inline", "inline"]
    assert matching["taskCards"] == 1
    assert matching["taskOwners"] == 1
    assert matching["ownerHasTask"] is True
    assert matching["standaloneFallbacks"] == 0
