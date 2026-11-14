import {test, expect, open, session, api, auth, capture} from './fixtures';

async function create(page:any, marker:string) {
  await open(page); const sid=await session(page);
  await page.locator('#msg').fill(marker); await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: '+marker.replace(/\d+$/,''),{timeout:30000});
  await expect.poll(async()=>(await api(page,'/health')).body.active_runs||0).toBe(0);
  return sid;
}
async function menu(page:any,sid:string) {
  const row=page.locator('.session-item[data-sid="'+sid+'"]');
  await row.hover(); await row.locator('.session-actions-trigger').click();
  await expect(page.locator('.session-action-menu')).toBeVisible();
}

test('US-SP-SESS-DISCOVERY rename Enter and Escape plus title content and URL search select persisted conversation',async({page},info)=>{
  const marker='QA_SEARCH_BODY_'+Date.now(); const sid=await create(page,marker);
  const name='QA renamed '+Date.now();
  await menu(page,sid); await page.getByRole('menuitem',{name:'Rename conversation',exact:true}).click();
  await page.locator('.session-title-input').fill(name); await page.locator('.session-title-input').press('Enter');
  await expect.poll(async()=>(await api(page,'/api/session?session_id='+sid)).body.session.title).toBe(name);
  await menu(page,sid); await page.getByRole('menuitem',{name:'Rename conversation',exact:true}).click();
  await page.locator('.session-title-input').fill('QA cancelled rename'); await page.locator('.session-title-input').press('Escape');
  expect((await api(page,'/api/session?session_id='+sid)).body.session.title).toBe(name);
  await page.reload(); await expect(page.locator('.session-item[data-sid="'+sid+'"]')).toContainText(name);
  for(const query of [name,marker,auth.base_url+'/session/'+sid]) {
    await page.locator('#sessionSearch').fill(query);
    const row=page.locator('.session-item[data-sid="'+sid+'"]'); await expect(row).toBeVisible();
    await row.click(); await expect(page.locator('#messages')).toContainText(marker);
    await page.locator('#sessionSearchClear').click(); await expect(page.locator('#sessionSearch')).toHaveValue('');
  }
  await page.locator('#sessionSearch').fill('QA_NO_MATCH_'+Date.now());
  await expect(page.locator('.session-item')).toHaveCount(0);
  await page.locator('#sessionSearchClear').click(); await expect(page.locator('.session-item[data-sid="'+sid+'"]')).toBeVisible();
  await capture(page,'conversation-discovery',info);
});

test('US-SP-SESS-ARCHIVE archive reload show archived and restore preserve authoritative transcript',async({page},info)=>{
  const marker='QA_ARCHIVE_'+Date.now(); const sid=await create(page,marker);
  await menu(page,sid); await page.getByRole('menuitem',{name:'Archive conversation',exact:true}).click();
  await expect.poll(async()=>(await api(page,'/api/session?session_id='+sid)).body.session.archived).toBe(true);
  await page.reload(); await expect(page.locator('.session-item[data-sid="'+sid+'"]')).toHaveCount(0);
  await page.getByText(/^Show \d+ archived$/).click();
  await expect(page.locator('.session-item[data-sid="'+sid+'"]')).toBeVisible();
  await menu(page,sid); await page.getByRole('menuitem',{name:'Restore conversation',exact:true}).click();
  await expect.poll(async()=>(await api(page,'/api/session?session_id='+sid)).body.session.archived).toBe(false);
  await page.reload(); await page.locator('.session-item[data-sid="'+sid+'"] .session-title').click();
  await expect(page.locator('#messages')).toContainText(marker);
  await capture(page,'conversation-restored',info);
});

