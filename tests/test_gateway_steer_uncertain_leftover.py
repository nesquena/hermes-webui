"""An uncertain Gateway steer later returned as pending_steer_leftover was delivered:
neither ordering may leave it both queued and restored in the composer."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _run(scenario):
    src = (ROOT / 'static/commands.js').read_text(encoding='utf-8')
    helpers = src[src.index('let _steerUploadCache = null;'):src.index('function _steerFilesSignature(')]
    fn = src[src.index('async function _trySteer('):src.index('async function cmdTitle(')]
    pred = src[src.index('function _steerFallbackIsDeadRun('):src.index('function _steerOwnerStreamIsCurrent(')]
    harness = r'''
const assert=require('node:assert/strict');
const S={session:{session_id:'s',active_stream_id:'run'},activeStreamId:'run',pendingFiles:[]};
const inp={value:''},recovery={removed:false,remove(){this.removed=true}};
let recoveryShown=false,cleared=[];
const document={getElementById:()=>({querySelector:()=>recoveryShown&&!recovery.removed?recovery:null})};
const $=()=>inp,t=x=>x,showToast=()=>{};
const _steerTextWithPendingFiles=async x=>x,_steerOwnerIsCurrent=()=>true,_steerRestoreText=x=>x;
const _steerFailureMessageKey=x=>x,_showSteerRecovery=()=>{recoveryShown=true};
const _steerClearCurrentOwnerDeadRun=()=>false,_steerOwnerStreamIsCurrent=()=>true;
const _clearComposerDraft=(sid,text)=>{cleared.push(text)};
let releaseApi;const apiDone=new Promise(r=>releaseApi=r);
const api=async()=>{await apiDone;return {accepted:false,fallback:'gateway_steer_uncertain',stream_id:'run'}};
const visible=()=>recoveryShown&&!recovery.removed;
'''
    subprocess.run(['node', '-e', helpers + fn + pred + harness + scenario], check=True, timeout=15)


def test_leftover_before_uncertain_response_is_not_restored():
    _run(r'''(async()=>{
const p=_trySteer('guidance',true);
await new Promise(r=>setImmediate(r));
assert.equal(_steerReconcileLeftover('s','guidance'),true);
releaseApi();
assert.equal(await p,true);
assert.equal(inp.value,'');assert.equal(visible(),false);
assert.deepEqual(cleared,['guidance']);
})().catch(e=>{console.error(e);process.exit(1)});''')


@pytest.mark.parametrize('edited', [False, True])
def test_uncertain_response_before_leftover_undoes_restore(edited):
    _run(r'''(async()=>{
const p=_trySteer('guidance',true);releaseApi();
assert.equal(await p,false);
assert.equal(inp.value,'guidance');assert.equal(visible(),true);
if(%s) inp.value='newer draft';
assert.equal(_steerReconcileLeftover('s','guidance'),true);
assert.equal(visible(),false);
assert.equal(inp.value,%s);
})().catch(e=>{console.error(e);process.exit(1)});''' % ('true' if edited else 'false', "'newer draft'" if edited else "''"))


def test_unrelated_leftover_keeps_uncertain_restore():
    _run(r'''(async()=>{
const p=_trySteer('guidance',true);releaseApi();
assert.equal(await p,false);
assert.equal(_steerReconcileLeftover('s','other text'),false);
assert.equal(_steerReconcileLeftover('x','guidance'),false);
assert.equal(inp.value,'guidance');assert.equal(visible(),true);
})().catch(e=>{console.error(e);process.exit(1)});''')


def test_leftover_handler_reconciles_uncertain_steer():
    js = (ROOT / 'static/messages.js').read_text(encoding='utf-8')
    block = js[js.index("addEventListener('pending_steer_leftover'"):]
    block = block[:block.index("addEventListener('compressing'")]
    assert '_steerReconcileLeftover(sid,txt)' in block
