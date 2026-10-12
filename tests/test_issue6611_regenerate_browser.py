import json
import subprocess
from pathlib import Path

from tests._issue6611_fixture import load_issue6611_fixture
from tests.js_source_extract import extract_function


ROOT = Path(__file__).parents[1]


def _start_regeneration_source():
    source = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = source.index("async function startRegeneration(")
    end = source.index("\nconst LIVE_STREAMS=", start)
    return source[start:end]


def _run_node(scenario, *, with_metadata=False):
    function_source = _start_regeneration_source()
    ui_source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    start_ownership_helpers = "\n".join(
        extract_function(ui_source, name)
        for name in ("_captureSessionActiveTurnIdentity", "_acceptedStartMayUpdateSession")
    )
    initial_messages = load_issue6611_fixture()["rows"]
    if with_metadata:
        initial_messages[0].update({"attachments": ["proof.txt"], "custom": "keep"})
    messages_json = json.dumps(initial_messages)
    script = f"""
const result={{renders:0,busy:[],attached:[],bodies:[],thinking:0}};
let S={{session:{{session_id:'s1',regeneration_revision:'rev-1'}},messages:{messages_json}}};
const INFLIGHT={{}};
function renderMessages(){{result.renders++;}}
function setBusy(v){{result.busy.push(v);}}
function ensureLiveWorklogShell(){{result.thinking++;}}
function appendThinking(){{result.thinking++;}}
function removeThinking(){{result.thinking--;}}
function setComposerStatus(){{}}
function clearInflightState(){{}}
function markInflight(sid,streamId){{result.marked=[sid,streamId];}}
function saveInflightState(){{}}
function showLiveRunStatus(){{}}
function updateSendBtn(){{}}
function renderSessionList(){{}}
function applySessionTitleUpdate(){{}}
function attachLiveStream(sid,streamId,files){{result.attached.push([sid,streamId,files]);}}
{start_ownership_helpers}
{function_source}
async function api(_path, options){{
  result.bodies.push(JSON.parse(options.body));
  if('{scenario}'==='reject') throw new Error('typed rejection');
  if('{scenario}'==='switch'){{
    S.session={{session_id:'s2'}};
    S.messages=[{{role:'user',content:'other session'}}];
  }}
  return {{stream_id:'stream-1',pending_started_at:123,title:'Title'}};
}}
(async()=>{{
  try{{await startRegeneration('s1','rev-1');}}catch(error){{result.error=error.message;}}
  result.messages=S.messages;
  result.session=S.session;
  result.inflight=Object.keys(INFLIGHT);
  process.stdout.write(JSON.stringify(result));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_reporter_flow_keeps_one_prompt_and_adopts_one_accepted_stream():
    result = _run_node("success", with_metadata=True)
    assert [row["role"] for row in result["messages"]] == ["user"]
    assert result["messages"][0]["content"] == "same prompt"
    assert result["messages"][0]["attachments"] == ["proof.txt"]
    assert result["messages"][0]["custom"] == "keep"
    assert result["attached"] == [["s1", "stream-1", []]]
    assert result["bodies"] == [
        {"session_id": "s1", "regenerate": True, "regeneration_revision": "rev-1"}
    ]


def test_regeneration_binds_projected_owner_and_storage_to_new_turn_token():
    from api.helpers import redact_session_data
    from api.process_event_utils import build_active_turn_token

    stream_id = "regeneration-stream"
    started_at = 123.5
    token = build_active_turn_token(stream_id, started_at)
    raw_rows = [
        {
            "role": "user", "content": "same prompt", "id": "older-user",
            "_source": "webui", "_ts": 100, "_active_turn_token": "older-turn-token",
            "attachments": ["older.txt"],
        },
        {"role": "assistant", "content": "older answer", "_ts": 101},
        {
            "role": "user", "content": "same prompt", "id": "selected-user",
            "_source": "webui", "_ts": 110, "_active_turn_token": token,
            "attachments": ["selected.txt"],
        },
        {
            "role": "assistant", "content": "", "_ts": 124,
            "tool_calls": [
                {"id": "regen-call", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "content": "result", "_ts": 124.1},
    ]
    public_session = redact_session_data(
        {
            "session_id": "s1", "active_stream_id": stream_id,
            "active_turn_token": token, "pending_started_at": started_at,
            "pending_user_message": "same prompt", "pending_attachments": ["selected.txt"],
            "messages": raw_rows,
        }
    )
    assert [row.get("_active_turn_user") for row in public_session["messages"]] == [
        None, None, True, None, None
    ]
    assert all("_active_turn_token" not in row for row in public_session["messages"])

    initial_rows = [
        {**raw_rows[0]},
        {**raw_rows[1]},
        {**raw_rows[2], "_active_turn_token": "previous-selected-turn"},
        {"role": "assistant", "content": "provider failed", "_error": True},
    ]
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    limits_start = source.index("const INFLIGHT_STATE_DEFAULT_LIMITS = {")
    limits_end = source.index("\n};", limits_start) + 3
    storage_helpers = "\n".join(
        [
            source[limits_start:limits_end],
            *(
                extract_function(source, name)
                for name in (
                    "_boundedInflightInt", "_getInflightStateLimits", "_truncateInflightValue",
                    "_compactInflightState", "_readInflightStateMap", "_isStorageQuotaError",
                    "_writeInflightStateMap", "saveInflightState",
                )
            ),
        ]
    )
    ui_owner_helpers = "\n".join(
        extract_function(source, name)
        for name in (
            "_captureSessionActiveTurnIdentity", "_acceptedStartMayUpdateSession",
            "_activeTurnTokenMatches", "_pendingActiveTurnUserMessage",
        )
    )
    sessions_source = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    reattach_helpers = "\n".join(
        extract_function(sessions_source, name)
        for name in (
            "_messageComparableText", "_stripAttachedFilesMarker", "_stripForcedSkillEnvelope",
            "_normalizeUserTranscriptText", "_sameTranscriptMessage", "_opaqueActiveTurnToken",
            "_currentTailUserMessage", "_hasCurrentTailUserDuplicate", "_mergeInflightTailMessages",
        )
    )
    function_source = _start_regeneration_source()
    script = r"""
