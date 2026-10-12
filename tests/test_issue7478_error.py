"""Application-error settlement releases its owner without cutting recovery short."""
import pytest
from tests.test_issue6391_live_scene_paint import run_js
from tests.test_issue7478_spaced_semantic import helpers, runtime


@pytest.mark.parametrize('lane', ['background', 'current', 'recovery', 'recovery-replaced'])
def test_application_error_releases_projection_owner(lane):
    run_js(helpers() + r"""
(async()=>{
""" + runtime() + r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false,_anchorPaintScheduler=null;
let _terminalStateReached=false,_streamFinalized=false,_persistTimer=null;
const activeSid='s',streamId='r',LIVE_STREAMS={},INFLIGHT={};
const S={session:{session_id:LANE==='background'?'other':'s'},messages:[],activeStreamId:'r'};
let assistantText='',liveReasoningText='',segmentStart=0,closed=0,lifecycle=0;
let _semanticProseTimer=null,_semanticProseDirty=false,semanticText='';
const assistantRow={},_freshSegment=false,ensureAssistantRow=()=>{},_completeAutomaticCompressionOnLiveProgress=()=>{};
const syncInflightAssistantMessage=()=>{},_upsertAnchorProcessProse=text=>{semanticText=text;};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_drainSemanticProse','_scheduleSemanticProse'])eval(extract(messageSource,f));
const _anchorRegistry={};
const _anchorSceneRenderProjectionCaches=new Map([['live',{sessionId:'s',streamId:'r'}]]);
eval(extract(src,'_releaseAnchorSceneRenderProjections'));
const _disposeAnchorScenePaint=()=>{_anchorPaintGeneration++;_anchorPaintDisposed=true;_releaseAnchorSceneRenderProjections(activeSid,streamId);};
const snapshotLiveTurnHtmlForSession=()=>{},_resumeSessionStreamAfterLiveChat=()=>{};
eval(extract(messageSource,'closeLiveStream'));
const _closeSource=source=>closeLiveStream(activeSid,streamId,source);
const _bailOutOfTerminalEventsFromStaleStream=()=>false;
const _clearStreamEndRecovery=()=>{},_cancelThrottledSnapshotTimer=()=>{};
const _clearAnchorProseIncrementalNode=()=>{},_cancelAnimationFramePendingStreamRender=()=>{};
const _streamFadeCleanupReduceMotionListener=()=>{},_smdEndParser=()=>{};
const _clearOwnerInflightState=()=>{},_clearStreamHidden=()=>{},_clearStreamNotificationBackground=()=>{};
const _clearApprovalForOwner=()=>{},_clearClarifyForOwner=()=>{},_flushReasoningToAnchor=()=>{};
const _applyToAnchor=()=>{},_scheduleAnchorRegistryCleanup=()=>{};
const _rememberRunJournalCursor=()=>{},_withDeferredAnchorScenePaint=fn=>fn;
const renderSessionList=()=>{},_setActivePaneIdleIfOwner=()=>{},trackBackgroundError=()=>{};
const _attachProjectedAnchorSceneToLastAssistant=()=>{},clearLiveToolCards=()=>{};
const _markSessionViewed=()=>{},renderMessages=()=>{},_settledAnchorRetryOwnerKey=()=>'';
const _filterRecoveryControlMessages=x=>x;
const _dispatchExtensionTurnLifecycle=()=>lifecycle++,_completeOwnedTerminal=()=>{};
const setTimeout=()=>1,clearTimeout=()=>{};
let resolveRecovery;
const _restoreSettledSession=()=>new Promise(resolve=>{resolveRecovery=resolve;});
const callbacks=[];
const source={readyState:1,close(){closed++;this.readyState=2;},addEventListener(type,fn){callbacks.push({type,fn});}};
eval(extract(messageSource,'_wireSSE'));_wireSSE(source);
for(const text of ['before','<thi'])for(const x of callbacks.filter(x=>x.type==='token'))x.fn({data:JSON.stringify({text})});
const data=LANE.startsWith('recovery')?{session_id:'s',type:'interrupted',recovery_control:true}:{session_id:'s',type:'rate_limit',message:'synthetic error'};
for(const x of callbacks.filter(x=>x.type==='apperror')) x.fn({data:JSON.stringify(data)});
assert.equal(semanticText,'before');assert.equal(_semanticProseTimer,null);
assert.equal(assistantText,'before<thi','raw partial delimiter is retained for canonical recovery');
if(LANE.startsWith('recovery')){
 assert.equal(_anchorPaintDisposed,false,'recovery retains generation until settlement');
 assert.equal(_anchorSceneRenderProjectionCaches.size,1);
 assert.equal(typeof resolveRecovery,'function');
 if(LANE==='recovery-replaced') LIVE_STREAMS.s={streamId:'r',source:{readyState:1}};
 resolveRecovery(true);
 await new Promise(resolve=>setImmediate(resolve));
}
assert.equal(lifecycle,1);assert.equal(closed,1);
if(LANE==='recovery-replaced'){
 assert.notEqual(LIVE_STREAMS.s.source,source);
 assert.equal(_anchorSceneRenderProjectionCaches.size,1,'late recovery must not release successor state');
 return;
}
assert.equal(LIVE_STREAMS.s,undefined,'application error retires the closed owner');
assert.equal(_anchorPaintDisposed,true);assert.equal(_anchorSceneRenderProjectionCaches.size,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace('LANE', repr(lane)))


@pytest.mark.parametrize('prefix', ['', 'before ', '<thi'])
@pytest.mark.parametrize('replay', [False, True])
def test_transport_recovery_recreates_semantics_before_first_token(prefix, replay):
    run_js(helpers() + r"""
(async()=>{
""" + runtime() + r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false,_anchorPaintScheduler=null;
let _terminalStateReached=false,_streamFinalized=false,_persistTimer=null;
let _pendingProsePaint=null,_pendingKatexPaint=false;const _pendingMediaPaintRoots=new Set();
let _pendingStreamEndRecovery=false,_reconnectAttempted=false;
const activeSid='s',streamId='r',LIVE_STREAMS={},INFLIGHT={s:{messages:[]}};
const S={session:{session_id:'s'},messages:[]};
let assistantText='',liveReasoningText='',reasoningText='',segmentStart=0;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},_freshSegment=false,ensureAssistantRow=()=>{};
const _completeAutomaticCompressionOnLiveProgress=()=>{},_upsertAnchorProcessProse=()=>{};
const _throttledPersist=()=>{},_rememberRunJournalCursor=()=>{};
const _cancelAnimationFramePendingStreamRender=()=>{},_cancelThrottledSnapshotTimer=()=>{};
const _clearStreamEndRecovery=()=>{_pendingStreamEndRecovery=false;};
const _bailOutOfTerminalEventsFromStaleStream=()=>false,_deferStreamErrorIfOffline=()=>false;
const _deferStreamErrorIfPageHidden=()=>false,_isSessionCurrentPane=()=>true;
const snapshotLiveTurnHtmlForSession=()=>{},_resumeSessionStreamAfterLiveChat=()=>{};
const setComposerStatus=()=>{},_runJournalReplayParams=()=>'';
const document={baseURI:'http://127.0.0.1/'};
const timers=new Map();let timerId=0;
const setTimeout=(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;};
const clearTimeout=id=>timers.delete(id),cancelAnimationFrame=()=>{};
const api=async()=>REPLAY?{replay_available:true}:{active:true};
const sources=[];
class EventSource{
 constructor(){this.readyState=1;this.callbacks=[];sources.push(this);}
 addEventListener(type,fn){this.callbacks.push({type,fn});}
 close(){this.readyState=2;}
 async emit(type,data){for(const x of this.callbacks.filter(x=>x.type===type))await x.fn({data:JSON.stringify(data)});}
}
for(const name of ['_stripXmlToolCalls','_parseStreamState','syncInflightAssistantMessage',
 '_drainSemanticProse','_scheduleSemanticProse','_withDeferredAnchorScenePaint',
 '_completeOwnedTerminal','_disposeAnchorScenePaint','_retireSourceForRecovery','closeLiveStream','_wireSSE'])eval(extract(messageSource,name));
const first=new EventSource();_wireSSE(first);
assert.equal(_semanticMetrics.fullParses,0,'first empty attach needs no recovery parse');
if(PREFIX)await first.emit('token',{text:PREFIX});
await first.emit('error',{});
assert.equal(_semanticState,null,'retired parser is actually released');
assert.equal(_semanticFallback,null);assert.equal(_semanticRaw,'');
const retry=[...timers].find(([,t])=>t.ms===1500);assert.ok(retry);
timers.delete(retry[0]);retry[1].fn();
await new Promise(resolve=>setImmediate(resolve));
assert.equal(sources.length,2,'actual reconnect status handler created successor transport');
const second=sources[1];
const tail=PREFIX==='<thi'?'nk>secret</think>answer':'<think>secret</think>answer';
for(const text of [tail.slice(0,4),tail.slice(4),'<function_calls>hidden</function_calls>end']){
 await second.emit('token',{text});_drainSemanticProse();
}
const expected=(PREFIX==='before '?'before ':'')+'answerend';
assert.equal(_semanticSnapshot().content,expected,'tokens after empty recovery must reach semantic output');
assert.equal(_semanticSnapshot().reasoning,'secret');
assert.equal(INFLIGHT.s.messages.length,1);
assert.equal(INFLIGHT.s.messages[0].content,expected);
assert.equal(INFLIGHT.s.messages[0].reasoning,'secret');
const accepted=assistantText;await first.emit('token',{text:'stale'});
assert.equal(assistantText,accepted,'old callbacks cannot feed successor parser');
closeLiveStream(activeSid,streamId,second);
assert.equal(_semanticState,null);assert.equal(_semanticFallback,null);assert.equal(_semanticRaw,'');
assert.equal(timers.size,0);assert.equal(LIVE_STREAMS.s,undefined);
await second.emit('token',{text:'late'});assert.equal(assistantText,accepted);
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace('PREFIX', repr(prefix)).replace('REPLAY', 'true' if replay else 'false'))
