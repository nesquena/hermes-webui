import {test,expect,open,api,auth,capture,selectPicker,picker} from './fixtures';
import type {Page,Browser,BrowserContext} from '@playwright/test';
const email='bob@example.test';
async function read(page:Page){const r=await api(page,'/api/governance/users');expect(r.status).toBe(200);return r.body.users[email];}
async function restore(page:Page,entry:any){const state=await api(page,'/api/governance/users');const r=await page.evaluate(async({entry,etag,email})=>{const response=await fetch('/api/governance/users/update',{method:'POST',headers:{'Content-Type':'application/json','If-Match':etag},body:JSON.stringify({email,entry})});return response.status;},{entry,etag:state.body.etag,email});expect(r,'restore exact original fixture policy').toBe(200);}
async function edit(page:Page){await open(page,'governance');await page.locator('[data-gov-tab="users"]').click();await page.locator('#govPaneUsers tr').filter({hasText:email}).getByRole('button',{name:'Edit',exact:true}).click();await expect(page.locator('#govUserEmail')).toHaveValue(email);}
async function chip(page:Page,id:string,value:string){await selectPicker(page,id+'Input',value);}
async function save(page:Page){const pending=page.waitForResponse(r=>r.url().endsWith('/api/governance/users/update')&&r.request().method()==='POST');await page.locator('#govUserSave').click();expect((await pending).status()).toBe(200);await expect(page.locator('#govUserEmail')).toHaveValue('');}
async function actor(browser:Browser,user:'bob'|'outsider'='bob'){
 const context=await browser.newContext({baseURL:auth.base_url});
 await context.route('**/*',r=>['127.0.0.1','localhost','[::1]'].includes(new URL(r.request().url()).hostname)?r.continue():r.abort('blockedbyclient'));
 await context.addCookies([{name:auth.cookie_name,value:auth.cookies[user],url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);
 const page=await context.newPage();const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));await page.goto('/');await expect(page.locator('#msg')).toBeVisible();return {context,page,errors};
}
async function skillStatus(page:Page,status:number){const response=await api(page,'/api/skills/content?name=qa-review');expect(response.status).toBe(status);return response;}
async function policyAudit(page:Page){const r=await api(page,'/api/governance/audit?limit=100');expect(r.status).toBe(200);const event=r.body.events.findLast((e:any)=>e.event==='policy_change'&&e.extra?.op==='user_update'&&e.extra?.target===email);expect(event).toBeDefined();expect(event.extra.old_etag).not.toBe(event.extra.new_etag);return event;}
async function cleanup(page:Page,original:any,member:{context:BrowserContext,errors:string[]}){await restore(page,original);await member.context.close();expect(member.errors,'retained member browser errors').toEqual([]);}

test('US-SP-POL-REVOKE-WHITELIST direct skill grant permits actual browser read then removal denies retained-session requests',async({page,browser},info)=>{
 await open(page);const original=await read(page);const member=await actor(browser);
 try{
  await edit(page);await page.locator('#govUserAccessLevel').selectOption('user');await page.locator('#govUserAccessMode').selectOption('whitelist');await save(page);
  expect((await api(member.page,'/api/skills')).status).toBe(403);await skillStatus(member.page,403);
  await edit(page);await page.locator('details.gov-access-details').nth(0).locator('summary').click();
  for(const [field,value] of [['Permissions','skills:read'],['Routes','*'],['Profiles','*']])await chip(page,'govUserGrant'+field,value);
  await chip(page,'govUserSkillsView','qa-review');await save(page);
  expect((await read(page)).grants.skills.view).toEqual(['qa-review']);await skillStatus(member.page,200);expect((await api(member.page,'/api/sessions')).status,'neighboring capability remains unlisted').toBe(403);
  await member.page.reload();await member.page.locator('.rail-btn[data-panel="skills"]').click();const item=member.page.locator('.skill-item').filter({has:member.page.locator('.skill-name',{hasText:'qa-review'})});await expect(item).toBeVisible();await item.click();await expect(member.page.locator('#skillDetailBody')).toContainText('Summarize only the synthetic evidence supplied.');
  await edit(page);await picker(page,'govUserSkillsViewInput').locator('.sp-chip-x').click();await save(page);await page.reload();expect((await read(page)).grants.skills?.view||[]).toEqual([]);
  const refused=member.page.waitForResponse(r=>new URL(r.url()).pathname==='/api/skills/content'&&r.status()===403);await item.click();expect((await refused).status()).toBe(403);await skillStatus(member.page,403);
  const catalog=await api(member.page,'/api/skills');expect(catalog.status).toBe(200);expect(catalog.body.skills.some((s:any)=>s.name==='qa-review')).toBe(false);
  await policyAudit(page);await capture(page,'whitelist-revoked',info);
 }finally{await cleanup(page,original,member);}
});

