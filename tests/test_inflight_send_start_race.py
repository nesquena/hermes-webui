"""Regression coverage for send/start optimistic INFLIGHT races."""
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", src.index(")", start))
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[brace + 1 : i - 1]


def _source_slice(src: str, start: str, end: str, offset: int = 0) -> str:
    begin = src.index(start, offset)
    finish = src.index(end, begin)
    return src[begin:finish]


_LOAD_SESSION_START = SESSIONS_JS.index("async function loadSession(sid)")
_LOAD_SESSION_SWITCH_BODY = _source_slice(
    SESSIONS_JS,
    "  const _loadGeneration = ++_loadSessionGeneration;",
    "  // Phase 1: Load metadata only",
    _LOAD_SESSION_START,
)
_LOAD_SESSION_METADATA_BODY = _source_slice(
    SESSIONS_JS,
    "  S.session=data.session;",
    "  // _mergePendingSessionMessage is the global identity-aware helper shared by",
    _LOAD_SESSION_START,
)
_LOAD_SESSION_REATTACH_BODY = _source_slice(
    SESSIONS_JS,
    "    if(INFLIGHT[sid].reattach&&activeStreamId&&typeof attachLiveStream==='function')",
    "    syncTopbar();renderMessages",
    _LOAD_SESSION_START,
)
_LOAD_SESSION_ACTIVE_BODY = _source_slice(
    SESSIONS_JS,
    "      S.busy=true;\n      S.activeStreamId=activeStreamId;",
    "      const restoredAnchorScene",
    SESSIONS_JS.index("  // Phase 2b: Idle session", _LOAD_SESSION_START),
)
_LOAD_SESSION_IDLE_BODY = _source_slice(
    SESSIONS_JS,
    "      S.busy=false;",
    "\n    }\n  }\n\n  // Sync context usage indicator",
    SESSIONS_JS.index("  // Phase 2b: Idle session", _LOAD_SESSION_START),
)
_UPLOAD_PROGRESS_HIDE_BODY = _function_body(
    UI_JS, "_uploadPendingFilesHideProgressBar"
)
_UPLOAD_PROGRESS_SHOW_BODY = _function_body(
    UI_JS, "_uploadPendingFilesShowProgressBar"
)
_UPLOAD_PROGRESS_SYNC_BODY = _function_body(
    UI_JS, "_uploadPendingFilesSyncProgressForSession"
)
_UPLOAD_CURRENT_BODY = _function_body(UI_JS, "_uploadPendingFilesCurrentSession")
_UPLOAD_UPDATE_BODY = _function_body(UI_JS, "_uploadPendingFilesUpdateProgress")
_UPLOAD_BODY = _function_body(UI_JS, "uploadPendingFiles")
_CURRENT_PANE_BODY = _function_body(MESSAGES_JS, "_isSessionCurrentPane")
_PRE_START_STEP_BODY = _function_body(MESSAGES_JS, "_runOptionalPreStartUiStep")
_POST_START_STEP_BODY = _function_body(MESSAGES_JS, "_runOptionalPostStartUiStep")
_NEW_SESSION_BODY = _function_body(SESSIONS_JS, "newSession")


def test_send_preserves_optimistic_messages_across_chat_start_await():
    """send() must not dereference INFLIGHT[activeSid] after await without a fallback."""
    body = _function_body(MESSAGES_JS, "send")
    setup_idx = body.index("optimisticMessages=[...S.messages];")
    inflight_idx = body.index("INFLIGHT[activeSid]={messages:optimisticMessages")
    await_idx = body.index("const startData=await api('/api/chat/start'")
    save_idx = body.index("saveInflightState(activeSid,{streamId", await_idx)

    assert setup_idx < inflight_idx < await_idx < save_idx
    post_await = body[await_idx:save_idx]
    assert "if(!INFLIGHT[activeSid])" in post_await, (
        "send() should recreate the INFLIGHT entry if a session-list refresh pruned it"
    )
    assert "messages:INFLIGHT[activeSid].messages" not in body[save_idx : save_idx + 220], (
        "saveInflightState() should use a guarded local/current inflight object, not a blind nested read"
    )


def _strip_js_comments(src: str) -> str:
    """Remove // line comments and /* */ block comments so source-grep assertions
    match real statements, not comment text (a comment must not satisfy a guard
    regression check). Good enough for these structural checks — not a full JS parser."""
    import re
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def test_stale_inflight_purge_preserves_current_send_before_stream_id_exists():
    """Sidebar cleanup must not delete the active send before /api/chat/start responds."""
    body = _strip_js_comments(_function_body(SESSIONS_JS, "_purgeStaleInflightEntries"))

    assert "_sendInProgress" in body and "_sendInProgressSid" in body, (
        "_purgeStaleInflightEntries() should skip the current send while start is in progress"
    )
    skip_idx = body.index("_sendInProgress")
    delete_idx = body.index("delete INFLIGHT[sid];")
    assert skip_idx < delete_idx, "the current-send skip must run before any purge deletion"
    # The skip must be a real guarded `continue`, not just a token in a comment (#4354/#2689).
    assert "continue;" in body[skip_idx:delete_idx], (
        "the current-send skip must be an actual `continue` before the purge deletion"
    )


def test_idle_reconcile_preserves_current_send_before_stream_id_exists():
    """The list-poll idle reconciler must not clear the session that is mid-send.

    #4354 removed the _sendInProgress guard here to unstick a hung indicator;
    #2689's start-race protection must still cover the ONE session actively
    mid-send (server row is briefly idle during /api/chat/start). Verified
    against comment-stripped source so a comment can't satisfy the check.
    """
    body = _strip_js_comments(_function_body(SESSIONS_JS, "_reconcileActiveSessionIdleStateFromList"))
    assert "_sendInProgress" in body and "_sendInProgressSid" in body, (
        "_reconcileActiveSessionIdleStateFromList() must skip the active mid-send session"
    )
    guard_idx = body.index("_sendInProgress")
    clear_idx = body.index("S.busy=false")
    assert guard_idx < clear_idx, "the mid-send skip must run before the idle clear"


def test_send_clears_stale_busy_state_before_queue_branch():
    """A stale client-only busy flag must not divert a new user turn into the invisible queue."""
    body = _function_body(MESSAGES_JS, "send")

    assert "_clearStaleBusyStateBeforeSend" in body, (
        "send() should reconcile client-only stale busy state before deciding busy/queue mode"
    )
    reconcile_idx = body.index("_clearStaleBusyStateBeforeSend")
    busy_branch_idx = body.index("if(S.busy||compressionRunning)")
    chat_start_idx = body.index("api('/api/chat/start'")
    assert reconcile_idx < busy_branch_idx < chat_start_idx, (
        "stale busy reconciliation must run before the queue branch and before /api/chat/start"
    )


def test_pre_start_optimistic_ui_helpers_cannot_block_chat_start():
    """Optional optimistic UI helpers must not strand a local bubble before /api/chat/start."""
    body = _function_body(MESSAGES_JS, "send")
    helper_body = _function_body(MESSAGES_JS, "_runOptionalPreStartUiStep")

    optimistic_idx = body.index("S.messages.push(userMsg);renderMessages();setBusy(true);")
    chat_start_idx = body.index("api('/api/chat/start'")
    pre_start = body[optimistic_idx:chat_start_idx]

    assert "try" in helper_body and "catch" in helper_body, (
        "optional pre-start UI helper wrapper must catch errors before /api/chat/start"
    )
    assert "setStatus(`UI warning before send:" not in helper_body, (
        "non-fatal pre-start UI helper failures should stay in the console; visible status flashes "
        "look like real send errors even though /api/chat/start continues"
    )
    assert "_runOptionalPreStartUiStep" in pre_start, (
        "send() should wrap optimistic sidebar/title/polling helpers before /api/chat/start"
    )
    assert "ensureLiveWorklogShell" in pre_start or "appendThinking('',{pending:true})" in pre_start, (
        "send() should render an assistant-side pending shell before /api/chat/start"
    )
    assert "upsertActiveSessionForLocalTurn" in pre_start and "applySessionTitleUpdate" in pre_start


def test_pre_start_optimistic_block_cannot_prevent_chat_start():
    """Any pre-start UI/storage exception must still fall through to /api/chat/start."""
    body = _function_body(MESSAGES_JS, "send")
    optimistic_idx = body.index("S.messages.push(userMsg);renderMessages();setBusy(true);")
    chat_start_idx = body.index("api('/api/chat/start'")
    pre_start = body[optimistic_idx:chat_start_idx]

    assert "}catch(preStartError){" in pre_start, (
        "The whole optimistic pre-start block needs a catch, not only individual optional helpers"
    )
    assert "continuing to /api/chat/start" in pre_start, (
        "The recovery path should document that chat/start must still execute"
    )
    assert pre_start.rindex("}catch(preStartError){") < chat_start_idx, (
        "pre-start catch must be before the /api/chat/start call"
    )


def test_post_start_bookkeeping_errors_cannot_block_live_attach():
    """Any optional post-start UI/bookkeeping failure should be recoverable once stream_id exists."""
    body = _function_body(MESSAGES_JS, "send")
    helper_body = _function_body(MESSAGES_JS, "_runOptionalPostStartUiStep")
    assert "optional post-start UI step failed" in helper_body, (
        "post-start optional helper failures should stay in warning logs, not user-facing error bubbles"
    )

    chat_start_idx = body.index("const startData=await api('/api/chat/start'")
    catch_idx = body.index("}catch(e){", chat_start_idx)
    optional_idx = body.index("_runOptionalPostStartUiStep('post-start ui/bookkeeping'", catch_idx)
    stream_id_idx = body.index("streamId = postStartData ? postStartData.stream_id : null;", catch_idx)
    attach_idx = body.index("attachLiveStream(activeSid, streamId, uploadedNames);")
    assert catch_idx < stream_id_idx < optional_idx < attach_idx, (
        "stream-id setup, post-start UI/bookkeeping, and attach must run after successful API catch"
    )
    assert "S.messages.push({role:'assistant',content:`**Error:**" not in body[optional_idx : attach_idx], (
        "post-start optional failures should not append assistant error messages before stream attach"
    )


