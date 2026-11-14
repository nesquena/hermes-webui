import fs from 'node:fs';
import path from 'node:path';
import {test,expect,open,session,api,auth,capture} from './fixtures';
const unique=(prefix:string)=>prefix+'-'+Date.now();
test('US-SP-FILE-DEEP editor save reopen download rename cancel and delete persist on disk',async({page},info)=>{
 await open(page);await session(page);await page.locator('#btnWorkspacePanelToggle').click();const name=unique('qa-edit')+'.txt';const target=path.join(auth.workspace,name);
 await page.locator('#btnNewFile').click();await page.locator('#appDialogInput').fill(name);await page.locator('#appDialogConfirm').click();await expect(page.locator('#previewPathText')).toContainText(name);await page.locator('#btnEditFile').click();await page.locator('#previewEditArea').fill('QA file saved through frontend.\nSecond line.');await page.locator('#btnEditFile').click();await expect(page.locator('#previewEditArea')).toBeHidden();expect(fs.readFileSync(target,'utf8')).toBe('QA file saved through frontend.\nSecond line.');
 await page.locator('#btnClearPreview').click();const row=page.locator('#fileTree .file-item').filter({hasText:name});await row.click();await expect(page.locator('#previewCode')).toContainText('Second line.');const download=page.waitForEvent('download');await page.locator('#btnDownloadFile').click();const result=await download;expect(result.suggestedFilename()).toBe(name);expect(fs.readFileSync((await result.path())!,'utf8')).toBe(fs.readFileSync(target,'utf8'));
 await page.locator('#btnClearPreview').click();await row.click({button:'right'});await page.locator('.file-ctx-menu').getByText('Rename',{exact:true}).click();const renamed=unique('qa-renamed')+'.txt';await page.locator('#appDialogInput').fill(renamed);await page.locator('#appDialogConfirm').click();await expect(page.locator('#fileTree')).toContainText(renamed);expect(fs.existsSync(target)).toBe(false);const next=path.join(auth.workspace,renamed);expect(fs.existsSync(next)).toBe(true);
 const newRow=page.locator('#fileTree .file-item').filter({hasText:renamed});await newRow.click({button:'right'});await page.locator('.file-ctx-menu').getByText('Delete',{exact:true}).click();await page.locator('#appDialogCancel').click();expect(fs.existsSync(next)).toBe(true);await newRow.click({button:'right'});await page.locator('.file-ctx-menu').getByText('Delete',{exact:true}).click();await page.locator('#appDialogConfirm').click();await expect(newRow).toHaveCount(0);expect(fs.existsSync(next)).toBe(false);await capture(page,'file-after-delete',info);
});
test('US-SP-TERM-DEEP actual terminal executes harmless local command collapses expands restarts closes',async({page},info)=>{
 const terminalAssets:string[]=[];const terminalResponses:{url:string,status:number}[]=[];
 page.on('request',r=>{if(/xterm/.test(r.url()))terminalAssets.push(r.url());});
 page.on('response',r=>{if(/\/static\/vendor\/xterm[^/]*\//.test(r.url()))terminalResponses.push({url:r.url(),status:r.status()});});
 await open(page);await session(page);await page.locator('#msg').fill('/terminal');await page.locator('#btnSend').click();await expect(page.locator('#composerTerminalPanel')).toBeVisible();
 await expect(page.locator('#terminalSurface .xterm-helper-textarea')).toBeAttached();
 await expect.poll(()=>terminalResponses.filter(r=>r.status===200).length).toBe(4);
 expect(terminalAssets).toHaveLength(4);for(const asset of terminalAssets)expect(new URL(asset).origin).toBe(new URL(auth.base_url).origin);
 await info.attach('terminal-local-assets',{body:JSON.stringify(terminalResponses),contentType:'application/json'});
 const target=path.join(auth.workspace,unique('qa-terminal')+'.txt');await page.locator('#terminalSurface .xterm-helper-textarea').focus();await page.keyboard.insertText("printf QA_TERMINAL_EXECUTED > '"+target+"'");await page.keyboard.press('Enter');await expect.poll(()=>fs.existsSync(target)&&fs.readFileSync(target,'utf8')).toBe('QA_TERMINAL_EXECUTED');
 await page.locator('#btnTerminalCollapse').click();await expect(page.locator('#composerTerminalDock')).toBeVisible();await page.locator('#btnTerminalExpand').click();await expect(page.locator('#terminalSurface')).toBeVisible();await page.locator('#btnTerminalClear').click();await page.locator('#btnTerminalRestart').click();await expect(page.locator('#terminalSurface .xterm-helper-textarea')).toBeAttached();await page.locator('#terminalSurface .xterm-helper-textarea').focus();await page.keyboard.insertText("printf QA_TERMINAL_RESTARTED > '"+target+"'");await page.keyboard.press('Enter');await expect.poll(()=>fs.readFileSync(target,'utf8')).toBe('QA_TERMINAL_RESTARTED');await capture(page,'terminal-executed',info);await page.locator('#btnTerminalClose').click();await expect(page.locator('#composerTerminalPanel')).toBeHidden();
});
test('US-SP-SESS-SPLIT all split counts create real independent frames and exit',async({page},info)=>{
 await open(page);await session(page);await page.locator('#btnSplitView').click();await expect(page.locator('#splitOverlay')).toBeVisible();const counts=await page.locator('#splitCounts [data-count]').evaluateAll(es=>es.map(e=>Number(e.getAttribute('data-count'))));expect(counts.length).toBeGreaterThan(1);for(const count of counts){await page.locator(`#splitCounts [data-count="${count}"]`).click();await expect(page.locator('#splitGrid iframe')).toHaveCount(count);for(let i=0;i<count;i++)await expect(page.frameLocator('#splitGrid iframe').nth(i).locator('#msg')).toBeVisible();}await capture(page,'split-all-counts',info);await page.locator('#btnSplitExit').click();await expect(page.locator('#splitOverlay')).toBeHidden();await expect(page.locator('#msg')).toBeVisible();
});
test('US-SP-TODO-DEEP real agent todo create update clear renders live and survives session reload',async({page},info)=>{
 await open(page); const sid=await session(page);
 const original=(await api(page,'/api/settings')).body.workspace_todos_tab === true;
 async function showTodos(){
  if(await page.locator('html').getAttribute('data-workspace-panel')==='closed') await page.locator('#btnWorkspacePanelToggle').click();
  await page.locator('#workspaceTodosTab').click();
 }
 try {
  expect((await api(page,'/api/settings',{workspace_todos_tab:true})).status).toBe(200);
  await page.reload();
  for(const [action,label] of [['create','QA todo created'],['update','QA todo updated'],['clear','No active task list in this session.']] as const){
   await page.locator('#msg').fill('QA_TODO|'+action);await page.locator('#btnSend').click();
   await expect.poll(async()=>(await api(page,'/api/session?session_id='+sid)).body.session.messages.filter((m:any)=>m.role==='assistant' && String(m.content).includes('QA_TODO_RESULT:')).length).toBe(['create','update','clear'].indexOf(action)+1);
   await showTodos(); await expect(page.locator('#workspaceTodosPanel')).toContainText(label);
   await page.reload(); await showTodos(); await expect(page.locator('#workspaceTodosPanel')).toContainText(label);
   if(action==='update')await expect(page.locator('#workspaceTodosPanel')).not.toContainText('QA todo created');
  }
  await capture(page,'todos-cleared',info);
 } finally { expect((await api(page,'/api/settings',{workspace_todos_tab:original})).status).toBe(200); }
});
test('US-SP-BOT-DEEP all editable tab fields filter select save and delete lifecycle',async({page},info)=>{
 const name=unique('qa-deep-bot');await open(page,'profiles');await page.getByRole('button',{name:'New bot',exact:true}).click();await page.locator('#builderName').fill(name);await page.locator('#builderTitle').fill(name);await page.locator('#builderDescription').fill('Initial');await page.locator('#builderNext').click();await page.locator('#builderPrompt').fill('QA deep bot instructions');await page.locator('#builderNext').click();await page.locator('#builderNext').click();await page.locator('#builderNext').click();await expect(page.locator('#profileDetailTitle')).toHaveText(name);await page.getByRole('button',{name:'Edit bot',exact:true}).click();
 await page.locator('#builderDescription').fill('Updated identity');await page.locator('#builderTab4').click();await page.locator('#builderMemory').fill('QA shared bot memory');await page.locator('#builderTab5').click();await page.locator('[data-filter="skills"]').fill('qa-review');await page.locator('[data-selection="skills"][value="qa-review"]').check();await page.locator('#builderTab2').click();await page.locator('[data-filter="mcp_servers"]').fill('qa-local');await page.locator('[data-selection="mcp_servers"][value="qa-local"]').check();await page.locator('[data-filter="cli_tools"]').fill('git');await page.locator('[data-selection="cli_tools"][value="git"]').check();await page.locator('#builderTab3').click();await page.locator('[data-filter="allowed_users"]').fill('bob@example.test');await page.locator('[data-selection="allowed_users"][value="bob@example.test"]').check();await page.locator('[data-selection="allowed_groups"][value="qa-team"]').check();await page.locator('#builderNext').click();await expect(page.locator('#profileDetailTitle')).toHaveText(name);
 const read=await api(page,'/api/bots/builder?profile='+name);expect(read.status).toBe(200);expect(read.body.config).toMatchObject({description:'Updated identity',bot_memory:'QA shared bot memory',skills:['qa-review'],mcp_servers:['qa-local'],cli_tools:['git'],allowed_users:expect.arrayContaining(['bob@example.test']),allowed_groups:['qa-team']});await capture(page,'bot-all-fields-saved',info);await page.locator('#btnDeleteProfileDetail').click();await page.locator('#appDialogCancel').click();await expect(page.locator('#profileDetailTitle')).toHaveText(name);await page.locator('#btnDeleteProfileDetail').click();await page.locator('#appDialogConfirm').click();await expect(page.locator('#profilesPanel')).not.toContainText(name);const absent=await api(page,'/api/profiles');expect(absent.status).toBe(200);expect(absent.body.profiles.map((x:any)=>x.name)).not.toContain(name);
});
