import fs from 'node:fs';
import type {Browser, Page, TestInfo} from '@playwright/test';
import {test, expect, open, api, auth, capture} from './fixtures';

const unique = (label: string) => `qa-supp-${label}-${Date.now()}-${Math.random().toString(16).slice(2, 7)}`;
const response = (page: Page, endpoint: string, method = 'POST') => page.waitForResponse(r =>
  new URL(r.url()).pathname === endpoint && r.request().method() === method);

// Separate signed principals use the same loopback-only boundary as the main
// harness. Record actual interactions across reloads without recording inputs.
async function actor(browser: Browser, info: TestInfo, user = 'bob') {
  const context = await browser.newContext({baseURL: auth.base_url, viewport: {width: 1440, height: 1000}});
  await context.route('**/*', r => ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(r.request().url()).hostname)
    ? r.continue() : r.abort('blockedbyclient'));
  await context.routeWebSocket('**/*', s => ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(s.url()).hostname)
    ? s.connectToServer() : s.close({code: 1008, reason: 'QA requires loopback networking'}));
  await context.addCookies([{name: auth.cookie_name, value: auth.cookies[user], url: auth.base_url, httpOnly: true, sameSite: 'Lax'}]);
  const errors: string[] = [], actions: unknown[] = [];
  context.on('page', p => p.on('pageerror', e => errors.push(e.message)));
  await context.exposeBinding('__qaRecordAction', (_source, action) => { actions.push(action); });
  await context.addInitScript(() => {
    try { localStorage.setItem('hermes-lang', 'en'); localStorage.setItem('hermes-webui-rail-expanded', '1'); }
    catch (error) { if (!(error instanceof DOMException) || error.name !== 'SecurityError') throw error; }
    const w = window as any; w.__qaActions = [];
    for (const event of ['click', 'change', 'input', 'keydown']) document.addEventListener(event, ev => {
      const e = ev.composedPath().find((n: any) => n instanceof Element &&
        (n.matches('button,input,select,textarea,a,[onclick],[role="button"]') || typeof n.onclick === 'function')) as Element | undefined;
      if (!e) return;
      const action = {event, tag: e.tagName, id: e.id,
        text: (e.getAttribute('aria-label') || e.getAttribute('title') || e.textContent || '').trim().slice(0, 90),
        onclick: e.getAttribute('onclick'), panel: e.getAttribute('data-panel'),
        scope: document.querySelector('.panel-view.active')?.id || 'app',
        selection: e.getAttribute('data-selection'), value: e.getAttribute('type') === 'checkbox' ? e.getAttribute('value') : null};
      w.__qaActions.push(action); w.__qaRecordAction(action).catch(() => {});
    }, true);
  });
  const page = await context.newPage();
  return {context, page, async finish() {
    await info.attach(`${user}-all-navigation-controls`, {body: JSON.stringify({actions, controls: []}), contentType: 'application/json'});
    await info.attach(`${user}-uncaught-browser-errors`, {body: JSON.stringify(errors), contentType: 'application/json'});
    await context.close(); expect(errors).toEqual([]);
  }};
}

async function project(page: Page, name: string) {
  await open(page, 'projects');
  await page.locator('.proj-row').filter({hasText: name}).click();
  await expect(page.locator('#projSummary')).toContainText(name);
  await expect(page.locator('#projStartChat')).toBeVisible();
  for (const key of ['team','uploads']) {
    const fold=page.locator('[data-project-section="'+key+'"]');
    await fold.waitFor();
    if (!(await fold.evaluate(e=>e.open))) await fold.locator('summary').click();
  }
}

async function grantProjectWorkspace(page: Page, workspace: string) {
  await open(page, 'governance'); await page.locator('[data-gov-tab="users"]').click();
  const original = (await api(page, '/api/governance/users')).body.users['bob@example.test'];
  await page.locator('#govPaneUsers tr').filter({hasText: 'bob@example.test'}).getByRole('button', {name: 'Edit', exact: true}).click();
  await page.locator('details.gov-access-details').first().locator('summary').click();
  for (const field of ['govUserGrantWorkspaces', 'govUserGrantFilesRead', 'govUserGrantFilesWrite']) {
    const input = page.locator('#' + field + 'Input');
    await input.fill(workspace); await input.press('Enter'); await input.press('Escape');
  }
  const saved = response(page, '/api/governance/users/update'); await page.locator('#govUserSave').click();
  expect((await saved).status()).toBe(200);
  const current = (await api(page, '/api/governance/users')).body.users['bob@example.test'];
  expect(current.grants.workspaces).toEqual([workspace]);
  expect(current.access_mode).toBeUndefined(); expect(current.access_level).toBeUndefined();
  return original;
}