test('US-SP-POL-REVOKE-BLACKLIST group wildcard cannot override direct denial and clearing denial restores the same signed session',async({page,browser},info)=>{
 await open(page);const original=await read(page);const member=await actor(browser);
 try{
  await edit(page);await page.locator('#govUserAccessLevel').selectOption('user');await page.locator('#govUserAccessMode').selectOption('blacklist');await expect(picker(page,'govUserGroupsSelInput')).toContainText('qa-team');await chip(page,'govUserSkillsView','qa-review');await chip(page,'govUserDenySkills','qa-review');await save(page);
  expect((await read(page)).deny.skills).toMatchObject({view:['qa-review'],load:['qa-review']});
  await page.locator('[data-gov-tab="preview"]').click();await page.locator('#govPreviewEmail').fill(email);await page.locator('[onclick="_govRunPreview()"]').click();await expect(page.locator('#govPreviewResult')).toContainText('skills / view: qa-review');await expect(page.locator('#govPreviewResult')).toContainText('qa-team');
  await skillStatus(member.page,403);const catalog=await api(member.page,'/api/skills');expect(catalog.status).toBe(200);expect(catalog.body.skills.some((s:any)=>s.name==='qa-review')).toBe(false);
  expect((await api(member.page,'/api/sessions')).status).toBe(200);
  await edit(page);await picker(page,'govUserDenySkillsInput').locator('.sp-chip-x').click();await save(page);await page.reload();expect((await read(page)).deny?.skills?.view||[]).toEqual([]);
  await skillStatus(member.page,200);await member.page.locator('.rail-btn[data-panel="skills"]').click();await member.page.locator('.skill-item').filter({has:member.page.locator('.skill-name',{hasText:'qa-review'})}).click();await expect(member.page.locator('#skillDetailBody')).toContainText('Summarize only the synthetic evidence supplied.');
  expect((await api(member.page,'/api/governance/users')).status).toBe(403);await policyAudit(page);await capture(page,'blacklist-denial-cleared',info);
 }finally{await cleanup(page,original,member);}
});

test('US-SP-POL-REVOKE-LEVELS user elevated and admin choices persist while only explicit admin can administer policy',async({page,browser},info)=>{
 await open(page);const original=await read(page);const member=await actor(browser);const currentMain=(await api(page,'/api/model/auxiliary')).body.main;
 try{
  for(const level of ['user','elevated','admin','user']){
   await edit(page);await page.locator('#govUserAccessMode').selectOption('blacklist');await page.locator('#govUserAccessLevel').selectOption(level);await save(page);await page.reload();expect((await read(page)).access_level).toBe(level);
   expect((await api(member.page,'/api/governance/users')).status).toBe(level==='admin'?200:403);
   const model=await api(member.page,'/api/model/set',{scope:'main',provider:currentMain.provider,model:currentMain.model,advanced:{base_url:currentMain.base_url,extra_body:currentMain.extra_body||{}}});expect(model.status,level+' model configuration ceiling').toBe(level==='user'?403:200);
   if(level!=='admin'){
    const denied=await api(member.page,'/api/governance/users/update',{email,entry:{roles:['member'],access_level:'admin',access_mode:'blacklist'}});expect(denied.status).toBe(403);expect((await read(page)).access_level).toBe(level);
   }
  }
  await policyAudit(page);await capture(page,'admin-level-revoked',info);
 }finally{await cleanup(page,original,member);}
});

