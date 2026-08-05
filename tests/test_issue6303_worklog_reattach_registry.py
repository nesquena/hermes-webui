"""Behavioral regression tests for #6303/#6381 — Worklog identity across session reattach.

The production fix (static/messages.js) makes registry reset and transport/closure
ownership one atomic decision: `attachLiveStream()` checks and reuses an existing
OPEN same-stream EventSource BEFORE deleting `window._liveAnchorRegistries[streamId]`,
and the independent pre-attach deletion was removed from `loadSession()` in
static/sessions.js. A reused transport keeps the registry its handlers own; only a
fresh connection replaces it.

These tests exercise the two ownership paths the maintainer re-gate required, using
the REAL production code (not re-implementations):

Path 1 — OPEN-transport reuse (test_open_transport_reuse_dispatches_post_reattach_event_into_same_registry)
  - Install an existing OPEN EventSource through the production listener-registration
    path (a real `attachLiveStream()` call wires real SSE handlers via `_wireSSE`).
  - Call the real `attachLiveStream(..., {reconnecting:true})`; it must reuse the OPEN
    transport (early-return branch) and keep the handler-owned registry.
  - Dispatch a post-reattach reasoning event THROUGH that production listener and
    assert the registry the handler applied to is object-identical to the registry
    the renderer projects from (`window._liveAnchorRegistries.get(streamId)`), with
    exactly one matching Worklog row.

Path 2 — loadSession() with a winning server journal snapshot
  (test_loadsession_journal_snapshot_reattach_reuses_open_source_exactly_once)
  - Run the REAL `loadSession()` with a winning `runtime_journal_snapshot`
    (no local INFLIGHT tail, so `_selectLiveRecoveryInflight()` picks the server
    snapshot, which carries `reattach:true`).
  - The existing OPEN source is reused through loadSession's real reattach call
    (`attachLiveStream(..., {reconnecting:true})` at the INFLIGHT reattach branch).
  - Assert the journal-snapshot branch and the reattach branch actually ran
    (`journalSnapshot===true`, `reattach` flipped to false), that the production
    listener still owns the SAME registry object, that a post-reattach reasoning
    event projects exactly one Worklog row, and that NO replacement EventSource was
    created during the whole loadSession reattach.

A third sentinel keeps the other side of the ownership decision locked: a fresh
connection (no existing OPEN transport) must delete the stale registry and let the
new closure create its own registry via the real anchor API.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
UI_JS = ROOT / "static" / "ui.js"
ANCHORS_JS = ROOT / "static" / "assistant_turn_anchors.js"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    assert path.exists(), f"{path} not found"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Function extractor — pulls a complete 'function name(...){...}' declaration
# (or 'async function name(...){...}') out of a source file, correctly
# handling nested braces by tracking depth.
# ---------------------------------------------------------------------------

def _function_source(src: str, name: str) -> str:
    """Extract a complete function declaration ('function name(...){...}' or
    'async function name(...){...}') from source, correctly handling nested
    braces by tracking depth.
    """
    start = src.find(f"function {name}(")
    assert start != -1, f"function {name} not found"
    # Preserve an 'async' modifier when present: 'async function name(...)'
    # must keep its modifier so 'await' stays inside an async context.
    head = src[max(0, start - 60):start]
    marker = head.rfind("async")
    if marker != -1 and head[marker:].strip() == "async":
        start = start - (len(head) - marker)
    params_idx = src.find("(", start)
    assert params_idx != -1
    depth_p = 0
    close_p = -1
    for idx in range(params_idx, len(src)):
        c = src[idx]
        if c == "(":
            depth_p += 1
        elif c == ")":
            depth_p -= 1
            if depth_p == 0:
                close_p = idx
                break
    assert close_p != -1, f"{name} params did not close"
    brace = src.find("{", close_p)
    assert brace != -1, f"{name} body not found"
    depth_b = 0
    for idx in range(brace, len(src)):
        c = src[idx]
        if c == "{":
            depth_b += 1
        elif c == "}":
            depth_b -= 1
            if depth_b == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


# ---------------------------------------------------------------------------
# Node.js harness — mirrors the module-scope environment the production
# functions expect, then concatenates the REAL production functions and runs
# the driver code. Results are printed as one JSON object on the last stdout
# line (same contract as the previous version of this file).
# ---------------------------------------------------------------------------

_MOCK_GLOBALS = textwrap.dedent("""\
// Mock globals expected by the production module scopes. `window` is the
// global object so window.* assignments mirror the browser.
var window = globalThis;
var INFLIGHT = {};
var LIVE_STREAMS = {};
var S = { session: null, activeStreamId: null, messages: [], toolCalls: [], busy: false };
var _STREAM_WAS_HIDDEN = {};
var _STREAM_NOTIFICATION_BACKGROUND = {};
var _desktopBackgroundedForNotifications = false;

