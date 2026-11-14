import {test, expect, open, api, auth, session} from './fixtures';

test.describe('role workspace', () => {
  test.use({user: 'alice', reducedMotion: 'reduce'});

  test('WORKSPACE SHELL member retains chat, bots, drafts and responsive navigation', async ({page}, info) => {
    await page.route('**/static/workspace-shell.css*', route => route.fulfill({contentType:'text/css', body:''}));
    await open(page);
    await page.screenshot({path:info.outputPath('before-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:info.outputPath('before-mobile.png')});
    await page.unroute('**/static/workspace-shell.css*');
    await page.reload();
    await expect(page.locator('#msg')).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute('data-workspace-kind','member');
    const identity=(await api(page,'/api/governance/me')).body;
    expect(identity.workspace_navigation.kind).toBe('member');
    expect((await api(page,'/api/governance/users')).status).toBe(403);
    await page.locator('#msg').fill('Draft stays in my workspace');
    await page.locator('#btnHamburger').click();
    await expect(page.locator('.sidebar-nav [data-panel="governance"]')).toBeHidden();
    await page.locator('.sidebar-nav [data-panel="profiles"]').click();
    await expect(page.locator('#profilesPanel')).toBeVisible();
    await page.locator('.sidebar-nav [data-panel="chat"]').click();
    await expect(page.locator('#msg')).toHaveValue('Draft stays in my workspace');
    for (const [name,width,height] of [['mobile',390,844],['laptop',1024,768],['desktop',1440,1000]] as const) {
      await page.setViewportSize({width,height});
      for (const selector of ['#msg','#btnSend','#btnRealtimeVoice']) {
        const box=await page.locator(selector).boundingBox();
        expect(box,selector).not.toBeNull();
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x+box!.width).toBeLessThanOrEqual(width+1);
        expect(box!.y+box!.height).toBeLessThanOrEqual(height+1);
      }
      await page.screenshot({path:info.outputPath('after-'+name+'.png')});
    }
    await page.evaluate(()=>{document.documentElement.classList.remove('dark');});
    await page.waitForTimeout(500);
    await page.screenshot({path:info.outputPath('after-light.png')});
    await page.evaluate(()=>{document.documentElement.classList.add('dark');});
    await page.waitForTimeout(500);
    await page.screenshot({path:info.outputPath('after-dark.png')});
    await session(page);
    await page.locator('#msg').fill('QA_WORKSPACE_PRESERVED_CHAT');
    await page.locator('#btnSend').click();
    await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_WORKSPACE_PRESERVED_CHAT',{timeout:30000});
    await page.reload();
    await expect(page.locator('#messages')).toContainText('QA_REPLY: QA_WORKSPACE_PRESERVED_CHAT');
    await page.screenshot({path:info.outputPath('after-conversation.png')});
  });

  test('WORKSPACE SHELL additional allowed views persist per user and never reveal denied panels', async ({page,browser}) => {
    const admin=await browser.newContext({baseURL:auth.base_url});
    await admin.addCookies([{name:auth.cookie_name,value:auth.cookies.admin,url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);
    const control=await admin.newPage();await open(control);
    const original=(await api(control,'/api/governance/users')).body.users['alice@example.test'];
    const write=async(entry:any)=>{
      const state=(await api(control,'/api/governance/users')).body;
      return control.evaluate(async({entry,etag})=>{
        const response=await fetch('/api/governance/users/update',{method:'POST',headers:{'Content-Type':'application/json','If-Match':etag},body:JSON.stringify({email:'alice@example.test',entry})});
        return response.status;
      },{entry,etag:state.etag});
    };
    try {
      expect(await write({...original,access_level:'user',access_mode:'blacklist',deny:{permissions:['governance:*','logs:*']}})).toBe(200);
      await open(page);
      await expect(page.locator('html')).toHaveAttribute('data-workspace-kind','member');
      await expect(page.locator('.rail [data-panel="insights"]')).toBeHidden();
      await page.locator('#btnWorkspaceNavigator').click();
      await page.locator('#workspaceNavigatorSearch').fill('insights');
      await expect(page.locator('[data-destination="insights"]')).toBeVisible();
      await expect(page.locator('[data-destination="governance"]')).toHaveCount(0);
      await page.keyboard.press('Escape');
      await page.locator('.rail .workspace-more').click();
      await expect(page.locator('.rail [data-panel="insights"]')).toBeVisible();
      await expect(page.locator('.rail [data-panel="governance"]')).toBeHidden();
      await page.reload();
      await expect(page.locator('.rail .workspace-more')).toHaveAttribute('aria-expanded','true');
      await expect(page.locator('.rail [data-panel="insights"]')).toBeVisible();
      await page.setViewportSize({width:390,height:844});
      await page.locator('#btnHamburger').click();
      await expect(page.locator('.sidebar-nav [data-panel="insights"]')).toBeVisible();
      await page.locator('.sidebar-nav .workspace-more').click();
      await expect(page.locator('.sidebar-nav [data-panel="insights"]')).toBeHidden();
      await page.locator('.sidebar-nav .workspace-more').click();
      await page.context().addCookies([{name:auth.cookie_name,value:auth.cookies.bob,url:auth.base_url,httpOnly:true,sameSite:'Lax'}]);
      await page.reload();
      expect(await page.evaluate(()=>localStorage.getItem('synthpulse-workspace-more:bob@example.test'))).toBeNull();
      await page.setViewportSize({width:1440,height:1000});
      await expect(page.locator('.rail [data-panel="insights"]')).toBeHidden();
    } finally { expect(await write(original)).toBe(200);await admin.close(); }
  });
});

test('WORKSPACE SHELL administrator keeps every existing destination and split view', async ({page},info) => {
  await open(page);
  await expect(page.locator('html')).toHaveAttribute('data-workspace-kind','admin');
  await expect(page.locator('.rail .workspace-more')).toBeHidden();
  for (const panel of ['profiles','projects','tasks','skills','memory','integrations','insights','governance','settings']) {
    await expect(page.locator('.rail [data-panel="'+panel+'"]')).toBeVisible();
  }
  await page.locator('#btnSplitView').click();
  await expect(page.locator('#splitGrid iframe').first()).toBeVisible();
  await page.screenshot({path:info.outputPath('after-admin-split.png')});
});
