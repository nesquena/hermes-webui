"""Already dispatched timers cannot mutate a superseding stream generation."""
from tests.test_issue6391_live_scene_paint import run_js


def test_dispatched_snapshot_and_persist_cannot_clear_new_timer_or_mutate():
    run_js(r"""
let _anchorPaintGeneration=0,_anchorPaintDisposed=false;
let _snapshotLiveTurnTimer=null,_persistTimer=null;
let snapshots=0,persists=0,id=0;const timers=new Map();
const setTimeout=fn=>{timers.set(++id,fn);return id;};
const snapshotLiveTurn=()=>snapshots++;
const persistInflightState=()=>persists++;
eval(extract(messageSource,'_throttledSnapshotLiveTurn'));
eval(extract(messageSource,'_throttledPersist'));
_throttledSnapshotLiveTurn();_throttledPersist();
const stale=[...timers.values()];
_anchorPaintGeneration++;_snapshotLiveTurnTimer=null;_persistTimer=null;
_throttledSnapshotLiveTurn();_throttledPersist();
const pending=[_snapshotLiveTurnTimer,_persistTimer];
for(const fn of stale)fn();
assert.equal(snapshots,0);assert.equal(persists,0);
assert.deepEqual([_snapshotLiveTurnTimer,_persistTimer],pending);
for(const key of pending)timers.get(key)();
assert.equal(snapshots,1);assert.equal(persists,1);
""")
