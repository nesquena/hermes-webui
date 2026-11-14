// Isolated Chromium regressions. Production renderers and event handlers run
// unchanged; no server, microphone, provider, or real approval is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {before, after, test} = require('node:test');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = 'https://workspace-ui-fixture.invalid';
const index = fs.readFileSync('static/index.html', 'utf8');
const voiceStart = index.indexOf('<section id="realtimeVoiceBar"');
const voiceEnd = index.indexOf('\n    <div class="messages-shell">', voiceStart);
assert(voiceStart >= 0 && voiceEnd > voiceStart, 'production voice markup exists');
let browser;
before(async () => { browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || undefined}); });
after(async () => { await browser?.close(); });

async function scene(t) {
  const context = await browser.newContext({serviceWorkers:'block', locale:'en-US'});
  t.after(() => context.close());
  const requests = [], errors = [];
  await context.route('**/*', route => {
    if (route.request().url() === origin + '/') return route.fulfill({contentType:'text/html', body:
      '<!doctype html><html lang="en"><button id="btnRealtimeVoice">Voice</button>' +
      '<textarea id="msg">Unsent draft</textarea>' + index.slice(voiceStart, voiceEnd) +
      '<div id="steps"></div></html>'});
    const url = new URL(route.request().url());
    if (url.origin === origin && ['/static/i18n.js', '/static/i18n/en.js'].includes(url.pathname)) {
      return route.fulfill({contentType:'text/javascript', body:fs.readFileSync('.' + url.pathname, 'utf8')});
    }
    requests.push(route.request().url()); return route.abort('blockedbyclient');
  });
  if (context.routeWebSocket) await context.routeWebSocket('**/*', socket => { requests.push(socket.url()); socket.close(); });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin + '/');
  await page.addScriptTag({url:origin + '/static/i18n.js'});
  await page.evaluate(() => i18nReady);
  for (const file of ['icons.js', 'ui.js', 'tool-friendly.js', 'realtime_voice.js', 'work-steps.js']) {
    await page.addScriptTag({path:'static/' + file});
  }
  t.after(() => { assert.deepEqual(errors, [], 'no uncaught browser errors'); assert.deepEqual(requests, [], 'no external or server requests'); });
  return page;
}

test('voice renders an untitled task and keeps the written draft', async t => {
  const page = await scene(t);
  await page.evaluate(() => {
    synPulseVoice.d.status('muted', true);
    synPulseVoice.applySnapshot({tasks:[{task_id:'untitled', session_id:'child', status:'running', result:'Synthetic work'}]});
  });
  assert.equal(await page.locator('.realtime-voice-task-title').textContent(), 'Background task');
  assert.equal(await page.locator('.realtime-voice-task-status').textContent(), 'Running');
  assert.equal(await page.locator('#msg').inputValue(), 'Unsent draft');
});

test('voice sparse approval keeps exact child identity and exposes both choices', async t => {
  const page = await scene(t);
  await page.evaluate(() => {
    window.__approvals = [];
    synPulseVoice.d.status('muted', true);
    synPulseVoice.approveTask = (...args) => __approvals.push(args);
    synPulseVoice.applySnapshot({tasks:[{task_id:'approve-task', session_id:'child', status:'waiting_approval', title:'Synthetic action', approval:{approval_id:'approval-qa'}}]});
  });
  assert.equal(await page.locator('.realtime-voice-task > div').textContent(), 'Open task in a new tabThis action needs approval.Allow onceDeny');
  assert.equal(await page.locator('[data-approval-choice="once"]').isEnabled(), true);
  assert.equal(await page.locator('[data-approval-choice="deny"]').isEnabled(), true);
  await page.locator('[data-approval-choice="deny"]').click();
  assert.deepEqual(await page.evaluate(() => __approvals), [['approve-task', 'approval-qa', 'deny']]);
});

test('Transparent Stream Enter and Space toggle a tool detail exactly once', async t => {
  const page = await scene(t);
  await page.evaluate(() => {
    const row = buildToolCard({name:'read_file', args:{path:'synthetic.txt'}, snippet:'Synthetic detail', done:true, tid:'transparent-qa'});
    document.getElementById('steps').append(row);
    _wireTransparentHeaderToggle(row.querySelector('.tool-card-header'));
  });
  const header = page.locator('#steps .tool-card-header'), card = page.locator('#steps .tool-card');
  await header.focus();
  await page.keyboard.press('Enter');
  assert.equal(await header.getAttribute('aria-expanded'), 'true');
  assert.equal(await card.evaluate(node => node.classList.contains('open')), true);
  await page.keyboard.press('Space');
  assert.equal(await header.getAttribute('aria-expanded'), 'false');
  assert.equal(await card.evaluate(node => node.classList.contains('open')), false);
  await header.click();
  assert.equal(await header.getAttribute('aria-expanded'), 'true');
});

test('compact steps preserve keyboard access and honest running, stopped, and unconfirmed states', async t => {
  const page = await scene(t);
  await page.evaluate(() => {
    document.getElementById('steps').innerHTML = '<div data-anchor-scene-owner="1"><div class="tool-call-group-summary"><span class="tool-call-group-chevron"></span></div><div id="rows"></div></div>';
    window.__rows = [
      {role:'tool', row_id:'read', status:'running', tool:{id:'read', name:'read_file', args:{path:'synthetic.txt'}, done:false}},
      {role:'tool', row_id:'stop', status:'cancelled', tool:{id:'stop', name:'terminal', done:false}},
      {role:'tool', row_id:'helper', status:'completed', tool:{id:'helper', name:'subagent_progress', args:{status:'running'}, done:true}},
    ];
    for (const row of __rows) document.getElementById('rows').append(_anchorSceneNodeForRow(row, {settled:false}));
    syncWorkStepOverview(document.querySelector('[data-anchor-scene-owner]'), __rows, {settled:false});
  });
  assert.equal(await page.locator('.work-step-summary').textContent(), '3 steps · 1 running · 1 stopped · 1 unconfirmed');
  assert.deepEqual(await page.locator('.work-step-number').allTextContents(), ['1','2','3']);
  const header = page.locator('#steps .tool-card-header').first();
  await header.focus(); await page.keyboard.press('Enter');
  assert.equal(await header.getAttribute('aria-expanded'), 'true');
  await page.keyboard.press('Space');
  assert.equal(await header.getAttribute('aria-expanded'), 'false');
  await page.evaluate(() => syncWorkStepOverview(document.querySelector('[data-anchor-scene-owner]'), __rows, {settled:true}));
  assert.equal(await page.locator('.work-step-summary').textContent(), '3 steps · 1 stopped · 2 unconfirmed');
});
