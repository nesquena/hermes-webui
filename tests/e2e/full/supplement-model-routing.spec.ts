import {test,expect,open,api,auth,session,capture} from './fixtures';
import fs from 'node:fs';
import path from 'node:path';

function providerRows(marker:string) {
 const file=path.join(auth.workspace,'..','provider-evidence.jsonl');
 if(!fs.existsSync(file)) return [];
 return fs.readFileSync(file,'utf8').split('\n').filter(Boolean).map(line=>JSON.parse(line))
  .filter(row=>!row.review && String(row.last_user_text||'').includes(marker));
}
function receipts(marker:string) {return providerRows(marker).filter(row=>row.tools_offered?.length);}

test('SUPPLEMENT MODEL ROUTING visible custom composer model reaches the provider and remains conversation scoped after reload',async({page},info)=>{
 await page.setViewportSize({width:1920,height:1080});
 await open(page);const sid=await session(page);
 const main=(await api(page,'/api/model/auxiliary')).body.main;
 const baseline=(await api(page,'/api/session?session_id='+sid)).body.session;
 const alternate='qa-alternate';const marker='QA_MODEL_ROUTING_'+Date.now();
 const second=(await api(page,'/api/session/new',{workspace:auth.workspace})).body.session;
 expect(second.session_id).not.toBe(sid);
 await page.locator('#composerModelChip').click();
 const picker=page.locator('#composerModelDropdown');await expect(picker).toHaveClass(/open/);
 await picker.locator('.model-custom-input').fill(alternate);
 const picked=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/session/update'&&r.request().method()==='POST');
 await picker.locator('.model-custom-btn').click();expect((await picked).status()).toBe(200);
 await expect(page.locator('#composerModelChip')).toContainText(alternate);
 expect((await api(page,'/api/session?session_id='+sid)).body.session.model).toBe(alternate);
 await page.locator('#msg').fill(marker);await page.locator('#btnSend').click();
 await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_MODEL_ROUTING_',{timeout:35000});
 await expect.poll(()=>receipts(marker).map(row=>row.request_model)).toEqual([alternate]);
 await page.reload();await expect(page.locator('#composerModelChip')).toContainText(alternate);
 const stored=(await api(page,'/api/session?session_id='+sid)).body.session;
 expect(stored.model).toBe(alternate);expect(stored.workspace).toBe(baseline.workspace);
 expect(stored.messages.some((m:any)=>m.role==='user'&&m.content===marker)).toBe(true);
 expect((await api(page,'/api/model/auxiliary')).body.main).toEqual(main);
 expect((await api(page,'/api/session?session_id='+second.session_id)).body.session.model).toBe(second.model);
 await capture(page,'composer-model-real-routing',info);
 await info.attach('actual-provider-model-receipt',{body:JSON.stringify(receipts(marker).map(r=>({model:r.request_model,marker:r.last_user_text}))),contentType:'application/json'});
 // Exercise a separately saved auxiliary route through the real title action.
 await expect.poll(async()=>(await api(page,'/api/session/status?session_id='+sid)).body.agent_running).toBe(false);
 const originalTitle=(await api(page,'/api/model/auxiliary')).body.tasks.find((t:any)=>t.task==='title_generation');
 try {
  await page.locator('.rail-btn[data-panel="settings"]').click();
  await page.locator('[data-settings-section="preferences"]').click();
  await page.locator('#aux-prov-title_generation').selectOption('custom:qa');
  await page.locator('#aux-model-title_generation').selectOption('__custom__');
  await page.locator('#appDialogInput').fill('qa-title-alternate');await page.locator('#appDialogConfirm').click();
  await page.locator('#btnApplyAuxModels').click();await expect(page.locator('#btnApplyAuxModels')).toBeHidden();
  expect((await api(page,'/api/model/auxiliary')).body.tasks.find((t:any)=>t.task==='title_generation')).toMatchObject({provider:'custom:qa',model:'qa-title-alternate'});
  await page.locator('.rail-btn[data-panel="chat"]').click();
  await page.locator('#conversationBoardBack').click();
  const before=providerRows(marker).length;
  const row=page.locator('.session-item[data-sid="'+sid+'"]');await row.hover();await row.locator('.session-actions-trigger').click();
  const regenerated=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/session/title/regenerate');
  await page.getByRole('menuitem',{name:'Regenerate title',exact:true}).click();
  const result=await regenerated;expect(result.status()).toBe(200);const changed=await result.json();
  const routed=providerRows(marker).slice(before);
  expect(routed.length).toBeGreaterThan(0);expect(routed.every(r=>r.request_model==='qa-title-alternate'&&!r.tools_offered.length)).toBe(true);
  expect((await api(page,'/api/model/auxiliary')).body.main).toEqual(main);
  expect((await api(page,'/api/session?session_id='+sid)).body.session).toMatchObject({title:changed.title,model:alternate});
  await page.reload();await expect(page.locator('.session-item[data-sid="'+sid+'"]')).toContainText(changed.title);
  await info.attach('actual-auxiliary-provider-receipt',{body:JSON.stringify({title:changed.title,requests:routed}),contentType:'application/json'});
  await capture(page,'auxiliary-title-real-routing',info);
 } finally {
  expect((await api(page,'/api/model/set',{scope:'auxiliary',task:'title_generation',provider:originalTitle.provider,model:originalTitle.model})).status).toBe(200);
 }
});

