"""Focused regression tests for bounded settled-session message windows."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for settled-window runtime tests")


def _node_driver(body: str, source: Path | None = None) -> dict:
    assert NODE is not None
    source = source or (REPO / "static" / "sessions.js")
    result = subprocess.run(
        [NODE, "-e", body, str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


_EXTRACT = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extractFunction(name) {
  const markers = [`async function ${name}(`, `function ${name}(`];
  let start = -1;
  for (const marker of markers) {
    start = src.indexOf(marker);
    if (start >= 0) break;
  }
  if (start < 0) throw new Error(`missing ${name}`);
  const params = src.indexOf('(', start);
  let paramDepth = 0;
  let brace = -1;
  for (let i = params; i < src.length; i += 1) {
    if (src[i] === '(') paramDepth += 1;
    else if (src[i] === ')') {
      paramDepth -= 1;
      if (paramDepth === 0) {
        brace = src.indexOf('{', i + 1);
        break;
      }
    }
  }
  if (brace < 0) throw new Error(`missing body for ${name}`);
  let depth = 0;
  for (let i = brace; i < src.length; i += 1) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}
"""


def test_settled_window_limit_preserves_loaded_width_and_turn_allowance():
    outcome = _node_driver(
        _EXTRACT
        + r"""
const _INITIAL_MSG_LIMIT = 30;
let _messagesTruncated = true;
let loadedRenderable = 30;
const S = {
  messages: Array.from({length: 30}, () => ({role: 'user'})),
  session: {message_count: 100},
};
function _currentLoadedRenderableMessageCount() { return loadedRenderable; }
eval(extractFunction('_settledSessionMessageWindowLimit'));
const results = [
  _settledSessionMessageWindowLimit({message_count: 103}, {}),
  _settledSessionMessageWindowLimit(null, {reserveNewTurn: true}),
];
_messagesTruncated = false;
results.push(_settledSessionMessageWindowLimit({message_count: 1000}, {}));
_messagesTruncated = true;
loadedRenderable = 80;
S.messages = Array.from({length: 80}, () => ({role: 'user'}));
S.session.message_count = 200;
results.push(_settledSessionMessageWindowLimit({message_count: 205}, {}));
console.log(JSON.stringify(results));
"""
    )

    assert outcome == [33, 60, None, 85]


def test_settled_window_fetch_uses_canonical_session_pagination():
    outcome = _node_driver(
        _EXTRACT
        + r"""
const _INITIAL_MSG_LIMIT = 30;
const _MSG_LIMIT_MAX = 500;
let _msgLimitMax = _MSG_LIMIT_MAX;
let _messagesTruncated = true;
let loadedRenderable = 30;
const S = {
  messages: Array.from({length: 30}, () => ({role: 'user'})),
  session: {message_count: 100},
};
function _currentLoadedRenderableMessageCount() { return loadedRenderable; }
const calls = [];
async function api(url, options) {
  calls.push({url, options});
  return {session: {_messages_truncated: true, _messages_offset: 70, messages: []}};
}
eval(extractFunction('_settledSessionMessageWindowLimit'));
eval(extractFunction('_sessionMessageReloadUrl'));
eval(extractFunction('_settledSessionMessageWindowUrl'));
eval(extractFunction('_fetchSettledSessionMessageWindow'));
(async()=>{
const bounded = await _fetchSettledSessionMessageWindow('sid-1', {message_count: 103}, {});
_messagesTruncated = false;
const full = await _fetchSettledSessionMessageWindow('sid-1', {message_count: 1000}, {});
const forced = await _fetchSettledSessionMessageWindow('sid-1', null, {reserveNewTurn: true, forceBounded: true});
const fullRecoveryUrl = _settledSessionMessageWindowUrl('sid-1', null, {reserveNewTurn: true, forceBounded: true});
_messagesTruncated = true;
loadedRenderable = 600;
S.messages = Array.from({length: 600}, () => ({role: 'user'}));
const aboveCeilingRecoveryUrl = _settledSessionMessageWindowUrl('sid-1', null, {reserveNewTurn: true, forceBounded: true});
console.log(JSON.stringify({bounded, full, forced, fullRecoveryUrl, aboveCeilingRecoveryUrl, calls}));
})().catch(err=>{ console.error(err.stack || err); process.exit(1); });
"""
    )

    assert outcome["bounded"]["_messages_offset"] == 70
    assert outcome["full"] is None
    assert outcome["forced"] is None
    assert "msg_limit=" not in outcome["fullRecoveryUrl"]
    assert "msg_limit=" not in outcome["aboveCeilingRecoveryUrl"]
    assert len(outcome["calls"]) == 1
    assert "session_id=sid-1&messages=1&resolve_model=0&msg_limit=33&expand_renderable=1" in outcome["calls"][0]["url"]
    assert outcome["calls"][0]["options"] == {"timeoutMs": 120000}


