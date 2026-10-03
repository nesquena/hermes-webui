"""Real SSE callback lifecycle and producer projection matrices for #6391."""

import pytest

from tests.test_issue6391_dirty_projection import _run_node
from tests.test_issue6391_live_scene_paint import run_js


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream"])
def test_real_registered_callbacks_claim_done_once_and_reject_late_producers(mode):
    run_js(
        r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false;
let _anchorPaintScheduler=scheduler;
let _terminalStateReached=false,_streamFinalized=false;
let _persistTimer=null;
const activeSid='session',streamId='stream';
const LIVE_STREAMS={};
const S={session:{session_id:activeSid},activeStreamId:streamId};
const INFLIGHT={[activeSid]:{messages:[],toolCalls:[]}};
let terminalClaims=0,handlerEffects=0,closed=0,cursorEffects=0;
const _closeSource=()=>{closed+=1;};
const _completeOwnedTerminal=()=>{
  const pending=_pendingTerminalFinish;
  if(!pending||_anchorPaintDisposed||pending.generation!==_anchorPaintGeneration)return;
  _pendingTerminalFinish=null;
  terminalClaims+=1;
};
const _bailOutOfTerminalEventsFromStaleStream=()=>false;
const _clearStreamEndRecovery=()=>{};
const _cancelThrottledSnapshotTimer=()=>{};
const _shouldUseLiveProseFade=()=>false;
const _rememberRunJournalCursor=()=>{cursorEffects+=1;};
const _applyToAnchor=()=>{handlerEffects+=1;};
const _withDeferredAnchorScenePaint=handler=>handler;
const _renderAnchorLiveScene=()=>true;
const _anchorPaintDisposedForTest=()=>_anchorPaintDisposed;
const _sourceEvents=[];
function transport(){
  const source={
    readyState:1,
    callbacks:_sourceEvents,
    addEventListener(type,handler){this.callbacks.push({type,handler});},
    close(){this.readyState=2;closed+=1;},
    dispatch(type,payload){
      const event={data:JSON.stringify(payload||{}),lastEventId:streamId+':'+type};
      for(const item of this.callbacks.filter(candidate=>candidate.type===type))item.handler(event);
    },
  };
  return source;
}
eval(extract(messageSource,'_wireSSE'));
const source=transport();
_wireSSE(source);
for(const type of ['token','interim_assistant','reasoning','tool','tool_complete','done','stream_end','cancel','error','apperror']){
  assert.ok(source.callbacks.some(item=>item.type===type),type+' callback must be registered');
}
source.dispatch('done',{status:'completed'});
assert.equal(terminalClaims,1,'the first done event must claim terminal ownership');
assert.equal(_pendingTerminalFinish,null);
const effectsAfterDone=[handlerEffects,cursorEffects,terminalClaims];
for(const type of ['done','token','interim_assistant','reasoning','tool','tool_complete','cancel','error','apperror']){
  source.dispatch(type,{text:'late',status:'error'});
}
assert.deepEqual([handlerEffects,cursorEffects,terminalClaims],effectsAfterDone);
assert.equal(_anchorPaintDisposedForTest(),false);
""".replace("MODE", repr(mode))
    )


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream"])
def test_done_fade_then_stream_end_completes_once_before_late_callbacks(mode):
    run_js(
        r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false;
let _anchorPaintScheduler=scheduler;
let _terminalStateReached=false,_streamFinalized=false;
let _persistTimer=null;
const activeSid='session',streamId='stream';
const LIVE_STREAMS={};
const S={session:{session_id:activeSid},activeStreamId:streamId};
const INFLIGHT={[activeSid]:{messages:[],toolCalls:[]}};
let terminalClaims=0,fadeFinish=null,closed=0;
const _closeSource=()=>{closed+=1;};
const _completeOwnedTerminal=()=>{
  const pending=_pendingTerminalFinish;
  if(!pending||_anchorPaintDisposed||pending.generation!==_anchorPaintGeneration)return;
  _pendingTerminalFinish=null;
  terminalClaims+=1;
};
const _bailOutOfTerminalEventsFromStaleStream=()=>false;
const _clearStreamEndRecovery=()=>{};
const _cancelThrottledSnapshotTimer=()=>{};
const _cancelAnimationFramePendingStreamRender=()=>{};
const _shouldUseLiveProseFade=()=>true;
const _drainStreamFadeBeforeDone=finish=>{fadeFinish=finish;};
const _rememberRunJournalCursor=()=>{};
const _withDeferredAnchorScenePaint=handler=>handler;
const _renderAnchorLiveScene=()=>true;
const assistantBody={};
function transport(){
  const callbacks=[];
  return {
    readyState:1,
    callbacks,
    addEventListener(type,handler){callbacks.push({type,handler});},
    close(){this.readyState=2;closed+=1;},
    dispatch(type,payload){
      const event={data:JSON.stringify(payload||{}),lastEventId:streamId+':'+type};
      for(const item of callbacks.filter(candidate=>candidate.type===type))item.handler(event);
    },
  };
}
eval(extract(messageSource,'_wireSSE'));
const source=transport();
_wireSSE(source);
source.dispatch('done',{status:'completed'});
assert.equal(_streamFinalized,true);
assert.equal(_terminalStateReached,true);
assert.ok(_pendingTerminalFinish,'done must leave a required finish pending during the fade');
assert.equal(terminalClaims,0);
assert.equal(typeof fadeFinish,'function');
source.dispatch('stream_end',{});
assert.equal(terminalClaims,1,'stream_end must complete the pending terminal exactly once');
assert.equal(_pendingTerminalFinish,null);
fadeFinish();
for(const type of ['done','token','reasoning','tool','tool_complete','cancel','error','apperror','stream_end']){
  source.dispatch(type,{text:'late',status:'error'});
}
assert.equal(terminalClaims,1,'late producers and terminal signals cannot reclaim completion');
""".replace("MODE", repr(mode))
    )


