import {test, expect, open, session, api} from './fixtures';

test.use({user:'alice'});

async function ready(page:any) {
  await open(page);
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first().click();
}

async function contrast(page:any,info:any,selectors:string[]) {
  const evidence:any[]=[];
  for(const dark of [false,true]) {
    await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
    await page.evaluate(()=>document.getAnimations().forEach(a=>{try{a.finish();}catch{}}));
    const readings=await page.evaluate(selectors=>{
      const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d')!;
      const rgb=(value:string)=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=value;ctx.fillRect(0,0,1,1);const c=[...ctx.getImageData(0,0,1,1).data];c[3]/=255;return c;};
      const lum=(c:number[])=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
      return selectors.map(selector=>{
        const node=document.querySelector(selector)!,style=getComputedStyle(node,selector==='#msg'?'::placeholder':null);
        const parents:Element[]=[];let opacity=1,bg=[255,255,255];
        for(let p:Element|null=node;p;p=p.parentElement){parents.unshift(p);opacity*=Number(getComputedStyle(p).opacity);}
        for(const parent of parents){const c=rgb(getComputedStyle(parent).backgroundColor);bg=bg.map((v,i)=>c[i]*c[3]+v*(1-c[3]));}
        const ink=rgb(style.color),alpha=opacity*ink[3];const fg=ink.slice(0,3).map((v,i)=>v*alpha+bg[i]*(1-alpha));
        const a=lum(fg),b=lum(bg);return {selector,fg,bg,opacity,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
      });
    },selectors);
    evidence.push({dark,readings});for(const reading of readings)expect(reading.ratio,JSON.stringify({dark,...reading})).toBeGreaterThanOrEqual(4.5);
  }
  await info.attach('chat-contrast',{body:JSON.stringify(evidence),contentType:'application/json'});
}

test('CHAT WORKSPACE starters preserve the mounted draft and wait for explicit send',async({page},info)=>{
  await ready(page); const sid=await session(page);
  await page.evaluate(()=>{window.__qaComposer=document.getElementById('msg');});
  for(const width of [1440,900,390]) {
    await page.setViewportSize({width,height:width===390?844:1000});
    await page.mouse.move(0,0);
    await page.screenshot({path:info.outputPath('welcome-'+width+'.png')});
    await expect(page.locator('#msg')).toBeInViewport();
    await expect(page.locator('#btnSend')).toBeInViewport();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  }
  await page.setViewportSize({width:1440,height:1000});
  await contrast(page,info,['#emptyState p','.suggestion','#msg','.chat-bot-toggle']);
  await page.locator('#msg').fill('QA_STARTER_DRAFT');
  const starts:string[]=[];
  page.on('request',r=>{if(/\/api\/chat\/(start|stream)/.test(r.url())&&r.method()==='POST')starts.push(r.url());});
  await page.locator('.suggestion').first().click();
  const draft='QA_STARTER_DRAFT\n\nWhat files are in this workspace?';
  await expect(page.locator('#msg')).toHaveValue(draft);
  await expect(page.locator('#msg')).toBeFocused();
  // Repeated selection is idempotent; normal input handling owns draft persistence.
  await page.locator('.suggestion').first().click();
  await expect(page.locator('#msg')).toHaveValue(draft);
  await expect.poll(async()=>(await api(page,'/api/session?session_id='+sid)).body.session.composer_draft?.text).toBe(draft);
  expect((await api(page,'/api/session?session_id='+sid)).body.session.messages).toEqual([]);
  expect(starts).toEqual([]);
  const welcome=await page.locator('#emptyState').boundingBox(),composer=await page.locator('.composer-box').boundingBox();
  expect((await page.locator('#composerWrap').boundingBox())!.y-(welcome!.y+welcome!.height)).toBeLessThan(45);
  expect(composer!.y+composer!.height).toBeLessThan(850);
  await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_STARTER_DRAFT');
  expect(await page.evaluate(()=>window.__qaComposer===document.getElementById('msg'))).toBe(true);
  await expect(page.locator('#emptyState')).toBeHidden();
  expect((await page.locator('.composer-box').boundingBox())!.y).toBeGreaterThan(800);
  await page.reload(); await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_STARTER_DRAFT');
  await page.evaluate(()=>setLocale('nl'));await page.locator('#btnNewChat').click();
  await expect(page.locator('.suggestion').first()).toHaveText('Verken bestanden');
  await page.locator('.suggestion').first().click();
  await expect(page.locator('#msg')).toHaveValue('Welke bestanden staan er in deze werkruimte?');
  const nextSid=page.url().split('/session/')[1];
  await expect.poll(async()=>(await api(page,'/api/session?session_id='+nextSid)).body.session.composer_draft?.text).toBe('Welke bestanden staan er in deze werkruimte?');
  await page.reload();await expect(page.locator('#msg')).toHaveValue('Welke bestanden staan er in deze werkruimte?');
  await page.evaluate(()=>setLocale('nl'));
  await expect(page.locator('#chatBotRosterToggle')).toHaveText('Bots verbergen');
  await page.locator('#chatBotRosterToggle').click();await expect(page.locator('#chatBotRosterToggle')).toHaveText('Bots tonen');
  await page.locator('#chatBotRosterToggle').click();await expect(page.locator('#msg')).toHaveValue('Welke bestanden staan er in deze werkruimte?');
  await page.mouse.move(0,0);await page.screenshot({path:info.outputPath('welcome-dutch.png')});
});

