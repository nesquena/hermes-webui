import {test,expect,open,api,auth} from './fixtures';
test.use({user:'autoapprove'});

test('BLACKLIST NAVIGATION effective technical panels remain visible without administrator identity and explicit denials hide them',async({page,browser},info)=>{
 const admin=await browser.newContext({baseURL:auth.base_url});await admin.route('**/*',r=>['127.0.0.1','localhost','[::1]'].includes(new URL(r.request().url()).hostname)?r.continue():r.abort());
 await admin.addCookies([{name:auth.cookie_name,value:auth.cookies.admin,url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);const control=await admin.newPage();await open(control);
 const email='autoapprove@example.test',name='qa-capability-nav-'+Date.now();let original:any,group=false;
 const write=async(url:string,data:any)=>{const state=(await api(control,'/api/governance/users')).body;return control.evaluate(async({url,data,etag})=>{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','If-Match':etag},body:JSON.stringify(data)});return{status:r.status,body:await r.json()};},{url,data,etag:state.etag});};
 try{
  original=(await api(control,'/api/governance/users')).body.users[email];
  expect((await write('/api/governance/groups',{name,entry:{grants:{permissions:['kanban:read','files:read','analytics:read','plugins:read','system:read','config:read']}}})).status).toBe(200);group=true;
  const entry={...original,groups:[...(original.groups||[]),name],access_level:'elevated',access_mode:'blacklist',deny:{permissions:['logs:*','plugins:*']}};
  expect((await write('/api/governance/users/update',{email,entry})).status).toBe(200);await open(page);
  const me=(await api(page,'/api/governance/me')).body;expect(me.access_mode).toBe('blacklist');expect(me.approval_mode).toBe('automatic');expect(me.nav_audience).toBe('member');expect(me.effective_access.is_admin).toBe(false);
  expect((await api(page,'/api/workspaces')).status).toBe(200);expect((await api(page,'/api/kanban/boards')).status).toBe(200);
  await expect(page.locator('html')).toHaveAttribute('data-workspace-kind','engineer');
  await expect(page.locator('html')).toHaveAttribute('data-advanced-chat-controls','1');
  await expect(page.locator('.rail [data-panel="workspaces"], .rail [data-panel="kanban"]')).toHaveCount(0);
  for(const panel of ['projects','insights'])await expect(page.locator('.rail-btn[data-panel="'+panel+'"]').first()).toBeVisible();
  for(const panel of ['logs','governance'])await expect(page.locator('.rail-btn[data-panel="'+panel+'"]').first()).toBeHidden();
  await page.locator('.rail-btn[data-panel="projects"]').click();await expect(page.locator('#mainProjects')).toBeVisible();
  await page.locator('.rail-btn[data-panel="settings"]').click();await expect(page.locator('#settingsMenu [data-settings-section="providers"]')).toBeVisible();await page.locator('#settingsMenu [data-settings-section="providers"]').click();await expect(page.locator('#settingsPaneProviders')).toHaveClass(/active/);
  await expect(page.locator('#settingsMenu [data-settings-section="extensions"]')).toBeHidden();expect((await api(page,'/api/extensions')).status).toBe(403);expect((await api(page,'/api/governance/users')).status).toBe(403);
  expect((await write('/api/governance/users/update',{email,entry:{...entry,deny:{permissions:['files:*','sessions:read','config:*','logs:*','plugins:*']}}})).status).toBe(200);await page.reload();await expect(page.locator('#msg')).toBeVisible();
  for(const panel of ['workspaces','files','kanban'])await expect(page.locator('.rail-btn[data-panel="'+panel+'"]').first()).toBeHidden();
  expect((await api(page,'/api/kanban/boards')).status).toBe(403);expect((await api(page,'/api/config')).status).toBe(403);
  await page.locator('.rail-btn[data-panel="settings"]').click();await expect(page.locator('#settingsMenu [data-settings-section="providers"]')).toBeHidden();await expect(page.locator('#settingsMenu [data-settings-section="appearance"]')).toBeVisible();
  await info.attach('blacklist-navigation-decisions',{body:JSON.stringify({automatic_configured:true,administrator:false,positive_panels:['projects','insights'],removed_standalone_panels:['workspaces','kanban'],denied_panels:['files','workspaces','kanban','logs','governance'],denied_settings:['providers','extensions']}),contentType:'application/json'});
 }finally{if(original)expect((await write('/api/governance/users/update',{email,entry:original})).status).toBe(200);if(group)expect((await write('/api/governance/groups/delete',{name})).status).toBe(200);await admin.close();}
});
