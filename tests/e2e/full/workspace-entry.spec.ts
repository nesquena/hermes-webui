import {test, expect, api} from './fixtures';

for (const user of ['admin', 'alice'] as const) {
  test.describe(user, () => {
    test.use({user});
    test('HOME ENTRY opens the dashboard for a returning user in the browser and installed app', async ({page}, info) => {
      await page.goto('/?view=chat');
      await page.waitForFunction(() => window.__GOV_ME__?.email && S._bootReady);
      const made = await api(page, '/api/session/new', {profile:'default', chat_mode:'normal'});
      expect(made.status).toBe(200);
      const sid = made.body.session.session_id;
      const draft = 'Continue this draft after visiting the dashboard';
      expect((await api(page, '/api/session/draft', {session_id:sid, text:draft, files:[]})).status).toBe(200);
      await page.evaluate(id => {
        localStorage.setItem('hermes-webui-session', id);
        localStorage.setItem('synthpulse-start:' + window.__GOV_ME__.email, 'chat');
      }, sid);
      for (const path of ['/?source=pwa', '/', '/index.html?utm_source=bookmark']) {
        await page.goto(path);
        await page.waitForFunction(() => S._bootReady && Array.isArray(window.__GOV_ME__?.hidden_nav));
        if (path === '/?source=pwa') await page.screenshot({path:info.outputPath('launch-'+user+'.png')});
        await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
        expect(new URL(page.url()).pathname).toBe(new URL(path, page.url()).pathname);
      }
      await page.goto('/session/' + sid);
      await expect(page.locator('#msg')).toHaveValue(draft);
      await page.locator('#workspaceHome').click();
      await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
      expect(new URL(page.url()).pathname).toBe('/');
      await page.goBack();
      await expect(page.locator('#msg')).toHaveValue(draft);
      await page.goForward();
      await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
      await page.reload();
      await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
    });
  });
}

test('HOME ENTRY shows Overview while permissions load and respects deliberate navigation', async ({page}) => {
  let release!: () => void;
  const ready = new Promise<void>(done => { release = done; });
  await page.route('**/api/governance/me', async route => { await ready; await route.continue(); });
  try {
    await page.goto('/', {waitUntil:'domcontentloaded'});
    await expect(page.locator('#mainStudio')).toBeVisible();
    await expect(page.locator('#mainChat')).toBeHidden();
    await page.keyboard.press('Tab');
  } finally { release(); }
  await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
  await page.goto('/?view=chat');
  await page.waitForFunction(() => S._bootReady && window.__GOV_ME__);
  await expect(page.locator('#msg')).toBeVisible();
  await page.goto('/?action=new-chat&source=pwa');
  await expect(page).toHaveURL(/\/session\//);
  await expect(page.locator('#msg')).toBeVisible();
});

test('CONVERSATIONS ENTRY navigation opens the searchable board on desktop and mobile', async ({page}, info) => {
  const board = page.locator('#mainConversations');
  await page.goto('/');
  await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
  const offer=page.locator('#spHelpCard[data-kind="tour_offer"] .sp-help-btn').first();
  await page.addLocatorHandler(offer, () => offer.click());
  await page.locator('.rail [data-panel="chat"]').click();
  await page.screenshot({path:info.outputPath('conversations-entry.png')});
  await expect(board.getByRole('heading', {name:'Conversation overview'})).toBeVisible();
  await expect(page.locator('#conversationBoardSearch')).toBeVisible();
  await page.reload();
  await expect(page.locator('#conversationBoardSearch')).toBeVisible();
  await page.locator('#conversationBoardNew').click();
  await expect(page.locator('#msg')).toBeVisible();
  await expect(page).toHaveURL(/\/session\//);
  await page.goBack();
  await expect(page.locator('#conversationBoardSearch')).toBeVisible();
  await page.goForward();
  await expect(page.locator('#msg')).toBeVisible();
  await page.locator('#workspaceHome').click();
  await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
  await page.locator('.wp-destinations summary').click();
  await page.locator('[data-wp-destination="conversations"]').click();
  await expect(board).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  await page.locator('#workspaceHome').click();
  await expect(page.locator('#mainStudio .wp-hero')).toBeVisible();
  await page.locator('#btnHamburger').click();
  await page.locator('.sidebar-nav [data-panel="chat"]').click();
  await expect(page.locator('#conversationBoardSearch')).toBeInViewport();
  await expect(page.locator('.sidebar')).not.toHaveClass(/mobile-open/);
});
