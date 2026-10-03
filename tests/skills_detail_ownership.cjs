// Complete production modules and real DOM; only transport/unrelated panels are fixtures.
const fs = require('fs');
const {chromium} = require(process.argv[3]);
const root = process.argv[2];
const template = fs.readFileSync(root+'/static/index.html','utf8');
const list = template.slice(template.indexOf('<div class="panel-view" id="panelSkills">'), template.indexOf('<!-- Memory panel -->'));
const detail = template.slice(template.indexOf('<div id="mainSkills"'),template.indexOf('<div id="mainMemory"'));
const css = fs.readFileSync(root+'/static/style.css','utf8');
const html = `<meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style><style>body{display:block;padding:16px}#panelSkills{display:flex;position:static;height:230px}#mainSkills{display:flex;position:static;height:460px} .fixture-profile{padding:12px}</style><div class="fixture-profile">Active profile: <span id="fixtureProfile">A</span></div>${list}${detail}`;
const setup = () => {
  window.S={activeProfile:'A',session:null,messages:[]};
  window.$=id=>document.getElementById(id);
  window.t=key=>key; window.li=()=>'';
  window.esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
  window.renderMd=value=>esc(value);
  window.events=[]; window.frames=[];
  window.showToast=value=>events.push(['toast',value]);
  window.setStatus=value=>events.push(['status',value]);
  window.highlightCode=()=>events.push(['highlight']);
  window.requestAnimationFrame=callback=>frames.push(callback);
  window.setInterval=()=>0; window.syncTopbar=()=>{};
  window.requests=[]; window.requestLog=[]; window.confirmations=[];
  window.showConfirmDialog=()=>new Promise(resolve=>confirmations.push(resolve));
  window.api=(path,opts)=>{
    const ordinal=requestLog.length+1;
    requestLog.push({ordinal,path,profile:S.activeProfile});
    if(path==='/api/profile/switch')return Promise.resolve({active:JSON.parse(opts.body).name});
    return new Promise((resolve,reject)=>requests.push({ordinal,path,opts,profile:S.activeProfile,resolve,reject}));
  };
};
const compose = () => {
  renderSessionList=async()=>{}; renderSessionListFromCache=()=>{}; showSessionListSkeleton=()=>{};
  closeSessionActionMenu=()=>{}; startGatewaySSE=()=>{}; applyBotName=()=>{};
  _refreshProfileSwitchBackground=()=>{};
  _resetCronUnreadForProfileSwitch=()=>{}; _openProfileSwitchSessionBrowser=()=>{};
  refreshProfileTransitionReasoningChip=()=>{}; invalidateSlashSkillCaches=()=>{};
  _closeMobileSidebarAfterPanelSelection=()=>{};
  window.syncAppTitlebar=()=>{};
  window.mutationsAfter=ordinal=>requestLog.filter(r=>r.ordinal>ordinal &&
    ['/api/skills/save','/api/skills/delete','/api/skills/toggle'].includes(r.path));
  window.flush=()=>new Promise(resolve=>setTimeout(resolve,0));
  window.payload=profile=>({runtime_scope:'profile',skills:[{name:'same',category:profile,description:profile+' description',disabled:profile!=='A'}]});
  window.content=profile=>({content:profile+' private content',linked_files:{references:['ref.md']}});
  window.snapshot=()=>({profile:S.activeProfile,data:_skillsData,detail:_currentSkillDetail,
    pre:_skillPreFormDetail,editing:_editingSkillName,mode:_skillMode,collapsed:[..._collapsedCats],
    list:$('skillsList').innerHTML,body:$('skillDetailBody').innerHTML,title:$('skillDetailTitle').textContent,
    bodyDisplay:$('skillDetailBody').style.display,emptyDisplay:$('skillDetailEmpty').style.display,
    buttons:[...document.querySelectorAll('#mainSkills button')].map(b=>b.style.display),events:events.slice()});
  window.populate=async profile=>{
    const list=loadSkills(); requests.at(-1).resolve(payload(profile)); await list;
    const read=openSkill('same',document.querySelector('.skill-item'));requests.at(-1).resolve(content(profile));await read;
    editCurrentSkill();
  };
};
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const reports=[];
 try{
  for(const switcher of ['switchToProfile','_switchProfileForSessionLoad']){
   for(const oldFirst of [false,true])for(const returnA of [false,true]){
    for(const action of ['detail','file','toggle','save','delete','confirm'])for(const error of [false,true]){
     const page=await browser.newPage({viewport:{width:1200,height:800}});
     await page.route('http://127.0.0.1:8789/fixture',r=>r.fulfill({contentType:'text/html',body:html})); await page.goto('http://127.0.0.1:8789/fixture'); await page.evaluate(setup);
     for(const file of ['sessions.js','panels.js'])await page.addScriptTag({content:fs.readFileSync(root+'/static/'+file,'utf8')});
     await page.evaluate(compose);
     const report=await page.evaluate(async({switcher,oldFirst,returnA,action,error})=>{
       await populate('A'); _collapsedCats.add('A'); _skillsDataIncomplete=true;
       loadSkills(); const oldList=requests.at(-1);
       let pending;
       if(action==='detail')pending=openSkill('same',null);
       if(action==='file')pending=openSkillFile('same','ref.md');
       if(action==='toggle')pending=toggleSkill('same',true);
       if(action==='save')pending=saveSkillForm();
       if(action==='delete'||action==='confirm'){
         pending=deleteCurrentSkill();
         if(action==='delete'){confirmations.at(-1)(true);await flush();}
       }
       const old=requests.at(-1);
       await window[switcher]('B'); $('fixtureProfile').textContent='B';
       if(returnA){await window[switcher]('A');$('fixtureProfile').textContent='A';}
       const switchOrdinal=requestLog.length;
       const immediate=snapshot();
       const settleOld=async()=>{
         oldList.resolve(payload('A'));
         if(action==='confirm')confirmations.at(-1)(true);
         else if(error)old.reject(Error('A stale failure'));
         else old.resolve(action==='detail'||action==='file'?content('A'):{ok:true});
         await flush(); // Never await a buggy mutation's follow-up network request.
         frames.splice(0).forEach(cb=>cb());
       };
       if(oldFirst)await settleOld();
       const neutral=snapshot();
       await populate(returnA?'A-new':'B'); const before=snapshot();
       if(!oldFirst)await settleOld();
       const after=snapshot();
       return {immediate,neutral,before,after,requestLog,mutationsAfterSwitch:mutationsAfter(switchOrdinal)};
     },{switcher,oldFirst,returnA,action,error});
     reports.push({switcher,oldFirst,returnA,action,error,...report});
     await page.close();
    }
   }
  }
  // Same-profile request supersession, navigation during writes, and callbacks
  // queued before a transition exercise ownership independently of profile names.
  for(const scenario of ['detail','file','toggle','save','delete','save-reload','delete-reload','enhance-md','enhance-code']){
   for(const error of [false,true]){
    const page=await browser.newPage();
    await page.route('http://127.0.0.1:8789/fixture',r=>r.fulfill({contentType:'text/html',body:html}));
    await page.goto('http://127.0.0.1:8789/fixture');await page.evaluate(setup);
    for(const file of ['sessions.js','panels.js'])await page.addScriptTag({content:fs.readFileSync(root+'/static/'+file,'utf8')});
    await page.evaluate(compose);
    const report=await page.evaluate(async({scenario,error})=>{
      await populate('A');
      let pending;
      if(scenario==='detail')pending=openSkill('old',null);
      if(scenario==='file')pending=openSkillFile('same','old.md');
      if(scenario==='toggle')pending=toggleSkill('same',true);
      if(scenario.startsWith('save'))pending=saveSkillForm();
      if(scenario.startsWith('delete')){
        pending=deleteCurrentSkill();confirmations.at(-1)(true);await flush();
      }
      if(scenario.startsWith('enhance')){
        pending=openSkillFile('same',scenario==='enhance-md'?'ref.md':'ref.py');
        requests.at(-1).resolve(content('A'));await pending;
        await _switchProfileForSessionLoad('B');
        const before=snapshot();frames.splice(0).forEach(cb=>cb());
        return {before,after:snapshot()};
      }
      let old=requests.at(-1);
      if(scenario.endsWith('reload')){
        old.resolve({ok:true});await flush();old=requests.at(-1);
        await _switchProfileForSessionLoad('B');
        const before=snapshot();
        if(error)old.reject(Error('stale reload failure'));else old.resolve(payload('A'));
        await flush();
        return {before,after:snapshot(),paths:requests.map(r=>r.path)};
      }
      if(scenario==='toggle'){
        const latest=toggleSkill('same',false);requests.at(-1).resolve({ok:true});await latest;
      }else{
        const latest=openSkill('new',null);requests.at(-1).resolve(content('B'));await latest;
      }
      const before=snapshot();
      if(error)old.reject(Error('superseded failure'));
      else old.resolve(scenario==='detail'||scenario==='file'?content('A'):{ok:true});
      await flush();
      return {before,after:snapshot()};
    },{scenario,error});
    reports.push({scenario,error,...report});await page.close();
   }
  }
  // Compose the actual transport too: a failed request must not retry with
  // the destination profile cookie or emit the helper's unowned timeout toast.
  for(const action of ['detail','file','toggle','save','delete'])for(const timeout of [false,true]){
    const page=await browser.newPage();
    await page.route('http://127.0.0.1:8789/fixture',r=>r.fulfill({contentType:'text/html',body:html}));
    await page.goto('http://127.0.0.1:8789/fixture');await page.evaluate(setup);
    for(const file of ['sessions.js','panels.js'])await page.addScriptTag({content:fs.readFileSync(root+'/static/'+file,'utf8')});
    await page.evaluate(compose);
    await page.evaluate(async()=>{await populate('A');});
    await page.addScriptTag({content:fs.readFileSync(root+'/static/workspace.js','utf8')});
    const report=await page.evaluate(async({action,timeout})=>{
      const fetches=[];
      window.fetch=(url,opts)=>{
        const path=new URL(url,location.href).pathname;
        requestLog.push({ordinal:requestLog.length+1,path,profile:S.activeProfile});
        if(url.includes('/api/profile/switch'))return Promise.resolve(new Response(JSON.stringify({active:'B'}),{headers:{'Content-Type':'application/json'}}));
        return new Promise((resolve,reject)=>fetches.push({url,opts,resolve,reject}));
      };
      if(action==='detail')openSkill('same',null);
      if(action==='file')openSkillFile('same','ref.md');
      if(action==='toggle')toggleSkill('same',true);
      if(action==='save')saveSkillForm();
      if(action==='delete'){deleteCurrentSkill();confirmations.at(-1)(true);await flush();}
      await _switchProfileForSessionLoad('B');const before=snapshot();const switchOrdinal=requestLog.length;
      const error=timeout?Object.assign(Error('timed out'),{name:'TimeoutError'}):new TypeError('network disconnected');
      fetches[0].reject(error);await flush();
      return {before,after:snapshot(),fetches:fetches.length,requestLog,mutationsAfterSwitch:mutationsAfter(switchOrdinal)};
    },{action,timeout});
    reports.push({scenario:'transport-'+action,timeout,...report});await page.close();
  }
  // Real panel lifecycle: visible switch awaits the destination load; hidden
  // switch reloads only on reopening through switchPanel -> loadSkills.
  for(const visibility of ['visible','hidden'])for(const oldFirst of [false,true]){
    const page=await browser.newPage();
    await page.route('http://127.0.0.1:8789/fixture',r=>r.fulfill({contentType:'text/html',body:html}));
    await page.goto('http://127.0.0.1:8789/fixture');await page.evaluate(setup);
    for(const file of ['sessions.js','panels.js'])await page.addScriptTag({content:fs.readFileSync(root+'/static/'+file,'utf8')});
    await page.evaluate(compose);
    const report=await page.evaluate(async({visibility,oldFirst})=>{
      await populate('A');
      if(visibility==='visible')await switchPanel('skills');
      _skillsDataIncomplete=true;loadSkills();const old=requests.at(-1);
      let switched=false;
      const transition=switchToProfile('B').then(()=>{switched=true;});
      await flush();
      const switchOrdinal=requestLog.findLast(r=>r.path==='/api/profile/switch').ordinal;
      const immediate=snapshot();
      if(visibility==='visible' && switched)throw Error('visible switch did not await destination load');
      if(visibility==='hidden')await transition;
      if(oldFirst){old.resolve(payload('A'));await flush();}
      const reopening=visibility==='hidden'?switchPanel('skills'):transition;
      await flush();const destination=requests.at(-1);
      if(destination===old || destination.profile!=='B' || destination.path!=='/api/skills')throw Error('missing destination panel load');
      destination.resolve(payload('B'));await reopening;await transition;
      const before=snapshot();
      if(!oldFirst){old.resolve(payload('A'));await flush();}
      return {immediate,before,after:snapshot(),destinationRequests:requestLog.filter(r=>r.profile==='B'&&r.path==='/api/skills').length,
        requestLog,mutationsAfterSwitch:mutationsAfter(switchOrdinal)};
    },{visibility,oldFirst});
    reports.push({scenario:'panel-'+visibility,oldFirst,...report});await page.close();
  }
  for(const action of ['toggle','save','delete','delete-cancel']){
    const page=await browser.newPage();
    await page.route('http://127.0.0.1:8789/fixture',r=>r.fulfill({contentType:'text/html',body:html}));
    await page.goto('http://127.0.0.1:8789/fixture');await page.evaluate(setup);
    for(const file of ['sessions.js','panels.js'])await page.addScriptTag({content:fs.readFileSync(root+'/static/'+file,'utf8')});
    await page.evaluate(compose);
    const report=await page.evaluate(async action=>{
      await populate('A');
      await switchToProfile('B');
      const switchOrdinal=requestLog.length;
      await populate('B');
      if(action==='delete-cancel'){
        cancelSkillForm();
        const pending=deleteCurrentSkill();confirmations.at(-1)(false);await pending;
        document.querySelector('.skill-linked-file').click();
        const last=requests.at(-1);
        if(!last.path.includes('&file='))throw Error('cancelled deletion retired current links');
        last.resolve(content('linked'));await flush();
        return {...snapshot(),switchOrdinal,mutationRequests:mutationsAfter(switchOrdinal)};
      }
      const pending=action==='toggle'?toggleSkill('same',false):action==='save'?saveSkillForm():deleteCurrentSkill();
      if(action==='delete'){confirmations.at(-1)(true);await flush();}
      requests.at(-1).resolve({ok:true});await flush();
      if(action==='toggle'){
        await pending;cancelSkillForm();
        return {...snapshot(),switchOrdinal,mutationRequests:mutationsAfter(switchOrdinal)};
      }
      requests.at(-1).resolve(action==='save'?payload('B'):{runtime_scope:'profile',skills:[]});await flush();
      if(action==='save')requests.at(-1).resolve(content('saved'));
      await pending;return {...snapshot(),switchOrdinal,mutationRequests:mutationsAfter(switchOrdinal)};
    },action);
    reports.push({happy:action,...report});await page.close();
  }
  console.log(JSON.stringify(reports));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
