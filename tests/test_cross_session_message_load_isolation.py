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
PANELS_SRC = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
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

    brace_start = source.find("{", start)
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
BEGIN_SESSION_NAVIGATION_REQUEST_SRC = _extract_function(SESSIONS_SRC, "_beginSessionNavigationRequest")
SESSION_NAVIGATION_REQUEST_IS_CURRENT_SRC = _extract_function(SESSIONS_SRC, "_sessionNavigationRequestIsCurrent")
SQUASH_RUNNING_HELPERS_SRC = PANELS_SRC[
    PANELS_SRC.index("const _squashRunningSessions = new Set()") :
    PANELS_SRC.index("async function squashConversation")
]


def _normalise_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_loadsession_has_generation_token_and_forwards_to_ensure_messages_loaded():
    body = LOAD_SESSION_SRC
    assert "const _loadGeneration = _beginSessionNavigationRequest(sid)" in body, (
        "loadSession() must capture requested-navigation authority at the shared chokepoint"
    )
    assert "const _isCurrentLoad = () => _sessionNavigationRequestIsCurrent(sid,_loadGeneration)" in body
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
function makeButton() {
  const classes = new Set();
  return {
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
  };
}

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
    squashButtons: [desktopSquashButton, mobileSquashButton]
      .map((button) => button.classList.contains('squash-running')),
  };
}

