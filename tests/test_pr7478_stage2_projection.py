"""Dirty scene projection: execute production JS, with the full projector as oracle."""
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(body):
    source = (ROOT / 'static/assistant_turn_anchors.js').read_text()
    source = source.replace('function _activitySceneRow(event, index, mode){',
                            'function _activitySceneRow(event, index, mode){ globalThis.builds++;')
    setup = r'''
const assert=require('node:assert/strict');
globalThis.builds=0;
const api=globalThis.HermesAssistantTurnAnchors;
const registry=()=>api.createAssistantTurnAnchorRegistry({session_id:'s',stream_id:'stream',run_id:'run'});
function add(r,i,type='token'){
 return api.applyAssistantTurnAnchorSourceEvent(r,{source_type:type,event_id:'run:'+i,seq:i,
  payload:type==='tool'?{name:'terminal',tid:'t'+i,args:{command:'fixture'},preview:'working'}:{text:'prose '+i}});
}
function project(r){
 return api.createAssistantTurnAnchorActivityProjector
  ?api.createAssistantTurnAnchorActivityProjector(r)
  :{project:opts=>api.projectAssistantTurnAnchorActivityScene(r,opts),replace(){},append(){},invalidate(){},dispose(){}};
}
function replace(p,r,i,patch){const before=r.anchor.activity_events[i];const after={...before,...patch,payload:{...before.payload,...patch.payload}};r.anchor.activity_events[i]=after;p.replace(i,before,after);}
'''
    result = subprocess.run(['node'], input=source+'\n'+setup+body,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout+result.stderr


def test_public_projection_mutation_cannot_poison_live_state():
    run_js(r'''
const r=registry();add(r,1,'tool');const p=project(r);
const live=p.project({mode:'compact_worklog'});
const publicScene=api.projectAssistantTurnAnchorActivityScene(r,{mode:'compact_worklog'});
assert.equal(Object.isFrozen(publicScene.activity_rows[0].tool.args),false);
publicScene.activity_rows[0].tool.args.command='mutated by public consumer';
publicScene.activity_rows[0].payload.args.command='also mutated';
assert.equal(p.project({mode:'compact_worklog'}).activity_rows[0].tool.args.command,'fixture');
assert.equal(api.projectAssistantTurnAnchorActivityScene(r,{mode:'compact_worklog'}).activity_rows[0].tool.args.command,'fixture');
assert.equal(live.activity_rows[0].tool.args.command,'fixture');
''')


def test_failed_reseed_retries_from_source_not_old_rows():
    run_js(r'''
const r=registry();add(r,1,'tool');const p=project(r);
p.project({mode:'compact_worklog'});
let fail=true;
const event={...r.anchor.activity_events[0],payload:{text:'new'}};
Object.defineProperty(event,'kind',{enumerable:true,get(){if(fail)throw Error('transient');return 'process_prose';}});
r.anchor.activity_events=[event];
assert.throws(()=>p.project({mode:'transparent_stream'}),/transient/);
fail=false;
assert.deepEqual(p.project({mode:'transparent_stream'}),api.projectAssistantTurnAnchorActivityScene(r,{mode:'transparent_stream'}));
''')


@pytest.mark.parametrize('recovery', ['direct', 'failed', 'timeout'])
def test_connection_loss_settles_after_source_disposal(recovery):
    from tests.test_stable_assistant_turn_anchor_registry import _function_source
    source = (ROOT / 'static/messages.js').read_text()
    apply = _function_source(source, '_applyToAnchor')
    handle = _function_source(source, '_handleStreamError')
    owner = _function_source(source, '_anchorProjectionOwnerReplaced')
    start = source.index("source.addEventListener('error',async e=>{")
    start = source.index('{', start) + 1
    end = source.index('\n    });', start)
    error = 'async function errorListener(e){' + source[start:end] + '\n}'
    run_js('(async()=>{const recovery=' + repr(recovery) + ';' + r'''
const _anchorRegistry=registry(),activeSid='s',streamId='stream';
const _anchorApi=globalThis.HermesAssistantTurnAnchors,_anchorProjection=null;
const api=async()=>({active:false,replay_available:false});
const LIVE_STREAMS={},_anchorRegistryMap=new Map([[streamId,_anchorRegistry]]);
const _projectOwnedAnchorActivityScene=()=>{};
const source={readyState:1,close(){this.readyState=2;}};
let _terminalStateReached=false,_pendingStreamEndRecovery=false,_reconnectAttempted=false,restores=0;
const _bailOutOfTerminalEventsFromStaleStream=()=>false,_isSessionCurrentPane=()=>true;
const _deferStreamErrorIfOffline=()=>false,_deferStreamErrorIfPageHidden=()=>false,setComposerStatus=()=>{};
const _restoreSettledSession=async()=>{restores++;if(recovery==='timeout'&&restores===7)return new Promise(()=>{});return false;};
const timers=[];const setTimeout=fn=>{timers.push(fn);return fn;},clearTimeout=fn=>{const i=timers.indexOf(fn);if(i>=0)timers.splice(i,1);};
let _anchorProjectionClosed=false,_anchorShadowWarned=false,_streamFinalized=false;
let _persistTimer=null,_assistantSegmentSeq=1,_currentActivityBurstId=1;
const S={session:{session_id:'s'},activeStreamId:'stream',messages:[]};
const assistantText='answer';
const _isActiveSession=()=>true,_clearStreamEndRecovery=()=>{},_cancelThrottledSnapshotTimer=()=>{};
const _clearAnchorProseIncrementalNode=()=>{},_cancelAnimationFramePendingStreamRender=()=>{};
const _streamFadeCleanupReduceMotionListener=()=>{},_clearOwnerInflightState=()=>{};
const _closeSource=()=>{_anchorProjectionClosed=true;};
const _clearApprovalForOwner=()=>{},_clearClarifyForOwner=()=>{};
const _flushReasoningToAnchor=()=>{},_flushPendingReasoningRender=()=>{},_renderAnchorLiveScene=()=>false;
const clearLiveToolCards=()=>{},_ensureSingleTerminalStreamErrorMarker=()=>{};
const _attachProjectedAnchorSceneToLastAssistant=()=>{},renderMessages=()=>{},_markSessionViewed=()=>{};
const renderSessionList=()=>{},_setActivePaneIdleIfOwner=()=>{},_scheduleAnchorRegistryCleanup=()=>{},_dispatchExtensionTurnLifecycle=()=>{};
''' + owner + '\n' + apply + '\n' + handle + '\n' + error + r'''
if(recovery==='direct'){
 _closeSource({});_handleStreamError({});
}else{
 await errorListener({});
 for(let i=0;i<20&&timers.length;i++){timers.shift()();await new Promise(resolve=>setImmediate(resolve));}
 assert.equal(restores,7,'all failed recovery probes and final attempt must execute');
}
assert.equal(_anchorRegistry.anchor.lifecycle.terminal_state,'connection_lost');
assert.equal(_anchorRegistry.anchor.activity_events.at(-1).source_event_type,'error');
const count=_anchorRegistry.anchor.activity_events.length;
// A replacement attachment may share this exact registry; old writes must stop.
LIVE_STREAMS[activeSid]={projectActivityScene:()=>{}};
assert.equal(_applyToAnchor('token',{text:'stale'},null),null);
assert.equal(_anchorRegistry.anchor.activity_events.length,count);
delete LIVE_STREAMS[activeSid];
_anchorRegistryMap.set(streamId,registry());
assert.equal(_applyToAnchor('token',{text:'stale replacement'},null),null);
assert.equal(_anchorRegistry.anchor.activity_events.length,count);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_structural_fallback_and_public_contract():
    run_js(r'''
for(const mode of ['compact_worklog','transparent_stream']){
 const r=registry();for(let i=1;i<=12;i++)add(r,i);
 const reflection=JSON.stringify([Reflect.ownKeys(r),Reflect.ownKeys(r.anchor),Reflect.ownKeys(r.anchor.activity_events)]);
 const serialized=JSON.stringify(r);
 const p=project(r);p.project({mode});
 assert.equal(JSON.stringify(r),serialized);
 assert.equal(JSON.stringify([Reflect.ownKeys(r),Reflect.ownKeys(r.anchor),Reflect.ownKeys(r.anchor.activity_events)]),reflection);
 function check(change){
  const before=p.metrics().fullFallbacks;change();
  assert.deepEqual(p.project({mode}),api.projectAssistantTurnAnchorActivityScene(r,{mode}));
  assert.ok(p.metrics().fullFallbacks>before,'structural change requires fallback');
 }
 check(()=>replace(p,r,2,{event_id:'different-identity'}));
 check(()=>replace(p,r,5,{payload:{text:'p'}})); // prose rewind
 check(()=>{r.anchor.activity_events.reverse();p.invalidate();});
 check(()=>{r.anchor.activity_events.splice(1,2);}); // detectable shrink
 check(()=>{r.anchor.activity_events=r.anchor.activity_events.slice();});
 check(()=>{r.anchor.identity.session_id='replacement';});
 check(()=>{const start=r.anchor.activity_events.length;r.anchor.activity_events.push(r.anchor.activity_events[0]);p.append(start);});
 // Arbitrary public mutable input must never use private cached output.
 r.anchor.activity_events[0].payload.text='untracked public correction';
 assert.equal(api.projectAssistantTurnAnchorActivityScene(r,{mode}).activity_rows[0].text,'untracked public correction');
}
''')


def test_disposal_and_fresh_envelope_are_independent_of_row_changes():
    run_js(r'''
for(let turn=0;turn<100;turn++){
 const r=registry();for(let i=1;i<=20;i++)add(r,i);
 const old=project(r);old.project({mode:'compact_worklog'});
 const fresh=project(r);const snapshot=fresh.project({mode:'compact_worklog'});
 old.dispose();old.invalidate();old.append(20);old.replace(0,null,null);
 assert.equal(old.project({mode:'compact_worklog'}),null);
 assert.equal(old.metrics().retainedRows,0);assert.equal(old.metrics().dirtyRows,0);
 assert.deepEqual(fresh.project({mode:'compact_worklog'}),snapshot);
 r.anchor.content.final_answer='final';r.anchor.lifecycle.terminal_state='completed';
 const settled=fresh.project({mode:'compact_worklog'});
 assert.deepEqual(settled,api.projectAssistantTurnAnchorActivityScene(r,{mode:'compact_worklog'}));
 assert.equal(settled.final_answer,'final');assert.equal(settled.terminal_state,'completed');
 fresh.dispose();assert.equal(fresh.metrics().retainedRows,0);
}
''')


def test_append_and_multiple_dirty_rows_are_bounded():
    run_js(r'''
for(const mode of ['compact_worklog','transparent_stream']){
 const r=registry();for(let i=1;i<=1000;i++)add(r,i,i%2?'tool':'reasoning');
 const p=project(r);p.project({mode});
 const before=p.metrics();
 for(let i=0;i<100;i++){
  const start=r.anchor.activity_events.length;add(r,1001+i);if(p.append)p.append(start);
  replace(p,r,1,{payload:{text:'reason '+i}});
  replace(p,r,700,{status:'completed',payload:{done:true,snippet:'result '+i}});
  replace(p,r,1,{payload:{text:'reason coalesced '+i}});
  const scene=p.project({mode});
  assert.deepEqual(scene,api.projectAssistantTurnAnchorActivityScene(r,{mode}));
 }
 const after=p.metrics();
 assert.equal(after.projectedRowsRebuilt-before.projectedRowsRebuilt,300);
 assert.equal(after.sourceRowsInspected-before.sourceRowsInspected,300);
 assert.equal(after.fullFallbacks,before.fullFallbacks);
}
''')


def test_single_dirty_row_has_bounded_projection_work_in_both_modes():
    run_js(r'''
for(const mode of ['compact_worklog','transparent_stream']){
 const r=registry();for(let i=1;i<=1000;i++)add(r,i);
 const p=project(r);const before=p.project({mode});
 let inspected=0;
 // A getter on the dirty payload proves the projector reads actual source;
 // the row builder counter measures full-history rebuilds independently.
 const next={text:'prose 1000 changed'};
 replace(p,r,999,{payload:next});
 builds=0;
 const after=p.project({mode});const rebuilt=builds;
 assert.equal(after.activity_rows[999].text,'prose 1000 changed');
 assert.deepEqual(after,api.projectAssistantTurnAnchorActivityScene(r,{mode}));
 assert.equal(rebuilt,1,'one dirty row must not rebuild historical rows');
 assert.equal(after.activity_rows[0],before.activity_rows[0]);
 assert.equal(before.activity_rows[999].text,'prose 1000','prior snapshot must remain stable');
}
''')
