import fs from 'node:fs';
import { test, expect, open, session, api, auth, capture } from './fixtures';
const marker = (prefix:string) => prefix+'-'+Date.now();
test('US-SP-BOT-001 wizard validates, preserves draft back navigation and creates real bot',async({page},info)=>{
 const name=marker('qa-bot');await open(page,'profiles');await page.getByRole('button',{name:'New bot',exact:true}).click();
 await page.locator('#builderNext').click();await expect(page.locator('#builderError')).toContainText('bot ID');
 await page.locator('#builderName').fill(name);await page.locator('#builderTitle').fill('QA Created Bot');await page.locator('#builderDescription').fill('Synthetic bot lifecycle');await page.locator('#builderNext').click();
 await page.locator('#builderPrompt').fill('QA_CREATED_BOT. Answer only the supplied synthetic request.');await page.locator('#builderBack').click();await expect(page.locator('#builderTitle')).toHaveValue('QA Created Bot');await page.locator('#builderNext').click();
 await expect(page.locator('#builderPrompt')).toHaveValue('QA_CREATED_BOT. Answer only the supplied synthetic request.');await page.locator('[data-selection="skills"][value="qa-review"]').check();await page.locator('#builderNext').click();await page.locator('#builderNext').click();
 await expect(page.locator('.bot-builder-review')).toContainText('qa-review');await page.locator('#builderNext').click();await expect(page.locator('#profileDetailTitle')).toContainText('QA Created Bot');
 const read=await api(page,'/api/bots/builder?profile='+name);expect(read.status).toBe(200);expect(read.body.config.skills).toContain('qa-review');
 await capture(page,'created-bot',info);
});
test('US-SP-BOT-004 six editor tabs, keyboard navigation, and save from instructions preserve real configuration',async({page},info)=>{
 await open(page,'profiles');await page.locator('#profilesPanel').getByText('Research',{exact:true}).click();await page.getByRole('button',{name:'Edit bot',exact:true}).click();
 for(const id of [0,1,4,5,2,3]){await page.locator('#builderTab'+id).click();await expect(page.locator('#builderTab'+id)).toHaveAttribute('aria-selected','true');await expect(page.locator('#builderTabPanel')).toBeVisible();await capture(page,'bot-tab-'+id,info);}
 await page.locator('#builderTab0').click();await page.locator('#builderTab0').press('ArrowRight');await expect(page.locator('#builderTab1')).toHaveAttribute('aria-selected','true');
 await page.locator('#builderPrompt').fill('QA_BOT_RESEARCH. Updated through actual frontend.');await page.locator('#builderNext').click();await expect(page.locator('#profileDetailTitle')).toContainText('Research');
 expect((await api(page,'/api/bots/builder?profile=qa-research')).body.config.system_prompt).toBe('QA_BOT_RESEARCH. Updated through actual frontend.');
});
test('US-SP-KNOW-001 upload does not select; explicit selection saves and survives editor reopen',async({page})=>{
 await open(page,'profiles');await page.locator('#profilesPanel').getByText('Research',{exact:true}).click();await page.getByRole('button',{name:'Edit bot',exact:true}).click();await page.locator('#builderTab4').click();
 const name=marker('qa-knowledge')+'.txt';await page.locator('[data-knowledge-upload]').setInputFiles({name,mimeType:'text/plain',buffer:Buffer.from('Synthetic bot knowledge evidence.')});
 const docs=page.locator('.sp-picker:has(#botKnowledgeSelect)');await expect(docs.locator('.sp-chip').filter({hasText:name})).toHaveCount(0);await docs.locator('.sp-picker-control').click();const option=page.locator('.sp-picker-menu .sp-picker-option').filter({hasText:name});await expect(option).toBeVisible();await expect(option).not.toHaveClass(/is-selected/);await option.click();await page.keyboard.press('Escape');await expect(docs.locator('.sp-chip').filter({hasText:name})).toBeVisible();await page.locator('[data-knowledge-save]').click();await expect(page.locator('[data-knowledge-status]')).toHaveText('Knowledge selection saved.');
 const read=await api(page,'/api/bots/knowledge?profile=qa-research');expect(read.status).toBe(200);const doc=read.body.files.find((x:any)=>x.name===name);expect(read.body.selected).toContain(doc.id);
 await page.locator('#builderTab1').click();await page.locator('#builderTab4').click();await expect(page.locator('.sp-picker:has(#botKnowledgeSelect) .sp-chip').filter({hasText:name})).toBeVisible();
});
test('US-SP-PROJ-001 create project, configure people and bots, upload file, reopen membership',async({page})=>{
 const name=marker('QA Project');await open(page,'projects');await page.locator('#projNewName').fill(name);await page.locator('#projCreateButton').click();await expect(page.locator('#projSummary')).toContainText(name);
 await page.locator('[data-project-section="team"] > summary').click();
  await page.locator('#projHumanChoices input[value="bob@example.test"]').check();await page.locator('#projBotChoices input[value="qa-research"]').check();await page.locator('#projSaveTeam').click();await expect(page.locator('#projHumanChoices input[value="bob@example.test"]')).toBeChecked();
 await page.locator('[data-project-section="uploads"] > summary').click();
  await page.locator('#projUpload').setInputFiles({name:'qa-project.txt',mimeType:'text/plain',buffer:Buffer.from('Synthetic project evidence')});await expect(page.locator('#projFileList')).toContainText('qa-project.txt');
 await page.reload();await page.locator('.rail-btn[data-panel="projects"]').click();await page.locator('.proj-row').filter({hasText:name}).click();await expect(page.locator('#projHumanChoices input[value="bob@example.test"]')).toBeChecked();await expect(page.locator('#projBotChoices input[value="qa-research"]')).toBeChecked();
});
test('US-SP-MEM-001 personal memory edits persist, cancel preserves previous value',async({page})=>{
 await open(page,'memory');await page.locator('#memoryPanel .side-menu-item').filter({hasText:'My Notes'}).click();await page.locator('#btnEditMemoryDetail').click();const text=marker('QA_PERSONAL_ADMIN');await page.locator('#memEditContent').fill(text);await page.locator('#btnSaveMemoryDetail').click();await expect(page.locator('#memoryDetailBody')).toContainText(text);
 await page.locator('#btnEditMemoryDetail').click();await page.locator('#memEditContent').fill('discard me');await page.locator('#btnCancelMemoryDetail').click();await expect(page.locator('#memoryDetailBody')).toContainText(text);
 await page.reload();await page.locator('.rail-btn[data-panel="memory"]').click();await page.locator('#memoryPanel .side-menu-item').filter({hasText:'My Notes'}).click();await expect(page.locator('#memoryDetailBody')).toContainText(text);
});
test('US-SP-SKILL-001 create real skill and filter list without changing stored content',async({page})=>{
 const name=marker('qa-skill');await open(page,'skills');await page.getByRole('button',{name:'New skill',exact:true}).click();await page.locator('#skillFormName').fill(name);await page.locator('#skillFormCategory').fill('qa');await page.locator('#skillFormContent').fill('---\nname: '+name+'\ndescription: Synthetic workflow validation\n---\nUse synthetic evidence only.');await page.locator('#btnSaveSkillDetail').click();await expect(page.locator('#skillDetailBody')).toContainText('Use synthetic evidence only.');
 await page.locator('#skillsSearch').fill(name);await expect(page.locator('#skillsList')).toContainText(name);await page.locator('#skillsSearch').fill('NO_SUCH_SKILL');await expect(page.locator('#skillsList')).not.toContainText(name);await page.locator('#skillsSearch').fill('');await expect(page.locator('#skillsList')).toContainText(name);
});
test('US-SP-WS-001 invalid workspace rejected; valid child added and renamed',async({page})=>{
 const child=auth.workspace+'/'+marker('qa-space');fs.mkdirSync(child);await open(page,'workspaces');await page.getByRole('button',{name:'Add space',exact:true}).click();await page.locator('#workspaceFormPath').fill(auth.workspace+'/missing');await page.locator('#btnSaveWorkspaceDetail').click();await expect(page.locator('#workspaceFormError')).toBeVisible();
 await page.locator('#workspaceFormPath').fill(child);await page.locator('#workspaceFormName').fill('QA child');await page.locator('#btnSaveWorkspaceDetail').click();await expect(page.locator('#workspaceDetailTitle')).toContainText('QA child');await page.locator('#btnEditWorkspaceDetail').click();await expect(page.locator('#workspaceFormPath')).toBeDisabled();await page.locator('#workspaceFormName').fill('QA child renamed');await page.locator('#btnSaveWorkspaceDetail').click();await expect(page.locator('#workspaceDetailTitle')).toContainText('QA child renamed');
});
test('US-SP-CRON-001 creates local scheduled task, reads persisted config and pauses',async({page})=>{
 const name=marker('QA Cron');await open(page,'tasks');await page.getByRole('button',{name:'New job',exact:true}).click();await page.locator('#cronFormName').fill(name);await page.locator('#cronFormPrompt').fill('QA_CRON. Return a short synthetic status.');await page.locator('#cronFormSchedulePreset').selectOption('daily');await page.locator('#cronFormScheduleTime').fill('09:30');await page.locator('#cronFormProfile').selectOption('qa-research');await page.locator('#cronFormDeliver').selectOption('local');await page.locator('#btnSaveTaskDetail').click();await expect(page.locator('#taskDetailTitle')).toContainText(name);await expect(page.locator('#taskDetailBody')).toContainText('QA_CRON');await page.locator('#btnPauseTaskDetail').click();await expect(page.locator('#btnResumeTaskDetail')).toBeVisible();
});
test('US-SP-KAN-001 creates unassigned Triage task, opens detail and filters list',async({page})=>{
 const name=marker('QA Kanban');await open(page,'kanban');await page.locator('#kanbanNewTaskBtn').click();await page.locator('#kanbanTaskModalTitleInput').fill(name);await page.locator('#kanbanTaskModalBody').fill('Synthetic task; no dispatcher execution.');await page.locator('#kanbanTaskModalStatus').selectOption('triage');await page.locator('#kanbanTaskModalAssignee').selectOption('');await page.locator('#kanbanTaskModalSubmit').click();await expect(page.locator('#kanbanList')).toContainText(name);await page.locator('#kanbanSearch').fill(name);await expect(page.locator('#kanbanList')).toContainText(name);await page.locator('#kanbanSearch').fill('NO_MATCH');await expect(page.locator('#kanbanList')).not.toContainText(name);
});
