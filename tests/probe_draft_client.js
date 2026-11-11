// Execute the real composer implementation with a deterministic transport/timer harness.
const fs = require('fs');
const vm = require('vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8').split('const SESSION_VIEWED_COUNTS_KEY =')[0];
const apiSource = fs.readFileSync(require('path').join(require('path').dirname(process.argv[2]),'workspace.js'),'utf8').split('function recordClientSSEError(')[0];
const wire = JSON.parse(process.argv[3]);
const localStorageData = new Map();
function page() {
  const timers = new Map(), requests = [], warnings = [], notices = [];
  let timerId = 0;
  const textarea = {value: ''};
  const context = vm.createContext({
    S: {session: {session_id:'probe-owner',composer_draft:wire.durable},pendingFiles:[]},
    $: () => textarea,
    console: {warn: (...args) => warnings.push(args)},
    toast: (...args) => notices.push(args),
    showToast: (...args) => notices.push(args),
    URL, document: {baseURI:'http://127.0.0.1/'},
    localStorage: {getItem: k => localStorageData.get(k) || null,
      setItem: (k,v) => localStorageData.set(k,v), removeItem: k => localStorageData.delete(k)},
    setTimeout: (fn, delay) => {const id=++timerId;timers.set(id,{fn,delay});return id;},
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      requests.push({url,body:JSON.parse(options.body)});
      return {ok:wire.status===200,status:wire.status,statusText:String(wire.status),
        headers:{get:()=> 'application/json'},
        text:async()=>JSON.stringify(wire.body),json:async()=>wire.body};
    },
  });
  vm.runInContext(apiSource + '\n' + source, context);
  return {context,textarea,timers,requests,warnings,notices, run: code => vm.runInContext(code, context)};
}
(async () => {
  const first=page();
  first.textarea.value='typed while streaming';
  first.run("_saveComposerDraft('probe-owner','typed while streaming',[])");
  const scheduled=[...first.timers.entries()];
  assert.equal(scheduled.length,1);
  for (const [id,timer] of scheduled) {first.timers.delete(id);timer.fn();}
  for(let n=0;n<20;n++) await Promise.resolve();
  const inMemory=first.run("(_composerDraftLocalFallbackBySid.get('probe-owner') || {}).text || null");
  first.textarea.value='';
  first.run("_restoreComposerDraft(S.session.composer_draft,'probe-owner')");
  const inPageRestored=first.textarea.value;
  // A fresh JS realm models a page reload: only actual persistent storage survives.
  const second=page();
  second.run("_restoreComposerDraft(S.session.composer_draft,'probe-owner')");
  const observed={wireStatus:wire.status, code:wire.body.code,
    requests:first.requests.length, pendingRetryTimers:first.timers.size,
    warnings:first.warnings.length, userNotices:first.notices.length,
    inMemory,inPageRestored,persistentStorageEntries:localStorageData.size,
    afterReload:second.textarea.value};
  process.stdout.write(JSON.stringify(observed)+'\n');
  if(wire.status!==200) assert.equal(inMemory,'typed while streaming');
  assert.equal(inPageRestored,'typed while streaming');
  assert.equal(second.textarea.value,'typed while streaming',
    'A same-profile checkpoint conflict must not silently lose the draft on reload');
})().catch(error => {process.stderr.write(error.stack+'\n');process.exitCode=1;});