@pytest.mark.parametrize("pane_state", ("assigned", "loading"))
@pytest.mark.parametrize("worklog_throws", (False, True))
def test_delayed_chat_start_keeps_a_owned_state_out_of_visible_b_pane(pane_state, worklog_throws):
    """A delayed A start must finish in A's recovery state after the pane switches to B."""
    send_body = _function_body(MESSAGES_JS, "send")
    script = f"""
const assert = require('assert');
let switched = false;
let releaseStart;
let startCalled;
const startCalledPromise = new Promise(resolve => startCalled = resolve);
const startResponse = new Promise(resolve => releaseStart = resolve);
const calls = {{
  attach: [], saved: [], marked: [], visibleAfterSwitch: [], persistedModels: [],
  modelControls: [], worklog: 0, topbar: 0, busyUi: 0, sidebar: 0,
}};
const elements = {{
  msg: {{value: 'A prompt'}},
  modelSelect: {{value: 'A requested model'}},
}};
function $(id) {{ return elements[id] || null; }}
const document = {{querySelector: () => null}};
const localStorage = {{
  values: Object.create(null),
  getItem(key) {{ return this.values[key] || null; }},
  setItem(key, value) {{ this.values[key] = String(value); }},
  removeItem(key) {{ delete this.values[key]; }},
}};
const window = {{_defaultMessageMode: 'steer', _defaultModel: 'A requested model', _activeProvider: 'provider-a'}};
const A = {{
  session_id: 'A', title: 'A', model: 'A requested model', model_provider: 'provider-a',
  workspace: '/a', active_turn_token: 'A-old-token', pending_started_at: 10,
}};
const B = {{
  session_id: 'B', title: 'B', model: 'B visible model', model_provider: 'provider-b',
  workspace: '/b', active_turn_token: 'B-token', pending_started_at: 20,
  active_stream_id: 'B-stream',
}};
const S = {{
  session: A,
  messages: [{{role: 'assistant', content: 'A history'}}],
  pendingFiles: [{{name: 'a.txt'}}],
  busy: false,
  activeStreamId: null,
  activeProfile: 'profile-a',
  toolCalls: [],
  todos: [],
  todoStateMeta: null,
}};
const INFLIGHT = Object.create(null);
let _sendInProgress = false;
let _sendInProgressSid = null;
let _pendingSelections = [];
let _forcedSkillDirectivePending = null;
let _queueDrainSid = null;
let _approvalSessionId = 'A';
let _clarifySessionId = 'A';
let uploadedName = 'a.txt';
let _loadingSessionId = null;
function _isSessionCurrentPane(sid) {{
  if(!sid || !S.session || S.session.session_id!==sid) return false;
  if(typeof _loadingSessionId!=='undefined' && _loadingSessionId && _loadingSessionId!==sid) return false;
  return true;
}}
function visibleCall(name) {{
  if (switched) calls.visibleAfterSwitch.push(name);
}}
function renderMessages() {{
  if ({json.dumps(worklog_throws)}) throw new Error('optimistic render failed');
}}
function renderTray() {{}}
function autoResize() {{}}
function setComposerStatus() {{}}
function setStatus() {{}}
function setBusy(value) {{ S.busy = value; visibleCall('busy'); }}
function updateSendBtn() {{ calls.busyUi++; visibleCall('send'); }}
function ensureLiveWorklogShell() {{
  calls.worklog++;
  visibleCall('worklog');
  if ({json.dumps(worklog_throws)}) throw new Error('worklog failed');
}}
function appendThinking() {{ calls.worklog++; visibleCall('worklog'); }}
function syncTopbar() {{ calls.topbar++; visibleCall('topbar'); }}
function syncModelChip() {{ calls.modelControls.push(['provider-chip', S.session && S.session.session_id]); visibleCall('model'); }}
function _applyModelToDropdown() {{ calls.modelControls.push(['dropdown', S.session && S.session.session_id]); visibleCall('model'); }}
function _writePersistedModelState(model, provider) {{ calls.persistedModels.push([model, provider, S.session && S.session.session_id]); }}
function showLiveRunStatus() {{ visibleCall('worklog-status'); }}
function upsertActiveSessionForLocalTurn() {{ calls.sidebar++; visibleCall('sidebar'); }}
function applySessionTitleUpdate() {{ calls.sidebar++; visibleCall('sidebar'); }}
function renderSessionList() {{ calls.sidebar++; return Promise.resolve(); }}
function renderSessionListFromCache() {{ calls.sidebar++; visibleCall('sidebar'); }}
function startApprovalPolling() {{}}
function startClarifyPolling() {{}}
function stopApprovalPolling() {{ visibleCall('approval'); }}
function stopClarifyPolling() {{ visibleCall('clarify'); }}
function stopApprovalPollingForSession() {{}}
function stopClarifyPollingForSession() {{}}
function hideApprovalCard() {{ visibleCall('approval'); }}
function hideClarifyCard() {{ visibleCall('clarify'); }}
function removeThinking() {{ visibleCall('worklog'); }}
function clearLiveToolCards() {{}}
function _fetchYoloState() {{}}
function markInflight(sid, streamId) {{
  const payload = JSON.stringify({{sid, streamId, ts: Date.now()}});
  localStorage.setItem('hermes-webui-inflight', payload);
  calls.marked.push([sid, streamId]);
}}
function saveInflightState(sid, state) {{
  calls.saved.push([sid, JSON.parse(JSON.stringify(state))]);
}}
function clearInflightState() {{}}
function clearOptimisticSessionStreaming() {{}}
function attachLiveStream(sid, streamId, uploaded) {{ calls.attach.push([sid, streamId, uploaded]); }}
function _clearComposerDraft() {{ return Promise.resolve(); }}
function uploadPendingFiles() {{ return Promise.resolve([{{name: uploadedName, path: uploadedName}}]); }}
function _clearStaleBusyStateBeforeSend() {{}}
function isCompressionUiRunning() {{ return false; }}
function _composerTextWithPendingSelections() {{ return elements.msg.value; }}
function _flushSelectionBlocksToComposer() {{}}
function _chatPayloadModelState() {{
  return {{model: S.session.model, model_provider: S.session.model_provider}};
}}
function _readPendingSessionModel() {{ return null; }}
function _opaqueActiveTurnToken(value) {{ return typeof value === 'string' && value.trim() ? value : null; }}
function _restoreComposerDraftAfterFailedSend() {{}}
function _runOptionalPreStartUiStep(label, fn) {{{_PRE_START_STEP_BODY}}}
function _runOptionalPostStartUiStep(label, fn) {{{_POST_START_STEP_BODY}}}
function api(path, options) {{
  assert.strictEqual(path, '/api/chat/start');
  startCalled();
  return startResponse;
}}
const startResult = {{
  stream_id: 'A-stream', active_turn_token: 'A-token', pending_started_at: 30,
  title: 'A server title', effective_model: 'A effective model',
  effective_model_provider: 'provider-a-effective',
}};
async function send() {{{send_body}}}

(async () => {{
  const sendPromise = send();
  await startCalledPromise;

  const paneState = {json.dumps(pane_state)};
  if (paneState === 'assigned') {{
    S.session = B;
    S.messages = [{{role: 'user', content: 'B existing'}}, {{role: 'assistant', content: 'B reply'}}];
    S.activeStreamId = 'B-stream';
    S.busy = true;
    S.activeProfile = 'profile-b';
  }} else {{
    S.messages = [];
    _loadingSessionId = 'B';
  }}
  INFLIGHT.B = {{
    streamId: 'B-stream', activeTurnToken: 'B-token',
    messages: S.messages.slice(), uploaded: [], toolCalls: [],
  }};
  const bMarker = JSON.stringify({{sid: 'B', streamId: 'B-stream', ts: 123}});
  localStorage.setItem('hermes-webui-inflight', bMarker);
  const visibleSessionBefore = JSON.parse(JSON.stringify(S.session));
  const visibleMessagesBefore = JSON.parse(JSON.stringify(S.messages));
  const visibleActiveStreamBefore = S.activeStreamId;
  const visibleBusyBefore = S.busy;
  const visibleInflightBefore = JSON.parse(JSON.stringify(INFLIGHT.B));
  switched = true;

  releaseStart(startResult);
  await sendPromise;

  assert.deepStrictEqual(S.session, visibleSessionBefore);
  assert.deepStrictEqual(S.messages, visibleMessagesBefore);
  assert.strictEqual(S.activeStreamId, visibleActiveStreamBefore);
  assert.strictEqual(S.busy, visibleBusyBefore);
  if (paneState === 'assigned') {{
    assert.strictEqual(S.activeStreamId, 'B-stream');
    assert.strictEqual(S.busy, true);
  }}
  assert.deepStrictEqual(INFLIGHT.B, visibleInflightBefore);
  assert.strictEqual(localStorage.getItem('hermes-webui-inflight'), bMarker);
  assert.strictEqual(calls.attach.length, 0);
  assert.strictEqual(calls.worklog, 1);
  assert.strictEqual(calls.topbar, 0);
  assert.strictEqual(calls.persistedModels.length, 0);
  assert.strictEqual(calls.modelControls.length, 0);
  assert.strictEqual(calls.busyUi, {0 if worklog_throws else 1});
  assert.deepStrictEqual(calls.visibleAfterSwitch, []);

  const a = INFLIGHT.A;
  assert(a);
  assert.strictEqual(a.streamId, 'A-stream');
  assert.strictEqual(a.activeTurnToken, 'A-token');
  assert.strictEqual(a.reattach, true);
  const aUser = a.messages.find(row => row && row.role === 'user' && row.content === 'A prompt');
  assert(aUser);
  assert.deepStrictEqual(aUser.attachments, ['a.txt']);
  assert.strictEqual(aUser._active_turn_token, 'A-token');
  assert.deepStrictEqual(calls.marked, []);
  const persisted = calls.saved.filter(item => item[0] === 'A').at(-1)[1];
  assert.strictEqual(persisted.streamId, 'A-stream');
  assert.strictEqual(persisted.activeTurnToken, 'A-token');
  assert.deepStrictEqual(persisted.messages.find(row => row.content === 'A prompt').attachments, ['a.txt']);
}})().catch(error => {{
  console.error(error.stack || error);
  process.exitCode = 1;
}});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize(
    "pane_state,start_outcome",
    (
        ("assigned", "success"),
        ("assigned", "error"),
        ("loading", "success"),
        ("loading", "error"),
        ("visible", "404"),
        ("assigned", "404"),
        ("loading", "404"),
    ),
)
def test_delayed_upload_skill_and_start_keep_a_ownership_through_production_reconcile(
    pane_state, start_outcome
):
    """Delayed A work must not repaint or reconcile an installed/pending B pane."""
    send_body = _function_body(MESSAGES_JS, "send")
    restore_body = _function_body(MESSAGES_JS, "_restoreComposerDraftAfterFailedSend")
    idle_row_body = _function_body(SESSIONS_JS, "_isServerIdleSessionRow")
    reconcile_body = _function_body(SESSIONS_JS, "_reconcileActiveSessionIdleStateFromList")
    script = f"""