def test_reload_limit_preserves_expanded_window_without_force_reload_hint():
    outcome = _node_driver(
        _EXTRACT
        + r"""
const _INITIAL_MSG_LIMIT = 30;
let _messagesTruncated = true;
let _sameSessionForceReloadHint = null;
let loadedRenderable = 90;
const S = {
  messages: Array.from({length: 90}, () => ({role: 'user'})),
  session: {session_id: 'sid-1', message_count: 300},
};
function _currentLoadedRenderableMessageCount() { return loadedRenderable; }
eval(extractFunction('_settledSessionMessageWindowLimit'));
eval(extractFunction('_messageReloadLimitForSession'));
console.log(JSON.stringify({
  expanded: _messageReloadLimitForSession('sid-1'),
  initial: (() => {
    loadedRenderable = 30;
    S.messages = Array.from({length: 30}, () => ({role: 'user'}));
    return _messageReloadLimitForSession('sid-1');
  })(),
}));
"""
    )

    assert outcome == {"expanded": 90, "initial": 30}


def test_mutation_reload_uses_renderable_width_not_hidden_tool_rows():
    outcome = _node_driver(
        _EXTRACT
        + r"""
const _INITIAL_MSG_LIMIT = 30;
let _messagesTruncated = true;
let _sameSessionForceReloadHint = null;
let loadedRenderable = 30;
const S = {
  messages: [
    ...Array.from({length: 30}, () => ({role: 'assistant', content: 'visible'})),
    ...Array.from({length: 30}, () => ({role: 'tool', content: 'hidden'})),
  ],
  session: {session_id: 'sid-1', message_count: 300},
};
function _currentLoadedRenderableMessageCount() { return loadedRenderable; }
eval(extractFunction('_captureSameSessionForceReloadHint'));
eval(extractFunction('_messageReloadLimitForSession'));
_captureSameSessionForceReloadHint('sid-1');
const beforeUndo = _messageReloadLimitForSession('sid-1');
S.session.message_count = 299;
const afterUndo = _messageReloadLimitForSession('sid-1');
console.log(JSON.stringify({beforeUndo, afterUndo}));
"""
    )

    assert outcome == {"beforeUndo": 30, "afterUndo": 30}


def test_rotated_done_window_uses_continuation_session_data():
    compact = "".join(MESSAGES_JS.split())
    done_start = MESSAGES_JS.index("source.addEventListener('done'")
    done_end = MESSAGES_JS.index("source.addEventListener('stream_end'", done_start)
    done_body = "".join(MESSAGES_JS[done_start:done_end].split())
    completed_sid_idx = done_body.index("constcompletedSid=completedSession.session_id||activeSid;")
    fetch_idx = done_body.index("_settledDoneWindow=await_fetchSettledSessionMessageWindow")
    assert completed_sid_idx < fetch_idx
    assert "_settledDoneWindow=await_fetchSettledSessionMessageWindow(completedSid,completedSession)" in compact
    assert "_settledDoneWindow=await_fetchSettledSessionMessageWindow(activeSid,completedSession)" not in done_body

    outcome = _node_driver(
        r"""
const activeSid = 'parent-session';
const completedSession = {session_id: 'continuation-session', message_count: 101};
const completedSid = completedSession.session_id || activeSid;
const windows = {
  'parent-session': {
    messages: [{role: 'assistant', content: 'STALE PARENT WINDOW'}],
    tool_calls: [{id: 'parent-tool'}],
    _messages_truncated: true,
    _messages_offset: 70,
  },
  'continuation-session': {
    messages: [{role: 'assistant', content: 'FINAL ANSWER'}],
    tool_calls: [{id: 'continuation-tool'}],
    _messages_truncated: true,
    _messages_offset: 70,
  },
};
async function _fetchSettledSessionMessageWindow(sid) {
  return windows[sid];
}
(async()=>{
  const _settledDoneWindow = await _fetchSettledSessionMessageWindow(completedSid, completedSession);
  const settled = {
    session_id: completedSid,
    messages: _settledDoneWindow.messages,
    tool_calls: _settledDoneWindow.tool_calls,
    _messages_offset: _settledDoneWindow._messages_offset,
  };
  console.log(JSON.stringify(settled));
})().catch(err=>{ console.error(err.stack || err); process.exit(1); });
"""
    )
    assert outcome == {
        "session_id": "continuation-session",
        "messages": [{"role": "assistant", "content": "FINAL ANSWER"}],
        "tool_calls": [{"id": "continuation-tool"}],
        "_messages_offset": 70,
    }


