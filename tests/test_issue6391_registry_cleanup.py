"""Shadow registry cleanup must not retain terminal owner/timer closures."""
from tests.test_issue6391_live_scene_paint import run_js

def test_registry_cleanup_cancels_timer_and_preserves_replacement_identity():
    run_js(r"""
let _anchorRegistryCleanupTimer=1;
const _anchorRegistry={},streamId='s';
const _anchorRegistryMap=new Map([[streamId,_anchorRegistry]]);
let cleanupCancelled=0;const clearTimeout=()=>cleanupCancelled++;
eval(extract(messageSource,'_cancelAnchorRegistryCleanup'));
_cancelAnchorRegistryCleanup();_cancelAnchorRegistryCleanup();
assert.equal(cleanupCancelled,1);assert.equal(_anchorRegistryMap.size,0);
const replacement={};_anchorRegistryMap.set(streamId,replacement);
_cancelAnchorRegistryCleanup();assert.strictEqual(_anchorRegistryMap.get(streamId),replacement);
""")


def test_recovery_rearms_matching_registry_and_cleanup_timer():
    run_js(r"""
const _anchorRegistry={},streamId='r',_anchorRegistryMap=new Map();
let _anchorRegistryCleanupTimer=null,arms=0;
const _scheduleAnchorRegistryCleanup=()=>{_anchorRegistryCleanupTimer=++arms;};
eval(extract(messageSource,'_activateAnchorRegistry'));
_activateAnchorRegistry();assert.equal(_anchorRegistryMap.get(streamId),_anchorRegistry);
const first=_anchorRegistryCleanupTimer;_anchorRegistryMap.delete(streamId);_anchorRegistryCleanupTimer=null;
_activateAnchorRegistry();assert.equal(_anchorRegistryMap.get(streamId),_anchorRegistry);
assert.ok(_anchorRegistryCleanupTimer>first);
""")
