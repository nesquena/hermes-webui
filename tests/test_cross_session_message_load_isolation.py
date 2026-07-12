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

async function runAll() {
  return {
    crossSessionOrdering: await runCrossSessionOrdering(),
    observedIdleCrossSessionOrdering: await runObservedIdleCrossSessionOrdering(),
    staleIdleCatch: await runStaleRejectedIdleCatch(),
  };
}

runAll()
  .then((r) => console.log(JSON.stringify(r)))
  .catch((err) => {
    console.error('NODE_ERROR', err && err.stack || err);
    process.exit(1);
  });
'''


def _run_node(script: str) -> dict:
    assert NODE is not None, "node is required"
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
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
def test_loadsession_cross_session_ordering_and_stale_reject_behavior():
    script = _NODE_SCRIPT_TEMPLATE.replace(
        "__LOAD_SESSION_SRC__", LOAD_SESSION_SRC
    ).replace(
        "__ENSURE_MESSAGES_LOADED_SRC__", ENSURE_MESSAGES_LOADED_SRC
    )
    body = _run_node(script)

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
