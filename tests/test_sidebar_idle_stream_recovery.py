"""A sidebar-idle hint must retain the live handoff without waiting forever.

Drive the production scheduling helper with a fake clock and a deferred snapshot
boundary. The browser lifecycle gate separately covers the real HTTP restore.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_issue2454_active_session_spinner import _function_body

ROOT = Path(__file__).resolve().parents[1]


def _run(case):
    sessions = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    functions = "\n".join(
        f"function {name}({params}) {{" + _function_body(sessions, f"function {name}(") + "}"
        for name, params in [
            ("_isServerIdleSessionRow", "s"),
            ("_hasOwnedOpenLiveStream", "sid"),
            ("_reconcileActiveSessionIdleStateFromList", "serverRows"),
        ]
    )
    if "function _bindSidebarIdleRecovery(" in messages:
        functions += "\nfunction _bindSidebarIdleRecovery(live) {" + _function_body(
            messages, "function _bindSidebarIdleRecovery("
        ) + "}"
    script = r"""
const assert=require('assert');
const activeSid='current',streamId='turn-1';
let _terminalStateReached=false,_streamFinalized=false;
let _pendingStreamEndRecovery=CASE==='stream-end-pending';
const S={session:{session_id:activeSid},busy:true,activeStreamId:streamId};
const INFLIGHT={current:{streamId}},original=INFLIGHT.current;
const timers=[],listeners={},calls=[],statusCalls=[],failures=[];
function setTimeout(fn,delay){const t={fn,delay,cancelled:false};timers.push(t);return t;}
function clearTimeout(t){if(t)t.cancelled=true;}
const source={readyState:1,addEventListener(name,fn){(listeners[name]??=[]).push(fn);}};
const live={streamId,source},LIVE_STREAMS={current:live};
let resolveSnapshot;
async function api(path,options){
  statusCalls.push({path,options});
  if(CASE==='probe-error') throw new Error('status unavailable');
  return {active:CASE==='runtime-active'};
}
async function _restoreSettledSession(source,options){
  calls.push({source,options});
  return new Promise(resolve=>{resolveSnapshot=resolve;});
}
function _handleStreamError(s){failures.push(s);S.busy=false;S.activeStreamId=null;}
FUNCTIONS
if(typeof _bindSidebarIdleRecovery==='function')_bindSidebarIdleRecovery(live);
const idle=[{session_id:activeSid,is_streaming:false,active_stream_id:null}];
(async()=>{
  _reconcileActiveSessionIdleStateFromList(idle);
  _reconcileActiveSessionIdleStateFromList(idle);
  if(CASE==='stream-end-pending'){assert.strictEqual(timers.length,0,'existing stream-end recovery must remain the sole owner');console.log('ok');return;}
  assert.strictEqual(timers.length,1,'an OPEN transport must schedule one bounded snapshot recovery');
  assert.ok(timers[0].delay>0&&timers[0].delay<=2000);
  assert.strictEqual(S.busy,true,'the first idle hint must not destroy the live handoff');
  const mode=CASE;
  if(mode==='cancel-rearm'){
    live.cancelIdleRecovery();
    _reconcileActiveSessionIdleStateFromList(idle);
    assert.strictEqual(timers.length,2);
    await timers[0].fn();
    assert.strictEqual(timers[1].cancelled,false,'a queued old callback cannot cancel a replacement ticket');
    const replacement=timers[1].fn();
    await Promise.resolve();
    assert.strictEqual(calls.length,1);
    resolveSnapshot('active');await replacement;
    console.log('ok');return;
  }
  if(mode==='cancel-before'){
    live.cancelIdleRecovery();
    assert.strictEqual(timers[0].cancelled,true);
    console.log('ok');return;
  }
  if(mode==='replace-before')LIVE_STREAMS.current={streamId,source:{readyState:1}};
  const running=timers[0].fn();
  if(mode==='replace-before'){
    await running;assert.strictEqual(calls.length,0);console.log('ok');return;
  }
  await Promise.resolve();
  assert.strictEqual(statusCalls.length,1);
  assert.ok(statusCalls[0].path.includes('/api/chat/stream/status?stream_id=turn-1'));
  assert.deepStrictEqual(statusCalls[0].options,{timeoutMs:8000,retries:0,timeoutToast:false});
  if(mode==='runtime-active'){
    await running;
    assert.strictEqual(calls.length,0,'active runtime must not fall through to session restore');
    assert.strictEqual(failures.length,0);
    assert.strictEqual(S.busy,true);
    assert.strictEqual(INFLIGHT.current,original);
    console.log('ok');return;
  }
  if(mode==='probe-error'){
    await running;
    assert.strictEqual(calls.length,0,'failed runtime probe must not trust idle session flags');
    assert.strictEqual(failures.length,1);
    console.log('ok');return;
  }
  assert.strictEqual(calls.length,1);
  _reconcileActiveSessionIdleStateFromList(idle);
  assert.strictEqual(timers.length,1,'repeated idle hints must not restart a pending deadline');
  const owns=calls[0].options.isCurrent;
  assert.strictEqual(owns(),true);
  assert.deepStrictEqual(calls[0].options.requestOptions,{timeoutMs:8000,retries:0,timeoutToast:false});
  if(mode==='replace-during')LIVE_STREAMS.current={streamId,source:{readyState:1}};
  if(mode==='new-turn')INFLIGHT.current={streamId:null};
  if(mode==='navigate')S.session={session_id:'other'};
  if(mode==='done'){
    for(const fn of listeners.done||[])fn();
    _streamFinalized=true;S.busy=false;S.activeStreamId=null;
  }
  if(['replace-during','new-turn','navigate','done'].includes(mode))assert.strictEqual(owns(),false);
  resolveSnapshot(mode==='active'?'active':mode==='restored'?'restored':'error');
  await running;
  assert.strictEqual(failures.length,mode==='error'?1:0);
  assert.strictEqual(owns(),false,'settled requests must lose authority');
  if(mode==='active'){
    assert.strictEqual(S.busy,true);assert.strictEqual(INFLIGHT.current,original);
  }
  console.log('ok');
})().catch(e=>{console.error(e.stack||e);process.exit(1);});
""".replace("FUNCTIONS", functions).replace("CASE", json.dumps(case))
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", [
    "error", "active", "restored", "runtime-active", "probe-error", "cancel-before", "replace-before",
    "replace-during", "new-turn", "navigate", "done", "stream-end-pending", "cancel-rearm",
])
def test_sidebar_idle_open_stream_has_bounded_owner_safe_recovery(case):
    _run(case)


def test_snapshot_restore_rechecks_request_owner_after_await():
    source = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    body = _function_body(source, "async function _restoreSettledSession(")
    script = r"""
const assert=require('assert');
const activeSid='current',streamId='turn-1';
let current=true,resolveSnapshot;
const S={session:{session_id:activeSid},activeStreamId:streamId};
let _streamFinalized=false;
function _isActiveSession(){return true;}
function _closeSource(){throw Error('stale restore must not close a replacement');}
function api(){return new Promise(resolve=>{resolveSnapshot=resolve;});}
async function _restoreSettledSession(source,options=null){BODY}
(async()=>{
 const pending=_restoreSettledSession({}, {status:true,isCurrent:()=>current});
 current=false;
 resolveSnapshot({session:{session_id:activeSid,active_stream_id:'newer-turn',messages:[]}});
 assert.strictEqual(await pending,'stale','request authority must be rechecked before using the response');
 assert.strictEqual(S.activeStreamId,streamId);
 console.log('ok');
})().catch(e=>{console.error(e.stack||e);process.exit(1);});
""".replace("BODY", body)
    node = shutil.which("node")
    if not node:
        pytest.skip("node required")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
