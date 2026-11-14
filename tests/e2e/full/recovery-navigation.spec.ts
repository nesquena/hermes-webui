import fs from 'node:fs';
import path from 'node:path';
import {test,expect,open,session,api,auth} from './fixtures';

test('US-SP-NAV-PANEL-CLICKS rail labels and workspace edge toggle preserve draft and workspace default survives reload',async({page})=>{
  await open(page);await session(page);
  const draft='QA navigation must preserve this unsent draft';
  await page.locator('#msg').fill(draft);
  const rail=page.locator('#railExpandToggle');
  for(const expanded of [false,true]){
    await rail.click();
    await expect(rail).toHaveAttribute('aria-pressed',String(expanded));
    await expect(rail).toHaveAttribute('aria-label',expanded?'Hide labels':'Show labels');
    await expect.poll(()=>page.evaluate(()=>localStorage.getItem('hermes-webui-rail-expanded'))).toBe(expanded?'1':'0');
    await expect(page.locator('#msg')).toHaveValue(draft);
  }
  if(await page.locator('html').getAttribute('data-workspace-panel')!=='closed') await page.locator('#btnWorkspacePanelToggle').click();
  await page.locator('#btnWorkspacePanelEdgeToggle').click();
  await expect(page.locator('.rightpanel')).toBeVisible();
  await expect(page.locator('#fileTree')).toContainText('qa-evidence.txt');
  await page.locator('#btnWorkspacePanelToggle').click();
  await expect(page.locator('html')).toHaveAttribute('data-workspace-panel','closed');
  await expect(page.locator('#msg')).toHaveValue(draft);
  // This preference is deliberately browser-local, not a server setting.
  for(const enabled of [true,false]){
    await page.locator('.rail-btn[data-panel="settings"]').click();
    await page.locator('[data-settings-section="appearance"]').click();
    const field=page.locator('#settingsWorkspacePanelOpen');
    await field.setChecked(enabled);
    await expect.poll(()=>page.evaluate(()=>localStorage.getItem('hermes-webui-workspace-panel-pref'))).toBe(enabled?'open':'closed');
    await page.locator('.rail-btn[data-panel="chat"]').click();
    await page.locator('#conversationBoardBack').click();
    await page.reload();await expect(page.locator('#msg')).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute('data-workspace-panel',enabled?'open':'closed');
    await session(page);
    await expect(page.locator('html')).toHaveAttribute('data-workspace-panel',enabled?'open':'closed');
    if(enabled) await expect(page.locator('#fileTree')).toContainText('qa-evidence.txt');
  }
});

test('US-SP-LOG-CLIPBOARD-CLICKS copy exact visible severity lines and refresh reads appended disk evidence',async({page,context})=>{
  const root=path.dirname(auth.workspace);
  expect(path.basename(root)).toBe('e2e-state');
  const file=path.join(root,'home','logs','gateway.log');
  fs.mkdirSync(path.dirname(file),{recursive:true});
  const previous=fs.existsSync(file)?fs.readFileSync(file):null;
  const lines=['INFO QA informational café','WARNING QA warning two','ERROR QA failure three'];
  fs.writeFileSync(file,lines.join('\n')+'\n');
  try{
    await context.grantPermissions(['clipboard-read','clipboard-write']);
    await open(page,'logs');
    await page.locator('#logsAutoRefresh').uncheck();
    const loaded=page.waitForResponse(r=>r.url().includes('/api/logs?file=gateway'));
    await page.locator('#logsFile').selectOption('gateway');expect((await loaded).status()).toBe(200);
    for(const [filter,selected] of [['all',lines],['warnings',lines.slice(1)],['errors',lines.slice(2)]] as const){
      await page.locator('#logsSeverityFilter').selectOption(filter);
      await expect(page.locator('#logsOutput .log-line')).toHaveText([...selected]);
      await page.locator('#logsCopyAll').click();
      await expect.poll(()=>page.evaluate(()=>navigator.clipboard.readText())).toBe(selected.join('\n'));
    }
    fs.appendFileSync(file,'ERROR QA appended exact line\n');
    const refreshed=page.waitForResponse(r=>r.url().includes('/api/logs?file=gateway'));
    await page.locator('#logsRefreshBtn').click();expect((await refreshed).status()).toBe(200);
    await expect(page.locator('#logsOutput .log-line')).toHaveText([lines[2],'ERROR QA appended exact line']);
    expect((await api(page,'/api/logs?file=gateway&tail=100')).body.lines).toEqual([...lines,'ERROR QA appended exact line']);
    await page.locator('#logsCopyAll').click();
    await expect.poll(()=>page.evaluate(()=>navigator.clipboard.readText())).toBe(lines[2]+'\nERROR QA appended exact line');
  }finally{if(previous===null)fs.unlinkSync(file);else fs.writeFileSync(file,previous);}
});
