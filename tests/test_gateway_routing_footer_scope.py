"""Renderer regression for the per-message routing footer (issue #8287).

A turn can carry several assistant segments — interim answers plus the final
one — and each of them may carry its own ``_gatewayRouting``. Two rules:

1. Every message keeps its **own** footer labels (model, duration, usage). The
   footer lookup must be scoped to the message's own ``.assistant-segment``:
   with a whole-turn ``.msg-foot`` lookup the interim answer wrote into the
   turn's only foot and the duplicate guard then made the final message skip
   its labels entirely (duration/usage rendered 0/0).
2. Turn-level labels (switch/failover warnings) belong to the turn's **final**
   assistant segment only — otherwise a routed turn shows them once per
   segment.

Drives the real ``renderMessages()`` in a node DOM shim, not source assertions:
a turn where interim and final both carry ``_gatewayRouting`` must keep
``.msg-duration-inline`` and ``.msg-usage-inline`` on the final foot.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO / "static" / "ui.js"


def _function_source(name: str) -> str:
    """Extract a top-level ``function name(...)`` body from static/ui.js."""
    ui_js = UI_JS_PATH.read_text(encoding="utf-8")
    start = ui_js.find(f"function {name}(")
    assert start != -1, f"function {name}() not found in static/ui.js"
    brace = ui_js.index("{", start)
    depth = 0
    for i in range(brace, len(ui_js)):
        if ui_js[i] == "{":
            depth += 1
        elif ui_js[i] == "}":
            depth -= 1
            if depth == 0:
                return ui_js[start : i + 1]
    raise AssertionError(f"function {name}() is unterminated")


_SCRIPT = r"""
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
}
function dataKey(name) {
  return String(name).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
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
function matchesComplex(el, selector) {
  // Descendant combinators: every compound part must match the element itself
  // or one of its ancestors, in order.
  const parts = String(selector || '').split(/\s+/).filter(Boolean);
  if (!parts.length) return false;
  const last = parts.pop();
  if (!matchesSimple(el, last)) return false;
  let node = el.parentElement;
  for (let i = parts.length - 1; i >= 0; i--) {
    while (node && !matchesSimple(node, parts[i])) node = node.parentElement;
    if (!node) return false;
    node = node.parentElement;
  }
  return true;
}
function matchesSelector(el, selector) {
  return String(selector || '').split(',').some((part) => matchesComplex(el, part.trim()));
}
class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.parentNode = null;
    this.dataset = {};
    this.attributes = {};
    this.className = '';
    this.id = '';
    this.hidden = false;
    this.innerHTML = '';
    this.style = {};
    this.classList = new FakeClassList(this);
  }
  get firstChild() { return this.children.length ? this.children[0] : null; }
  appendChild(child) {
    child.parentElement = this;
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, ref) {
    child.parentElement = this;
    child.parentNode = this;
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
    this.parentNode = null;
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
  insertAdjacentHTML(_position, html) { parseHtmlInto(this, html); }
}
function parseHtmlInto(parent, html) {
  // Minimal well-formed-HTML parser for the footer/body templates: elements,
  // class/data attributes, text nodes ignored (assertions target classes).
  const tagRe = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[\w-]+="[^"]*")*)\s*(\/?)>/g;
  let last = 0;
  let match;
  const stack = [parent];
  while ((match = tagRe.exec(String(html))) !== null) {
    last = tagRe.lastIndex;
    const closing = match[1] === '/';
    const tag = match[2];
    const attrs = match[3] || '';
    const selfClose = match[4] === '/';
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new FakeElement(tag);
    const attrRe = /([\w-]+)="([^"]*)"/g;
    let attr;
    while ((attr = attrRe.exec(attrs)) !== null) {
      if (attr[1] === 'class') el.className = attr[2];
      else if (attr[1].startsWith('data-')) el.dataset[dataKey(attr[1])] = attr[2];
      else el.setAttribute(attr[1], attr[2]);
    }
    stack[stack.length - 1].appendChild(el);
    if (!selfClose) stack.push(el);
  }
  return parent;
}

