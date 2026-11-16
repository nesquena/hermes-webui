"""Execute real client functions with synthetic transport and deferred draft saves.

These are Node VM behavior tests, not a rendered/login browser or provider run.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Client VM tests do not require an HTTP server."""


@pytest.fixture(autouse=True)
def cleanup_test_sessions():
    """No server sessions are created by this module."""


def _source():
    sessions = (ROOT / "static/sessions.js").read_text()
    ui = (ROOT / "static/ui.js").read_text()
    messages = (ROOT / "static/messages.js").read_text()
    names = (
        "_sessionProfileMismatchFromError",
        "_adoptRegenerationRevision",
        "_currentLoadedRenderableMessageCount",
        "_captureSameSessionForceReloadHint",
        "_clearSameSessionForceReloadHint",
        "_messageReloadLimitForSession",
    )
    functions = [extract_function(sessions, name) for name in names]
    for name in ("loadSession", "_ensureMessagesLoaded", "_ensureAllMessagesLoaded"):
        functions.append(extract_function(sessions, name, "async function"))
    functions.append(extract_function(ui, "regenerateResponse", "async function"))
    functions.append(extract_function(messages, "startRegeneration", "async function"))
    return "\n".join(functions)


HARNESS = r"""
const vm=require('node:vm');
const spec=SPEC;
const rows=Array.from({length:spec.total},(_,i)=>({role:i%2?'assistant':'user',content:'row '+i,timestamp:i+1}));
const cached=rows.slice(spec.offset, spec.initialTotal);
const requests=[], accepted=[], streams=[], statuses=[], renders=[], draftSaves=[];
const noop=()=>{};
let releaseDraft;
const draft=new Promise(resolve=>{releaseDraft=resolve;});
let releaseMessages;
const delayedMessages=new Promise(resolve=>{releaseMessages=resolve;});
let releaseMetadata;
const delayedMetadata=new Promise(resolve=>{releaseMetadata=resolve;});
let metadataReads=0,messageReads=0;
let savedHint,beforeOlderSettles;
const composer={value:'unsaved synthetic draft'};
const msgInner={innerHTML:''};
const stored=new Map();
const c={
  S:{session:{session_id:'A',message_count:spec.initialTotal,regeneration_revision:'old-revision'},messages:cached,toolCalls:[],pendingFiles:[],busy:false,lastUsage:{}},
  INFLIGHT:{}, window:{}, console,
  _loadingSessionId:null, _loadSessionGeneration:0,
  _messagesTruncated:spec.offset>0, _oldestIdx:spec.offset, _loadingOlder:false,
  _sameSessionForceReloadHint:null, _pendingCarryForwardSnapshot:null,
  _INITIAL_MSG_LIMIT:30,_MSG_LIMIT_MAX:500,_msgLimitMax:spec.ceiling||500,
  localStorage:{setItem:(k,v)=>stored.set(k,v),getItem:k=>stored.get(k)||null,removeItem:k=>stored.delete(k)},
  $:id=>id==='msg'?composer:id==='msgInner'?msgInner:null,
  _saveComposerDraftNow:(...args)=>{draftSaves.push(args);return draft;},
  _rearmActiveSessionStream:noop,stopApprovalPolling:noop,startApprovalPolling:noop,hideApprovalCard:noop,
  _updateYoloPill:noop,clearCompressionUi:noop,_sessionVisitHasUnreadState:()=>false,
  _clearDeferredActiveSessionExternalRefresh:noop,
  _resolveSessionModelForDisplaySoon:noop,_acknowledgeSessionVisit:noop,
  _setActiveSessionUrl:noop,_mergePendingSessionMessage:noop,
  _selectLiveRecoveryInflight:()=>null,
  updateQueueBadge:noop,updateSendBtn:noop,setComposerStatus:noop,
  syncTopbar:noop,_deferWorkspaceRefreshForSession:noop,
  _isMessagingSession:()=>false,_hideHandoffHint:noop,
  _syncToolCallsForLoadedMessages:noop,clearLiveToolCards:noop,
  _setSessionViewedCount:noop,_bumpMessagesGeneration:noop,
  renderMessages:()=>renders.push(c.S.messages.map(m=>m.content)),
  setStatus:s=>{if(s)statuses.push(s);}, t:s=>s,
  setTimeout,clearTimeout,
  setBusy:value=>{c.S.busy=value;}, markInflight:noop, removeThinking:noop,
  attachLiveStream:(...args)=>streams.push(args),
};
c.api=async(path,options)=>{
  if(path==='/api/chat/start'){
    const body=JSON.parse(options.body);
    if(body.regenerate!==true)throw new Error('not a regeneration request');
    accepted.push([body.session_id,body.regeneration_revision]);
    if(spec.regenReject)throw new Error('synthetic regeneration rejection');
    return {stream_id:'synthetic-stream',pending_started_at:1};
  }
  requests.push(path);
  const params=new URL(path,'https://synthetic.invalid').searchParams;
  const sid=params.get('session_id');
  if(params.get('messages')==='0'){
    const first=++metadataReads===1;
    if(spec.mode==='interrupted-refresh'&&first&&spec.pendingStage==='metadata'){
      await delayedMetadata;
      if(spec.olderFailure)throw Object.assign(new Error('superseded metadata failure'),{status:503});
    }
    return {session:{session_id:sid,message_count:spec.total,messages:[],payloadOwner:first?'older':'winning'}};
  }
  const firstMessage=++messageReads===1;
  if(spec.delayed||(spec.mode==='interrupted-refresh'&&firstMessage&&spec.pendingStage==='messages'))await delayedMessages;
  if(spec.mode==='interrupted-refresh'&&firstMessage&&spec.pendingStage==='messages'&&spec.olderFailure){
    throw Object.assign(new Error('superseded message failure'),{status:503});
  }
  if(spec.error){const e=new Error('synthetic read rejection');e.status=spec.error;throw e;}
  if(spec.authRedirect)return undefined;
  const raw=params.get('msg_limit');
  const n=(!raw||raw==='all')?rows.length:Math.min(Number(raw),spec.ceiling||500);
  const result=rows.slice(-n);
  const truncated=result.length<rows.length;
  return {session:{session_id:sid,messages:result,message_count:rows.length,
    _messages_truncated:truncated,_messages_offset:rows.length-result.length,_msg_limit_max:spec.ceiling||500,
    ...(!truncated&&!spec.ineligible?{regeneration_revision:'fresh-revision'}:{})}};
};
vm.createContext(c);
vm.runInContext(SOURCE,c);
(async()=>{
 const snapshot=()=>({sid:c.S.session?.session_id,owner:c.S.session?.payloadOwner,
   count:c.S.messages.length,first:c.S.messages[0]?.content,last:c.S.messages.at(-1)?.content,
   offset:c._oldestIdx,truncated:c._messagesTruncated,loadingSid:c._loadingSessionId,
   generation:c._loadSessionGeneration,hint:c._sameSessionForceReloadHint,
   renders:renders.length,revision:c.S.session?.regeneration_revision});
 if(spec.mode==='interrupted-refresh'){
   // Use the full production forced-load entry point, not a pre-seeded hint.
   const pendingA=c.loadSession('A',{force:true,keepStaleUntilLoaded:!!spec.keepStale});
   for(let i=0;i<12&&spec.pendingStage==='messages'&&messageReads===0;i++)await Promise.resolve();
   if(metadataReads!==1||(spec.pendingStage==='messages'&&messageReads!==1))throw new Error('forced refresh did not reach pending transport');
   if(c.S.messages.length!==(spec.keepStale?cached.length:0))throw new Error('forced row-clear seam was not reached');
   savedHint=JSON.parse(JSON.stringify(c._sameSessionForceReloadHint));
   if(savedHint.session_id!=='A'||savedHint.loaded_message_count!==cached.length)throw new Error('forced load did not save the actual window');
   const pendingB=c.loadSession('B');
   if(c._loadingSessionId!=='B'||draftSaves.length!==1||c.S.session.session_id!=='A')throw new Error('pending B draft seam was not reached');
   if(spec.foreignHint)c._sameSessionForceReloadHint={...savedHint,session_id:'B'};
   const winning=c.loadSession(spec.returnSid||'A');
   const releaseOlder=()=>{releaseDraft();releaseMetadata();releaseMessages();};
   if(spec.releaseOrder==='older-first')releaseOlder();
   if(spec.returnSid&&spec.returnSid!=='A')releaseDraft();
   await winning;
   beforeOlderSettles=snapshot();
   releaseOlder();
   await Promise.all([pendingA,pendingB]);
 }else if(spec.mode==='navigation'){
   const pendingB=c.loadSession('B');
   // B waits inside the real draft-save await; A remains on screen.
   if(c._loadingSessionId!=='B'||draftSaves.length!==1)throw new Error('draft seam was not reached');
   await c.loadSession('A');
   releaseDraft();await pendingB;
 }else if(spec.mode==='cold'){
   c.S.session=null;c.S.messages=[];c._messagesTruncated=false;c._oldestIdx=0;
   await c.loadSession('A');
 }else{
   c._loadingSessionId='A';c._loadSessionGeneration=1;
   if(!spec.keepRevision)delete c.S.session.regeneration_revision;
   c._captureSameSessionForceReloadHint('A');
   const pending=c._ensureMessagesLoaded('A',{loadGeneration:1,force:!!spec.force});
   if(spec.delayed){
     // A stale response may not overwrite a new pane or a new same-SID generation.
     c._loadSessionGeneration=2;
     if(spec.switchSid){c._loadingSessionId='B';c.S.session={session_id:'B'};}
     c.S.messages=[{role:'assistant',content:'newer pane'}];
     c._sameSessionForceReloadHint={session_id:spec.switchSid?'B':'A',marker:'newer-owner'};
     releaseMessages();
   }
   try{await pending;}catch(e){statuses.push('read-'+e.status);}
 }
 const beforeRegeneration={count:c.S.messages.length,first:c.S.messages[0]?.content,
   last:c.S.messages.at(-1)?.content,offset:c._oldestIdx,truncated:c._messagesTruncated,
   revision:c.S.session?.regeneration_revision};
 if(spec.regenerate){
   await c.regenerateResponse({closest:()=>({dataset:{msgIdx:String(c.S.messages.length-1)}})});
 }
 process.stdout.write(JSON.stringify({requests,accepted,streams,statuses,renders,draftSaves:draftSaves.length,
   beforeRegeneration,savedHint,beforeOlderSettles,afterOlderSettles:snapshot(),
   activeSid:c.S.session?.session_id,loadingSid:c._loadingSessionId,
   hint:c._sameSessionForceReloadHint,messageCount:c.S.messages.length,loadingOlder:c._loadingOlder}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def _run_case(**options):
    assert shutil.which("node"), "node is required for client behavior tests"
    spec = {"mode": "navigation", "total": 80, "initialTotal": 80, "offset": 0, **options}
    script = HARNESS.replace("SPEC", json.dumps(spec)).replace("SOURCE", json.dumps(_source()))
    result = subprocess.run(
        ["node", "--max-old-space-size=96", "-e", script],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _message_requests(result):
    return [path for path in result["requests"] if "messages=1" in path]


def test_deferred_draft_a_b_a_refetches_authority_and_can_regenerate():
    result = _run_case(regenerate=True)
    assert result["draftSaves"] == 1
    assert result["activeSid"] == "A"
    assert result["loadingSid"] is None
    assert len(result["requests"]) == 2
    assert all("session_id=A" in path for path in result["requests"])
    assert result["accepted"] == [["A", "fresh-revision"]]
    assert result["streams"] == [["A", "synthetic-stream", []]]
    assert result["statuses"] == []
    assert result["hint"] is None


@pytest.mark.parametrize(
    "total,initial,offset,expected_count,expected_offset,limit",
    [(80,80,0,80,0,None), (100,100,40,60,40,60),
     (1000,1000,340,1000,0,None), (82,80,0,82,0,None),
     (102,100,40,62,40,62), (20,20,0,20,0,None)],
)
def test_authority_refetch_preserves_loaded_window_and_external_append(
    total, initial, offset, expected_count, expected_offset, limit,
):
    result = _run_case(total=total, initialTotal=initial, offset=offset)
    fetches = _message_requests(result)
    assert len(fetches) == 1
    if limit is None:
        assert "msg_limit=" not in fetches[0]
    else:
        assert f"msg_limit={limit}&" in fetches[0]
    state = result["beforeRegeneration"]
    assert state["count"] == expected_count
    assert state["offset"] == expected_offset
    assert state["first"] == f"row {expected_offset}"
    assert state["last"] == f"row {total - 1}"
    assert result["hint"] is None


def test_partial_history_gets_full_authority_only_when_regenerating():
    result = _run_case(total=100, initialTotal=100, offset=40, regenerate=True)
    assert result["beforeRegeneration"]["count"] == 60
    assert "revision" not in result["beforeRegeneration"]
    assert result["beforeRegeneration"]["truncated"] is True
    assert len(_message_requests(result)) == 2
    assert "msg_limit=all" in _message_requests(result)[1]
    assert result["accepted"] == [["A", "fresh-revision"]]
    assert result["messageCount"] == 99
    assert result["loadingOlder"] is False


def test_cold_navigation_still_uses_default_tail():
    result = _run_case(mode="cold")
    assert len(_message_requests(result)) == 1
    assert "msg_limit=30&" in _message_requests(result)[0]
    assert result["beforeRegeneration"]["count"] == 30
    assert result["beforeRegeneration"]["offset"] == 50


def test_cached_messages_with_authority_do_not_refetch():
    result = _run_case(mode="loader", keepRevision=True)
    assert result["requests"] == []
    assert result["beforeRegeneration"]["count"] == 80
    assert result["hint"] is None


def test_full_ineligible_response_does_not_loop_or_invent_authority():
    result = _run_case(ineligible=True, regenerate=True)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["count"] == 80
    assert result["accepted"] == []
    assert result["statuses"] == ["regen_failed"]


@pytest.mark.parametrize("switch_sid", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_delayed_message_response_cannot_overwrite_newer_owner(switch_sid, force):
    result = _run_case(mode="loader", delayed=True, switchSid=switch_sid, force=force)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["first"] == "newer pane"
    assert result["hint"]["marker"] == "newer-owner"
    assert result["activeSid"] == ("B" if switch_sid else "A")


@pytest.mark.parametrize("error", [409, 413, 503])
@pytest.mark.parametrize("force", [False, True])
def test_rejected_authority_read_keeps_transcript_and_clears_owned_hint(error, force):
    result = _run_case(mode="loader", error=error, force=force)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["count"] == 80
    assert result["hint"] is None
    assert result["statuses"] == [f"read-{error}"]


def test_auth_redirect_keeps_cached_messages_without_authority():
    result = _run_case(mode="loader", authRedirect=True, force=True)
    assert len(_message_requests(result)) == 1
    assert result["beforeRegeneration"]["count"] == 80
    assert "revision" not in result["beforeRegeneration"]
    assert result["hint"] is None


def test_partial_window_above_live_ceiling_uses_existing_full_escape_hatch():
    result = _run_case(total=100, initialTotal=100, offset=30, ceiling=60)
    assert "msg_limit=" not in _message_requests(result)[0]
    assert result["beforeRegeneration"]["count"] == 100
    assert result["beforeRegeneration"]["offset"] == 0


def test_same_session_force_reload_keeps_existing_full_window_behavior():
    result = _run_case(mode="loader", keepRevision=True, force=True)
    assert len(_message_requests(result)) == 1
    assert "msg_limit=" not in _message_requests(result)[0]
    assert result["beforeRegeneration"]["count"] == 80
    assert result["hint"] is None


def test_rejected_regeneration_restores_full_transcript_after_authority_fetch():
    result = _run_case(regenerate=True, regenReject=True)
    assert result["accepted"] == [["A", "fresh-revision"]]
    assert result["streams"] == []
    assert result["messageCount"] == 80
    assert result["statuses"] == ["regen_failedsynthetic regeneration rejection"]


@pytest.mark.parametrize("pending_stage", ["metadata", "messages"])
@pytest.mark.parametrize("release_order", ["winner-first", "older-first"])
@pytest.mark.parametrize(
    "total,initial,offset,ceiling,expected_count,expected_offset,limit",
    [(80,80,0,500,80,0,None), (100,100,40,500,60,40,60),
     (1000,1000,340,500,1000,0,None), (100,100,30,60,100,0,None),
     (102,100,40,500,62,40,62)],
)
def test_interrupted_forced_refresh_retains_saved_window_after_all_responses(
    pending_stage, release_order, total, initial, offset, ceiling,
    expected_count, expected_offset, limit,
):
    result = _run_case(
        mode="interrupted-refresh", pendingStage=pending_stage, releaseOrder=release_order,
        total=total, initialTotal=initial, offset=offset, ceiling=ceiling,
    )
    assert result["savedHint"]["loaded_message_count"] == initial - offset
    assert result["savedHint"]["truncated"] == (offset > 0)
    state = result["afterOlderSettles"]
    assert state["count"] == expected_count
    assert state["first"] == f"row {expected_offset}"
    assert state["last"] == f"row {total - 1}"
    assert state["offset"] == expected_offset
    assert state["truncated"] == (expected_offset > 0)
    assert state["sid"] == "A"
    assert state["owner"] == "winning"
    assert state["loadingSid"] is None
    assert state["generation"] == 3
    assert state["hint"] is None
    assert state == result["beforeOlderSettles"]
    fetches = _message_requests(result)
    assert len(fetches) == (1 if pending_stage == "metadata" else 2)
    if limit is None:
        assert "msg_limit=" not in fetches[-1]
    else:
        assert f"msg_limit={limit}&" in fetches[-1]
    assert result["draftSaves"] == 1
    assert result["statuses"] == []


@pytest.mark.parametrize("pending_stage", ["metadata", "messages"])
@pytest.mark.parametrize("release_order", ["winner-first", "older-first"])
def test_interrupted_keep_stale_refresh_recaptures_resident_window(pending_stage, release_order):
    result = _run_case(
        mode="interrupted-refresh", pendingStage=pending_stage,
        releaseOrder=release_order, keepStale=True,
    )
    assert result["afterOlderSettles"]["count"] == 80
    assert result["afterOlderSettles"]["first"] == "row 0"
    assert result["afterOlderSettles"] == result["beforeOlderSettles"]
    assert "msg_limit=" not in _message_requests(result)[-1]


@pytest.mark.parametrize("pending_stage", ["metadata", "messages"])
@pytest.mark.parametrize("release_order", ["winner-first", "older-first"])
@pytest.mark.parametrize("isolation", ["foreign-hint", "different-session"])
def test_interrupted_refresh_cannot_apply_another_sessions_saved_width(
    pending_stage, release_order, isolation,
):
    result = _run_case(
        mode="interrupted-refresh", pendingStage=pending_stage, releaseOrder=release_order,
        foreignHint=isolation == "foreign-hint",
        returnSid="C" if isolation == "different-session" else "A",
    )
    state = result["afterOlderSettles"]
    assert state["sid"] == ("C" if isolation == "different-session" else "A")
    assert state["owner"] == "winning"
    assert state["count"] == 30
    assert state["first"] == "row 50"
    assert state["offset"] == 50
    assert state["generation"] == 3
    assert state["loadingSid"] is None
    assert state == result["beforeOlderSettles"]
    assert "msg_limit=30&" in _message_requests(result)[-1]


def test_interrupted_forced_refresh_still_restores_regeneration_authority():
    result = _run_case(
        mode="interrupted-refresh", pendingStage="metadata",
        releaseOrder="winner-first", regenerate=True,
    )
    assert result["beforeOlderSettles"]["count"] == 80
    assert result["beforeRegeneration"]["revision"] == "fresh-revision"
    assert result["accepted"] == [["A", "fresh-revision"]]
    assert result["streams"] == [["A", "synthetic-stream", []]]
    assert result["statuses"] == []


@pytest.mark.parametrize("pending_stage", ["metadata", "messages"])
@pytest.mark.parametrize("release_order", ["winner-first", "older-first"])
def test_superseded_refresh_failure_cannot_clear_winning_window(pending_stage, release_order):
    result = _run_case(
        mode="interrupted-refresh", pendingStage=pending_stage,
        releaseOrder=release_order, olderFailure=True,
    )
    state = result["afterOlderSettles"]
    assert state["count"] == 80
    assert state["first"] == "row 0"
    assert state["offset"] == 0
    assert state["sid"] == "A"
    assert state["owner"] == "winning"
    assert state["loadingSid"] is None
    assert state["hint"] is None
    assert state == result["beforeOlderSettles"]
    assert result["statuses"] == []
