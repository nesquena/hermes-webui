"""Pins the ``loadSession()`` generation bump against an in-flight older page.

Review note on #9280 (2026-11-23T16:54:09Z): with only the ``loadSession()``
bump removed, the three cases of
``tests/test_issue6421_older_page_generation.py`` still pass, so nothing keeps
that half of the repair alive.

The gap is the window the bump at ``static/sessions.js`` (inside the
``forceReload`` teardown, right before ``_loadingOlder = false``) exists for:
an older-page continuation is already in flight when a same-session force
reload starts. ``loadSession()`` raises ``_messagesGeneration`` there so the
continuation's post-await guard (``_loadOlderMessages``) bails *before* the
reload's own publish bump (``_ensureMessagesLoaded``). Without the early bump
the stale page lands on the transcript that is about to be replaced:

* ``keepStaleUntilLoaded`` (the #5177 visibility/focus recovery callers): the
  previous transcript is still on screen, so the page is prepended to it and
  the paging cursor rewinds from 200 to 90 while the refresh is pending;
* plain ``force:true``: the pane was cleared, so the page installs rows that
  belong to the previous generation into an empty transcript.

Both are caught here plus the positive control: with no reload in flight the
same page must still prepend (the guard must not disable pagination).

The production functions are extracted from ``static/sessions.js`` and driven
in Node; nothing is re-implemented in the harness.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_cross_session_message_load_isolation import (
    COMPOSER_DRAFT_HELPERS_SRC,
    ENSURE_MESSAGES_LOADED_SRC,
    INFLIGHT_HAS_VISIBLE_STATE_SRC,
    LOAD_SESSION_SRC,
    MERGE_PENDING_SESSION_MESSAGE_SRC,
    SELECT_LIVE_RECOVERY_INFLIGHT_SRC,
    _extract_function,
)

REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

LOAD_OLDER_AND_GENERATION_SRC = "\n\n".join(
    _extract_function(SESSIONS_SRC, name)
    for name in (
        "_bumpMessagesGeneration",
        "_advanceVisibleBaselineForServerKnownRows",
        "_loadOlderMessages",
    )
)

_NODE_SCRIPT_TEMPLATE = r'''
import assert from 'node:assert/strict';

// ---- deferred transport ---------------------------------------------------
function makeHarness() {
  const calls = [];
  const pending = [];
  function enqueue(url) {
    const entry = { url: String(url), landed: false };
    entry.promise = new Promise((resolve) => { entry._resolve = resolve; });
    calls.push(entry);
    return entry;
  }
  function take(url, options) {
    const want = String(url);
    const entry = calls.find((c) => !c.landed && c.url === want);
    if (!entry) {
      throw new Error('Unexpected API call: ' + want
        + '\nqueued=' + JSON.stringify(calls.map((c) => c.url)));
    }
    entry.landed = true;
    entry.options = options || null;
    pending.push(entry);
    return entry;
  }
  async function api(url, options) {
    return take(url, options).promise;
  }
  return { api, calls, pending, enqueue };
}

async function waitFor(host, url) {
  const want = String(url);
  for (let i = 0; i < 500; i++) {
    if (host.pending.some((p) => p.url === want)) return;
    await new Promise((r) => setImmediate(r));
  }
  throw new Error('request was never issued: ' + want);
}

// ---- module-scope state read by the extracted functions -------------------
function createEnvironment() {
  const draftMaps = [
    _composerDraftRevBySid, _composerDraftLastPayloadBySid,
    _composerDraftRestoreSuppressedUntilBySid, _composerDraftLocalFallbackBySid,
    _composerDraftLatestIntentSequenceBySid, _composerDraftPostPromisesBySid,
  ];
  draftMaps.forEach((m) => m.clear());
  _composerDraftKnownPayloadSessions.clear();

  globalThis.INFLIGHT = {};
  globalThis.S = {
    session: { session_id: 'sid-init', message_count: 0 },
    messages: [],
    toolCalls: [],
    pendingFiles: [],
    busy: false,
    activeStreamId: null,
  };
  globalThis._loadingSessionId = null;
  globalThis._loadingOlder = false;
  globalThis._loadSessionGeneration = 0;
  globalThis._messagesGeneration = 0;
  globalThis._pendingCarryForwardSnapshot = null;
  globalThis._messagesTruncated = false;
  globalThis._oldestIdx = 0;
  globalThis._messageRenderWindowSize = 0;
  globalThis._msgLimitMax = 5;
  globalThis._INITIAL_MSG_LIMIT = 2;
  globalThis._MSG_LIMIT_MAX = 500;
  globalThis.MESSAGE_RENDER_WINDOW_DEFAULT = 50;
  globalThis._programmaticScroll = false;
  globalThis._scrollPinned = true;

  globalThis._messageReloadLimitForSession = () => 2;
  globalThis._currentMessageRenderWindowSize = () => 1;
  globalThis._messageRenderableMessageCount = () => 2;
  // Renderability is not the seam under test; every fixture row carries a role.
  globalThis._messageIsRenderable = (m) => !!(m && m.role);

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
  globalThis._captureSameSessionForceReloadHint = () => {};
  globalThis._setSessionViewedCount = () => {};
  globalThis._acknowledgeSessionVisit = () => {};
  globalThis._sessionVisitHasUnreadState = () => false;
  globalThis.scheduleTodosRefresh = () => {};
  globalThis.startSessionStream = () => {};
  globalThis.syncTopbar = () => {};
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
  globalThis._syncToolCallsForLoadedMessages = () => {};
  globalThis.clearVisibleMessageRowCache = () => {};
  globalThis.clearLiveToolCards = () => {};
  globalThis._syncCtxIndicator = () => {};
  globalThis._renderPendingPromptsForActiveSession = () => {};
  globalThis._restoreComposerDraft = () => {};
  globalThis.renderSessionArtifacts = () => {};
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
  globalThis.autoResize = () => {};
  globalThis.showToast = (msg) => { toastCalls.push(String(msg)); };
  globalThis.history = { replaceState: () => {} };
  globalThis.localStorage = { removeItem: () => {}, setItem: () => {}, getItem: () => null };
  globalThis._appRootPath = () => '/';

  globalThis.renderMessages = (opts) => { renderCalls.push({ opts: opts || null, len: (S.messages || []).length }); };
  globalThis._captureMessageViewportAnchor = () => null;
  globalThis._restoreMessageViewportAnchor = () => false;
  globalThis._advanceVisibleBaselineForServerKnownRowsCalls = [];
  globalThis.performance = { now: () => 0 };
  globalThis.requestAnimationFrame = () => {};
  globalThis.window = {};
  globalThis._msgInner = { innerHTML: '' };
  globalThis.$ = (id) => {
    if (id === 'msgInner') return globalThis._msgInner;
    if (id === 'messages') return null;
    return null;
  };

  rearmCalls = 0;
  clearHintCalls = 0;
  toastCalls = [];
}
'''

# The composer-draft helpers and the INFLIGHT/merge helpers live in another
# module of the same file; the preamble below mirrors the module-scope
# declarations they close over.
_NODE_SCRIPT_PREAMBLE = r'''
let renderCalls = [];
let rearmCalls = 0;
let clearHintCalls = 0;
let toastCalls = [];

// ---- harness state --------------------------------------------------------
const _composerDraftRevBySid = new Map();
const _composerDraftLastPayloadBySid = new Map();
const _composerDraftRestoreSuppressedUntilBySid = new Map();
const _composerDraftLocalFallbackBySid = new Map();
const _composerDraftLatestIntentSequenceBySid = new Map();
const _composerDraftPostPromisesBySid = new Map();
const _composerDraftKnownPayloadSessions = new Set();
let _composerDraftIntentSequence = 0;
let _draftSaveTimer = null;
let _draftSaveTimerSid = null;
let _draftSaveTimerPayload = null;
let _draftSaveTimerRev = 0;
const _DRAFT_SAVE_DELAY_MS = 400;
const NEW_CHAT_DRAFT_SESSION_KEY = 'hermes-new-chat-draft-session';
const _COMPOSER_DRAFT_RESTORE_SUPPRESS_MS = 30000;

__COMPOSER_DRAFT_HELPERS_SRC__
__INFLIGHT_HAS_VISIBLE_STATE_SRC__
__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__
__MERGE_PENDING_SESSION_MESSAGE_SRC__
__LOAD_OLDER_AND_GENERATION_SRC__
__LOAD_SESSION_SRC__
__ENSURE_MESSAGES_LOADED_SRC__
__HARNESS__

// ---- fixture --------------------------------------------------------------
const SID = 'sid-a';
const OLDER_URL = '/api/session?session_id=sid-a&messages=1&resolve_model=0&msg_before=100&msg_limit=2';
const META_URL = '/api/session?session_id=sid-a&messages=0&resolve_model=0';
const MSGS_URL = '/api/session?session_id=sid-a&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1';

function rows(prefix, from, count) {
  const out = [];
  for (let i = 0; i < count; i++) {
    out.push({ role: i % 2 === 0 ? 'user' : 'assistant', content: prefix + (from + i) });
  }
  return out;
}

function resetState(host) {
  createEnvironment();
  renderCalls = [];
  globalThis.api = host.api;
  globalThis._loadingSessionId = null;
  globalThis._loadSessionGeneration = 1;
  globalThis._messagesGeneration = 0;
  globalThis._loadingOlder = false;
  globalThis._messagesTruncated = true;
  globalThis._oldestIdx = 100;
  S.session = {
    session_id: SID,
    message_count: 110,
    active_stream_id: null,
    regeneration_revision: 1,
    _visibleBaselineLocal: 10,
  };
  S.messages = rows('m', 100, 10);
  S.toolCalls = [];
  S.busy = false;
  S.activeStreamId = null;
}

function metaPayload() {
  return {
    session: {
      session_id: SID,
      message_count: 110,
      active_stream_id: null,
      resolve_model: 'qwen/qwq-32b-instruct',
    },
  };
}

function freshPayload() {
  return {
    session: {
      session_id: SID,
      _messages_truncated: true,
      _messages_offset: 200,
      messages: rows('fresh-', 200, 5),
      message_count: 205,
      tool_calls: [],
      regeneration_revision: 2,
    },
  };
}

function staleOlderPage() {
  return {
    session: {
      session_id: SID,
      _messages_truncated: true,
      _messages_offset: 90,
      messages: rows('m', 90, 10),
      message_count: 205,
      tool_calls: [],
    },
  };
}

function snapshot() {
  return {
    messages: (S.messages || []).map((m) => String(m.content)),
    oldestIdx: _oldestIdx,
    truncated: _messagesTruncated,
    generation: _messagesGeneration,
    renders: renderCalls.map((c) => c.len),
    loadingOlder: _loadingOlder,
    visibleBaselineLocal: S.session ? S.session._visibleBaselineLocal : null,
  };
}

// Defect case: the older-page continuation is already in flight when a
// same-session force reload starts; it resolves while the reload's own
// messages request is still pending.
async function runStalePageDuringPendingReload(opts) {
  const host = makeHarness();
  resetState(host);
  const older = host.enqueue(OLDER_URL);
  const olderRun = _loadOlderMessages();
  await waitFor(host, OLDER_URL);

  const meta = host.enqueue(META_URL);
  const msgs = host.enqueue(MSGS_URL);
  const loadRun = loadSession(SID, Object.assign({ force: true }, opts));
  await waitFor(host, META_URL);
  meta._resolve(metaPayload());
  await waitFor(host, MSGS_URL);

  const before = snapshot();
  older._resolve(staleOlderPage());
  await olderRun;
  const whilePending = snapshot();

  msgs._resolve(freshPayload());
  await loadRun;
  const final = snapshot();
  return { before, whilePending, final, apiCalls: host.pending.map((p) => p.url) };
}

// Control: no reload intervened, so the same page must still prepend.
async function runOlderPageWithoutReload() {
  const host = makeHarness();
  resetState(host);
  const older = host.enqueue(OLDER_URL);
  const olderRun = _loadOlderMessages();
  await waitFor(host, OLDER_URL);
  older._resolve(staleOlderPage());
  await olderRun;
  return { final: snapshot() };
}

const out = {
  keepStale: await runStalePageDuringPendingReload({ keepStaleUntilLoaded: true }),
  plainForce: await runStalePageDuringPendingReload({}),
  noReload: await runOlderPageWithoutReload(),
};
console.log(JSON.stringify(out));
'''.strip()


def _build_script() -> str:
    return (
        _NODE_SCRIPT_PREAMBLE.replace(
            "__COMPOSER_DRAFT_HELPERS_SRC__", COMPOSER_DRAFT_HELPERS_SRC
        )
        .replace("__INFLIGHT_HAS_VISIBLE_STATE_SRC__", INFLIGHT_HAS_VISIBLE_STATE_SRC)
        .replace(
            "__SELECT_LIVE_RECOVERY_INFLIGHT_SRC__", SELECT_LIVE_RECOVERY_INFLIGHT_SRC
        )
        .replace(
            "__MERGE_PENDING_SESSION_MESSAGE_SRC__", MERGE_PENDING_SESSION_MESSAGE_SRC
        )
        .replace(
            "__LOAD_OLDER_AND_GENERATION_SRC__", LOAD_OLDER_AND_GENERATION_SRC
        )
        .replace("__LOAD_SESSION_SRC__", LOAD_SESSION_SRC)
        .replace("__ENSURE_MESSAGES_LOADED_SRC__", ENSURE_MESSAGES_LOADED_SRC)
    ).replace("__HARNESS__", _NODE_SCRIPT_TEMPLATE.strip())


def _run_node(tmp_path: Path) -> dict:
    assert NODE is not None, "node is required"
    script = _build_script()
    script_path = tmp_path / "issue6421-loadsession-bump.mjs"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(script_path)],
        cwd=str(REPO),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"node produced no output\nstdout={completed.stdout}\nstderr={completed.stderr}"
    return json.loads(lines[-1])


OLD_TRANSCRIPT = [f"m{index}" for index in range(100, 110)]
FRESH_TRANSCRIPT = [f"fresh-{index}" for index in range(200, 205)]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_older_page_is_dropped_while_a_force_reload_is_pending(tmp_path):
    body = _run_node(tmp_path)

    # keepStaleUntilLoaded: the previous transcript is still on screen when the
    # reload starts, so a page prepended in the window rewinds the cursor in
    # front of the reader.
    kept = body["keepStale"]
    assert kept["whilePending"]["messages"] == OLD_TRANSCRIPT, (
        "a stale older page was installed on the transcript the force reload is "
        f"replacing: {kept['whilePending']['messages']}"
    )
    assert kept["whilePending"]["oldestIdx"] == 100, (
        "the stale older page rewound the paging cursor while the reload was "
        f"pending: {kept['whilePending']['oldestIdx']}"
    )
    assert kept["whilePending"]["renders"] == [], (
        "the stale older page rendered a previous-generation transcript during "
        f"the reload: {kept['whilePending']['renders']}"
    )

    # plain force:true (the shape the review named) clears the pane, so the
    # stale page must not repopulate it.
    plain = body["plainForce"]
    assert plain["before"]["messages"] == [], (
        "a plain force reload should have cleared the transcript before the "
        f"stale page landed: {plain['before']['messages']}"
    )
    assert plain["whilePending"]["messages"] == [], (
        "a stale older page repopulated the cleared transcript while the force "
        f"reload was pending: {plain['whilePending']['messages']}"
    )
    assert plain["whilePending"]["oldestIdx"] == 0, (
        "a stale older page set a paging cursor on the cleared transcript: "
        f"{plain['whilePending']['oldestIdx']}"
    )

    # The reload's own publish still owns the outcome in both shapes.
    for label, case in (("keepStaleUntilLoaded", kept), ("plain force", plain)):
        assert case["final"]["messages"] == FRESH_TRANSCRIPT, (
            f"{label}: the reload did not publish its transcript: {case['final']['messages']}"
        )
        assert case["final"]["oldestIdx"] == 200, (
            f"{label}: the reload did not publish its paging cursor: {case['final']['oldestIdx']}"
        )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_older_page_without_a_reload_still_prepends(tmp_path):
    body = _run_node(tmp_path)
    case = body["noReload"]["final"]
    assert case["messages"] == [f"m{index}" for index in range(90, 100)] + OLD_TRANSCRIPT, (
        "the generation guard must not turn pagination off when nothing "
        f"replaced the transcript: {case['messages']}"
    )
    assert case["oldestIdx"] == 90