async function restoreMember(page: Page, entry: unknown) {
  const current = await api(page, '/api/governance/users'); expect(current.status).toBe(200);
  const result = await page.evaluate(async ({entry, etag}) => {
    const r = await fetch('/api/governance/users/update', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'If-Match': etag},
      body: JSON.stringify({email: 'bob@example.test', entry})}); return r.status;
  }, {entry, etag: current.body.etag});
  expect(result).toBe(200);
}

async function createBot(page: Page, name: string) {
  await open(page, 'profiles');
  await page.getByRole('button', {name: 'New bot', exact: true}).click();
  await page.locator('#builderName').fill(name);
  await page.locator('#builderTitle').fill(name);
  await page.locator('#builderDescription').fill('Disposable knowledge QA bot');
  await page.locator('#builderNext').click();
  await page.locator('#builderPrompt').fill('Use only the synthetic QA documents selected for this bot.');
  await page.locator('#builderNext').click();
  await page.locator('#builderNext').click();
  await page.locator('#builderNext').click();
  await expect(page.locator('#profileDetailTitle')).toHaveText(name);
}

async function knowledge(page: Page, name: string) {
  await open(page, 'profiles');
  await page.locator('#profilesPanel').getByText(name, {exact: true}).click();
  await page.getByRole('button', {name: 'Edit bot', exact: true}).click();
  await page.locator('#builderTab4').click();
  await expect(page.locator('[data-knowledge-save]')).toBeVisible();
}
// Documents are chosen in the shared picker: a chip means selected, an option
// in the open menu means available. These helpers keep the flow readable.
const docsPicker = (page: Page) => page.locator('.sp-picker:has(#botKnowledgeSelect)');
const docChip = (page: Page, name: string) => docsPicker(page).locator('.sp-chip').filter({hasText: name});
async function openDocs(page: Page) {
  if (!(await page.locator('.sp-picker-menu').count())) await docsPicker(page).locator('.sp-picker-control').click();
  await expect(page.locator('.sp-picker-menu')).toBeVisible();
}
async function closeDocs(page: Page) { await page.keyboard.press('Escape'); await expect(page.locator('.sp-picker-menu')).toHaveCount(0); }
const docOption = (page: Page, name: string) => page.locator('.sp-picker-menu .sp-picker-option').filter({hasText: name});
async function docAvailable(page: Page, name: string) { await openDocs(page); const n = await docOption(page, name).count(); await closeDocs(page); return n; }
async function setDoc(page: Page, name: string, on: boolean) {
  await openDocs(page); const option = docOption(page, name); await expect(option).toBeVisible();
  const selected = /is-selected/.test((await option.getAttribute('class')) || '');
  if (selected !== on) await option.click();
  await closeDocs(page);
  if (on) await expect(docChip(page, name)).toBeVisible(); else await expect(docChip(page, name)).toHaveCount(0);
}
async function upload(page: Page, names: string[]) {
  await page.locator('[data-knowledge-upload]').setInputFiles(names.map(name =>
    ({name, mimeType: 'text/plain', buffer: Buffer.from(`Synthetic private document: ${name}`)})));
  await expect(page.locator('[data-knowledge-status]')).toContainText(`${names.length} document(s) uploaded.`);
  for (const name of names) await expect(docChip(page, name)).toHaveCount(0);
}
async function saveKnowledge(page: Page, expectedStatus = 200) {
  const saved = response(page, '/api/bots/knowledge');
  await page.locator('[data-knowledge-save]').click();
  const result = await saved; expect(result.status()).toBe(expectedStatus);
  const body = await result.json();
  if (expectedStatus === 200) await expect(page.locator('[data-knowledge-status]')).toHaveText('Knowledge selection saved.');
  return body;
}
async function catalog(page: Page, name: string) {
  const r = await api(page, '/api/bots/knowledge?profile=' + name); expect(r.status).toBe(200); return r.body;
}

