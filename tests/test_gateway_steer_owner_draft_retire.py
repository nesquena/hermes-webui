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
    if(typeof body.if_text==='string')await casGate;
    const cur=drafts[body.session_id]||{text:'',files:[]};
    if(typeof body.if_text==='string'&&((cur.text||'')!==body.if_text||(Array.isArray(body.if_files)&&JSON.stringify(cur.files||[])!==JSON.stringify(body.if_files))))
      return {ok:true,draft:cur,unchanged:true,mismatch:true};
    const next={...cur};if(body.text!==undefined)next.text=body.text;if(body.files!==undefined)next.files=body.files;
    drafts[body.session_id]=next;return {ok:true,draft:next};
  }
  if(path==='/api/chat/steer'){if(steerReplies.length)return steerReplies.shift();await steerGate;return {accepted:false,fallback:'gateway_steer_uncertain',stream_id:'runA'};}
  throw new Error('unexpected '+path);
}
const steerReplies=[];let releaseSteer;const steerGate=new Promise(r=>releaseSteer=r);
let casGate=Promise.resolve();let _loadingSessionId=null;
const noop=()=>{};
const updateQueueBadge=noop,clearLiveToolCards=noop,updateSendBtn=noop,setStatus=noop,setComposerStatus=noop,syncTopbar=noop,renderMessages=noop,
  _setActiveSessionUrl=noop,_setSessionViewedCount=noop,_steerTextWithPendingFiles=async(x,_s,f)=>x||(f&&f.length?'[files]':''),_steerFailureMessageKey=x=>x,_showSteerRecovery=noop,
  loadDir=async()=>{},startSessionStream=noop,_applyToAnchor=noop,_chatPayloadModelState=()=>({model:'m',model_provider:'p'});
const _profileMatchesActiveProfile=()=>true,_showSteerIndicator=noop,_steerIndicatorText=x=>x;
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


def test_return_to_owner_during_delayed_compare_and_clear_does_not_restore():
    _run(r'''
const p=submitSteer();await tick();
await newSession();releaseSteer();assert.equal(await p,false);
assert.equal(drafts.A.text,'guidance');
let releaseCas;casGate=new Promise(r=>releaseCas=r);
emitLeftover('guidance');await tick();
S.session={session_id:'A',active_stream_id:null,composer_draft:{...drafts.A}};
_restoreComposerDraft(drafts.A,'A');
assert.equal(inp.value,'','retiring draft not restored while compare-and-clear is pending');
releaseCas();await tick();await tick();await tick();
assert.equal(drafts.A.text,'');assert.equal(inp.value,'');
assert.equal(_steerRetiringBySid.size,0);
finalChecks();
''')


def test_newer_text_typed_during_uncertain_request_is_kept():
    _run(r'''
const p=submitSteer();await tick();
inp.value='newer';
releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'newer','newer composer text not overwritten');
emitLeftover('guidance');await tick();await tick();
assert.equal(inp.value,'newer');
finalChecks();
''')


def test_offscreen_uncertain_restore_keeps_newer_saved_draft():
    _run(r'''
const p=submitSteer();await tick();
_saveComposerDraftNow('A','newer',[]);await tick();
await newSession();
releaseSteer();assert.equal(await p,false);
assert.equal(drafts.A.text,'newer','newer saved owner draft not overwritten');
emitLeftover('guidance');await tick();await tick();
assert.equal(drafts.A.text,'newer');
finalChecks();
''')


def test_clear_owner_draft_keeps_other_sessions_debounced_save():
    _run(r'''
_saveComposerDraft('B9','b draft',[]);
_clearComposerDraft('A','guidance',[]);
await sleep(450);
assert.equal(drafts.B9.text,'b draft');
''')


ATT = "const att={name:'a.txt',size:1,type:'text/plain'},newer={name:'new.txt',size:2,type:'text/plain'};"