test('US-SP-SESS-SHARE create public snapshot read anonymously cancel revoke then revoke invalidates public link',async({page,browser},info)=>{
  const marker='QA_SHARE_'+Date.now(); const sid=await create(page,marker);
  await page.locator('.rail-btn[data-panel="settings"]').click();
  const created=page.waitForResponse(r=>r.url().endsWith('/api/share/create')&&r.request().method()==='POST');
  await page.locator('#btnShareSession').click(); const response=await created;
  expect(response.status(),'rendered Share must create a real snapshot').toBe(200);
  const payload=await response.json(); const href=new URL(payload.share.url,auth.base_url).href;
  expect(href).toContain('/share/');
  const anonymous=await browser.newContext();
  try {
    await anonymous.route('**/*',route=>['127.0.0.1','localhost'].includes(new URL(route.request().url()).hostname)?route.continue():route.abort('blockedbyclient'));
    const publicPage=await anonymous.newPage();const publicErrors:string[]=[];publicPage.on('pageerror',error=>publicErrors.push(error.message));await publicPage.goto(href);
    await expect(publicPage.locator('body')).toContainText('QA_REPLY: '+marker.replace(/\d+$/,''));
    await expect(publicPage.locator('#msg')).toHaveCount(0);
    const publicAPI=auth.base_url+'/api/share/'+payload.share.token;
    const firstSnapshot=(await (await anonymous.request.get(publicAPI)).json()).share;
    expect(Object.keys(firstSnapshot).sort()).toEqual(['created_at','message_count','messages','title','updated_at']);
    expect(firstSnapshot.messages.every((message:any)=>['user','assistant'].includes(message.role))).toBe(true);
    expect((await anonymous.request.post(auth.base_url+'/api/share/create',{data:{session_id:sid}})).status()).toBe(401);
    expect((await anonymous.request.post(auth.base_url+'/api/share/revoke',{data:{session_id:sid}})).status()).toBe(401);
    const crossOrigin=await page.context().request.post(auth.base_url+'/api/share/revoke',{data:{session_id:sid},headers:{Origin:'https://cross-origin.invalid'}});
    expect(crossOrigin.status()).toBe(403);
    const followup='QA_SHARE_LATER_'+Date.now();
    await page.locator('.rail-btn[data-panel="chat"]').click();
    await page.locator('#conversationBoardBack').click();await page.locator('#msg').fill(followup);await page.locator('#btnSend').click();
    await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_SHARE_LATER_');
    await expect.poll(async()=>(await api(page,'/api/session/status?session_id='+sid)).body.agent_running).toBe(false);
    expect((await (await anonymous.request.get(publicAPI)).json()).share).toEqual(firstSnapshot);
    await page.reload();await expect(page.locator('#msg')).toBeVisible();await page.locator('.rail-btn[data-panel="settings"]').click();
    await expect(page.locator('#btnStopSharingSession')).toBeEnabled();
    const refreshed=page.waitForResponse(r=>r.url().endsWith('/api/share/create')&&r.request().method()==='POST');
    await page.locator('#btnShareSession').click();await page.locator('#appDialogCancel').click();
    expect((await refreshed).status()).toBe(200);
    expect(JSON.stringify((await (await anonymous.request.get(publicAPI)).json()).share)).toContain(followup);
    await publicPage.reload();await expect(publicPage.locator('#shareTranscript')).toContainText(followup);
    await publicPage.screenshot({path:info.outputPath('public-share-desktop.png'),fullPage:true});
    await publicPage.setViewportSize({width:390,height:844});
    await expect(publicPage.locator('#shareCopyBtn')).toBeVisible();
    await expect(publicPage.locator('#shareHomeLink')).toBeVisible();
    expect(await publicPage.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
    await publicPage.screenshot({path:info.outputPath('public-share-mobile.png'),fullPage:true});
    await page.locator('#btnStopSharingSession').click(); await page.locator('#appDialogCancel').click();
    expect((await anonymous.request.get(href)).status()).toBe(200);
    await page.locator('#btnStopSharingSession').click(); await page.locator('#appDialogConfirm').click();
    await expect.poll(async()=>(await api(page,'/api/session?session_id='+sid)).body.session.share_token||null).toBe(null);
    expect((await anonymous.request.get(href)).status()).toBe(404);
    const response=await publicPage.reload(); expect(response!.status()).toBe(404);
    await expect(publicPage.locator('body')).not.toContainText(marker);
    expect((await api(page,'/api/session?session_id='+sid)).body.session.messages.length).toBeGreaterThan(0);
    expect(publicErrors,'anonymous public-page uncaught errors').toEqual([]);
    await capture(page,'sharing-revoked',info);
  } finally {await anonymous.close();}
});

test.describe('Public share publisher ownership',()=>{
 test.use({user:'alice'});
 test('US-SP-SESS-SHARE-AUTH a readable group participant cannot publish or revoke its owners snapshot',async({page,browser})=>{
  const sid=await create(page,'QA_SHARE_OWNER_'+Date.now());
  expect((await api(page,'/api/session/participants',{session_id:sid,participants:['bob@example.test']})).status).toBe(200);
  const other=await browser.newContext();
  try{
   await other.route('**/*',route=>['127.0.0.1','localhost'].includes(new URL(route.request().url()).hostname)?route.continue():route.abort('blockedbyclient'));
   await other.addCookies([{name:auth.cookie_name,value:auth.cookies.bob,url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);
   const bob=await other.newPage();const errors:string[]=[];bob.on('pageerror',e=>errors.push(e.message));
   await bob.goto(auth.base_url+'/');await expect(bob.locator('#msg')).toBeVisible();
   expect((await api(bob,'/api/session?session_id='+sid)).status).toBe(200);
   expect((await api(bob,'/api/share/create',{session_id:sid})).status).toBe(404);
   const created=await api(page,'/api/share/create',{session_id:sid});expect(created.status).toBe(200);
   expect((await api(bob,'/api/share/revoke',{session_id:sid})).status).toBe(404);
   expect((await other.request.get(auth.base_url+created.body.share.url)).status()).toBe(200);
   expect((await api(page,'/api/share/revoke',{session_id:sid})).status).toBe(200);
   expect((await other.request.get(auth.base_url+created.body.share.url)).status()).toBe(404);
   expect(errors).toEqual([]);
  }finally{await other.close();}
 });
});
