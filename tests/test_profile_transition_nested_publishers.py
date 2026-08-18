"""Behavioral matrix for profile-transition ownership inside nested publishers."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function(source: str, name: str) -> str:
    markers = (f"async function {name}(", f"function {name}(")
    start = next((source.find(marker) for marker in markers if source.find(marker) >= 0), -1)
    assert start >= 0, name
    brace = source.find("{", source.find(")", start))
    depth = 1
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    i = brace + 1
    while depth and i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n": line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/": block_comment = False; i += 1
        elif quote:
            if escaped: escaped = False
            elif ch == "\\": escaped = True
            elif ch == quote: quote = None
        elif ch == "/" and nxt == "/": line_comment = True; i += 1
        elif ch == "/" and nxt == "*": block_comment = True; i += 1
        elif ch in ("'", '"', "`"): quote = ch
        elif ch == "{": depth += 1
        elif ch == "}": depth -= 1
        i += 1
    assert depth == 0, name
    return source[start:i]


OWNER_SOURCE = "var _profileTransitionPostTail=Promise.resolve();\n" + "\n".join(
    _function(UI, name)
    for name in (
        "_beginProfileTransitionOwner", "_acceptProfileTransitionOwner",
        "_isProfileTransitionOwner", "_currentProfileTransitionOwner",
        "_currentProfileTransitionEpoch", "_cancelProfileTransitionOwner",
        "_postProfileTransition",
    )
)


def _run(script: str) -> dict | list:
    result = subprocess.run([NODE], input=script, cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stdout
    return json.loads(lines[-1])


_NEW_SESSION_ENV = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(SESSIONS, 'newSession')}
var _newSessionInFlight=null,_newSessionInFlightOwner=null;
var _messagesTruncated=false,_oldestIdx=0,_activeProject=null;
var NO_PROJECT_FILTER='__all__',_sessionSourceFilter='webui';
global.window={{_defaultModel:null}};
global.document={{createElement:()=>({{dataset:{{}},appendChild(){{}}}})}};
global.$=()=>null;global.t=k=>k;
global._newSessionPendingText=()=> 'creating';global._setNewSessionPending=()=>{{}};
global.updateQueueBadge=()=>{{}};global.clearLiveToolCards=()=>{{}};
global.updateSendBtn=()=>{{}};global.setStatus=()=>{{}};global.setComposerStatus=()=>{{}};
global.syncTopbar=()=>{{}};global.renderMessages=()=>{{}};global._setSessionViewedCount=()=>{{}};
global._rememberNewChatDraftSession=()=>{{}};
global.loadDir=()=>Promise.resolve();global.refreshSessionList=()=>Promise.resolve();
"""


def test_stale_new_session_response_publishes_nothing():
    script = _NEW_SESSION_ENV + r"""
let resolveNew;const events=[];
var S={activeProfile:'A',session:{session_id:'old',workspace:null},messages:[{role:'assistant',content:'old'}],toolCalls:[],_profileSwitchWorkspace:null,_profileDefaultWorkspace:null};
global.localStorage={setItem:()=>events.push('storage')};
global._setActiveSessionUrl=()=>events.push('url');global.startSessionStream=()=>events.push('sse');
global.api=()=>new Promise(resolve=>{resolveNew=resolve;});
(async()=>{
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pending=newSession(false,{transitionOwner:ownerA});
  while(!resolveNew) await Promise.resolve();
  const beforeStale=events.length;
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  resolveNew({session:{session_id:'new-A',messages:[],workspace:null}});
  const result=await pending;
  console.log(JSON.stringify({result,sid:S.session.session_id,after:events.slice(beforeStale)}));
})().catch(e=>{console.error(e);process.exit(1);});
"""
    assert _run(script) == {"result": False, "sid": "old", "after": []}


