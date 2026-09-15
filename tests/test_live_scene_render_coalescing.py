"""Behavior regression for live anchor-scene paint coalescing.

``reasoning`` SSE events arrive once per provider reasoning delta. Every delta
used to re-project and re-paint the whole live activity scene through
``_renderAnchorLiveScene`` (each paint rebuilds the live activity rows and runs
two forced-layout scroll restores), while the prose path already capped itself at
~15fps via ``_scheduleRender``. On a long transcript that asymmetry saturated the
main thread (a reported run measured 2,874 long tasks and 37s of blocked
main-thread time).

``_renderAnchorLiveScene`` is the single entry every streaming caller funnels
through, so it is the chokepoint that must bound the paint rate. This drives the
real functions from ``static/messages.js`` in node against a fake clock/timer
queue, so the assertions are about observable paint counts for a burst of deltas,
not about source text.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _run_live_scene_coalescing() -> dict:
    assert NODE, "node is required for the live anchor scene behavior test"
    env = os.environ.copy()
    env.setdefault("LIVE_SCENE_MESSAGES_JS", str(ROOT / "static" / "messages.js"))
    result = subprocess.run(
        [NODE, "-e", _NODE_SCRIPT],
        env=env,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_burst_of_reasoning_deltas_paints_the_live_scene_once():
    result = _run_live_scene_coalescing()
    # Ownership must still be reported so reasoning deltas never fall back to a
    # second visible surface while a paint is queued.
    assert result["ownershipReturned"] is True
    # 50 deltas inside one frame budget: at most a leading paint...
    assert result["paintsDuringBurst"] <= 1
    # ...and exactly one trailing paint, so the newest delta is still rendered.
    assert result["paintsAfterTrailingDrain"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_coalesced_paint_is_not_a_dead_throttle():
    result = _run_live_scene_coalescing()
    # A later burst, past the budget, must paint again.
    assert result["paintsInSecondBurst"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_queued_live_scene_paint_is_released_on_stream_exit():
    result = _run_live_scene_coalescing()
    # A paint queued before settlement must never land on the settled turn.
    # Settlement is the scene-settled flag (not bare _streamFinalized, which
    # flips before the `done` fade drain so drain paints stay live).
    assert result["paintsAfterSettle"] == 0
    assert result["paintsAfterCancel"] == 0
    # Same guarantee for the teardown path the terminal handlers actually call.
    assert result["paintsAfterTeardownCancel"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cancel_only_touches_the_matching_timer_api():
    result = _run_live_scene_coalescing()
    # An unrelated rAF sharing the pending timeout's numeric ID must survive.
    assert result["unrelatedRafSurvivesCancel"] is True
    # An unrelated timeout sharing the pending rAF's numeric ID must survive.
    assert result["unrelatedTimeoutSurvivesCancel"] is True
    # The cancelled scene paint itself still never lands.
    assert result["paintsAfterCollisionCancel"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_done_fade_drain_still_paints_newest_prose():
    result = _run_live_scene_coalescing()
    # `done` flips _streamFinalized before the fade drain; the drain's
    # _upsertAnchorProcessProse calls must still reach the visible row.
    assert result["paintsAfterFinalizedDrain"] == 1
    assert result["drainCoalescedBurstPaints"] == 1
    assert result["drainPendingReleased"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_terminal_settlement_cannot_recreate_scene_paint():
    result = _run_live_scene_coalescing()
    # done / apperror / cancel / recovery all settle before replacing the DOM:
    # a paint scheduled after settlement must not recreate a timeout/rAF.
    assert result["settleRecreatePaints"] == 0
    assert result["settleRecreatePending"] is False
    for key in (
        "settleDonePaints",
        "settleApperrorPaints",
        "settleCancelPaints",
        "settleRecoveryPaints",
    ):
        assert result[key] == 0, key
    for key in (
        "settleDonePending",
        "settleApperrorPending",
        "settleCancelPending",
        "settleRecoveryPending",
    ):
        assert result[key] is False, key


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_scene_callback_cannot_paint_into_replacement_stream():
    result = _run_live_scene_coalescing()
    # A timeout-phase callback queued for stream-1 must not paint after the
    # session moves to stream-2.
    assert result["paintsAfterStreamSwitch"] == 0
    assert result["streamSwitchPendingReleased"] is True
    # Same for an rAF-phase callback.
    assert result["paintsAfterRafStreamSwitch"] == 0
    assert result["rafStreamSwitchPendingReleased"] is True
    # The replacement stream itself must still be able to paint.
    assert result["replacementStreamPaints"] == 1


_NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.env.LIVE_SCENE_MESSAGES_JS, 'utf8');

function extractFunc(name){
  const marker = new RegExp('function\\s+' + name + '\\s*\\(');
  const m = marker.exec(src);
  if(!m) return null;
  let i = src.indexOf('{', m.index) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}') depth -= 1;
    i += 1;
  }
  return src.slice(m.index, i);
}

const clock = { t: 0 };
const timeouts = [];
const rafs = [];
let nextTimeoutId = 1;
let nextRafId = 1;
let nextSeq = 1;
const sandbox = { console };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
// Fake clock + timer queues: requestAnimationFrame is a same-tick timer so the
// burst/leading/trailing edges are deterministic across runs. Timeout and rAF
// IDs come from independent counters — a real browser keeps separate ID spaces,
// so a cancel must only ever touch the matching queue.
sandbox.performance = { now: () => clock.t };
sandbox.setTimeout = (cb, ms) => { const id = nextTimeoutId++; timeouts.push({ kind: 'timeout', id, seq: nextSeq++, cb, at: clock.t + (Number(ms) || 0) }); return id; };
sandbox.clearTimeout = (id) => { const i = timeouts.findIndex(t => t.id === id); if(i >= 0) timeouts.splice(i, 1); };
sandbox.requestAnimationFrame = (cb) => { const id = nextRafId++; rafs.push({ kind: 'raf', id, seq: nextSeq++, cb, at: clock.t }); return id; };
sandbox.cancelAnimationFrame = (id) => { const i = rafs.findIndex(t => t.id === id); if(i >= 0) rafs.splice(i, 1); };
sandbox.drain = () => {
  for(let guard = 0; guard < 5000; guard += 1){
    let due = null;
    for(const t of timeouts){
      if(t.at <= clock.t && (!due || t.at < due.at || (t.at === due.at && t.seq < due.seq))) due = t;
    }
    for(const t of rafs){
      if(t.at <= clock.t && (!due || t.at < due.at || (t.at === due.at && t.seq < due.seq))) due = t;
    }
    if(!due) return;
    (due.kind === 'timeout' ? timeouts : rafs).splice((due.kind === 'timeout' ? timeouts : rafs).indexOf(due), 1);
    due.cb();
  }
  throw new Error('timer drain did not settle');
};
sandbox.advance = (ms) => { clock.t += ms; };
vm.createContext(sandbox);

// Production keeps these bindings in closure scope; extracting the functions
// evaluates them as globals, so the harness owns the shared state.
// _anchorScenePaintHandle is kept declared so the harness also runs against the
// pre-fix single-handle implementation — the collision assertions below fail
// there, which is the regression this file guards.
// _anchorSceneSettled / S / _testSessionActive model the terminal lifecycle:
// _streamFinalized flips on terminal-event arrival (drain still live), while
// _anchorSceneSettled flips only immediately before the settled DOM replacement.
vm.runInContext(
  'var _anchorScenePaintHandle=null; var _anchorSceneTimeoutHandle=null; var _anchorSceneRafHandle=null; var _anchorSceneLastPaintMs=0;' +
  'var _anchorScenePendingReasoning=null; var _anchorSceneSettled=false;' +
  'var _streamFinalized=false; var _anchorShadowWarned=false;' +
  'var _pendingRafHandle=null; var _renderPending=false;' +
  'var _anchorRegistry={}; var streamId="stream-1"; var activeSid="sid-1";' +
  'var S={activeStreamId:"stream-1",session:{session_id:"sid-1"}}; var _testSessionActive=true;',
  sandbox
);
vm.runInContext(
  'function _isActiveSession(){ return _testSessionActive!==false && S && S.session && S.session.session_id==="sid-1"; }' +
  'function _anchorSceneActiveMode(){return "transparent_stream";}' +
  'function _shouldUseLiveProseFade(){return false;}' +
  'function isLiveAnchorActivitySceneOwner(){return true;}',
  sandbox
);
sandbox.paints = 0;
sandbox._renderLiveAnchorActivitySceneForStream = () => { sandbox.paints += 1; return true; };

const loaded = [];
for(const name of [
  '_anchorScenePaintIntervalMs',
  '_isAnchorScenePaintLive',
  '_settleAnchorScenePaint',
  '_cancelPendingAnchorScenePaint',
  '_paintAnchorLiveScene',
  '_scheduleAnchorSceneRender',
  '_renderAnchorLiveScene',
  '_cancelAnimationFramePendingStreamRender',
]){
  const fn = extractFunc(name);
  if(!fn) continue;
  vm.runInContext(fn, sandbox, { filename: 'messages.js:' + name });
  loaded.push(name);
}
if(!loaded.includes('_renderAnchorLiveScene')) throw new Error('_renderAnchorLiveScene not found in static/messages.js');
// Back-compat shims so this harness also runs observably against the pre-fix
// implementation: without the split settled guard, _streamFinalized alone gates
// the fire() callback and there is no settle helper. The new drain/settle
// assertions below fail there, which is the lifecycle regression they guard.
if(!loaded.includes('_isAnchorScenePaintLive')){
  vm.runInContext('function _isAnchorScenePaintLive(){ return !_streamFinalized && _isActiveSession(); }', sandbox);
}
if(!loaded.includes('_settleAnchorScenePaint')){
  vm.runInContext('function _settleAnchorScenePaint(){ _streamFinalized=true; if(typeof _cancelPendingAnchorScenePaint==="function") _cancelPendingAnchorScenePaint(); }', sandbox);
}

const render = sandbox._renderAnchorLiveScene;
const result = {};
const pendingSceneHandles = () => vm.runInContext(
  '(_anchorSceneTimeoutHandle!==null||_anchorSceneRafHandle!==null||(typeof _anchorScenePaintHandle!=="undefined"&&_anchorScenePaintHandle!==null))',
  sandbox
);
const resetScene = (opts={}) => {
  clock.t = opts.t ?? 0;
  nextTimeoutId = 1; nextRafId = 1; nextSeq = 1;
  timeouts.length = 0; rafs.length = 0;
  vm.runInContext(
    '_anchorSceneLastPaintMs=0; _anchorScenePendingReasoning=null; _anchorSceneSettled=false; _streamFinalized=false;' +
    'S.activeStreamId="stream-1"; S.session={session_id:"sid-1"}; _testSessionActive=true; streamId="stream-1";' +
    'if(typeof _anchorSceneTimeoutHandle!=="undefined") _anchorSceneTimeoutHandle=null;' +
    'if(typeof _anchorSceneRafHandle!=="undefined") _anchorSceneRafHandle=null;' +
    'if(typeof _anchorScenePaintHandle!=="undefined") _anchorScenePaintHandle=null;',
    sandbox
  );
  if(opts.streamId) vm.runInContext('streamId=' + JSON.stringify(opts.streamId) + ';', sandbox);
  if(opts.activeStreamId) vm.runInContext('S.activeStreamId=' + JSON.stringify(opts.activeStreamId) + ';', sandbox);
};

const burst = (count, msPerDelta) => {
  const owned = [];
  for(let i = 0; i < count; i += 1){
    owned.push(render() === true);
    clock.t += msPerDelta;
  }
  return owned;
};

// 1. A burst of reasoning deltas inside one frame budget.
resetScene();
const first = sandbox.paints;
result.ownershipReturned = burst(50, 1).every(Boolean);
result.paintsDuringBurst = sandbox.paints - first;
sandbox.advance(200);
sandbox.drain();
result.paintsAfterTrailingDrain = sandbox.paints - first;

// 2. A later burst, past the budget, must paint again.
const second = sandbox.paints;
burst(50, 1);
sandbox.advance(200);
sandbox.drain();
result.paintsInSecondBurst = sandbox.paints - second;

// 3. A paint queued before settlement must not land. Settlement is the
// scene-settled flag: every terminal path settles immediately before replacing
// the DOM, so a queued paint is released and no new paint can be scheduled.
resetScene();
const third = sandbox.paints;
render();
vm.runInContext('_settleAnchorScenePaint()', sandbox);
sandbox.advance(200);
sandbox.drain();
result.paintsAfterSettle = sandbox.paints - third;

// 4. An explicit cancel releases the queued paint as well.
resetScene();
const fourth = sandbox.paints;
render();
if(typeof sandbox._cancelPendingAnchorScenePaint === 'function') sandbox._cancelPendingAnchorScenePaint();
sandbox.advance(200);
sandbox.drain();
result.paintsAfterCancel = sandbox.paints - fourth;

// 5. The teardown entry the terminal handlers call releases it too.
resetScene();
const fifth = sandbox.paints;
render();
vm.runInContext('_cancelAnimationFramePendingStreamRender()', sandbox);
sandbox.advance(200);
sandbox.drain();
result.paintsAfterTeardownCancel = sandbox.paints - fifth;

// 6. Cancelling a pending scene paint must only touch the matching timer API.
// Timeout and rAF IDs are opaque and not collision-free across namespaces, so
// an unrelated timer/frame sharing the pending handle's numeric ID must survive.
const sixth = sandbox.paints;
let unrelatedRafFired = false;
let unrelatedTimeoutFired = false;
// 6a. Pending paint sits in the timeout phase; an unrelated rAF shares its ID.
resetScene();
sandbox.requestAnimationFrame(() => { unrelatedRafFired = true; }); // rAF id 1
render(); // timeout phase: waitMs = 66 - 0 > 0 → timeout id 1 (collides)
sandbox._cancelPendingAnchorScenePaint();
sandbox.advance(200);
sandbox.drain();
result.unrelatedRafSurvivesCancel = unrelatedRafFired === true;
// 6b. Pending paint sits in the rAF phase; an unrelated timeout shares its ID.
resetScene();
sandbox.setTimeout(() => { unrelatedTimeoutFired = true; }, 200); // timeout id 1
sandbox.advance(100); // past the 66ms budget → direct rAF, rAF id 1 (collides)
render();
sandbox._cancelPendingAnchorScenePaint();
sandbox.advance(200);
sandbox.drain();
result.unrelatedTimeoutSurvivesCancel = unrelatedTimeoutFired === true;
result.paintsAfterCollisionCancel = sandbox.paints - sixth;

// 7. The `done` fade drain must still paint the newest prose. `done` flips
// _streamFinalized before the drain, but the scene stays live until settlement:
// each drain step calls _upsertAnchorProcessProse → _renderAnchorLiveScene.
resetScene();
const seventh = sandbox.paints;
render();
sandbox._streamFinalized = true;
sandbox.advance(200);
sandbox.drain();
result.paintsAfterFinalizedDrain = sandbox.paints - seventh;
result.drainPendingReleased = pendingSceneHandles() === false;
// A burst arriving during the drain (still finalized, not yet settled) still
// coalesces to exactly one trailing paint.
resetScene();
vm.runInContext('_streamFinalized=true;', sandbox);
const drainBurstBase = sandbox.paints;
burst(5, 1);
sandbox.advance(200);
sandbox.drain();
result.drainCoalescedBurstPaints = sandbox.paints - drainBurstBase;

// 8. Terminal settlement cannot recreate a timeout/rAF after teardown. Each
// terminal path (done / apperror / cancel / recovery) settles immediately
// before replacing the DOM; a later _renderAnchorLiveScene (e.g. via
// _applyToAnchor('done')) must not schedule new work.
const settleOnce = (label) => {
  resetScene();
  const base = sandbox.paints;
  render();
  vm.runInContext('_settleAnchorScenePaint()', sandbox);
  // Terminal handlers call _applyToAnchor after settling, which funnels through
  // _renderAnchorLiveScene — simulate that post-settlement scheduling attempt.
  // The contract is zero new handles *immediately* (not just zero paints after
  // the drain): the old code scheduled a guarded no-op callback here, leaving
  // a pending timeout/rAF after teardown.
  const recreated = render();
  const pendingImmediately = pendingSceneHandles() === true;
  sandbox.advance(200);
  sandbox.drain();
  const pendingAfterDrain = pendingSceneHandles() === true;
  return { paints: sandbox.paints - base, pending: pendingImmediately || pendingAfterDrain, recreated };
};
{
  const r = settleOnce('done');
  result.settleRecreatePaints = r.paints;
  result.settleRecreatePending = r.pending;
  result.settleDonePaints = r.paints;
  result.settleDonePending = r.pending;
}
for(const label of ['Apperror', 'Cancel', 'Recovery']){
  const r = settleOnce(label);
  result['settle' + label + 'Paints'] = r.paints;
  result['settle' + label + 'Pending'] = r.pending;
}

// 9. A stale callback cannot paint into a replacement stream. The timeout
// handoff and the rAF callback must reject work whose stream no longer owns
// the session (S.activeStreamId !== streamId).
resetScene();
const ninth = sandbox.paints;
render(); // timeout-phase paint for stream-1
vm.runInContext('S.activeStreamId="stream-2";', sandbox);
sandbox.advance(200);
sandbox.drain();
result.paintsAfterStreamSwitch = sandbox.paints - ninth;
result.streamSwitchPendingReleased = pendingSceneHandles() === false;
resetScene();
sandbox.advance(100); // past the budget → direct rAF
const ninthRaf = sandbox.paints;
render(); // rAF-phase paint for stream-1
vm.runInContext('S.activeStreamId="stream-2";', sandbox);
sandbox.advance(200);
sandbox.drain();
result.paintsAfterRafStreamSwitch = sandbox.paints - ninthRaf;
result.rafStreamSwitchPendingReleased = pendingSceneHandles() === false;
// The replacement stream itself must still paint.
timeouts.length = 0; rafs.length = 0;
clock.t = 0; nextTimeoutId = 1; nextRafId = 1; nextSeq = 1;
vm.runInContext(
  '_anchorSceneLastPaintMs=0; _anchorScenePendingReasoning=null; _anchorSceneSettled=false; _streamFinalized=false;' +
  'streamId="stream-2"; S.activeStreamId="stream-2";',
  sandbox
);
const tenth = sandbox.paints;
render();
sandbox.advance(200);
sandbox.drain();
result.replacementStreamPaints = sandbox.paints - tenth;

console.log(JSON.stringify(result));
"""
