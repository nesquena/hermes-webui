import fs from 'node:fs';
import path from 'node:path';
import type {Page, TestInfo} from '@playwright/test';
import {test, expect, open, session, api, auth} from './fixtures';

declare const S: any, _oldestIdx: number, _messagesTruncated: boolean, _messagesGeneration: number, _scrollPinned: boolean, _messageUserUnpinned: boolean, _messageScrollInputGeneration: number, _programmaticScroll: boolean, _loadingOlder: boolean, _loadingSessionId: string|null;

const unique = (prefix: string) => `${prefix}-${Date.now()}`;

async function preference(page: Page, section: string, id: string, key: string, value: boolean) {
  if (!(await page.locator('#mainSettings').isVisible())) await page.locator('.rail-btn[data-panel="settings"]').click();
  await page.locator(`[data-settings-section="${section}"]`).click();
  await page.locator('#' + id).setChecked(value);
  await expect.poll(async () => (await api(page, '/api/settings')).body[key]).toBe(value);
}

async function downloadExact(page: Page, name: string, bytes: Buffer) {
  const pending = page.waitForEvent('download');
  await page.locator('#btnDownloadFile').click();
  const download = await pending;
  expect(download.suggestedFilename()).toBe(name);
  expect(fs.readFileSync((await download.path())!)).toEqual(bytes);
}

async function deniedReader(page: Page, sid: string, name: string, marker: string) {
  const response = await page.request.get(`/api/file/raw?session_id=${sid}&path=${encodeURIComponent(name)}`, {
    headers: {Cookie: auth.cookie_name + '=' + auth.cookies.bob},
  });
  expect(response.status()).toBe(404);
  expect(await response.text()).not.toContain(marker);
}

async function fileSession(page: Page, fixtures: Record<string, string>) {
  for (const [name, content] of Object.entries(fixtures)) fs.writeFileSync(path.join(auth.workspace, name), content);
  await open(page);
  const sid = await session(page);
  await page.locator('#btnWorkspacePanelToggle').click();
  return sid;
}

async function chooseFile(page: Page, name: string) {
  await page.locator('#fileTree .file-item').filter({hasText: name}).click();
  await expect(page.locator('#previewPathText')).toHaveText(name);
}

test('SUPPLEMENT TRANSCRIPT FILES virtualized long history supports Start outline and End without losing or duplicating turns', async ({page}, info) => {
  await virtualizedLongHistory(page, info, false);
});

test('SUPPLEMENT TRANSCRIPT FILES delayed terminal dock follow cannot override newer Start navigation', async ({page}, info) => {
  await virtualizedLongHistory(page, info, true);
});