test('SUPPLEMENT PROJECT KNOWLEDGE retained project pages lose files conversation creation and chat access after membership removal', async ({page, browser}, info) => {
  test.setTimeout(90_000);
  const name = unique('project'), filename = 'project-member-proof.txt', bytes = 'QA_PROJECT_MEMBER_DOWNLOAD';
  await open(page, 'projects');
  const previousWorkspace = (await api(page, '/api/workspaces')).body.last;
  expect(typeof previousWorkspace).toBe('string');
  await page.locator('#projNewName').fill(name);
  const created = response(page, '/api/projects/team'); await page.locator('#projCreateButton').click();
  expect((await created).status()).toBe(200);
  const initial = (await (await created).json()).project;
  await expect(page.locator('#projSummary')).toContainText(name);
  await page.locator('[data-project-section="team"] > summary').click();
  await page.locator('#projHumanChoices input[value="bob@example.test"]').check();
  await page.locator('#projBotChoices input[value="qa-research"]').check();
  const assigned = response(page, '/api/projects/team'); await page.locator('#projSaveTeam').click();
  expect((await assigned).status()).toBe(200);
  const team = (await (await assigned).json()).project;
  expect(team.members).toEqual(['bob@example.test']);
  await page.locator('[data-project-section="uploads"] > summary').click();
  await page.locator('#projUpload').setInputFiles({name: filename, mimeType: 'text/plain', buffer: Buffer.from(bytes)});
  await expect(page.locator('#projFileList').getByRole('button', {name: filename, exact: true})).toBeVisible();
  // Membership and the RBAC workspace ceiling are independent. Establish the
  // exact new project root through the real administrator UI before testing
  // membership removal. The legacy member's unrelated privileges stay intact.
  const originalMember = await grantProjectWorkspace(page, team.workspace);
  let member: Awaited<ReturnType<typeof actor>> | undefined;
  try {
  // Project state is outside the fixture's private OS home, so use the normal
  // admin workspace registration and assignment forms for this exact directory.
  await open(page, 'workspaces');
  await page.getByRole('button', {name: 'Add space', exact: true}).click();
  await page.locator('#workspaceFormPath').fill(team.workspace);
  await page.locator('#workspaceFormName').fill(name);
  await page.locator('#btnSaveWorkspaceDetail').click();
  await expect(page.locator('#workspaceDetailTitle')).toHaveText(name);
  await open(page, 'governance'); await page.locator('[data-gov-tab="workspaces"]').click();
  const workspaceRow = page.locator('#govPaneWorkspaces tbody tr').filter({has: page.getByText(team.workspace, {exact: true})});
  await workspaceRow.locator('[id^="govWsMembers_"]').fill('bob@example.test');
  const workspaceAssigned = response(page, '/api/workspaces/assign');
  await workspaceRow.getByRole('button', {name: 'Save', exact: true}).click();
  expect((await workspaceAssigned).status()).toBe(200);
  await project(page, name);
  member = await actor(browser, info);
    await project(member.page, name);
    await expect(member.page.locator('#projSaveTeam')).toHaveCount(0);
    const downloading = member.page.waitForEvent('download');
    await member.page.locator('#projFileList').getByRole('button', {name: filename, exact: true}).click();
    const download = await downloading; expect(download.suggestedFilename()).toBe(filename);
    expect(fs.readFileSync((await download.path())!, 'utf8')).toBe(bytes);
    const starting = response(member.page, '/api/projects/chat'); await member.page.locator('#projStartChat').click();
    expect((await starting).status()).toBe(200);
    const sid = (await (await starting).json()).session.session_id;
    await expect(member.page.locator('#msg')).toBeVisible();
    await member.page.locator('#msg').fill('@qa-research QA_PROJECT_MEMBER_ALLOWED');
    const accepted = response(member.page, '/api/chat/start'); await member.page.locator('#btnSend').click();
    expect((await accepted).status()).toBe(200);
    await expect(member.page.locator('#messages')).toContainText('QA_REPLY: QA_PROJECT_MEMBER_ALLOWED', {timeout: 30_000});
    await expect.poll(async () => (await api(member.page, '/api/session/status?session_id=' + sid)).body.agent_running).toBe(false);
    const retained = await member.context.newPage(); await project(retained, name);
    await capture(retained, 'member-project-before-revoke', info);
    await page.locator('#projHumanChoices input[value="bob@example.test"]').uncheck();
    const removing = response(page, '/api/projects/team'); await page.locator('#projSaveTeam').click();
    expect((await removing).status()).toBe(200);
    const removed = (await (await removing).json()).project;
    expect(removed.members).toEqual([]); expect(removed.revision).toBe(team.revision + 1);

    // Existing controls send real requests with a now-revoked signed identity.
    let unexpectedDownloads = 0; retained.on('download', () => unexpectedDownloads++);
    const deniedFile = response(retained, '/api/projects/files', 'GET');
    await retained.locator('#projFileList').getByRole('button', {name: filename, exact: true}).click();
    expect((await deniedFile).status()).toBe(404);
    await expect(retained.locator('#toast')).toContainText('Project or file not found');
    expect(unexpectedDownloads).toBe(0);
    const deniedUpload = response(retained, '/api/projects/files');
    await retained.locator('#projUpload').setInputFiles({name: 'forbidden-after-revoke.txt', mimeType: 'text/plain', buffer: Buffer.from('FORBIDDEN')});
    expect((await deniedUpload).status()).toBe(404);
    const deniedStart = response(retained, '/api/projects/chat'); await retained.locator('#projStartChat').click();
    expect((await deniedStart).status()).toBe(404);
    // The composer rejects the stale group in mention preparation, before a
    // chat run can start. Also check the direct start boundary independently.
    const deniedSend = response(member.page, '/api/chat/mentions/prepare');
    await member.page.locator('#msg').fill('@qa-research QA_PROJECT_MEMBER_REVOKED'); await member.page.locator('#btnSend').click();
    expect((await deniedSend).status()).toBe(404);
    expect((await api(member.page, '/api/chat/start', {session_id: sid, message: '@qa-research QA_PROJECT_MEMBER_REVOKED', profile: 'default', workspace: team.workspace})).status).toBe(404);
    expect((await api(member.page, '/api/session?session_id=' + sid)).status).toBe(404);
    expect((await api(member.page, '/api/projects/team', {project_id: initial.project_id, revision: removed.revision, members: ['bob@example.test']})).status).toBe(403);
    const finalFiles = await api(page, '/api/projects/files?project_id=' + initial.project_id);
    expect(finalFiles.status).toBe(200); expect(finalFiles.body.files.map((f: any) => f.name)).toEqual([filename]);
    const persisted = await api(page, '/api/session?session_id=' + sid);
    expect(persisted.status).toBe(200);
    expect(JSON.stringify(persisted.body.session.messages)).not.toContain('QA_PROJECT_MEMBER_REVOKED');
    await capture(retained, 'retained-project-denied-controls', info);
    await project(page, name);
    await expect(page.locator('#projHumanChoices input[value="bob@example.test"]')).not.toBeChecked();
    await open(retained, 'projects'); await expect(retained.locator('.proj-row').filter({hasText: name})).toHaveCount(0);
    await member.page.goto('/session/' + sid); await expect(member.page.locator('#msg')).toBeVisible();
    await expect(member.page.locator('#messages')).not.toContainText('QA_PROJECT_MEMBER_ALLOWED');
  } finally {
    // A successful project chat remembers its directory for the whole profile.
    // Restore that shared preference before removing our exact registration.
    // Use ordinary authenticated session routes, not a direct fixture-file edit.
    const cleanupSession = await api(page, '/api/session/new', {workspace: previousWorkspace});
    expect(cleanupSession.status).toBe(200);
    const cleanupSid = cleanupSession.body.session.session_id;
    try {
      expect((await api(page, '/api/session/update', {session_id: cleanupSid, workspace: previousWorkspace})).status).toBe(200);
      expect((await api(page, '/api/workspaces')).body.last).toBe(previousWorkspace);
      expect((await api(page, '/api/profile/active')).body.default_workspace).toBe(previousWorkspace);
    } finally {
      expect((await api(page, '/api/session/delete', {session_id: cleanupSid})).status).toBe(200);
    }
    expect((await api(page, '/api/workspaces/remove', {path: team.workspace})).status).toBe(200);
    await restoreMember(page, originalMember); if (member) await member.finish();
  }
});