def test_same_source_rewire_retires_already_registered_callbacks():
    run_js(
        r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false;
let _anchorPaintScheduler=scheduler;
let _pendingProsePaint=null,_pendingKatexPaint=false;
const _pendingMediaPaintRoots=new Set();
let _terminalStateReached=false,_streamFinalized=false;
let _persistTimer=null;
const activeSid='session',streamId='stream';
const LIVE_STREAMS={};
const S={session:{session_id:activeSid},activeStreamId:streamId};
const INFLIGHT={[activeSid]:{messages:[],toolCalls:[]}};
let assistantText='',tokenEffects=0;
let _snapshotLiveTurnTimer=null,_streamEndRecoveryTimer=null;
let _pendingStreamEndRecovery=false,_pendingRafHandle=null;
const _anchorRegistryCleanupTimer=null;
const _clearStreamEndRecovery=()=>{_pendingStreamEndRecovery=false;_streamEndRecoveryTimer=null;};
const _cancelThrottledSnapshotTimer=()=>{_snapshotLiveTurnTimer=null;};
const _cancelAnimationFramePendingStreamRender=()=>{_pendingRafHandle=null;};
const _closeSource=()=>{};
const _bailOutOfTerminalEventsFromStaleStream=()=>false;
const _rememberRunJournalCursor=()=>{};
const _completeAutomaticCompressionOnLiveProgress=()=>{};
const syncInflightAssistantMessage=()=>{};
const _parseStreamState=()=>({displayText:assistantText});
const ensureAssistantRow=()=>{};
const _scheduleRender=()=>{};
const _upsertAnchorProcessProse=()=>{tokenEffects+=1;};
const _scheduleSemanticProse=()=>{tokenEffects+=1;};
const _withDeferredAnchorScenePaint=handler=>handler;
const _renderAnchorLiveScene=()=>true;
const segmentStart=0;
const _freshSegment=false;
const assistantRow={};
const assistantBody=null;
function transport(){
  const callbacks=[];
  return {
    readyState:1,
    callbacks,
    addEventListener(type,handler){callbacks.push({type,handler});},
    close(){this.readyState=2;},
    dispatch(type,payload){
      const event={data:JSON.stringify(payload||{}),lastEventId:streamId+':'+type};
      for(const item of callbacks.filter(candidate=>candidate.type===type))item.handler(event);
    },
  };
}
const clearTimeout=()=>{};
const cancelAnimationFrame=()=>{};
eval(extract(messageSource,'_disposeAnchorScenePaint'));
eval(extract(messageSource,'_wireSSE'));
const source=transport();
_wireSSE(source);
const firstOwner=LIVE_STREAMS[activeSid];
const oldCallbacks=source.callbacks.slice();
firstOwner.disposeScene();
_wireSSE(source);
const currentOwner=LIVE_STREAMS[activeSid];
assert.notStrictEqual(currentOwner,firstOwner);
for(const item of oldCallbacks.filter(candidate=>['token','reasoning','tool','tool_complete','done','cancel','error'].includes(candidate.type))){
  item.handler({data:JSON.stringify({text:'stale'}),lastEventId:'stale:1'});
}
assert.equal(assistantText,'');
assert.equal(tokenEffects,0,'same-source callbacks from the retired generation must be inert');
source.dispatch('token',{text:'fresh'});
assert.equal(assistantText,'fresh');
assert.equal(tokenEffects,1);
assert.equal(firstOwner.requestScene(),false,'retired owner cannot be revived by a same-source rewire');
assert.equal(currentOwner.requestScene(),true);
"""
    )


def test_disposal_is_idempotent_without_timer_or_owner_resurrection():
    run_js(
        r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false;
let _anchorPaintScheduler=null;
_terminalFadeTimer=11,_terminalFadeFrame=12;
let _pendingProsePaint=null,_pendingKatexPaint=false;
const _pendingMediaPaintRoots=new Set();
let _persistTimer=13,_snapshotLiveTurnTimer=14;
let _streamEndRecoveryTimer=15,_pendingStreamEndRecovery=true;
let _pendingRafHandle=16;
let clearCalls=0,cancelCalls=0,schedulerDisposals=0;
const activeSid='session',streamId='stream';
const LIVE_STREAMS={};
const S={session:{session_id:activeSid},activeStreamId:streamId};
const INFLIGHT={[activeSid]:{messages:[],toolCalls:[]}};
const _clearStreamEndRecovery=()=>{
  if(_streamEndRecoveryTimer!==null){clearTimeout(_streamEndRecoveryTimer);_streamEndRecoveryTimer=null;}
  _pendingStreamEndRecovery=false;
};
const _rememberRunJournalCursor=()=>{};
const _cancelThrottledSnapshotTimer=()=>{
  if(_snapshotLiveTurnTimer!==null){clearTimeout(_snapshotLiveTurnTimer);_snapshotLiveTurnTimer=null;}
};
const _cancelAnimationFramePendingStreamRender=()=>{
  if(_pendingRafHandle!==null){cancelAnimationFrame(_pendingRafHandle);_pendingRafHandle=null;}
};
const clearTimeout=()=>{clearCalls+=1;};
const cancelAnimationFrame=()=>{cancelCalls+=1;};
const _renderAnchorLiveScene=()=>true;
const _withDeferredAnchorScenePaint=handler=>handler;
function transport(){
  const callbacks=[];
  return {
    readyState:1,
    callbacks,
    addEventListener(type,handler){callbacks.push({type,handler});},
  };
}
_anchorPaintScheduler={dispose(){schedulerDisposals+=1;}};
eval(extract(messageSource,'_disposeAnchorScenePaint'));
eval(extract(messageSource,'_wireSSE'));
const source=transport();
_wireSSE(source);
const owner=LIVE_STREAMS[activeSid];
owner.disposeScene();
const firstCounts=[clearCalls,cancelCalls,schedulerDisposals,source.callbacks.length];
assert.equal(owner.requestScene(),false);
owner.disposeScene();
assert.deepEqual([clearCalls,cancelCalls,schedulerDisposals,source.callbacks.length],firstCounts);
assert.strictEqual(LIVE_STREAMS[activeSid],owner,'disposal must not replace or resurrect the stream owner');
assert.equal(_anchorPaintDisposed,true);
assert.equal(_pendingTerminalFinish,null);
assert.equal(_persistTimer,null);
assert.equal(_snapshotLiveTurnTimer,null);
assert.equal(_streamEndRecoveryTimer,null);
assert.equal(_pendingStreamEndRecovery,false);
assert.equal(_pendingRafHandle,null);
"""
    )


