import fs from 'node:fs';
import path from 'node:path';
import {test,expect,open,api,auth,capture,picker,selectPicker} from './fixtures';
const unique=(s:string)=>'qa-admin-'+s+'-'+Date.now()+'-'+Math.random().toString(36).slice(2,6);
async function tab(page:any,name:string){await open(page,'governance');await page.locator(`[data-gov-tab="${name}"]`).click();}
async function replaceSelection(page:any,inputId:string,value:string){
 const root=picker(page,inputId),remove=root.locator('.sp-chip-x');await expect(root.getByRole('combobox')).toBeVisible();
 const existing=await remove.count();for(let i=0;i<existing;i++)await remove.first().click();
 await selectPicker(page,inputId,value);
}
async function write(page:any,url:string,data:any){const current=await api(page,'/api/governance/users');expect(current.status).toBe(200);return page.evaluate(async({url,data,etag})=>{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','If-Match':etag},body:JSON.stringify(data)});return {status:r.status,body:await r.json()};},{url,data,etag:current.body.etag});}
async function denied(browser:any,urls:[string,any?][]){const c=await browser.newContext({baseURL:auth.base_url});try{await c.addCookies([{name:auth.cookie_name,value:auth.cookies.outsider,url:auth.base_url}]);for(const [url,data] of urls){const r=data===undefined?await c.request.get(url):await c.request.post(url,{data,headers:{Origin:auth.base_url}});expect(r.status(),url).toBe(403);}}finally{await c.close();}}

for(const template of ['viewers','operators','engineers'])test(`US-SP-GOV-ADMIN-GROUPS ${template} template and six grant fields create edit cancel clear and delete with persisted negative checks`,async({page,browser},info)=>{
 test.setTimeout(60000);
 const fields=[['SkillsView','qa-review'],['SkillsLoad','qa-review'],['SkillsManage','qa-review'],['McpServers','qa-local'],['CliCommands','git'],['CliApproval','git']];
  await tab(page,'groups');await page.locator('[onclick="_govToggleGroupForm()"]').click();await page.locator(`[onclick="_govApplyGroupTemplate('${template}')"]`).click();
  await expect(page.locator('#govGroupName')).toHaveValue(template);await expect(page.locator('#govGroupDesc')).not.toHaveValue('');
  const name=unique(template),description='QA template '+template;await page.locator('#govGroupName').fill(name);await page.locator('#govGroupDesc').fill(description);await replaceSelection(page,'govGroupRoles','member');await replaceSelection(page,'govGroupSso',name+'@example.test');
  for(const [field,value] of fields)await replaceSelection(page,'govGroup'+field+'Input',value);
  await page.locator('[onclick="_govSaveGroup()"]').click();const card=page.locator('.gov-group-card').filter({has:page.locator('.gov-group-card-name',{hasText:name})});await expect(card).toBeVisible();
  const created=(await api(page,'/api/governance/groups')).body.groups[name];expect(created).toMatchObject({description,roles:['member'],sso_groups:[name+'@example.test'],grants:{skills:{view:['qa-review'],load:['qa-review'],manage:['qa-review']},mcp:{servers:['qa-local']},cli:{commands:['git'],approval_commands:['git']}}});
  await page.reload();await page.locator('.rail-btn[data-panel="governance"]').click();await page.locator('[data-gov-tab="groups"]').click();await card.getByRole('button',{name:'Edit',exact:true}).click();await expect(page.locator('#govGroupName')).toBeDisabled();await expect(page.locator('#govGroupDesc')).toHaveValue(description);
  await page.locator('#govGroupDesc').fill('UNSAVED');await page.locator('[onclick="_govResetGroupForm()"]').click();expect((await api(page,'/api/governance/groups')).body.groups[name].description).toBe(description);
  await card.getByRole('button',{name:'Edit',exact:true}).click();await page.locator('#govGroupDesc').fill('Edited '+description);for(const [field] of fields){const remove=picker(page,'govGroup'+field+'Input').locator('.sp-chip-x');await expect(remove).toHaveCount(1);await remove.click();}await page.locator('[onclick="_govSaveGroup()"]').click();await expect(card).toContainText('Edited '+description);expect((await api(page,'/api/governance/groups')).body.groups[name]).not.toHaveProperty('grants');
  await denied(browser,[['/api/governance/groups'],['/api/governance/groups/update',{name,entry:{description:'FORBIDDEN'}}],['/api/governance/groups/delete',{name}]]);expect((await api(page,'/api/governance/groups')).body.groups[name].description).toBe('Edited '+description);
  await card.getByRole('button',{name:'Delete',exact:true}).click();await expect(card).toHaveCount(0);expect((await api(page,'/api/governance/groups')).body.groups).not.toHaveProperty(name);
 await capture(page,'groups-after-lifecycle',info);
});

test('US-SP-GOV-ADMIN-USERS role and group assignment persists cancel preserves policy and assigned group cannot be deleted',async({page,browser})=>{
 await open(page);const group=unique('membership'),email=unique('member')+'@example.test';expect((await write(page,'/api/governance/groups',{name:group,entry:{roles:['member']}})).status).toBe(200);
 await tab(page,'users');await selectPicker(page,'govUserEmail',email);await selectPicker(page,'govUserRolesSelInput','member');await selectPicker(page,'govUserGroupsSelInput',group);await page.locator('#govUserSave').click();await expect(page.locator('#govUserEmail')).toHaveValue('');
 const initial=(await api(page,'/api/governance/users')).body.users[email];expect(initial).toMatchObject({roles:['member'],groups:[group],access_level:'user',access_mode:'whitelist'});
 await page.reload();await tab(page,'users');const row=page.locator('#govPaneUsers tr').filter({hasText:email});await row.getByRole('button',{name:'Edit',exact:true}).click();await expect(picker(page,'govUserGroupsSelInput').locator('.sp-chip-label')).toHaveText(group);await page.locator('#govUserAccessLevel').selectOption('elevated');await page.locator('#govUserAccessMode').selectOption('blacklist');await page.locator('[onclick="_govResetUserForm()"]').click();expect((await api(page,'/api/governance/users')).body.users[email]).toEqual(initial);
 await page.locator('[data-gov-tab="groups"]').click();const card=page.locator('.gov-group-card').filter({hasText:group});await card.getByRole('button',{name:'Delete',exact:true}).click();await expect(page.locator('#toast')).toContainText('reassign them first');expect((await api(page,'/api/governance/groups')).body.groups).toHaveProperty(group);
 await denied(browser,[['/api/governance/users'],['/api/governance/users/update',{email,entry:{access_level:'admin'}}],['/api/governance/users/delete',{email}]]);expect((await api(page,'/api/governance/users')).body.users[email]).toEqual(initial);
 await page.locator('[data-gov-tab="users"]').click();await row.getByRole('button',{name:'Delete',exact:true}).click();await expect(row).toHaveCount(0);expect((await api(page,'/api/governance/users')).body.users).not.toHaveProperty(email);
 await page.locator('[data-gov-tab="groups"]').click();await card.getByRole('button',{name:'Delete',exact:true}).click();await expect(card).toHaveCount(0);
});

test('US-SP-GOV-ADMIN-APPROVAL actual denied route becomes admin approval then grants one route while rejection and replay remain bounded',async({page,browser},info)=>{
 await open(page);const email='govrequest@example.test';const entry={roles:['member'],access_level:'user',access_mode:'whitelist',grants:{permissions:['*'],profiles:['*'],routes:[],skills:{view:['*'],load:['*']}}};
 expect((await write(page,'/api/governance/users/update',{email,entry})).status).toBe(200);
 const requester=await browser.newContext({baseURL:auth.base_url});await requester.addCookies([{name:auth.cookie_name,value:auth.cookies.govrequest,url:auth.base_url}]);
 try {
  for(const [route,decision] of [['/api/skills','approve'],['/api/skills/usage','reject']] as const){
   const response=await requester.request.get(route);expect(response.status()).toBe(403);expect((await response.json()).reason).toBe('route_not_allowed');
   await tab(page,'approvals');const key=email+'|route|'+route;const button=page.locator(`[data-gov-approval="${decision}"]`).and(page.locator('[data-key='+JSON.stringify(key)+']'));await expect(button).toBeVisible();
   await denied(browser,[['/api/governance/approvals/decide',{kind:'grant',key,decision}]]);
   const requestRow=button.locator('xpath=ancestor::tr');const explanation=requestRow.locator('details.gov-explain').filter({has:page.locator('summary .gov-explain-toggle',{hasText:'What this grants'})});await explanation.locator('summary').click();await expect(explanation.locator('.gov-explain-body')).toBeVisible();await capture(page,'pending-'+decision,info);
   const decided=page.waitForResponse(r=>r.url().endsWith('/api/governance/approvals/decide')&&r.request().method()==='POST');await button.click();expect((await decided).status()).toBe(200);await expect(button).toHaveCount(0);
   const result=await requester.request.get(route);expect(result.status()).toBe(decision==='approve'?200:403);
   const user=(await api(page,'/api/governance/users')).body.users[email];if(decision==='approve')expect(user.grants.routes).toContain(route);else expect(user.grants.routes).not.toContain(route);
   expect((await api(page,'/api/governance/approvals/decide',{kind:'grant',key,decision})).status).toBe(409);
   const file=path.join(path.dirname(path.resolve(process.env.QA_SESSIONS!)),'home','dashboard-governance-audit.jsonl');const events=fs.readFileSync(file,'utf8').trim().split('\n').map(x=>JSON.parse(x));expect(events.some(e=>e.event==='approval_decision'&&e.extra?.key==='grant:'+key&&e.extra?.op==='approvals.'+decision)).toBe(true);
   await page.reload();await tab(page,'approvals');await expect(page.locator('[data-gov-approval][data-key='+JSON.stringify(key)+']')).toHaveCount(0);
  }
 }finally{await requester.close();}
});

test('US-SP-GOV-ADMIN-LEGACY unchanged group save preserves command denials metadata and unexposed grants',async({page})=>{
 await open(page);const name=unique('legacy-group');const entry={roles:['member'],description:'Legacy group metadata',grants:{cli:{commands:[{id:'git',argv0:'git'}],denied_commands:['rm'],workdir_roots:[auth.workspace]},mcp:{servers:['qa-local'],tools:{'qa-local':['qa_list']}},files:{read_roots:[auth.workspace],denied_globs:['**/private/**']},usage_caps:{daily_tool_calls:7}}};
 expect((await write(page,'/api/governance/groups',{name,entry})).status).toBe(200);await tab(page,'groups');await page.locator('.gov-group-card').filter({hasText:name}).getByRole('button',{name:'Edit',exact:true}).click();
 const save=page.waitForResponse(r=>r.url().endsWith('/api/governance/groups/update')&&r.request().method()==='POST');await page.locator('[onclick="_govSaveGroup()"]').click();expect((await save).status()).toBe(200);await page.reload();
 const stored=(await api(page,'/api/governance/groups')).body.groups[name];expect(stored.grants).toEqual(entry.grants);
});
