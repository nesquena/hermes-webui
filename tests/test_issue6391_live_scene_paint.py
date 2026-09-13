"""Executable scheduling contracts for the live scene's visual projection.

The fake frame clock deliberately retains cancelled callbacks so teardown tests
also exercise a callback that has already been handed to the browser.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def run_js(body):
    assert NODE, "node is required for executable frame scheduling contracts"
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const name = '_createLiveScenePaintScheduler';
const start = src.indexOf('function ' + name + '(');
assert.notEqual(start, -1, 'live scene frame scheduler must exist');
let end = src.indexOf('{', start), depth = 1;
for(end++; depth && end < src.length; end++) {
  if(src[end] === '{') depth++;
  if(src[end] === '}') depth--;
}
    eval(src.slice(start, end));
    function extract(source, name) {
      const start=source.indexOf('function '+name+'(');
      assert.notEqual(start,-1,name+' must exist');
      let params=source.indexOf('(',start), nesting=1;
      for(params++; nesting && params<source.length; params++) {
        if(source[params]==='(') nesting++;
        if(source[params]===')') nesting--;
      }
      let end=source.indexOf('{',params), depth=1;
      for(end++; depth && end<source.length; end++) {
        if(source[end]==='{') depth++;
        if(source[end]==='}') depth--;
      }
      return source.slice(start,end);
    }
    const messageSource=fs.readFileSync(process.argv[2], 'utf8');
let _pendingTerminalFinish=null,_terminalFadeTimer=null,_terminalFadeFrame=null;
function _cancelAnchorRegistryCleanup(){}
let nextId = 0, frames = new Map(), cancelled = [], current = true;
const events = [], paints = [];
const scheduler = _createLiveScenePaintScheduler({
  requestFrame(fn) { frames.set(++nextId, fn); return nextId; },
  cancelFrame(id) { cancelled.push(id); },
  isCurrent() { return current; },
  paint() { paints.push(events.slice()); },
});
function frame() {
  const batch = [...frames.values()]; frames.clear();
  for(const fn of batch) fn();
}
""" + body
    result = subprocess.run(
        [NODE, "-e", script, str(ROOT / "static/ui.js"), str(ROOT / "static/messages.js")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("mode", ["compact_worklog", "transparent_stream"])
def test_real_anchor_ingestion_preserves_750_lifecycles_and_replay_order(mode):
    run_js("""
require(require('node:path').join(require('node:path').dirname(process.argv[1]),'assistant_turn_anchors.js'));
const _anchorApi=globalThis.HermesAssistantTurnAnchors;
const activeSid='a',streamId='s',_assistantSegmentSeq=1,_currentActivityBurstId=1;
const _anchorPaintDisposed=false;
let _anchorShadowWarned=false;
const _anchorRegistry=_anchorApi.createAssistantTurnAnchorRegistry({session_id:activeSid,stream_id:streamId});
const _renderAnchorLiveScene=()=>{scheduler.request();return true;};
eval(extract(messageSource,'_applyToAnchor'));
const _anchorSceneRenderProjectionCaches=new Map();
for(const fn of ['_anchorSceneRenderCacheKey','_anchorSceneRenderOutputKey','_anchorSceneAttachIncrementalProjection','_anchorSceneSourceRows','_anchorSceneRowsIncrementalProjection','_anchorSceneRememberFullProjection','_anchorSceneToolRowLogicalKey','_anchorSceneMergeToolRows','_anchorSceneRowsForRendering','_anchorSceneIsSettledSuccessfulCompression']) eval(extract(src,fn));
for(let i=0;i<750;i++) {
  const start={id:'tool-'+i,name:'terminal',args:{i},created_at:1};
  const startEvent={lastEventId:'start-'+i};
  assert.equal(_applyToAnchor('tool',start,startEvent).applied,true);
  assert.equal(_applyToAnchor('tool',start,startEvent).reason,'duplicate');
  assert.equal(_applyToAnchor('tool_complete',{...start,done:true,snippet:'result-'+i},{lastEventId:'end-'+i}).applied,true);
  events.push(i);
  if(i%250===249) frame();
}
assert.equal(paints.length,3);
assert.equal(paints[0].length,250);
assert.equal(paints[1].length,500);
assert.deepEqual(paints[2],Array.from({length:750},(_,i)=>i));
assert.equal(_anchorRegistry.stats.skipped_duplicate,750);
const scene=_anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry,{mode:MODE});
const rows=_anchorSceneRowsForRendering(scene,{settled:false});
assert.equal(rows.length,750);
rows.forEach((row,i)=>{
 assert.equal(row.tool_call_id,'tool-'+i);
 assert.equal(row.tool.args.i,i);
 assert.equal(row.tool.snippet,'result-'+i);
 assert.equal(row.tool.done,true);
});
""".replace("MODE", repr(mode)))


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_disposed_owner_rejects_queued_events_and_replacement_gets_new_generation():
    run_js("""
let _deferAnchorScenePaint=false, _anchorPaintScheduler=scheduler;
let _anchorPaintGeneration=0, _anchorPaintDisposed=false;
let _pendingProsePaint=null,_committingLivePaint=false,_pendingKatexPaint=false,_persistTimer=null; const _pendingMediaPaintRoots=new Set();
let cancelledSnapshotTimers=0;
let _semanticState={pending:'<thi',content:'retained'.repeat(10000)},_semanticFallback={content:'fallback'},_semanticRaw='raw';
const _cancelAnimationFramePendingStreamRender=()=>{};
const _clearStreamEndRecovery=()=>{};
const _cancelThrottledSnapshotTimer=()=>{cancelledSnapshotTimers++;};
eval(extract(messageSource,'_withDeferredAnchorScenePaint'));
eval(extract(messageSource,'_disposeAnchorScenePaint'));
const oldHandler=_withDeferredAnchorScenePaint(e=>{events.push(e);scheduler.request();});
oldHandler('start');
_disposeAnchorScenePaint();
assert.equal(cancelledSnapshotTimers,1,'dispose must cancel trailing DOM snapshots');
assert.equal(_semanticState,null);assert.equal(_semanticFallback,null);assert.equal(_semanticRaw,'');
oldHandler('late-complete');
frame();
assert.deepEqual(events,['start'],'disposed source must reject queued tool events');
assert.equal(paints.length,0);
assert.equal(scheduler.pending(),false);
// Reconnection in the same attachment gets a fresh generation, while the
// callback captured by the old EventSource remains invalid forever.
_anchorPaintDisposed=false;
const replacement=_withDeferredAnchorScenePaint(e=>events.push(e));
oldHandler('obsolete');replacement('current');
assert.deepEqual(events,['start','current']);
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_navigation_disposes_every_live_scene_without_closing_transport():
    run_js("""
const disposed=[];
const LIVE_STREAMS={a:{disposeScene:()=>disposed.push('a')},b:{disposeScene:()=>disposed.push('b')},legacy:{}};
eval(extract(messageSource,'_disposeLiveScenePaints'));
_disposeLiveScenePaints();
assert.deepEqual(disposed,['a','b']);
assert.equal(Object.keys(LIVE_STREAMS).length,3);
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_close_stream_flushes_owned_visual_work_before_snapshot_and_disposes():
    run_js("""
const order=[];
const source={readyState:1,close(){order.push('close');}};
const LIVE_STREAMS={a:{streamId:'s',source,
  flushScene(){order.push('flush');scheduler.flush();},
  disposeScene(){order.push('dispose');scheduler.dispose();},
}};
const INFLIGHT={};
const snapshotLiveTurnHtmlForSession=()=>order.push('snapshot');
const _resumeSessionStreamAfterLiveChat=()=>{};
eval(extract(messageSource,'closeLiveStream'));
events.push('tool');scheduler.request();
closeLiveStream('a','other',source);
assert.deepEqual(order,[]);
closeLiveStream('a','s',{});
assert.deepEqual(order,[]);
closeLiveStream('a','s',source);
assert.deepEqual(order,['flush','snapshot','dispose','close']);
frame(); assert.equal(paints.length,1);
assert.equal(scheduler.pending(),false);
assert.equal(LIVE_STREAMS.a,undefined);
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_real_render_request_is_coalesced_and_snapshot_follows_commit():
    run_js("""
function _flushStreamingMediaPostProcess(){}
let _deferAnchorScenePaint=false, _anchorPaintScheduler=null;
let _anchorPaintGeneration=0, _anchorPaintDisposed=false;
let _pendingProsePaint=null,_committingLivePaint=false,_pendingKatexPaint=false,_persistTimer=null; const _pendingMediaPaintRoots=new Set();
let _anchorRegistry={}, _anchorShadowWarned=false;
let activeSid='a', streamId='s', _terminalStateReached=false, _streamFinalized=false;
const S={session:{session_id:activeSid},activeStreamId:streamId};
const LIVE_STREAMS={};
let snapshots=0;const order=[];let _livePaintScrollSnapshot=null;
const _captureMessageScrollSnapshot=()=>{order.push('capture');return {};};
const _restoreMessageScrollSnapshotSameFrame=()=>order.push('restore');
const snapshotLiveTurn=()=>{ snapshots++; };
const _isActiveSession=()=>current;
const _anchorSceneActiveMode=()=> 'transparent_stream';
const window={
  _createLiveScenePaintScheduler,
  requestAnimationFrame:fn=>{frames.set(++nextId,fn);return nextId;},
  cancelAnimationFrame:id=>cancelled.push(id),
  _renderLiveAnchorActivitySceneForStream:(s,a,opts)=>{if(!opts.scrollOwned)_captureMessageScrollSnapshot();order.push('scene');paints.push(events.slice());if(!opts.scrollOwned)_restoreMessageScrollSnapshotSameFrame();return true;},
  _liveAnchorRegistries:new Map([[streamId,_anchorRegistry]]),
};
eval(extract(messageSource,'_withDeferredAnchorScenePaint'));
eval(extract(messageSource,'_renderAnchorLiveScene'));
const handler=_withDeferredAnchorScenePaint(e=>{
  events.push(e); assert.equal(_renderAnchorLiveScene(),true);
  assert.equal(_renderAnchorLiveScene(),true);
});
handler('start'); handler('complete');
_pendingProsePaint=()=>order.push('prose');
assert.equal(paints.length,0);
assert.equal(frames.size,1);
frame(); assert.deepEqual(paints,[events]);
assert.equal(snapshots,1);
assert.deepEqual(order,['capture','prose','scene','restore']);
handler('next'); current=false; frame();
assert.equal(paints.length,1);
assert.equal(_anchorPaintScheduler.pending(),false);
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_event_batch_defers_only_paint_and_restores_scope_on_error():
    run_js("""
let _deferAnchorScenePaint=false;
let _anchorPaintGeneration=0, _anchorPaintDisposed=false;
let _pendingProsePaint=null,_committingLivePaint=false,_pendingKatexPaint=false,_persistTimer=null; const _pendingMediaPaintRoots=new Set();
let _anchorPaintScheduler=scheduler;
eval(extract(messageSource, '_withDeferredAnchorScenePaint'));
const handler=_withDeferredAnchorScenePaint(event=>{
  events.push(event);
  assert.equal(_deferAnchorScenePaint,true);
  scheduler.request(); scheduler.request();
});
handler('tool'); handler('tool_complete');
assert.deepEqual(events,['tool','tool_complete']);
assert.equal(_deferAnchorScenePaint,false);
assert.equal(paints.length,0);
assert.equal(frames.size,1);
frame(); assert.deepEqual(paints,[events]);
assert.throws(_withDeferredAnchorScenePaint(()=>{throw Error('test');}));
assert.equal(_deferAnchorScenePaint,false);
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("transition", ["switch", "dispose", "flush", "cancel_replace"])
def test_lifecycle_invalidates_already_dispatched_callbacks(transition):
    run_js("""
events.push('start'); scheduler.request();
const obsolete = [...frames.values()][0];
""" + {
        "switch": """
current=false; frame();
assert.equal(paints.length, 0);
assert.equal(scheduler.pending(), false);
""",
        "dispose": """
scheduler.dispose(); obsolete(); scheduler.request();
assert.equal(paints.length, 0);
assert.equal(scheduler.pending(), false);
assert.equal(cancelled.length, 1);
""",
        "flush": """
events.push('complete'); scheduler.flush();
assert.deepEqual(paints, [events]);
obsolete(); assert.equal(paints.length, 1);
assert.equal(scheduler.pending(), false);
""",
        "cancel_replace": """
scheduler.cancel(); events.push('replacement'); scheduler.request();
obsolete(); assert.equal(paints.length, 0);
assert.equal(scheduler.pending(), true);
frame(); assert.deepEqual(paints, [events]);
assert.equal(scheduler.pending(), false);
""",
    }[transition])


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_burst_coalesces_without_deferring_authoritative_state():
    run_js("""
for(let i=0; i<100; i++) { events.push(i); scheduler.request(); }
assert.equal(events.length, 100);
assert.equal(frames.size, 1);
assert.equal(paints.length, 0);
frame();
assert.deepEqual(paints, [events]);
assert.equal(scheduler.pending(), false);
events.push(100); scheduler.request(); frame();
assert.deepEqual(paints[1], events);
assert.equal(paints.length, 2);
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_actual_sse_registration_rejects_obsolete_callbacks_before_handlers():
    run_js(r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false,_anchorPaintScheduler=null;
let _pendingProsePaint=null,_committingLivePaint=false,_deferAnchorScenePaint=false,_pendingKatexPaint=false,_persistTimer=null; const _pendingMediaPaintRoots=new Set();
const activeSid='a',streamId='s',LIVE_STREAMS={};
const INFLIGHT={};
const _resumeSessionStreamAfterLiveChat=()=>{};
const _drainSemanticProse=()=>{},_scheduleSemanticProse=()=>{};
eval(extract(messageSource,'closeLiveStream'));
let _terminalStateReached=false,_streamFinalized=false,assistantText='',mirrors=0;
const S={session:{session_id:'background-pane'},activeStreamId:'other'};
const syncInflightAssistantMessage=()=>mirrors++;
const _cancelThrottledSnapshotTimer=()=>{};
const _clearStreamEndRecovery=()=>{};
const _cancelAnimationFramePendingStreamRender=()=>{};
const _rememberRunJournalCursor=()=>mirrors++;
const _renderAnchorLiveScene=()=>{throw Error('obsolete paint');};
eval(extract(messageSource,'_withDeferredAnchorScenePaint'));
eval(extract(messageSource,'_disposeAnchorScenePaint'));
eval(extract(messageSource,'_wireSSE'));
function transport(){
 const callbacks=[];
 return {readyState:1,callbacks,
   addEventListener(type,fn){callbacks.push({type,fn});},
   close(){this.readyState=2;},
   dispatch(type,data){for(const c of callbacks.filter(x=>x.type===type))c.fn({data:JSON.stringify(data),lastEventId:'s:1'});}
 };
}
const old=transport();_wireSSE(old);
const oldOwner=LIVE_STREAMS[activeSid];
assert.ok(old.callbacks.some(c=>c.type==='token'));
assert.ok(old.callbacks.some(c=>c.type==='interim_assistant'));
assert.ok(old.callbacks.some(c=>c.type==='reasoning'));
assert.ok(old.callbacks.some(c=>c.type==='tool'));
_disposeAnchorScenePaint();
// These are the real registered production closures, not stand-in handlers.
for(const c of old.callbacks)c.fn({data:'{}',lastEventId:'s:999'});
assert.equal(mirrors,0);
assert.equal(assistantText,'');
assert.equal(_anchorPaintScheduler,null);
const fresh=transport();_wireSSE(fresh);
oldOwner.disposeScene();oldOwner.flushScene();
assert.equal(oldOwner.requestScene(),false);
assert.equal(_anchorPaintDisposed,false);
for(const c of old.callbacks)c.fn({data:'{}',lastEventId:'s:999'});
assert.equal(mirrors,0);
fresh.dispatch('token',{text:'new generation'});
assert.equal(assistantText,'new generation');
assert.equal(mirrors,1,'cursor executes; inflight extraction waits for the semantic batch');
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_streaming_media_postprocess_uses_shared_owner_and_deduplicates_roots():
    run_js(r"""
let _anchorPaintDisposed=false,_pendingMediaPaintRoots=new Set(),requests=0;
function _renderAnchorLiveScene(){requests++;return true;}
function postProcessRenderedMessages(){}
function requestAnimationFrame(){throw new Error('independent media frame');}
eval(extract(fs.readFileSync('static/messages.js','utf8'),'_smdScheduleMediaPostProcess'));
const root={};_smdScheduleMediaPostProcess(root);_smdScheduleMediaPostProcess(root);
assert.equal(_pendingMediaPaintRoots.size,1);
assert.equal(requests,1);
_anchorPaintDisposed=true;
_smdScheduleMediaPostProcess({});
assert.equal(_pendingMediaPaintRoots.size,1);assert.equal(requests,1);
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_terminal_continuations_reject_disposed_generation_before_state_or_dom():
    run_js(r"""
{
const text=fs.readFileSync('static/messages.js','utf8');
const isCurrentGeneration=()=>false;
let _streamFinalized=false;
const start=text.indexOf('const _finishDone=()=>{')+'const _finishDone=()=>{'.length;
const end=text.indexOf('\n      };',start);
assert.ok(start>0&&end>start);
const finish=eval('(()=>{'+text.slice(start,end)+'})');
assert.doesNotThrow(()=>finish());assert.equal(_streamFinalized,false);
const a=text.indexOf('const _applyCancelSessionPayload=(sessionPayload)=>{')+'const _applyCancelSessionPayload=(sessionPayload)=>{'.length;
const b=text.indexOf('\n      };',a);
const apply=eval('((sessionPayload)=>{'+text.slice(a,b)+'})');
assert.equal(apply({session_id:'obsolete'}),false);
}
""")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_prose_replacement_invalidates_only_its_projection_key():
    run_js(r"""
{
require(process.cwd()+'/static/assistant_turn_anchors.js');
const _anchorApi=globalThis.HermesAssistantTurnAnchors;
const _anchorRegistry=_anchorApi.createAssistantTurnAnchorRegistry({session_id:'s',stream_id:'stream'});
let _anchorPaintDisposed=false;
_anchorApi.applyAssistantTurnAnchorSourceEvent(_anchorRegistry,{source_event_type:'interim_assistant',event_id:'e',local_id:'prose',seq:1,payload:{text:'before'}},{session_id:'s',stream_id:'stream'});
const before=_anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry,{mode:'transparent_stream'});
const src=fs.readFileSync('static/messages.js','utf8');
eval(extract(src,'_anchorActivityEvents'));
eval(extract(src,'_replaceAnchorActivityEventByLocalId'));
const localId=_anchorRegistry.anchor.activity_events[0].local_id;
const replacement=_replaceAnchorActivityEventByLocalId(localId,'interim_assistant',{payload:{text:'after'}});
assert.ok(replacement);
const after=_anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry,{mode:'transparent_stream'});
assert.equal(after.activity_rows[0].payload.text,'after');
assert.equal(after.projection_stats.events_scanned,1);
_anchorPaintDisposed=true;
assert.equal(_replaceAnchorActivityEventByLocalId(localId,'interim_assistant',{payload:{text:'obsolete'}}),null);
assert.equal(_anchorRegistry.anchor.activity_events[0].payload.text,'after');
}
""")