// Module-scope state loadSession() touches.
var _loadingSessionId = null;
var _loadSessionGeneration = 0;
var _pendingCarryForwardSnapshot = null;
var _messagesTruncated = false;
var _oldestIdx = 0;
var _loadingOlder = false;
var _messageUserUnpinned = false;
var _scrollPinned = true;
var _yoloEnabled = false;

window._liveAnchorRegistries = new Map();

var document = {
  baseURI: 'http://localhost:8080/',
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  querySelector: () => null,
  getElementById: () => null,
  createElement: () => ({ className: '', dataset: {}, style: {}, setAttribute: () => {}, appendChild: () => {}, querySelector: () => null, querySelectorAll: () => [], classList: { add: () => {}, remove: () => {} } }),
};
var location = { href: 'http://localhost:8080/' };
var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
function $(id) { return null; }

// Recording EventSource: mirrors browser semantics — addEventListener APPENDS
// (production registers both the typed handler and the run-journal cursor
// listener for the same event name), dispatch fires every listener in order.
// Every construction is counted so tests can prove no replacement transport
// was created.
var __esCreated = [];
function EventSource(url) {
  this.url = url;
  this.readyState = EventSource.OPEN;
  this._handlers = {};
  this.addEventListener = (type, fn) => {
    if (!this._handlers[type]) this._handlers[type] = [];
    this._handlers[type].push(fn);
  };
  this.removeEventListener = (type, fn) => {
    const list = this._handlers[type];
    if (!list) return;
    if (!fn) { delete this._handlers[type]; return; }
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  };
  this.close = () => { this.readyState = EventSource.CLOSED; };
  this.dispatch = (type, data) => {
    const list = this._handlers[type] || [];
    const event = { data: JSON.stringify(data), lastEventId: (data && data.event_id) || '' };
    for (const fn of list.slice()) fn(event);
  };
  __esCreated.push(this);
}
EventSource.OPEN = 1;
EventSource.CONNECTING = 0;
EventSource.CLOSED = 2;

// The mocked API fetch used by loadSession(); the driver sets __apiResponse.
var __apiResponse = null;
async function api(url) { return __apiResponse; }

// Stubbed module-scope helpers the production functions call. The code paths
// under test (registry ownership, reattach, journal-snapshot projection) are
// real; these are presentation/side-effect hooks outside the tested invariant.
function _bindStreamHiddenTracker() {}
function ensureLiveWorklogShell() {}
function showLiveRunStatus() {}
function closeOtherLiveStreams() {}
function closeLiveStream() {}
function resetTurnWorkspaceMutations() {}
function _resetStreamScrollFollow() {}
function _suspendSessionStreamForLiveChat() {}
function clearInflight() {}
function clearInflightState() {}
function _closeSource() {}
function _approvalBelongsToOwner() { return true; }
function _clarifyBelongsToOwner() { return true; }
function _clearApprovalPendingForSession() {}
function _clearClarifyPendingForSession() {}
function stopApprovalPolling() {}
function stopClarifyPolling() {}
function hideApprovalCard() {}
function hideClarifyCard() {}
function _closeAdjustableRelated() {}
function _relateLiveStreamPane() {}
function _ensureInflightLiveAssistantMessageStub() {}
function startSessionStream() {}
function stopSessionStream() {}
function _updateYoloPill() {}
function syncTopbar() {}
function renderMessages() {}
function setBusy() {}
function setComposerStatus() {}
function startApprovalPolling() {}
function ensureRunActivityForCurrentTurn() {}
function _resolveSessionModelForDisplaySoon() {}
function _deferWorkspaceRefreshForSession() {}
function _setActiveSessionUrl() {}
function _acknowledgeSessionVisit() {}
function _clearDeferredActiveSessionExternalRefresh() {}
function _clearSameSessionForceReloadHint() {}
function _captureSameSessionForceReloadHint() {}
function _clearEmptyComposerModelOverride() {}
function _renderRuntimeJournalAnchorActivityScene() { return false; }
async function _ensureMessagesLoaded() { return undefined; }
function updateThinking() {}
function appendThinking() {}
function _hideHandoffHint() {}
function _checkAndShowHandoffHint() {}

