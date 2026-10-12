"""Mixed order domains must retain source identity without false regressions."""
import pytest


@pytest.mark.parametrize('unknown', ['opaque', None, ''])
def test_stable_identity_does_not_make_unknown_order_incrementally_safe(unknown):
    import json
    _run_node(r"""
for(const mode of ['transparent_stream','compact_worklog']){
 const identity={session_id:'s',stream_id:'run'};
 const r=api.createAssistantTurnAnchorRegistry(identity);
 function feed(id,seq){
  api.applyAssistantTurnAnchorSourceEvent(r,{source_event_type:'tool',event_id:id,seq,payload:{id,name:'read_file'}},identity);
  return api.projectAssistantTurnAnchorActivityScene(r,{mode});
 }
 feed('first',10);
 const ambiguous=feed('second',UNKNOWN);
 assert.equal(ambiguous.projection.full_rebuild,true,'stable event identity cannot prove opaque/missing sequence order');
 assert.equal(ambiguous.projection.fallback_reason,'uncertain_order');
 const regression=feed('third',9);
 assert.equal(regression.projection.full_rebuild,true,'unknown sequence must not erase numeric high-water mark');
 assert.equal(regression.activity_rows.length,3,'fail closed without dropping semantic rows');
}
""".replace('UNKNOWN', json.dumps(unknown)))

from tests.test_issue6391_dirty_projection import _run_node

def test_mixed_local_transport_order_keeps_incremental_and_detects_real_regression():
    _run_node(r"""
for(const mode of ['transparent_stream','compact_worklog']){
 const r=api.createAssistantTurnAnchorRegistry({session_id:'s',stream_id:'run'});
 const feed=(type,seq,domain,id)=>api.applyAssistantTurnAnchorSourceEvent(r,{
  source_event_type:type,seq,order_domain:domain,event_id:domain==='transport'?'run:'+seq:null,
  local_id:id,payload:{local_id:id,id,name:'read_file',text:'prose'},
 },{session_id:'s',stream_id:'run',order_domain:domain});
 feed('tool',4,'transport','t0');api.projectAssistantTurnAnchorActivityScene(r,{mode});
 feed('token',8,'local','p1');feed('tool',7,'transport','t1');
 const scene=api.projectAssistantTurnAnchorActivityScene(r,{mode});
 assert.equal(scene.projection.full_rebuild,false,'local8 -> transport7 is valid');
 assert.equal(scene.activity_rows.length,3);
 feed('tool',6,'transport','t2');
 assert.equal(api.projectAssistantTurnAnchorActivityScene(r,{mode}).projection.fallback_reason,'uncertain_order');
}
""")


def test_normalized_replay_preserves_trusted_domain_but_raw_cannot_opt_out():
    _run_node(r"""
const identity={session_id:'s',stream_id:'run'};
const r=api.createAssistantTurnAnchorRegistry(identity);
const local=api.normalizeAssistantTurnAnchorSourceEvent({source_event_type:'token',seq:8,payload:{text:'local'}},{...identity,order_domain:'local'});
const wire=api.normalizeAssistantTurnAnchorSourceEvent({source_event_type:'tool',event_id:'run:7',seq:7,payload:{id:'t',name:'read_file'}},identity);
api.applyAssistantTurnAnchorNormalizedEvent(r,local);
api.projectAssistantTurnAnchorActivityScene(r);
api.applyAssistantTurnAnchorNormalizedEvent(r,wire);
assert.equal(api.projectAssistantTurnAnchorActivityScene(r).projection_stats.full_rebuild,false);
const raw=api.createAssistantTurnAnchorRegistry(identity);
api.applyAssistantTurnAnchorSourceEvent(raw,{source_event_type:'tool',event_id:'run:8',seq:8,payload:{id:'a',name:'read_file'}},identity);
api.projectAssistantTurnAnchorActivityScene(raw);
api.applyAssistantTurnAnchorSourceEvent(raw,{source_event_type:'tool',event_id:'run:7',seq:7,order_domain:'local',payload:{id:'b',name:'read_file',order_domain:'local'}},identity);
assert.equal(api.projectAssistantTurnAnchorActivityScene(raw).projection_stats.order_uncertain,true);
""")