const assert = require('assert');
let switched = false;
let releaseUpload, releaseSkill, releaseStart, rejectStart, resolveSkillGate;
let uploadCalled, skillCalled, startCalled, bMetadataCalled;
const uploadCalledPromise = new Promise(resolve => uploadCalled = resolve);
const skillCalledPromise = new Promise(resolve => skillCalled = resolve);
const startCalledPromise = new Promise(resolve => startCalled = resolve);
const bMetadataCalledPromise = new Promise(resolve => bMetadataCalled = resolve);
const uploadResponse = new Promise(resolve => releaseUpload = resolve);
const skillGate = new Promise(resolve => resolveSkillGate = resolve);
const skillResponse = {{then(resolve, reject) {{ skillCalled(); return skillGate.then(resolve, reject); }}}};
releaseSkill = value => resolveSkillGate(value);
const startResponse = new Promise((resolve, reject) => {{ releaseStart = resolve; rejectStart = reject; }});
let releaseBMetadata;
const bMetadataResponse = new Promise(resolve => releaseBMetadata = resolve);
const calls = {{
  attach: [], saved: [], marked: [], uploaded: [], requests: [], drafts: [],
  visibleAfterSwitch: [], renderSessionList: 0, reconcile: 0, reload: 0,
  optimisticOwners: [], renderOwners: [],
  stopped: [], cleared: [], sidebar: 0, worklog: 0, topbar: 0,
  modelControls: [], persistedModels: [], busyUi: 0,
}};
const elements = {{
  msg: {{value: 'A prompt'}},
  modelSelect: {{value: 'A requested model'}},
  emptyState: {{style: {{display: ''}}}},
  msgInner: {{innerHTML: ''}},
  uploadBar: {{style: {{width: '0%'}}}},
  uploadBarWrap: {{classList: {{add() {{}}, remove() {{}}, contains() {{return false;}}}}, dataset: {{}}}},
}};
function $(id) {{ return elements[id] || null; }}
const document = {{querySelector: () => null}};
const localStorage = {{
  values: Object.create(null),
  getItem(key) {{ return this.values[key] || null; }},
  setItem(key, value) {{ this.values[key] = String(value); }},
  removeItem(key) {{ delete this.values[key]; }},
}};
const window = {{_defaultMessageMode: 'steer', _defaultModel: 'A default', _activeProvider: 'provider-a'}};
const historyCalls = [];
const history = {{replaceState(...args) {{ historyCalls.push(args); }}}};
const A = {{
  session_id: 'A', title: 'Untitled', model: 'A requested model', model_provider: 'provider-a',
  workspace: '/a', profile: 'session-profile-a', active_turn_token: 'A-old-token',
  pending_started_at: 10, active_stream_id: 'A-stream', pending_attachments: [],
}};
const B = {{
  session_id: 'B', title: 'B', model: 'B visible model', model_provider: 'provider-b',
  workspace: '/b', profile: 'session-profile-b', active_turn_token: 'B-token',
  pending_started_at: 20, active_stream_id: 'B-stream', pending_attachments: ['b.txt'],
}};
const serverSessions = {{
  A: {{session: A, messages: [{{role: 'assistant', content: 'A history'}}]}},
  B: {{session: B, messages: [{{role: 'user', content: 'B existing'}}, {{role: 'assistant', content: 'B reply'}}]}},
}};
const cards = {{approval: 'B approval', clarify: 'B clarify', live: 'B live', tools: ['B tool']}};
const polling = {{approval: 'B approval poll', clarify: 'B clarify poll'}};
const sidebarState = {{rows: ['A', 'B'], selected: 'B'}};
const S = {{
  session: A,
  messages: [{{role: 'assistant', content: 'A history'}}],
  pendingFiles: [{{name: 'a.txt'}}],
  busy: false, activeStreamId: null, activeProfile: 'profile-a',
  toolCalls: [], todos: [{{text: 'A todo'}}], todoStateMeta: {{owner: 'A'}},
}};
const INFLIGHT = Object.create(null);
const _sessionStreamingById = new Map();
let _sendInProgress = false;
let _sendInProgressSid = null;
let _pendingSelections = [];
let _forcedSkillDirectivePending = {{sessionId: 'A', promise: skillResponse}};
let _queueDrainSid = null;
let _approvalSessionId = 'A';
let _clarifySessionId = 'A';
let _loadingSessionId = null, _loadSessionGeneration = 0;
let _messageUserUnpinned = false, _scrollPinned = true, _loadingOlder = false;
let _messagesTruncated = false, _oldestIdx = 0, _pendingCarryForwardSnapshot = null;
const _uploadPendingFilesProgressBySession = new Map();
function _isSessionCurrentPane(sid) {{
  if(!sid || !S.session || S.session.session_id!==sid) return false;
  if(typeof _loadingSessionId!=='undefined' && _loadingSessionId && _loadingSessionId!==sid) return false;
  return true;
}}
function visibleCall(name) {{ if(switched) calls.visibleAfterSwitch.push(name); }}
function renderMessages() {{ visibleCall('messages'); }}
function renderTray() {{ visibleCall('tray'); }}
function autoResize() {{ visibleCall('resize'); }}
let composerStatus = '';
function setComposerStatus(value) {{ composerStatus = String(value || ''); visibleCall('composer-status'); }}
function setStatus() {{ visibleCall('status'); }}
function setBusy(value) {{ S.busy = value; visibleCall('busy'); }}
function updateSendBtn() {{ calls.busyUi++; visibleCall('send-button'); }}
function ensureLiveWorklogShell() {{ calls.worklog++; visibleCall('worklog'); }}
function appendThinking() {{ calls.worklog++; visibleCall('worklog'); }}
function syncTopbar() {{ calls.topbar++; visibleCall('topbar'); }}
function syncModelChip() {{ calls.modelControls.push(['provider-chip', S.session && S.session.session_id]); visibleCall('model'); }}
function _applyModelToDropdown() {{ calls.modelControls.push(['dropdown', S.session && S.session.session_id]); visibleCall('model'); }}
function _writePersistedModelState(model, provider) {{ calls.persistedModels.push([model, provider, S.session && S.session.session_id]); }}
function showLiveRunStatus() {{ cards.live = 'changed'; visibleCall('live-status'); }}
function upsertActiveSessionForLocalTurn() {{ calls.sidebar++; sidebarState.rows = ['changed']; visibleCall('sidebar'); }}
function applySessionTitleUpdate() {{ calls.sidebar++; sidebarState.rows = ['changed-title']; visibleCall('sidebar'); }}
function renderSessionListFromCache() {{ calls.sidebar++; sidebarState.rows = ['changed-cache']; visibleCall('sidebar'); }}
function startApprovalPolling() {{ polling.approval = 'changed'; visibleCall('approval-poll'); }}
function startClarifyPolling() {{ polling.clarify = 'changed'; visibleCall('clarify-poll'); }}
function stopApprovalPolling() {{ calls.stopped.push('approval-visible'); visibleCall('approval'); }}
function stopClarifyPolling() {{ calls.stopped.push('clarify-visible'); visibleCall('clarify'); }}
function stopApprovalPollingForSession(sid) {{ calls.stopped.push('approval:' + sid); }}
function stopClarifyPollingForSession(sid) {{ calls.stopped.push('clarify:' + sid); }}
function hideApprovalCard() {{ cards.approval = null; visibleCall('approval'); }}
function hideClarifyCard() {{ cards.clarify = null; visibleCall('clarify'); }}
function hideLiveRunStatus() {{ cards.live = null; visibleCall('live-status'); }}
function removeThinking() {{ cards.live = null; visibleCall('worklog'); }}
function clearLiveToolCards() {{ cards.tools = []; visibleCall('tools'); }}
function _fetchYoloState() {{ visibleCall('yolo'); }}
function _forgetObservedStreamingSession() {{}}
function _scheduleActiveSessionIdleReload() {{ calls.reload++; }}
function _isServerIdleSessionRow(row) {{{idle_row_body}}}
function _reconcileActiveSessionIdleStateFromList(serverRows) {{{reconcile_body}}}
function renderSessionList() {{
  calls.renderSessionList++;
  calls.renderOwners.push(S.session && S.session.session_id || null);
  return Promise.resolve().then(() => {{
    calls.reconcile++;
    _reconcileActiveSessionIdleStateFromList([
      {{session_id: 'A', is_streaming: true, active_stream_id: 'A-stream', pending_user_message: 'A prompt', pending_started_at: 30}},
      {{session_id: 'B', is_streaming: false, active_stream_id: null, pending_user_message: null, has_pending_user_message: false, pending_started_at: null}},
    ]);
  }});
}}
function markInflight(sid, streamId) {{
  localStorage.setItem('hermes-webui-inflight', JSON.stringify({{sid, streamId, ts: 123}}));
  calls.marked.push([sid, streamId]);
}}
function saveInflightState(sid, state) {{ calls.saved.push([sid, JSON.parse(JSON.stringify(state))]); }}
function clearInflightState(sid) {{ calls.cleared.push(sid); }}
function clearOptimisticSessionStreaming(sid) {{
  calls.cleared.push('optimistic:' + sid);
  calls.optimisticOwners.push(S.session && S.session.session_id || null);
  visibleCall('clear-optimistic');
}}
function attachLiveStream(sid, streamId, uploaded, options) {{
  calls.attach.push([sid, streamId, uploaded, options || null]);
  if(sid === 'B') cards.live = 'B live';
}}
function _clearComposerDraft(sid, text, files) {{ calls.drafts.push({{sid, text, files: files.map(file => file.name)}}); return Promise.resolve(); }}
function _saveComposerDraftNow(sid, text, files) {{ calls.drafts.push({{sid, text, files: files.map(file => file.name)}}); return Promise.resolve(); }}
function uploadPendingFiles(options) {{
  calls.uploaded.push({{sessionId: options.sessionId, files: options.files.map(file => file.name)}});
  uploadCalled();
  return uploadResponse;
}}
function _clearStaleBusyStateBeforeSend() {{}}
function isCompressionUiRunning() {{ return false; }}
function _composerTextWithPendingSelections() {{ return elements.msg.value; }}
function _flushSelectionBlocksToComposer() {{}}
function _chatPayloadModelState() {{
  return {{model: S.session.model, model_provider: S.session.model_provider}};
}}
function _readPendingSessionModel(sid) {{
  return sid === 'A' ? {{model: 'A requested model', model_provider: 'provider-a'}} : null;
}}
function _clearPendingSessionModel(sid) {{ calls.cleared.push('pick:' + sid); }}
function _opaqueActiveTurnToken(value) {{ return typeof value === 'string' && value.trim() ? value : null; }}
function _restoreComposerDraftAfterFailedSend(draftText, filesSnapshot, sid, clearPromise) {{
{restore_body}
}}
function _runOptionalPreStartUiStep(label, fn) {{ return fn(); }}
function _runOptionalPostStartUiStep(label, fn) {{ return fn(); }}
function _dismissHandoffHint() {{}}
function queueSessionMessage() {{}}
function updateQueueBadge() {{}}
function showToast() {{ visibleCall('toast'); }}
function _clearQueueCardDisplay() {{}}
function _clearDeferredActiveSessionExternalRefresh() {{}}
function _clearSameSessionForceReloadHint() {{}}
function _captureSameSessionForceReloadHint() {{}}
function _rearmActiveSessionStream() {{}}
function _updateYoloPill() {{}}
function _clearEmptyComposerModelOverride() {{}}
function clearCompressionUi() {{}}
function stopSessionStream() {{}}
function closeOtherLiveStreams() {{}}
function snapshotLiveTurnHtmlForSession() {{}}
function _deferWorkspaceRefreshForSession() {{}}
function _setActiveSessionUrl() {{}}
function startSessionStream() {{}}
function _hydrateTodosFromSession(session) {{
  S.todos = [{{text: session.session_id + ' todo'}}];
  S.todoStateMeta = {{owner: session.session_id}};
}}
function _applyPendingSessionModelForSession() {{ return false; }}
function _resolveSessionModelForDisplaySoon(sid) {{
  const session = serverSessions[sid].session;
  S.activeProfile = session.profile;
  elements.modelSelect.value = session.model;
  window._defaultModel = session.model;
  window._activeProvider = session.model_provider;
}}
function _acknowledgeSessionVisit(sid) {{ sidebarState.selected = sid; }}
localStorage.setItem('hermes-webui-session', 'A');
    function _uploadPendingFilesHideProgressBar() {{
    {_UPLOAD_PROGRESS_HIDE_BODY}
    }}
    function _uploadPendingFilesShowProgressBar(owner, percent) {{
    {_UPLOAD_PROGRESS_SHOW_BODY}
    }}
    function _uploadPendingFilesSyncProgressForSession(sessionId) {{
{_UPLOAD_PROGRESS_SYNC_BODY}
}}
function loadInflightState(sid) {{
  if(sid !== 'B') return null;
  return {{streamId: 'B-stream', messages: serverSessions.B.messages.slice(), uploaded: ['b.txt'], toolCalls: [{{id: 'B-tool'}}], activeTurnToken: 'B-token'}};
}}
async function _ensureMessagesLoaded(sid) {{
  S.messages = INFLIGHT[sid] ? INFLIGHT[sid].messages.map(row => ({{...row}})) : serverSessions[sid].messages.map(row => ({{...row}}));
}}
async function loadSession(sid, opts = {{}}) {{
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid === sid;
  {_LOAD_SESSION_SWITCH_BODY}
  const data = await api(`/api/session?session_id=${{encodeURIComponent(sid)}}&messages=0&resolve_model=0`);
  if(!data || !_isCurrentLoad()) return;
  {_LOAD_SESSION_METADATA_BODY}
  if(!INFLIGHT[sid] && activeStreamId && typeof loadInflightState === 'function') {{
    const stored = loadInflightState(sid, activeStreamId);
    if(stored) INFLIGHT[sid] = {{...stored, reattach: true}};
  }}
  await _ensureMessagesLoaded(sid, {{force: _keepStaleUntilLoaded, loadGeneration: _loadGeneration}});
  if(!_isCurrentLoad()) return;
  if(activeStreamId && INFLIGHT[sid]) {{
    S.busy = true;
    S.activeStreamId = activeStreamId;
    let didReconnect = false;
    {_LOAD_SESSION_REATTACH_BODY}
    setComposerStatus('');
    startApprovalPolling(sid);
    startClarifyPolling(sid);
  }} else if(activeStreamId) {{
    {_LOAD_SESSION_ACTIVE_BODY}
  }} else {{
    {_LOAD_SESSION_IDLE_BODY}
  }}
  if(_isCurrentLoad()) _loadingSessionId = null;
}}
function _appRootPath() {{ return '/'; }}
function api(path, options) {{
  if(path.startsWith('/api/session?')) {{
    const sid = new URLSearchParams(path.split('?')[1]).get('session_id');
    if(sid === 'B') {{ bMetadataCalled(); return bMetadataResponse; }}
    return Promise.resolve(serverSessions[sid]);
  }}
  assert.strictEqual(path, '/api/chat/start');
  calls.requests.push(JSON.parse(options.body));
  startCalled();
  return startResponse;
}}
const startResult = {{
  stream_id: 'A-stream', active_turn_token: 'A-token', pending_started_at: 30,
  title: 'A server title', effective_model: 'A effective model',
  effective_model_provider: 'provider-a-effective',
}};
async function send() {{{send_body}}}

function bSnapshot() {{
  return JSON.stringify({{
    session: S.session, messages: S.messages, pendingFiles: S.pendingFiles,
    busy: S.busy, activeStreamId: S.activeStreamId, activeProfile: S.activeProfile,
    toolCalls: S.toolCalls, todos: S.todos, todoStateMeta: S.todoStateMeta,
    composerStatus, composer: elements.msg.value, modelSelect: elements.modelSelect.value,
    modelDefault: window._defaultModel, activeProvider: window._activeProvider,
    uploadWidth: elements.uploadBar.style.width,
    uploadOwner: elements.uploadBarWrap.dataset.uploadSessionId || null,
    cards, polling, sidebarState, inflight: INFLIGHT.B,
    marker: localStorage.getItem('hermes-webui-inflight'),
    savedSession: localStorage.getItem('hermes-webui-session'),
  }});
}}