def test_done_and_recovery_paths_do_not_expand_the_render_window():
    compact = "".join(MESSAGES_JS.split())
    assert "_fetchSettledSessionMessageWindow(completedSid,completedSession)" in compact
    assert "_settledSessionMessageWindowUrl(activeSid,null,{reserveNewTurn:true,forceBounded:true})" in compact
    assert "_messagesTruncated=!!session._messages_truncated" in compact
    assert "_messageRenderWindowSize=Math.max(typeof _currentMessageRenderWindowSize" not in compact

    done_start = MESSAGES_JS.index("source.addEventListener('done'")
    done_end = MESSAGES_JS.index("source.addEventListener('stream_end'", done_start)
    done_body = MESSAGES_JS[done_start:done_end]
    assert done_body.index("const _settledDoneInflightSnapshot") < done_body.index("_clearOwnerInflightState({deferSessionStreamResume:true})")
    refresh_idx = done_body.index("await _fetchSettledSessionMessageWindow")
    ownership_idx = done_body.index("if(isActiveSession&&!_isSessionCurrentPane(activeSid)) isActiveSession=false;")
    assert refresh_idx < ownership_idx < done_body.index("S.session=_settledSession")


def test_done_defers_session_stream_resume_until_after_async_settlement():
    done_start = MESSAGES_JS.index("source.addEventListener('done'")
    done_end = MESSAGES_JS.index("source.addEventListener('stream_end'", done_start)
    done_body = MESSAGES_JS[done_start:done_end]
    clear_idx = done_body.index("_clearOwnerInflightState({deferSessionStreamResume:true})")
    fetch_idx = done_body.index("await _fetchSettledSessionMessageWindow")
    idle_idx = done_body.index("_setActivePaneIdleIfOwner()")
    resume_idx = done_body.rindex("_resumeSessionStreamAfterLiveChat(completedSid)")
    assert clear_idx < fetch_idx < idle_idx < resume_idx
    assert "finally" in done_body[idle_idx:resume_idx]


