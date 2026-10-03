"""Terminal claim rejects competing late events before their handlers."""
from tests.test_issue6391_live_scene_paint import run_js

def test_registered_callbacks_cannot_override_claimed_terminal():
    run_js(r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false,_anchorPaintScheduler=null;
const LIVE_STREAMS={},activeSid='s',streamId='r';
let _terminalStateReached=true,_streamFinalized=true,finished=0,closed=0;
const _completeOwnedTerminal=()=>{finished++;_pendingTerminalFinish=null;},_closeSource=()=>closed++;
let _deferAnchorScenePaint=false;
const _rememberRunJournalCursor=()=>{throw new Error('late cursor callback');};
eval(extract(messageSource,'_withDeferredAnchorScenePaint'));
eval(extract(messageSource,'_wireSSE'));
const listeners=[];
const source={addEventListener(type,fn){listeners.push({type,fn});}};
_wireSSE(source);
for(const type of ['token','reasoning','interim_assistant','tool','tool_complete','done']){
 for(const c of listeners.filter(c=>c.type===type))c.fn({data:'invalid JSON'});
}
assert.equal(finished,0);
_pendingTerminalFinish={generation:0,finish:()=>{}};
for(const c of listeners.filter(c=>c.type==='error'))c.fn({data:'invalid JSON'});
assert.equal(finished,1);assert.equal(closed,1);
""")


def test_terminal_cursor_is_recorded_before_completion_disposes_source():
    run_js(r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false,_anchorPaintScheduler=null;
const LIVE_STREAMS={},activeSid='s',streamId='r',INFLIGHT={s:{}};
let _terminalStateReached=false,_streamFinalized=false,_deferAnchorScenePaint=false;
let _lastRunJournalSeq=0,_lastRunJournalEventId='';
const _throttledPersist=()=>{},_completeOwnedTerminal=()=>{},_closeSource=()=>{},_clearStreamEndRecovery=()=>{};
eval(extract(messageSource,'_rememberRunJournalCursor'));
eval(extract(messageSource,'_withDeferredAnchorScenePaint'));
eval(extract(messageSource,'_wireSSE'));
const listeners=[];const source={addEventListener(type,fn){listeners.push({type,fn});}};
_wireSSE(source);
const _bailOutOfTerminalEventsFromStaleStream=()=>{
 assert.equal(_lastRunJournalSeq,42);assert.equal(INFLIGHT.s.lastRunJournalSeq,42);
 _terminalStateReached=true;_anchorPaintDisposed=true;
 return true;
};
listeners.find(x=>x.type==='done').fn({lastEventId:'r:42',data:'{}'});
assert.equal(_lastRunJournalSeq,42);
""")