def test_attachment_only_retiring_draft_restores_empty_without_recursion():
    _run(ATT + r'''
S.pendingFiles=[att];
const p=_trySteer('',false);await tick();
await newSession();releaseSteer();assert.equal(await p,false);
assert.equal(drafts.A.text,'');assert.equal(drafts.A.files.length,1,'attachment-only owner draft persisted');
let releaseCas;casGate=new Promise(r=>releaseCas=r);
emitLeftover('[files]');await tick();
S.session={session_id:'A',active_stream_id:null,composer_draft:{...drafts.A}};inp.value='x';
assert.doesNotThrow(()=>_restoreComposerDraft(drafts.A,'A'));
assert.equal(inp.value,'','retiring attachment-only draft shown empty');
releaseCas();await tick();await tick();await tick();
assert.deepEqual(drafts.A,{text:'',files:[]});assert.equal(_steerRetiringBySid.size,0);
''')


def test_same_text_newer_files_draft_is_not_hidden_or_cleared():
    _run(ATT + r'''
const p=submitSteer();await tick();
await newSession();releaseSteer();assert.equal(await p,false);
assert.equal(drafts.A.text,'guidance');
let releaseCas;casGate=new Promise(r=>releaseCas=r);
emitLeftover('guidance');await tick();
// While compare-and-clear is pending, a newer draft with the same text and a file wins.
drafts.A={text:'guidance',files:_composerDraftFilesForPersist([newer])};
S.session={session_id:'A',active_stream_id:null,composer_draft:{...drafts.A}};S.pendingFiles=[newer];
_restoreComposerDraft(drafts.A,'A');
assert.equal(inp.value,'guidance','newer same-text draft with other files restored while retiring');
releaseCas();await tick();await tick();await tick();
assert.equal(drafts.A.text,'guidance');assert.equal(drafts.A.files.length,1,'server kept newer draft');
assert.equal(inp.value,'guidance','settlement left the newer visible draft');assert.deepEqual(S.pendingFiles,[newer]);
_restoreComposerDraft(drafts.A,'A');assert.equal(inp.value,'guidance','still restorable after settle');
''')


def test_same_text_retyped_on_owner_after_retire_starts_survives():
    _run(r'''
const p=submitSteer();releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'guidance');
let releaseCas;casGate=new Promise(r=>releaseCas=r);
emitLeftover('guidance');await tick();
assert.equal(inp.value,'','owned visible draft retired');
inp.value='guidance';_saveComposerDraft('A',inp.value,[]);   // user deliberately re-types it
_restoreComposerDraft({text:'guidance',files:[]},'A');
assert.equal(inp.value,'guidance','re-typed draft is newer, not retiring');
releaseCas();await tick();await tick();await tick();
assert.equal(inp.value,'guidance','settlement does not clear the re-typed draft');
await sleep(450);assert.equal(drafts.A.text,'guidance','re-typed debounced save still lands');
''')


def test_debounced_save_of_retired_text_does_not_write_it_back():
    _run(r'''
const p=submitSteer();releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'guidance');
_saveComposerDraft('A',inp.value,[]);   // debounced save of the restored text, not yet sent
emitLeftover('guidance');await tick();await tick();await tick();
assert.equal(drafts.A&&drafts.A.text||'','');
await sleep(450);
assert.equal(drafts.A&&drafts.A.text||'','','pending save did not resurrect retired guidance');
await returnToA();assert.equal(inp.value,'');
''')


def test_interior_line_of_accepted_multiline_steer_keeps_uncertain_recovery():
    _run(r'''
steerReplies.push({accepted:true,fallback:null,stream_id:'runA'});
inp.value='';assert.equal(await _trySteer('accepted intro\nguidance\naccepted outro',false),true);
const p=submitSteer();releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'guidance');
emitLeftover('accepted intro\nguidance\naccepted outro');await tick();await tick();
assert.equal(inp.value,'guidance','recovery draft kept: leftover only proves the accepted steer');
assert.equal(_steerUncertainBySid.size,1);
''')


