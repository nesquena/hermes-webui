import { test as base, expect, Page, BrowserContext } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
const file = path.resolve(process.env.QA_SESSIONS || '../e2e-state/browser-sessions.json');
export const auth = JSON.parse(fs.readFileSync(file, 'utf8'));
export type User = 'clionce' | 'clideny' | 'cliauto' | 'grantapprove' | 'grantdeny' | 'grantmanual' | 'grantharddeny' | 'climcp' | 'continuation' | 'govrequest' | 'resource' | 'admin' | 'alice' | 'bob' | 'outsider' | 'denied' | 'voicedenied' | 'autoapprove' | 'autodeny' | 'automanual' | 'manualapprove' | 'manualdeny';
export const test = base.extend<{ user: User; errors: string[] }>({
  user: ['admin', { option: true }],
  errors: [async ({ page }, use, info) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await use(errors);
    await info.attach('uncaught-browser-errors', {body:JSON.stringify(errors),contentType:'application/json'});
    expect(errors, 'uncaught browser errors').toEqual([]);
  }, {auto:true}],
  context: async ({ context, user }, use, info) => {
    expect(auth.base_url, 'must target the seeded fixture').toBe(process.env.QA_BASE_URL || 'http://127.0.0.1:19086');
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      return ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
        ? route.continue() : route.abort('blockedbyclient');
    });
    await context.routeWebSocket('**/*', socket => {
      const host = new URL(socket.url()).hostname;
      if (['127.0.0.1', 'localhost', '[::1]'].includes(host)) socket.connectToServer();
      else socket.close({code:1008,reason:'QA requires loopback networking'});
    });
    await context.addCookies([{ name: auth.cookie_name, value: auth.cookies[user], url: auth.base_url, httpOnly: true, sameSite: 'Lax' }]);
    const actions: any[] = [];
    await context.exposeBinding('__qaRecordAction', (_source, action) => { actions.push(action); });
    await context.addInitScript(() => {
      try {
        localStorage.setItem('hermes-lang', 'en');
        localStorage.setItem('hermes-webui-rail-expanded', '1');
      } catch (error) {
        // The application intentionally uses opaque sandboxed preview frames.
        // Only fixture preference writes are inapplicable in those frames.
        if (!(error instanceof DOMException) || error.name !== 'SecurityError') throw error;
      }
      const w = window as any;
      w.__qaActions = [];
      for (const event of ['click', 'dblclick', 'contextmenu', 'change', 'input', 'keydown']) document.addEventListener(event, (ev) => {
        const e = ev.composedPath().find((node:any) => node instanceof Element && (node.matches('button,input,select,textarea,a,[onclick],[role="button"]') || typeof node.onclick==='function' || typeof node.oncontextmenu==='function')) as Element | undefined;
        if (e) {
          const action={event, tag:e.tagName, id:e.id, text:(e.getAttribute('aria-label') || e.getAttribute('title') || e.textContent || '').trim().slice(0,90), onclick:e.getAttribute('onclick'), panel:e.getAttribute('data-panel'), scope:document.querySelector('.panel-view.active')?.id||'app', selection:e.getAttribute('data-selection'), value:e.getAttribute('type')==='checkbox'?e.getAttribute('value'):null};
          w.__qaActions.push(action);
          w.__qaRecordAction(action).catch(() => {});
        }
      }, true);
    });
    await use(context);
    await info.attach('all-navigation-controls',{body:JSON.stringify({actions,controls:[]}),contentType:'application/json'});
  },
});
export { expect };
export function picker(page: Page, inputId: string) {
  return page.locator('.sp-picker').filter({has:page.locator('#' + inputId)});
}
export async function selectPicker(page: Page, inputId: string, value: string) {
  const root = picker(page, inputId);
  await root.getByRole('combobox').click();
  const menu = page.locator('.sp-picker-menu[role="listbox"]');
  await menu.getByRole('textbox', {name:'Search', exact:true}).fill(value);
  // Custom values are available only where the rendered control allows them.
  // Match IDs as well as display names, including normalized email addresses.
  await menu.locator('.sp-picker-option[data-value=' + JSON.stringify(value) + ' i]').click();
  if (await menu.isVisible()) await page.keyboard.press('Escape');
  await expect(root.locator('.sp-chip[data-value=' + JSON.stringify(value) + ' i]')).toHaveCount(1);
}
export async function open(page: Page, panel = 'chat') {
  await page.goto('/');
  await expect(page.locator('#msg')).toBeVisible();
  if (panel !== 'chat') await page.locator(`.rail-btn[data-panel="${panel}"]`).click();
}
export async function api(page: Page, url: string, data?: any) {
  return page.evaluate(async ({url,data}) => {
    const r = await fetch(url, data === undefined ? {} : { method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    return { status:r.status, body:await r.json() };
  }, {url,data});
}
export async function session(page: Page) {
  await page.locator('#btnNewChat').click();
  await expect.poll(() => page.url()).toContain('/session/');
  return page.url().split('/session/')[1].split(/[?#]/)[0];
}
export async function capture(page: Page, label: string, testInfo: any) {
  const snapshot = await page.evaluate(() => ({
    actions: (window as any).__qaActions || [],
    controls: [...document.querySelectorAll('*')]
      .filter((e: any) => e.matches('button,input,textarea,select,a[href],[onclick],[role="button"]') || typeof e.onclick==='function' || typeof e.oncontextmenu==='function')
      .filter((e: any) => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
      .map((e: any) => ({tag:e.tagName,id:e.id,text:(e.getAttribute('aria-label')||e.getAttribute('title')||e.textContent||'').trim().slice(0,90),onclick:e.getAttribute('onclick'),panel:e.getAttribute('data-panel'),scope:document.querySelector('.panel-view.active')?.id||'app',selection:e.getAttribute('data-selection'),value:e.getAttribute('type')==='checkbox'?e.getAttribute('value'):null})),
  }));
  await testInfo.attach(label+'-controls',{body:JSON.stringify(snapshot,null,2),contentType:'application/json'});
}
test.afterEach(async ({page,errors}, info) => { await capture(page, 'interaction-evidence', info); });