def _done_continuation_stream_harness(settlement_mode: str) -> dict:
    assert settlement_mode in {"success", "rejection"}
    return _node_driver(
        _EXTRACT
        + rf"""
const path = require('path');
const uiSrc = fs.readFileSync(path.join(path.dirname(process.argv[1]), 'ui.js'), 'utf8');
function extractFrom(source, name) {{
  const markers = [`async function ${{name}}(`, `function ${{name}}(`];
  let start = -1;
  for (const marker of markers) {{
    start = source.indexOf(marker);
    if (start >= 0) break;
  }}
  if (start < 0) throw new Error(`missing ${{name}}`);
  const params = source.indexOf('(', start);
  let paramDepth = 0;
  let brace = -1;
  for (let i = params; i < source.length; i += 1) {{
    if (source[i] === '(') paramDepth += 1;
    else if (source[i] === ')') {{
      paramDepth -= 1;
      if (paramDepth === 0) {{
        brace = source.indexOf('{{', i + 1);
        break;
      }}
    }}
  }}
  if (brace < 0) throw new Error(`missing body for ${{name}}`);
  let depth = 0;
  for (let i = brace; i < source.length; i += 1) {{
    if (source[i] === '{{') depth += 1;
    else if (source[i] === '}}') {{
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }}
  }}
  throw new Error(`unterminated ${{name}}`);
}}

const parentSid = 'parent-session';
const continuationSid = 'continuation-session';
const streamId = 'stream-1';
const fallbackMessages = [
  {{role:'user', content:'bounded question'}},
  {{role:'assistant', content:'bounded local answer', _live:true}},
];
const fallbackToolCalls = [{{id:'local-tool', name:'read_file', done:true}}];
const settledMessages = [
  {{role:'user', content:'settled question'}},
  {{role:'assistant', content:'settled authoritative answer'}},
];
const settledToolCalls = [{{id:'settled-tool', name:'read_file', done:true}}];

const storage = new Map([
  ['hermes-webui-inflight', JSON.stringify({{sid:parentSid,streamId,ts:1}})],
  ['hermes-webui-inflight-state', JSON.stringify({{[parentSid]:{{streamId}}}})],
]);
global.localStorage = {{
  getItem:key=>storage.has(key)?storage.get(key):null,
  setItem:(key,value)=>storage.set(key,String(value)),
  removeItem:key=>storage.delete(key),
}};
global.location = {{href:'http://example.test/', pathname:'/', search:''}};
global.history = {{replaceState:()=>{{}}}};
global.document = {{
  hidden:false,
  visibilityState:'visible',
  baseURI:'http://example.test/',
  addEventListener:()=>{{}},
  removeEventListener:()=>{{}},
  querySelector:()=>null,
  hasFocus:()=>true,
}};
global.window = global;
window.location = global.location;
window._compressionUi = null;
window._streamJustFinished = false;

const S = global.S = {{
  session:{{session_id:parentSid,message_count:90,pending_started_at:1}},
  messages:fallbackMessages.map(message=>({{...message}})),
  toolCalls:fallbackToolCalls.map(tool=>({{...tool}})),
  activeStreamId:streamId,
  activeProfile:'default',
  busy:true,
  todos:[],
}};
const INFLIGHT = global.INFLIGHT = {{
  [parentSid]:{{
    messages:fallbackMessages.map(message=>({{...message}})),
    toolCalls:fallbackToolCalls.map(tool=>({{...tool}})),
    uploaded:[],
  }},
}};
const LIVE_STREAMS = global.LIVE_STREAMS = {{}};
const _STREAM_WAS_HIDDEN = global._STREAM_WAS_HIDDEN = {{}};
const _STREAM_NOTIFICATION_BACKGROUND = global._STREAM_NOTIFICATION_BACKGROUND = {{}};
const _desktopBackgroundedForNotifications = false;
let _messagesTruncated = true;
let _oldestIdx = 60;
let _loadingSessionId = null;
let _queueDrainSid = null;
let _approvalSessionId = null;
let _clarifySessionId = null;
let _sessionEventSource = null;
let _sessionStreamSessionId = null;
let _sessionStreamReconnectTimer = null;
let _sessionStreamHiddenSid = null;
let _sessionStreamHiddenPollTimer = null;
let _sessionStreamHiddenPollSid = null;
let _sessionStreamHiddenPollFalseStreamId = null;
let _sessionStreamHiddenPollFalseCount = 0;
const _SESSION_STREAM_HIDDEN_POLL_MAX_FALSE = 3;
const INFLIGHT_KEY = 'hermes-webui-inflight';
const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state';

class FakeEventSource {{
  static instances = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 2;
  constructor(url) {{
    this.url = String(url);
    this.listeners = Object.create(null);
    this.readyState = FakeEventSource.OPEN;
    FakeEventSource.instances.push(this);
  }}
  addEventListener(name, fn) {{
    (this.listeners[name] || (this.listeners[name] = [])).push(fn);
  }}
  emit(name, data) {{
    for (const fn of this.listeners[name] || []) fn({{data:JSON.stringify(data),lastEventId:''}});
  }}
  close() {{ this.readyState = FakeEventSource.CLOSED; }}
}}
global.EventSource = FakeEventSource;

const noops = [
  '_resetStreamScrollFollow','ensureLiveWorklogShell','resetTurnWorkspaceMutations',
  'snapshotLiveTurnHtmlForSession','_clearLiveRunStatusTimer','hideLiveRunStatus',
  'saveInflightState','_setSessionViewedCount','_markSessionViewed','_markSessionCompletionUnread',
  '_clearSessionCompletionUnread','_markSessionCompletedInList','_clearApprovalPendingForSession',
  '_clearClarifyPendingForSession','stopApprovalPolling','hideApprovalCard','stopClarifyPolling',
  'hideClarifyCard','finalizeThinkingCard','_cancelAnimationFramePendingStreamRender',
  '_streamFadeCleanupReduceMotionListener','_smdEndParser','_flushReasoningToAnchor',
  '_applyToAnchor','_scheduleAnchorRegistryCleanup','_clearAnchorProseIncrementalNode',
  '_hydrateTodosFromSession','clearVisibleMessageRowCache','_setActiveSessionUrl',
  '_attachProjectedAnchorSceneToLastAssistant','renderSessionArtifacts','clearLiveToolCards',
  'removeThinking','syncTopbar','renderMessages','_followSettledDoneIfStillPinned',
  'noteWorkspaceMutationsFromToolCalls','loadDir','renderSessionList','playNotificationSound',
  'sendBrowserNotification','setComposerStatus','setStatus','setBusy','updateQueueBadge',
  'trackBackgroundError','_stopHiddenActiveStreamPoll','_startHiddenActiveStreamPoll',
];
for (const name of noops) global[name] = ()=>{{}};
global.$ = ()=>null;
global.msgContent = message=>String(message&&message.content||'');
global._isPreservedCompressionTaskListMarkerOnlyText = ()=>false;
global._replaceMarkerOnlyAssistantWithStreamError = ()=>false;
global._filterRecoveryControlMessages = messages=>messages;
global._carryForwardEphemeralTurnFields = (_oldMessages,newMessages)=>newMessages;
global._splitThinkFromContent = (content,reasoning)=>({{content,reasoning}});
global._mergeSettledToolCallsWithLiveMetadata = calls=>calls;
global._shouldUseLiveProseFade = ()=>false;
global._isMessagePaneNearBottom = ()=>true;
global._shouldFollowMessagesOnDomReplace = ()=>false;
global._isDocumentVisibleAndFocused = ()=>true;
global._completionNotificationPreviewText = ()=>'';
global._shouldForceCompletionNotification = ()=>false;
global._chatPayloadModelState = ()=>({{model:'model',model_provider:'provider'}});
global._apiUrl = value=>value;
global._readInflightStateMap = ()=>{{
  try {{ return JSON.parse(localStorage.getItem(INFLIGHT_STATE_KEY)||'{{}}'); }}
  catch (_) {{ return {{}}; }}
}};
global._isSessionCurrentPane = sid=>!!(sid&&S.session&&S.session.session_id===sid);
global._isSessionActivelyViewed = sid=>global._isSessionCurrentPane(sid);
global._clearStreamHidden = (sid,ownerStreamId)=>{{
  const entry=_STREAM_WAS_HIDDEN[sid];
  if(entry&&(!ownerStreamId||!entry.streamId||entry.streamId===ownerStreamId)) delete _STREAM_WAS_HIDDEN[sid];
}};
global._clearStreamNotificationBackground = (sid,ownerStreamId)=>{{
  const entry=_STREAM_NOTIFICATION_BACKGROUND[sid];
  if(entry&&(!ownerStreamId||!entry.streamId||entry.streamId===ownerStreamId)) delete _STREAM_NOTIFICATION_BACKGROUND[sid];
}};
global._bindStreamHiddenTracker = ()=>{{}};
global._runJournalReplayParams = ()=>'';
global._extractInlineThinkingFromContent = (content,reasoning)=>({{content:String(content||''),reasoning:String(reasoning||'')}});
global._syncCtxIndicator = ()=>{{}};
global._mergeUsageForCtxIndicator = (usage,fallback)=>({{...fallback,...usage}});

let resolveSettlement;
let rejectSettlement;
let fetchCalls = [];
const settlement = new Promise((resolve,reject)=>{{
  resolveSettlement=resolve;
  rejectSettlement=reject;
}});
global._fetchSettledSessionMessageWindow = (sid,session)=>{{
  fetchCalls.push({{sid,sessionId:session&&session.session_id}});
  return settlement;
}};

for (const name of ['clearInflightState','clearInflight']) eval(extractFrom(uiSrc,name));
for (const name of [
  'closeLiveStream','closeOtherLiveStreams','attachLiveStream',
  '_chatStreamActiveForSession','_suspendSessionStreamForLiveChat',
  '_resumeSessionStreamAfterLiveChat','stopSessionStream','startSessionStream',
]) eval(extractFunction(name));

const flush = () => new Promise(resolve=>setTimeout(resolve,0));
const sessionSources = () => FakeEventSource.instances.filter(source=>source.url.includes('api/session/stream'));
const chatSources = () => FakeEventSource.instances.filter(source=>source.url.includes('api/chat/stream'));

(async()=>{{
  attachLiveStream(parentSid,streamId);
  await flush();
  const parentSource=chatSources()[0];
  if(!parentSource) throw new Error('production attachLiveStream did not create the parent chat EventSource');
  parentSource.emit('done',{{
    stream_id:streamId,
    status:'completed',
    session:{{
      session_id:continuationSid,
      message_count:92,
      messages:[{{role:'assistant',content:'terminal snapshot'}}],
      tool_calls:[],
      _messages_truncated:true,
      _messages_offset:61,
    }},
  }});
  await Promise.resolve();
  await flush();
  const pending={{
    fetchCalls:fetchCalls.slice(),
    sessionSources:sessionSources().map(source=>source.url),
    sessionId:S.session&&S.session.session_id,
    activeStreamId:S.activeStreamId,
    parentInflightPresent:Object.prototype.hasOwnProperty.call(INFLIGHT,parentSid),
  }};

  if ({json.dumps(settlement_mode)} === 'success') {{
    resolveSettlement({{
      session_id:continuationSid,
      message_count:92,
      messages:settledMessages.map(message=>({{...message}})),
      tool_calls:settledToolCalls.map(tool=>({{...tool}})),
      _messages_truncated:true,
      _messages_offset:62,
    }});
  }} else {{
    rejectSettlement(new Error('settled window unavailable'));
  }}
  await flush();
  await flush();
  const afterFirstResume=sessionSources().map(source=>source.url);
  await flush();

  console.log(JSON.stringify({{
    mode:{json.dumps(settlement_mode)},
    pending,
    final:{{
      sessionId:S.session&&S.session.session_id,
      messages:S.messages,
      toolCalls:S.toolCalls,
      activeStreamId:S.activeStreamId,
      parentInflightPresent:Object.prototype.hasOwnProperty.call(INFLIGHT,parentSid),
      messagesTruncated:_messagesTruncated,
      oldestIdx:_oldestIdx,
      sessionSources:sessionSources().map(source=>source.url),
      afterFirstResume,
      sessionStreamSessionId:_sessionStreamSessionId,
      sessionEventSourceUrl:_sessionEventSource&&_sessionEventSource.url,
      chatSourceCount:chatSources().length,
    }},
  }}));
}})().catch(error=>{{console.error(error.stack||error);process.exit(1);}});
""",
        REPO / "static" / "messages.js",
    )