def test_uncertain_multiline_steer_not_matched_by_two_separate_accepted_steers():
    _run(r'''
steerReplies.push({accepted:true,fallback:null,stream_id:'runA'},{accepted:true,fallback:null,stream_id:'runA'});
assert.equal(await _trySteer('alpha',false),true);assert.equal(await _trySteer('beta',false),true);
inp.value='';const p=_trySteer('alpha\nbeta',false);releaseSteer();assert.equal(await p,false);
emitLeftover('alpha\nbeta');await tick();await tick();
assert.equal(inp.value,'alpha\nbeta','separate accepted steers are not proof for the uncertain one');
// Positive control: the uncertain steer joined after the accepted ones still reconciles.
emitLeftover('alpha\nbeta\nalpha\nbeta');await tick();await tick();
assert.equal(inp.value,'');assert.equal(_steerUncertainBySid.size,0);
''')


def test_consumed_substring_then_pending_multiline_keeps_uncertain_prefix():
    # Review 5441028456 item 1: 'guidance' was accepted and consumed, the pending
    # multiline accepted body is the whole leftover, so 'accepted intro' is unproven.
    _run(r"""
steerReplies.push({accepted:true,fallback:null,stream_id:'runA'},{accepted:true,fallback:null,stream_id:'runA'});
assert.equal(await _trySteer('guidance',false),true);
assert.equal(await _trySteer('accepted intro\nguidance\naccepted outro',false),true);
inp.value='';const p=_trySteer('accepted intro',false);releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'accepted intro');
emitLeftover('accepted intro\nguidance\naccepted outro');await tick();await tick();
assert.equal(inp.value,'accepted intro','unproven uncertain steer keeps its recovery draft');
assert.equal(_steerUncertainBySid.size,1);
""")


def test_uncertain_multiline_found_after_overlapping_accepted_steer():
    # Greptile 4205694547: uncertain 'a\nb' + accepted 'a' -> leftover 'a\nb\na'.
    _run(r"""
inp.value='';const p=_trySteer('a\nb',false);releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'a\nb');
steerReplies.push({accepted:true,fallback:null,stream_id:'runA'});
inp.value='a\nb';assert.equal(await _trySteer('a',false),true);
emitLeftover('a\nb\na');await tick();await tick();await tick();
assert.equal(inp.value,'','delivered uncertain steer retired');assert.equal(_steerUncertainBySid.size,0);
""")


def test_later_uncertain_steer_does_not_replace_earlier_recovery_record():
    # Greptile 4205443403: a second uncertain steer must not drop the first's record.
    _run(r"""
const p1=submitSteer();releaseSteer();assert.equal(await p1,false);
assert.equal(inp.value,'guidance');
// Second steer sent while the first's recovery draft is still shown.
assert.equal(await _trySteer('second',false),false);
assert.equal(inp.value,'guidance');assert.equal(_steerUncertainBySid.get('A').size,2);
emitLeftover('guidance');await tick();await tick();await tick();
assert.equal(inp.value,'','first steer recovery retired');
assert.equal(_steerUncertainBySid.get('A').size,1,'second steer still tracked');
emitLeftover('second');await tick();await tick();
assert.equal(_steerUncertainBySid.size,0);
assert.deepEqual(queues.A,['guidance','second']);
""")


def test_erase_and_retype_identical_draft_before_leftover_keeps_pending_save():
    # Review 5441028456 item 2: the retyped draft is newer, not the restored one.
    _run(r"""
const p=submitSteer();releaseSteer();assert.equal(await p,false);
assert.equal(inp.value,'guidance');
inp.value='';_saveComposerDraft('A',inp.value,[]);          // user clears (input event)
inp.value='guidance';_saveComposerDraft('A',inp.value,[]);  // and deliberately retypes it
emitLeftover('guidance');await tick();await tick();await tick();
assert.equal(inp.value,'guidance','newer identical draft stays visible');
await sleep(450);
assert.equal(drafts.A&&drafts.A.text,'guidance','newer identical draft save still lands');
finalChecks();
""")