test('SUPPLEMENT PROJECT KNOWLEDGE bot catalogs search deselection and foreign document IDs stay scoped to their own bot', async ({page, browser}, info) => {
  test.setTimeout(90_000);
  const alpha = unique('knowledge-a'), beta = unique('knowledge-b');
  const alphaFile = 'alpha-only.txt', otherFile = 'alpha-secondary.txt', betaFile = 'beta-only.txt';
  await createBot(page, alpha); await knowledge(page, alpha); await upload(page, [alphaFile, otherFile]);
  await openDocs(page); await page.locator('.sp-picker-menu input').fill(alphaFile);
  await expect(docOption(page, alphaFile)).toBeVisible(); await expect(docOption(page, otherFile)).toHaveCount(0);
  await page.locator('.sp-picker-menu input').fill(''); await expect(docOption(page, otherFile)).toBeVisible(); await closeDocs(page);
  await setDoc(page, alphaFile, true); const selectedAlpha = await saveKnowledge(page);
  const alphaId = selectedAlpha.files.find((f: any) => f.name === alphaFile).id;
  expect(selectedAlpha.selected).toEqual([alphaId]);
  await knowledge(page, alpha); await expect(docChip(page, alphaFile)).toBeVisible();
  await setDoc(page, alphaFile, false); const deselected = await saveKnowledge(page);
  expect(deselected.selected).toEqual([]); expect(deselected.revision).toBe(selectedAlpha.revision + 1);
  await knowledge(page, alpha); await expect(docChip(page, alphaFile)).toHaveCount(0);
  await createBot(page, beta); await knowledge(page, beta); await upload(page, [betaFile]);
  expect(await docAvailable(page, alphaFile)).toBe(0);
  await setDoc(page, betaFile, true); const selectedBeta = await saveKnowledge(page);
  const betaId = selectedBeta.files.find((f: any) => f.name === betaFile).id;
  expect(selectedBeta.selected).toEqual([betaId]);
  // No foreign ID is selectable in the UI; forge the direct request to prove
  // the backend rejects it without changing the revision or current selection.
  const foreign = await api(page, '/api/bots/knowledge', {profile: beta, action: 'select', selected: [alphaId], revision: selectedBeta.revision});
  expect(foreign.status).toBe(400); expect(foreign.body.error).toContain('no longer available');
  expect(await catalog(page, beta)).toEqual(selectedBeta);
  expect(await catalog(page, alpha)).toEqual(deselected);
  await knowledge(page, alpha); expect(await docAvailable(page, betaFile)).toBe(0);
  await expect(docChip(page, alphaFile)).toHaveCount(0);
  const member = await actor(browser, info);
  try {
    await open(member.page, 'profiles');
    await expect(member.page.locator('#profilesPanel').getByText(alpha, {exact: true})).toHaveCount(0);
    for (const name of [alpha, beta]) {
      const denied = await api(member.page, '/api/bots/knowledge?profile=' + name);
      expect(denied.status).toBe(403); expect(JSON.stringify(denied.body)).not.toContain('-only.txt');
    }
    await capture(page, 'bot-scoped-document-controls', info);
  } finally { await member.finish(); }
});

