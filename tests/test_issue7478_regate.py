"""Maintainer re-gate: literal delimiters and active registry leases."""
import json

import pytest

from tests.test_issue6391_live_scene_paint import run_js
from tests.test_issue7478_spaced_semantic import helpers, runtime


@pytest.mark.parametrize('chunks,content,reasoning', [
    (['Use x << 2 for a left shift.'], 'Use x << 2 for a left shift.', ''),
    (['Use x <', '<', ' 2', ' and later prose.'], 'Use x << 2 and later prose.', ''),
    (['a <<< b ', '<< c ', '<= d ', '< < e ', '< 4.'], 'a <<< b << c <= d < < e < 4.', ''),
    (['literal << ', '<thi', 'nk>secret</thi', 'nk>answer'], 'literal << answer', 'secret'),
    (['before<< ', '<fun', 'ction_calls>hidden</function_', 'calls>after'], 'before<< after', ''),
    (['before<< ', '<｜DS', 'ML｜function_calls>hidden</｜DSML｜function_calls>after'], 'before<< after', ''),
    (['before<', ' ', '4 after'], 'before< 4 after', ''),
])
def test_literal_openers_recover_without_full_history_scan(chunks, content, reasoning):
    run_js(helpers() + runtime() + r"""
let assistantText='',liveReasoningText='',reasoningText='',segmentStart=0,_anchorPaintDisposed=false;
let _semanticProseTimer=null,_semanticProseDirty=false;
const assistantRow={},activeSid='s',INFLIGHT={s:{messages:[]}},_throttledPersist=()=>{};
let prose='';const _upsertAnchorProcessProse=text=>{prose=text;};
const setTimeout=()=>1,clearTimeout=()=>{};
for(const f of ['_stripXmlToolCalls','_parseStreamState','_splitThinkFromContent','syncInflightAssistantMessage','_drainSemanticProse','_scheduleSemanticProse'])eval(extract(messageSource,f));
for(const text of CHUNKS){assistantText+=text;_scheduleSemanticProse(text);_drainSemanticProse();}
assert.equal(_semanticSnapshot().content,CONTENT);
assert.equal(_semanticSnapshot().reasoning,REASONING);
assert.equal(_semanticSnapshot().inThinking,false);
assert.equal(prose,CONTENT);
assert.equal(INFLIGHT.s.messages[0].content,CONTENT);
assert.equal(INFLIGHT.s.messages[0].reasoning||'',REASONING);
assert.equal(_semanticState.uncertain(),false);
assert.equal(_semanticState.hasPending(),false);
assert.equal(_semanticMetrics.fullParses,0);
""".replace('CHUNKS', json.dumps(chunks)).replace('CONTENT', json.dumps(content)).replace('REASONING', json.dumps(reasoning)))


def test_active_registry_deadline_extends_one_lease_and_settlement_expires():
    run_js(r"""
const activeSid='session',streamId='stream',_anchorRegistry={};
const _anchorRegistryMap=new Map([[streamId,_anchorRegistry]]);
let _anchorRegistryCleanupTimer=null,_anchorPaintDisposed=false;
const source={};const LIVE_STREAMS={[activeSid]:{streamId,source}};
let _anchorRegistryOwner=LIVE_STREAMS[activeSid];
let clock=0,id=0;const timers=new Map();
const setTimeout=(fn,ms)=>{timers.set(++id,{fn,at:clock+ms});return id;};
const clearTimeout=id=>timers.delete(id);
function advance(ms){clock+=ms;for(const [id,t] of [...timers])if(t.at<=clock){timers.delete(id);t.fn();}}
for(const f of ['_cancelAnchorRegistryCleanup','_scheduleAnchorRegistryCleanup'])eval(extract(messageSource,f));
_scheduleAnchorRegistryCleanup();
advance(599999);assert.equal(_anchorRegistryMap.get(streamId),_anchorRegistry);
for(let i=0;i<3;i++){
 advance(i?600000:1);
 assert.equal(_anchorRegistryMap.get(streamId),_anchorRegistry,'active registry must survive deadline');
 assert.equal(timers.size,1,'exactly one renewed cleanup lease');
}
const stale=timers.get(_anchorRegistryCleanupTimer).fn;
const replacement={};_anchorRegistryMap.set(streamId,replacement);
stale();assert.equal(_anchorRegistryMap.get(streamId),replacement);
assert.equal(timers.size,0);
_anchorRegistryMap.set(streamId,_anchorRegistry);_scheduleAnchorRegistryCleanup();
delete LIVE_STREAMS[activeSid];advance(600000);
assert.equal(_anchorRegistryMap.size,0);assert.equal(timers.size,0);
assert.equal(_anchorRegistryCleanupTimer,null);
""")


def test_sidebar_preserves_closed_transport_while_semantic_finish_is_owned():
    run_js(r"""
const sessionsSource=fs.readFileSync(require('node:path').join(require('node:path').dirname(process.argv[1]),'sessions.js'),'utf8');
const S={session:{session_id:'s'},activeStreamId:'r',busy:true};
let pending=true;
const LIVE_STREAMS={s:{streamId:'r',source:{readyState:2},hasPendingFinish:()=>pending}};
eval(extract(sessionsSource,'_hasOwnedOpenLiveStream'));
assert.equal(_hasOwnedOpenLiveStream('s'),true,'closed transport still has an exact semantic finish owner');
pending=false;assert.equal(_hasOwnedOpenLiveStream('s'),false,'ordinary closed transport must not suppress idle recovery');
S.activeStreamId='replacement';pending=true;
assert.equal(_hasOwnedOpenLiveStream('s'),false,'stale semantic finish does not own replacement stream');
""")