// ui.js renderer hooks: chatActivityMode() is the production mode source;
// renderLiveAnchorActivityScene() is the DOM paint step — recorded here so
// tests can inspect the scene the real renderer would have painted.
function chatActivityMode() { return 'compact_worklog'; }
var __renderCalls = [];
function renderLiveAnchorActivityScene(streamId, scene, opts) {
  __renderCalls.push({ streamId: streamId, scene: scene, opts: opts || {} });
  return true;
}
""").lstrip()

# Real sessions.js helpers pulled in for the loadSession() test. Order does not
# matter — function declarations hoist within the script scope.
_SESSIONS_HELPERS = [
    "loadSession",
    "_rearmActiveSessionStream",
    "_serverLiveSnapshotInflight",
    "_selectLiveRecoveryInflight",
    "_inflightHasVisibleLiveState",
    "_ensureInflightLiveAssistantMessage",
    "_projectInflightMessagesForActivityBursts",
    "_messageComparableText",
    "_compactTranscriptText",
    "_currentTurnAssistantText",
    "_sameTranscriptMessage",
    "_hasCurrentTailUserDuplicate",
    "_dropCurrentTurnAssistantMessages",
    "_prepareRunningLiveTail",
    "_mergeInflightTailMessages",
    "_isMessagingSession",
]


def _run_harness(setup_code: str, include_loadsession: bool = False) -> dict:
    """Assemble the REAL production functions + mocks + driver and run via Node.

    The script always includes:
      - the real HermesAssistantTurnAnchors API (static/assistant_turn_anchors.js);
      - the real attachLiveStream() (static/messages.js, incl. its _wireSSE
        listener-registration path and all closure helpers);
      - the real renderer projection (static/ui.js
        _projectLiveAnchorActivitySceneForStream /
        _renderLiveAnchorActivitySceneForStream).

    When include_loadsession is true it also includes the real loadSession() and
    the sessions.js helpers it calls.
    """
    assert NODE, "node executable is required for JavaScript behavioral checks"
    parts = [
        _MOCK_GLOBALS,
        _read(ANCHORS_JS),
        _function_source(_read(MESSAGES_JS), "_extractInlineThinkingFromContent"),
        _function_source(_read(MESSAGES_JS), "attachLiveStream"),
        _function_source(_read(UI_JS), "_projectLiveAnchorActivitySceneForStream"),
        _function_source(_read(UI_JS), "_renderLiveAnchorActivitySceneForStream"),
    ]
    if include_loadsession:
        sessions = _read(SESSIONS_JS)
        parts.append(
            "const _MESSAGING_RAW_SOURCES = new Set("
            "['weixin','telegram','discord','slack','email','wecom','wecom_callback']);"
        )
        for name in _SESSIONS_HELPERS:
            parts.append(_function_source(sessions, name))
    parts.append(setup_code)
    full_script = "\n".join(parts)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    ) as f:
        f.write(full_script)
        script_path = f.name
    try:
        result = subprocess.run(
            [NODE, script_path],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        Path(script_path).unlink(missing_ok=True)
        pytest.fail(
            "node behavior check timed out"
            f"\nstdout:\n{exc.stdout or '<empty>'}"
            f"\nstderr:\n{exc.stderr or '<empty>'}"
        )
    Path(script_path).unlink(missing_ok=True)
    if result.returncode:
        pytest.fail(
            "node behavior check failed"
            f"\nexit code: {result.returncode}"
            f"\nstdout:\n{result.stdout or '<empty>'}"
            f"\nstderr:\n{result.stderr or '<empty>'}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        pytest.fail("node produced no stdout output")
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Required path 1: existing OPEN same-stream transport reused through the
# production listener-registration path.
# ---------------------------------------------------------------------------

_OPEN_REUSE_DRIVER = textwrap.dedent("""\
const __results = {};
const SID = 'test-sid';
const STREAM_ID = 'test-stream';

window._liveAnchorRegistries = new Map();
window._renderLiveAnchorActivitySceneForStream = _renderLiveAnchorActivitySceneForStream;
window._projectLiveAnchorActivitySceneForStream = _projectLiveAnchorActivitySceneForStream;

INFLIGHT[SID] = {
  messages: [], uploaded: [], toolCalls: [],
  streamId: STREAM_ID,
  activityBurstAnchors: [], currentActivityBurstId: 0, currentLiveSegmentSeq: 0,
};
S.session = { session_id: SID };
S.activeStreamId = STREAM_ID;
S.messages = [];