function createEnvironment() {
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
  _squashRunningSessions.clear();
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
  globalThis._clearStuckSessionOnBoot = () => {};
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
  globalThis.desktopSquashButton = makeButton();
  globalThis.mobileSquashButton = makeButton();
  globalThis.$ = (id) => {
    if (id === 'msgInner') return _msgInner;
    if (id === 'msg') return _msgInput;
    if (id === 'btnSquash') return desktopSquashButton;
    if (id === 'composerMobileSquashBtn') return mobileSquashButton;
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
__INFLIGHT_HAS_VISIBLE_STATE_SRC__
__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__
__MERGE_PENDING_SESSION_MESSAGE_SRC__
__BEGIN_SESSION_NAVIGATION_REQUEST_SRC__
__SESSION_NAVIGATION_REQUEST_IS_CURRENT_SRC__
__SQUASH_RUNNING_HELPERS_SRC__
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

const API_CINDER_META = {
  session: {
    session_id: 'sid-cinder',
    message_count: 8,
    active_stream_id: null,
    resolve_model: 'qwen/qwq-32b-instruct',
  },
};

const API_CINDER_MSGS = {
  session: {
    session_id: 'sid-cinder',
    _messages_truncated: false,
    _messages_offset: 0,
    messages: [{ role: 'assistant', content: 'cinder-transcript' }],
    message_count: 8,
    tool_calls: [],
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

async function runPendingSwitchBack() {
  createEnvironment();
  S.session = JSON.parse(JSON.stringify(API_BEACON_META.session));
  S.messages = [{ role: 'assistant', content: 'visible-beacon-transcript' }];

  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;

  const calls = {
    atlasMeta: apiHost.enqueue(buildMessageUrl('sid-atlas', 0)),
    beaconMeta: apiHost.enqueue(buildMessageUrl('sid-beacon', 0)),
    beaconMsgs: apiHost.enqueue(buildMessageUrl('sid-beacon', 1)),
  };

  const pendingSwitch = loadSession('sid-atlas');
  await waitForQueued(apiHost, calls.atlasMeta.url);
  const pendingAuthority = {
    destination: _loadingSessionId,
    generation: _loadSessionGeneration,
  };

  // S.session still identifies Beacon while Atlas metadata is pending. Clicking
  // Beacon again must supersede the Atlas request instead of hitting the
  // same-session no-op.
  const switchBack = loadSession('sid-beacon');
  await waitForQueued(apiHost, calls.beaconMeta.url);
  const switchBackAuthority = {
    destination: _loadingSessionId,
    generation: _loadSessionGeneration,
  };

  // Let the superseded Atlas response return first. Its stale continuation must
  // preserve Beacon's newer destination + generation authority.
  calls.atlasMeta._resolve(API_ATLAS_META);
  await pendingSwitch;
  calls.beaconMeta._resolve(API_BEACON_META);
  await waitForQueued(apiHost, calls.beaconMsgs.url);
  calls.beaconMsgs._resolve(API_BEACON_MSGS);
  await switchBack;

  return {
    scenario: 'pending-switch-back',
    pendingAuthority,
    switchBackAuthority,
    finalSid: S.session && S.session.session_id,
    messages: snapshotState().messages,
    toolCalls: snapshotState().toolCalls,
    apiCalls: snapshotState().apiCalls,
    loadingSid: snapshotState().loadingSid,
    loadingGeneration: snapshotState().loadingGeneration,
  };
}

function seedRunningSquashOwner() {
  S.session = JSON.parse(JSON.stringify(API_BEACON_META.session));
  S.messages = [{ role: 'assistant', content: 'visible-beacon-transcript' }];
  _squashSetRunning('sid-beacon', true);
}

async function runSquashIndicatorMetadataFailure() {
  createEnvironment();
  seedRunningSquashOwner();
  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;
  const atlasMeta = apiHost.enqueue(buildMessageUrl('sid-atlas', 0));

  const navigation = loadSession('sid-atlas');
  await waitForQueued(apiHost, atlasMeta.url);
  const whilePending = snapshotState().squashButtons;
  const error = new Error('Atlas metadata failed');
  error.status = 500;
  atlasMeta._reject(error);
  await navigation;

  return {
    finalSid: S.session && S.session.session_id,
    whilePending,
    afterFailure: snapshotState().squashButtons,
    loadingSid: _loadingSessionId,
  };
}

async function runSquashIndicatorMetadataSuccess() {
  createEnvironment();
  seedRunningSquashOwner();
  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;
  const atlasMeta = apiHost.enqueue(buildMessageUrl('sid-atlas', 0));
  const atlasMsgs = apiHost.enqueue(buildMessageUrl('sid-atlas', 1));

  const navigation = loadSession('sid-atlas');
  await waitForQueued(apiHost, atlasMeta.url);
  const whilePending = snapshotState().squashButtons;
  atlasMeta._resolve(API_ATLAS_META);
  await waitForQueued(apiHost, atlasMsgs.url);
  const afterMetadataAccepted = snapshotState().squashButtons;
  atlasMsgs._resolve(API_ATLAS_MSGS);
  await navigation;

  return {
    finalSid: S.session && S.session.session_id,
    whilePending,
    afterMetadataAccepted,
    afterSuccess: snapshotState().squashButtons,
  };
}

async function runSquashIndicatorStaleMetadata() {
  createEnvironment();
  seedRunningSquashOwner();
  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;
  const atlasMeta = apiHost.enqueue(buildMessageUrl('sid-atlas', 0));
  const cinderMeta = apiHost.enqueue(buildMessageUrl('sid-cinder', 0));
  const cinderMsgs = apiHost.enqueue(buildMessageUrl('sid-cinder', 1));

  const staleNavigation = loadSession('sid-atlas');
  await waitForQueued(apiHost, atlasMeta.url);
  const currentNavigation = loadSession('sid-cinder');
  await waitForQueued(apiHost, cinderMeta.url);
  const whileBothPending = snapshotState().squashButtons;

  atlasMeta._resolve(API_ATLAS_META);
  await staleNavigation;
  const afterStaleMetadata = snapshotState().squashButtons;
  cinderMeta._resolve(API_CINDER_META);
  await waitForQueued(apiHost, cinderMsgs.url);
  const afterCurrentMetadata = snapshotState().squashButtons;
  cinderMsgs._resolve(API_CINDER_MSGS);
  await currentNavigation;

  return {
    finalSid: S.session && S.session.session_id,
    whileBothPending,
    afterStaleMetadata,
    afterCurrentMetadata,
    afterSuccess: snapshotState().squashButtons,
  };
}

async function runAll() {
  return {
    crossSessionOrdering: await runCrossSessionOrdering(),
    observedIdleCrossSessionOrdering: await runObservedIdleCrossSessionOrdering(),
    staleIdleCatch: await runStaleRejectedIdleCatch(),
    pendingSwitchBack: await runPendingSwitchBack(),
    squashIndicatorMetadataFailure: await runSquashIndicatorMetadataFailure(),
    squashIndicatorMetadataSuccess: await runSquashIndicatorMetadataSuccess(),
    squashIndicatorStaleMetadata: await runSquashIndicatorStaleMetadata(),
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
        .replace("__BEGIN_SESSION_NAVIGATION_REQUEST_SRC__", BEGIN_SESSION_NAVIGATION_REQUEST_SRC)
        .replace("__SESSION_NAVIGATION_REQUEST_IS_CURRENT_SRC__", SESSION_NAVIGATION_REQUEST_IS_CURRENT_SRC)
        .replace("__SQUASH_RUNNING_HELPERS_SRC__", SQUASH_RUNNING_HELPERS_SRC)
        .replace("__LOAD_SESSION_SRC__", LOAD_SESSION_SRC)
        .replace("__ENSURE_MESSAGES_LOADED_SRC__", ENSURE_MESSAGES_LOADED_SRC)
    )
    body = _run_node(script, tmp_path)

    cross = body["crossSessionOrdering"]
    stale = body["staleIdleCatch"]
    observed = body["observedIdleCrossSessionOrdering"]
    switch_back = body["pendingSwitchBack"]
    squash_failure = body["squashIndicatorMetadataFailure"]
    squash_success = body["squashIndicatorMetadataSuccess"]
    squash_stale = body["squashIndicatorStaleMetadata"]

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

    # 4) Pending switch-back: while Atlas metadata is unresolved, S.session still
    #    identifies Beacon. Re-selecting Beacon must supersede Atlas rather than
    #    taking the ordinary same-session no-op, and both destination + generation
    #    must remain authoritative after Atlas's stale continuation returns.
    assert switch_back["pendingAuthority"] == {"destination": "sid-atlas", "generation": 1}
    assert switch_back["switchBackAuthority"] == {"destination": "sid-beacon", "generation": 2}
    assert switch_back["finalSid"] == "sid-beacon"
    assert switch_back["messages"] == ["stale-beacon-transcript"]
    assert switch_back["toolCalls"] == [{"name": "tool-beacon-stale", "done": True}]
    assert switch_back["apiCalls"] == [
        "/api/session?session_id=sid-atlas&messages=0&resolve_model=0",
        "/api/session?session_id=sid-beacon&messages=0&resolve_model=0",
        "/api/session?session_id=sid-beacon&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1",
    ]
    assert switch_back["loadingSid"] is None
    assert switch_back["loadingGeneration"] == 2

    # 5) Exact production loadSession runtime: A owns an active squash while B's
    #    metadata is pending. Shared desktop/mobile controls continue to project
    #    the actually visible A until metadata for a current destination succeeds.
    #    Failure keeps A visible and running; stale metadata cannot project its
    #    superseded destination; accepted B/C metadata switches both controls.
    assert squash_failure == {
        "finalSid": "sid-beacon",
        "whilePending": [True, True],
        "afterFailure": [True, True],
        "loadingSid": None,
    }
    assert squash_success == {
        "finalSid": "sid-atlas",
        "whilePending": [True, True],
        "afterMetadataAccepted": [False, False],
        "afterSuccess": [False, False],
    }
    assert squash_stale == {
        "finalSid": "sid-cinder",
        "whileBothPending": [True, True],
        "afterStaleMetadata": [True, True],
        "afterCurrentMetadata": [False, False],
        "afterSuccess": [False, False],
    }

    assert cross["loadingSid"] is None, "load marker should be cleared after successful completion"
    assert stale["loadingSid"] is None, "load marker should be cleared after stale reject + re-owner completion"