test('CHAT WORKSPACE reading surface fits answers, tables, code and text size at all widths',async({page},info)=>{
  await ready(page);const sid=await session(page);
  await page.locator('#msg').fill('QA_READING_SURFACE');await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('A clear place to start');
  await expect.poll(async()=>(await api(page,'/health')).body.active_runs||0).toBe(0);
  await contrast(page,info,['.msg-row[data-role="assistant"] .msg-body','.msg-body td','.msg-role-name']);
  await page.locator('#msg').fill('Keep this follow-up draft');
  await page.screenshot({path:info.outputPath('reading-desktop.png')});
  await page.evaluate(()=>document.documentElement.classList.remove('dark'));
  await page.screenshot({path:info.outputPath('reading-light.png')});
  await page.evaluate(()=>document.documentElement.classList.add('dark'));
  for(const width of [900,390]) {
    await page.setViewportSize({width,height:width===390?844:900});
    await expect(page.locator('#msg')).toBeInViewport();await expect(page.locator('#btnSend')).toBeInViewport();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('reading-'+width+'.png')});
  }
  const original=await page.locator('.msg-row[data-role="assistant"] .msg-body').first().evaluate(el=>parseFloat(getComputedStyle(el).fontSize));
  await page.evaluate(()=>document.documentElement.dataset.fontSize='large');
  expect(await page.locator('.msg-row[data-role="assistant"] .msg-body').first().evaluate(el=>parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThan(original);
  await expect(page.locator('#msg')).toHaveValue('Keep this follow-up draft');
  expect((await api(page,'/api/session?session_id='+sid)).body.session.messages.filter((m:any)=>m.role==='user')).toHaveLength(1);
  await page.setViewportSize({width:390,height:500});
  await expect(page.locator('#msg')).toBeInViewport();await expect(page.locator('#btnSend')).toBeInViewport();
});

test('CHAT WORKSPACE welcome preferences and reduced motion retain a usable composer',async({page},info)=>{
  await page.emulateMedia({reducedMotion:'reduce'});await ready(page);await session(page);
  await page.evaluate(()=>{window._hideEmptyStatePanel=true;applyEmptyStatePanelPref();});
  await expect(page.locator('#emptyState')).toBeHidden();await expect(page.locator('#msg')).toBeInViewport();
  await page.evaluate(()=>{window._hideEmptyStatePanel=false;applyEmptyStatePanelPref();window._hideEmptyStateSuggestions=true;applyEmptyStateSuggestionPref();});
  await expect(page.locator('#emptyState')).toBeVisible();await expect(page.locator('.suggestion-grid')).toBeHidden();
  for(const viewport of [{width:900,height:900},{width:390,height:500}]) {
    await page.setViewportSize(viewport);
    await page.locator('#msg').fill('A multiline draft\n'.repeat(8));
    await expect(page.locator('#msg')).toBeInViewport();await expect(page.locator('#btnSend')).toBeInViewport();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath('welcome-draft-'+viewport.width+'.png')});
  }
});