const elements = {
  msgInner: new FakeElement('div'),
  emptyState: new FakeElement('div'),
};
global.window = { _showTokenUsage: true, _showThinking: true };
global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => elements[id] || null,
};
global.performance = { now: () => 1 };
global.requestAnimationFrame = (fn) => fn();
global.setTimeout = (fn) => fn();
function $(id) { return elements[id] || null; }
function isTransparentStream() { return false; }
function isCompactWorklogMode() { return false; }
function isSimplifiedToolCalling() { return true; }
function t(key) { return key; }
function li() { return ''; }
function esc(value) { return String(value == null ? '' : value); }
function msgContent(message) {
  if (Array.isArray(message.content)) {
    return message.content.filter((p) => p && p.type === 'text').map((p) => p.text || '').join('\n');
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
const _sessionHtmlCache = new Map();
const _recycleStash = new Map();
const _msgNodeRecycleEnabled = false;
const _recycleResetAttrs = [];
const _ERR_MSG_RE = /__never__/;
// Generic no-op stubs for helpers this scenario does not exercise.
function _activeCompressionRecoveryPayload() { return null; }
function _appendWorklogStep() { return null; }
function _applyMediaPlaybackPreferences() { return null; }
function _applyTransparentRowFading() { return null; }
function _applyUserRowIntrinsicHeight() { return null; }
function _assistantAnchorForActivity() { return null; }
function _assistantTurnHasVisibleRenderedSegment() { return true; }
function _cacheSettledTranscriptHtml() { return null; }
function _clearCompressionElapsedTimer() { return null; }
function _collectToolResultSnippetsByTid() { return null; }
function _compactionCardEntry() { return null; }
function _compactionSummarySegment() { return null; }
function _compressionCardsNode() { return null; }
function _compressionRecoveryHtml() { return null; }
function _compressionReferenceCardHtml() { return null; }
function _decorateTransparentEventRow() { return null; }
function _deferClearProgrammaticScroll() { return null; }
function _extractInlineThinkingFromContentForRender() { return null; }
function _fixMobileScrollJank() { return null; }
function _flush() { return null; }
function _fmtDateSep() { return ''; }
function _fmtSv() { return ''; }
function _formatFirstToken() { return ''; }
function _hydrateIdLinkedHistoricalToolScenes() { return null; }
function _initMediaPlaybackObserver() { return null; }
function _insertCompressionLikeNodeByRawIdx() { return null; }
function _isBranchableReadOnlySession() { return false; }
function _isKeepSettledWorklogOpenArmed() { return false; }
function _isRecoveryControlMessage() { return false; }
function _isReadOnlySession() { return false; }
function _legacySettledFallbackHasToolMetadata() { return false; }
function _liveAssistantSegmentTextLength() { return 0; }
function _materializeDeferredWorklogRows() { return null; }
function _pinSettledCompressionReferenceAtTop() { return null; }
function _processWakeupCardHtml() { return ''; }
function _processWakeupInfo() { return null; }
function _reanchorPinnedTailAfterRender() { return null; }
function _rehydrateDeferredWorklogsFromCache() { return null; }
function _rehydrateTransparentStreamDom() { return null; }
function _rememberRenderedUserRowIntrinsicHeights() { return null; }
function _renderSettledAnchorSceneForMessage() { return false; }
function _renderTransparentTurnFooter() { return null; }
function _setCompressionSessionLock() { return null; }
function _settledTranscriptOwnsLiveTurn() { return false; }
function _stampMediaSnapshots(html) { return html; }
function _structuralCount() { return 0; }
function _syncExtensionMessageActionSlots() { return null; }
function _syncLiveRunStatusAfterRender() { return null; }
function _syncTransparentEventControls() { return null; }
function _thinkingActivityNode() { return null; }
function _transparentOrderedDisplayText(value) { return String(value || ''); }
function _transparentOrderedToolCall() { return null; }
function _transparentStreamOrderedParts() { return null; }
function _transparentToolStatus() { return ''; }
function _transparentTurnMetaMessage() { return ''; }
function _turnDurationForAnchor() { return null; }
function _turnHasVisibleContent() { return true; }
function _wireMessageWindowLoadEarlierButton() { return null; }
function _wireTransparentTurnToggle() { return null; }

function _captureMessageScrollSnapshot() { return null; }
function _resetMessageRenderWindow(sid) { _messageRenderWindowSid = sid; }
function _latestPreservedCompressionTaskListMessages() { return []; }
function _getVisibleMessagesWithIdx() {
  return S.messages.map((m, rawIdx) => (m && m.role !== 'tool') ? { m, rawIdx } : null).filter(Boolean);
}
function _messageVirtualKeepTailCount() { return 100; }
function _currentMessageVirtualWindow(vis) {
  return { virtualized: false, start: 0, end: vis.length, topPad: 0, bottomPad: 0, total: vis.length, tailStart: vis.length };
}
function _messageVirtualWindowKeyFor() { return 'all'; }
function _messageRenderCacheSignature() { return 'sig'; }
function _compressionStateForCurrentSession() { return null; }
function clearCompressionUi() {}
function _handoffStateForCurrentSession() { return null; }
function _captureWorklogDetailDisclosureState() { return null; }
function _latestCompressionReferenceMessage() { return { message: null, rawIdx: -1 }; }
function _shouldShowSettledCompressionReference() { return false; }
function _applySessionNavigationPrefs() {}
function _messageVirtualSpacer() { return new FakeElement('div'); }
function _compressionAnchorIndex() { return null; }
function _assistantTurnFinalVisibleContentMap() { return new Map(); }
function _assistantTurnVisibleContentMap() { return new Map(); }
function _isPreservedCompressionTaskListMessage() { return false; }
function _preservedCompressionTaskListCardsHtml() { return ''; }
function _isContextCompactionMessage() { return false; }
function _createAssistantTurn(tsTitle = '', tpsText = '') {
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
function _formatMessageFooterTimestamp() { return 'STAMP'; }
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
function _insertCompressionLikeNode() {}
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
function _syncToolCallGroupSummary() {}
function _restoreWorklogDetailDisclosureState() {}
function _scrollAfterMessageRender() {}
function _maybeRecoverVirtualizedBlankViewport() { return false; }
function _updateMessageVirtualMeasurements() {}
function _resetMessageVirtualMeasurementBurst() {}
function postProcessRenderedMessages() {}
function _postProcessWithAnchorSuppression() {}
function _loadedCompactionMarkerRawIdxs() { return []; }
function _selectCompactionCardPlacements() {
  return { preWindowMarkers: [], inlineMarkers: [], taskOwner: null };
}
function _insertCompactionCardNodes() {
  return { insertedNodes: [], taskOwnerNode: null };
}
function _insertPreservedCompressionTaskFallback() { return false; }
function _pinCompactionCardAtTop() { return false; }
function _thinkingCardHtml() { return ''; }
// Master calls this at the end of renderMessages (#7591). The harness extracts
// that function without the helper, so a missing binding aborts before the
// footer assertions. Blank-viewport recovery is stubbed off above; this return
// only feeds that stub. Do not default isTurnFinal to true to paper over it —
// that paints turn warnings on every segment.
function _captureMessageRecoveryOwnership() {
  return { programmatic: false, jumpOwner: false };
}

// Footer label helpers: distinctive strings so the assertions can tell which
// fragment landed in which foot. The gateway-routing footer helpers from
// static/ui.js are covered by their own tests.
function _formatGatewayModelLabel() { return 'GATEWAY-MODEL'; }
function _gatewayRoutingFailoverText(routing) { return routing ? 'FAILOVER-WARN' : ''; }
function _gatewayModelWarningText(routing) { return routing ? 'MODEL-WARN' : ''; }
function _usedModelTurnChipLabel() { return 'USED-MODEL'; }
function _formatTurnDuration(value) { return value ? 'DURATION' : ''; }
function _fmtTokens(value) { return String(value); }

/*RAW_TURN_BOUNDARY*/
/*INSERT_SEGMENT_BLOCK*/
/*ASSISTANT_SEGMENT_IS_TURN_FINAL*/
/*RENDER_MESSAGES*/

function footCounts(foot) {
  const count = (selector) => foot ? foot.querySelectorAll(selector).length : -1;
  return {
    gateway: count('.msg-gateway-inline'),
    usedModel: count('.msg-used-model-inline'),
    duration: count('.msg-duration-inline'),
    usage: count('.msg-usage-inline'),
    warning: count('.msg-model-warning-inline'),
    failover: count('.gateway-failover-inline'),
    footFound: !!foot,
  };
}
function footFor(rawIdx) {
  const segs = elements.msgInner.querySelectorAll(`.assistant-segment[data-msg-idx="${rawIdx}"]`);
  const seg = segs.length ? segs[segs.length - 1] : null;
  return seg ? seg.querySelector('.msg-foot') : null;
}

S = {
  session: { session_id: 's1', model: 'gpt-5-mini' },
  messages: [
    { role: 'user', content: 'run' },
    { role: 'assistant', content: 'Interim answer', _gatewayRouting: { used_model: 'a/m1' } },
    {
      role: 'assistant',
      content: 'Final answer',
      _gatewayRouting: { used_model: 'a/m1' },
      _turnDuration: { elapsed_seconds: 2 },
      _turnUsage: { input_tokens: 5, output_tokens: 3 },
    },
  ],
  toolCalls: [],
  busy: false,
};
renderMessages();

const result = {
  interim: footCounts(footFor(1)),
  final: footCounts(footFor(2)),
  turnWarningTotal: elements.msgInner.querySelectorAll('.msg-model-warning-inline').length,
  turnFailoverTotal: elements.msgInner.querySelectorAll('.gateway-failover-inline').length,
  segmentCount: elements.msgInner.querySelectorAll('.assistant-segment[data-msg-idx]').length,
};
console.log(JSON.stringify(result));
"""


def _run_footer_scenario() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable is required for JavaScript behavior checks")
    script = (
        _SCRIPT.replace("/*RAW_TURN_BOUNDARY*/", _function_source("_rawTurnBoundaryByRawIdx"))
        .replace("/*INSERT_SEGMENT_BLOCK*/", _function_source("_insertSegmentBlock"))
        .replace(
            "/*ASSISTANT_SEGMENT_IS_TURN_FINAL*/",
            _function_source("_assistantSegmentIsTurnFinal"),
        )
        .replace("/*RENDER_MESSAGES*/", _function_source("renderMessages"))
    )
    proc = subprocess.run(
        [node, "-e", script],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_final_answer_keeps_its_own_duration_and_usage_foot():
    """Interim + final both routed: the final foot keeps duration and usage.

    With the whole-turn ``.msg-foot`` lookup the interim answer wrote into the
    turn's only foot, and the duplicate guard then skipped the final message
    entirely (duration/usage rendered 0/0 on the final foot).
    """
    result = _run_footer_scenario()
    assert result["segmentCount"] == 2, result
    assert result["final"]["footFound"], result
    assert result["final"]["duration"] == 1, result
    assert result["final"]["usage"] == 1, result
    # The final answer keeps its own model labels, too.
    assert result["final"]["gateway"] == 1, result
    assert result["final"]["usedModel"] == 1, result


def test_interim_answer_keeps_its_own_model_labels():
    result = _run_footer_scenario()
    assert result["interim"]["footFound"], result
    assert result["interim"]["gateway"] == 1, result
    assert result["interim"]["usedModel"] == 1, result
    # Per-segment data only: no turn-level duration/usage on an interim answer.
    assert result["interim"]["duration"] == 0, result
    assert result["interim"]["usage"] == 0, result


def test_turn_level_warnings_render_once_on_the_final_segment():
    """Switch/failover warnings describe the turn, not each segment."""
    result = _run_footer_scenario()
    assert result["turnWarningTotal"] == 1, result
    assert result["turnFailoverTotal"] == 1, result
    assert result["final"]["warning"] == 1, result
    assert result["final"]["failover"] == 1, result
    assert result["interim"]["warning"] == 0, result
    assert result["interim"]["failover"] == 0, result


def test_footer_lookup_is_scoped_to_the_message_segments():
    """The lookup must not fall back to the turn's last foot (the bug)."""
    ui_js = UI_JS_PATH.read_text(encoding="utf-8")
    assert (
        'querySelectorAll(`.assistant-segment[data-msg-idx="${mi}"] .msg-foot`)' in ui_js
    ), (
        "the footer lookup must be scoped to the message's own segments; "
        "a whole-turn '.msg-foot' lookup makes the final message skip its labels"
    )


def test_touch_devices_keep_footer_time_and_actions_visible():
    """A routing label moves an interim footer into .msg-foot-with-usage, which hides the
    timestamp and actions until hover. Touch devices cannot hover, so a (hover: none)
    rule must restore them (#8287 UX gate: phone interims lost time, speak, branch, copy)."""
    css = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(encoding="utf-8")
    start = css.index("@media (hover: none), (max-width: 640px) {\n  .msg-foot-with-usage .msg-time")
    block = css[start:css.index("}\n}", start)]
    assert ".msg-foot-with-usage .msg-actions" in block
    assert "opacity: 1" in block
