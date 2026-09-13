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
    # A paint queued before `done`/cancel must never land on the settled turn.
    assert result["paintsAfterFinalize"] == 0
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
vm.runInContext(
  'var _anchorScenePaintHandle=null; var _anchorSceneTimeoutHandle=null; var _anchorSceneRafHandle=null; var _anchorSceneLastPaintMs=0;' +
  'var _streamFinalized=false; var _anchorShadowWarned=false;' +
  'var _pendingRafHandle=null; var _renderPending=false;' +
  'var _anchorRegistry={}; var streamId="stream-1"; var activeSid="sid-1";',
  sandbox
);
vm.runInContext(
  'function _isActiveSession(){return true;}' +
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

const render = sandbox._renderAnchorLiveScene;
const result = {};

const burst = (count, msPerDelta) => {
  const owned = [];
  for(let i = 0; i < count; i += 1){
    owned.push(render() === true);
    clock.t += msPerDelta;
  }
  return owned;
};

// 1. A burst of reasoning deltas inside one frame budget.
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

// 3. A paint queued before the stream finalizes must not land.
const third = sandbox.paints;
render();
sandbox._streamFinalized = true;
sandbox.advance(200);
sandbox.drain();
result.paintsAfterFinalize = sandbox.paints - third;
sandbox._streamFinalized = false;

// 4. An explicit cancel releases the queued paint as well.
const fourth = sandbox.paints;
render();
if(typeof sandbox._cancelPendingAnchorScenePaint === 'function') sandbox._cancelPendingAnchorScenePaint();
sandbox.advance(200);
sandbox.drain();
result.paintsAfterCancel = sandbox.paints - fourth;

// 5. The teardown entry the terminal handlers call releases it too.
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
clock.t = 0;
nextTimeoutId = 1; nextRafId = 1; nextSeq = 1;
vm.runInContext('_anchorSceneLastPaintMs=0;', sandbox);
sandbox.requestAnimationFrame(() => { unrelatedRafFired = true; }); // rAF id 1
render(); // timeout phase: waitMs = 66 - 0 > 0 → timeout id 1 (collides)
sandbox._cancelPendingAnchorScenePaint();
sandbox.advance(200);
sandbox.drain();
result.unrelatedRafSurvivesCancel = unrelatedRafFired === true;
// 6b. Pending paint sits in the rAF phase; an unrelated timeout shares its ID.
clock.t = 0;
nextTimeoutId = 1; nextRafId = 1; nextSeq = 1;
vm.runInContext('_anchorSceneLastPaintMs=0;', sandbox);
sandbox.setTimeout(() => { unrelatedTimeoutFired = true; }, 200); // timeout id 1
sandbox.advance(100); // past the 66ms budget → direct rAF, rAF id 1 (collides)
render();
sandbox._cancelPendingAnchorScenePaint();
sandbox.advance(200);
sandbox.drain();
result.unrelatedTimeoutSurvivesCancel = unrelatedTimeoutFired === true;
result.paintsAfterCollisionCancel = sandbox.paints - sixth;

console.log(JSON.stringify(result));
"""
