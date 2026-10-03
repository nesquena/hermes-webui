"""Persisted scene hydration preserves trusted local ordering provenance."""
from tests.test_issue6391_live_scene_paint import run_js

def test_real_snapshot_hydration_preserves_domains_without_trusting_wire_payload():
    run_js(r"""
require(process.argv[2].replace('messages.js','assistant_turn_anchors.js'));
const _anchorApi=globalThis.HermesAssistantTurnAnchors;
const activeSid='s',streamId='run';let _anchorShadowWarned=false;
const ctx={session_id:activeSid,stream_id:streamId};
const live=_anchorApi.createAssistantTurnAnchorRegistry(ctx);
_anchorApi.applyAssistantTurnAnchorSourceEvent(live,{source_event_type:'token',seq:8,local_id:'local:1',payload:{text:'progress'}},{...ctx,order_domain:'local'});
_anchorApi.applyAssistantTurnAnchorSourceEvent(live,{source_event_type:'tool',event_id:'run:7',seq:7,payload:{id:'t',name:'read_file'}},ctx);
const scene=JSON.parse(JSON.stringify(_anchorApi.projectAssistantTurnAnchorActivityScene(live)));
assert.equal(scene.activity_rows[0].order_domain,'local');
const _anchorRegistry=_anchorApi.createAssistantTurnAnchorRegistry(ctx);
_anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry);
eval(extract(messageSource,'_sourceEventTypeForSnapshotAnchorRow'));
eval(extract(messageSource,'_hydrateAnchorRegistryFromActivityScene'));
assert.equal(_hydrateAnchorRegistryFromActivityScene(scene),true);
const rebuilt=_anchorApi.projectAssistantTurnAnchorActivityScene(_anchorRegistry);
assert.equal(rebuilt.projection_stats.order_uncertain,false);
assert.equal(rebuilt.activity_rows[0].order_domain,'local');
assert.equal(rebuilt.activity_rows[1].order_domain,'transport');
const length=_anchorRegistry.anchor.activity_events.length;
assert.equal(_hydrateAnchorRegistryFromActivityScene(scene),true);
assert.equal(_anchorRegistry.anchor.activity_events.length,length);
""")