@pytest.mark.parametrize("settlement_mode", ["success", "rejection"])
def test_done_restarts_continuation_session_stream_after_settlement(settlement_mode):
    outcome = _done_continuation_stream_harness(settlement_mode)

    assert outcome["pending"] == {
        "fetchCalls": [
            {"sid": "continuation-session", "sessionId": "continuation-session"}
        ],
        "sessionSources": [],
        "sessionId": "parent-session",
        "activeStreamId": "stream-1",
        "parentInflightPresent": False,
    }

    final = outcome["final"]
    assert final["sessionId"] == "continuation-session"
    assert final["activeStreamId"] is None
    assert final["parentInflightPresent"] is False
    assert final["messagesTruncated"] is True
    assert final["sessionStreamSessionId"] == "continuation-session"
    assert len(final["sessionSources"]) == 1
    assert final["afterFirstResume"] == final["sessionSources"]
    assert "session_id=continuation-session" in final["sessionSources"][0]
    assert final["sessionEventSourceUrl"] == final["sessionSources"][0]
    assert final["chatSourceCount"] == 1

    if settlement_mode == "success":
        assert [(message["role"], message["content"]) for message in final["messages"]] == [
            ("user", "settled question"),
            ("assistant", "settled authoritative answer"),
        ]
        assert final["toolCalls"] == [
            {"id": "settled-tool", "name": "read_file", "done": True}
        ]
        assert final["oldestIdx"] == 62
    else:
        assert [(message["role"], message["content"]) for message in final["messages"]] == [
            ("user", "bounded question"),
            ("assistant", "bounded local answer"),
        ]
        assert final["messages"][1]["_live"] is True
        assert final["toolCalls"] == [
            {"id": "local-tool", "name": "read_file", "done": True}
        ]
        assert final["oldestIdx"] == 60


def test_settled_window_helpers_and_cross_module_callers_are_present():
    assert "function _settledSessionMessageWindowLimit" in SESSIONS_JS
    assert "async function _fetchSettledSessionMessageWindow" in SESSIONS_JS
    assert "_fetchSettledSessionMessageWindow(completedSid,completedSession)" in MESSAGES_JS


def test_reconnect_refresh_uses_a_bounded_session_window():
    start = UI_JS.index("async function refreshSession()")
    end = UI_JS.index("// ── Update banner", start)
    body = "".join(UI_JS[start:end].split())
    assert "_messageReloadLimitForSession(sid)" in body
    assert "_sessionMessageReloadUrl(sid,refreshLimit)" in body
    assert "api(`/api/session?session_id=${encodeURIComponent(S.session.session_id)}`)" not in body