(async () => {{
  const sendPromise = send();
  await uploadCalledPromise;
  _uploadPendingFilesProgressBySession.set('A', {{percent: 42}});
  _uploadPendingFilesShowProgressBar('A', 42);
  const paneState = {json.dumps(pane_state)};
  const bLoadPromise = paneState === 'visible' ? null : loadSession('B');
  if (paneState !== 'visible') await bMetadataCalledPromise;
  if (paneState === 'visible') {{
    switched = false;
  }} else if (paneState === 'assigned') {{
    releaseBMetadata(serverSessions.B);
    await bLoadPromise;
    switched = true;
  }} else {{
    switched = true;
  }}
  let visibleB = paneState === 'visible' ? null : bSnapshot();
  if (paneState === 'loading') {{
    assert.strictEqual(JSON.parse(visibleB).composerStatus, '');
  }}
  if (paneState === 'assigned') {{
    assert.strictEqual(JSON.parse(visibleB).composerStatus, '');
    assert.strictEqual(JSON.parse(visibleB).uploadWidth, '0%');
    assert.strictEqual(JSON.parse(visibleB).uploadOwner, null);
  }}
  const attachBefore = calls.attach.length;
  releaseUpload([{{name: 'a.txt', path: '/a/a.txt'}}]);
  await skillCalledPromise;
  assert.strictEqual(calls.uploaded[0].sessionId, 'A');
  assert.deepStrictEqual(calls.uploaded[0].files, ['a.txt']);
  if (paneState !== 'visible') assert.strictEqual(bSnapshot(), visibleB);
  releaseSkill({{directive: 'Use A skill', name: 'skill-a', content: 'A-only context'}});
  await startCalledPromise;
  const request = calls.requests[0];
  assert.strictEqual(request.session_id, 'A');
  if ({json.dumps(start_outcome)} !== '404') {{
    assert.strictEqual(request.model, 'A requested model');
    assert.strictEqual(request.model_provider, 'provider-a');
    assert.strictEqual(request.workspace, '/a');
    assert.strictEqual(request.profile, 'profile-a');
    assert.strictEqual(request.explicit_model_pick, true);
    assert.strictEqual(request.attachments[0].path, '/a/a.txt');
    assert(request.message.includes('A-only context'));
  }}
  if (paneState !== 'visible') assert.strictEqual(bSnapshot(), visibleB);
  if ({json.dumps(start_outcome)} === 'success') releaseStart(startResult);
  else if ({json.dumps(start_outcome)} === '404') rejectStart(Object.assign(new Error('A session is gone'), {{status: 404}}));
  else rejectStart(Object.assign(new Error('A start failed'), {{status: 500}}));
  await sendPromise;
  await new Promise(resolve => setImmediate(() => setImmediate(resolve)));
  if (paneState === 'loading') {{
    assert.strictEqual(bSnapshot(), visibleB);
    switched = false;
    releaseBMetadata(serverSessions.B);
    await bLoadPromise;
    switched = true;
    visibleB = bSnapshot();
    assert.strictEqual(JSON.parse(visibleB).composerStatus, '');
    assert.strictEqual(JSON.parse(visibleB).uploadWidth, '0%');
    assert.strictEqual(JSON.parse(visibleB).uploadOwner, null);
  }}
  if (paneState !== 'visible') {{
    assert.strictEqual(bSnapshot(), visibleB);
    assert.deepStrictEqual(calls.visibleAfterSwitch, []);
    assert.strictEqual(calls.renderSessionList, 0);
    assert.strictEqual(calls.reconcile, 0);
    assert.strictEqual(calls.reload, 0);
  }}
  assert.deepStrictEqual(calls.attach.slice(attachBefore).filter(call => call[0] === 'A'), []);
  assert.deepStrictEqual(calls.marked, []);
  assert.deepStrictEqual(calls.modelControls, []);
  assert.deepStrictEqual(calls.persistedModels, []);

  if ({json.dumps(start_outcome)} === 'error') {{
    const recovery = calls.drafts.filter(item => item.sid === 'A').at(-1);
    assert.strictEqual(recovery.text, 'A prompt');
    assert.deepStrictEqual(recovery.files, ['a.txt']);
    assert.strictEqual(JSON.parse(bSnapshot()).composer, '');
  }} else if ({json.dumps(start_outcome)} === '404') {{
    assert.strictEqual(INFLIGHT.A, undefined);
    assert.strictEqual(calls.cleared.filter(item => item === 'optimistic:A').length,
      paneState === 'visible' ? 1 : 0);
    assert.strictEqual(calls.renderSessionList, paneState === 'visible' ? 1 : 0);
    if (paneState === 'visible') {{
      assert.strictEqual(S.session, null);
      assert.deepStrictEqual(S.messages, []);
      assert.deepStrictEqual(calls.optimisticOwners, [null]);
      assert.deepStrictEqual(calls.renderOwners, [null]);
      assert.strictEqual(localStorage.getItem('hermes-webui-session'), null);
      assert.strictEqual(historyCalls.length, 1);
      assert.strictEqual(historyCalls[0][2], '/');
    }}
  }} else {{
  const a = INFLIGHT.A;
  assert(a);
  assert.strictEqual(a.streamId, 'A-stream');
  assert.strictEqual(a.activeTurnToken, 'A-token');
  assert.strictEqual(a.reattach, true);
  const aUser = a.messages.find(row => row && row.role === 'user' && row.content === 'A prompt');
  assert(aUser);
  assert.deepStrictEqual(aUser.attachments, ['a.txt']);
  assert.strictEqual(aUser._active_turn_token, 'A-token');
  const persisted = calls.saved.filter(item => item[0] === 'A').at(-1)[1];
  assert.strictEqual(persisted.streamId, 'A-stream');
  assert.strictEqual(persisted.activeTurnToken, 'A-token');
  assert.strictEqual(persisted.reattach, true);
  assert.deepStrictEqual(persisted.messages.find(row => row.content === 'A prompt').attachments, ['a.txt']);
  switched = false;
  await loadSession('A');
  switched = true;
  const aAttach = calls.attach.filter(call => call[0] === 'A');
  assert.strictEqual(aAttach.length, 1);
  assert.strictEqual(aAttach[0][1], 'A-stream');
  assert.strictEqual(aAttach[0][3].reconnecting, true);
  assert.strictEqual(S.session.session_id, 'A');
  assert.strictEqual(S.activeStreamId, 'A-stream');
  const loadedUser = S.messages.find(row => row && row.role === 'user' && row.content === 'A prompt');
  assert(loadedUser);
  assert.deepStrictEqual(loadedUser.attachments, ['a.txt']);
  assert.strictEqual(loadedUser._active_turn_token, 'A-token');
  }}
}})().catch(error => {{
  console.error(error.stack || error);
  process.exitCode = 1;
}});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("pane_state", ("pending-load", "assigned", "same"))
@pytest.mark.parametrize("upload_outcome", ("file", "archive", "error"))
def test_production_upload_side_effects_follow_canonical_owner(pane_state, upload_outcome):
    """The real upload helper must not repaint a pane that replaced its owner."""
    script = f"""
const assert = require('assert');
const paneState = {json.dumps(pane_state)};
const outcome = {json.dumps(upload_outcome)};
let releaseFetch;
let fetchStarted;
const fetchStartedPromise = new Promise(resolve => fetchStarted = resolve);
const fetchResponse = new Promise(resolve => releaseFetch = resolve);
const activeClasses = new Set(['active']);
const elements = {{
  uploadBar: {{style: {{width: '77%'}}}},
  uploadBarWrap: {{
    dataset: {{uploadSessionId: paneState === 'same' ? 'A' : 'B'}},
    classList: {{
      add(name) {{ activeClasses.add(name); }},
      remove(name) {{ activeClasses.delete(name); }},
    }},
  }},
}};
function $(id) {{ return elements[id] || null; }}
const document = {{baseURI: 'https://hermes.test/'}};
const location = {{href: document.baseURI}};
class FormData {{ constructor() {{ this.values = []; }} append(...value) {{ this.values.push(value); }} }}
const A = {{session_id: 'A'}};
const B = {{session_id: 'B'}};
const S = {{
  session: paneState === 'assigned' ? B : A,
  pendingFiles: [{{name: paneState === 'same' ? 'a-existing.txt' : 'b-existing.txt'}}],
  currentDir: paneState === 'same' ? '/a' : '/b',
  workspace: paneState === 'same' ? '/a' : '/b',
}};
let _loadingSessionId = paneState === 'pending-load' ? 'B' : null;
const _uploadPendingFilesProgressBySession = new Map();
const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
const _ARCHIVE_EXTS = /\\.(zip|tar|gz|tgz|bz2|xz|7z|rar)$/i;
let status = paneState === 'same' ? 'A status' : 'B status';
let toasts = [paneState === 'same' ? 'A toast' : 'B toast'];
let trayRenders = 7;
let loadCalls = [paneState === 'same' ? '/a' : '/b'];
let statusCalls = [];
let toastCalls = [];
function _isSessionCurrentPane(sid) {{{_CURRENT_PANE_BODY}}}
function _uploadPendingFilesCurrentSession(sessionId) {{{_UPLOAD_CURRENT_BODY}}}
function _uploadPendingFilesHideProgressBar() {{{_UPLOAD_PROGRESS_HIDE_BODY}}}
function _uploadPendingFilesShowProgressBar(owner, percent) {{{_UPLOAD_PROGRESS_SHOW_BODY}}}
function _uploadPendingFilesUpdateProgress(sessionId, percent) {{{_UPLOAD_UPDATE_BODY}}}
function renderTray() {{ trayRenders++; }}
function loadDir(path) {{ loadCalls.push(path); }}
function setStatus(value) {{ status = String(value); statusCalls.push(status); }}
function showToast(value) {{ toasts.push(String(value)); toastCalls.push(String(value)); }}
function _redirectIfUnauth() {{ return false; }}
function _uploadTooLargeMessage() {{ return 'too large'; }}
function t(key, ...args) {{ return key + (args.length ? ':' + args.join(',') : ''); }}
function fetch(url, options) {{
  fetchStarted({{url, sessionId: options.body.values[0][1]}});
  return fetchResponse;
}}
async function uploadPendingFiles(options = {{}}) {{{_UPLOAD_BODY}}}
function snapshot() {{
  return JSON.stringify({{
    session: S.session.session_id,
    loading: _loadingSessionId,
    pending: S.pendingFiles.map(file => file.name),
    currentDir: S.currentDir,
    workspace: S.workspace,
    status,
    toasts,
    trayRenders,
    loadCalls,
    statusCalls,
    toastCalls,
    width: elements.uploadBar.style.width,
    uploadOwner: elements.uploadBarWrap.dataset.uploadSessionId || null,
    active: activeClasses.has('active'),
  }});
}}
const fileName = outcome === 'archive' ? 'bundle.zip' : 'note.txt';
const file = {{name: fileName, size: 12}};
const response = outcome === 'error'
  ? {{ok: false, text: async () => 'server rejected upload'}}
  : outcome === 'archive'
    ? {{ok: true, json: async () => ({{dest: '/uploaded/bundle', extracted: 2}})}}
    : {{ok: true, json: async () => ({{filename: 'note.txt', path: '/uploaded/note.txt', mime: 'text/plain', size: 12, is_image: false}})}};
(async () => {{
  const uploadPromise = uploadPendingFiles({{sessionId: 'A', files: [file]}});
  await Promise.race([
    fetchStartedPromise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('upload did not reach fetch')), 1000)),
  ]);
  const before = snapshot();
  _uploadPendingFilesUpdateProgress('A', 42);
  if (paneState !== 'same') assert.strictEqual(snapshot(), before);
  else {{
    assert.strictEqual(elements.uploadBar.style.width, '42%');
    assert.strictEqual(elements.uploadBarWrap.dataset.uploadSessionId, 'A');
  }}
  releaseFetch(response);
  let result = null;
  let error = null;
  try {{ result = await uploadPromise; }} catch (exc) {{ error = String(exc && exc.message || exc); }}

  if (paneState !== 'same') {{
    assert.strictEqual(snapshot(), before);
    assert.strictEqual(statusCalls.length, 0);
    assert.strictEqual(toastCalls.length, 0);
    assert.strictEqual(loadCalls.length, 1);
    assert.strictEqual(trayRenders, 7);
    if (outcome === 'error') assert(error && result === null);
    else assert.strictEqual(result.length, 1);
  }} else {{
    if (outcome === 'error') {{
      assert(error && result === null);
    }} else {{
      assert.strictEqual(error, null);
      assert.strictEqual(result.length, 1);
    }}
    assert.deepStrictEqual(S.pendingFiles, []);
    assert.strictEqual(elements.uploadBar.style.width, '0%');
    assert.strictEqual(elements.uploadBarWrap.dataset.uploadSessionId, undefined);
    assert.strictEqual(activeClasses.has('active'), false);
    assert.strictEqual(trayRenders, 8);
    if (outcome === 'archive') {{
      assert.strictEqual(loadCalls.length, 2);
      assert.strictEqual(loadCalls[1], '/a');
      assert.strictEqual(toastCalls.length, 1);
    }} else if (outcome === 'error') {{
      assert.strictEqual(statusCalls.length, 1);
      assert.strictEqual(toastCalls.length, 0);
      assert.strictEqual(loadCalls.length, 1);
    }} else {{
      assert.strictEqual(statusCalls.length, 0);
      assert.strictEqual(toastCalls.length, 0);
      assert.strictEqual(loadCalls.length, 1);
    }}
  }}
}})().catch(error => {{
  console.error(error.stack || error);
  process.exitCode = 1;
}});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize(
    "scenario",
    (
        "assigned-agent",
        "pending-agent",
        "assigned-resolve",
        "pending-resolve",
        "assigned-bundle-resolve",
        "same-moa",
        "same-reload-moa",
        "same-reload-bundle-resolve",
    ),
)
def test_pre_owner_slash_awaits_abort_or_send_from_production_pane(scenario):
    """Agent-bound slash awaits must not transform or send into a new pane."""
    send_body = _function_body(MESSAGES_JS, "send")
    script = r"""