// Phase 1 — install the OPEN source through the PRODUCTION listener path:
// a real attachLiveStream() call creates the EventSource and _wireSSE() binds
// the real reasoning/tool handlers that close over the anchor registry.
attachLiveStream(SID, STREAM_ID, [], {});
const handlerRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);
const source = __esCreated[0];
__results.registryCreatedThroughProduction = !!handlerRegistryRef;
__results.esCreatedAfterInstall = __esCreated.length;

// Phase 2 — reattach: the OPEN same-stream transport must be reused (early
// return) and the registry it owns must NOT be deleted.
attachLiveStream(SID, STREAM_ID, [], { reconnecting: true });
__results.esCreatedAfterReattach = __esCreated.length;
__results.registryKeptThroughReattach = window._liveAnchorRegistries.get(STREAM_ID) === handlerRegistryRef;

// Phase 3 — dispatch a post-reattach reasoning event THROUGH the production
// listener installed in phase 1.
source.dispatch('reasoning', { text: 'deep reasoning step' });

// Phase 4 — the handler applied the event to its closure registry; the
// renderer resolves the same object from the window map.
__results.projectedRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);
__results.sameObjectRef = __results.projectedRegistryRef === handlerRegistryRef;
__results.appliedEvents = handlerRegistryRef.anchor.activity_events.length;
const scene = window._projectLiveAnchorActivitySceneForStream(STREAM_ID, 'compact_worklog');
__results.sceneRows = scene && Array.isArray(scene.activity_rows) ? scene.activity_rows.length : -1;
__results.sceneRowKind = scene && scene.activity_rows[0] ? scene.activity_rows[0].kind : null;
__results.sceneRowRole = scene && scene.activity_rows[0] ? scene.activity_rows[0].role : null;
__results.sceneRowSourceType = scene && scene.activity_rows[0] ? scene.activity_rows[0].source_event_type : null;
__results.renderCalls = __renderCalls.length;

process.stdout.write(JSON.stringify(__results) + '\\n', () => { process.exit(0); });
""")


def test_open_transport_reuse_dispatches_post_reattach_event_into_same_registry():
    """Required path 1: OPEN transport reuse keeps handler/renderer registry
    identity and projects exactly one Worklog row after a post-reattach event.

    Installs the OPEN source through the production listener-registration path
    (real attachLiveStream -> _wireSSE), calls the real
    attachLiveStream({reconnecting:true}), dispatches a reasoning event through
    that listener, and proves:
      - no replacement EventSource was created on reattach;
      - the registry the handler applied the event to is object-identical to the
        registry the renderer projects from;
      - exactly one matching Worklog row is projected.
    """
    result = _run_harness(_OPEN_REUSE_DRIVER)

    assert result["registryCreatedThroughProduction"] is True, (
        "the first attachLiveStream call must create a registry through the real "
        "anchor API and register it in window._liveAnchorRegistries"
    )
    assert result["esCreatedAfterInstall"] == 1, (
        "exactly one EventSource should exist after the initial install"
    )
    assert result["esCreatedAfterReattach"] == 1, (
        "reattach must reuse the existing OPEN transport — no replacement "
        "EventSource may be created"
    )
    assert result["registryKeptThroughReattach"] is True, (
        "the registry owned by the reused OPEN transport must survive the "
        "reconnecting:true call (delete order bug #6381)"
    )
    assert result["sameObjectRef"] is True, (
        "the registry the production listener applied the post-reattach event to "
        "must be object-identical to the registry the renderer projects from"
    )
    assert result["appliedEvents"] == 1, (
        "the post-reattach reasoning event must be applied exactly once"
    )
    assert result["sceneRows"] == 1, (
        "the renderer projection must contain exactly one Worklog row — not zero "
        "(orphaned registry) and not duplicates"
    )
    assert result["sceneRowKind"] == "reasoning" and result["sceneRowRole"] == "thinking", (
        "the single Worklog row must be the reasoning event that was dispatched"
    )
    assert result["sceneRowSourceType"] == "reasoning", (
        "the Worklog row must carry the dispatched event's source type"
    )


# ---------------------------------------------------------------------------
# Required path 2: real loadSession() with a winning server journal snapshot.
# ---------------------------------------------------------------------------

_LOADSESSION_DRIVER = textwrap.dedent("""\
const __results = {};
const SID = 'test-sid';
const STREAM_ID = 'test-stream';

