import path from 'node:path';
import fs from 'node:fs';
import {test, expect, open, session, api, auth} from './fixtures';

async function ready(page:any){
  await open(page);
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first().click();
}
async function send(page:any, marker:string){
  const url='/api/session?session_id='+page.url().split('/session/')[1];
  const before=(await api(page,url)).body.session.messages.length;
  await page.locator('#msg').fill(marker); await page.locator('#btnSend').click();
  await expect.poll(async()=>(await api(page,url)).body.session.messages.length).toBeGreaterThanOrEqual(before+2);
  await expect.poll(async()=>(await api(page,'/health')).body.active_runs||0).toBe(0);
}

test('CONVERSATION WORKBENCH overview preserves drafts, filters persisted pins and opens the chosen conversation',async({page},info)=>{
  await ready(page);const first=await session(page);await send(page,'QA_BOARD_FIRST');
  expect((await api(page,'/api/session/rename',{session_id:first,title:'Launch plan · customer onboarding'})).status).toBe(200);
  expect((await api(page,'/api/session/pin',{session_id:first,pinned:true})).status).toBe(200);
  await page.locator('#btnNewChat').click();await expect(page).not.toHaveURL(new RegExp('/session/'+first+'$'));
  const second=page.url().split('/session/')[1];await send(page,'QA_BOARD_SECOND');
  expect((await api(page,'/api/session/rename',{session_id:second,title:'Website review · next steps'})).status).toBe(200);
  await page.evaluate(()=>renderSessionList());
  await page.locator('#msg').fill('Keep this unsent draft');
  await page.screenshot({path:info.outputPath('before-overview.png')});
  await expect(page.locator('#btnConversationBoard')).toBeVisible({timeout:3000});
  await page.locator('#btnConversationBoard').click();
  await expect(page.locator('#msg')).toBeHidden();
  await expect(page.locator('[data-conversation-open="'+first+'"]')).toBeVisible();
  await expect(page.locator('[data-conversation-open="'+second+'"]')).toBeVisible();
  await page.screenshot({path:info.outputPath('conversations-desktop.png')});
  await page.evaluate(()=>document.documentElement.classList.remove('dark'));
  await page.screenshot({path:info.outputPath('conversations-light.png')});
  await page.evaluate(()=>document.documentElement.classList.add('dark'));
  await page.setViewportSize({width:900,height:900});
  await expect(page.locator('#conversationBoardSearch')).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('conversations-narrow.png')});
  await page.setViewportSize({width:1440,height:1000});
  const writes:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))writes.push(r.url());});
  await page.locator('#conversationBoardSearch').fill('Launch onboarding');
  await expect(page.locator('.conversation-board-card')).toHaveCount(1);
  await page.locator('[data-conversation-filter="pinned"]').click();
  await expect(page.locator('[data-conversation-open="'+first+'"]')).toBeVisible();
  await page.locator('#conversationBoardSearch').fill('no-such-session');
  await expect(page.locator('.conversation-board-card')).toHaveCount(0);
  await page.locator('#conversationBoardReset').click();
  await page.locator('#conversationBoardBack').click();
  await expect(page.locator('#mainConversations')).toBeHidden();
  await expect(page.locator('#msg')).toHaveValue('Keep this unsent draft');
  expect(writes).toEqual([]);
  await page.locator('#btnConversationBoard').click();
  await page.locator('[data-conversation-open="'+first+'"]').click();
  await expect(page).toHaveURL(new RegExp('/session/'+first+'$'));
  await expect(page.locator('#msg')).toBeVisible();
  await expect(page.locator('#messages')).toContainText('QA_BOARD_FIRST');
  await page.setViewportSize({width:390,height:844});await page.locator('#btnHamburger').click();
  await page.locator('#btnConversationBoard').click();
  await expect(page.locator('.sidebar')).not.toHaveClass(/mobile-open/);
  await expect(page.locator('#conversationBoardSearch')).toBeInViewport();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('conversations-mobile.png')});
  await page.locator('#conversationBoardNew').click();await expect(page.locator('#msg')).toBeVisible();
});

test('CONVERSATION WORKBENCH live activity follows the existing runtime and refresh keeps keyboard focus',async({page})=>{
  await ready(page);const sid=await session(page);
  await page.locator('#msg').fill('QA_SLOW_RESPONSE');await page.locator('#btnSend').click();
  await page.locator('#btnConversationBoard').click();
  await page.locator('[data-conversation-filter="running"]').click();
  await expect(page.locator('[data-conversation-open="'+sid+'"] [data-state="running"]')).toBeVisible();
  await expect(page.locator('[data-conversation-group="running"] [data-conversation-open="'+sid+'"]')).toBeVisible();
  await expect(page.locator('[data-conversation-filter="running"] .conversation-filter-count')).not.toHaveText('0');
  await page.locator('[data-conversation-filter="all"]').click();
  const card=page.locator('[data-conversation-open="'+sid+'"]');await card.focus();
  await page.evaluate(()=>renderSessionListFromCache());await expect(card).toBeFocused();
  await expect.poll(async()=>(await api(page,'/health')).body.active_runs||0).toBe(0);
  await page.evaluate(()=>renderSessionList());
  await expect(card.locator('[data-state="running"]')).toHaveCount(0);
  await expect(page.locator('[data-conversation-group="recent"] [data-conversation-open="'+sid+'"]')).toBeVisible();
  await card.press('Enter');await expect(page.locator('#messages')).toContainText('QA_SLOW_RESPONSE');
  await expect(page.locator('#mainConversations')).toBeHidden();
});

