"""Live projections preserve old snapshots without copying historical rows."""
from tests.test_issue6391_dirty_projection import _run_node


def test_projection_uses_bounded_persistent_rows_and_preserves_snapshot():
    _run_node(r"""
const r=api.createAssistantTurnAnchorRegistry({session_id:'s',stream_id:'run'});
for(let i=0;i<100;i++)api.applyAssistantTurnAnchorSourceEvent(r,{source_event_type:'tool',event_id:'run:'+i,seq:i,payload:{id:'t'+i,name:'read_file'}},{session_id:'s',stream_id:'run'});
const a=api.projectAssistantTurnAnchorActivityScene(r,{mode:'compact_worklog'});
const old=JSON.stringify(a.activity_rows);
const rows=r._activity_projection_cache?.rows;
api.applyAssistantTurnAnchorSourceEvent(r,{source_event_type:'tool',event_id:'run:100',seq:100,payload:{id:'t100',name:'read_file'}},{session_id:'s',stream_id:'run'});
const b=api.projectAssistantTurnAnchorActivityScene(r,{mode:'compact_worklog'});
assert.equal(JSON.stringify(a.activity_rows),old);
assert.equal(b._activity_rows_view.length,101);
assert.equal(b.projection_stats.historical_rows_copied,0);
assert.ok(b.projection_stats.row_index_nodes_copied<=5);
assert.equal(Array.isArray(b.activity_rows),true);
assert.equal(b.activity_rows.map(x=>x.role).length,101);
assert.equal(Object.isFrozen(b.activity_rows),true);
assert.strictEqual(b.activity_rows[0],a.activity_rows[0]);
""")
