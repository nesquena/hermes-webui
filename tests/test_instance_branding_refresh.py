"""A branding change must never silently discard another tab's editors."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_returning_tab_preserves_unsaved_cron_prompt_until_reload_is_accepted():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for frontend behavior test")
    source = Path("static/instance-branding.js").read_text()
    driver = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const events = {};
let revision = 'initial', accept = false, reloads = 0, prompts = 0, offline = false;
const context = {
  URL,
  cronFormPrompt: 'Unsaved scheduled-job instructions',
  document: {baseURI: 'https://example.test/demo/', visibilityState: 'visible',
    addEventListener: (name, fn) => {events[name] = fn;}},
  window: {
    __HERMES_INSTANCE_BRANDING__: {effective: {revision: 'initial'}},
    addEventListener: (name, fn) => {events[name] = fn;},
  },
  t: key => key,
  APP_DIALOG: {resolve: null},
  showConfirmDialog: async options => {
    assert.equal(options.message, 'instance_branding_reload_confirm');
    assert.equal(options.focusCancel, true);
    prompts++;
    return accept;
  },
  location: {reload: () => {reloads++; context.cronFormPrompt = '';}},
  fetch: async (url, options) => {
    assert.equal(String(url), 'https://example.test/demo/api/instance-branding');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.credentials, 'same-origin');
    if (offline) throw new Error('offline');
    return {ok: true, json: async () => ({api_version: 1, effective: {revision}})};
  },
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
(async () => {
  await events.pageshow();
  assert.equal(prompts, 0);
  revision = 'changed';
  await events.visibilitychange();
  assert.equal(reloads, 0, 'branding must not silently discard the unfinished cron form');
  assert.equal(context.cronFormPrompt, 'Unsaved scheduled-job instructions');
  assert.equal(prompts, 1);
  await events.pageshow();
  assert.equal(prompts, 1, 'declining once must not nag on every return');
  revision = 'next';
  context.APP_DIALOG.resolve = () => {};
  await events.pageshow();
  assert.equal(prompts, 1, 'an existing dialog must not be replaced');
  context.APP_DIALOG.resolve = null;
  context.document.visibilityState = 'hidden';
  await events.visibilitychange();
  assert.equal(prompts, 1);
  context.document.visibilityState = 'visible';
  offline = true;
  await events.visibilitychange();
  assert.equal(reloads, 0);
  offline = false;
  accept = true;
  await events.visibilitychange();
  assert.equal(prompts, 2);
  assert.equal(reloads, 1, 'a deliberate acceptance applies the new shell');
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("SOURCE", repr(source))
    result = subprocess.run([node, "-e", driver], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
