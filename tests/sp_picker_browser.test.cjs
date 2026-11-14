// Real Chromium form semantics: the picker API, native input slot, emitted
// events, and FormData must agree. No application server or directory is used.
const assert = require('node:assert/strict');
const {before, after, test} = require('node:test');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
let browser;
before(async () => { browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || undefined}); });
after(async () => { await browser?.close(); });

async function scene(t, initial = '') {
  const context = await browser.newContext({serviceWorkers:'block'});
  t.after(() => context.close());
  const requests = [], errors = [];
  await context.route('**/*', route => { requests.push(route.request().url()); return route.abort('blockedbyclient'); });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.setContent('<form id="editor"><label for="person">Person</label><input id="person" name="person" data-sp-picker data-sp-source="fixture">' +
    '<label for="members">Members</label><input id="members" name="members" data-sp-picker data-sp-source="fixture" data-sp-multi="1">' +
    '<button id="reset" type="reset">Reset form</button></form>');
  await page.evaluate(initial => { document.getElementById('person').value = initial; document.getElementById('members').value = initial; }, initial);
  await page.addScriptTag({path:'static/sp-picker.js'});
  await page.evaluate(() => {
    SpPicker.sources.fixture = () => ['alpha', 'beta', 'gamma'].map(value => ({value, label:value.toUpperCase()}));
    SpPicker.mountAll(document.getElementById('editor'));
    window.__nativeValue = input => Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').get.call(input);
    window.__read = id => {
      const input = document.getElementById(id);
      return {property:input.value, native:__nativeValue(input), form:new FormData(input.form).get(input.name), values:SpPicker.get(id)};
    };
    window.__events = [];
    for (const name of ['input', 'change']) document.getElementById('editor').addEventListener(name, event => {
      if (event.target.matches('[data-sp-picker]')) __events.push({event:name, id:event.target.id, state:__read(event.target.id)});
    });
  });
  t.after(() => { assert.deepEqual(errors, []); assert.deepEqual(requests, []); });
  return page;
}
async function assertValue(page, id, values) {
  const csv = values.join(', ');
  assert.deepEqual(await page.evaluate(id => __read(id), id), {property:csv, native:csv, form:csv, values});
}
function control(page, id) { return page.locator('.sp-picker').filter({has:page.locator('#' + id)}).locator('.sp-picker-control'); }

test('programmatic picker edits update native value and FormData without emitting input events', async t => {
  const page = await scene(t);
  await page.evaluate(() => { document.getElementById('person').value = ' beta, beta, gamma '; });
  await assertValue(page, 'person', ['beta']);
  assert.deepEqual(await page.evaluate(() => __events), []);
  assert.equal(await control(page, 'person').locator('.sp-chip-label').textContent(), 'BETA');
  await page.evaluate(() => { document.getElementById('person').value = ''; });
  await assertValue(page, 'person', []);
});

test('selecting a picker option synchronizes native value before input and change events', async t => {
  const page = await scene(t);
  await control(page, 'person').click();
  await page.locator('.sp-picker-option[data-value="beta"]').click();
  await assertValue(page, 'person', ['beta']);
  assert.deepEqual(await page.evaluate(() => __events), ['input', 'change'].map(event => ({event, id:'person', state:{property:'beta', native:'beta', form:'beta', values:['beta']}})));
});

test('removing a stored single selection clears its submitted native value', async t => {
  const page = await scene(t, 'alpha');
  await assertValue(page, 'person', ['alpha']);
  await control(page, 'person').locator('.sp-chip-x').click();
  await assertValue(page, 'person', []);
});

test('multi selection, deselection, and clear submit the same CSV as the picker API', async t => {
  const page = await scene(t, 'alpha');
  await control(page, 'members').click();
  await page.locator('.sp-picker-option[data-value="beta"]').click();
  await assertValue(page, 'members', ['alpha', 'beta']);
  await page.locator('.sp-picker-option[data-value="alpha"]').click();
  await assertValue(page, 'members', ['beta']);
  await page.locator('.sp-picker-clear').click();
  await assertValue(page, 'members', []);
});

