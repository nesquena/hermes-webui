// Real composer code in fresh realms sharing only browser durable storage.
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8').split('const SESSION_VIEWED_COUNTS_KEY =')[0];
const wire = JSON.parse(process.argv[3] || '{}');
const storage = new Map();
function page(profile = 'default', failureStatus = 409) {
  const timers = new Map(), requests = [], ta = {value: ''};
  let id = 0, fail = true, hold = null;
  const c = vm.createContext({S: {activeProfile: profile, session: {session_id:'probe-owner'}, pendingFiles:[]},
    $: () => ta, console: {warn() {}},
    localStorage: {getItem:k=>storage.get(k)||null, setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)},
    setTimeout:(fn,delay)=>{timers.set(++id,{fn,delay});return id;}, clearTimeout:id=>timers.delete(id),
    api: async (_,opts) => {const body=JSON.parse(opts.body); requests.push(body);
      if (wire.bridge && wire.bridge-- > 0) {
        process.stdout.write(JSON.stringify({request:body})+'\n');
        let line='', byte=Buffer.alloc(1);
        while (fs.readSync(0,byte,0,1,null) && byte[0]!==10) line+=byte.toString();
        const reply=JSON.parse(line);
        if(reply.status!==200) throw Object.assign(new Error('conflict'), {status:reply.status,body:JSON.stringify(reply.body)});
        return reply.body;
      }
      if (hold) return new Promise(resolve=>{hold.resolve=resolve;});
      if (fail) throw Object.assign(new Error('transport failure'), {status:failureStatus,body:JSON.stringify({code:'draft_authority_unconfirmed'})});
      return {session_id:body.session_id,draft:body};},
  });
  vm.runInContext(source,c);
  return {run:s=>vm.runInContext(s,c), ta,timers,requests,c, succeed:()=>fail=false,
    hold:()=>{hold={};return hold;},
    tick:async()=>{const batch=[...timers];timers.clear();for(const [,t] of batch)t.fn();await flush();}};
}
async function flush(){for(let i=0;i<40;i++) await Promise.resolve();}
(async()=>{
  const first=page();
  first.run("_saveComposerDraft('probe-owner','typed while streaming',[])");
  await first.tick();
  assert.equal(first.requests.length,1);
  assert.ok(first.timers.size, 'conflict schedules backoff');
  assert.ok(storage.size, 'unacknowledged intent is durable');
  const foreign=page('bravo');
  foreign.run("_restoreComposerDraft({text:'foreign'},'probe-owner')");
  assert.equal(foreign.ta.value,'foreign');
  await foreign.tick(); assert.equal(foreign.requests.length,0);
  const reload=page();
  reload.run("_restoreComposerDraft({text:'last durable draft'},'probe-owner')");
  assert.equal(reload.ta.value,'typed while streaming','fresh realm restores pending edit');
  reload.succeed(); await reload.tick();
  assert.equal(reload.requests.at(-1).text,'typed while streaming');
  assert.equal(storage.size,0,'ack removes pending intent');
  // Timers from another realm must not replay an acknowledged or superseded edit.
  await first.tick(); assert.equal(first.requests.length,1);
  const newer=page();
  newer.run("_saveComposerDraftNow('probe-owner','older',[]).catch(()=>{})");await flush();
  const held=newer.hold(); newer.succeed(); await newer.tick();
  newer.run("_saveComposerDraft('probe-owner','newer',[])");
  held.resolve({session_id:'probe-owner'});await flush();
  const latest=page();latest.run("_restoreComposerDraft({text:'old'},'probe-owner')");
  assert.equal(latest.ta.value,'newer','stale ack cannot erase new edit');
  newer.run("_clearComposerDraft('probe-owner','newer',[])"); await flush();
  const cleared=page();cleared.run("_restoreComposerDraft({text:'old'},'probe-owner')");
  assert.equal(cleared.ta.value,'','pending clear supersedes edits after reload');
  cleared.succeed();await cleared.tick();
  assert.equal(cleared.requests.at(-1).text,'');
  // Profile switches invalidate an already scheduled retry, including same SID.
  const switched=page();switched.run("_saveComposerDraftNow('probe-owner','private',[]).catch(()=>{})");await flush();
  switched.run("S.activeProfile='bravo'");await switched.tick();
  assert.equal(switched.requests.length,1);
  switched.run("_restoreComposerDraft({text:'bravo'},'probe-owner')");
  assert.equal(switched.ta.value,'bravo');
  // Pending state exists before debounce, including continuation and attachment metadata.
  const tokenPage=page();
  tokenPage.run("_peekRestoredGoalContinuationDraft=()=>({id:'goal-token',text:'continue'}); _saveComposerDraft('other-sid','continue',[{name:'note.txt',size:3,type:'text/plain'}])");
  const tokenReload=page();
  tokenReload.run("_setRestoredGoalContinuationDraft=(id,text)=>{S.restored={id,text}}; _restoreComposerDraft({text:'stale'},'other-sid')");
  assert.equal(tokenReload.ta.value,'continue');
  assert.equal(tokenReload.run('S.restored.id'),'goal-token');
  assert.equal(tokenReload.run('S.pendingFiles.length'),0,'metadata must not impersonate live File objects');
  tokenReload.succeed();await tokenReload.tick();
  assert.equal(tokenReload.requests.at(-1).goal_continuation_id,'goal-token');
  assert.equal(tokenReload.requests.at(-1).files[0].name,'note.txt');
  const clearRetry=page();
  clearRetry.run("_clearComposerDraft('probe-owner','private',[])");await flush();
  assert.equal([...clearRetry.timers.values()][0].delay,1000);
  await clearRetry.tick();
  assert.equal([...clearRetry.timers.values()][0].delay,2000,'backoff grows between conflicts');
  clearRetry.succeed();await clearRetry.tick();
  assert.equal(clearRetry.requests.at(-1).text,'');
  const isolatedSid=page();
  isolatedSid.run("_restoreComposerDraft({text:'separate'},'another-sid')");
  assert.equal(isolatedSid.ta.value,'separate');
  for (const status of [undefined,503]) {
    const offline=page('default',status===undefined?0:status);
    offline.run("_saveComposerDraftNow('offline-sid','offline',[]).catch(()=>{})");await flush();
    assert.ok(offline.timers.size,'transient network/server failure schedules retry');
    offline.succeed();await offline.tick();
    assert.equal(offline.requests.at(-1).text,'offline');
  }
  const denied=page('default',403);
  denied.run("_saveComposerDraftNow('denied-sid','denied',[]).catch(()=>{})");await flush();
  assert.equal(denied.timers.size,0,'authorization refusals do not retry');
  process.stdout.write('durability recovery, supersession, clear, profile isolation: PASS\n');
})().catch(e=>{console.error(e);process.exitCode=1;});
