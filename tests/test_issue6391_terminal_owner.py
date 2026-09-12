"""Required terminal completion is claimed once before transport disposal."""
from tests.test_issue6391_live_scene_paint import run_js


def test_terminal_claim_is_exactly_once_and_stale_generation_cannot_finish():
    run_js(r"""
let _anchorPaintDisposed=false,_anchorPaintGeneration=0;
_pendingTerminalFinish=null;_terminalFadeTimer=null;_terminalFadeFrame=null;
let finished=0,cleared=0;
const clearTimeout=()=>cleared++,cancelAnimationFrame=()=>cleared++;
eval(extract(messageSource,'_completeOwnedTerminal'));
_pendingTerminalFinish={generation:0,finish:()=>finished++};
_terminalFadeTimer=1;_terminalFadeFrame=2;
_completeOwnedTerminal();_completeOwnedTerminal();
assert.equal(finished,1);assert.equal(cleared,2);
assert.equal(_pendingTerminalFinish,null);
assert.equal(_terminalFadeTimer,null);assert.equal(_terminalFadeFrame,null);
_pendingTerminalFinish={generation:0,finish:()=>finished++};
_anchorPaintGeneration=1;
_completeOwnedTerminal();assert.equal(finished,1);
""")
