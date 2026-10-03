"""Recovery scheduling is protected even after a timer dispatches."""
from tests.test_issue6391_live_scene_paint import run_js

def test_dispatched_recovery_cannot_run_in_replacement_generation():
    run_js(r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false;
let _streamEndRecoveryTimer=null,_pendingStreamEndRecovery=false;
let count=0,tid=0;const callbacks=[];
const clearTimeout=()=>{},setTimeout=fn=>{callbacks.push(fn);return ++tid;};
const _runStreamEndRecovery=()=>count++;
eval(extract(messageSource,'_scheduleStreamEndRecovery'));
_scheduleStreamEndRecovery({});_anchorPaintGeneration++;
_scheduleStreamEndRecovery({});const replacement=_streamEndRecoveryTimer;
callbacks[0]();assert.equal(count,0);assert.equal(_streamEndRecoveryTimer,replacement);
callbacks[1]();assert.equal(count,1);
""")
