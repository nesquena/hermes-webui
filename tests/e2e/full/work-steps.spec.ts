import {test,expect,open,session,api,auth} from './fixtures';
import path from 'node:path';

test.use({user:'alice'});
test('WORK STEPS actual actions stay numbered and counted after reload without opening detail',async({page},info)=>{
  test.setTimeout(60000);
  await open(page);
  await api(page,'/api/settings',{chat_activity_display_mode:'compact_worklog'});
  await page.reload();const sid=await session(page);
  await page.locator('#msg').fill('QA_STEP_OVERVIEW|'+path.join(auth.workspace,'qa-evidence.txt'));
  await page.locator('#btnSend').click();
  await expect(page.locator('#messages')).toContainText('QA_STEPS_COMPLETE:',{timeout:25000});
  await expect.poll(async()=>(await api(page,'/api/session/status?session_id='+sid)).body.agent_running).toBe(false);
  for(const reloaded of [false,true]){
    if(reloaded){await page.reload();await expect(page.locator('#msg')).toBeVisible();}
    const group=page.locator('[data-anchor-scene-owner="1"]').first();
    await group.screenshot({path:info.outputPath('steps-'+(reloaded?'reloaded':'settled')+'.png')});
    await expect(group.locator('.work-step-summary')).toHaveText('3 steps · 1 failed · 2 done');
    const summary=group.locator('.tool-call-group-summary');
    if(await summary.getAttribute('aria-expanded')==='false')await summary.click();
    await expect(group.locator('.work-step-number')).toHaveText(['1','2','3']);
    await expect(group.locator('.tool-card-header')).toHaveCount(3);
    for(const header of await group.locator('.tool-card-header').all())await expect(header).toBeVisible();
    const header=group.locator('.tool-card-header').first();
    await header.focus();await page.keyboard.press('Enter');
    await expect(header).toHaveAttribute('aria-expanded','true');
    await page.keyboard.press('Space');await expect(header).toHaveAttribute('aria-expanded','false');
  }
  await page.evaluate(()=>setLocale('nl'));
  await expect(page.locator('.work-step-summary')).toHaveText('3 stappen · 1 mislukt · 2 klaar');
  await page.locator('#msg').fill('QA_STEP_OVERVIEW|'+path.join(auth.workspace,'qa-evidence.txt'));
  await page.locator('#btnSend').click();
  await expect.poll(async()=>(await api(page,'/api/session/status?session_id='+sid)).body.agent_running).toBe(false);
  await expect(page.locator('.work-step-summary')).toHaveCount(2);
  const dutch=page.locator('[data-anchor-scene-owner="1"]').last();
  const disclosure=dutch.locator('.tool-call-group-summary');
  if(await disclosure.getAttribute('aria-expanded')==='false')await disclosure.click();
  await expect(dutch.locator('.tool-card-header').first()).toContainText('Bestand gelezen');
  for(const width of [1440,900,390]){
    await page.setViewportSize({width,height:width===390?844:1000});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await dutch.screenshot({path:info.outputPath('steps-dutch-'+width+'.png')});
  }
});
