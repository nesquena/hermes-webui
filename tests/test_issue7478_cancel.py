"""Payload-less cancellation keeps its async canonical settlement owner."""
import pytest
from tests.test_issue6391_live_scene_paint import run_js
from tests.test_issue7478_spaced_semantic import helpers as semantic_helpers, runtime


@pytest.mark.parametrize('tail', ['', '<thi'])
@pytest.mark.parametrize('spacing', [0, 40])
@pytest.mark.parametrize('has_row', [True, False])
def test_wire_cancel_awaits_canonical_snapshot(has_row, spacing, tail):
    run_js(semantic_helpers() + r"""
(async()=>{
""" + runtime() + r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false,_anchorPaintScheduler=null;
let _terminalStateReached=false,_streamFinalized=false,_persistTimer=null;
const activeSid='s',streamId='r',LIVE_STREAMS={};
const S={session:{session_id:'s'},messages:[],activeStreamId:'r'};
let assistantText='',liveReasoningText='',closed=0,lifecycle=0,rendered=0;
let segmentStart=0,_semanticProseTimer=null,_semanticProseDirty=false,scans=0,semanticText='';
let time=0,timerId=0;const timers=new Map();
const setTimeout=(fn,ms)=>{timers.set(++timerId,{fn,at:time+ms});return timerId;},clearTimeout=id=>timers.delete(id);
function advance(){time+=SPACING;for(const [id,t]of [...timers])if(t.at<=time){timers.delete(id);t.fn();}}
const assistantRow=ASSISTANT_ROW,_freshSegment=false;
let inflightScans=0;
const syncInflightAssistantMessage=()=>{inflightScans++;},_completeAutomaticCompressionOnLiveProgress=()=>{};
const ensureAssistantRow=()=>{};
eval(extract(messageSource,'_stripXmlToolCalls'));eval(extract(messageSource,'_parseStreamState'));
const fullParse=_parseStreamState;_parseStreamState=()=>{scans++;return fullParse();};
const _upsertAnchorProcessProse=x=>{semanticText=x;};
for(const f of ['_drainSemanticProse','_scheduleSemanticProse']) eval(extract(messageSource,f));
let resolveFetch; const api=()=>new Promise(resolve=>{resolveFetch=resolve;});
const _bailOutOfTerminalEventsFromStaleStream=()=>false;
const _clearStreamEndRecovery=()=>{},_cancelThrottledSnapshotTimer=()=>{};
const _clearAnchorProseIncrementalNode=()=>{},_cancelAnimationFramePendingStreamRender=()=>{};
const _streamFadeCleanupReduceMotionListener=()=>{},_smdEndParser=()=>{};
const _clearOwnerInflightState=()=>{},_clearStreamHidden=()=>{},_clearStreamNotificationBackground=()=>{};
const _clearApprovalForOwner=()=>{},_clearClarifyForOwner=()=>{},_flushReasoningToAnchor=()=>{};
const _applyToAnchor=()=>{},_scheduleAnchorRegistryCleanup=()=>{};
const _rememberRunJournalCursor=()=>{},_withDeferredAnchorScenePaint=fn=>fn;
const renderSessionList=()=>{},_setActivePaneIdleIfOwner=()=>{};
const _attachProjectedAnchorSceneToLastAssistant=()=>{};
const _carryForwardEphemeralTurnFields=(_,messages)=>messages;
const clearLiveToolCards=()=>{},_markSessionViewed=()=>{};
const renderMessages=()=>rendered++;
const _dispatchExtensionTurnLifecycle=type=>{assert.equal(type,'turn:cancel');lifecycle++;};
const _completeOwnedTerminal=()=>{};
const _closeSource=()=>{_anchorPaintGeneration++;_anchorPaintDisposed=true;delete LIVE_STREAMS.s;};
const callbacks=[];
const source={readyState:1,close(){closed++;this.readyState=2;},addEventListener(type,fn){callbacks.push({type,fn});}};
eval(extract(messageSource,'_wireSSE')); _wireSSE(source);
for(let i=0;i<10000;i++){for(const x of callbacks.filter(x=>x.type==='token')) x.fn({data:'{"text":"x"}'});advance();}
assert.equal(scans,0,'the actual token handler must not scan full history');
assert.equal(inflightScans,SPACING?10000:0,'publication follows virtual time, not parsing');
if(TAIL)for(const x of callbacks.filter(x=>x.type==='token'))x.fn({data:JSON.stringify({text:TAIL})});
for(const x of callbacks.filter(x=>x.type==='cancel')) x.fn({data:'{}'});
assert.equal(scans,TAIL?1:0,'only terminal partial-delimiter fallback may rescan');
assert.equal(_semanticState.stats.incrementalBytes,10000+TAIL.length);
assert.equal(semanticText,'x'.repeat(10000));
for(const x of callbacks.filter(x=>x.type==='stream_end')) x.fn({data:'{}'});
assert.equal(_anchorPaintDisposed,false,'auxiliary cancel must not dispose pending canonical settlement');
assert.equal(typeof resolveFetch,'function');
resolveFetch({session:{session_id:'s',messages:[{role:'assistant',content:'canonical partial',_partial:true}]}});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(S.messages[0].content,'canonical partial');
assert.equal(rendered,1);assert.equal(lifecycle,1);assert.equal(closed,1);
assert.equal(LIVE_STREAMS.s,undefined);
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace('ASSISTANT_ROW', '{}' if has_row else 'null').replace('SPACING',str(spacing)).replace('TAIL',repr(tail)))
