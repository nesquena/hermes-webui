"""Uncertain Gateway steer + leftover across New Chat and durable drafts.

Composes the real _trySteer / _steerReconcileLeftover / pending_steer_leftover
handler / newSession() / composer-draft helpers against a fake HTTP layer whose
/api/session/draft mirrors the server's compare-and-clear contract (the route
itself is covered by test_draft_compare_and_clear_route below).
"""
import json
import subprocess
import urllib.request
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _slice(src, start, end):
    return src[src.index(start):src.index(end)]


def _run(scenario):
    cmd = (ROOT / 'static/commands.js').read_text(encoding='utf-8')
    ses = (ROOT / 'static/sessions.js').read_text(encoding='utf-8')
    msg = (ROOT / 'static/messages.js').read_text(encoding='utf-8')
    parts = [
        _slice(ses, 'let _draftSaveTimer = null;', 'function _composerDraftFileSignature('),
        _slice(ses, 'function _composerDraftFileSignature(', 'function _profileMatchesActiveProfile('),
        _slice(ses, 'function _isRestorableNewChatDraftSession(', 'function _adoptRegenerationRevision('),
        _slice(ses, 'function _saveComposerDraft(', 'const SESSION_VIEWED_COUNTS_KEY'),
        _slice(ses, 'let _newSessionInFlight=null;', 'const _emptyComposerModelOverrideHost'),
        _slice(ses, 'function _setNewSessionPending(', '// #2971 (Greptile P1'),
        _slice(cmd, 'let _steerUploadCache = null;', 'function _steerFilesSignature('),
        _slice(cmd, 'function _steerFallbackIsDeadRun(', 'function _steerSetComposerStatusForOwner('),
        _slice(cmd, 'function _steerRestoreText(', 'function _steerIndicatorText('),
        _slice(cmd, 'async function _steerPersistDraftForOwner(', '// #5459 gate'),
        _slice(cmd, 'async function _trySteer(', 'async function cmdTitle('),
    ]
    leftover = _slice(msg, "source.addEventListener('pending_steer_leftover',", "source.addEventListener('compressing',")
    leftover = leftover[leftover.index('e=>{'):leftover.rindex('});')] + '}'
    harness = r'''
const assert=require('node:assert/strict');
const store={localStorage:{}};
const localStorage={getItem:k=>store.localStorage[k]??null,setItem:(k,v)=>{store.localStorage[k]=String(v)},removeItem:k=>{delete store.localStorage[k]}};
const window={};const NO_PROJECT_FILTER='__none__';let _activeProject=null,_sessionSourceFilter='all',_messagesTruncated=false,_oldestIdx=0;
const inp={value:''},el={disabled:false,setAttribute(){},focus(){}};
const document={getElementById:()=>null};
const $=id=>id==='msg'?inp:el,t=x=>x,showToast=()=>{};
const S={session:null,activeStreamId:null,pendingFiles:[],messages:[],activeProfile:'default'};
const drafts={},queues={};let nextSid=0;
const sessions={};
async function api(path,opts){
  const body=JSON.parse(opts&&opts.body||'{}');
  await null;
  if(path==='/api/session/new'){const sid='B'+(++nextSid);sessions[sid]={session_id:sid,messages:[],composer_draft:{}};return {session:{...sessions[sid]}};}
  if(path==='/api/session/draft'){  // same contract as api/routes.py
    const cur=drafts[body.session_id]||{text:'',files:[]};
    if(typeof body.if_text==='string'&&((cur.text||'')!==body.if_text||(Array.isArray(body.if_files)&&JSON.stringify(cur.files||[])!==JSON.stringify(body.if_files))))
      return {ok:true,draft:cur,unchanged:true,mismatch:true};
    const next={...cur};if(body.text!==undefined)next.text=body.text;if(body.files!==undefined)next.files=body.files;
    drafts[body.session_id]=next;return {ok:true,draft:next};
  }
  if(path==='/api/chat/steer'){await steerGate;return {accepted:false,fallback:'gateway_steer_uncertain',stream_id:'runA'};}
  throw new Error('unexpected '+path);
}
let releaseSteer;const steerGate=new Promise(r=>releaseSteer=r);
const noop=()=>{};
const updateQueueBadge=noop,clearLiveToolCards=noop,updateSendBtn=noop,setStatus=noop,setComposerStatus=noop,syncTopbar=noop,renderMessages=noop,
  _setActiveSessionUrl=noop,_setSessionViewedCount=noop,_steerTextWithPendingFiles=async x=>x,_steerFailureMessageKey=x=>x,_showSteerRecovery=noop,
  loadDir=async()=>{},startSessionStream=noop,_applyToAnchor=noop,_chatPayloadModelState=()=>({model:'m',model_provider:'p'});
const _profileMatchesActiveProfile=()=>true;
function _steerOwnerIsCurrent(sid){return !!(S.session&&S.session.session_id===sid);}
function queueSessionMessage(sid,p){(queues[sid]=queues[sid]||[]).push(p.text);}
const activeSid='A';
const leftover=(LEFTOVER_FN);
function emitLeftover(text){leftover({data:JSON.stringify({session_id:'A',text})});}
// Simulated loadSession(A): flush current composer, then show A's saved draft.
async function returnToA(){
  if(S.session)await _saveComposerDraftNow(S.session.session_id,inp.value,[]);
  S.session={session_id:'A',active_stream_id:null,composer_draft:{...(drafts.A||{})}};
  inp.value=(drafts.A&&drafts.A.text)||'';
}
const tick=()=>new Promise(r=>setTimeout(r,0));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
// Viewing A with a running Gateway stream; Steer submit clears the composer first.
S.session={session_id:'A',active_stream_id:'runA',messages:[]};S.activeStreamId='runA';S.busy=true;
function submitSteer(){inp.value='';return _trySteer('guidance',false);}
function finalChecks(){
  assert.deepEqual(queues.A,['guidance'],'exactly one delivery queued');
  assert.equal(_steerUncertainBySid.size,0,'settled record removed');
}
'''.replace('LEFTOVER_FN', leftover)
    src = '\n'.join(parts) + harness + scenario
    subprocess.run(['node', '-e', '(async()=>{' + src + '\n})().catch(e=>{console.error(e);process.exit(1)});'],
                   check=True, timeout=20)