test('SUPPLEMENT AUX SAVE navigation Save applies pending auxiliary choices and Apply preserves unrelated edits and failed choices',async({page},info)=>{
 await open(page);await session(page);
 const initial=(await api(page,'/api/model/auxiliary')).body;
 const original=initial.tasks.find((t:any)=>t.task==='title_generation');
 const settingsBefore=(await api(page,'/api/settings')).body;
 async function preferences(){await page.locator('.rail-btn[data-panel="settings"]').click();await page.locator('[data-settings-section="preferences"]').click();await expect(page.locator('#aux-prov-title_generation')).toBeVisible();}
 async function pick(model:string){await page.locator('#aux-prov-title_generation').selectOption('custom:qa');await page.locator('#aux-model-title_generation').selectOption('__custom__');await page.locator('#appDialogInput').fill(model);await page.locator('#appDialogConfirm').click();}
 async function saved(){return (await api(page,'/api/model/auxiliary')).body.tasks.find((t:any)=>t.task==='title_generation');}
 try {
  await preferences();await pick('qa-title-global-save');
  await page.locator('.rail-btn[data-panel="chat"]').click();await expect(page.locator('#settingsUnsavedBar')).toBeVisible();
  await page.locator('#settingsUnsavedBar').getByRole('button',{name:'Save',exact:true}).click();
  await expect(page.locator('#conversationBoardSearch')).toBeVisible();expect(await saved()).toMatchObject({provider:'custom:qa',model:'qa-title-global-save'});
  expect((await api(page,'/api/model/auxiliary')).body.main).toEqual(initial.main);
  await preferences();await page.locator('#settingsMaxTokens').fill('789');await pick('qa-title-applied');
  await page.locator('#btnApplyAuxModels').click();await expect(page.locator('#btnApplyAuxModels')).toBeHidden();
  await page.locator('.rail-btn[data-panel="chat"]').click();await expect(page.locator('#settingsUnsavedBar')).toBeVisible();
  expect((await api(page,'/api/settings')).body.max_tokens).toEqual(settingsBefore.max_tokens);
  expect(await saved()).toMatchObject({model:'qa-title-applied'});
  await page.locator('#settingsUnsavedBar').getByRole('button',{name:'Discard',exact:true}).click();await expect(page.locator('#conversationBoardSearch')).toBeVisible();
  await preferences();await expect(page.locator('#settingsMaxTokens')).not.toHaveValue('789');await pick('qa-title-refused');
  await page.route('**/api/model/set',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'QA controlled save failure'})}));
  await page.locator('.rail-btn[data-panel="chat"]').click();await page.locator('#settingsUnsavedBar').getByRole('button',{name:'Save',exact:true}).click();
  await expect(page.locator('#toast')).toContainText(/failed to save/i);
  await expect(page.locator('#settingsUnsavedBar')).toBeVisible();await expect(page.locator('#aux-model-title_generation')).toHaveValue('qa-title-refused');
  expect(await saved()).toMatchObject({model:'qa-title-applied'});
  await page.unroute('**/api/model/set');await capture(page,'auxiliary-save-state',info);
 } finally {
  await page.unroute('**/api/model/set');
  expect((await api(page,'/api/model/set',{scope:'auxiliary',task:'title_generation',provider:original.provider,model:original.model})).status).toBe(200);
 }
});
