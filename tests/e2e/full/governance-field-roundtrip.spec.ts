import { test, expect, open, api, auth, capture, picker, selectPicker } from './fixtures';
import type { Page } from '@playwright/test';

// API calls seed disposable users and verify storage. Every operation under
// test (edit, type, remove, select, save and reload) uses the rendered frontend.
const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
const userId = (purpose: string) => `qa-fields-${purpose}-${runId}@example.test`;
const fields: [string, string[], string][] = [
  ['Permissions', ['permissions'], 'chat:use'],
  ['Profiles', ['profiles'], 'default'],
  ['Workspaces', ['workspaces'], auth.workspace],
  ['Routes', ['routes'], '/api/sessions'],
  ['Tools', ['tools', 'builtins'], 'read_file'],
  ['Toolsets', ['tools', 'toolsets'], 'file'],
  ['Models', ['models', 'models'], 'qa-deterministic'],
  ['Providers', ['models', 'providers'], 'custom:qa'],
  ['SettingsRead', ['settings', 'read'], 'appearance'],
  ['SettingsWrite', ['settings', 'write'], 'appearance'],
  ['FilesRead', ['files', 'read_roots'], auth.workspace],
  ['FilesWrite', ['files', 'write_roots'], auth.workspace],
  ['Workdirs', ['cli', 'workdir_roots'], auth.workspace],
  ['Environment', ['env', 'vars'], 'QA_PUBLIC_NAME'],
];

async function users(page: Page) {
  await open(page, 'governance');
  await page.locator('[data-gov-tab="users"]').click();
  await expect(picker(page, 'govUserEmail').getByRole('combobox')).toBeVisible();
}
async function edit(page: Page, email: string) {
  await page.locator('#govPaneUsers tr').filter({ hasText: email })
    .getByRole('button', { name: 'Edit', exact: true }).click();
  await expect(page.locator('#govUserEmail')).toHaveValue(email);
}
async function stored(page: Page, email: string) {
  const r = await api(page, '/api/governance/users');
  expect(r.status).toBe(200);
  return r.body.users[email];
}
async function seed(page: Page, email: string, entry: any) {
  // Other independent suites may be editing their own fixture users, so retry
  // a real optimistic-concurrency conflict without resetting shared policy.
  for (let attempt = 0; attempt < 5; attempt++) {
    const current = await api(page, '/api/governance/users');
    expect(current.status).toBe(200);
    const result = await page.evaluate(async ({ email, entry, etag }) => {
      const r = await fetch('/api/governance/users', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'If-Match': etag },
        body: JSON.stringify({ email, entry }) });
      return { status: r.status, body: await r.json() };
    }, { email, entry, etag: current.body.etag });
    if (result.status === 412) continue;
    expect(result.status, JSON.stringify(result.body)).toBe(200);
    return;
  }
  throw new Error('Fixture user creation repeatedly conflicted with another policy writer');
}
async function save(page: Page) {
  const response = page.waitForResponse(r => /\/api\/governance\/users(?:\/update)?$/.test(r.url()) && r.request().method() === 'POST');
  await page.locator('#govUserSave').click();
  const result = await response;
  expect(result.status(), await result.text()).toBe(200);
  await expect(page.locator('#govUserEmail')).toHaveValue('');
}
async function createWithAllFields(page: Page, email: string) {
  await users(page);
  await selectPicker(page, 'govUserEmail', email);
  await selectPicker(page, 'govUserRolesSelInput', 'member');
  for (const [kind, index] of [['Grant', 0], ['Deny', 1]] as const) {
    await page.locator('details.gov-access-details').nth(index).locator('summary').click();
    for (const [name, , value] of fields) {
      await test.step(`Enter ${kind.toLowerCase()} ${name}`, async () => {
        await selectPicker(page, `govUser${kind}${name}Input`, value);
        await expect(picker(page, `govUser${kind}${name}Input`).locator('.sp-chip')).toHaveCount(1);
      });
    }
  }
  await selectPicker(page, 'govUserDenySkillsManageInput', 'qa-review');
  await save(page);
}

test('US-SP-GOV-006 [field roundtrip] all 14 extra allow and 14 deny controls persist through real save and reload', async ({ page }, info) => {
  const email = userId('roundtrip');
  await createWithAllFields(page, email);
  await page.reload();
  await users(page);
  await edit(page, email);
  const entry = await stored(page, email);
  expect(entry.access_mode).toBe('whitelist');
  expect(entry.access_level).toBe('user');
  expect(entry.approval.mode).toBe('manual');
  expect(entry.deny.skills.manage).toEqual(['qa-review']);
  await expect(picker(page, 'govUserDenySkillsManageInput').locator('.sp-chip')).toHaveCount(1);
  await expect(picker(page, 'govUserDenySkillsManageInput').locator('.sp-chip')).toContainText('qa-review');
  for (const [kind, section, index] of [['Grant', 'grants', 0], ['Deny', 'deny', 1]] as const) {
    await page.locator('details.gov-access-details').nth(index).locator('summary').click();
    for (const [name, path, value] of fields) {
      await test.step(`Verify persisted ${kind.toLowerCase()} ${name}`, async () => {
        expect(path.reduce((node: any, key) => node[key], entry[section])).toEqual([value]);
        await expect(picker(page, `govUser${kind}${name}Input`).locator('.sp-chip')).toHaveCount(1);
        await expect(picker(page, `govUser${kind}${name}Input`).locator('.sp-chip')).toContainText(value);
      });
    }
  }
  await capture(page, 'all-governance-fields-roundtrip', info);
});