def test_profile_switch_posts_are_serialized_so_final_owner_sets_cookie_last():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
const requests=[];let cookie='default';
global.api=(path,opts)=>new Promise(resolve=>{{
  const name=JSON.parse(opts.body).name;
  requests.push({{name,finish:()=>{{cookie=name;resolve({{active:name}});}}}});
}});
async function waitFor(count){{for(let i=0;i<200;i++){{if(requests.length===count)return;await Promise.resolve();}}throw new Error('request count '+count+' not reached');}}
(async()=>{{
  const ownerA=_beginProfileTransitionOwner('A','canonical');
  const pendingA=_postProfileTransition(ownerA);await waitFor(1);
  const ownerB=_beginProfileTransitionOwner('B','recovery');
  const pendingB=_postProfileTransition(ownerB);
  await Promise.resolve();
  const beforeFirstSettles=requests.map(r=>r.name);
  requests[0].finish();await waitFor(2);
  requests[1].finish();await Promise.all([pendingA,pendingB]);
  console.log(JSON.stringify({{beforeFirstSettles,all:requests.map(r=>r.name),cookie}}));
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run(script) == {
        "beforeFirstSettles": ["A"],
        "all": ["A", "B"],
        "cookie": "B",
    }


def test_new_session_completion_order_matrix_publishes_only_final_owner():
    script = _NEW_SESSION_ENV + r"""
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r;});return{promise,resolve};}
async function scenario(order){
  const requests=new Map(),published=[];
  global.S={activeProfile:'A',session:{session_id:'old',workspace:null},messages:[{role:'assistant',content:'old'}],toolCalls:[],_profileSwitchWorkspace:null,_profileDefaultWorkspace:null};
  global.localStorage={setItem:()=>{}};
  global._setActiveSessionUrl=sid=>published.push(['url',sid]);
  global.startSessionStream=sid=>published.push(['sse',sid]);
  global.api=(path,opts)=>{const body=JSON.parse(opts.body),d=deferred();requests.set(body.profile,d);return d.promise;};
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pendingA=newSession(false,{transitionOwner:ownerA});
  while(!requests.has('A')) await Promise.resolve();
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  S.activeProfile='B';
  const pendingB=newSession(false,{transitionOwner:ownerB});
  while(!requests.has('B')) await Promise.resolve();
  const resolveA=()=>requests.get('A').resolve({session:{session_id:'new-A',messages:[],workspace:null}});
  const resolveB=()=>requests.get('B').resolve({session:{session_id:'new-B',messages:[],workspace:null}});
  if(order==='A-first'){resolveA();await Promise.resolve();resolveB();}else{resolveB();await Promise.resolve();resolveA();}
  const results=await Promise.all([pendingA,pendingB]);
  return{order,results,sid:S.session.session_id,published};
}
(async()=>console.log(JSON.stringify([await scenario('A-first'),await scenario('B-first')])) )().catch(e=>{console.error(e);process.exit(1);});
"""
    result = _run(script)
    for row in result:
        assert row["results"] == [False, True]
        assert row["sid"] == "new-B"
        assert row["published"] == [["url", "new-B"], ["sse", "new-B"]]


def test_stale_session_list_success_and_rejection_publish_nothing():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(SESSIONS, '_runRenderSessionListRefresh')}
let settle;const events=[];
var _pendingSessionListPayload=null,_cronPollGeneration=0,_contentSearchResults=[];
var _sessionListHasLoadedOnce=true,_renderSessionListGen=1,_profileSwitchListEmbargo=false;
global.$=()=>({{value:''}});global._sessionListQueryString=()=>'';global._isSessionListUserInteracting=()=>false;
global._applySessionListPayload=()=>events.push('apply');global._showSessionListLoadError=()=>events.push('error');
global._requestedSessionSidebarSource=()=> 'webui';global._sessionListExcludeHiddenEnabled=()=>true;global.S={{activeProfile:'A'}};
async function one(mode){{
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  global._loadSidebarSessionListPayload=()=>new Promise((resolve,reject)=>{{settle=mode==='resolve'?resolve:reject;}});
  const pending=_runRenderSessionListRefresh({{transitionOwner:ownerA}},1);
  while(!settle) await Promise.resolve();
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  if(mode==='resolve') settle({{sessData:{{sessions:[]}},projData:{{projects:[]}}}}); else settle(new Error('late A failure'));
  await pending;
}}
(async()=>{{await one('resolve');settle=null;await one('reject');console.log(JSON.stringify(events));}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run(script) == []


def test_session_list_completion_order_matrix_publishes_only_final_owner():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(SESSIONS, '_runRenderSessionListRefresh')}
function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return{{promise,resolve}};}}
async function scenario(order){{
  const requests=[],applied=[];
  var _pendingSessionListPayload=null,_cronPollGeneration=0,_contentSearchResults=[];
  global._sessionListHasLoadedOnce=true;global._renderSessionListGen=2;global._profileSwitchListEmbargo=false;
  global.$=()=>({{value:''}});global._sessionListQueryString=()=>'';global._isSessionListUserInteracting=()=>false;
  global._applySessionListPayload=data=>applied.push(data.tag);global._showSessionListLoadError=()=>applied.push('error');
  global._requestedSessionSidebarSource=()=> 'webui';global._sessionListExcludeHiddenEnabled=()=>true;global.S={{activeProfile:'A'}};
  global._loadSidebarSessionListPayload=()=>{{const d=deferred();requests.push(d);return d.promise;}};
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pendingA=_runRenderSessionListRefresh({{transitionOwner:ownerA}},1);
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  const pendingB=_runRenderSessionListRefresh({{transitionOwner:ownerB}},2);
  while(requests.length<2)await Promise.resolve();
  const resolveA=()=>requests[0].resolve({{sessData:{{tag:'A'}},projData:{{projects:[]}}}});
  const resolveB=()=>requests[1].resolve({{sessData:{{tag:'B'}},projData:{{projects:[]}}}});
  if(order==='A-first'){{resolveA();await Promise.resolve();resolveB();}}else{{resolveB();await Promise.resolve();resolveA();}}
  await Promise.all([pendingA,pendingB]);return{{order,applied}};
}}
(async()=>console.log(JSON.stringify([await scenario('A-first'),await scenario('B-first')])))().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run(script) == [
        {"order": "A-first", "applied": ["B"]},
        {"order": "B-first", "applied": ["B"]},
    ]


def test_stale_workspace_response_cannot_paint_or_cache():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(WORKSPACE, 'loadDir')}
let resolveList;const events=[];var _wsTreeGen=0,_previewDirty=false;
var S={{session:{{session_id:'sid-A',workspace:'/A'}},entries:['old'],_dirCache:{{}},_expandedDirs:new Set()}};
global.api=()=>new Promise(resolve=>{{resolveList=resolve;}});global._workspaceRouteForPath=()=>'/api/list';
global._restoreExpandedDirs=()=>{{}};global.renderBreadcrumb=()=>events.push('crumb');global.renderFileTree=()=>events.push('tree');
global.renderSessionArtifacts=()=>events.push('artifacts');global.clearPreview=()=>events.push('preview');global._refreshGitBadge=()=>events.push('git');
global.$=()=>null;console.warn=()=>{{}};
(async()=>{{
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pending=loadDir('.',{{transitionOwner:ownerA}});while(!resolveList) await Promise.resolve();
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  resolveList({{entries:['A-only']}});await pending;
  console.log(JSON.stringify({{entries:S.entries,events}}));
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run(script) == {"entries": ["old"], "events": []}


def test_stale_workspace_confirm_callback_cannot_clear_new_owner_preview():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(WORKSPACE, 'loadDir')}
let resolveList,resolveConfirm;const events=[];var _wsTreeGen=0,_previewDirty=true;
var S={{session:{{session_id:'shared',workspace:'/A'}},entries:[],_dirCache:{{}},_expandedDirs:new Set()}};
global.api=()=>new Promise(resolve=>{{resolveList=resolve;}});global._workspaceRouteForPath=()=>'/api/list';global._restoreExpandedDirs=()=>{{}};
global.renderBreadcrumb=()=>{{}};global.renderFileTree=()=>{{}};global.renderSessionArtifacts=()=>{{}};global._refreshGitBadge=()=>{{}};global.$=()=>null;
global.t=k=>k;global.showConfirmDialog=()=>new Promise(resolve=>{{resolveConfirm=resolve;}});global.clearPreview=()=>events.push('clear');console.warn=()=>{{}};
(async()=>{{
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pending=loadDir('.',{{transitionOwner:ownerA}});while(!resolveList)await Promise.resolve();
  resolveList({{entries:[]}});while(!resolveConfirm)await Promise.resolve();
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  resolveConfirm(true);await pending;await Promise.resolve();
  console.log(JSON.stringify(events));
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run(script) == []


def test_workspace_completion_order_matrix_paints_only_final_owner():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(WORKSPACE, 'loadDir')}
function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return{{promise,resolve}};}}
async function scenario(order){{
  const requests=[],events=[];global._wsTreeGen=0;global._previewDirty=false;
  global.S={{session:{{session_id:'shared',workspace:'/B'}},entries:['old'],_dirCache:{{}},_expandedDirs:new Set()}};
  global.api=()=>{{const d=deferred();requests.push(d);return d.promise;}};global._workspaceRouteForPath=()=>'/api/list';
  global._restoreExpandedDirs=()=>{{}};global.renderBreadcrumb=()=>events.push('crumb');global.renderFileTree=()=>events.push('tree');
  global.renderSessionArtifacts=()=>events.push('artifacts');global.clearPreview=()=>events.push('preview');global._refreshGitBadge=()=>events.push('git');
  global.$=()=>null;console.warn=()=>{{}};
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pendingA=loadDir('.',{{transitionOwner:ownerA}});
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  const pendingB=loadDir('.',{{transitionOwner:ownerB}});
  while(requests.length<2)await Promise.resolve();
  const resolveA=()=>requests[0].resolve({{entries:['A']}}),resolveB=()=>requests[1].resolve({{entries:['B']}});
  if(order==='A-first'){{resolveA();await Promise.resolve();resolveB();}}else{{resolveB();await Promise.resolve();resolveA();}}
  await Promise.all([pendingA,pendingB]);return{{order,entries:S.entries,events}};
}}
(async()=>console.log(JSON.stringify([await scenario('A-first'),await scenario('B-first')])))().catch(e=>{{console.error(e);process.exit(1);}});
"""
    result = _run(script)
    for row in result:
        assert row["entries"] == ["B"]
        assert row["events"] == ["crumb", "tree", "artifacts", "preview", "git"]


def test_live_model_completion_order_matrix_publishes_only_final_owner():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(UI, '_fetchLiveModels')}
function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return{{promise,resolve}};}}
var _modelDropdownRequestSeq=0;const _liveModelCache={{}},_liveModelFetchPending=new Set(),_liveModelFetchPendingOwner=new Map();
async function scenario(order){{
  delete _liveModelCache.p;_liveModelFetchPending.clear();_liveModelFetchPendingOwner.clear();
  const requests=[],added=[];global.document={{baseURI:'https://example.test/'}};global.location={{href:'https://example.test/'}};
  global.fetch=()=>{{const d=deferred();requests.push(d);return d.promise;}};global._redirectIfUnauth=()=>false;
  global._addLiveModelsToSelect=(provider,models)=>{{added.push(models[0].id);return 1;}};global.syncModelChip=()=>{{}};
  const sel={{}};const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pendingA=_fetchLiveModels('p',sel,null,ownerA);
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');
  const pendingB=_fetchLiveModels('p',sel,null,ownerB);while(requests.length<2)await Promise.resolve();
  const response=id=>({{status:200,json:()=>Promise.resolve({{models:[{{id}}]}})}});
  const resolveA=()=>requests[0].resolve(response('A')),resolveB=()=>requests[1].resolve(response('B'));
  if(order==='A-first'){{resolveA();await Promise.resolve();resolveB();}}else{{resolveB();await Promise.resolve();resolveA();}}
  await Promise.all([pendingA,pendingB]);return{{order,cache:_liveModelCache.p&&_liveModelCache.p[0].id,added,pending:_liveModelFetchPending.has('p')}};
}}
(async()=>console.log(JSON.stringify([await scenario('A-first'),await scenario('B-first')])))().catch(e=>{{console.error(e);process.exit(1);}});
"""
    result = _run(script)
    for row in result:
        assert row["cache"] == "B"
        assert row["added"] == ["B"]
        assert row["pending"] is False


def test_stale_session_metadata_cannot_rebind_to_newer_owner():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(SESSIONS, 'loadSession')}
let resolveMeta,pendingError=null;const events=[];
var _loadSessionGeneration=0,_loadingSessionId=null,_loadingOlder=false,_yoloEnabled=false;
var _messagesTruncated=false,_oldestIdx=0,_pendingCarryForwardSnapshot=null;var INFLIGHT={{}};
var S={{session:{{session_id:'old'}},messages:[{{role:'assistant',content:'old'}}],toolCalls:[],pendingFiles:[],busy:false,activeStreamId:null}};
global.window={{_clearPendingSelections:()=>{{}}}};global._resolveSessionIdFromSidebarLineage=s=>s;
global._rearmActiveSessionStream=()=>events.push('rearm');global._sessionVisitHasUnreadState=()=>false;
global.stopApprovalPolling=()=>{{}};global.hideApprovalCard=()=>{{}};global.stopSessionStream=()=>{{}};global._updateYoloPill=()=>{{}};
global.stopClarifyPolling=()=>{{}};global.hideClarifyCard=()=>{{}};global._saveComposerDraftNow=()=>Promise.resolve();global.closeOtherLiveStreams=()=>{{}};
global._clearSameSessionForceReloadHint=()=>{{}};global._captureSameSessionForceReloadHint=()=>{{}};
global.$=id=>id==='msgInner'?{{innerHTML:''}}:(id==='msg'?{{value:''}}:null);global.api=()=>new Promise(resolve=>{{resolveMeta=resolve;}});
global.localStorage={{setItem:()=>events.push('storage'),removeItem:()=>events.push('remove')}};global.history={{replaceState:()=>events.push('url-clean')}};
global._appRootPath=()=>'/';global._setActiveSessionUrl=()=>events.push('url');global.startSessionStream=()=>events.push('sse');
global.syncTopbar=()=>events.push('topbar');global.renderMessages=()=>events.push('dom');
async function waitForMeta(){{for(let i=0;i<200;i++){{if(resolveMeta||pendingError)return;await Promise.resolve();}}throw new Error('metadata request was not reached');}}
(async()=>{{
  const ownerA=_beginProfileTransitionOwner('A','canonical');_acceptProfileTransitionOwner(ownerA,'A');
  const pending=loadSession('sid-A',{{force:true,transitionOwner:ownerA}}).catch(e=>{{pendingError=e;}});
  await waitForMeta();if(pendingError) throw pendingError;
  const ownerB=_beginProfileTransitionOwner('B','recovery');_acceptProfileTransitionOwner(ownerB,'B');const before=events.length;
  resolveMeta({{session:{{session_id:'sid-A',profile:'A',messages:[]}}}});await pending;
  console.log(JSON.stringify({{sid:S.session.session_id,events:events.slice(before)}}));
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    result = _run(script)
    assert result["sid"] == "old"
    assert result["events"] == ["rearm"]


def test_unaccepted_transition_epoch_cannot_start_session_metadata_load():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(SESSIONS, 'loadSession')}
const calls=[];var _loadSessionGeneration=0,_loadingSessionId=null;
var S={{session:{{session_id:'old'}},messages:[],toolCalls:[]}};
global.api=url=>{{calls.push(url);return Promise.resolve(null);}};
(async()=>{{
  const owner=_beginProfileTransitionOwner('A','canonical');
  const result=await loadSession('sid-A',{{force:true,transitionOwner:owner}});
  console.log(JSON.stringify({{result:result??null,calls,loading:_loadingSessionId,generation:_loadSessionGeneration}}));
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run(script) == {
        "result": None,
        "calls": [],
        "loading": None,
        "generation": 0,
    }


def test_recovery_rejection_cannot_cleanup_newer_owner_state():
    script = f"""
var _profileTransitionOwnerSeq=0,_profileTransitionOwner=null;
{OWNER_SOURCE}
{_function(SESSIONS, '_switchProfileForSessionLoad')}
let rejectPost;const events=[];var S={{activeProfile:'default'}};
global.api=()=>new Promise((resolve,reject)=>{{rejectPost=reject;}});global._invalidateSessionListRenders=()=>{{}};
global._setProfileSwitchListEmbargo=v=>events.push(['embargo',v]);global.showSessionListSkeleton=()=>events.push(['skeleton']);
global.renderSessionListFromCache=()=>events.push(['cache']);global.localStorage={{removeItem(){{}}}};var _sessionListSkeletonActive=true;
(async()=>{{
  const pending=_switchProfileForSessionLoad('A');while(!rejectPost) await Promise.resolve();
  const ownerB=_beginProfileTransitionOwner('B','canonical');_acceptProfileTransitionOwner(ownerB,'B');const before=events.length;
  rejectPost(new Error('late A failure'));const result=await pending;
  console.log(JSON.stringify({{result:result??null,events:events.slice(before),owner:_profileTransitionOwner.profile,skeleton:_sessionListSkeletonActive}}));
}})().catch(e=>{{console.error(e);process.exit(1);}});
"""
    assert _run(script) == {"result": None, "events": [], "owner": "B", "skeleton": True}
