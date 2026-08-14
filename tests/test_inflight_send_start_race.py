"""Regression coverage for send/start optimistic INFLIGHT races."""
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[brace + 1 : i - 1]


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
def test_delayed_chat_start_keeps_a_owned_state_out_of_visible_b_pane(pane_state):
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
function renderMessages() {{}}
function renderTray() {{}}
function autoResize() {{}}
function setComposerStatus() {{}}
function setStatus() {{}}
function setBusy(value) {{ S.busy = value; visibleCall('busy'); }}
function updateSendBtn() {{ calls.busyUi++; visibleCall('send'); }}
function ensureLiveWorklogShell() {{ calls.worklog++; visibleCall('worklog'); }}
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
function _runOptionalPreStartUiStep(label, fn) {{ return fn(); }}
function _runOptionalPostStartUiStep(label, fn) {{ return fn(); }}
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
  assert.strictEqual(calls.busyUi, 1);
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
