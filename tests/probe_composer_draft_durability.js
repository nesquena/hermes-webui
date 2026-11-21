// Real composer code in fresh realms sharing only browser durable storage.
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const path = require('node:path');
const sessions = fs.readFileSync(process.argv[2], 'utf8');
const commands = fs.readFileSync(path.join(path.dirname(process.argv[2]), 'commands.js'), 'utf8');
const between = (src, start, end) => src.slice(src.indexOf(start), src.indexOf(end, src.indexOf(start)));
const source = [
  sessions.split('const SESSION_VIEWED_COUNTS_KEY =')[0],
  between(sessions, 'async function _switchProfileForSessionLoad(', 'async function loadSession('),
  between(commands, 'function _steerOwnerIsCurrent(', 'function _steerFallbackIsDeadRun('),
  between(commands, 'let _steerUploadCache =', 'function _steerFilesSignature('),
].join('\n');
const wire = JSON.parse(process.argv[3] || '{}');
const storage = new Map();
function page(profile = 'default', failureStatus = 409) {
  const timers = new Map(), requests = [], ta = {value: ''};
  let id = 0, fail = true, hold = null;
  const c = vm.createContext({S: {activeProfile: profile, session: {session_id:'probe-owner'}, pendingFiles:[]},
    $: () => ta, console: {warn() {}}, document: {getElementById:()=>null},
    localStorage: {getItem:k=>storage.get(k)||null, setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)},
    setTimeout:(fn,delay)=>{timers.set(++id,{fn,delay});return id;}, clearTimeout:id=>timers.delete(id),
    api: async (url,opts) => {const body=JSON.parse(opts.body);
      if (url==='/api/profile/switch') return {active:body.name};
      assert.equal(url,'/api/session/draft'); requests.push(body);
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
  for (const editOtherProfile of [false, true]) {
    storage.clear();
    const saving=page(), ack=saving.hold();
    saving.run("_saveComposerDraftNow('probe-owner','already saved',[]).catch(()=>{})");
    await flush();
    assert.equal(saving.requests.length,1,'original save is in flight before the profile switch');
    await saving.run("_switchProfileForSessionLoad('bravo')");
    assert.equal(saving.run('S.activeProfile'),'bravo');
    saving.run("S.session.composer_draft={text:'bravo draft',files:[]}");
    saving.ta.value='bravo composer';
    if (editOtherProfile) saving.run("_saveComposerDraft('probe-owner','bravo pending',[])");
    ack.resolve({session_id:'probe-owner'}); await flush();
    const key=profile=>'hermes-composer-pending:v1:'+JSON.stringify([profile,'probe-owner']);
    assert.equal(storage.has(key('default')),false,'ack clears its captured profile after a switch');
    assert.equal(saving.ta.value,'bravo composer','old ack leaves active composer alone');
    assert.equal(saving.run('S.session.composer_draft.text'),'bravo draft');
    assert.equal(storage.has(key('bravo')),editOtherProfile,'other profile pending intent survives');
    const original=page(); original.succeed();
    original.run("_restoreComposerDraft({text:'newer remote draft'},'probe-owner')");
    assert.equal(original.ta.value,'newer remote draft','saved text is not restored after a switch');
    await original.tick(); assert.equal(original.requests.length,0,'saved text is not replayed');
  }
  storage.clear();
  const retiring=page();
  retiring.run("_saveComposerDraftNow('probe-owner','delivered steer',[]).catch(()=>{})"); await flush();
  let remote={text:'newer remote draft',files:[]}, releaseCompare;
  const compareGate=new Promise(resolve=>{releaseCompare=resolve;});
  retiring.c.api=async (_,opts)=>{
    const body=JSON.parse(opts.body); retiring.requests.push(body);
    if (Object.hasOwn(body,'if_text')) {
      await compareGate;
      return {session_id:body.session_id,mismatch:true,draft:remote};
    }
    remote={text:body.text,files:body.files}; return {session_id:body.session_id,draft:remote};
  };
  retiring.ta.value='delivered steer';
  const retirement=retiring.run("_steerRetireOwnedDraft('probe-owner',{text:'delivered steer',files:[],rev:_composerDraftRevision('probe-owner')})");
  await retiring.tick(); // A retry queued behind the in-flight comparison must also be cancelled.
  releaseCompare();
  await retirement;
  assert.equal(retiring.ta.value,'','delivered guidance is removed from the composer');
  await retiring.tick();
  assert.equal(remote.text,'newer remote draft','retry cannot overwrite a mismatching draft after retirement');
  assert.equal(storage.size,0,'retired edit is not restored after reload');
  assert.equal(retiring.timers.size,0,'retirement cancels its retry');
  for (const mismatch of [false, true]) {
    for (const replacement of ['text','files','retype','profile']) {
      storage.clear();
      const editing=page();
      editing.run("_saveComposerDraftNow('probe-owner','delivered steer',[]).catch(()=>{})"); await flush();
      const comparison=editing.hold();
      editing.ta.value='delivered steer';
      const clearing=editing.run("_steerRetireOwnedDraft('probe-owner',{text:'delivered steer',files:[],rev:_composerDraftRevision('probe-owner')})");
      await flush();
      if (replacement==='profile') await editing.run("_switchProfileForSessionLoad('bravo')");
      if (replacement==='retype') editing.run("_noteComposerDraftPayload('probe-owner','',[])");
      const nextText=replacement==='text'?'new local edit':'delivered steer';
      const nextFiles=replacement==='files'?[{name:'new.txt',size:3}]:[];
      editing.run(`_saveComposerDraft('probe-owner',${JSON.stringify(nextText)},${JSON.stringify(nextFiles)})`);
      const pendingKey='hermes-composer-pending:v1:'+JSON.stringify([replacement==='profile'?'bravo':'default','probe-owner']);
      const pendingValue=storage.get(pendingKey);
      comparison.resolve({session_id:'probe-owner',mismatch}); await clearing;
      assert.equal(storage.get(pendingKey),pendingValue,'retirement leaves replacement intent intact');
      if (replacement==='profile') {
        const original=page(); original.succeed();
        original.run("_restoreComposerDraft({text:'newer remote draft'},'probe-owner')");
        assert.equal(original.ta.value,'newer remote draft','retirement discards its captured profile intent');
        await original.tick();
        assert.equal(original.requests.length,0,'retired guidance does not replay after a profile switch');
      }
      const restored=page(replacement==='profile'?'bravo':'default'); restored.succeed();
      restored.run("_restoreComposerDraft({text:'remote'},'probe-owner')");
      assert.equal(restored.ta.value,nextText,'replacement survives reload');
      await restored.tick();
      assert.equal(restored.requests.at(-1).text,nextText,'replacement still persists');
      assert.equal(restored.requests.at(-1).files.length,nextFiles.length);
    }
  }
  storage.clear();
  const retyped=page();
  retyped.run("_saveComposerDraft('probe-owner','delivered steer',[]); globalThis.retiredRev=_composerDraftRevision('probe-owner'); _noteComposerDraftPayload('probe-owner','',[]); _saveComposerDraft('probe-owner','delivered steer',[])");
  retyped.succeed();
  const retypedRecord=[...storage.values()][0];
  await retyped.run("_clearComposerDraftIfUnchanged('probe-owner','delivered steer',[],retiredRev)");
  assert.equal([...storage.values()][0],retypedRecord,'newer revision survives retirement of identical text');
  await retyped.tick();
  assert.equal(retyped.requests.at(-1).text,'delivered steer','newer identical text still autosaves');
  process.stdout.write('durability recovery, supersession, clear, profile isolation, retirement: PASS\n');
})().catch(e=>{console.error(e);process.exitCode=1;});