const assert = require('assert');
const result = {attached:[],marked:[],saved:[]};
const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state';
const INFLIGHT_KEY = 'hermes-webui-inflight';
const localStorage = {
  values:Object.create(null),
  getItem(key){return this.values[key]||null;},
  setItem(key,value){this.values[key]=String(value);},
  removeItem(key){delete this.values[key];},
};
const window = {};
let S = {session:{session_id:'s1',regeneration_revision:'rev-1'},messages:__INITIAL_ROWS__};
const INFLIGHT = {s1:{
  messages:JSON.parse(JSON.stringify(__INITIAL_ROWS__)),uploaded:[],toolCalls:[],
  activeTurnToken:'previous-selected-turn',streamId:'previous-stream',
}};
function renderMessages(){}
function setBusy(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function removeThinking(){}
function setComposerStatus(){}
function clearInflightState(){}
function markInflight(sid,streamId){result.marked.push([sid,streamId]);}
function showLiveRunStatus(){}
function updateSendBtn(){}
function renderSessionList(){}
function applySessionTitleUpdate(){}
function attachLiveStream(sid,streamId,files){result.attached.push([sid,streamId,files]);}
__STORAGE_HELPERS__
__OWNER_HELPERS__
__REATTACH_HELPERS__
__REGENERATION_FUNCTION__
let releaseStart, notifyStart;
const startCalled = new Promise(resolve=>notifyStart=resolve);
async function api(_path, options){
  result.body=JSON.parse(options.body);
  notifyStart();
  return await new Promise(resolve=>releaseStart=resolve);
}
(async()=>{
  const regeneration = startRegeneration('s1','rev-1');
  await startCalled;
  // Same-session reload replaces the pane arrays with the actual server public projection
  // before the accepted regeneration response returns.
  S.session = {...__PUBLIC_SESSION__,regeneration_revision:null};
  S.messages = JSON.parse(JSON.stringify(__PUBLIC_SESSION__.messages));
  releaseStart(__START_RESULT__);
  await regeneration;
  const inflight = INFLIGHT.s1;
  const liveOwner = inflight.messages.find(row=>row&&row.id==='selected-user'&&row.role==='user');
  const tail = [
    {...liveOwner},
    {role:'assistant',content:'working',_live:true,_active_turn_token:__TURN_TOKEN__},
  ];
  const reattached = _mergeInflightTailMessages(S.messages,tail,__TURN_TOKEN__,S.session);
  const persisted = JSON.parse(localStorage.getItem(INFLIGHT_STATE_KEY)).s1;
  result.sessionToken = S.session.active_turn_token;
  result.inflightToken = inflight.activeTurnToken;
  result.ownerToken = liveOwner._active_turn_token;
  result.ownerAttachments = liveOwner.attachments;
  result.older = inflight.messages.find(row=>row&&row.id==='older-user');
  result.users = reattached.filter(row=>row&&row.role==='user');
  result.persistedToken = persisted.activeTurnToken;
  result.persistedOwnerToken = persisted.messages.find(row=>row&&row.id==='selected-user')._active_turn_token;
  result.persistedOlderToken = persisted.messages.find(row=>row&&row.id==='older-user')._active_turn_token||null;
  result.marked = result.marked;
  process.stdout.write(JSON.stringify(result));
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
"""
    script = (
        script.replace("__INITIAL_ROWS__", json.dumps(initial_rows))
        .replace("__PUBLIC_SESSION__", json.dumps(public_session))
        .replace("__START_RESULT__", json.dumps({
            "stream_id": stream_id,
            "active_turn_token": token,
            "pending_started_at": started_at,
            "title": "Regenerated",
        }))
        .replace("__TURN_TOKEN__", json.dumps(token))
        .replace("__STORAGE_HELPERS__", storage_helpers)
        .replace("__OWNER_HELPERS__", ui_owner_helpers)
        .replace("__REATTACH_HELPERS__", reattach_helpers)
        .replace("__REGENERATION_FUNCTION__", function_source)
    )
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["sessionToken"] == token
    assert result["inflightToken"] == token
    assert result["ownerToken"] == token
    assert result["ownerAttachments"] == ["selected.txt"]
    assert result["older"]["attachments"] == ["older.txt"]
    assert all(row["_active_turn_token"] == token for row in result["users"] if row["id"] == "selected-user")
    assert len(result["users"]) == 2
    assert result["persistedToken"] == token
    assert result["persistedOwnerToken"] == token
    assert result["persistedOlderToken"] is None
    assert result["marked"] == [["s1", stream_id]]


def test_late_regeneration_response_cannot_replace_newer_same_session_owner():
    from api.helpers import redact_session_data
    from api.process_event_utils import build_active_turn_token

    token_b = build_active_turn_token("B-stream", 40.0)
    public_session = redact_session_data(
        {
            "session_id": "s1", "active_stream_id": "B-stream",
            "active_turn_token": token_b, "pending_started_at": 40.0,
            "pending_user_message": "same prompt", "pending_attachments": ["B-file.txt"],
            "messages": [
                {"role": "user", "content": "same prompt", "id": "older-user", "_ts": 10,
                 "_active_turn_token": "older-token", "attachments": ["older.txt"]},
                {"role": "assistant", "content": "older reply", "_ts": 11},
                {"role": "user", "content": "same prompt", "id": "current-user", "_ts": 40,
                 "_active_turn_token": token_b},
                {"role": "assistant", "content": "B running", "_ts": 41},
            ],
        }
    )
    assert public_session["messages"][2]["_active_turn_user"] is True
    assert all("_active_turn_token" not in row for row in public_session["messages"])

    ui_source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    sessions_source = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    shared_helpers = "\n".join(
        extract_function(ui_source, name)
        for name in (
            "_captureSessionActiveTurnIdentity", "_acceptedStartMayUpdateSession",
            "_activeTurnTokenMatches", "_pendingActiveTurnUserMessage",
        )
    )
    transcript_helpers = "\n".join(
        extract_function(sessions_source, name)
        for name in (
            "_messageComparableText", "_stripAttachedFilesMarker", "_stripForcedSkillEnvelope",
            "_normalizeUserTranscriptText", "_sameTranscriptMessage", "_opaqueActiveTurnToken",
        )
    )
    script = r"""
const assert = require('assert');
const publicSession=__PUBLIC_SESSION__;
const result={attached:[],marked:[],saved:[]};
let S={session:{session_id:'s1',regeneration_revision:'rev-1',active_stream_id:null,active_turn_token:'old-token'},
  messages:[{role:'user',content:'same prompt',id:'selected-user',attachments:['A-file.txt']},
    {role:'assistant',content:'failed'}],activeStreamId:null,busy:false};
const INFLIGHT={s1:{streamId:'B-stream',activeTurnToken:publicSession.active_turn_token,
  messages:JSON.parse(JSON.stringify(publicSession.messages)),uploaded:['B-file.txt'],toolCalls:[{id:'B-tool'}],reattach:true}};
const localStorage={values:Object.create(null),getItem(k){return this.values[k]||null;},setItem(k,v){this.values[k]=String(v);},removeItem(k){delete this.values[k];}};
localStorage.setItem('hermes-webui-inflight-state',JSON.stringify({s1:INFLIGHT.s1}));
localStorage.setItem('hermes-webui-inflight',JSON.stringify({sid:'s1',streamId:'B-stream'}));
function renderMessages(){}
function setBusy(value){S.busy=!!value;}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function removeThinking(){}
function setComposerStatus(){}
function clearInflightState(){}
function markInflight(sid,streamId){result.marked.push([sid,streamId]);}
function saveInflightState(sid,state){result.saved.push([sid,JSON.parse(JSON.stringify(state))]);localStorage.setItem('hermes-webui-inflight-state',JSON.stringify({[sid]:state}));}
function showLiveRunStatus(){}
function updateSendBtn(){}
function renderSessionList(){}
function applySessionTitleUpdate(){}
function attachLiveStream(sid,streamId){result.attached.push([sid,streamId]);}
function _isSessionCurrentPane(sid){return !!(S.session&&S.session.session_id===sid);}
function _stripWorkspaceDisplayPrefix(value){return value;}
__SHARED_HELPERS__
__TRANSCRIPT_HELPERS__
__REGENERATION_FUNCTION__
let releaseStart,notifyStart;
const startCalled=new Promise(resolve=>notifyStart=resolve);
async function api(){notifyStart();return await new Promise(resolve=>releaseStart=resolve);}
(async()=>{
  const task=startRegeneration('s1','rev-1');
  await startCalled;
  S.session={...publicSession,regeneration_revision:null};
  S.messages=JSON.parse(JSON.stringify(publicSession.messages));
  S.activeStreamId='B-stream';S.busy=true;
  const before=JSON.stringify({session:S.session,messages:S.messages,activeStreamId:S.activeStreamId,busy:S.busy,
    inflight:INFLIGHT.s1,storage:localStorage.values});
  releaseStart({stream_id:'A-stream',active_turn_token:'A-token',pending_started_at:30});
  await task;
  assert.strictEqual(JSON.stringify({session:S.session,messages:S.messages,activeStreamId:S.activeStreamId,busy:S.busy,
    inflight:INFLIGHT.s1,storage:localStorage.values}),before);
  assert.deepStrictEqual(result,{attached:[],marked:[],saved:[]});
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
"""
    script = (
        script.replace("__PUBLIC_SESSION__", json.dumps(public_session))
        .replace("__SHARED_HELPERS__", shared_helpers)
        .replace("__TRANSCRIPT_HELPERS__", transcript_helpers)
        .replace("__REGENERATION_FUNCTION__", _start_regeneration_source())
    )
    completed = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_issue_artifact_regeneration_leaves_one_user_row():
    fixture = load_issue6611_fixture()
    assert fixture["issue"] == 6611
    artifact_rows = fixture["rows"]
    result = _run_node("success")
    assert [row["role"] for row in result["messages"]].count("user") == 1
    assert [row["role"] for row in result["messages"]] == ["user"]
    assert result["messages"][0]["content"] == artifact_rows[0]["content"]


def test_normal_full_load_adopts_and_clears_regeneration_revision():
    source = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = source.index("function _adoptRegenerationRevision(")
    end = source.index("\n}\n\nasync function _restoreRememberedNewChatDraftSession", start) + 2
    function_source = source[start:end]
    script = f"""
let S={{session:{{session_id:'s1',regeneration_revision:'old'}}}};
{function_source}
_adoptRegenerationRevision({{session_id:'s1',regeneration_revision:'fresh'}});
if(S.session.regeneration_revision!=='fresh') throw new Error('fresh revision was not adopted');
_adoptRegenerationRevision({{session_id:'s1'}});
if(Object.prototype.hasOwnProperty.call(S.session,'regeneration_revision')) throw new Error('stale revision survived replacement');
process.stdout.write('revision adoption ok');
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True)
    assert result.stdout == "revision adoption ok"


def test_typed_rejection_restores_the_complete_local_transcript():
    result = _run_node("reject")
    assert [row["role"] for row in result["messages"]] == ["user", "assistant"]
    assert result["error"] == "typed rejection"
    assert result["busy"][-1] is False
    assert result["attached"] == []


def test_delayed_response_never_attaches_to_a_newly_selected_session():
    result = _run_node("switch")
    assert result["session"]["session_id"] == "s2"
    assert result["messages"] == [{"role": "user", "content": "other session"}]
    assert result["attached"] == []


def test_regenerate_response_has_no_truncate_or_generic_send_reentry():
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    body = source[
        source.index("async function regenerateResponse"):
        source.index("// postProcessRenderedMessages")
    ]
    assert "startRegeneration(initialSid" in body
    assert "/api/session/truncate" not in body
    assert "await send(" not in body


def test_regenerate_response_loads_the_full_session_before_requiring_revision():
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    body = source[
        source.index("async function regenerateResponse"):
        source.index("// postProcessRenderedMessages")
    ]
    assert "if(!S.session || S.busy || !S.session.regeneration_revision) return;" not in body
    assert body.index("await _ensureAllMessagesLoaded()") < body.index(
        "if(!S.session.regeneration_revision)"
    )
