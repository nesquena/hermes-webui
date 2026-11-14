import {test, expect, open} from './fixtures';

async function ready(page:any) {
  await open(page);
  await page.waitForFunction(()=>typeof S!=='undefined'&&S._bootReady&&_loadingSessionId===null);
  await page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first().click();
}

test('workspace clarity: page help is short and keeps the complete guide optional', async({page})=>{
  await ready(page);
  await page.locator('#msg').fill('Keep my unsent question');
  const catalogs:string[]=[];
  page.on('request',r=>{if(/help-(features|msp-features|feature-targets|guide)\.js/.test(r.url()))catalogs.push(r.url());});
  await page.locator('#btnPageHelp').click();
  const tip=page.locator('.sp-help-tooltip');
  await expect(tip).toBeVisible();
  const count=Number((await tip.locator('.guida-progress').innerText()).match(/\d+\D+(\d+)/)?.[1]);
  expect(count).toBeGreaterThan(1);
  expect(count).toBeLessThanOrEqual(6);
  expect(catalogs).toEqual([]);
  await tip.locator('[data-guide-link]').click();
  await expect(page.locator('#spFeatureGuide')).toBeVisible();
  expect(await page.locator('.sp-guide-result').count()).toBeGreaterThan(10);
  await page.keyboard.press('Escape');
  await expect(page.locator('#msg')).toHaveValue('Keep my unsent question');
  await expect(page.locator('#btnPageHelp')).toBeFocused();
});

test('workspace clarity: bot search is local and preserves the selected editor draft', async({page})=>{
  await ready(page);
  await page.locator('.rail [data-panel="profiles"]').click();
  await expect(page.locator('[data-bot-open="qa-research"]')).toBeVisible();
  await page.locator('[data-bot-open="qa-research"]').click();
  await page.evaluate(()=>openBotConfiguration('identity'));
  await expect(page.locator('#builderTitle')).toBeVisible();
  await page.locator('#builderTitle').fill('Unsent bot name');
  await page.evaluate(()=>{_botJobsCache=null;});
  const writes:string[]=[],reads:string[]=[];
  page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))writes.push(r.url());if(/\/api\/(profiles|cron|crons)/.test(r.url()))reads.push(r.url());});
  await page.locator('#profileSearch').fill('Research');
  await expect(page.locator('.profile-card[data-name]')).toHaveCount(1);
  await page.locator('#profileSearch').fill('no-matching-bot');
  await expect(page.locator('#profilesPanel .sp-search-empty')).toBeVisible();
  await page.locator('#profilesPanel [data-clear-profile-search]').click();
  await expect(page.locator('.profile-card[data-name]')).toHaveCount(2);
  await expect(page.locator('#builderTitle')).toHaveValue('Unsent bot name');
  expect(writes).toEqual([]);
  expect(reads).toEqual([]);
});

test('workspace clarity: mobile guide has separate results and detail views', async({page})=>{
  await ready(page);await page.setViewportSize({width:390,height:844});
  await page.evaluate(async()=>{await window.loadSynthPulseFeatureGuide();window.SynthPulseFeatureGuide.open({locale:'nl'});});
  await expect(page.locator('#spFeatureGuide')).toHaveAttribute('data-view','results');
  await expect(page.locator('.sp-guide-detail')).not.toBeVisible();
  await page.locator('#spGuideSearch').fill('meldingen dempen');
  await page.locator('[data-feature="webui.tasks.mute"]').click();
  await expect(page.locator('#spFeatureGuide')).toHaveAttribute('data-view','detail');
  await expect(page.locator('#spGuideFeatureTitle')).toBeVisible();
  await expect(page.locator('.sp-guide-results')).not.toBeVisible();
  await page.locator('[data-guide-back]').click();
  await expect(page.locator('#spGuideSearch')).toHaveValue('meldingen dempen');
  await expect(page.locator('[data-feature="webui.tasks.mute"]')).toBeFocused();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test('workspace clarity: bot editor stays usable with keyboard at desktop, laptop and mobile sizes', async({page},info)=>{
  await ready(page);
  await page.locator('.rail [data-panel="profiles"]').click();
  await page.locator('[data-bot-open="qa-research"]').click();
  await page.evaluate(()=>openBotConfiguration('identity'));
  await page.locator('#builderTitle').fill('Keep this bot draft');
  for(const width of [1440,900,390]){
    await page.setViewportSize({width,height:900});
    await page.evaluate(()=>closeMobileSidebar());
    const tabs=page.locator('.bot-builder-tabs');
    await expect(tabs).toHaveAttribute('aria-orientation',width>=1100?'vertical':'horizontal');
    await page.locator('#builderTab0').focus();
    await page.keyboard.press('End');
    await expect(page.locator('#builderTab7')).toBeFocused();
    await expect(page.locator('#builderTab7')).toHaveAttribute('aria-selected','true');
    await expect(page.locator('#builderTabPanel')).toHaveAttribute('aria-labelledby','builderTab7');
    await page.keyboard.press('Home');
    await expect(page.locator('#builderTitle')).toHaveValue('Keep this bot draft');
    await expect(page.locator('#builderTitle')).toBeInViewport();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:info.outputPath(`bot-editor-${width}.png`)});
  }
});

test('workspace clarity: unavailable connectors give a useful nontechnical recovery', async({page})=>{
  await ready(page);
  await page.route('**/api/integrations/connections',r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Nango secret key file is not readable: No such file or directory'})}));
  await page.route('**/api/integrations/hosted/accounts',r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Connection service unavailable'})}));
  await page.locator('.rail [data-panel="integrations"]').click();
  await expect(page.locator('#intgConnections')).not.toContainText('Nango');
  await expect(page.locator('#intgConnections')).toContainText(/refresh|retry|again/i);
  await expect(page.locator('#intgGrid')).not.toContainText('secret key');
  await page.route('**/api/integrations/connections',r=>r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({connections:[]})}));
  await page.route('**/api/integrations/hosted/accounts',r=>r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({accounts:[]})}));
  await page.locator('#mainIntegrations [data-i18n="integrations_refresh"]').click();
  await expect(page.locator('#intgConnections .intg-error')).toHaveCount(0);
});