test('US-SP-GOV-REQUESTER-STATUS retained requester sees pending approved rejected while another account cannot read or decide those requests',async({page,browser},info)=>{
 await open(page);const original=await read(page);const member=await actor(browser);const outsider=await actor(browser,'outsider');
 const permitted=['/api/auth/me','/api/governance/me','/api/governance/effective-access','/api/governance/approvals/mine','/api/settings','/api/model/auxiliary','/api/models','/api/profiles','/api/sessions','/api/sessions/events','/api/workspaces'];
 try{
  await edit(page);await page.locator('#govUserAccessLevel').selectOption('elevated');await page.locator('#govUserAccessMode').selectOption('blacklist');
  // A direct route deny cannot be approved; instead constrain the direct
  // whitelist to the listed shell routes and explicitly grant skill scope.
  await page.locator('#govUserAccessMode').selectOption('whitelist');await page.locator('details.gov-access-details').nth(0).locator('summary').click();
  await chip(page,'govUserGrantPermissions','*');await chip(page,'govUserGrantProfiles','*');await chip(page,'govUserGrantSettingsRead','*');for(const route of permitted)await chip(page,'govUserGrantRoutes',route);await chip(page,'govUserSkillsView','qa-review');await save(page);
  for(const [route,decision,status] of [['/api/skills','approve','approved'],['/api/skills/usage','reject','rejected']] as const){
   const result=await api(member.page,route);expect(result.status).toBe(403);expect(result.body.reason).toBe('route_not_allowed');
   const key=email+'|route|'+route;
   const own=(await api(member.page,'/api/governance/approvals/mine')).body;expect(own.owner_email).toBe(email);expect(own.requests.find((r:any)=>r.key===key).status).toBe('pending');
   await member.page.locator('.rail-btn[data-panel="chat"]').click();await member.page.locator('.rail-btn[data-panel="settings"]').click();await member.page.locator('[data-settings-section="access"]').click();await member.page.locator('#btnAccessRequestsRefresh').click();
   const row=member.page.locator('#accessRequestsList .access-request-row').filter({has:member.page.getByText('API route: '+route,{exact:true})});await expect(row).toBeVisible();await expect(row.locator('..').locator('.gov-pill')).toHaveText('Pending');await expect(row.locator('.access-request-next')).toHaveText('Waiting for an admin decision.');
   expect((await api(member.page,'/api/governance/approvals/decide',{kind:'grant',key,decision:'approve'})).status).toBe(403);
   const other=(await api(outsider.page,'/api/governance/approvals/mine?owner_email='+encodeURIComponent(email))).body;expect(other.owner_email).toBe('outsider@example.test');expect(other.requests.some((r:any)=>r.key===key)).toBe(false);
   expect((await api(outsider.page,'/api/governance/approvals/decide',{kind:'grant',key,decision})).status).toBe(403);
   await open(page,'governance');await page.locator('[data-gov-tab="approvals"]').click();const button=page.locator(`[data-gov-approval="${decision}"][data-key=${JSON.stringify(key)}]`);await expect(button).toBeVisible();const decided=page.waitForResponse(r=>r.url().endsWith('/api/governance/approvals/decide')&&r.request().method()==='POST');await button.click();expect((await decided).status()).toBe(200);const audit=(await api(page,'/api/governance/audit?limit=100')).body.events;expect(audit.some((e:any)=>e.event==='approval_decision'&&e.extra?.key==='grant:'+key&&e.extra?.op==='approvals.'+decision)).toBe(true);
   await member.page.locator('#btnAccessRequestsRefresh').click();await expect(row.locator('..').locator('.gov-pill')).toHaveText(status==='approved'?'Approved':'Rejected');await expect(row.locator('.access-request-next')).toHaveText(status==='approved'?'Granted: try the action again.':'Not granted. Ask an admin if you need this for your work.');expect((await api(member.page,'/api/governance/approvals/mine')).body.requests.find((r:any)=>r.key===key).status).toBe(status);
   expect((await api(member.page,route)).status).toBe(status==='approved'?200:403);
   await member.page.reload();await member.page.locator('.rail-btn[data-panel="chat"]').click();await member.page.locator('.rail-btn[data-panel="settings"]').click();await member.page.locator('[data-settings-section="access"]').click();await expect(row.locator('..').locator('.gov-pill')).toHaveText(status==='approved'?'Approved':'Rejected');
  }
  await capture(member.page,'requester-decided-statuses',info);
 }finally{await outsider.context.close();expect(outsider.errors).toEqual([]);await cleanup(page,original,member);}
});
