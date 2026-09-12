"""Client budget guard executes before any anchor persistence request."""
from tests.test_issue6391_live_scene_paint import run_js

def test_oversized_scene_is_observable_without_post_or_semantic_truncation():
    run_js(r"""
let activeSid='s',streamId='run',_persistAnchorSceneWarned=false;
let calls=[],warnings=[];
const api=(url,opts)=>{calls.push({url,opts});return Promise.resolve({ok:true});};
const _anchorSceneMessageOffsetForPersist=()=>0;
const _anchorSceneAbsoluteMessageIndexForPersist=i=>i;
const _anchorSceneMessageRef=()=> 'ref';
const showToast=(...args)=>warnings.push(args);
const oldWarn=console.warn;console.warn=(...args)=>warnings.push(args);
eval(extract(messageSource,'_persistSettledAnchorScene'));
const scene={activity_rows:[{text:'界'.repeat(100000)}]};
const before=JSON.stringify(scene);
_persistSettledAnchorScene({},scene,0);
assert.equal(calls.length,0,'known oversized UTF8 scene must not be posted');
assert.equal(JSON.stringify(scene),before,'semantic scene must not be truncated');
assert.ok(warnings.length,'failure must be observable');
_persistSettledAnchorScene({}, {activity_rows:[{text:'ok'}]},0);
assert.equal(calls.length,1,'under-budget scene naturally persists');
console.warn=oldWarn;
""")


def test_server_exponent_padding_is_budgeted_without_counting_string_text():
    run_js(r"""
let activeSid='s',streamId='run',_persistAnchorSceneWarned=false;
const calls=[],warnings=[];
const api=(url,opts)=>{calls.push(opts);return Promise.resolve({});};
const _anchorSceneMessageOffsetForPersist=()=>0;
const _anchorSceneAbsoluteMessageIndexForPersist=(i)=>i;
const _anchorSceneMessageRef=()=>({});
const showToast=()=>{};
console.warn=(...args)=>warnings.push(args);
eval(extract(messageSource,'_persistSettledAnchorScene'));
for(const number of [1e-7,-2.5e-8,9.1e-9]){
  const scene={version:'activity_scene_v1',mode:'compact_worklog',activity_rows:[{role:'prose',payload:{text:'',numbers:Array(100).fill(number)}}]};
  scene.activity_rows[0].payload.text='x'.repeat(255999-new TextEncoder().encode(JSON.stringify(scene)).length);
  const original=JSON.stringify(scene),message={};calls.length=0;
  _persistSettledAnchorScene(message,scene,0);
  assert.equal(calls.length,0,'Python pads single-digit exponents, making this scene over the endpoint cap');
  assert.equal(message._anchor_scene_persistence_status,'over_budget');
  assert.equal(JSON.stringify(scene),original,'semantic state must not be truncated');
  scene.activity_rows[0].payload.text=scene.activity_rows[0].payload.text.slice(0,-100);
  _persistSettledAnchorScene({},scene,0);assert.equal(calls.length,1,'under-budget scientific numbers still persist');
}
const stringScene={version:'activity_scene_v1',activity_rows:[{role:'prose',payload:{text:'1e-7 \\" -2e-8 '.repeat(12000)}}]};
const pad=255999-new TextEncoder().encode(JSON.stringify(stringScene)).length;
stringScene.activity_rows[0].payload.text+='x'.repeat(pad);calls.length=0;
_persistSettledAnchorScene({},stringScene,0);
assert.equal(calls.length,1,'numeric-looking text inside JSON strings must not consume numeric padding');
""")