window._liveAnchorRegistries = new Map();
window._renderLiveAnchorActivitySceneForStream = _renderLiveAnchorActivitySceneForStream;
window._projectLiveAnchorActivitySceneForStream = _projectLiveAnchorActivitySceneForStream;

// Session the mocked metadata API will return to loadSession(): an active
// stream plus a WINNING server runtime_journal_snapshot.
const __session = {
  session_id: SID,
  active_stream_id: STREAM_ID,
  pending_attachments: [],
  message_count: 0,
  last_usage: {},
  runtime_journal_snapshot: {
    stream_id: STREAM_ID,
    last_seq: 5,
    last_event_id: 'evt-5',
    last_assistant_text: 'partial answer',
    last_reasoning_text: '',
    current_activity_burst_id: 0,
    current_live_segment_seq: 0,
    activity_burst_anchors: [],
    messages: [{ role: 'assistant', content: 'partial answer', _live: true, _journal_snapshot: true }],
    tool_calls: [],
  },
};
__apiResponse = { session: __session };

// Phase 1 — boot state: no current session, no local INFLIGHT tail (so the
// server journal snapshot must win), and an existing OPEN same-stream source
// whose listeners were installed through the production path before the load.
S.session = null;
S.activeStreamId = null;
S.messages = [];
S.toolCalls = [];
S.busy = false;
delete INFLIGHT[SID];

INFLIGHT[SID] = {
  messages: [], uploaded: [], toolCalls: [],
  streamId: STREAM_ID,
  activityBurstAnchors: [], currentActivityBurstId: 0, currentLiveSegmentSeq: 0,
};
attachLiveStream(SID, STREAM_ID, [], {});
const handlerRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);
const source = __esCreated[0];
delete INFLIGHT[SID];   // drop the local tail: server snapshot must win
S.session = null;       // fresh boot for the real loadSession()
S.activeStreamId = null;

