"""Lazy live view must not replace the public dense activity_rows contract."""
from tests.test_issue6391_dirty_projection import _run_node

def test_live_view_and_public_array_have_separate_materialization_contracts():
    _run_node(r"""
const r=api.createAssistantTurnAnchorRegistry({session_id:'s',stream_id:'run'});
api.applyAssistantTurnAnchorSourceEvent(r,{type:'token',seq:1,payload:{text:'one'}});
const scene=api.projectAssistantTurnAnchorActivityScene(r,{mode:'transparent_stream'});
assert.ok(scene._activity_rows_view);
assert.equal(scene.projection_stats.historical_rows_copied,0);
assert.equal(scene._activity_rows_view.length,1);
assert.equal(scene.projection_stats.historical_rows_copied,0);
const rows=scene.activity_rows;
assert.deepEqual(Object.keys(rows),['0']);assert.equal(Object.hasOwn(rows,0),true);
assert.equal(Object.entries(rows).length,1);assert.ok(Object.isFrozen(rows));
assert.equal(rows,scene.activity_rows);
assert.equal(scene.projection_stats.historical_rows_copied,1);
assert.equal(Object.hasOwn(JSON.parse(JSON.stringify(scene)),'_activity_rows_view'),false);
assert.deepEqual(JSON.parse(JSON.stringify(rows)),JSON.parse(JSON.stringify(scene)).activity_rows);
""")


def test_clean_projection_reuses_private_row_identity_without_weakening_guard():
    _run_node(r"""
const r=api.createAssistantTurnAnchorRegistry({session_id:'s',stream_id:'run'});
api.applyAssistantTurnAnchorSourceEvent(r,{source_event_type:'tool',event_id:'run:1',seq:1,payload:{id:'t',name:'read_file'}});
const a=api.projectAssistantTurnAnchorActivityScene(r);
const b=api.projectAssistantTurnAnchorActivityScene(r);
assert.equal(b.projection_stats.rows_rebuilt,0);
assert.strictEqual(b._activity_rows_view,a._activity_rows_view);
assert.equal(b.projection_stats.historical_rows_copied,0);
""")
