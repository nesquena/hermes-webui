// Pure JavaScript behavior harness; no browser or external services.
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), crypto=require('node:crypto');
const source=fs.readFileSync('static/projects.js','utf8');
function scene(storage=new Map()){
 const x={storage,posts:[],loads:[],switches:[],document:{},_loadSessionGeneration:0,_currentPanel:'projects',console,Map,Promise,crypto,
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}};
 x.api=async(url,options)=>{
  if(url.includes('hub/detail'))return {project:{project_id:'team',collaboration:true,bot_participants:x.bots||['writer']}};
  x.posts.push(JSON.parse(options.body));
  if(x.failure)throw x.failure;
  if(x.blockPost)await new Promise(resolve=>x.finishPost=resolve);
  return {session:{session_id:'one'}};
 };
 x.loadSession=async id=>{x._loadSessionGeneration++;x.loads.push(id);if(x.blockLoad)await new Promise(resolve=>x.finishLoad=resolve)};
 x.switchPanel=name=>x.switches.push(name);
 vm.createContext(x);vm.runInContext(source,x);return x;
}
async function until(predicate){for(let i=0;i<30&&!predicate();i++)await new Promise(resolve=>setImmediate(resolve));assert.ok(predicate());}
(async()=>{
 let x=scene();x.blockPost=true;const a=x._projStartSharedConversation('team'),b=x._projStartSharedConversation('team');assert.equal(a,b);
 await until(()=>x.finishPost);assert.equal(x.posts.length,1);x.finishPost();await a;assert.deepEqual(x.loads,['one']);assert.equal(x.storage.size,0);
 x=scene();x.failure=new Error('offline');await assert.rejects(x._projStartSharedConversation('team'));
 const original=x.posts[0].request_id;const resumed=scene(x.storage);await resumed._projStartSharedConversation('team');assert.equal(resumed.posts[0].request_id,original);
 x=scene();x.failure=Object.assign(new Error('conflict'),{status:409,body:JSON.stringify({code:'project_creation_conflict'})});
 await assert.rejects(x._projStartSharedConversation('team'),/start a separate conversation/);assert.equal(x.storage.size,0);const conflicted=x.posts[0].request_id;
 x.failure=null;await x._projStartSharedConversation('team');assert.notEqual(x.posts[1].request_id,conflicted);
 x=scene();x.failure=Object.assign(new Error('unconfirmed'),{status:409,body:JSON.stringify({code:'project_creation_unconfirmed'})});await assert.rejects(x._projStartSharedConversation('team'));assert.equal(x.storage.size,1);
 x=scene();x.failure=Object.assign(new Error('denied'),{status:403});await assert.rejects(x._projStartSharedConversation('team'),/membership and bot access/);assert.equal(x.storage.size,1);
 for(const stage of ['Post','Load'])for(const navigation of ['session','panel']){
  x=scene();x['block'+stage]=true;const p=x._projStartSharedConversation('team');await until(()=>x['finish'+stage]);
  if(navigation==='session')x._loadSessionGeneration++;else x._currentPanel='settings';
  x['finish'+stage]();await p;assert.deepEqual(x.switches,[],stage+' '+navigation+' must keep newer navigation');
 }
 x=scene();x.bots=['writer','reviewer'];await x._projStartSharedConversation('team','reviewer');
 assert.deepEqual(x.posts[0].bot_participants,['reviewer','writer']);
 x=scene();await assert.rejects(x._projStartSharedConversation('team','outsider'));assert.equal(x.posts.length,0);
 x=scene();x.bots=['writer','reviewer'];x.failure=new Error('offline');
 await assert.rejects(x._projStartSharedConversation('team','writer'));
 await assert.rejects(x._projStartSharedConversation('team','reviewer'));
 assert.notEqual(x.posts[0].request_id,x.posts[1].request_id);
 console.log('12 project creation JavaScript scenarios passed');
})().catch(error=>{console.error(error);process.exitCode=1});