@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream"])
def test_normalizer_projection_covers_prose_reasoning_tools_and_mixed_producers(mode):
    _run_node(
        r"""
const scenarios={
  prose_only:{
    events:[
      {source_event_type:'token',event_id:'prose-1',seq:1,payload:{text:'hello'}},
      {source_event_type:'token',event_id:'prose-2',seq:2,payload:{text:' world'}},
      {source_event_type:'interim_assistant',event_id:'prose-3',seq:3,payload:{text:'interim answer'}},
    ],
    roles:['prose','prose','prose'],
  },
  reasoning:{
    events:[
      {source_event_type:'reasoning',event_id:'reason-1',seq:1,payload:{text:'first thought'}},
      {source_event_type:'reasoning',event_id:'reason-2',seq:2,payload:{reasoning:'second thought'}},
    ],
    roles:['thinking','thinking'],
  },
  tools:{
    events:[
      {source_event_type:'tool',event_id:'tool-1',seq:1,payload:{id:'call-1',name:'terminal',args:{command:'pwd'}}},
      {source_event_type:'tool_complete',event_id:'tool-2',seq:2,payload:{id:'call-1',result:'ok',done:true}},
    ],
    roles:['tool','tool'],
  },
  mixed:{
    events:[
      {source_event_type:'token',event_id:'mixed-1',seq:1,payload:{text:'before'}},
      {source_event_type:'reasoning',event_id:'mixed-2',seq:2,payload:{text:'thinking'}},
      {source_event_type:'tool',event_id:'mixed-3',seq:3,payload:{id:'call-m',name:'search',args:{query:'x'}}},
      {source_event_type:'tool_complete',event_id:'mixed-4',seq:4,payload:{id:'call-m',result:'found'}},
      {source_event_type:'token',event_id:'mixed-5',seq:5,payload:{text:'after'}},
    ],
    roles:['prose','thinking','tool','tool','prose'],
  },
};
for(const [name,scenario] of Object.entries(scenarios)){
  const registry=api.createAssistantTurnAnchorRegistry({session_id:'session',stream_id:'stream-'+name});
  const context={session_id:'session',stream_id:'stream-'+name,order_domain:'transport'};
  for(const event of scenario.events){
    const result=api.applyAssistantTurnAnchorSourceEvent(registry,event,context);
    assert.equal(result.applied,true,name+' event must apply');
  }
  const terminal=api.applyAssistantTurnAnchorSourceEvent(registry,{
    source_event_type:'done',event_id:name+'-done',seq:100,payload:{status:'completed'},
  },context);
  assert.equal(terminal.applied,true,name+' terminal event must apply');
  const duplicate=api.applyAssistantTurnAnchorSourceEvent(registry,scenario.events[0],context);
  assert.equal(duplicate.applied,false,name+' replayed event must dedupe');
  assert.equal(duplicate.reason,'duplicate');
  const scene=api.projectAssistantTurnAnchorActivityScene(registry,{mode:MODE});
  const rows=scene.activity_rows;
  assert.equal(rows.length,scenario.roles.length+1,name+' row count');
  assert.deepEqual(rows.slice(0,-1).map(row=>row.role),scenario.roles,name+' producer order');
  assert.equal(rows.at(-1).role,'terminal');
  assert.equal(rows.at(-1).source_event_type,'done');
  assert.equal(scene.lifecycle.status,'completed');
  assert.equal(scene.terminal_state,'completed');
  for(const row of rows.slice(0,-1)){
    const expectedHint=MODE==='transparent_stream'
      ? 'chronological_activity'
      : ({prose:'main_prose',thinking:'collapsed_thinking',tool:'tool_row'}[row.role]);
    assert.equal(row.display_hint,expectedHint,name+' mode hint');
  }
  if(name==='tools'){
    assert.equal(rows[0].tool_call_id,'call-1');
    assert.equal(rows[0].tool.done,false);
    assert.equal(rows[1].tool_call_id,'call-1');
    assert.equal(rows[1].tool.done,true);
    assert.equal(rows[0].tool.args.command,'pwd');
    assert.equal(rows[1].tool.snippet,'ok');
  }
  if(name==='mixed'){
    assert.equal(rows[1].thinking.text,'thinking');
    assert.equal(rows[2].tool.name,'search');
    assert.equal(rows[4].text,'after');
  }
}
""".replace("MODE", repr(mode))
    )
