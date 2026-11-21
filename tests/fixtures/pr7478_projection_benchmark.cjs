// Portable source-executing comparison: node <file> <checkout-root>.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=process.argv[2]||require('node:path').resolve(__dirname,'../..');
let source=fs.readFileSync(root+'/static/assistant_turn_anchors.js','utf8');
source=source.replace('function _activitySceneRow(event, index, mode){',
 'function _activitySceneRow(event, index, mode){ globalThis.rowBuilds++;');
vm.runInThisContext(source);
const api=globalThis.HermesAssistantTurnAnchors,results=[];
for(const mode of ['compact_worklog','transparent_stream']){
 for(const [size,updates,shape] of [[1000,100,'append'],[1000,100,'single'],[10000,100,'single'],[1000,100,'mixed']]){
  const r=api.createAssistantTurnAnchorRegistry({session_id:'synthetic',stream_id:'stream',run_id:'run'});
  const add=(i,type='token')=>api.applyAssistantTurnAnchorSourceEvent(r,{source_type:type,event_id:'run:'+i,seq:i,payload:type==='tool'?{name:'terminal',tid:'tool'+i,args:{command:'printf fixture'},preview:'fixture'}:{text:'synthetic prose '+i}});
  for(let i=1;i<=size;i++)add(i,shape==='mixed'&&i%2?'tool':'token');
  let inspected=0;
  r.anchor.activity_events=new Proxy(r.anchor.activity_events,{get(target,key,receiver){if(typeof key==='string'&&/^\d+$/.test(key))inspected++;return Reflect.get(target,key,receiver);}});
  const p=api.createAssistantTurnAnchorActivityProjector?api.createAssistantTurnAnchorActivityProjector(r):null;
  const project=()=>p?p.project({mode}):api.projectAssistantTurnAnchorActivityScene(r,{mode});
  globalThis.rowBuilds=0;project();
  const initial={sourceRowsInspected:inspected,projectedRowsRebuilt:rowBuilds};
  const baseFallbacks=p?p.metrics().fullFallbacks:0;
  let sourceRowsInspected=0,projectedRowsRebuilt=0;const times=[];
  for(let i=0;i<updates;i++){
   if(shape==='append'){
    const start=r.anchor.activity_events.length;add(size+i+1);if(p)p.append(start);
   }else{
    const indices=shape==='mixed'?[size-1,1,700]:[size-1];
    for(const j of indices){
     const old=r.anchor.activity_events[j];
     const next={...old,payload:{...old.payload,text:(old.payload.text||'')+' delta',snippet:'tool result '+i,done:true}};
     r.anchor.activity_events[j]=next;if(p)p.replace(j,old,next);
    }
   }
   const readsBefore=inspected,buildsBefore=rowBuilds,start=performance.now();
   const actual=project();times.push(performance.now()-start);
   sourceRowsInspected+=inspected-readsBefore;projectedRowsRebuilt+=rowBuilds-buildsBefore;
   // Independent full projection oracle is outside measured work/timing.
   assert.deepEqual(actual,api.projectAssistantTurnAnchorActivityScene(r,{mode}));
  }
  const result={mode,size,updates,shape,initial,sourceRowsInspected,projectedRowsRebuilt,
   fullProjectionFallbacks:p?p.metrics().fullFallbacks-baseFallbacks:updates,
   msTotal:times.reduce((a,b)=>a+b,0),msPerUpdate:times.reduce((a,b)=>a+b,0)/updates};
  if(p){
   assert.equal(projectedRowsRebuilt,updates*(shape==='mixed'?3:1));
   assert.equal(sourceRowsInspected,projectedRowsRebuilt);
   assert.equal(result.fullProjectionFallbacks,0);p.dispose();assert.equal(p.metrics().retainedRows,0);
  }
  results.push(result);
 }
}
console.log(JSON.stringify(results,null,2));