test('SpPicker.set keeps multi values normalized and disabled fields retain native form rules', async t => {
  const page = await scene(t);
  await page.evaluate(() => SpPicker.set('members', ['alpha', ' beta ', 'alpha'], true));
  await assertValue(page, 'members', ['alpha', 'beta']);
  await page.evaluate(() => { document.getElementById('members').disabled = true; });
  assert.equal(await page.evaluate(() => new FormData(document.getElementById('editor')).has('members')), false);
  await page.evaluate(() => { document.getElementById('members').disabled = false; SpPicker.set('members', []); });
  await assertValue(page, 'members', []);
});

test('native form reset stays consistent with the hidden input value/default-value mode', async t => {
  const page = await scene(t, 'alpha');
  await page.evaluate(() => { document.getElementById('person').value = 'beta'; });
  await page.locator('#reset').click();
  // Hidden input values reflect their value attribute: reset preserves the
  // latest assigned value, unlike a text input's separate dirty-value slot.
  await assertValue(page, 'person', ['beta']);
});

async function governanceScene(t) {
  const page = await scene(t);
  await page.evaluate(() => {
    window.$ = id => document.getElementById(id);
    window.__GOV_CAT__ = {roles:['member', 'admin'], groups:[], skills:['qa-review'], mcp:[], cli:[], sso:[]};
  });
  await page.addScriptTag({path:'static/governance.js'});
  return page;
}

test('all governance fields without a catalog accept custom grants and denials through their visible picker', async t => {
  const page = await governanceScene(t);
  const fields = [
    ['Permissions', ['permissions'], 'chat:use'], ['Profiles', ['profiles'], 'default'],
    ['Workspaces', ['workspaces'], '/workspace/qa'], ['Routes', ['routes'], '/api/sessions'],
    ['Tools', ['tools', 'builtins'], 'read_file'], ['Toolsets', ['tools', 'toolsets'], 'file'],
    ['Models', ['models', 'models'], 'qa-deterministic'], ['Providers', ['models', 'providers'], 'custom:qa'],
    ['SettingsRead', ['settings', 'read'], 'appearance'], ['SettingsWrite', ['settings', 'write'], 'appearance'],
    ['FilesRead', ['files', 'read_roots'], '/workspace/qa'], ['FilesWrite', ['files', 'write_roots'], '/workspace/qa'],
    ['Workdirs', ['cli', 'workdir_roots'], '/workspace/qa'], ['Environment', ['env', 'vars'], 'QA_PUBLIC_NAME'],
  ];
  await page.evaluate(() => {
    $('editor').innerHTML = _govUserExtraFieldsHtml('grant') + _govUserExtraFieldsHtml('deny');
    document.querySelectorAll('details').forEach(details => { details.open = true; });
    SpPicker.mountAll($('editor'));
  });
  for (const kind of ['Grant', 'Deny']) {
    const expected = {};
    for (const [name, path, value] of fields) {
      const id = 'govUser' + kind + name + 'Input';
      await control(page, id).click();
      await page.locator('.sp-picker-search input').fill(value);
      const option = page.locator('.sp-picker-custom');
      assert.equal(await option.count(), 1, kind + ' ' + name + ' must offer the typed value');
      await option.click();
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#' + id).inputValue(), value);
      let node = expected;
      for (const key of path.slice(0, -1)) node = node[key] ||= {};
      node[path.at(-1)] = [value];
    }
    assert.deepEqual(await page.evaluate(kind => _govCollectExtraUserFields(kind.toLowerCase(), {}), kind), expected);
  }
  await control(page, 'govUserGrantPermissionsInput').locator('.sp-chip-x').click();
  const cleared = await page.evaluate(() => _govCollectExtraUserFields('grant', {}));
  assert.equal(Object.hasOwn(cleared, 'permissions'), false);
  assert.deepEqual(cleared.env.vars, ['QA_PUBLIC_NAME']);
});

test('governance catalog fields still require a real catalog choice', async t => {
  const page = await governanceScene(t);
  await page.evaluate(() => {
    $('editor').innerHTML = _govChipFieldHtml('govUserRolesSel', 'Roles', 'govDlRolesUsr', 'Pick a role');
    SpPicker.mountAll($('editor'));
  });
  await control(page, 'govUserRolesSelInput').click();
  await page.locator('.sp-picker-search input').fill('invented-admin-role');
  assert.equal(await page.locator('.sp-picker-custom').count(), 0);
  await page.locator('.sp-picker-search input').fill('member');
  await page.locator('.sp-picker-option[data-value="member"]').click();
  assert.equal(await page.locator('#govUserRolesSelInput').inputValue(), 'member');
});