const assert = require('assert');
const scenario = __SCENARIO__;
const paneState = scenario.startsWith('pending-') ? 'pending' : scenario.startsWith('assigned-') ? 'assigned' : 'same';
const command = scenario.includes('bundle') ? 'bundle' : 'moa';
const stage = scenario.includes('agent') ? 'agent' : scenario.includes('bundle') ? 'bundle-resolve' : 'resolve';
const forceReloadSamePane = scenario.startsWith('same-reload-');
let releaseAgent, releaseResolve, releaseBundleResolve, releaseBMetadata, releaseReload;
let agentCalled, resolveCalled, bundleResolveCalled, bMetadataCalled, startCalled;
let reloadCalled;
const agentCalledPromise = new Promise(resolve => agentCalled = resolve);
const resolveCalledPromise = new Promise(resolve => resolveCalled = resolve);
const bundleResolveCalledPromise = new Promise(resolve => bundleResolveCalled = resolve);
const bMetadataCalledPromise = new Promise(resolve => bMetadataCalled = resolve);
const reloadCalledPromise = new Promise(resolve => reloadCalled = resolve);
const agentResponse = new Promise(resolve => releaseAgent = resolve);
const resolveResponse = new Promise(resolve => releaseResolve = resolve);
const bundleResolveResponse = new Promise(resolve => releaseBundleResolve = resolve);
const bMetadataResponse = new Promise(resolve => releaseBMetadata = resolve);
const reloadResponse = new Promise(resolve => releaseReload = resolve);
const calls = {starts: [], drafts: [], status: [], attach: []};
const elements = {
  msg: {value: command === 'bundle' ? '/bundle A prompt' : '/moa A prompt'},
  modelSelect: {value: 'A model'},
  msgInner: {innerHTML: ''},
  emptyState: {style: {display: ''}},
};
function $(id) { return elements[id] || null; }
const document = {querySelector: () => null};
const localStorage = {
  values: Object.create(null),
  getItem(key) { return this.values[key] || null; },
  setItem(key, value) { this.values[key] = String(value); },
  removeItem(key) { delete this.values[key]; },
};
const window = {_defaultMessageMode: 'steer', _defaultModel: 'A default', _activeProvider: 'provider-a'};
const history = {replaceState() {}};
const A = {session_id: 'A', title: 'A', model: 'A model', model_provider: 'provider-a', workspace: '/a', profile: 'profile-a', active_stream_id: null};
const B = {session_id: 'B', title: 'B', model: 'B model', model_provider: 'provider-b', workspace: '/b', profile: 'profile-b', active_stream_id: null};
const serverSessions = {A: {session: A, messages: [{role: 'assistant', content: 'A history'}]}, B: {session: B, messages: [{role: 'assistant', content: 'B history'}]}};
const S = {session: A, messages: serverSessions.A.messages.slice(), pendingFiles: [], busy: false, activeStreamId: null, activeProfile: 'profile-a', toolCalls: [], todos: [], todoStateMeta: {}};
const INFLIGHT = Object.create(null);
let _sendInProgress = false, _sendInProgressSid = null;
let _loadingSessionId = null, _loadSessionGeneration = 0;
let _messageUserUnpinned = false, _scrollPinned = true, _loadingOlder = false;
let _messagesTruncated = false, _oldestIdx = 0, _pendingCarryForwardSnapshot = null;
let _yoloEnabled = false;
let _pendingSelections = [], _forcedSkillDirectivePending = null;
let _pendingMoaConfig = null;
let _approvalSessionId = null, _clarifySessionId = null, _queueDrainSid = null;
const noop = () => {};
[
  'stopApprovalPolling','hideApprovalCard','stopSessionStream','_updateYoloPill',
  'stopClarifyPolling','hideClarifyCard','clearCompressionUi','_clearQueueCardDisplay',
  'stopApprovalPollingForSession','stopClarifyPollingForSession',
  'snapshotLiveTurnHtmlForSession','_captureSameSessionForceReloadHint',
  '_clearSameSessionForceReloadHint','closeOtherLiveStreams','clearLiveToolCards',
  'updateSendBtn','renderMessages','renderSessionListFromCache','startApprovalPolling',
  'startClarifyPolling','_fetchYoloState','applySessionTitleUpdate','markInflight',
  'syncTopbar','startSessionStream','_deferWorkspaceRefreshForSession',
  '_setActiveSessionUrl','updateQueueBadge','resumeManualCompressionForSession',
  '_clearDeferredActiveSessionExternalRefresh','_rearmActiveSessionStream',
  '_clearEmptyComposerModelOverride','_applyPendingSessionModelForSession',
  '_acknowledgeSessionVisit','_resolveSessionModelForDisplaySoon',
  '_uploadPendingFilesSyncProgressForSession',
  'clearInflightState','_clearQueueCardDisplay','_clearStuckSessionOnBoot',
].forEach(name => globalThis[name] = noop);
function setComposerStatus(value) { calls.status.push(String(value || '')); }
function setStatus() {}
function setBusy(value) { S.busy = !!value; }
function autoResize() {}
function renderTray() {}
function ensureLiveWorklogShell() {}
function appendThinking() {}
function upsertActiveSessionForLocalTurn() {}
function showLiveRunStatus() {}
function attachLiveStream(sid, streamId, uploaded, options) { calls.attach.push([sid, streamId, options || null]); }
function saveInflightState() {}
function clearOptimisticSessionStreaming() {}
function hideLiveRunStatus() {}
function removeThinking() {}
function startApprovalPollingForSession() {}
function startClarifyPollingForSession() {}
function showToast() {}
function queueSessionMessage() {}
function _clearStaleBusyStateBeforeSend() {}
function isCompressionUiRunning() { return false; }
function shouldInterceptCompressionRecoveryContinuation() { return false; }
function _composerTextWithPendingSelections() { return elements.msg.value; }
function _flushSelectionBlocksToComposer() {}
function _chatPayloadModelState() { return {model: S.session.model, model_provider: S.session.model_provider}; }
function _readPendingSessionModel() { return null; }
function _opaqueActiveTurnToken(value) { return typeof value === 'string' && value.trim() ? value : null; }
function _clearComposerDraft() { return Promise.resolve(); }
function _saveComposerDraftNow(sid, text, files) { calls.drafts.push({sid, text, files: files.map(file => file.name)}); return Promise.resolve(); }
function _restoreComposerDraftAfterFailedSend() {}
function uploadPendingFiles() { return Promise.resolve([]); }
function _isSessionCurrentPane(sid) { __CURRENT_PANE_BODY__ }
function parseCommand(text) { return {name: text.split(/\s+/, 1)[0].slice(1)}; }
const COMMANDS = [];
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set();
function getAgentCommandMetadata(name) {
  if (command !== 'moa') return Promise.resolve(null);
  if (stage !== 'agent') return Promise.resolve({name: 'moa'});
  agentCalled();
  return agentResponse;
}
function getBundleCommandMetadata() {
  if (command !== 'bundle') return null;
  return Promise.resolve({name: 'bundle'});
}
function resolveBundleCommand() {
  bundleResolveCalled();
  return bundleResolveResponse;
}
function _runOptionalPreStartUiStep(label, fn) { return fn(); }
function _runOptionalPostStartUiStep(label, fn) { return fn(); }
function _dismissHandoffHint() {}
function _clearPendingSessionModel() {}
function api(path, options) {
  if (path.startsWith('/api/session?')) {
    const sid = new URLSearchParams(path.split('?')[1]).get('session_id');
    if (sid === 'A' && forceReloadSamePane) {
      reloadCalled();
      return reloadResponse;
    }
    if (sid === 'B') {
      bMetadataCalled();
      return paneState === 'pending' ? bMetadataResponse : Promise.resolve(serverSessions.B);
    }
    return Promise.resolve(serverSessions[sid]);
  }
  if (path === '/api/commands/moa/resolve') {
    resolveCalled();
    return resolveResponse;
  }
  if (path === '/api/chat/start') {
    calls.starts.push(JSON.parse(options.body));
    startCalled();
    return Promise.resolve({stream_id: 'A-stream', active_turn_token: 'A-token', pending_started_at: 30});
  }
  throw new Error('unexpected API ' + path);
}
async function _ensureMessagesLoaded(sid) { S.messages = serverSessions[sid].messages.slice(); }
async function loadSession(sid, opts = {}) {
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid === sid;
  __LOAD_SWITCH_BODY__
  const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
  if (!data || !_isCurrentLoad()) return;
  __LOAD_METADATA_BODY__
  await _ensureMessagesLoaded(sid, {loadGeneration: _loadGeneration});
  if (!_isCurrentLoad()) return;
  if (activeStreamId) {
    S.busy = true;
  } else {
    __LOAD_IDLE_BODY__
  }
  if (_isCurrentLoad()) _loadingSessionId = null;
}
const startResult = {stream_id: 'A-stream', active_turn_token: 'A-token', pending_started_at: 30};
async function send() { __SEND_BODY__ }
function bSnapshot() {
  return JSON.stringify({session: S.session && S.session.session_id, messages: S.messages, busy: S.busy,
    activeStreamId: S.activeStreamId, activeProfile: S.activeProfile, status: calls.status.at(-1) || '',
    model: elements.modelSelect.value, composer: elements.msg.value});
}
(async () => {
  const sendPromise = send();
  let bLoadPromise = null;
  let sameReloadPromise = null;
  let sameReloadBefore = null;
  const switchToB = async () => {
    bLoadPromise = loadSession('B');
    if (paneState === 'pending') await bMetadataCalledPromise;
    else await bLoadPromise;
  };
  const switchToSameReload = async () => {
    sameReloadPromise = loadSession('A', {force: true});
    await reloadCalledPromise;
    sameReloadBefore = bSnapshot();
  };
  if (stage === 'agent') {
    await agentCalledPromise;
    if (paneState !== 'same') await switchToB();
    releaseAgent({name: 'moa'});
    releaseResolve({preset: 'moa-default'});
  } else if (stage === 'resolve') {
    await resolveCalledPromise;
    if (forceReloadSamePane) await switchToSameReload();
    else if (paneState !== 'same') await switchToB();
    releaseResolve({preset: 'moa-default'});
  } else {
    await bundleResolveCalledPromise;
    if (forceReloadSamePane) await switchToSameReload();
    else await switchToB();
    releaseBundleResolve({message: 'A bundle invocation'});
  }
  await sendPromise;
  assert.strictEqual(calls.starts.length, forceReloadSamePane ? 0 : paneState === 'same' ? 1 : 0);
  if (forceReloadSamePane) {
    assert.strictEqual(bSnapshot(), sameReloadBefore);
    releaseReload(serverSessions.A);
    await sameReloadPromise;
    return;
  }
  if (paneState !== 'same') {
    assert.deepStrictEqual(calls.drafts.at(-1), {sid: 'A', text: command === 'bundle' ? '/bundle A prompt' : '/moa A prompt', files: []});
    if (paneState === 'pending') {
      assert.strictEqual(calls.status.at(-1), '');
      releaseBMetadata(serverSessions.B);
      await bLoadPromise;
    }
    const b = bSnapshot();
    assert.strictEqual(JSON.parse(b).session, 'B');
    assert.strictEqual(JSON.parse(b).status, '');
    assert.strictEqual(b, bSnapshot());
  } else {
    const request = calls.starts[0];
    assert.strictEqual(request.session_id, 'A');
    assert.strictEqual(request.model, 'A model');
    assert.strictEqual(request.model_provider, 'provider-a');
    assert.strictEqual(request.workspace, '/a');
    assert.strictEqual(request.profile, 'profile-a');
    assert.strictEqual(request.moa_config, true);
    assert.strictEqual(request.message, 'A prompt');
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""
    script = (
        script.replace("__SCENARIO__", json.dumps(scenario))
        .replace("__CURRENT_PANE_BODY__", _CURRENT_PANE_BODY)
        .replace("__LOAD_SWITCH_BODY__", _LOAD_SESSION_SWITCH_BODY)
        .replace("__LOAD_METADATA_BODY__", _LOAD_SESSION_METADATA_BODY)
        .replace("__LOAD_IDLE_BODY__", _LOAD_SESSION_IDLE_BODY)
        .replace("__SEND_BODY__", send_body)
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("pane_state", ("assigned", "pending", "same"))
def test_new_session_response_respects_production_pane_owner(pane_state):
    """A stale New Chat response must not replace an installed/loading pane."""
    script = r"""
const assert = require('assert');
const paneState = __PANE_STATE__;
let releaseNew, releaseBMetadata, newCalled, bMetadataCalled;
let startCalled = () => {};
const newCalledPromise = new Promise(resolve => newCalled = resolve);
const bMetadataCalledPromise = new Promise(resolve => bMetadataCalled = resolve);
const newResponse = new Promise(resolve => releaseNew = resolve);
const bMetadataResponse = new Promise(resolve => releaseBMetadata = resolve);
const calls = {starts: [], urls: [], renders: 0, applied: 0, pending: [], newResult: undefined};
const elements = {
  msg: {value: '/moa A prompt'},
  modelSelect: {value: 'A model', dataset: {provider: 'provider-a'}, appendChild() {}},
  msgInner: {innerHTML: ''},
  emptyState: {style: {display: ''}},
  composerStatus: {textContent: ''},
  a11yAnnouncer: {textContent: ''},
  composerWorkspaceContext: {textContent: ''},
};
function $(id) { return elements[id] || null; }
const document = {
  querySelector: () => null,
  visibilityState: 'visible',
  hasFocus: () => true,
  createElement: () => ({dataset: {}, appendChild() {}}),
};
const localStorage = {
  values: {"hermes-webui-session": 'old-session'},
  getItem(key) { return this.values[key] || null; },
  setItem(key, value) { this.values[key] = String(value); },
  removeItem(key) { delete this.values[key]; },
};
const window = {
  _defaultModel: 'A model', _activeProvider: 'provider-a', _defaultMessageMode: 'steer',
  _clearPendingSelections() {},
};
const history = {replaceState() {}};
const A = {
  session_id: 'A-new', title: 'A new chat', model: 'A model', model_provider: 'provider-a',
  workspace: '/new', profile: 'profile-a', messages: [], last_usage: {},
};
const B = {
  session_id: 'B', title: 'B', model: 'B model', model_provider: 'provider-b',
  workspace: '/b', profile: 'profile-b', active_stream_id: null, messages: [],
};
const serverSessions = {
  B: {session: B, messages: [{role: 'assistant', content: 'B history'}]},
};
const S = {
  session: null, messages: [], pendingFiles: [], toolCalls: [], todos: [], todoStateMeta: {},
  busy: false, activeStreamId: null, activeProfile: 'profile-a', lastUsage: {},
  _profileDefaultWorkspace: '/empty', _profileSwitchWorkspace: null,
  _pendingSessionToolsets: null,
};
const INFLIGHT = Object.create(null);
let _loadingSessionId = null, _loadSessionGeneration = 0;
let _newSessionInFlight = null;
let _sessionSourceFilter = 'webui';
let _activeProject = null;
const NO_PROJECT_FILTER = '__none__';
let _messagesTruncated = false, _oldestIdx = 0, _pendingCarryForwardSnapshot = null;
let _messageUserUnpinned = false, _scrollPinned = true, _loadingOlder = false;
let _yoloEnabled = false;
let _sendInProgress = false, _sendInProgressSid = null;
let _pendingSelections = [], _forcedSkillDirectivePending = null;
let _pendingMoaConfig = null, _approvalSessionId = null, _clarifySessionId = null, _queueDrainSid = null;
const _sessionStreamingById = new Map();
function _isSessionCurrentPane(sid) {
  if (!sid || !S.session || S.session.session_id !== sid) return false;
  return !(_loadingSessionId && _loadingSessionId !== sid);
}
function _setNewSessionPending(value) { calls.pending.push(!!value); }
function _newSessionPendingText() { return 'Creating new conversation…'; }
function updateQueueBadge() {}
function clearLiveToolCards() {}
function _readEmptyComposerModelOverride() { return null; }
function _clearEmptyComposerModelOverride() { calls.applied++; }
function _modelStateForSelect(select) { return {model: select.value, model_provider: select.dataset.provider}; }
function _readPersistedModelState() { return null; }
function _hydrateTodosFromSession() {}
function _rememberNewChatDraftSession() {}
function _setActiveSessionUrl(sid) { calls.urls.push('/session/' + sid); }
function startSessionStream() {}
function _setSessionViewedCount() {}
function _applyModelToDropdown() { return true; }
function syncModelChip() {}
function _setLiveAssistantTps() {}
function _syncCtxIndicator() {}
function setStatus() {}
function setComposerStatus(value) { elements.composerStatus.textContent = String(value || ''); }
function updateSendBtn() {}
function syncTopbar() {}
function renderMessages() { calls.renders++; }
function _announceNewSessionWorkspace() {}
function _deferWorkspaceRefreshForSession() {}
function loadDir() { return Promise.resolve(); }
function refreshSessionList() { return Promise.resolve(); }
function _clearDeferredActiveSessionExternalRefresh() {}
function _resetScrollDirectionTracker() {}
function _applyPendingSessionModelForSession() { return false; }
function _clearSameSessionForceReloadHint() {}
function _captureSameSessionForceReloadHint() {}
function _resolveSessionModelForDisplaySoon(sid) {
  const session = serverSessions[sid].session;
  S.activeProfile = session.profile;
  elements.modelSelect.value = session.model;
  window._defaultModel = session.model;
  window._activeProvider = session.model_provider;
}
function _acknowledgeSessionVisit() {}
function populateModelDropdown() { return Promise.resolve(); }
function _deferSessionSideEffect(sid, fn) { return Promise.resolve().then(fn); }
function stopApprovalPolling() {}
function startApprovalPolling() {}
function hideApprovalCard() {}
function stopSessionStream() {}
function _updateYoloPill() {}
function stopClarifyPolling() {}
function startClarifyPolling() {}
function hideClarifyCard() {}
function clearCompressionUi() {}
function _clearQueueCardDisplay() {}
function closeOtherLiveStreams() {}
function snapshotLiveTurnHtmlForSession() {}
function resumeManualCompressionForSession() {}
function _uploadPendingFilesSyncProgressForSession() {}
function clearInflightState() {}
function _rearmActiveSessionStream() {}
function _clearStuckSessionOnBoot() {}
function _isServerIdleSessionRow(row) { return !!row; }
function loadInflightState() { return null; }
async function _ensureMessagesLoaded(sid) { S.messages = serverSessions[sid].messages.map(row => ({...row})); }
function _appRootPath() { return '/'; }
function setBusy(value) { S.busy = !!value; }
function autoResize() {}
function renderTray() {}
function ensureLiveWorklogShell() {}
function appendThinking() {}
function upsertActiveSessionForLocalTurn() {}
function showLiveRunStatus() {}
function attachLiveStream() {}
function markInflight() {}
function saveInflightState() {}
function clearOptimisticSessionStreaming() {}
function hideLiveRunStatus() {}
function removeThinking() {}
function startApprovalPollingForSession() {}
function startClarifyPollingForSession() {}
function startApprovalPolling() {}
function startClarifyPolling() {}
function _fetchYoloState() {}
function applySessionTitleUpdate() {}
function renderSessionListFromCache() {}
function showToast() {}
function queueSessionMessage() {}
function _clearStaleBusyStateBeforeSend() {}
function isCompressionUiRunning() { return false; }
function shouldInterceptCompressionRecoveryContinuation() { return false; }
function _composerTextWithPendingSelections() { return elements.msg.value; }
function _flushSelectionBlocksToComposer() {}
function _chatPayloadModelState() { return {model: S.session.model, model_provider: S.session.model_provider}; }
function _readPendingSessionModel() { return null; }
function _opaqueActiveTurnToken(value) { return typeof value === 'string' && value.trim() ? value : null; }
function _clearComposerDraft() { return Promise.resolve(); }
function _restoreComposerDraftAfterFailedSend() {}
function uploadPendingFiles() { return Promise.resolve([]); }
function parseCommand(text) { return {name: text.split(/\s+/, 1)[0].slice(1)}; }
const COMMANDS = [];
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set();
function getAgentCommandMetadata() { return Promise.resolve({name: 'moa'}); }
function getBundleCommandMetadata() { return null; }
function _runOptionalPreStartUiStep(label, fn) { return fn(); }
function _runOptionalPostStartUiStep(label, fn) { return fn(); }
function _dismissHandoffHint() {}
function _clearPendingSessionModel() {}
function hideCmdDropdown() {}
function _isCompressionUiRunning() { return false; }
function api(path, options) {
  if (path === '/api/session/new') {
    newCalled();
    return newResponse;
  }
  if (path.startsWith('/api/session?')) {
    bMetadataCalled();
    return bMetadataResponse;
  }
  if (path === '/api/commands/moa/resolve') return Promise.resolve({preset: 'moa-default'});
  if (path === '/api/chat/start') {
    calls.starts.push(JSON.parse(options.body));
    startCalled();
    return Promise.resolve({stream_id: 'A-stream', active_turn_token: 'A-token', pending_started_at: 3});
  }
  throw new Error('unexpected API ' + path);
}
async function _newSessionProduction(flash, options = {}) { __NEW_SESSION_BODY__ }
async function newSession(flash, options = {}) {
  const result = await _newSessionProduction(flash, options);
  calls.newResult = result;
  return result;
}
async function loadSession(sid, opts = {}) {
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid === sid;
  __LOAD_SWITCH_BODY__
  const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
  if (!data || !_isCurrentLoad()) return;
  __LOAD_METADATA_BODY__
  await _ensureMessagesLoaded(sid, {loadGeneration: _loadGeneration});
  if (!_isCurrentLoad()) return;
  if (activeStreamId) {
    S.busy = true;
    S.activeStreamId = activeStreamId;
  } else {
    __LOAD_IDLE_BODY__
  }
  if (_isCurrentLoad()) _loadingSessionId = null;
}
async function send() { __SEND_BODY__ }
function renderSessionList() { calls.renders++; return Promise.resolve(); }
function snapshot() {
  return JSON.stringify({
    session: S.session, messages: S.messages, pendingFiles: S.pendingFiles,
    busy: S.busy, activeStreamId: S.activeStreamId, activeProfile: S.activeProfile,
    model: elements.modelSelect.value, modelDefault: window._defaultModel,
    activeProvider: window._activeProvider, composerStatus: elements.composerStatus.textContent,
    msgInner: elements.msgInner.innerHTML, url: calls.urls.at(-1) || '/',
    savedSession: localStorage.getItem('hermes-webui-session'),
  });
}
(async () => {
  if (paneState === 'same') {
    const sendPromise = send();
    await newCalledPromise;
    releaseNew({session: A});
    await sendPromise;
    assert.strictEqual(calls.starts.length, 1);
    assert.strictEqual(calls.starts[0].session_id, 'A-new');
    assert.strictEqual(calls.starts[0].model, 'A model');
    assert.strictEqual(calls.starts[0].model_provider, 'provider-a');
    assert.strictEqual(calls.starts[0].workspace, '/new');
    assert.strictEqual(calls.starts[0].profile, 'profile-a');
    assert.strictEqual(calls.starts[0].moa_config, true);
    assert.strictEqual(S.session.session_id, 'A-new');
    return;
  }
  const sendPromise = send();
  await newCalledPromise;
  const bLoadPromise = loadSession('B');
  await bMetadataCalledPromise;
  if (paneState === 'assigned') {
    releaseBMetadata(serverSessions.B);
    await bLoadPromise;
  }
  const before = snapshot();
  const rendersBefore = calls.renders;
  releaseNew({session: A});
  await sendPromise;
  assert.strictEqual(calls.starts.length, 0);
  assert.strictEqual(snapshot(), before);
  assert.strictEqual(calls.newResult, null);
  assert.strictEqual(calls.renders, rendersBefore);
  if (paneState === 'pending') {
    releaseBMetadata(serverSessions.B);
    await bLoadPromise;
    const b = JSON.parse(snapshot());
    assert.strictEqual(b.session.session_id, 'B');
    assert.strictEqual(b.url, '/session/B');
    assert.strictEqual(b.savedSession, 'B');
    assert.strictEqual(b.model, 'B model');
    assert.strictEqual(b.activeProvider, 'provider-b');
    assert.strictEqual(b.session.workspace, '/b');
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""
    script = (
        script.replace("__PANE_STATE__", json.dumps(pane_state))
        .replace("__NEW_SESSION_BODY__", _NEW_SESSION_BODY)
        .replace("__LOAD_SWITCH_BODY__", _LOAD_SESSION_SWITCH_BODY)
        .replace("__LOAD_METADATA_BODY__", _LOAD_SESSION_METADATA_BODY)
        .replace("__LOAD_IDLE_BODY__", _LOAD_SESSION_IDLE_BODY)
        .replace("__SEND_BODY__", _function_body(MESSAGES_JS, "send"))
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("order", ("reload-new", "new-reload", "ordinary"))
def test_new_session_and_force_reload_use_production_generation_order(order):
    """New Chat and a same-pane force reload must resolve by latest intent."""
    script = r"""
const assert = require('assert');
const order = __ORDER__;
let releaseReload, releaseNew, reloadCalled, newCalled;
const reloadCalledPromise = new Promise(resolve => reloadCalled = resolve);
const newCalledPromise = new Promise(resolve => newCalled = resolve);
const reloadResponse = new Promise(resolve => releaseReload = resolve);
const newResponse = new Promise(resolve => releaseNew = resolve);
const calls = {urls: ['/session/A'], renders: 0};
const elements = {
  msg: {value: 'A draft'},
  modelSelect: {value: 'A model', dataset: {provider: 'provider-a'}, appendChild() {}},
  msgInner: {innerHTML: '<div>A</div>'},
  emptyState: {style: {display: 'none'}},
  composerStatus: {textContent: ''},
};
function $(id) { return elements[id] || null; }
const document = {createElement: () => ({dataset: {}, appendChild() {}})};
const localStorage = {
  values: {'hermes-webui-session': 'A'},
  getItem(key) { return this.values[key] || null; },
  setItem(key, value) { this.values[key] = String(value); },
  removeItem(key) { delete this.values[key]; },
};
const window = {
  _defaultModel: 'A model', _activeProvider: 'provider-a',
  _clearPendingSelections() {},
};
const history = {replaceState() {}};
const A = {
  session_id: 'A', title: 'A', model: 'A model', model_provider: 'provider-a',
  workspace: '/a', profile: 'profile-a', active_stream_id: null,
};
const reloadedA = {
  session_id: 'A', title: 'A refreshed', model: 'A reload model',
  model_provider: 'provider-a-reload', workspace: '/a-reloaded', profile: 'profile-a-reload',
  active_stream_id: null,
};
const created = {
  session_id: 'C', title: 'Created', model: 'C model', model_provider: 'provider-c',
  workspace: '/c', profile: 'profile-c', messages: [], last_usage: {},
};
const serverSessions = {
  A: {session: reloadedA, messages: [{role: 'user', content: 'reloaded A'}]},
};
const S = {
  session: A, messages: [{role: 'user', content: 'original A'}], pendingFiles: [],
  busy: false, activeStreamId: null, activeProfile: 'profile-a', lastUsage: {},
  toolCalls: [], todos: [], todoStateMeta: {}, _profileDefaultWorkspace: null,
  _profileSwitchWorkspace: null, _pendingSessionToolsets: null,
};
const INFLIGHT = Object.create(null);
let _loadingSessionId = null, _loadSessionGeneration = 0;
let _newSessionInFlight = null;
let _sessionSourceFilter = 'webui';
let _activeProject = null;
const NO_PROJECT_FILTER = '__none__';
let _messagesTruncated = false, _oldestIdx = 0, _pendingCarryForwardSnapshot = null;
let _messageUserUnpinned = false, _scrollPinned = true, _loadingOlder = false;
function _isSessionCurrentPane(sid) {
  if (!sid || !S.session || S.session.session_id !== sid) return false;
  return !(_loadingSessionId && _loadingSessionId !== sid);
}
function _setNewSessionPending() {}
function _newSessionPendingText() { return 'Creating new conversation…'; }
function _readEmptyComposerModelOverride() { return null; }
function _clearEmptyComposerModelOverride() {}
function _modelStateForSelect(select) { return {model: select.value, model_provider: select.dataset.provider}; }
function _readPersistedModelState() { return null; }
function _hydrateTodosFromSession() {}
function _rememberNewChatDraftSession() {}
function _setActiveSessionUrl(sid) { calls.urls.push('/session/' + sid); }
function startSessionStream() {}
function _setSessionViewedCount() {}
function _applyModelToDropdown(model, select) { if (select) select.value = model; return true; }
function syncModelChip() {}
function _setLiveAssistantTps() {}
function _syncCtxIndicator() {}
function setStatus() {}
function setComposerStatus(value) { elements.composerStatus.textContent = String(value || ''); }
function updateSendBtn() {}
function syncTopbar() {}
function renderMessages() { calls.renders++; }
function _announceNewSessionWorkspace() {}
function _deferWorkspaceRefreshForSession() {}
function loadDir() { return Promise.resolve(); }
function refreshSessionList() { return Promise.resolve(); }
function _deferSessionSideEffect(sid, fn) { return Promise.resolve().then(fn); }
function populateModelDropdown() { return Promise.resolve(); }
function _resolveSessionModelForDisplaySoon() {
  const session = serverSessions.A.session;
  elements.modelSelect.value = session.model;
  S.activeProfile = session.profile;
  window._defaultModel = session.model;
  window._activeProvider = session.model_provider;
}
function _acknowledgeSessionVisit() {}
function _applyPendingSessionModelForSession() {}
function _clearDeferredActiveSessionExternalRefresh() {}
function _resetScrollDirectionTracker() {}
function _uploadPendingFilesSyncProgressForSession() {}
function clearInflightState() {}
function _rearmActiveSessionStream() {}
function clearCompressionUi() {}
function _clearQueueCardDisplay() {}
function stopApprovalPolling() {}
function startApprovalPolling() {}
function hideApprovalCard() {}
function stopSessionStream() {}
function _updateYoloPill() {}
function stopClarifyPolling() {}
function startClarifyPolling() {}
function hideClarifyCard() {}
function closeOtherLiveStreams() {}
function snapshotLiveTurnHtmlForSession() {}
function _captureSameSessionForceReloadHint() {}
function _clearSameSessionForceReloadHint() {}
function _clearStuckSessionOnBoot() {}
function _opaqueActiveTurnToken(value) { return typeof value === 'string' ? value : null; }
function _isServerIdleSessionRow() { return true; }
function loadInflightState() { return null; }
function _mergePendingSessionMessage() {}
function _mergeInflightTailMessages() {}
function _prepareRunningLiveTail() {}
function _dropCurrentTurnAssistantMessages() {}
function _currentTurnAssistantText() { return ''; }
function _compactTranscriptText() { return ''; }
async function _ensureMessagesLoaded(sid) {
  S.messages = serverSessions[sid].messages.map(row => ({...row}));
}
function _isSessionActivelyViewed() { return true; }
function _sessionVisitHasUnreadState() { return false; }
async function _saveComposerDraftNow() { return undefined; }
function clearLiveToolCards() {}
function updateQueueBadge() {}
function _appRootPath() { return '/'; }
function api(path) {
  if (path === '/api/session/new') {
    newCalled();
    return newResponse;
  }
  if (path.startsWith('/api/session?')) {
    reloadCalled();
    return reloadResponse;
  }
  throw new Error('unexpected API ' + path);
}
async function loadSession(sid, opts = {}) {
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid === sid;
  __LOAD_SWITCH_BODY__
  const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
  if (!data || !_isCurrentLoad()) return;
  __LOAD_METADATA_BODY__
  await _ensureMessagesLoaded(sid, {loadGeneration: _loadGeneration});
  if (!_isCurrentLoad()) return;
  if (activeStreamId) {
    S.busy = true;
    S.activeStreamId = activeStreamId;
  } else {
    __LOAD_IDLE_BODY__
  }
  if (_isCurrentLoad()) _loadingSessionId = null;
}
async function newSession(flash, options = {}) { __NEW_SESSION_BODY__ }
function snapshot() {
  return JSON.stringify({
    session: S.session, messages: S.messages, model: elements.modelSelect.value,
    activeProfile: S.activeProfile, defaultModel: window._defaultModel,
    provider: window._activeProvider, workspace: S.session && S.session.workspace,
    url: calls.urls.at(-1), saved: localStorage.getItem('hermes-webui-session'),
  });
}
(async () => {
  if (order === 'reload-new') {
    const oldLoad = loadSession('A', {force: true});
    await reloadCalledPromise;
    const newChat = newSession();
    await newCalledPromise;
    releaseNew({session: created});
    await newChat;
    assert.strictEqual(S.session.session_id, 'C');
    const expected = snapshot();
    releaseReload(serverSessions.A);
    await oldLoad;
    assert.strictEqual(snapshot(), expected);
    return;
  }
  if (order === 'new-reload') {
    const newChat = newSession();
    await newCalledPromise;
    const oldLoad = loadSession('A', {force: true});
    await reloadCalledPromise;
    releaseReload(serverSessions.A);
    await oldLoad;
    const expected = snapshot();
    releaseNew({session: created});
    assert.ok((await newChat) == null);
    assert.strictEqual(snapshot(), expected);
    return;
  }
  const newChat = newSession();
  await newCalledPromise;
  releaseNew({session: created});
  await newChat;
  assert.strictEqual(S.session.session_id, 'C');
  assert.strictEqual(S.session.workspace, '/c');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""
    script = (
        script.replace("__ORDER__", json.dumps(order))
        .replace("__NEW_SESSION_BODY__", _NEW_SESSION_BODY)
        .replace("__LOAD_SWITCH_BODY__", _LOAD_SESSION_SWITCH_BODY)
        .replace("__LOAD_METADATA_BODY__", _LOAD_SESSION_METADATA_BODY)
        .replace("__LOAD_IDLE_BODY__", _LOAD_SESSION_IDLE_BODY)
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("command", ("goal", "pet", "busy-goal"))
def test_empty_send_commands_abort_after_production_new_session_returns_null(command):
    """An empty-pane command must stop when New Chat loses pane ownership."""
    script = r"""
const assert = require('assert');
const command = __COMMAND__;
const busyPath = command === 'busy-goal';
let releaseNew, releaseBMetadata, newCalled, bMetadataCalled;
const newCalledPromise = new Promise(resolve => newCalled = resolve);
const bMetadataCalledPromise = new Promise(resolve => bMetadataCalled = resolve);
const newResponse = new Promise(resolve => releaseNew = resolve);
const bMetadataResponse = new Promise(resolve => releaseBMetadata = resolve);
const calls = {goal: 0, pet: 0, queue: 0, starts: 0, renders: 0, resizes: 0, trays: 0};
const elements = {
  msg: {value: command === 'pet' ? '/pet hello' : '/goal stop'},
  modelSelect: {value: 'A model', dataset: {provider: 'provider-a'}, appendChild() {}},
  msgInner: {innerHTML: '<div>B history</div>'},
  emptyState: {style: {display: 'none'}},
  composerStatus: {textContent: ''},
  a11yAnnouncer: {textContent: ''},
  composerWorkspaceContext: {textContent: ''},
};
function $(id) { return elements[id] || null; }
const document = {querySelector: () => null, createElement: () => ({dataset: {}, appendChild() {}})};
const localStorage = {
  values: {'hermes-webui-session': 'B'},
  getItem(key) { return this.values[key] || null; },
  setItem(key, value) { this.values[key] = String(value); },
  removeItem(key) { delete this.values[key]; },
};
const window = {
  _defaultMessageMode: 'queue', _defaultModel: 'A model', _activeProvider: 'provider-a',
  _clearPendingSelections() {},
};
const history = {replaceState() {}};
const B = {
  session_id: 'B', title: 'B', model: 'B model', model_provider: 'provider-b',
  workspace: '/b', profile: 'profile-b', active_stream_id: 'B-stream',
};
const created = {
  session_id: 'C', title: 'Created', model: 'C model', model_provider: 'provider-c',
  workspace: '/c', profile: 'profile-c', messages: [], last_usage: {},
};
const serverSessions = {B: {session: B, messages: [{role: 'assistant', content: 'B history'}]}};
const S = {
  session: null, messages: [], pendingFiles: [], busy: busyPath, activeStreamId: null,
  activeProfile: 'profile-a', lastUsage: {}, toolCalls: [], todos: [], todoStateMeta: {},
  _profileDefaultWorkspace: '/empty', _profileSwitchWorkspace: null,
  _pendingSessionToolsets: null,
};
const INFLIGHT = Object.create(null);
let _loadingSessionId = null, _loadSessionGeneration = 0;
let _newSessionInFlight = null;
let _sessionSourceFilter = 'webui';
let _activeProject = null;
const NO_PROJECT_FILTER = '__none__';
let _messagesTruncated = false, _oldestIdx = 0, _pendingCarryForwardSnapshot = null;
let _messageUserUnpinned = false, _scrollPinned = true, _loadingOlder = false;
let _yoloEnabled = false;
let _pendingSelections = [], _forcedSkillDirectivePending = null;
let _pendingMoaConfig = null, _approvalSessionId = null, _clarifySessionId = null, _queueDrainSid = null;
const _sessionStreamingById = new Map();
let _sendInProgress = false, _sendInProgressSid = null;
function _isSessionCurrentPane(sid) {
  if (!sid || !S.session || S.session.session_id !== sid) return false;
  return !(_loadingSessionId && _loadingSessionId !== sid);
}
function _setNewSessionPending(value) {
  if (value) setComposerStatus('Creating new conversation…');
  else if (elements.composerStatus.textContent === 'Creating new conversation…') setComposerStatus('');
}
function _newSessionPendingText() { return 'Creating new conversation…'; }
function updateQueueBadge() {}
function clearLiveToolCards() {}
function _readEmptyComposerModelOverride() { return null; }
function _clearEmptyComposerModelOverride() {}
function _modelStateForSelect(select) { return {model: select.value, model_provider: select.dataset.provider}; }
function _readPersistedModelState() { return null; }
function _hydrateTodosFromSession() {}
function _rememberNewChatDraftSession() {}
function _setActiveSessionUrl() {}
function startSessionStream() {}
function _setSessionViewedCount() {}
function _applyModelToDropdown(model, select) { select.value = model; return true; }
function syncModelChip() {}
function _setLiveAssistantTps() {}
function _syncCtxIndicator() {}
function setStatus() {}
function setComposerStatus(value) { elements.composerStatus.textContent = String(value || ''); }
function updateSendBtn() {}
function syncTopbar() {}
function renderMessages() { calls.renders++; }
function _announceNewSessionWorkspace() {}
function _deferWorkspaceRefreshForSession() {}
function loadDir() { return Promise.resolve(); }
function refreshSessionList() { calls.renders++; return Promise.resolve(); }
function renderSessionList() { calls.renders++; return Promise.resolve(); }
function _deferSessionSideEffect(sid, fn) { return Promise.resolve().then(fn); }
function populateModelDropdown() { return Promise.resolve(); }
function _resolveSessionModelForDisplaySoon(sid) {
  const session = serverSessions[sid].session;
  elements.modelSelect.value = session.model;
  S.activeProfile = session.profile;
  window._defaultModel = session.model;
  window._activeProvider = session.model_provider;
}
function _acknowledgeSessionVisit() {}
function _applyPendingSessionModelForSession() {}
function _clearDeferredActiveSessionExternalRefresh() {}
function _resetScrollDirectionTracker() {}
function _uploadPendingFilesSyncProgressForSession() {}
function clearInflightState() {}
function _rearmActiveSessionStream() {}
function clearCompressionUi() {}
function _clearQueueCardDisplay() {}
function stopApprovalPolling() {}
function startApprovalPolling() {}
function hideApprovalCard() {}
function stopSessionStream() {}
function _updateYoloPill() {}
function stopClarifyPolling() {}
function startClarifyPolling() {}
function hideClarifyCard() {}
function closeOtherLiveStreams() {}
function snapshotLiveTurnHtmlForSession() {}
function _captureSameSessionForceReloadHint() {}
function _clearSameSessionForceReloadHint() {}
function _clearStuckSessionOnBoot() {}
function _opaqueActiveTurnToken(value) { return typeof value === 'string' ? value : null; }
function _isServerIdleSessionRow() { return true; }
function loadInflightState() { return null; }
function _mergePendingSessionMessage() {}
function _mergeInflightTailMessages() {}
function _prepareRunningLiveTail() {}
function _dropCurrentTurnAssistantMessages() {}
function _currentTurnAssistantText() { return ''; }
function _compactTranscriptText() { return ''; }
function _isSessionActivelyViewed() { return true; }
function _sessionVisitHasUnreadState() { return false; }
async function _saveComposerDraftNow() { return undefined; }
async function _ensureMessagesLoaded(sid) {
  S.messages = serverSessions[sid].messages.map(row => ({...row}));
}
function autoResize() { calls.resizes++; }
function renderTray() { calls.trays++; }
function ensureLiveWorklogShell() {}
function appendThinking() {}
function upsertActiveSessionForLocalTurn() {}
function showLiveRunStatus() {}
function attachLiveStream() {}
function markInflight() {}
function saveInflightState() {}
function clearOptimisticSessionStreaming() {}
function hideLiveRunStatus() {}
function removeThinking() {}
function startApprovalPollingForSession() {}
function startClarifyPollingForSession() {}
function showToast() {}
function queueSessionMessage() { calls.queue++; }
function _clearStaleBusyStateBeforeSend() {}
function isCompressionUiRunning() { return false; }
function shouldInterceptCompressionRecoveryContinuation() { return false; }
function _composerTextWithPendingSelections() { return elements.msg.value; }
function _flushSelectionBlocksToComposer() {}
function _chatPayloadModelState() { return {model: S.session.model, model_provider: S.session.model_provider}; }
function _readPendingSessionModel() { return null; }
function _opaqueActiveTurnToken(value) { return typeof value === 'string' && value.trim() ? value : null; }
function _clearComposerDraft() { return Promise.resolve(); }
function _restoreComposerDraftAfterFailedSend() {}
function uploadPendingFiles() { return Promise.resolve([]); }
function parseCommand(text) { return {name: text.split(/\s+/, 1)[0].slice(1), args: text.split(/\s+/).slice(1)}; }
function hideCmdDropdown() {}
const COMMANDS = [{name: 'goal', noEcho: false, fn() { calls.goal++; }}];
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set();
function getAgentCommandMetadata() { return Promise.resolve(null); }
function getBundleCommandMetadata() { return null; }
function _runOptionalPreStartUiStep(label, fn) { return fn(); }
function _runOptionalPostStartUiStep(label, fn) { return fn(); }
function _dismissHandoffHint() {}
function _clearPendingSessionModel() {}
function handlePetSlashCommand() { calls.pet++; return {handled: true, message: 'pet handled'}; }
function api(path, options) {
  if (path === '/api/session/new') {
    newCalled();
    return newResponse;
  }
  if (path.startsWith('/api/session?')) {
    bMetadataCalled();
    return bMetadataResponse;
  }
  if (path === '/api/chat/start') {
    calls.starts++;
    return Promise.resolve({stream_id: 'C-stream', active_turn_token: 'C-token', pending_started_at: 4});
  }
  throw new Error('unexpected API ' + path);
}
async function loadSession(sid, opts = {}) {
  const forceReload = !!opts.force;
  const currentSid = S.session ? S.session.session_id : null;
  const sameSessionForceReload = forceReload && currentSid === sid;
  __LOAD_SWITCH_BODY__
  const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
  if (!data || !_isCurrentLoad()) return;
  __LOAD_METADATA_BODY__
  await _ensureMessagesLoaded(sid, {loadGeneration: _loadGeneration});
  if (!_isCurrentLoad()) return;
  if (activeStreamId) {
    S.busy = true;
    S.activeStreamId = activeStreamId;
  } else {
    __LOAD_IDLE_BODY__
  }
  if (_isCurrentLoad()) _loadingSessionId = null;
}
async function newSession(flash, options = {}) { __NEW_SESSION_BODY__ }
async function send() { __SEND_BODY__ }
function snapshot() {
  return JSON.stringify({
    session: S.session, messages: S.messages, busy: S.busy, activeStreamId: S.activeStreamId,
    activeProfile: S.activeProfile, model: elements.modelSelect.value,
    defaultModel: window._defaultModel, provider: window._activeProvider,
    composer: elements.msg.value, status: elements.composerStatus.textContent,
  });
}
(async () => {
  const sendPromise = send();
  await newCalledPromise;
  const loadPromise = loadSession('B');
  await bMetadataCalledPromise;
  releaseBMetadata(serverSessions.B);
  await loadPromise;
  const before = snapshot();
  const counts = {...calls};
  releaseNew({session: created});
  await sendPromise;
  assert.strictEqual(snapshot(), before);
  assert.deepStrictEqual(calls, counts);
  assert.strictEqual(calls.goal, 0);
  assert.strictEqual(calls.pet, 0);
  assert.strictEqual(calls.queue, 0);
  assert.strictEqual(calls.starts, 0);
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""
    script = (
        script.replace("__COMMAND__", json.dumps(command))
        .replace("__NEW_SESSION_BODY__", _NEW_SESSION_BODY)
        .replace("__LOAD_SWITCH_BODY__", _LOAD_SESSION_SWITCH_BODY)
        .replace("__LOAD_METADATA_BODY__", _LOAD_SESSION_METADATA_BODY)
        .replace("__LOAD_IDLE_BODY__", _LOAD_SESSION_IDLE_BODY)
        .replace("__SEND_BODY__", _function_body(MESSAGES_JS, "send"))
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_chat_start_stamps_shared_optimistic_row_with_server_turn_token():
    body = _function_body(MESSAGES_JS, "send")
    stamp_start = body.index("const _stampActiveTurnRows=(messages, preferredRow=null)=>{")
    stamp_end = body.index("  const _stampInflightTurnState=()=>{", stamp_start)
    stamp = body[stamp_start:stamp_end]

    script = f"""
const userMsg = {{role:'user', content:'repeat me', _ts:50, _pending:true}};
const S = {{messages:[
  {{role:'user', content:'repeat me', _ts:100}},
  {{role:'assistant', content:'completed', _ts:101}},
]}};
const INFLIGHT = {{sid:{{messages:[
  {{role:'user', content:'repeat me', _ts:100}},
  {{role:'assistant', content:'working', _live:true, _ts:102}},
]}}}};
const activeSid = 'sid';
const streamId = 'new-stream';
const _activeTurnToken = 'opaque-server-token';
const startData = {{pending_started_at:200}};
const uploadedNames = [];
const optimisticMessages = [];
{stamp}
_stampActiveTurnRows(S.messages);
_stampActiveTurnRows(INFLIGHT.sid.messages);
S.messages=[{{role:'user',content:'repeat me',_pending:true,_ts:100}}];
INFLIGHT.sid.messages=[{{role:'user',content:'repeat me',_pending:true,_ts:100}}];
_stampActiveTurnRows(S.messages);
_stampActiveTurnRows(INFLIGHT.sid.messages);
process.stdout.write(JSON.stringify({{
  visibleTokens:S.messages.filter(m=>m&&m.role==='user').map(m=>m._active_turn_token||null),
  inflightTokens:INFLIGHT.sid.messages.filter(m=>m&&m.role==='user').map(m=>m._active_turn_token||null),
  visibleAttachmentRow:S.messages.filter(m=>m&&m.role==='user').find(m=>m._active_turn_token===_activeTurnToken)===userMsg,
  inflightAttachmentRow:INFLIGHT.sid.messages.filter(m=>m&&m.role==='user').find(m=>m._active_turn_token===_activeTurnToken)===userMsg,
  replacedPendingRows:S.messages.filter(m=>m&&m.role==='user').map(m=>m._ts),
  replacedPendingOwner:S.messages.find(m=>m&&m._active_turn_token===_activeTurnToken)===userMsg,
}}));
"""
    result = json.loads(subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    ).stdout)

    assert result == {
        "visibleTokens": [None, "opaque-server-token"],
        "inflightTokens": [None, "opaque-server-token"],
        "visibleAttachmentRow": True,
        "inflightAttachmentRow": True,
        "replacedPendingRows": [100, 50],
        "replacedPendingOwner": True,
    }


def test_inflight_storage_compaction_preserves_opaque_turn_token():
    ui_src = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    compact = _function_body(ui_src, "_compactInflightState")
    script = f"""
function _getInflightStateLimits(){{return {{messages:24, toolCalls:48, stringChars:60000}};}}
function _truncateInflightValue(value){{return value;}}
function _compactInflightState(state){{{compact}}}
const state = _compactInflightState({{activeTurnToken:'opaque:token with spaces', messages:[], toolCalls:[]}});
process.stdout.write(JSON.stringify(state));
"""
    result = json.loads(subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    ).stdout)
    assert result["activeTurnToken"] == "opaque:token with spaces"


def test_server_absent_optimistic_first_turn_rows_are_not_kept_forever():
    """A local first-turn sidebar row must expire when /api/chat/start never persisted it."""
    body = _function_body(SESSIONS_JS, "_mergeOptimisticFirstTurnSessions")

    assert "_shouldKeepLocalOnlyOptimisticSessionRow(local)" in body, (
        "server-absent optimistic rows need an explicit keep/drop gate"
    )
    keep_idx = body.index("if(_shouldKeepLocalOnlyOptimisticSessionRow(local))")
    append_idx = body.index("merged.push({...local,is_streaming:true});")
    drop_idx = body.index("_dropStaleOptimisticSessionRow(sid);", append_idx)
    assert keep_idx < append_idx < drop_idx, (
        "local optimistic rows may only be appended inside the explicit keep gate"
    )
    drop_body = _function_body(SESSIONS_JS, "_dropStaleOptimisticSessionRow")
    assert "clearInflightState(sid)" in drop_body, (
        "dropping a phantom row should also clear persisted browser recovery state"
    )


def test_server_idle_row_wins_over_stale_optimistic_count():
    """If the server says the row is idle, stale local message_count/title must not win."""
    body = _function_body(SESSIONS_JS, "_mergeOptimisticFirstTurnSessions")

    assert "const keepLocalOptimistic=" in body
    assert "message_count:keepLocalOptimistic?Math.max(localCount,fetchedCount):fetchedCount" in body, (
        "stale optimistic message_count must not override a confirmed idle server row"
    )
    assert "title:keepLocalOptimistic?(local.title||fetched.title):fetched.title" in body, (
        "stale optimistic provisional title must not override a confirmed idle server row"
    )
