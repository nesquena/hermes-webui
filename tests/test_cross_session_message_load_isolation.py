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
MESSAGES_SRC = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
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
ATTACH_SETTLED_RUNTIME_JOURNAL_CONTROLS_SRC = _extract_function(
    SESSIONS_SRC, "_attachSettledRuntimeJournalControls"
)
MESSAGE_IDENTITY_KEY_SRC = _extract_function(MESSAGES_SRC, "_messageIdentityKey")
IS_HISTORICAL_ANCHOR_ACTIVITY_SCENE_SRC = _extract_function(
    MESSAGES_SRC, "_isHistoricalAnchorActivityScene"
)
CARRY_FORWARD_EPHEMERAL_TURN_FIELDS_SRC = _extract_function(
    MESSAGES_SRC, "_carryForwardEphemeralTurnFields"
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
    attachLiveCalls,
    setBusyCalls: setBusyCalls.slice(),
    renderedControlRows: renderedControlRows.slice(),
    renderedActivityRows: renderedActivityRows.slice(),
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
  globalThis.renderMessages = () => {
    const controls=[];
    const activity=[];
    for(const message of (Array.isArray(S.messages)?S.messages:[])){
      const scene=message&&message._anchor_activity_scene;
      for(const row of (Array.isArray(scene&&scene.activity_rows)?scene.activity_rows:[])){
        if(row&&row.role==='control') controls.push({event_id:String(row.event_id||''),text:String(row.text||'')});
        if(row) activity.push({
          role:String(row.role||''),
          event_id:String(row.event_id||''),
          text:String(row.text||''),
          seq:row.seq,
          order_index:row.order_index,
        });
      }
    }
    renderedControlRows.push(controls);
    renderedActivityRows.push(activity);
  };
  globalThis._checkAndShowHandoffHint = () => {};
  globalThis._hideHandoffHint = () => {};
  globalThis._isMessagingSession = () => true;
  globalThis._clearDeferredActiveSessionExternalRefresh = () => {};

  globalThis.setStatus = () => {};
  globalThis.setComposerStatus = () => {};
  globalThis.setBusy = (value) => { setBusyCalls.push(!!value); };
  globalThis.attachLiveStream = () => { attachLiveCalls += 1; };
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
  window._carryForwardEphemeralTurnFields=_carryForwardEphemeralTurnFields;
  globalThis._RUN_OWNED_EPHEMERAL_TURN_FIELDS = new Set(['_anchor_stream_id','_anchor_activity_scene']);
  globalThis._EPHEMERAL_TURN_FIELDS = ['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
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
  attachLiveCalls = 0;
  setBusyCalls = [];
  renderedControlRows = [];
  renderedActivityRows = [];
  toastCalls = [];
}

let rearmCalls = 0;
let clearHintCalls = 0;
let visibleCacheClears = 0;
let liveCardClears = 0;
let toolSyncCalls = 0;
let attachLiveCalls = 0;
let setBusyCalls = [];
let renderedControlRows = [];
let renderedActivityRows = [];
let toastCalls = [];

// Source under test
globalThis.window = {};
__MESSAGE_IDENTITY_KEY_SRC__
__IS_HISTORICAL_ANCHOR_ACTIVITY_SCENE_SRC__
__CARRY_FORWARD_EPHEMERAL_TURN_FIELDS_SRC__
__INFLIGHT_HAS_VISIBLE_STATE_SRC__
__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__
__MERGE_PENDING_SESSION_MESSAGE_SRC__
__ATTACH_SETTLED_RUNTIME_JOURNAL_CONTROLS_SRC__
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

const SETTLED_STEER_EVENT_ID = 'settled-run:7';
const API_SETTLED_META = {
  session: {
    session_id: 'sid-settled',
    message_count: 2,
    active_stream_id: null,
    last_run_stream_id: 'settled-run',
    resolve_model: 'test-provider/test-model',
    runtime_journal_snapshot: {
      stream_id: 'settled-run',
      last_seq: 8,
      last_event_id: 'settled-run:8',
      anchor_activity_scene: {
        version: 'activity_scene_v1',
        mode: 'compact_worklog',
        identity: {session_id:'sid-settled',stream_id:'settled-run',run_id:'settled-run'},
        activity_rows: [
          {role:'control',kind:'control_boundary',source_event_type:'steer_delivered',event_id:SETTLED_STEER_EVENT_ID,row_id:SETTLED_STEER_EVENT_ID,local_id:SETTLED_STEER_EVENT_ID,seq:7,text:'durable settled steer',status:'delivered'},
          {role:'control',kind:'control_boundary',source_event_type:'steer_delivered',event_id:SETTLED_STEER_EVENT_ID,row_id:SETTLED_STEER_EVENT_ID,local_id:SETTLED_STEER_EVENT_ID,seq:7,text:'durable settled steer',status:'delivered'},
        ],
      },
    },
  },
};

const API_SETTLED_MSGS = {
  session: {
    session_id: 'sid-settled',
    last_run_stream_id: 'settled-run',
    _messages_truncated: false,
    _messages_offset: 0,
    messages: [
      {role:'user',content:'original request'},
      {
        role:'assistant',
        content:'settled answer',
        _anchor_activity_scene:{
          version:'activity_scene_v1',
          mode:'compact_worklog',
          identity:{session_id:'sid-settled',stream_id:'settled-run',run_id:'settled-run'},
          final_answer:'settled answer',
          activity_rows:[
            {
              role:'thinking',kind:'reasoning',source_event_type:'reasoning',
              event_id:'settled-run:6',row_id:'settled-run:6',seq:0,order_index:0,
              text:'reasoning before steer',status:'completed',
              identity:{event_id:'settled-run:6',run_id:'settled-run',seq:6},
            },
            {
              role:'tool',kind:'tool_completed',source_event_type:'tool_complete',
              event_id:'settled-run:8',row_id:'settled-run:8',seq:1,order_index:1,
              text:'tool after steer',status:'completed',
              identity:{event_id:'settled-run:8',run_id:'settled-run',seq:8},
            },
          ],
        },
      },
    ],
    message_count: 2,
    tool_calls: [],
  },
};

const API_SPLIT_RUN_META = JSON.parse(JSON.stringify(API_SETTLED_META));
API_SPLIT_RUN_META.session.session_id = 'sid-split-run';
API_SPLIT_RUN_META.session.last_run_stream_id = 'run-a';
API_SPLIT_RUN_META.session.runtime_journal_snapshot.stream_id = 'run-a';
API_SPLIT_RUN_META.session.runtime_journal_snapshot.last_event_id = 'run-a:8';
API_SPLIT_RUN_META.session.runtime_journal_snapshot.anchor_activity_scene.identity = {
  session_id:'sid-split-run',stream_id:'run-a',run_id:'run-a'
};
API_SPLIT_RUN_META.session.runtime_journal_snapshot.anchor_activity_scene.activity_rows = [
  {
    role:'control',kind:'control_boundary',source_event_type:'steer_delivered',
    event_id:'run-a:7',row_id:'run-a:7',local_id:'run-a:7',seq:7,
    text:'run A steer must not attach',status:'delivered',
    identity:{event_id:'run-a:7',run_id:'run-a',seq:7},
  },
];

const API_SPLIT_RUN_MSGS = JSON.parse(JSON.stringify(API_SETTLED_MSGS));
API_SPLIT_RUN_MSGS.session.session_id = 'sid-split-run';
API_SPLIT_RUN_MSGS.session.last_run_stream_id = 'run-b';
API_SPLIT_RUN_MSGS.session.messages[1].content = 'run B answer';
API_SPLIT_RUN_MSGS.session.messages[1]._anchor_activity_scene.identity = {
  session_id:'sid-split-run',stream_id:'run-b',run_id:'run-b'
};
API_SPLIT_RUN_MSGS.session.messages[1]._anchor_activity_scene.activity_rows = [
  {
    role:'thinking',kind:'reasoning',source_event_type:'reasoning',
    event_id:'run-b:6',row_id:'run-b:6',seq:0,order_index:0,
    text:'run B reasoning',status:'completed',
    identity:{event_id:'run-b:6',run_id:'run-b',seq:6},
  },
];

const API_SPLIT_RUN_MSGS_NO_SCENE = JSON.parse(JSON.stringify(API_SPLIT_RUN_MSGS));
API_SPLIT_RUN_MSGS_NO_SCENE.session.messages[1] = {
  role:'assistant',
  content:'identical settled answer',
};

const RUN_A_CARRY_SCENE = {
  version:'activity_scene_v1',
  mode:'compact_worklog',
  identity:{session_id:'sid-split-run-no-scene',stream_id:'run-a',run_id:'run-a'},
  final_answer:'identical settled answer',
  activity_rows:[{
    role:'control',kind:'control_boundary',source_event_type:'steer_delivered',
    event_id:'run-a:7',row_id:'run-a:7',seq:0,order_index:0,
    text:'run A steer must not carry',status:'delivered',
  }],
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

async function runSettledJournalRefresh() {
  createEnvironment();
  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;
  const meta=apiHost.enqueue(buildMessageUrl('sid-settled',0));
  const messages=apiHost.enqueue(buildMessageUrl('sid-settled',1));
  const loaded=loadSession('sid-settled',{force:true});
  meta._resolve(API_SETTLED_META);
  await waitForQueued(apiHost,messages.url);
  messages._resolve(API_SETTLED_MSGS);
  await loaded;
  return {
    scenario:'settled-journal-refresh',
    busy:S.busy,
    activeStreamId:S.activeStreamId,
    attachLiveCalls,
    setBusyCalls:setBusyCalls.slice(),
    renderedControlRows:renderedControlRows.slice(),
    renderedActivityRows:renderedActivityRows.slice(),
  };
}

async function runSplitResponseStreamCoherence() {
  createEnvironment();
  const apiHost = makeHarness();
  globalThis.apiHost = apiHost;
  globalThis.api = apiHost.api;
  const meta=apiHost.enqueue(buildMessageUrl('sid-split-run',0));
  const messages=apiHost.enqueue(buildMessageUrl('sid-split-run',1));
  const loaded=loadSession('sid-split-run',{force:true});
  meta._resolve(API_SPLIT_RUN_META);
  await waitForQueued(apiHost,messages.url);
  messages._resolve(API_SPLIT_RUN_MSGS);
  await loaded;
  return {
    scenario:'split-response-stream-coherence',
    sessionLastRunStreamId:S.session&&S.session.last_run_stream_id,
    renderedControlRows:renderedControlRows.slice(),
    renderedActivityRows:renderedActivityRows.slice(),
  };
}

async function runSplitResponseCarryForwardCoherence({sameRun}) {
  createEnvironment();
  const sid='sid-split-run-no-scene';
  const metadata=JSON.parse(JSON.stringify(API_SPLIT_RUN_META));
  metadata.session.session_id=sid;
  metadata.session.last_run_stream_id='run-a';
  metadata.session.runtime_journal_snapshot={
    stream_id:'run-a',
    last_seq:8,
    last_event_id:'run-a:8',
    anchor_activity_scene:{
      version:'activity_scene_v1',
      mode:'compact_worklog',
      identity:{session_id:sid,stream_id:'run-a',run_id:'run-a'},
      activity_rows:[],
    },
  };
  const messages=JSON.parse(JSON.stringify(API_SPLIT_RUN_MSGS_NO_SCENE));
  messages.session.session_id=sid;
  messages.session.last_run_stream_id=sameRun?'run-a':'run-b';
  S.session={session_id:sid,message_count:2,last_run_stream_id:'run-a'};
  S.messages=[
    {role:'user',content:'original request'},
    {
      role:'assistant',content:'identical settled answer',
      _turnUsage:{total_tokens:99},
      _anchor_stream_id:'run-a',
      _anchor_activity_scene:JSON.parse(JSON.stringify(RUN_A_CARRY_SCENE)),
    },
  ];
  _pendingCarryForwardSnapshot=S.messages.slice();
  const apiHost=makeHarness();
  globalThis.apiHost=apiHost;
  globalThis.api=apiHost.api;
  const meta=apiHost.enqueue(buildMessageUrl(sid,0));
  const transcript=apiHost.enqueue(buildMessageUrl(sid,1));
  const loaded=loadSession(sid,{force:true});
  meta._resolve(metadata);
  await waitForQueued(apiHost,transcript.url);
  transcript._resolve(messages);
  await loaded;
  const assistant=S.messages.find(m=>m&&m.role==='assistant')||{};
  return {
    sameRun,
    sessionLastRunStreamId:S.session&&S.session.last_run_stream_id,
    anchorStreamId:assistant._anchor_stream_id||null,
    anchorScene:assistant._anchor_activity_scene||null,
    turnUsage:assistant._turnUsage||null,
  };
}

async function runAll() {
  return {
    crossSessionOrdering: await runCrossSessionOrdering(),
    observedIdleCrossSessionOrdering: await runObservedIdleCrossSessionOrdering(),
    staleIdleCatch: await runStaleRejectedIdleCatch(),
    settledJournalRefresh: await runSettledJournalRefresh(),
    splitResponseStreamCoherence: await runSplitResponseStreamCoherence(),
    splitResponseCarryForwardMismatch: await runSplitResponseCarryForwardCoherence({sameRun:false}),
    splitResponseCarryForwardSameRun: await runSplitResponseCarryForwardCoherence({sameRun:true}),
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
            "__MESSAGE_IDENTITY_KEY_SRC__", MESSAGE_IDENTITY_KEY_SRC
        )
        .replace(
            "__IS_HISTORICAL_ANCHOR_ACTIVITY_SCENE_SRC__",
            IS_HISTORICAL_ANCHOR_ACTIVITY_SCENE_SRC,
        )
        .replace(
            "__CARRY_FORWARD_EPHEMERAL_TURN_FIELDS_SRC__",
            CARRY_FORWARD_EPHEMERAL_TURN_FIELDS_SRC,
        )
        .replace(
            "__INFLIGHT_HAS_VISIBLE_STATE_SRC__", INFLIGHT_HAS_VISIBLE_STATE_SRC
        )
        .replace(
            "__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__", SELECT_LIVE_RECOVERY_INFLIGHT_SRC
        )
        .replace(
            "__MERGE_PENDING_SESSION_MESSAGE_SRC__", MERGE_PENDING_SESSION_MESSAGE_SRC
        )
        .replace(
            "__ATTACH_SETTLED_RUNTIME_JOURNAL_CONTROLS_SRC__",
            ATTACH_SETTLED_RUNTIME_JOURNAL_CONTROLS_SRC,
        )
        .replace("__LOAD_SESSION_SRC__", LOAD_SESSION_SRC)
        .replace("__ENSURE_MESSAGES_LOADED_SRC__", ENSURE_MESSAGES_LOADED_SRC)
    )
    body = _run_node(script, tmp_path)

    cross = body["crossSessionOrdering"]
    stale = body["staleIdleCatch"]
    observed = body["observedIdleCrossSessionOrdering"]
    settled = body["settledJournalRefresh"]
    split = body["splitResponseStreamCoherence"]
    carry_mismatch = body["splitResponseCarryForwardMismatch"]
    carry_same = body["splitResponseCarryForwardSameRun"]

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

    assert settled["busy"] is False
    assert settled["activeStreamId"] is None
    assert settled["attachLiveCalls"] == 0, "idle journal replay must not reopen the run SSE"
    assert True not in settled["setBusyCalls"], "idle journal replay must not show a busy spinner"
    assert settled["renderedControlRows"][-1] == [
        {"event_id": "settled-run:7", "text": "durable settled steer"}
    ], "hard refresh must render the durable control row exactly once by event_id"
    assert [row["role"] for row in settled["renderedActivityRows"][-1]] == [
        "thinking",
        "control",
        "tool",
    ], "journal seq 7 Steer must remain between settled activity from seq 6 and seq 8"
    assert [row["event_id"] for row in settled["renderedActivityRows"][-1]] == [
        "settled-run:6",
        "settled-run:7",
        "settled-run:8",
    ]
    assert [row["seq"] for row in settled["renderedActivityRows"][-1]] == [0, 1, 2]
    assert [row["order_index"] for row in settled["renderedActivityRows"][-1]] == [0, 1, 2]

    assert split["sessionLastRunStreamId"] == "run-b", (
        "the transcript response must advance the accepted metadata to its own run generation"
    )
    assert split["renderedControlRows"][-1] == [], (
        "run A metadata Steer must not attach to the run B assistant transcript"
    )
    assert [row["event_id"] for row in split["renderedActivityRows"][-1]] == [
        "run-b:6"
    ]

    assert carry_mismatch["sessionLastRunStreamId"] == "run-b"
    assert carry_mismatch["anchorStreamId"] is None
    assert carry_mismatch["anchorScene"] is None, (
        "run A's scene/control row must not transplant onto a scene-less run B assistant"
    )
    assert carry_mismatch["turnUsage"] == {"total_tokens": 99}, (
        "safe client-only usage metadata should still survive the split reload"
    )
    assert carry_same["sessionLastRunStreamId"] == "run-a"
    assert carry_same["anchorStreamId"] == "run-a"
    assert carry_same["anchorScene"]["activity_rows"][0]["event_id"] == "run-a:7"
    assert carry_same["turnUsage"] == {"total_tokens": 99}

    assert cross["loadingSid"] is None, "load marker should be cleared after successful completion"
    assert stale["loadingSid"] is None, "load marker should be cleared after stale reject + re-owner completion"