test('CONVERSATION WORKBENCH clears previous bot rows during switching and handles failed refresh',async({page})=>{
  await ready(page);const sid=await session(page);await send(page,'QA_BOARD_SCOPE');
  await page.locator('#btnConversationBoard').click();
  await expect(page.locator('[data-conversation-open="'+sid+'"]')).toBeVisible();
  let release!:()=>void,entered!:()=>void;
  const held=new Promise<void>(r=>{entered=r;}),gate=new Promise<void>(r=>{release=r;});
  await page.route('**/api/profile/switch',async route=>{entered();await gate;await route.continue();});
  try{
    await page.locator('#conversationBoardBack').click();
    await page.locator('#profileChip').click();
    await page.locator('#profileDropdown [data-profile-name="qa-research"]').click();await held;
    await page.locator('#btnConversationBoard').click();
    await expect(page.locator('.conversation-board-card')).toHaveCount(0);
    release();await expect.poll(()=>page.evaluate(()=>S.activeProfile)).toBe('qa-research');
    await expect(page.locator('#mainConversations')).toHaveAttribute('aria-busy','false');
    await expect(page.locator('[data-conversation-open="'+sid+'"]')).toHaveCount(0);
  }finally{release();await page.unroute('**/api/profile/switch');}
  await page.route('**/api/sessions?**',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"QA unavailable"}'}));
  await page.evaluate(()=>renderSessionList());
  await expect(page.locator('#conversationBoardNotice')).toContainText('could not be refreshed');
  await page.unroute('**/api/sessions?**');await page.locator('#conversationBoardNotice button').click();
  await expect(page.locator('#conversationBoardNotice')).toBeHidden();
  await page.context().addCookies([{name:auth.cookie_name,value:auth.cookies.bob,url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);
  await page.goto('/');await page.locator('#btnConversationBoard').click();
  await expect(page.locator('[data-conversation-open="'+sid+'"]')).toHaveCount(0);
});

test('CONVERSATION WORKBENCH files filter locally, preview the selected file and reset on session change',async({page},info)=>{
  test.setTimeout(60000);await ready(page);const sid=await session(page);
  const doc=path.join(auth.workspace,'launch-summary.md'),data=path.join(auth.workspace,'launch-metrics.csv');
  await send(page,'QA_GOV_WRITE|launch-summary.md');await send(page,'QA_GOV_WRITE|launch-metrics.csv');
  expect(fs.readFileSync(doc,'utf8')).toContain('QA_GOVERNANCE_EXECUTED');
  expect(fs.readFileSync(data,'utf8')).toContain('QA_GOVERNANCE_EXECUTED');
  await page.locator('#btnWorkspacePanelEdgeToggle').click();await page.locator('#workspaceArtifactsTab').click();
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(2);
  await page.locator('.rightpanel').screenshot({path:info.outputPath('artifacts-desktop.png')});
  const writes:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))writes.push(r.url());});
  await page.locator('#workspaceArtifactType').selectOption('documents');
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(1);
  await expect(page.locator('.workspace-artifact-item')).toContainText('launch-summary.md');
  await page.locator('#workspaceArtifactSearch').fill('no-match');
  await page.evaluate(()=>renderSessionArtifacts());
  await expect(page.locator('#workspaceArtifactSearch')).toHaveValue('no-match');
  await expect(page.locator('#workspaceArtifactSearch')).toBeFocused();
  await expect(page.locator('.workspace-artifact-empty')).toContainText('No files match');
  await page.locator('#workspaceArtifactReset').click();
  await page.locator('#workspaceArtifactSearch').fill('launch-metrics');
  await page.locator('.workspace-artifact-item').click();
  await expect(page.locator('#workspaceFilesTab')).toHaveAttribute('aria-selected','true');
  await expect.poll(()=>page.evaluate(()=>_previewCurrentPath)).toBe('launch-metrics.csv');
  expect(writes).toEqual([]);
  await session(page);await page.locator('#workspaceArtifactsTab').click();
  await expect(page.locator('#workspaceArtifactSearch')).toHaveValue('');
  await expect(page.locator('#workspaceArtifactType')).toHaveValue('all');
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(0);
  await page.evaluate(sid=>loadSession(sid),sid);await page.locator('#workspaceArtifactsTab').click();
  await expect(page.locator('.workspace-artifact-item')).toHaveCount(2);
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('#workspaceArtifactSearch')).toBeInViewport();
  // Wait for the existing slide-over animation before measuring its controls.
  await expect.poll(async()=>{const box=await page.locator('.rightpanel').boundingBox();return box!.x+box!.width;}).toBeLessThanOrEqual(391);
  for(const selector of ['#workspaceArtifactSearch','#workspaceArtifactType','#workspaceArtifactReset']){
    const box=await page.locator(selector).boundingBox();expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(391);
  }
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.locator('.rightpanel').screenshot({path:info.outputPath('artifacts-mobile.png')});
});