(async () => {
  // Phase 2 — run the REAL loadSession() with the winning journal snapshot.
  await loadSession(SID, { skipLineageResolve: true, skipExtHooks: true, skipContinuationResolve: true });

  // The journal-snapshot branch and the reattach branch must have run:
  __results.journalSnapshotWon = !!(INFLIGHT[SID] && INFLIGHT[SID].journalSnapshot === true);
  __results.reattachBranchRan = !!(INFLIGHT[SID] && INFLIGHT[SID].reattach === false);
  // No replacement EventSource may have been created by the reattach call.
  __results.esCreatedDuringLoad = __esCreated.length;
  // The registry the OPEN source's handlers own must have survived loadSession.
  __results.registryKeptThroughLoad = window._liveAnchorRegistries.get(STREAM_ID) === handlerRegistryRef;

  // Phase 3 — dispatch a post-reattach reasoning event through the production
  // listener (the one installed before loadSession).
  source.dispatch('reasoning', { text: 'post-reattach reasoning' });

  // Phase 4 — identity + exactly-once Worklog projection.
  __results.projectedRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);
  __results.sameObjectRef = __results.projectedRegistryRef === handlerRegistryRef;
  __results.appliedEvents = handlerRegistryRef.anchor.activity_events.length;
  const scene = window._projectLiveAnchorActivitySceneForStream(STREAM_ID, 'compact_worklog');
  __results.sceneRows = scene && Array.isArray(scene.activity_rows) ? scene.activity_rows.length : -1;
  __results.sceneRowKind = scene && scene.activity_rows[0] ? scene.activity_rows[0].kind : null;
  __results.sceneRowRole = scene && scene.activity_rows[0] ? scene.activity_rows[0].role : null;
  __results.loadSessionRenderedScene = __renderCalls.some(c => c.streamId === STREAM_ID);

  process.stdout.write(JSON.stringify(__results) + '\\n', () => { process.exit(0); });
})().catch(err => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(2);
});
""")


def test_loadsession_journal_snapshot_reattach_reuses_open_source_exactly_once():
    """Required path 2: real loadSession() with a winning server journal
    snapshot reuses the OPEN source through its real reattach call and keeps
    registry identity with an exactly-once Worklog projection.

    Asserts:
      - the server runtime_journal_snapshot won (journalSnapshot===true);
      - loadSession's reattach branch actually ran (reattach flipped to false);
      - the production listener from the pre-load OPEN source still owns the
        SAME registry object as the renderer;
      - a post-reattach reasoning event projects exactly one Worklog row;
      - no replacement EventSource was created during loadSession.
    """
    result = _run_harness(_LOADSESSION_DRIVER, include_loadsession=True)

    assert result["journalSnapshotWon"] is True, (
        "the server runtime_journal_snapshot must win inside loadSession() "
        "(_serverLiveSnapshotInflight/_selectLiveRecoveryInflight) and mark "
        "INFLIGHT with journalSnapshot:true"
    )
    assert result["reattachBranchRan"] is True, (
        "loadSession() must have executed its reattach branch (it flips "
        "INFLIGHT.reattach to false before calling attachLiveStream)"
    )
    assert result["esCreatedDuringLoad"] == 1, (
        "loadSession()'s real reattach call must reuse the existing OPEN "
        "transport — no replacement EventSource may be created"
    )
    assert result["registryKeptThroughLoad"] is True, (
        "the registry owned by the reused OPEN transport must survive the full "
        "loadSession() reattach path"
    )
    assert result["sameObjectRef"] is True, (
        "after loadSession(), the registry the production listener applied the "
        "post-reattach event to must be object-identical to the registry the "
        "renderer projects from"
    )
    assert result["appliedEvents"] == 1, (
        "the post-reattach reasoning event must be applied exactly once"
    )
    assert result["sceneRows"] == 1, (
        "the renderer projection must contain exactly one Worklog row — not zero "
        "and not duplicates"
    )
    assert result["sceneRowKind"] == "reasoning" and result["sceneRowRole"] == "thinking", (
        "the single Worklog row must be the dispatched reasoning event"
    )
    assert result["loadSessionRenderedScene"] is True, (
        "loadSession() must have invoked the real renderer for the stream"
    )


# ---------------------------------------------------------------------------
# Sentinel: fresh connection (no existing OPEN transport) replaces the stale
# registry through the real anchor API.
# ---------------------------------------------------------------------------


def test_fresh_connection_replaces_stale_registry_when_no_open_transport():
    """The other side of the ownership decision: when attachLiveStream() does
    NOT find an existing OPEN same-stream transport, it must delete the stale
    registry and the fresh closure must create its own registry via the real
    anchor API (never reuse the stale object).
    """
    setup = textwrap.dedent("""\
    const __results = {};
    const SID = 'test-sid';
    const STREAM_ID = 'test-stream';

    window._liveAnchorRegistries = new Map();
    window._renderLiveAnchorActivitySceneForStream = _renderLiveAnchorActivitySceneForStream;
    window._projectLiveAnchorActivitySceneForStream = _projectLiveAnchorActivitySceneForStream;

    INFLIGHT[SID] = {
      messages: [], uploaded: [], toolCalls: [],
      streamId: STREAM_ID,
      activityBurstAnchors: [], currentActivityBurstId: 0, currentLiveSegmentSeq: 0,
    };

    // A stale registry left behind by a prior stream connection.
    const staleRegistry = { anchor: { session_id: SID, turn_id: 'turn-stale' }, events: [] };
    window._liveAnchorRegistries.set(STREAM_ID, staleRegistry);

    // NO existing OPEN transport for the stream.
    attachLiveStream(SID, STREAM_ID, [], { reconnecting: true });

    // The stale object must be gone from the map (delete-order fix) and a
    // brand-new registry created by the fresh closure via the real API.
    __results.staleRegistryReplaced = window._liveAnchorRegistries.get(STREAM_ID) !== staleRegistry;
    __results.freshRegistryCreated = !!window._liveAnchorRegistries.get(STREAM_ID);
    __results.freshRegistrySession = window._liveAnchorRegistries.get(STREAM_ID)
      ? window._liveAnchorRegistries.get(STREAM_ID).anchor.identity.session_id
      : null;
    __results.freshRegistryIdentity = window._liveAnchorRegistries.get(STREAM_ID)
      ? (window._liveAnchorRegistries.get(STREAM_ID).anchor.identity.stream_id === STREAM_ID)
      : false;

    process.stdout.write(JSON.stringify(__results) + '\\n', () => { process.exit(0); });
    """)

    result = _run_harness(setup)

    assert result["staleRegistryReplaced"] is True, (
        "with no existing OPEN transport, the stale registry must be deleted "
        "from window._liveAnchorRegistries"
    )
    assert result["freshRegistryCreated"] is True, (
        "the fresh connection closure must create its own registry through the "
        "real anchor API"
    )
    assert result["freshRegistrySession"] == "test-sid", (
        "the fresh registry must be seeded for the active session"
    )
    assert result["freshRegistryIdentity"] is True, (
        "the fresh registry must be seeded for the active stream"
    )