def test_new_chat_before_uncertain_reply_does_not_leave_restorable_guidance():
    _run(r'''
const p=submitSteer();await tick();
await newSession();assert.equal(S.session.session_id,'B1');
inp.value='b draft';
releaseSteer();assert.equal(await p,false);
assert.equal(drafts.A.text,'guidance','offscreen owner draft persisted');
emitLeftover('guidance');await tick();await tick();
assert.equal(drafts.A.text,'','offscreen owner draft retired');
assert.equal(inp.value,'b draft','B composer untouched');
await returnToA();assert.equal(inp.value,'','no restored guidance on return to A');
finalChecks();
''')


def test_restored_and_saved_a_then_new_chat_then_leftover():
    _run(r'''
const p=submitSteer();releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'guidance','restored on A');
_saveComposerDraft('A',inp.value,[]);await sleep(450);
assert.equal(drafts.A.text,'guidance');
await newSession();inp.value='b draft';_saveComposerDraft(S.session.session_id,inp.value,[]);await sleep(450);
emitLeftover('guidance');await tick();await tick();
assert.equal(drafts.A.text,'');assert.equal(drafts.B1.text,'b draft');assert.equal(inp.value,'b draft');
await returnToA();assert.equal(inp.value,'');
finalChecks();
''')


def test_leftover_before_reply_keeps_newer_durable_draft():
    _run(r'''
const p=submitSteer();await tick();
inp.value='newer';_saveComposerDraft('A',inp.value,[]);await sleep(450);
emitLeftover('guidance');
releaseSteer();assert.equal(await p,true);await tick();await tick();
assert.equal(inp.value,'newer');assert.equal(drafts.A.text,'newer','newer saved draft preserved');
finalChecks();
''')


def test_leftover_before_reply_without_newer_draft_clears_owner_draft():
    _run(r'''
drafts.A={text:'guid',files:[]};  // debounced prefix saved before submit
S.session.composer_draft={...drafts.A};
const p=submitSteer();await tick();
emitLeftover('guidance');
releaseSteer();assert.equal(await p,true);await tick();await tick();
assert.equal(inp.value,'');assert.equal(drafts.A.text,'');
finalChecks();
''')


def test_reply_then_newer_draft_then_leftover_keeps_newer():
    _run(r'''
const p=submitSteer();releaseSteer();assert.equal(await p,false);
inp.value='newer';_saveComposerDraft('A',inp.value,[]);await sleep(450);
emitLeftover('guidance');await tick();await tick();
assert.equal(inp.value,'newer');assert.equal(drafts.A.text,'newer');
finalChecks();
''')


def test_run_end_without_leftover_drops_settled_record():
    _run(r'''
const p=submitSteer();releaseSteer();assert.equal(await p,false);
assert.equal(_steerUncertainBySid.size,1);
_steerSettleTerminal('A');
assert.equal(_steerUncertainBySid.size,0);
assert.equal(inp.value,'guidance','unconfirmed steer stays restored');
''')


def test_terminal_handlers_settle_uncertain_steer():
    js = (ROOT / 'static/messages.js').read_text(encoding='utf-8')
    for ev in ('done', 'apperror', 'cancel'):
        block = js[js.index("source.addEventListener('%s',e=>{" % ev):]
        block = block[:block.index('\n    });')]
        assert "_steerSettleTerminal(activeSid)" in block, ev


def test_draft_compare_and_clear_route():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from conftest import TEST_BASE

    def post(path, body):
        req = urllib.request.Request(TEST_BASE + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    sid = post('/api/session/new', {})['session']['session_id']
    post('/api/session/draft', {'session_id': sid, 'text': 'newer', 'files': []})
    r = post('/api/session/draft', {'session_id': sid, 'text': '', 'files': [], 'if_text': 'guidance', 'if_files': []})
    assert r.get('mismatch') is True and r['draft']['text'] == 'newer'
    r = post('/api/session/draft', {'session_id': sid, 'text': '', 'files': [], 'if_text': 'newer', 'if_files': []})
    assert not r.get('mismatch') and r['draft']['text'] == ''
