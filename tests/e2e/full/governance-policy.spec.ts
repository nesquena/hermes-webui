import { test, expect, open, api, capture, auth, picker, selectPicker } from './fixtures';
import fs from 'node:fs';
async function users(page:any) {await open(page,'governance');await page.locator('[data-gov-tab="users"]').click();await expect(picker(page,'govUserEmail').getByRole('combobox')).toBeVisible();}
async function chip(page:any,id:string,value:string){await selectPicker(page,id+'Input',value);}
test('US-SP-POL-001 new policy defaults to user whitelist manual and renders at desktop and mobile',async({page},info)=>{
 await users(page);await expect(page.locator('#govUserAccessLevel')).toHaveValue('user');await expect(page.locator('#govUserAccessMode')).toHaveValue('whitelist');await expect(page.locator('#govUserApprovalMode')).toHaveValue('manual');await expect(page.locator('#govUserApprovalPrompt')).toBeHidden();
 await page.locator('[data-testid="user-governance-policy"]').scrollIntoViewIfNeeded();await page.screenshot({path:info.outputPath('governance-defaults-desktop.png')});await page.setViewportSize({width:390,height:844});await page.locator('[data-testid="user-governance-policy"]').scrollIntoViewIfNeeded();await expect(page.locator('#govUserAccessMode')).toBeVisible();await page.screenshot({path:info.outputPath('governance-defaults-mobile.png')});await capture(page,'mobile-policy',info);
});
for (const [label,width,height] of [['desktop',1440,1000],['laptop',1024,768],['mobile',390,844]] as const) {
 test('US-SP-POL-RESPONSIVE '+label+' edits policy prompt and saves accessible last controls after reload',async({page},info)=>{
  await users(page);await page.setViewportSize({width,height});
  const email='qa-responsive-'+label+'-'+Date.now()+'@example.test';
  const prompt='Allow reading synthetic QA documents. Deny deleting files and sending messages.';
  await selectPicker(page,'govUserEmail',email);await chip(page,'govUserRolesSel','member');
  await page.locator('#govUserAccessLevel').selectOption('elevated');await page.locator('#govUserAccessMode').selectOption('blacklist');await page.locator('#govUserApprovalMode').selectOption('automatic');
  await page.locator('#govUserApprovalPrompt').fill(prompt);
  for(const id of ['govUserAccessLevel','govUserAccessMode','govUserApprovalMode','govUserApprovalPrompt','govUserSave']){
   const control=page.locator('#'+id);await control.scrollIntoViewIfNeeded();const bounds=await control.boundingBox();
   expect(bounds,id+' has measurable visible bounds').not.toBeNull();expect(bounds!.x,id+' starts within viewport').toBeGreaterThanOrEqual(0);expect(bounds!.x+bounds!.width,id+' ends within viewport').toBeLessThanOrEqual(width+1);
  }
  await page.screenshot({path:info.outputPath('governance-save-controls-'+label+'.png')});
  const saved=page.waitForResponse(r=>/\/api\/governance\/users(?:\/update)?$/.test(r.url())&&r.request().method()==='POST');await page.locator('#govUserSave').click();expect((await saved).status()).toBe(200);
  await page.reload();
  if(label==='mobile'){await page.locator('#btnHamburger').click();await page.locator('.sidebar [data-panel="governance"]').click();await expect(page.locator('.sidebar')).not.toHaveClass(/mobile-open/);}
  else await page.locator('.rail-btn[data-panel="governance"]').click();
  await page.locator('[data-gov-tab="users"]').click();await page.locator('#govPaneUsers tr').filter({hasText:email}).getByRole('button',{name:'Edit',exact:true}).click();
  await expect(page.locator('#govUserAccessMode')).toHaveValue('blacklist');await expect(page.locator('#govUserAccessLevel')).toHaveValue('elevated');await expect(page.locator('#govUserApprovalMode')).toHaveValue('automatic');await expect(page.locator('#govUserApprovalPrompt')).toHaveValue(prompt);
  await page.locator('[data-testid="user-governance-policy"]').scrollIntoViewIfNeeded();await page.screenshot({path:info.outputPath('governance-after-'+label+'.png')});await capture(page,'responsive-policy-'+label,info);
 });
}
test('US-SP-POL-VALIDATION automatic without policy prompt rejects before mutation',async({page})=>{
 await users(page);const email='qa-no-prompt-'+Date.now()+'@example.test';await selectPicker(page,'govUserEmail',email);await page.locator('#govUserApprovalMode').selectOption('automatic');await expect(page.locator('#govUserApprovalPrompt')).toBeVisible();
 await page.locator('[onclick="_govSaveUser()"]').click();await expect(page.getByText('Describe what this user may and may not do before enabling automatic approval.',{exact:true})).toBeVisible();
 expect((await api(page,'/api/governance/users')).body.users).not.toHaveProperty(email);
});
test('US-SP-POL-LEGACY editing a legacy user preserves omitted access level and mode',async({page})=>{
 await users(page);await page.locator('#govPaneUsers tr').filter({hasText:'alice@example.test'}).getByRole('button',{name:'Edit',exact:true}).click();await expect(page.locator('#govUserAccessMode')).toHaveValue('');await expect(page.locator('#govUserAccessLevel')).toHaveValue('');await page.locator('[onclick="_govSaveUser()"]').click();
 await expect.poll(async()=>{const r=await api(page,'/api/governance/users');return r.body.users['alice@example.test'].roles;}).toContain('bot_creator');const user=(await api(page,'/api/governance/users')).body.users['alice@example.test'];expect(user).not.toHaveProperty('access_mode');expect(user).not.toHaveProperty('access_level');
});
test('US-SP-POL-002 whitelist with role but zero direct grant cannot use work APIs',async({page,browser})=>{
 await users(page);await selectPicker(page,'govUserEmail','denied@example.test');await chip(page,'govUserRolesSel','member');await page.locator('[onclick="_govSaveUser()"]').click();await expect.poll(async()=>Object.keys((await api(page,'/api/governance/users')).body.users)).toContain('denied@example.test');
 const c=await browser.newContext({baseURL:auth.base_url});try{await c.addCookies([{name:auth.cookie_name,value:auth.cookies.denied,url:auth.base_url}]);for(const route of ['/api/sessions','/api/skills','/api/profiles']){const r=await c.request.get(route);expect(r.status(),route).toBe(403);}}finally{await c.close();}
});
test('US-SP-POL-004 blacklist elevated automatic policy and narrow denials survive save reload',async({page})=>{
 await users(page);const email='qa-blacklist-'+Date.now()+'@example.test';await selectPicker(page,'govUserEmail',email);await chip(page,'govUserRolesSel','member');await page.locator('#govUserAccessLevel').selectOption('elevated');await page.locator('#govUserAccessMode').selectOption('blacklist');await page.locator('#govUserApprovalMode').selectOption('automatic');await page.locator('#govUserApprovalPrompt').fill('Allow reading synthetic QA files. Deny deleting files or sending external messages.');
 await page.locator('details.gov-access-details').nth(1).locator('summary').click();await chip(page,'govUserDenyPermissions','cron:write');await chip(page,'govUserDenyTools','write_file');await page.locator('[onclick="_govSaveUser()"]').click();
 await expect.poll(async()=>Object.keys((await api(page,'/api/governance/users')).body.users)).toContain(email);await page.reload();await page.locator('.rail-btn[data-panel="governance"]').click();await page.locator('[data-gov-tab="users"]').click();await page.locator('#govPaneUsers tr').filter({hasText:email}).getByRole('button',{name:'Edit',exact:true}).click();
 await expect(page.locator('#govUserAccessLevel')).toHaveValue('elevated');await expect(page.locator('#govUserAccessMode')).toHaveValue('blacklist');await expect(page.locator('#govUserApprovalMode')).toHaveValue('automatic');await expect(page.locator('#govUserApprovalPrompt')).toHaveValue('Allow reading synthetic QA files. Deny deleting files or sending external messages.');const user=(await api(page,'/api/governance/users')).body.users[email];expect(user.deny.permissions).toContain('cron:write');expect(user.deny.tools.builtins).toContain('write_file');
});
test('US-SP-GOV-PREVIEW real preview button shows chosen user and effective grants',async({page})=>{
 await open(page,'governance');await page.locator('[data-gov-tab="preview"]').click();await page.locator('#govPreviewEmail').fill('alice@example.test');await page.locator('[onclick="_govRunPreview()"]').click();await expect(page.locator('#govPreviewResult')).toContainText('alice@example.test');await expect(page.locator('#govPreviewResult')).toContainText('member');
});
test('US-SP-AUTH-CSRF cross-origin cookie-auth mutation without CSRF is rejected',async({page,context})=>{
 await open(page);const r=await context.request.post('/api/session/new',{data:{},headers:{Origin:'https://attacker.example.test'}});expect(r.status()).toBe(403);expect(await r.text()).toMatch(/csrf|origin/i);
});
