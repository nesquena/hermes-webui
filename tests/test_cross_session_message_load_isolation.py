"""Regression tests for cross-session transcript isolation in loadSession.

The checks lock behavior at both levels:

1) Structural guard checks in ``loadSession()`` and ``_ensureMessagesLoaded()``
   around ownership tokens and catch-path mutations.
2) Runtime ordering/catch coverage using an executable Node harness to reproduce
   old->new load overlap and stale rejected continuation behavior.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    """Return the full function source for ``name`` from a single js file.

    Brace-depth tracking handles nested blocks and avoids fragile substring
    matching in the large, hand-formatted source file.
    """
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name} not found in sessions.js"

    signature_start = source.find("(", start)
    assert signature_start >= 0, f"function {name} is missing its parameter list"
    paren_depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    signature_end = None
    for index in range(signature_start, len(source)):
        ch = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ('\'', '"', "`"):
            in_string = ch
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth == 0:
                signature_end = index + 1
                break
    assert signature_end is not None, f"function {name} has an unterminated parameter list"

    brace_start = source.find("{", signature_end)
    assert brace_start >= 0, f"function {name} is missing '{{'"

    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False

    for index in range(brace_start, len(source)):
        ch = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ('\'', '"', "`"):
            in_string = ch
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]

    raise AssertionError(f"Could not extract function {name}")


LOAD_SESSION_SRC = _extract_function(SESSIONS_SRC, "loadSession")
ENSURE_MESSAGES_LOADED_SRC = _extract_function(SESSIONS_SRC, "_ensureMessagesLoaded")
INFLIGHT_HAS_VISIBLE_STATE_SRC = _extract_function(SESSIONS_SRC, "_inflightHasVisibleLiveState")
SELECT_LIVE_RECOVERY_INFLIGHT_SRC = _extract_function(SESSIONS_SRC, "_selectLiveRecoveryInflight")
MERGE_PENDING_SESSION_MESSAGE_SRC = _extract_function(SESSIONS_SRC, "_mergePendingSessionMessage")
COMPOSER_DRAFT_HELPERS_SRC = "\n".join(
    _extract_function(SESSIONS_SRC, name)
    for name in (
        "_composerDraftFilesForPersist",
        "_composerDraftFileSignature",
        "_composerDraftPayloadSignature",
        "_composerDraftPayloadSignatureForSid",
        "_composerDraftRevision",
        "_noteComposerDraftPayload",
        "_bumpComposerDraftRevision",
        "_clearComposerDraftRestoreSuppression",
        "_isComposerDraftRestoreSuppressed",
        "_suppressComposerDraftRestoreAfterSubmit",
        "_clearRememberedNewChatDraftSession",
        "_safeDraftSessionId",
        "_parseSessionMovedDraftError",
        "_composerDraftResponseSessionId",
        "_setComposerDraftLocalIntent",
        "_forgetComposerDraftLocalFallback",
        "_handleComposerDraftPostFailure",
        "_postComposerDraftPayload",
        "_composerDraftHasPayload",
        "_sessionComposerDraftHasPayload",
        "_rememberComposerDraftPayloadState",
        "_saveComposerDraftNow",
        "_restoreComposerDraft",
        "_switchProfileForSessionLoad",
        "_clearComposerDraft",
    )
)


def _normalise_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_loadsession_has_generation_token_and_forwards_to_ensure_messages_loaded():
    body = LOAD_SESSION_SRC
    assert "_loadSessionGeneration" in body, (
        "loadSession() must use a global generation counter so superseded loads "
        "can be rejected by continuation ownership checks"
    )
    assert "const _loadGeneration = ++_loadSessionGeneration" in body, (
        "loadSession() must increment and capture per-call generation"
    )
    assert "const _isCurrentLoad = () => _loadingSessionId === sid && _loadSessionGeneration === _loadGeneration" in body
    assert "loadGeneration:_loadGeneration" in body, (
        "loadSession() must thread generation into _ensureMessagesLoaded()"
    )
    # Guard each await/catch branch so stale continuation cannot mutate shared pane state.
    # Two calls exist in this function: INFLIGHT and idle branches.
    norm = _normalise_ws(body)
    assert norm.count("if(!_isCurrentLoad())") >= 6, (
        "loadSession() should check ownership in multiple await/catch paths, "
        "including stale _ensureMessagesLoaded catch branches"
    )
    ensure_call = _normalise_ws("await _ensureMessagesLoaded(sid, {force:_keepStaleUntilLoaded, loadGeneration:_loadGeneration});")
    assert ensure_call in norm, (
        "loadSession() must pass generation into _ensureMessagesLoaded() for stale-owner checks"
    )
    assert (
        "showToast('Failed to load session" in LOAD_SESSION_SRC
        or "showToast('Failed to load conversation messages" in LOAD_SESSION_SRC
    ), "loadSession() should preserve toast-based failure paths"


def test_ensure_messages_loaded_ownership_guard_pre_and_post_await():
    body = ENSURE_MESSAGES_LOADED_SRC
    assert "_loadSessionGeneration" in body, "_ensureMessagesLoaded should read generation"
    assert "const _loadGeneration = Number.isFinite(opts.loadGeneration) ? Number(opts.loadGeneration) : null" in body
    norm = _normalise_ws(body)
    assert (
        "_loadGeneration===null||_loadSessionGeneration===_loadGeneration" in norm
    ), "_ensureMessagesLoaded must compare generation token"
    assert norm.count("if(!_ownsLoad())return;") >= 2, (
        "_ensureMessagesLoaded needs pre/post await ownership guards"
    )
    assert "_loadGeneration" in body, "_ensureMessagesLoaded should read generation from opts"


_NODE_SCRIPT_TEMPLATE = r'''
import assert from 'node:assert/strict';

function makeHarness() {
  const apiCalls = [];
  const queue = [];
  const pending = [];
  function enqueue(url, value, mode="resolve") {
    const defer = { url: String(url), value, mode, resolved: false };
    defer.promise = new Promise((resolve, reject) => {
      defer._resolve = resolve;
      defer._reject = reject;
    });
    queue.push(defer);
    return defer;
  }

  async function api(url) {
    apiCalls.push(String(url));
    const entry = queue.shift();
    if (!entry) {
      throw new Error('Unexpected API call: ' + String(url));
    }
    if (entry.url !== String(url)) {
      throw new Error('API order mismatch, expected ' + entry.url + ', got ' + String(url));
    }
    pending.push(entry);
    return entry.promise;
  }

  return { api, apiCalls, enqueue, pending };
}

function snapshotState() {
  return {
    sid: S.session && S.session.session_id,
    messages: Array.isArray(S.messages) ? S.messages.map((m) => (m && m.role ? String(m.content || '') : null)).filter(Boolean) : [],
    toolCalls: Array.isArray(S.toolCalls) ? S.toolCalls.slice() : [],
    truncated: _messagesTruncated,
    oldestIdx: _oldestIdx,
    loadingSid: _loadingSessionId,
    loadingGeneration: _loadSessionGeneration,
    msgInner: _msgInner.innerHTML,
    toastCalls: toastCalls.slice(),
    rearmCalls,
    apiCalls: apiHost.apiCalls.slice(),
    clearHintCalls,
    visibleCacheClears,
    liveCardClears,
    toolSyncCalls,
  };
}

function createEnvironment() {
  _composerDraftPostPromisesBySid.clear();
  globalThis.INFLIGHT = {};
  globalThis.S = {
    session: { session_id: 'sid-init', message_count: 0 },
    messages: [{ role: 'assistant', content: 'seed' }],
    toolCalls: [],
    pendingFiles: [],
    busy: false,
    activeStreamId: null,
  };
  globalThis._loadingSessionId = null;
  globalThis._loadingOlder = false;
  globalThis._loadSessionGeneration = 0;
  globalThis._pendingCarryForwardSnapshot = null;
  globalThis._messagesTruncated = false;
  globalThis._oldestIdx = 0;
  globalThis._messageRenderWindowSize = 0;
  globalThis._messageReloadLimitForSession = () => 2;
  // sessions.js module-level const, referenced by _ensureMessagesLoaded's
  // boundedReloadLimit ceiling check (#6152/#6154). Not one of the extracted
  // functions, so define it in the harness (matching the real value) or the
  // reload-width path resolves it as undefined -> boundedReloadLimit=null ->
  // the fetch URL drops msg_limit/expand_renderable and mismatches the
  // enqueued buildMessageUrl(), stalling the ordered api() harness.
  globalThis._MSG_LIMIT_MAX = 500;
  // #6177: _msgLimitMax is a module-scope `let` (live server-advertised ceiling,
  // defaulting to _MSG_LIMIT_MAX). It's read by _ensureMessagesLoaded's
  // boundedReloadLimit and _loadOlderMessages's useBeforePaging; the harness
  // injects only the extracted functions, not module-level lets, so define it
  // here or those reads resolve undefined -> wrong fetch URL -> ordered-api stall.
  globalThis._msgLimitMax = 500;
  globalThis._currentMessageRenderWindowSize = () => 1;
  globalThis._messageRenderableMessageCount = () => 2;

  globalThis._rearmActiveSessionStream = () => { rearmCalls += 1; };
  globalThis.stopApprovalPolling = () => {};
  globalThis.hideApprovalCard = () => {};
  globalThis.stopSessionStream = () => {};
  globalThis._yoloEnabled = false;
  globalThis._updateYoloPill = () => {};
  globalThis.stopClarifyPolling = () => {};
  globalThis.hideClarifyCard = () => {};
  globalThis._saveComposerDraftNow = () => Promise.resolve();
  globalThis._sessionProfileMismatchFromError = () => null;
  globalThis._switchProfileForSessionLoad = async () => {};
  globalThis._clearSameSessionForceReloadHint = () => { clearHintCalls += 1; };
  globalThis._setSessionViewedCount = () => {};
  // #4946: loadSession() now routes its viewed-count/unread clear through
  // _acknowledgeSessionVisit(). This harness exercises cross-session load
  // ordering + stale-reject, not unread-dot state, so stub it (and its
  // same-session-guard predicate) to no-ops — mirroring the pre-existing
  // _setSessionViewedCount / _clearSessionCompletionUnread stubs it replaced.
  globalThis._acknowledgeSessionVisit = () => {};
  globalThis._sessionVisitHasUnreadState = () => false;
  globalThis.scheduleTodosRefresh = () => {};
  globalThis.startSessionStream = () => {};
  globalThis.syncTopbar = () => {};
  globalThis._captureSameSessionForceReloadHint = () => {};
  globalThis._resolveSessionModelForDisplaySoon = () => {};
  globalThis._setSessionCompletionUnread = () => {};
  globalThis._clearSessionCompletionUnread = () => {};
  globalThis._setActiveSessionUrl = () => {};
  globalThis._deferWorkspaceRefreshForSession = () => {};
  globalThis._sessionListRender = () => {};
  globalThis._setSessionToolset = () => {};
  globalThis._applyPendingSessionModelForSession = () => {};
  globalThis.populateModelDropdown = () => {};
  globalThis._deferSessionSideEffect = (sid, fn) => Promise.resolve(fn());
  globalThis._hydrateTodosFromSession = () => {};
  globalThis._resolveLineage = () => {};
  globalThis._clearPendingSelections = () => {};
  globalThis._clearQueueCardDisplay = () => {};
  globalThis._syncTodosForSession = () => {};
  globalThis._clearAllTodosFromSession = () => {};
  globalThis._setSessionModelFromSession = () => {};
  globalThis._clearEmptyComposerModelOverride = () => {};
  globalThis._deferSessionProfileSwitch = () => {};
  globalThis._resolveSessionSideEffect = () => {};


  globalThis._clearMessageCache = () => {};
  globalThis._syncToolCallsForLoadedMessages = (msgs, toolCalls) => {
    toolSyncCalls += 1;
    S.toolCalls = [];
    if (Array.isArray(toolCalls)) {
      S.toolCalls = toolCalls.map((tc) => ({ ...tc, done: true }));
    }
  };
  globalThis.clearVisibleMessageRowCache = () => { visibleCacheClears += 1; };
  globalThis.clearLiveToolCards = () => { liveCardClears += 1; };

  globalThis._syncCtxIndicator = () => {};
  globalThis._renderPendingPromptsForActiveSession = () => {};
  globalThis._restoreComposerDraft = () => {};
  globalThis.renderSessionArtifacts = () => {};
  globalThis.renderMessages = () => {};
  globalThis._checkAndShowHandoffHint = () => {};
  globalThis._hideHandoffHint = () => {};
  globalThis._isMessagingSession = () => true;
  globalThis._clearDeferredActiveSessionExternalRefresh = () => {};

  globalThis.setStatus = () => {};
  globalThis.setComposerStatus = () => {};
  globalThis.setBusy = () => {};
  globalThis.updateSendBtn = () => {};
  globalThis.updateQueueBadge = () => {};
  globalThis.startApprovalPolling = () => {};
  globalThis.startClarifyPolling = () => {};
  globalThis._fetchYoloState = () => {};

  globalThis._resolveSessionIdFromSidebarLineage = (sid) => sid;
  globalThis._resolveSessionLineage = (sid) => sid;

  globalThis._messageReloadLimitForSession = () => 2;

  globalThis._msgInner = { innerHTML: 'INIT_LOADING' };
  const _msgInput = { value: '' };
  globalThis.$ = (id) => {
    if (id === 'msgInner') return _msgInner;
    if (id === 'msg') return _msgInput;
    return null;
  };

  globalThis.autoResize = () => {};
  globalThis.showToast = (msg) => {
    toastCalls.push(String(msg));
  };

  globalThis.window = {};
  globalThis.history = { replaceState: () => {} };
  globalThis.localStorage = {
    removeItem: () => {},
    setItem: () => {},
    getItem: () => null,
  };
  globalThis._appRootPath = () => '/';

  rearmCalls = 0;
  clearHintCalls = 0;
  visibleCacheClears = 0;
  liveCardClears = 0;
  toolSyncCalls = 0;
  toastCalls = [];
}

let rearmCalls = 0;
let clearHintCalls = 0;
let visibleCacheClears = 0;
let liveCardClears = 0;
let toolSyncCalls = 0;
let toastCalls = [];

// Source under test
let _draftSaveTimer = null;
let _draftSaveTimerSid = null;
let _draftSaveTimerPayload = null;
let _draftSaveTimerRev = 0;
const _composerDraftRevBySid = new Map();
const _composerDraftLastPayloadBySid = new Map();
const _DRAFT_SAVE_DELAY_MS = 400;
const NEW_CHAT_DRAFT_SESSION_KEY = 'hermes-new-chat-draft-session';
const _COMPOSER_DRAFT_RESTORE_SUPPRESS_MS = 30000;
const _composerDraftKnownPayloadSessions = new Set();
const _composerDraftRestoreSuppressedUntilBySid = new Map();
const _composerDraftLocalFallbackBySid = new Map();
const _composerDraftLatestIntentSequenceBySid = new Map();
const _composerDraftPostPromisesBySid = new Map();
let _composerDraftIntentSequence = 0;
__COMPOSER_DRAFT_HELPERS_SRC__
__INFLIGHT_HAS_VISIBLE_STATE_SRC__
__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__
__MERGE_PENDING_SESSION_MESSAGE_SRC__
__LOAD_SESSION_SRC__
__ENSURE_MESSAGES_LOADED_SRC__

async function waitForQueued(apiHost, url) {
  const target = String(url);
  while (!apiHost.pending.some((entry) => entry.url === target)) {
    await Promise.resolve();
  }
}

const API_BEACON_META = {
  session: {
    session_id: 'sid-beacon',
    message_count: 12,
    active_stream_id: null,
    resolve_model: 'qwen/qwq-32b-instruct',
  },
};

const API_BEACON_MSGS = {
  session: {
    session_id: 'sid-beacon',
    _messages_truncated: true,
    _messages_offset: 7,
    messages: [{ role: 'assistant', content: 'stale-beacon-transcript' }],
    message_count: 12,
    tool_calls: [{ name: 'tool-beacon-stale' }],
  },
};

const API_BEACON_INFLIGHT_STATE = {
  messages: [
    {
      role: 'assistant',
      content: 'beacon-inflight-tail',
      _live: true,
    },
  ],
  uploaded: [],
  toolCalls: [{ name: 'tool-beacon-inflight' }],
};

const API_ATLAS_META = {
  session: {
    session_id: 'sid-atlas',
    message_count: 21,
    active_stream_id: null,
    resolve_model: 'qwen/qwq-32b-instruct',
  },
};

const API_ATLAS_MSGS = {
  session: {
    session_id: 'sid-atlas',
    _messages_truncated: false,
    _messages_offset: 98,
    messages: [{ role: 'assistant', content: 'new-active-transcript' }],
    message_count: 21,
    tool_calls: [{ name: 'tool-atlas' }],
  },
};

const API_ATLAS_RELOAD_META = {
  session: {
    session_id: 'sid-atlas',
    message_count: 31,
    active_stream_id: null,
    resolve_model: 'qwen/qwq-32b-instruct',
  },
};

const API_ATLAS_RELOAD_MSGS = {
  session: {
    session_id: 'sid-atlas',
    _messages_truncated: true,
    _messages_offset: 33,
    messages: [{ role: 'assistant', content: 'reloaded-active-transcript' }],
    message_count: 31,
    tool_calls: [{ name: 'tool-atlas-new' }],
  },
};

function buildMessageUrl(sid, mode, suffix='') {
  const base = `/api/session?session_id=${encodeURIComponent(sid)}&messages=${mode}&resolve_model=0`;
  if (mode === 0) return base;
  return `${base}&msg_limit=${_messageReloadLimitForSession()}&expand_renderable=1${suffix}`;
}

function makeCrossSessionCalls(apiHost) {
  return {
    beaconMeta: apiHost.enqueue(buildMessageUrl('sid-beacon', 0)),
    beaconMsgs: apiHost.enqueue(buildMessageUrl('sid-beacon', 1)),
    atlasMeta: apiHost.enqueue(buildMessageUrl('sid-atlas', 0)),
    atlasMsgs: apiHost.enqueue(buildMessageUrl('sid-atlas', 1)),
  };
}

function runCrossSessionOrderingBase({seedBeaconInflight, resolveBeaconMsgsBeforeAtlasMeta}) {
  createEnvironment();
  if (seedBeaconInflight) {
    INFLIGHT['sid-beacon'] = JSON.parse(JSON.stringify(API_BEACON_INFLIGHT_STATE));
  }

  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;

  const calls = makeCrossSessionCalls(apiHost);

  const first = loadSession('sid-beacon', { force: true });
  return (async () => {
    await waitForQueued(apiHost, calls.beaconMeta.url);
    calls.beaconMeta._resolve(API_BEACON_META);

    await waitForQueued(apiHost, calls.beaconMsgs.url);
    const second = loadSession('sid-atlas', { force: true });
    await waitForQueued(apiHost, calls.atlasMeta.url);

    if (resolveBeaconMsgsBeforeAtlasMeta) {
      calls.beaconMsgs._resolve(API_BEACON_MSGS);
      // Wait for the stale first load continuation to process so we can continue the
      // Atlas path from a clearly stale state.
      await first;
      calls.atlasMeta._resolve(API_ATLAS_META);
      await waitForQueued(apiHost, calls.atlasMsgs.url);
    } else {
      calls.atlasMeta._resolve(API_ATLAS_META);
      await waitForQueued(apiHost, calls.atlasMsgs.url);
      calls.beaconMsgs._resolve(API_BEACON_MSGS);
    }

    calls.atlasMsgs._resolve(API_ATLAS_MSGS);
    await Promise.all([first, second]);

    return {
      finalSid: S.session && S.session.session_id,
      messages: snapshotState().messages,
      toolCalls: snapshotState().toolCalls,
      truncated: snapshotState().truncated,
      oldestIdx: snapshotState().oldestIdx,
      msgInner: snapshotState().msgInner,
      toastCalls: snapshotState().toastCalls,
      apiCalls: snapshotState().apiCalls,
      loadingSid: snapshotState().loadingSid,
      loadingGeneration: snapshotState().loadingGeneration,
      rearmCalls: snapshotState().rearmCalls,
    };
  })();
}

async function runCrossSessionOrdering() {
  return {
    scenario: 'cross-session-ordering',
    ...(await runCrossSessionOrderingBase({ seedBeaconInflight: true, resolveBeaconMsgsBeforeAtlasMeta: false })),
  };
}

async function runObservedIdleCrossSessionOrdering() {
  return {
    scenario: 'observed-idle-cross-session-ordering',
    ...(await runCrossSessionOrderingBase({ seedBeaconInflight: false, resolveBeaconMsgsBeforeAtlasMeta: true })),
  };
}

async function runStaleRejectedIdleCatch() {
  createEnvironment();
  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;

  S.session = { session_id: 'sid-atlas', message_count: 0 };

  const calls = {
    firstMeta: apiHost.enqueue(buildMessageUrl('sid-atlas', 0)),
    firstMsgs: apiHost.enqueue(buildMessageUrl('sid-atlas', 1)),
    secondMeta: apiHost.enqueue(buildMessageUrl('sid-atlas', 0)),
    secondMsgs: apiHost.enqueue(buildMessageUrl('sid-atlas', 1)),
  };

  const first = loadSession('sid-atlas', { force: true });
  calls.firstMeta._resolve(API_ATLAS_META);

  // Ensure the first load has entered the messages fetch and owns the pending API
  // call before the superseding same-session load begins.
  await waitForQueued(apiHost, calls.firstMsgs.url);

  const second = loadSession('sid-atlas', { force: true });

  // The stale first request rejects while the second newer request is in flight.
  calls.firstMsgs._reject(new Error('owner lost while load was in-flight'));
  calls.secondMeta._resolve(API_ATLAS_RELOAD_META);
  calls.secondMsgs._resolve(API_ATLAS_RELOAD_MSGS);

  await Promise.all([first, second]);

  return {
    scenario: 'stale-idle-catch',
    finalSid: S.session && S.session.session_id,
    messages: snapshotState().messages,
    toolCalls: snapshotState().toolCalls,
    truncated: snapshotState().truncated,
    oldestIdx: snapshotState().oldestIdx,
    msgInner: snapshotState().msgInner,
    toastCalls: snapshotState().toastCalls,
    apiCalls: snapshotState().apiCalls,
    loadingSid: snapshotState().loadingSid,
    loadingGeneration: snapshotState().loadingGeneration,
    rearmCalls: snapshotState().rearmCalls,
  };
}

async function runDraftSaveFailureNavigation() {
  createEnvironment();
  _composerDraftKnownPayloadSessions.clear();
  _composerDraftRestoreSuppressedUntilBySid.clear();
  _composerDraftLocalFallbackBySid.clear();
  const sourceSession = { session_id: 'sid-source', message_count: 1, composer_draft: null };
  S.session = sourceSession;
  const makeFile = (name, size, type) => typeof File === 'function'
    ? new File([new Uint8Array(size)], name, {type}) : {name, size, type};
  const oldFile = makeFile('old.txt', 12, 'text/plain');
  const newerFile = makeFile('newer.txt', 14, 'text/plain');
  const backgroundFile = makeFile('steer.txt', 16, 'text/plain');
  S.pendingFiles = [oldFile];
  $('msg').value = 'captured old draft';
  const draftRequests = [];
  const apiCalls = [];
  const streamCalls = [];
  const failureReports = [];
  const realFailureHandler = _handleComposerDraftPostFailure;
  _handleComposerDraftPostFailure = (error, sid, text, files) => {
    failureReports.push({sid, text, files:files.map((file)=>({name:file.name,size:file.size,type:file.type}))});
    return realFailureHandler(error, sid, text, files);
  };
  globalThis.startSessionStream = (sid) => streamCalls.push(sid);
  globalThis.api = (path, options={}) => {
    apiCalls.push(String(path));
    if (path === '/api/profile/switch') {
      const body = JSON.parse(options.body);
      return Promise.resolve({active:body.name, is_default:body.name==='default'});
    }
    if (path === '/api/session/draft') {
      const body = JSON.parse(options.body);
      return new Promise((resolve, reject) => draftRequests.push({body, resolve, reject}));
    }
    const query = new URL(path, 'http://example.test').searchParams;
    const sid = query.get('session_id');
    if (query.get('messages') === '0') {
      const composer_draft = sid === 'sid-source'
        ? {text:'stale server draft', files:[{name:'server-stale.txt'}]}
        : sid === 'sid-background'
          ? {text:'stale background server draft', files:[]}
          : {text:'', files:[]};
      return Promise.resolve({session: {session_id:sid, message_count:1, active_stream_id:null, composer_draft}});
    }
    if (query.get('messages') === '1') {
      return Promise.resolve({session: {
        session_id:sid,
        messages:[{role:'user', content:'destination transcript'}],
        message_count:1,
        _messages_truncated:false,
        _messages_offset:0,
        tool_calls:[],
      }});
    }
    throw new Error('Unexpected API call: ' + path);
  };
  const observed = (promise) => promise.then(
    () => ({error:null}),
    (error) => ({error:String(error && error.message || error)}),
  );
  const first = observed(loadSession('sid-stale-destination'));
  assert.strictEqual(draftRequests.length, 1);
  $('msg').value = 'newer composer draft';
  S.pendingFiles = [newerFile];
  const second = observed(loadSession('sid-live-destination'));
  const staleError = new Error('stale draft POST rejected');
  staleError.status = 503;
  draftRequests[0].reject(staleError);
  const firstResult = await first;
  assert.strictEqual(draftRequests.length, 2);
  const afterStale = {
    loadingSid:_loadingSessionId,
    activeSid:S.session && S.session.session_id,
    sourceDraft:sourceSession.composer_draft,
    pendingDraft:(() => { const d=_composerDraftLocalFallbackBySid.get('sid-source'); return d&&{text:d.text,files:d.files.map((file)=>({name:file.name,size:file.size,type:file.type})),sameFile:d.files[0]===newerFile}; })(),
  };
  const currentError = new Error('current draft POST rejected');
  currentError.status = 503;
  draftRequests[1].reject(currentError);
  const secondResult = await second;
  const destinationNavigation = {
    activeSid:S.session && S.session.session_id,
    loadingSid:_loadingSessionId,
    streamCalls:streamCalls.slice(),
  };
  const returnedToSource = observed(loadSession('sid-source'));
  const returnResult = await returnedToSource;
  const sourceRestored = {
    text:$('msg').value,
    files:S.pendingFiles.map((file)=>({name:file.name,size:file.size,type:file.type})),
    sameFile:S.pendingFiles[0]===newerFile,
  };
  const backgroundWrite = observed(_saveComposerDraftNow(
    'sid-background', 'background steer draft', [backgroundFile],
  ).catch((error) => _handleComposerDraftPostFailure(
    error, 'sid-background', 'background steer draft', [backgroundFile],
  )));
  assert.strictEqual(draftRequests.length, 3);
  draftRequests[2].reject(new Error('background steer draft rejected'));
  const backgroundWriteResult = await backgroundWrite;
  await _switchProfileForSessionLoad('alternate');
  const backgroundFallbackAcrossProfile = (() => {
    const draft = _composerDraftLocalFallbackBySid.get('sid-background');
    return !!draft && draft.files[0] === backgroundFile;
  })();
  await _switchProfileForSessionLoad('default');
  const backgroundLoad = observed(loadSession('sid-background'));
  assert.strictEqual(draftRequests.length, 4);
  draftRequests[3].resolve({session_id:'sid-source'});
  const backgroundResult = await backgroundLoad;
  const backgroundRestored = {
    text:$('msg').value,
    files:S.pendingFiles.map((file)=>({name:file.name,size:file.size,type:file.type})),
    sameFile:S.pendingFiles[0]===backgroundFile,
  };
  const completedNavigation = {
    activeSid:S.session && S.session.session_id,
    loadingSid:_loadingSessionId,
    streamCalls:streamCalls.slice(),
  };
  const supersedingFailureReports = failureReports.slice();

  // A single failed outgoing save must survive a real A→B→A navigation even
  // when A's server response still carries an older draft.
  _handleComposerDraftPostFailure = realFailureHandler;
  createEnvironment();
  _composerDraftKnownPayloadSessions.clear();
  _composerDraftRestoreSuppressedUntilBySid.clear();
  _composerDraftLocalFallbackBySid.clear();
  const revisitFile = makeFile('revisit.txt', 21, 'text/plain');
  const revisitRequests = [];
  S.session = {session_id:'sid-revisit-owner', message_count:1, profile:'default', composer_draft:null};
  S.pendingFiles = [revisitFile];
  $('msg').value = 'failed outgoing exact draft';
  globalThis.api = (path, options={}) => {
    if (path === '/api/session/draft') {
      const body = JSON.parse(options.body);
      return new Promise((resolve, reject) => {
        const request = {body, resolve, reject};
        revisitRequests.push(request);
        if (revisitRequests.length > 1) resolve({session_id:body.session_id});
      });
    }
    const query = new URL(path, 'http://example.test').searchParams;
    const sid = query.get('session_id');
    if (query.get('messages') === '0') return Promise.resolve({session:{
      session_id:sid, message_count:1, active_stream_id:null,
      composer_draft:sid==='sid-revisit-owner'
        ? {text:'older server draft',files:[{name:'old-server.txt'}]} : {text:'',files:[]},
    }});
    if (query.get('messages') === '1') return Promise.resolve({session:{
      session_id:sid, messages:[{role:'user',content:'transcript'}], message_count:1,
      _messages_truncated:false, _messages_offset:0, tool_calls:[],
    }});
    throw new Error('Unexpected API call: ' + path);
  };
  const failedOutgoing = observed(loadSession('sid-revisit-destination'));
  assert.strictEqual(revisitRequests.length, 1);
  revisitRequests[0].reject(new Error('outgoing save rejected'));
  const failedOutgoingResult = await failedOutgoing;
  const revisitResult = await observed(loadSession('sid-revisit-owner'));
  const revisitedDraft = {
    error:revisitResult.error,
    text:$('msg').value,
    files:S.pendingFiles.map((file)=>({name:file.name,size:file.size,type:file.type})),
    sameFile:S.pendingFiles[0]===revisitFile,
  };

  createEnvironment();
  _composerDraftLocalFallbackBySid.clear();
  _composerDraftRestoreSuppressedUntilBySid.clear();
  S.session = {session_id:'sid-empty-local-clear', message_count:1};
  $('msg').value = 'stale server draft';
  _setComposerDraftLocalIntent('sid-empty-local-clear', '', []);
  _restoreComposerDraft(
    {text:'stale server draft', files:[]}, 'sid-empty-local-clear',
  );
  const emptyLocalDraftRestored = $('msg').value;

  createEnvironment();
  _composerDraftLocalFallbackBySid.clear();
  const activeRefreshFile = makeFile('active-refresh.txt', 19, 'text/plain');
  S.session = {session_id:'sid-file-only-refresh', message_count:1};
  S.pendingFiles = [activeRefreshFile];
  _restoreComposerDraft(
    {text:'', files:[]}, 'sid-file-only-refresh', {preserveActiveInput:true},
  );
  const fileOnlyRefreshRetained = S.pendingFiles[0] === activeRefreshFile;

  async function newerTargetBlocksReplay(newerText) {
    createEnvironment();
    _composerDraftLocalFallbackBySid.clear();
    _composerDraftLatestIntentSequenceBySid.clear();
    _composerDraftKnownPayloadSessions.clear();
    const targetFile = makeFile('target.txt', 22, 'text/plain');
    const isClear = newerText === '';
    S.session = {
      session_id:'sid-target', message_count:1,
      composer_draft:isClear ? {text:'older target draft',files:[]} : null,
    };
    if (isClear) _composerDraftKnownPayloadSessions.add('sid-target');
    const requests = [];
    globalThis.api = (_path, options) => {
      const body = JSON.parse(options.body);
      return new Promise((resolve, reject) => requests.push({body, resolve, reject}));
    };
    const oldSave = _saveComposerDraftNow('sid-parent', 'older parent draft', []);
    const oldOutcome = oldSave.catch((error) => error);
    const targetFiles = isClear ? [] : [targetFile];
    const targetSave = _saveComposerDraftNow('sid-target', newerText, targetFiles);
    assert.strictEqual(requests.length, 2);
    const moved = new Error('Session moved');
    moved.status = 409;
    moved.body = JSON.stringify({code:'session_moved',session_id:'sid-target'});
    requests[0].reject(moved);
    const staleOutcome = await oldOutcome;
    const targetIntent = _composerDraftLocalFallbackBySid.get('sid-target');
    const sameFile = isClear || targetIntent.files[0] === targetFile;
    requests[1].resolve({session_id:'sid-target'});
    await targetSave;
    return {
      calls:requests.map((request)=>request.body),
      staleStatus:Number(staleOutcome && staleOutcome.status),
      targetDraft:S.session.composer_draft,
      sameFile,
    };
  }
  const newerTargetDraftWins = await newerTargetBlocksReplay('newer target draft');
  const newerTargetClearWins = await newerTargetBlocksReplay('');

  createEnvironment();
  _composerDraftLocalFallbackBySid.clear();
  _composerDraftLatestIntentSequenceBySid.clear();
  _composerDraftRestoreSuppressedUntilBySid.clear();
  S.session = {session_id:'sid-clear-target', message_count:1, composer_draft:{text:'old',files:[]}};
  const clearRequests = [];
  globalThis.api = (_path, options) => {
    const body = JSON.parse(options.body);
    return new Promise((resolve, reject) => clearRequests.push({body, resolve, reject}));
  };
  const clearSave = _clearComposerDraft('sid-clear-parent', 'submitted text', []);
  assert.strictEqual(clearRequests.length, 1);
  const clearMoved = new Error('Session moved');
  clearMoved.status = 409;
  clearMoved.body = JSON.stringify({code:'session_moved',session_id:'sid-clear-target'});
  clearRequests[0].reject(clearMoved);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(clearRequests.length, 2);
  clearRequests[1].resolve({session_id:'sid-clear-target'});
  await clearSave;
  const clearReplay = {
    sids:clearRequests.map((request)=>request.body.session_id),
    payload:clearRequests[1].body,
    targetDraft:S.session.composer_draft,
  };

  createEnvironment();
  _composerDraftLocalFallbackBySid.clear();
  _composerDraftLatestIntentSequenceBySid.clear();
  S.session = {session_id:'sid-serialized-target', message_count:1};
  const serializedRequests = [];
  let durableDraft = null;
  globalThis.api = (_path, options) => {
    const body = JSON.parse(options.body);
    durableDraft = body;
    return new Promise((resolve, reject) => serializedRequests.push({body, resolve, reject}));
  };
  const staleSave = _saveComposerDraftNow('sid-serialized-parent', 'stale parent draft', []);
  const staleOutcome = staleSave.catch((error) => error);
  assert.strictEqual(serializedRequests.length, 1);
  const serializedMoved = new Error('Session moved');
  serializedMoved.status = 409;
  serializedMoved.body = JSON.stringify({code:'session_moved',session_id:'sid-serialized-target'});
  serializedRequests[0].reject(serializedMoved);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(serializedRequests.length, 2);
  const newerSave = _saveComposerDraftNow('sid-serialized-target', 'newer target draft', []);
  const callsBeforeReplaySettles = serializedRequests.length;
  serializedRequests[1].resolve({session_id:'sid-serialized-target'});
  await new Promise((resolve) => setImmediate(resolve));
  const callsAfterReplaySettles = serializedRequests.length;
  if (serializedRequests[2]) serializedRequests[2].resolve({session_id:'sid-serialized-target'});
  await Promise.all([staleOutcome, newerSave]);
  const serializedReplayOrdering = {
    calls:serializedRequests.map((request)=>request.body),
    callsBeforeReplaySettles,
    callsAfterReplaySettles,
    durableDraft,
  };

  return {
    firstError:firstResult.error,
    secondError:secondResult.error,
    destinationNavigation,
    afterStale,
    activeSid:completedNavigation.activeSid,
    loadingSid:completedNavigation.loadingSid,
    streamCalls:completedNavigation.streamCalls,
    apiCalls,
    draftRequests:draftRequests.map((entry) => entry.body),
    failureReports,
    sourceDraft:sourceSession.composer_draft,
    returnError:returnResult.error,
    sourceRestored,
    backgroundWriteError:backgroundWriteResult.error,
    backgroundError:backgroundResult.error,
    backgroundFallbackAcrossProfile,
    backgroundRestored,
    failedOutgoingError:failedOutgoingResult.error,
    revisitedDraft,
    emptyLocalDraftRestored,
    fileOnlyRefreshRetained,
    newerTargetDraftWins,
    newerTargetClearWins,
    clearReplay,
    serializedReplayOrdering,
    supersedingFailureReports,
  };
}

async function runAll() {
  return {
    crossSessionOrdering: await runCrossSessionOrdering(),
    observedIdleCrossSessionOrdering: await runObservedIdleCrossSessionOrdering(),
    staleIdleCatch: await runStaleRejectedIdleCatch(),
    draftSaveFailureNavigation: await runDraftSaveFailureNavigation(),
  };
}

runAll()
  .then((r) => console.log(JSON.stringify(r)))
  .catch((err) => {
    console.error('NODE_ERROR', err && err.stack || err);
    process.exit(1);
  });
'''


def _run_node(script: str, tmp_path: Path) -> dict:
    assert NODE is not None, "node is required"
    script_path = tmp_path / "cross-session-message-load-isolation.mjs"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(script_path)],
        cwd=str(REPO),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, f"node produced no parseable output\nstdout={completed.stdout}\nstderr={completed.stderr}"
    return json.loads(output_lines[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_loadsession_cross_session_ordering_and_stale_reject_behavior(tmp_path):
    script = (
        _NODE_SCRIPT_TEMPLATE.replace(
            "__INFLIGHT_HAS_VISIBLE_STATE_SRC__", INFLIGHT_HAS_VISIBLE_STATE_SRC
        )
        .replace(
            "__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__", SELECT_LIVE_RECOVERY_INFLIGHT_SRC
        )
        .replace(
            "__MERGE_PENDING_SESSION_MESSAGE_SRC__", MERGE_PENDING_SESSION_MESSAGE_SRC
        )
        .replace("__COMPOSER_DRAFT_HELPERS_SRC__", COMPOSER_DRAFT_HELPERS_SRC)
        .replace("__LOAD_SESSION_SRC__", LOAD_SESSION_SRC)
        .replace("__ENSURE_MESSAGES_LOADED_SRC__", ENSURE_MESSAGES_LOADED_SRC)
    )
    body = _run_node(script, tmp_path)

    cross = body["crossSessionOrdering"]
    stale = body["staleIdleCatch"]
    observed = body["observedIdleCrossSessionOrdering"]

    def _assert_atlas_wins(session_result, *, label):
        assert session_result["finalSid"] == "sid-atlas", f"{label}: stale overlap should end on Atlas session"
        assert session_result["messages"] == ["new-active-transcript"], (
            f"{label}: stale Beacon transcript must not replace Atlas transcript"
        )
        assert session_result["toolCalls"] == [{"name": "tool-atlas", "done": True}], (
            f"{label}: Atlas tool summary must apply on fresh load"
        )
        assert session_result["truncated"] is False and session_result["oldestIdx"] == 98, (
            f"{label}: Atlas metadata should remain the active state"
        )

    # 1) Cross-session ordering: old (Beacon) loads first, but user advances to Atlas.
    assert cross["apiCalls"][0] == "/api/session?session_id=sid-beacon&messages=0&resolve_model=0", (
        "first API call should target old session's metadata"
    )
    assert cross["apiCalls"][1] == "/api/session?session_id=sid-beacon&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1", (
        "beacon transcript request should queue before atlas metadata resolves"
    )
    assert cross["apiCalls"][2] == "/api/session?session_id=sid-atlas&messages=0&resolve_model=0", (
        "second API call should target atlas metadata while stale beacon messages are in flight"
    )
    assert cross["apiCalls"][3] == "/api/session?session_id=sid-atlas&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1", (
        "atlas should still fetch a transcript while beacon was stale"
    )
    assert cross["apiCalls"].count("/api/session?session_id=sid-beacon&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1") == 1, (
        "stale overlap should still issue the Beacon transcript call, but it must not win"
    )
    _assert_atlas_wins(cross, label="cross-session-ordering")

    # 2) Observed idle-path race with no INFLIGHT: stale Beacon transcript returns
    #    before Atlas metadata, but ownership guard must still force Atlas fetch+swap.
    assert observed["apiCalls"][0] == "/api/session?session_id=sid-beacon&messages=0&resolve_model=0", (
        "idle-path race should start from old Beacon metadata"
    )
    assert observed["apiCalls"][1] == "/api/session?session_id=sid-beacon&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1", (
        "Beacon transcript call should remain queued before Atlas metadata under observed race"
    )
    assert observed["apiCalls"][2] == "/api/session?session_id=sid-atlas&messages=0&resolve_model=0", (
        "Atlas metadata must start while Beacon continuation returns stale"
    )
    assert observed["apiCalls"][3] == "/api/session?session_id=sid-atlas&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1", (
        "Atlas transcript request must still issue despite stale Beacon return"
    )
    assert observed["apiCalls"].count("/api/session?session_id=sid-beacon&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1") == 1, (
        "stale Beacon transcript should occur once in observed race"
    )
    assert observed["apiCalls"].count("/api/session?session_id=sid-atlas&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1") == 1, (
        "Atlas transcript must be issued once once stale Beacon is processed first"
    )
    _assert_atlas_wins(observed, label="observed-idle-cross-session-ordering")
    assert observed["toastCalls"] == [], "stale Beacon return in idle-path race should not show toast"

    # 3) Stale rejected idle-branch catch must be ownership-guarded and not mutate shared pane.
    assert stale["messages"] == ["reloaded-active-transcript"], "stale catch must not keep stale transcript"
    assert stale["toolCalls"] == [{"name": "tool-atlas-new", "done": True}], "stale catch must not overwrite tool state"
    assert stale["truncated"] is True and stale["oldestIdx"] == 33, "active owner should install latest metadata"
    assert stale["msgInner"] == "INIT_LOADING", (
        "stale reject from superseded load must not write failure placeholder"
    )
    assert stale["toastCalls"] == [], "stale reject must not surface toast for superseded load"
    assert stale["apiCalls"].count(
        "/api/session?session_id=sid-atlas&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1"
    ) == 2, "both old and active loads should have attempted message fetch"

    assert cross["loadingSid"] is None, "load marker should be cleared after successful completion"
    assert stale["loadingSid"] is None, "load marker should be cleared after stale reject + re-owner completion"

    superseded = body["draftSaveFailureNavigation"]
    assert superseded["firstError"] is None
    assert superseded["secondError"] is None, "a failed draft POST must not reject the current destination load"
    assert superseded["afterStale"] == {
        "loadingSid":"sid-live-destination",
        "activeSid":"sid-source",
        "sourceDraft":None,
        "pendingDraft":{
            "text":"newer composer draft",
            "files":[{"name":"newer.txt", "size":14, "type":"text/plain"}],
            "sameFile":True,
        },
    }, "rejected stale navigation must retain its exact snapshot and leave the newer load owned"
    assert superseded["destinationNavigation"] == {
        "activeSid":"sid-live-destination",
        "loadingSid":None,
        "streamCalls":["sid-live-destination"],
    }, "a failed outgoing draft POST must still load and attach the destination stream"
    assert superseded["activeSid"] == "sid-background"
    assert superseded["loadingSid"] is None
    assert superseded["streamCalls"] == ["sid-live-destination", "sid-source", "sid-background"]
    assert superseded["failureReports"] == [
        {
            "sid":"sid-source",
            "text":"captured old draft",
            "files":[{"name":"old.txt", "size":12, "type":"text/plain"}],
        },
        {
            "sid":"sid-source",
            "text":"newer composer draft",
            "files":[{"name":"newer.txt", "size":14, "type":"text/plain"}],
        },
        {
            "sid":"sid-background",
            "text":"background steer draft",
            "files":[{"name":"steer.txt", "size":16, "type":"text/plain"}],
        },
    ]
    assert superseded["returnError"] is None
    assert superseded["sourceRestored"] == {
        "text":"newer composer draft",
        "files":[{"name":"newer.txt", "size":14, "type":"text/plain"}],
        "sameFile":True,
    }, "failed local draft must override stale server text and retain the original File reference on revisit"
    assert superseded["backgroundError"] is None
    assert superseded["backgroundWriteError"] is None
    assert superseded["backgroundFallbackAcrossProfile"] is True
    assert superseded["backgroundRestored"] == {
        "text":"background steer draft",
        "files":[{"name":"steer.txt", "size":16, "type":"text/plain"}],
        "sameFile":True,
    }, "the shared failure handler must retain background send/steer files for their owning session"
    assert superseded["failedOutgoingError"] is None
    assert superseded["revisitedDraft"] == {
        "error":None,
        "text":"failed outgoing exact draft",
        "files":[{"name":"revisit.txt", "size":21, "type":"text/plain"}],
        "sameFile":True,
    }, "A→B→A must restore the exact failed local text and original File over stale server state"
    assert superseded["emptyLocalDraftRestored"] == "", (
        "an empty failed local clear must override stale server draft text"
    )
    assert superseded["fileOnlyRefreshRetained"] is True, (
        "same-session force refresh must retain the exact active File reference"
    )
    assert len(superseded["newerTargetDraftWins"]["calls"]) == 2
    assert superseded["newerTargetDraftWins"]["calls"][0]["session_id"] == "sid-parent"
    assert superseded["newerTargetDraftWins"]["calls"][1]["session_id"] == "sid-target"
    assert superseded["newerTargetDraftWins"]["calls"][1]["text"] == "newer target draft"
    assert superseded["newerTargetDraftWins"]["staleStatus"] == 409
    assert superseded["newerTargetDraftWins"]["targetDraft"]["text"] == "newer target draft"
    assert superseded["newerTargetDraftWins"]["sameFile"] is True
    assert len(superseded["newerTargetClearWins"]["calls"]) == 2
    assert superseded["newerTargetClearWins"]["calls"][0]["session_id"] == "sid-parent"
    # #7855: a clear posts an explicit empty continuation token so the stored
    # token is dropped with the draft it belonged to.
    assert superseded["newerTargetClearWins"]["calls"][1] == {
        "session_id":"sid-target", "text":"", "files":[], "goal_continuation_id":"",
    }
    assert superseded["newerTargetClearWins"]["staleStatus"] == 409
    assert superseded["newerTargetClearWins"]["targetDraft"] == {"text":"", "files":[]}
    assert superseded["clearReplay"]["sids"] == ["sid-clear-parent", "sid-clear-target"]
    assert superseded["clearReplay"]["payload"] == {
        "session_id":"sid-clear-target", "text":"", "files":[], "goal_continuation_id":"",
    }
    assert superseded["clearReplay"]["targetDraft"] == {"text":"", "files":[]}
    replay_order = superseded["serializedReplayOrdering"]
    assert replay_order["callsBeforeReplaySettles"] == 2
    assert replay_order["callsAfterReplaySettles"] == 3
    assert [(call["session_id"], call["text"]) for call in replay_order["calls"]] == [
        ("sid-serialized-parent", "stale parent draft"),
        ("sid-serialized-target", "stale parent draft"),
        ("sid-serialized-target", "newer target draft"),
    ]
    assert replay_order["durableDraft"]["text"] == "newer target draft"