test('SUPPLEMENT PROJECT KNOWLEDGE concurrent bot editors reject stale selection without overwriting then recover through reopen', async ({page, browser}, info) => {
  test.setTimeout(90_000);
  const name = unique('knowledge-conflict'), first = 'first-editor.txt', second = 'second-editor.txt';
  await createBot(page, name); await knowledge(page, name); await upload(page, [first, second]);
  const initial = await catalog(page, name);
  const other = await actor(browser, info, 'admin');
  try {
    await knowledge(other.page, name);
    await setDoc(page, first, true); const saved = await saveKnowledge(page);
    const firstId = saved.files.find((f: any) => f.name === first).id, secondId = saved.files.find((f: any) => f.name === second).id;
    expect(saved.selected).toEqual([firstId]); expect(saved.revision).toBe(initial.revision + 1);
    await setDoc(other.page, second, true);
    const conflict = await saveKnowledge(other.page, 409);
    expect(conflict.error).toContain('Bot documents changed elsewhere');
    await expect(other.page.locator('[data-knowledge-status]')).toContainText('Close and reopen the bot editor before saving');
    await expect(docChip(other.page, second)).toBeVisible();
    await expect(docChip(other.page, first)).toHaveCount(0);
    expect(await catalog(page, name)).toEqual(saved);
    await capture(other.page, 'stale-document-selection-conflict', info);
    // Use the visible editor Cancel and Edit controls to obtain a fresh revision.
    await other.page.locator('#btnCancelProfileDetail').click();
    await other.page.locator('#profilesPanel').getByText(name, {exact: true}).click();
    await other.page.getByRole('button', {name: 'Edit bot', exact: true}).click();
    await other.page.locator('#builderTab4').click();
    await expect(docChip(other.page, first)).toBeVisible();
    await expect(docChip(other.page, second)).toHaveCount(0);
    await setDoc(other.page, first, false); await setDoc(other.page, second, true);
    const final = await saveKnowledge(other.page); expect(final.selected).toEqual([secondId]); expect(final.revision).toBe(saved.revision + 1);
    expect(final.files).toEqual(initial.files);
    await knowledge(page, name);
    await expect(docChip(page, first)).toHaveCount(0); await expect(docChip(page, second)).toBeVisible();
    expect(await catalog(page, name)).toEqual(final); await capture(page, 'fresh-document-selection-persisted', info);
  } finally { await other.finish(); }
});
