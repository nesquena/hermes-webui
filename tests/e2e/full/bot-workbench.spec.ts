import {test, expect, open, auth, api} from './fixtures';

async function ready(page:any){
  await open(page);
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first().click();
}

test('BOT WORKBENCH catalog favorites and layout are personal and keep search local',async({page},info)=>{
  await ready(page);
  await page.locator('.rail [data-panel="profiles"]').click();
  await expect(page.locator('.bot-directory-card')).toHaveCount(2);
  await page.screenshot({path:info.outputPath('bots-directory-desktop.png')});
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>closeMobileSidebar());
  await expect(page.locator('#botDirectory')).toBeVisible();
  await page.screenshot({path:info.outputPath('bots-directory-mobile.png')});
  await page.setViewportSize({width:1440,height:1000});
  const writes:string[]=[];page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))writes.push(r.url());});
  await page.locator('[data-bot-favorite="qa-research"]').click();
  await page.locator('[data-bot-filter="favorites"]').click();
  await expect(page.locator('.bot-directory-card')).toHaveCount(1);
  await expect(page.locator('.profile-card[data-name]')).toHaveCount(1);
  await page.locator('#profileSearch').fill('does-not-exist');
  await expect(page.locator('#botDirectory .sp-search-empty')).toBeVisible();
  await page.locator('#botDirectory [data-reset-bots]').click();
  await expect(page.locator('.bot-directory-card')).toHaveCount(2);
  await page.locator('[data-bot-layout="list"]').click();
  await page.reload();await page.locator('.rail [data-panel="profiles"]').click();
  await expect(page.locator('#botDirectory')).toHaveAttribute('data-layout','list');
  await expect(page.locator('[data-bot-favorite="qa-research"]')).toHaveAttribute('aria-pressed','true');
  expect(writes).toEqual([]);
  await page.context().addCookies([{name:auth.cookie_name,value:auth.cookies.alice,url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);
  await page.reload();await page.locator('.rail [data-panel="profiles"]').click();
  await expect(page.locator('[data-bot-favorite="qa-research"]')).toHaveAttribute('aria-pressed','false');
  await expect(page.locator('#botDirectory')).toHaveAttribute('data-layout','grid');
});

test('BOT WORKBENCH chat action selects the bot and uses the existing conversation engine',async({page})=>{
  await ready(page);await page.locator('.rail [data-panel="profiles"]').click();
  await page.locator('[data-bot-open="qa-research"]').click();
  await page.locator('[data-bot-chat="qa-research"]').click();
  await expect(page.locator('#msg')).toBeVisible();
  await expect.poll(()=>page.evaluate(()=>S.activeProfile)).toBe('qa-research');
  await page.locator('#msg').fill('QA_BOT_WORKSPACE_CHAT');await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_BOT_WORKSPACE_CHAT',{timeout:30000});
  const sid=await page.evaluate(()=>S.session.session_id);
  const persisted=(await api(page,'/api/session?session_id='+encodeURIComponent(sid))).body.session;
  expect(persisted.profile).toBe('qa-research');
});

test('BOT WORKBENCH detail sections preserve drafts and remain usable at narrow widths',async({page},info)=>{
  await ready(page);await page.locator('.rail [data-panel="profiles"]').click();
  await page.locator('[data-bot-open="qa-research"]').click();
  await expect(page.locator('#botDetailTab-overview')).toHaveAttribute('aria-selected','true');
  await expect(page.locator('#botTitle')).toBeHidden();
  await page.screenshot({path:info.outputPath('bot-overview-desktop.png')});
  await page.locator('#botDetailTab-identity').click();
  await page.locator('#botTitle').fill('Keep this identity draft');
  await page.locator('#botDetailTab-work').click();
  await expect(page.locator('#profileBotJobs')).toBeVisible();
  await page.locator('#botDetailTab-overview').focus();await page.keyboard.press('End');
  await expect(page.locator('#botDetailTab-configuration')).toBeFocused();
  await page.locator('#botDetailTab-identity').click();
  await expect(page.locator('#botTitle')).toHaveValue('Keep this identity draft');
  for(const width of [900,390]){
    await page.setViewportSize({width,height:900});
    await page.evaluate(()=>closeMobileSidebar());
    await page.locator('#botDetailTab-overview').click();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await expect(page.locator('[data-bot-chat="qa-research"]')).toBeInViewport();
    await page.screenshot({path:info.outputPath('bot-overview-'+width+'.png')});
  }
});

test('BOT WORKBENCH searchable switcher preserves query across refresh and restores focus on Escape',async({page},info)=>{
  await ready(page);await page.locator('#msg').fill('Keep my chat draft');
  await page.locator('#profileChip').click();
  await page.locator('#profilePickerSearch').fill('Research');
  await expect(page.locator('#profileDropdown [data-profile-name]:visible')).toHaveCount(1);
  await page.evaluate(()=>renderProfileDropdown(_profilesCache));
  await expect(page.locator('#profilePickerSearch')).toHaveValue('Research');
  await expect(page.locator('#profilePickerSearch')).toBeFocused();
  await page.screenshot({path:info.outputPath('bot-switcher.png')});
  await page.keyboard.press('ArrowDown');
  await expect(page.locator('#profileDropdown [data-profile-name="qa-research"]')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#profileDropdown')).not.toHaveClass(/open/);
  await expect(page.locator('#profileChip')).toBeFocused();
  await expect(page.locator('#msg')).toHaveValue('Keep my chat draft');
});