test('US-SP-GOV-006 [field removal] clearing all extra grants and denials removes persisted entries', async ({ page }) => {
  const email = userId('clear');
  await createWithAllFields(page, email);
  await edit(page, email);
  for (const [kind, index] of [['Grant', 0], ['Deny', 1]] as const) {
    await page.locator('details.gov-access-details').nth(index).locator('summary').click();
    for (const [name] of fields) {
      await test.step(`Remove ${kind.toLowerCase()} ${name}`, async () => {
        await expect(picker(page, `govUser${kind}${name}Input`).locator('.sp-chip-x')).toHaveCount(1);
        await picker(page, `govUser${kind}${name}Input`).locator('.sp-chip-x').click();
        await expect(picker(page, `govUser${kind}${name}Input`).locator('.sp-chip')).toHaveCount(0);
      });
    }
  }
  await picker(page, 'govUserDenySkillsManageInput').locator('.sp-chip-x').click();
  await save(page);
  await page.reload();
  await users(page);
  await edit(page, email);
  const entry = await stored(page, email);
  expect(entry).not.toHaveProperty('grants');
  expect(entry).not.toHaveProperty('deny');
  await expect(picker(page, 'govUserDenySkillsManageInput').locator('.sp-chip')).toHaveCount(0);
  for (const [name] of fields) {
    await expect(picker(page, `govUserGrant${name}Input`).locator('.sp-chip')).toHaveCount(0);
    await expect(picker(page, `govUserDeny${name}Input`).locator('.sp-chip')).toHaveCount(0);
  }
});

test('US-SP-POL-032 [legacy subset] unchanged user save preserves unexposed command denials and policy metadata', async ({ page }) => {
  await open(page);
  const email = userId('legacy');
  const entry = { description: 'Keep synthetic legacy policy metadata', roles: ['member'], groups: [],
    grants: { cli: { commands: ['git'], denied_commands: ['rm'], workdir_roots: [auth.workspace] },
      mcp: { servers: ['qa-local'], tools: { 'qa-local': ['qa_list'] } },
      files: { read_roots: [auth.workspace], denied_globs: ['**/private/**'], allow_globs: ['**/*.txt'] },
      usage_caps: { daily_tool_calls: 7 } },
    deny: { skills: { view: ['qa-private-view'], load: ['qa-private-load'], manage: ['qa-private-manage'] } } };
  await seed(page, email, entry);
  await users(page);
  await edit(page, email);
  await expect(page.locator('#govUserAccessLevel')).toHaveValue('');
  await expect(page.locator('#govUserAccessMode')).toHaveValue('');
  await expect(page.locator('#govUserApprovalMode')).toHaveValue('');
  await save(page);
  await page.reload();
  const after = await stored(page, email);
  expect.soft(after.grants.cli.denied_commands, 'an unchanged save must not remove a real legacy command deny').toEqual(['rm']);
  expect.soft(after.grants.files).toEqual(entry.grants.files);
  expect.soft(after.grants.mcp).toEqual(entry.grants.mcp);
  expect.soft(after.grants.usage_caps).toEqual(entry.grants.usage_caps);
  expect.soft(after.description).toBe(entry.description);
  expect.soft(after.deny.skills, 'untouched asymmetric view/load denials must remain intact').toEqual(entry.deny.skills);
  expect(after).not.toHaveProperty('access_mode');
  expect(after).not.toHaveProperty('access_level');
  expect(after).not.toHaveProperty('approval');
});

test('US-SP-POL-013 [bootstrap affordance] typing a recovery admin identity explains and locks ineffective restrictions', async ({ page }) => {
  await users(page);
  // No bootstrap account is changed: this is a read-only form affordance test.
  const admins = (await api(page, '/api/governance/policy')).body.policy.bootstrap_admins;
  expect(admins).toContain('admin@example.test');
  await selectPicker(page, 'govUserEmail', 'ADMIN@example.test');
  await expect(page.locator('#govUserBootstrapHint')).toBeVisible();
  for (const id of ['govUserAccessLevel', 'govUserAccessMode', 'govUserApprovalMode', 'govUserApprovalPrompt']) {
    await expect(page.locator('#' + id)).toBeDisabled();
  }
});

test('US-SP-POL-001 [legacy level-only edit] Keep existing policy retains assigned role access when changing level', async ({ page }) => {
  await open(page);
  const email = userId('level-only');
  await seed(page, email, { roles: ['member'], groups: [] });
  await users(page);
  await edit(page, email);
  await expect(page.locator('#govUserAccessMode')).toHaveValue('');
  await page.locator('#govUserAccessLevel').selectOption('elevated');
  await save(page);
  const preview = await api(page, '/api/governance/preview', { email });
  expect(preview.status).toBe(200);
  expect(preview.body.effective_access.permissions, 'Keep existing policy must not silently behave like an empty whitelist').toContain('chat:use');
});