async function virtualizedLongHistory(page: Page, info: TestInfo, holdDock: boolean) {
  test.setTimeout(90000);
  if (holdDock) await page.addInitScript(() => {
    const w = window as any;
    const raf = window.requestAnimationFrame.bind(window);
    w.__qaHeldDockFollow = [];
    w.__qaDockFollowSchedulers = [];
    window.requestAnimationFrame = callback => {
      const stack = new Error().stack;
      return raf(timestamp => {
        if (w.__qaHoldDockFollow && /at _sync(?:HandoffDockSpace|TerminalTranscriptSpace|ApprovalTranscriptSpace|ClarifyTranscriptSpace)\b/.test(stack || '')) {
          w.__qaHeldDockFollow.push(callback);
          w.__qaDockFollowSchedulers.push(stack);
        } else callback(timestamp);
      });
    };
    w.__qaReleaseDockFollow = () => {
      w.__qaHoldDockFollow = false;
      for (const callback of w.__qaHeldDockFollow.splice(0)) raf(callback);
    };
  });
  await open(page);
  await preference(page, 'preferences', 'settingsVirtualizeTranscript', 'virtualize_transcript', true);
  await preference(page, 'preferences', 'settingsShowConversationOutline', 'show_conversation_outline', true);
  await preference(page, 'appearance', 'settingsSessionJumpButtons', 'session_jump_buttons', true);
  const title = unique('qa-long-transcript');
  const messages = Array.from({length: 120}, (_, index) => [
    {role: 'user', content: `SUPPLEMENT_QUESTION_${String(index).padStart(3, '0')} exact user question.`},
    {role: 'assistant', content: `SUPPLEMENT_ANSWER_${String(index).padStart(3, '0')}\n\n${'A persisted paragraph with distinct content. '.repeat(8)}`},
  ]).flat();
  await page.locator('[data-settings-section="conversation"]').click();
  const chooser = page.waitForEvent('filechooser');
  await page.locator('#btnImportJSON').click();
  const imported = page.waitForResponse(r => r.url().endsWith('/api/session/import') && r.request().method() === 'POST');
  await (await chooser).setFiles({name: 'qa-long.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({title, messages, workspace: auth.workspace}))});
  const response = await imported;
  expect(response.status()).toBe(200);
  const sid = (await response.json()).session.session_id;
  await expect(page).toHaveURL(new RegExp('/session/' + sid));
  await page.waitForFunction(() => typeof _loadingSessionId === 'undefined' || _loadingSessionId === null);
  await expect(page.locator('#messages')).toContainText('SUPPLEMENT_ANSWER_119');
  await expect(page.locator('#jumpToSessionStartBtn')).toBeVisible();
  if (holdDock) {
    // Close a real disposable terminal while at the tail, then delay only the
    // actual dock-layout RAF. Its captured follow intent must yield to Start.
    await page.locator('#msg').fill('/terminal');
    await page.locator('#btnSend').click();
    await expect(page.locator('#terminalSurface .xterm-helper-textarea')).toBeAttached();
    await page.evaluate(() => { (window as any).__qaHoldDockFollow = true; });
    await page.locator('#btnTerminalClose').click();
    await expect(page.locator('#composerTerminalPanel')).toBeHidden();
    await expect.poll(() => page.evaluate(() => (window as any).__qaHeldDockFollow.length)).toBeGreaterThan(0);
  }
  await page.evaluate(() => {
    const w = window as any;
    const el = document.getElementById('messages')!;
    const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')!;
    w.__qaStartNavigation = [];
    const state = () => ({time: performance.now(), top: el.scrollTop, height: el.scrollHeight,
      state: {count:S.messages.length,oldest:_oldestIdx,truncated:_messagesTruncated,generation:_messagesGeneration,pinned:_scrollPinned,unpinned:_messageUserUnpinned,input:_messageScrollInputGeneration},
      rows: [...el.querySelectorAll('[id^="msg-user-"]')].map(n => n.id)});
    Object.defineProperty(el, 'scrollTop', {configurable: true, get() {return descriptor.get!.call(this);}, set(value) {
      const before = state(); descriptor.set!.call(this, value);
      if (w.__qaStartNavigation.length < 160) w.__qaStartNavigation.push({kind:'scroll-write',requested:value,before,after:state(),stack:new Error().stack});
    }});
    const original = w._ensureAllMessagesLoaded;
    w._ensureAllMessagesLoaded = async (...args: any[]) => {
      w.__qaStartNavigation.push({kind:'load-start',...state()});
      const result = await original(...args);
      w.__qaStartNavigation.push({kind:'load-end',result,...state()});
      return result;
    };
    w.__qaStartNavigationRestore = () => { delete (el as any).scrollTop; w._ensureAllMessagesLoaded = original; };
  });
  await page.locator('#jumpToSessionStartBtn').click();
  try {
    await expect(page.locator('#msg-user-0')).toBeInViewport();
    if (holdDock) {
      await page.evaluate(() => (window as any).__qaReleaseDockFollow());
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await expect(page.locator('#msg-user-0')).toBeInViewport();
    }
    const obsoleteTailMoves = await page.evaluate(() => (window as any).__qaStartNavigation.filter((event: any) =>
      event.kind === 'scroll-write' && event.after.state.count === 240 && event.requested > document.getElementById('messages')!.clientHeight));
    expect(obsoleteTailMoves, 'Start must not restore the previous tail while mounting full history').toEqual([]);
  } finally {
    if (holdDock) await info.attach('dock-follow-schedulers', {body: JSON.stringify(await page.evaluate(() => (window as any).__qaDockFollowSchedulers)), contentType:'application/json'});
    await info.attach('start-navigation-diagnostics', {body: JSON.stringify(await page.evaluate(() => (window as any).__qaStartNavigation)), contentType:'application/json'});
    await page.evaluate(() => (window as any).__qaStartNavigationRestore());
  }
  await expect(page.locator('#msg-user-0')).toContainText('SUPPLEMENT_QUESTION_000');
  await expect.poll(() => page.locator('.message-virtual-spacer').count()).toBeGreaterThan(0);
  const renderedQuestions = page.locator('#messages [id^="msg-user-"]');
  await expect.poll(() => renderedQuestions.count()).toBeGreaterThan(0);
  await expect.poll(() => renderedQuestions.count()).toBeLessThan(120);
  await page.locator('#outlineToggleBtn').click();
  await expect(page.locator('#outlinePanel .outline-entry')).toHaveCount(120);
  await page.locator('#outlinePanel .outline-entry').filter({hasText: 'SUPPLEMENT_QUESTION_060'}).click();
  await expect(page.locator('#msg-user-120')).toBeInViewport();
  await expect(page.locator('#msg-user-120')).toContainText('SUPPLEMENT_QUESTION_060');
  await page.locator('.outline-close-btn').click();
  await expect(page.locator('#outlinePanelWrapper')).toBeHidden();
  await page.locator('#scrollToBottomBtn').click();
  await expect(page.locator('#msg-user-238')).toBeInViewport();
  const persisted = (await api(page, `/api/session?session_id=${sid}&messages=1`)).body.session.messages;
  expect(persisted.map((m: any) => ({role: m.role, content: m.content}))).toEqual(messages);
  await page.reload();
  await expect(page.locator('#messages')).toContainText('SUPPLEMENT_ANSWER_119');
  await preference(page, 'preferences', 'settingsVirtualizeTranscript', 'virtualize_transcript', false);
  await page.locator('.rail-btn[data-panel="chat"]').click();
  await page.locator('#conversationBoardBack').click();
  await page.locator('#outlineToggleBtn').click();
  await expect(page.locator('#outlinePanel .outline-entry')).toHaveCount(120);
  await page.locator('#outlinePanel .outline-entry').filter({hasText: 'SUPPLEMENT_QUESTION_000'}).click();
  await expect(page.locator('#msg-user-0')).toBeInViewport();
  await expect(page.locator('#msg-user-0')).toContainText('SUPPLEMENT_QUESTION_000');
  await page.locator('.outline-close-btn').click();
  await expect(page.locator('.message-virtual-spacer')).toHaveCount(0);
  const questionIds = await page.locator('#messages [id^="msg-user-"]').evaluateAll(rows => rows.map(row => row.id));
  expect(questionIds).toHaveLength(120);
  expect(new Set(questionIds).size).toBe(120);
  expect((await api(page, `/api/session?session_id=${sid}&messages=1`)).body.session.messages.map((m: any) => ({role: m.role, content: m.content}))).toEqual(messages);
  const denied = await page.request.get(`/api/session?session_id=${sid}&messages=1`, {headers: {Cookie: auth.cookie_name + '=' + auth.cookies.bob}});
  expect(denied.status()).toBe(404);
  expect(await denied.text()).not.toContain('SUPPLEMENT_QUESTION_');
}

test('SUPPLEMENT TRANSCRIPT FILES CSV table and valid or malformed JSON preserve literal content and exact downloads', async ({page}) => {
  const csvName = unique('qa-preview') + '.csv';
  const jsonName = unique('qa-preview') + '.json';
  const malformedName = unique('qa-malformed') + '.json';
  const semicolonName = unique('qa-semicolon') + '.csv';
  const tabName = unique('qa-tab') + '.csv';
  const brokenCsvName = unique('qa-broken-csv') + '.csv';
  const csv = 'name,note,amount\r\n"café","comma, quoted ""value""",12\r\n"<img src=x onerror=alert(1)>","line1\nline2",0\r\n';
  const json = '{\n  "marker": "SUPPLEMENT_JSON_LITERAL",\n  "html": "<script>window.QA_JSON_RAN=true</script>",\n  "values": [1, true, null]\n}\n';
  const malformed = '{"SUPPLEMENT_MALFORMED_LITERAL": [1, 2, }\n';
  const semicolon = '\ufeff\r\n"quoted, header";value\r\n"  keep spaces  ";"semi;colon"\r\n';
  const tab = 'name\tvalue\rplain\t"tab\tinside"\r';
  const brokenCsv = 'name,value\n"unterminated,value\n';
  const sid = await fileSession(page, {[csvName]: csv, [jsonName]: json, [malformedName]: malformed, [semicolonName]: semicolon, [tabName]: tab, [brokenCsvName]: brokenCsv});
  await chooseFile(page, csvName);
  await downloadExact(page, csvName, Buffer.from(csv));
  await deniedReader(page, sid, csvName, 'café');
  for (const [name, content] of [[jsonName, json], [malformedName, malformed]]) {
    await page.locator('#btnClearPreview').click();
    await chooseFile(page, name);
    await expect(page.locator('#previewCode')).toHaveText(content);
    await expect(page.locator('#previewCode code')).toHaveClass(/language-json/);
    expect(await page.evaluate(() => (window as any).QA_JSON_RAN)).toBeUndefined();
    await downloadExact(page, name, Buffer.from(content));
    await deniedReader(page, sid, name, 'SUPPLEMENT_');
    expect(fs.readFileSync(path.join(auth.workspace, name), 'utf8')).toBe(content);
  }
  await page.locator('#btnClearPreview').click();
  await chooseFile(page, csvName);
  await expect(page.locator('#previewMd .csv-table thead th')).toHaveText(['name', 'note', 'amount']);
  await expect(page.locator('#previewMd .csv-table tbody tr')).toHaveCount(2);
  await expect(page.locator('#previewMd .csv-table tbody tr').first().locator('td')).toHaveText(['café', 'comma, quoted "value"', '12']);
  await expect(page.locator('#previewMd .csv-table tbody tr').last().locator('td')).toHaveText(['<img src=x onerror=alert(1)>', 'line1\nline2', '0']);
  await expect(page.locator('#previewMd img')).toHaveCount(0);
  for (const [name, content, headers, cells] of [[semicolonName, semicolon, ['quoted, header', 'value'], ['  keep spaces  ', 'semi;colon']], [tabName, tab, ['name', 'value'], ['plain', 'tab\tinside']]] as const) {
    await page.locator('#btnClearPreview').click();
    await chooseFile(page, name);
    await expect(page.locator('#previewMd .csv-table thead th')).toHaveText([...headers]);
    await expect(page.locator('#previewMd .csv-table tbody tr')).toHaveCount(1);
    expect(await page.locator('#previewMd .csv-table tbody td').allTextContents()).toEqual([...cells]);
    await downloadExact(page, name, Buffer.from(content));
  }
  await page.locator('#btnClearPreview').click();
  await chooseFile(page, brokenCsvName);
  await expect(page.locator('#previewMd .csv-table')).toHaveCount(0);
  await expect(page.locator('#previewMd .diff-inline-error')).toBeVisible();
  await downloadExact(page, brokenCsvName, Buffer.from(brokenCsv));
});

test('SUPPLEMENT TRANSCRIPT FILES HTML preview runs inside an opaque sandbox and cannot read parent identity or cookies', async ({page}) => {
  const name = unique('qa-sandbox') + '.html';
  const html = '<!doctype html><meta charset="utf-8"><h1>SUPPLEMENT_HTML_PREVIEW</h1><div id="script-result"></div><script>const result={script:true};for(const [key,read] of [["parent",()=>parent.document.title],["cookies",()=>document.cookie],["storage",()=>localStorage.length]]){try{read();result[key]="ALLOWED"}catch(e){result[key]="BLOCKED"}}document.getElementById("script-result").textContent=JSON.stringify(result)</script>\n';
  const sid = await fileSession(page, {[name]: html});
  const before = page.url();
  await chooseFile(page, name);
  await expect(page.locator('#previewHtmlIframe')).toHaveAttribute('sandbox', 'allow-scripts allow-popups allow-popups-to-escape-sandbox');
  const frame = page.frameLocator('#previewHtmlIframe');
  await expect(frame.locator('h1')).toHaveText('SUPPLEMENT_HTML_PREVIEW');
  await expect(frame.locator('#script-result')).toHaveText(JSON.stringify({script: true, parent: 'BLOCKED', cookies: 'BLOCKED', storage: 'BLOCKED'}));
  expect(page.url()).toBe(before);
  await expect(page.locator('#msg')).toBeVisible();
  await downloadExact(page, name, Buffer.from(html));
  await deniedReader(page, sid, name, 'SUPPLEMENT_HTML_PREVIEW');
  expect(fs.readFileSync(path.join(auth.workspace, name), 'utf8')).toBe(html);
});

test('SUPPLEMENT TRANSCRIPT FILES large Markdown falls back to literal text then explicit render is scoped to the current file', async ({page}) => {
  const largeName = unique('qa-large') + '.md';
  const smallName = unique('qa-small') + '.md';
  const large = '# SUPPLEMENT_LARGE_MARKDOWN\n\n' + Array.from({length: 5005}, (_, i) => `Line ${i} stays exact.`).join('\n') + '\n';
  const small = '# SUPPLEMENT_SMALL_MARKDOWN\n\n**Only the selected small file.**\n';
  const sid = await fileSession(page, {[largeName]: large, [smallName]: small});
  await chooseFile(page, largeName);
  await expect(page.locator('#previewCode')).toHaveText(large);
  await expect(page.locator('#previewMd')).toBeHidden();
  await expect(page.locator('#btnRenderMarkdownAnyway')).toBeVisible();
  await downloadExact(page, largeName, Buffer.from(large));
  await page.locator('#btnRenderMarkdownAnyway').click();
  await expect(page.locator('#previewMd h1')).toHaveText('SUPPLEMENT_LARGE_MARKDOWN');
  await expect(page.locator('#previewMd')).toContainText('Line 5004 stays exact.');
  await expect(page.locator('#previewCode')).toBeHidden();
  await expect(page.locator('#btnRenderMarkdownAnyway')).toBeHidden();
  await page.locator('#btnClearPreview').click();
  await chooseFile(page, smallName);
  await expect(page.locator('#previewMd h1')).toHaveText('SUPPLEMENT_SMALL_MARKDOWN');
  await expect(page.locator('#previewMd')).not.toContainText('SUPPLEMENT_LARGE_MARKDOWN');
  await expect(page.locator('#btnRenderMarkdownAnyway')).toBeHidden();
  await downloadExact(page, smallName, Buffer.from(small));
  await page.locator('#btnClearPreview').click();
  await chooseFile(page, largeName);
  await expect(page.locator('#previewCode')).toHaveText(large);
  await expect(page.locator('#btnRenderMarkdownAnyway')).toBeVisible();
  await deniedReader(page, sid, largeName, 'SUPPLEMENT_LARGE_MARKDOWN');
  expect(fs.readFileSync(path.join(auth.workspace, largeName), 'utf8')).toBe(large);
});

test('SUPPLEMENT TRANSCRIPT FILES delayed outline history cannot replace a newer active turn', async ({page}) => {
  test.setTimeout(90000);
  await open(page);
  await preference(page, 'preferences', 'settingsShowConversationOutline', 'show_conversation_outline', true);
  const messages = Array.from({length: 40}, (_, index) => [
    {role: 'user', content: `SUPPLEMENT_RACE_QUESTION_${index}`},
    {role: 'assistant', content: `SUPPLEMENT_RACE_ANSWER_${index}`},
  ]).flat();
  await page.locator('[data-settings-section="conversation"]').click();
  const chooser = page.waitForEvent('filechooser');
  await page.locator('#btnImportJSON').click();
  const imported = page.waitForResponse(r => r.url().endsWith('/api/session/import') && r.request().method() === 'POST');
  await (await chooser).setFiles({name: 'qa-history-race.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({title: unique('qa-history-race'), messages, workspace: auth.workspace}))});
  const response = await imported;
  expect(response.status()).toBe(200);
  const sid = (await response.json()).session.session_id;
  await expect(page).toHaveURL(new RegExp('/session/' + sid));
  await page.waitForFunction(() => _loadingSessionId === null);
  await expect(page.locator('#messages')).toContainText('SUPPLEMENT_RACE_ANSWER_39');
  expect(await page.evaluate('Boolean(_messagesTruncated)')).toBe(true);
  let captured!: () => void;
  let release!: () => void;
  const responseCaptured = new Promise<void>(resolve => { captured = resolve; });
  const releaseResponse = new Promise<void>(resolve => { release = resolve; });
  let held = false;
  await page.route('**/api/session?**', async route => {
    const url = new URL(route.request().url());
    if (!held && url.searchParams.get('session_id') === sid && url.searchParams.get('messages') === '1' && !url.searchParams.has('msg_limit')) {
      held = true;
      const oldResponse = await route.fetch();
      expect(oldResponse.status()).toBe(200);
      captured();
      await releaseResponse;
      await route.fulfill({response: oldResponse});
    } else await route.continue();
  });
  await page.locator('#outlineToggleBtn').click();
  await responseCaptured;
  const marker = 'QA_SLOW_RESPONSE QA_NEW_WHILE_LOADING';
  const reply = 'QA_REPLY: QA_NEW_WHILE_LOADING';
  await page.locator('#msg').fill(marker);
  await page.locator('#btnSend').click();
  await expect(page.locator('#btnSend')).toHaveAttribute('aria-label', 'Stop generation');
  const beforeRelease = await page.evaluate('({truncated:_messagesTruncated,oldest:_oldestIdx})');
  expect(beforeRelease.truncated).toBe(true);
  release();
  await page.waitForFunction(() => _loadingOlder === false);
  expect(await page.evaluate('({truncated:_messagesTruncated,oldest:_oldestIdx})')).toEqual(beforeRelease);
  await expect(page.locator('#messages')).toContainText(marker);
  await expect(page.locator('#outlinePanel .outline-entry')).toHaveCount(0);
  await expect(page.locator('#messages')).toContainText(reply, {timeout: 30000});
  await expect(page.locator('#btnSend')).not.toHaveAttribute('aria-label', 'Stop generation');
  const persisted = (await api(page, `/api/session?session_id=${sid}&messages=1`)).body.session.messages;
  expect(persisted.slice(0, messages.length).map((m: any) => ({role: m.role, content: m.content}))).toEqual(messages);
  expect(persisted.filter((m: any) => m.role === 'user' && m.content === marker)).toHaveLength(1);
  expect(persisted.filter((m: any) => m.role === 'assistant' && String(m.content).includes(reply))).toHaveLength(1);
  await page.locator('.outline-close-btn').click();
  await page.locator('#outlineToggleBtn').click();
  await expect(page.locator('#outlinePanel .outline-entry')).toHaveCount(41);
  await page.locator('#outlinePanel .outline-entry').filter({hasText: 'SUPPLEMENT_RACE_QUESTION_0'}).click();
  await expect(page.locator('#msg-user-0')).toContainText('SUPPLEMENT_RACE_QUESTION_0');
  await expect(page.locator('#msg-user-0')).toBeInViewport();
});

test('SUPPLEMENT TRANSCRIPT FILES newer manual scroll during history loading keeps correct message Edit indices and earlier persisted turns', async ({page}, info) => {
  test.setTimeout(90000);
  await open(page);
  await preference(page, 'preferences', 'settingsVirtualizeTranscript', 'virtualize_transcript', true);
  await preference(page, 'appearance', 'settingsSessionJumpButtons', 'session_jump_buttons', true);
  const messages = Array.from({length: 40}, (_, index) => [
    {role: 'user', content: `SUPPLEMENT_END_RACE_QUESTION_${index}`},
    {role: 'assistant', content: `SUPPLEMENT_END_RACE_ANSWER_${index}\n\n${'Distinct history content stays attached to its message. '.repeat(5)}`},
  ]).flat();
  await page.locator('[data-settings-section="conversation"]').click();
  const chooser = page.waitForEvent('filechooser');
  await page.locator('#btnImportJSON').click();
  const imported = page.waitForResponse(r => r.url().endsWith('/api/session/import') && r.request().method() === 'POST');
  await (await chooser).setFiles({name:'qa-start-end-race.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({title:unique('qa-start-end-race'),messages,workspace:auth.workspace}))});
  const importedResponse = await imported;
  expect(importedResponse.status()).toBe(200);
  const sid = (await importedResponse.json()).session.session_id;
  await expect(page).toHaveURL(new RegExp('/session/' + sid));
  await page.waitForFunction(() => _loadingSessionId === null);
  await expect(page.locator('#messages')).toContainText('SUPPLEMENT_END_RACE_ANSWER_39');
  expect(await page.evaluate('({truncated:_messagesTruncated,oldest:_oldestIdx,count:S.messages.length})')).toEqual({truncated:true,oldest:50,count:30});
  await page.waitForFunction(() => !_programmaticScroll);
  let captured!:()=>void, release!:()=>void;
  const responseCaptured = new Promise<void>(resolve=>{captured=resolve;});
  const releaseResponse = new Promise<void>(resolve=>{release=resolve;});
  let held = false;
  await page.route('**/api/session?**', async route => {
    const url = new URL(route.request().url());
    if (!held && url.searchParams.get('session_id') === sid && url.searchParams.get('messages') === '1' && !url.searchParams.has('msg_limit')) {
      held = true;
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      captured();
      await releaseResponse;
      await route.fulfill({response});
    } else await route.continue();
  });
  try {
    await page.locator('#jumpToSessionStartBtn').click();
    await responseCaptured;
    const startGeneration = await page.evaluate('_messageScrollInputGeneration');
    await page.locator('#messages').hover();
    await page.mouse.wheel(0,-700);
    await page.waitForFunction(() => {const el=document.getElementById('messages')!;return el.scrollHeight-el.scrollTop-el.clientHeight>300;});
    await page.mouse.wheel(0,1400);
    await page.waitForFunction(() => {const el=document.getElementById('messages')!;return el.scrollHeight-el.scrollTop-el.clientHeight<2;});
    expect(await page.evaluate('_messageScrollInputGeneration')).toBeGreaterThan(startGeneration);
    release();
    await page.waitForFunction(() => _loadingOlder === false && !_messagesTruncated && S.messages.length === 80);
    // Pick the actual visible final question by content: an obsolete DOM index
    // would still show the correct text yet truncate the wrong earlier turn.
    const lastUser = page.locator('#messages .msg-row[data-session-msg-idx="78"]');
    await expect(lastUser).toContainText('SUPPLEMENT_END_RACE_QUESTION_39');
    await expect(lastUser).toBeInViewport();
    await lastUser.hover();
    await lastUser.getByRole('button',{name:'Edit message',exact:true}).click();
    await expect(lastUser.locator('.msg-edit-area')).toHaveValue('SUPPLEMENT_END_RACE_QUESTION_39');
    const marker = 'QA_END_NAVIGATION_EDIT_TARGET';
    await lastUser.locator('.msg-edit-area').fill(marker);
    const truncated = page.waitForResponse(r=>r.url().endsWith('/api/session/truncate')&&r.request().method()==='POST');
    await lastUser.locator('.msg-edit-send').click();
    const truncateResponse = await truncated;
    expect(truncateResponse.status()).toBe(200);
    await expect(page.locator('#messages')).toContainText('QA_REPLY: '+marker,{timeout:30000});
    await expect(page.locator('#btnSend')).not.toHaveAttribute('aria-label','Stop generation');
    const persisted=(await api(page,`/api/session?session_id=${sid}&messages=1`)).body.session.messages.map((m:any)=>({role:m.role,content:m.content}));
    await info.attach('superseded-start-edit-target',{body:JSON.stringify({truncate:truncateResponse.request().postDataJSON(),persisted_count:persisted.length,preserved_prefix:persisted.slice(0,78)}),contentType:'application/json'});
    expect(truncateResponse.request().postDataJSON()).toMatchObject({session_id:sid,keep_count:78});
    expect(persisted.slice(0,78)).toEqual(messages.slice(0,78));
    expect(persisted).toHaveLength(80);
    expect(persisted[78]).toEqual({role:'user',content:marker});
    expect(persisted[79].content).toContain('QA_REPLY: '+marker);
    await page.reload();
    await expect(page.locator('#messages')).toContainText('QA_REPLY: '+marker);
    expect((await api(page,`/api/session?session_id=${sid}&messages=1`)).body.session.messages.slice(0,78).map((m:any)=>({role:m.role,content:m.content}))).toEqual(messages.slice(0,78));
  } finally { release(); }
});
