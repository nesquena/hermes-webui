"""Live projection lifetime is bounded by explicit stream ownership."""
from tests.test_issue6391_live_scene_paint import run_js


def test_many_live_identities_release_and_rebuild():
    run_js(r"""
const _anchorSceneRenderProjectionCaches=new Map();
for(const f of ['_anchorSceneRenderCacheKey','_anchorSceneSourceRows','_anchorSceneRenderOutputKey','_anchorSceneRememberFullProjection','_releaseAnchorSceneRenderProjections']) eval(extract(src,f));
let released=0,peakRows=0;
for(let i=0;i<200;i++){
 const rows=Array.from({length:100},(_,n)=>({role:'prose',row_id:'p'+n,text:'x'.repeat(512)+n}));
 const scene={identity:{session_id:'s'+i,stream_id:'r'+i,turn_id:'t'+i},projection:{revision:1},activity_rows:rows};
 _anchorSceneRememberFullProjection(scene,{settled:false},rows);
 assert.equal(_anchorSceneRenderProjectionCaches.size,1);
 const entry=[..._anchorSceneRenderProjectionCaches.values()][0];
 peakRows=Math.max(peakRows,entry.sourceCount);
 assert.equal(entry.sessionId,'s'+i);
 released+=_releaseAnchorSceneRenderProjections('s'+i,'r'+i);
 assert.equal(_anchorSceneRenderProjectionCaches.size,0);
 _anchorSceneRememberFullProjection(scene,{settled:false},rows);
 assert.equal(_anchorSceneRenderProjectionCaches.size,1,'revisit rebuilds released identity');
 _anchorSceneRememberFullProjection(scene,{settled:true},rows);
 assert.equal(_anchorSceneRenderProjectionCaches.size,0,'settled projection evicts live identity');
}
assert.equal(released,200);assert.equal(peakRows,100);
""")


def test_ownerless_projection_is_not_retained():
    run_js(r"""
const _anchorSceneRenderProjectionCaches=new Map();
for(const f of ['_anchorSceneRenderCacheKey','_anchorSceneSourceRows','_anchorSceneRenderOutputKey','_anchorSceneRememberFullProjection']) eval(extract(src,f));
for(const identity of [{session_id:'s'}, {session_id:'s',turn_id:'t'}, {stream_id:'r',turn_id:'t'}]){
 const scene={identity,projection:{revision:1},activity_rows:[{role:'prose',row_id:'p',text:'text'}]};
 _anchorSceneRememberFullProjection(scene,{settled:false},scene.activity_rows);
}
assert.equal(_anchorSceneRenderProjectionCaches.size,0,'cache admission requires a releasable live owner');
""")


def test_settled_projections_are_not_retained():
    run_js(r"""
const _anchorSceneRenderProjectionCaches=new Map();
for(const f of ['_anchorSceneRenderCacheKey','_anchorSceneSourceRows','_anchorSceneRenderOutputKey','_anchorSceneRememberFullProjection']) eval(extract(src,f));
const scene={identity:{session_id:'s',stream_id:'r',turn_id:'t'},projection:{revision:1},activity_rows:[]};
_anchorSceneRememberFullProjection(scene,{settled:true},[]);
assert.equal(_anchorSceneRenderProjectionCaches.size,0);
""")