test('US-SP-UX-007 [preview lifecycle] a stale preview for the same reselected user cannot replace its newer policy', async ({ page }) => {
  await open(page);
  const first = userId('preview-first');
  const second = userId('preview-second');
  await seed(page, first, { roles: ['member'], grants: { cli: { commands: ['qa-old-command'] } } });
  await seed(page, second, { roles: ['member'] });
  await users(page);
  let release!: () => void;
  let captured!: () => void;
  let delayed = false;
  const held = new Promise<void>(resolve => { release = resolve; });
  const reached = new Promise<void>(resolve => { captured = resolve; });
  await page.route('**/api/governance/preview', async route => {
    if (route.request().postDataJSON().email !== first || delayed) return route.continue();
    delayed = true;
    const response = await route.fetch();
    captured();
    await held;
    await route.fulfill({ response });
  });
  try {
    await edit(page, first);
    await reached;
    const current = await api(page, '/api/governance/users');
    const changed = await page.evaluate(async ({ email, etag }) => {
      const r = await fetch('/api/governance/users/update', { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'If-Match': etag },
        body: JSON.stringify({ email, entry: { roles: ['member'], grants: { cli: { commands: ['qa-new-command'] } } } }) });
      return { status: r.status, body: await r.json() };
    }, { email: first, etag: current.body.etag });
    expect(changed.status, JSON.stringify(changed.body)).toBe(200);
    await edit(page, second);
    await edit(page, first);
    await expect(page.locator('#govUserEffective')).toContainText('qa-new-command');
    const finalResponse = page.waitForResponse(r => r.url().endsWith('/api/governance/preview') && r.request().postDataJSON().email === first);
    release();
    await (await finalResponse).finished();
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.locator('#govUserEffective')).toContainText('qa-new-command');
    await expect(page.locator('#govUserEffective')).not.toContainText('qa-old-command');
  } finally { release(); }
});

test('US-SP-UX-007 [save lifecycle] a late save cannot discard a newly selected user draft', async ({ page }) => {
  await open(page);
  const first = userId('save-first');
  const second = userId('save-second');
  await seed(page, first, { roles: ['member'], groups: [] });
  await seed(page, second, { roles: ['member'], groups: [] });
  await users(page);
  await edit(page, first);
  let release!: () => void;
  let captured!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const reached = new Promise<void>(resolve => { captured = resolve; });
  await page.route('**/api/governance/users/update', async route => {
    if (route.request().postDataJSON().email !== first) return route.continue();
    const response = await route.fetch(); // Real persisted mutation; only response delivery is delayed.
    captured();
    await held;
    await route.fulfill({ response });
  });
  try {
    await page.locator('#govUserSave').click();
    await reached;
    await edit(page, second);
    await page.locator('#govUserApprovalMode').selectOption('automatic');
    await page.locator('#govUserApprovalPrompt').fill('Unsaved second user policy must survive the first user response.');
    release();
    await expect(page.locator('#govUserSave')).toBeEnabled();
    await expect(page.locator('#govUserEmail')).toHaveValue(second);
    await expect(page.locator('#govUserApprovalPrompt')).toHaveValue('Unsaved second user policy must survive the first user response.');
    expect((await stored(page, second)).approval).toBeUndefined();
  } finally { release(); }
});

test('US-SP-UX-007 [inspector lifecycle] late standalone preview cannot replace a more recently requested user', async ({page}) => {
  await open(page);
  const older=userId('inspector-old'), newer=userId('inspector-new');
  await seed(page,older,{roles:['member'],grants:{permissions:['chat:use']}});
  await seed(page,newer,{roles:['member'],grants:{permissions:['chat:use','sessions:read']}});
  await open(page,'governance');await page.locator('[data-gov-tab="preview"]').click();
  let release!:()=>void, fetched!:()=>void;
  const held=new Promise<void>(r=>release=r), received=new Promise<void>(r=>fetched=r);
  await page.route('**/api/governance/preview',async route=>{
    if(route.request().postDataJSON()?.email!==older)return route.continue();
    const response=await route.fetch();fetched();await held;await route.fulfill({response});
  });
  await page.locator('#govPreviewEmail').fill(older);await page.locator('[onclick="_govRunPreview()"]').click();await received;
  await page.locator('#govPreviewEmail').fill(newer);await page.locator('[onclick="_govRunPreview()"]').click();
  await expect(page.locator('#govPreviewResult')).toContainText(newer);
  const finished=page.waitForResponse(r=>r.url().endsWith('/api/governance/preview')&&r.request().postDataJSON()?.email===older);
  release();await finished;await page.waitForTimeout(100);
  await expect(page.locator('#govPreviewResult')).toContainText(newer);
  await expect(page.locator('#govPreviewResult')).not.toContainText(older);
});
