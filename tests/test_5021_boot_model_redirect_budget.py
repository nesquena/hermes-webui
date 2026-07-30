"""Browserless regression coverage for #5021 boot model redirect budgeting."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
BOOT_JS = ROOT / "static" / "boot.js"
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")
BOOT_MARKER_KEY = "hermes-webui-active-profile-bootstrap-401"


pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute the boot model redirect budget harness",
)


_DRIVER = r"""
const fs = require('fs');
const bootSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSrc = fs.readFileSync(process.argv[3], 'utf8');
const scenario = JSON.parse(process.argv[4] || '{}');
globalThis.window = globalThis;

function extractBlock(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  if (start < 0) throw new Error(`missing block start: ${startMarker}`);
  const end = source.indexOf(endMarker, start);
  if (end < 0) throw new Error(`missing block end: ${endMarker}`);
  return source.slice(start, end);
}

function extractFunction(source, name) {
  const marker = `async function ${name}`;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing function: ${name}`);
  const end = source.indexOf("\n// Cache so we don't re-fetch on every page load", start);
  if (end < 0) throw new Error(`missing end marker after: ${name}`);
  return source.slice(start, end);
}

function extractSingleLineFunction(source, marker) {
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing function marker: ${marker}`);
  const end = source.indexOf("\n", start);
  if (end < 0) throw new Error(`missing newline after function marker: ${marker}`);
  return source.slice(start, end);
}

class FakeStorage {
  constructor(seed = {}) {
    this.store = { ...seed };
  }

  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key)
      ? this.store[key]
      : null;
  }

  setItem(key, value) {
    this.store[key] = String(value);
  }

  removeItem(key) {
    delete this.store[key];
  }

  snapshot() {
    return { ...this.store };
  }
}

class FakeNode {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.classList = { contains: () => false };
    this.style = {};
    this.textContent = '';
    this.label = '';
    this.value = '';
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
}

class FakeSelect extends FakeNode {
  constructor() {
    super('select');
    this.id = 'modelSelect';
    this._innerHTML = '';
    this._value = '';
    this.classList = { contains: () => false };
  }

  set innerHTML(value) {
    this._innerHTML = value;
    this.children = [];
  }

  get innerHTML() {
    return this._innerHTML;
  }

  get options() {
    const options = [];
    for (const child of this.children) {
      if (child.tagName === 'OPTGROUP') {
        options.push(...child.children);
      } else if (child.tagName === 'OPTION') {
        options.push(child);
      }
    }
    return options;
  }

  set value(value) {
    this._value = value;
    const idx = this.options.findIndex((opt) => opt.value === value);
    this.selectedIndex = idx >= 0 ? idx : -1;
  }

  get value() {
    return this._value;
  }

  querySelector(selector) {
    if (selector === 'optgroup > option, option') {
      return this.options[0] || null;
    }
    return null;
  }

  querySelectorAll(selector) {
    if (selector === 'optgroup') {
      return this.children.filter((child) => child.tagName === 'OPTGROUP');
    }
    return [];
  }
}

function buildSelect() {
  return new FakeSelect();
}

function buildModelResponse(payload, status = 200) {
  let jsonCalls = 0;
  return {
    response: {
      status,
      ok: status >= 200 && status < 300,
      json: async () => {
        jsonCalls += 1;
        return payload;
      },
      text: async () => JSON.stringify(payload),
    },
    get jsonCalls() {
      return jsonCalls;
    },
  };
}

function installGlobals(select, redirects, fetchQueue, jsonCalls) {
  const windowObj = globalThis.window || globalThis;
  globalThis.window = windowObj;
  windowObj.location = {
    pathname: '/session/abc',
    search: '?workspace=test',
    set href(value) {
      redirects.push(value);
    },
  };
  globalThis.location = windowObj.location;
  globalThis.sessionStorage = new FakeStorage();
  globalThis.localStorage = new FakeStorage();
  globalThis.document = {
    baseURI: 'http://localhost/session/abc?workspace=test',
    createElement(tag) {
      return new FakeNode(tag);
    },
  };
  globalThis.$ = (id) => (id === 'modelSelect' ? select : null);
  globalThis.getModelLabel = (id) => `label:${id}`;
  globalThis.syncModelChip = () => {};
  globalThis.syncTopbar = () => {};
  globalThis._captureModelDropdownSelection = () => null;
  globalThis._reconcileModelDropdownSelection = () => {};
  globalThis._applyModelToDropdown = () => false;
  globalThis._ensureModelOptionInDropdown = () => {};
  globalThis._refreshOpenModelDropdown = () => {};
  globalThis._modelDropdownRequestSeq = 0;
  globalThis._fetchLiveModels = () => {};
  globalThis.console = { warn() {}, debug() {}, log() {} };
  globalThis.fetch = async () => {
    if (!fetchQueue.length) throw new Error('unexpected fetch');
    const next = fetchQueue.shift();
    if (next.kind === '401') return next.response;
    if (next.kind === 'json') {
      jsonCalls.count += 1;
      return next.response;
    }
    return next.response;
  };
}

const bootBlock = extractBlock(
  bootSrc,
  '  const _bootActiveProfileUnauthRedirectBudget=(()=>{',
  '  // Fetch active profile'
);
const resolveBlock = extractBlock(
  bootSrc,
  '  async function _resolveActiveProfileBootstrapState({',
  '  // Fetch active profile'
);
const bootDropdownBlock = extractBlock(
  bootSrc,
  '  const _redirectBootModelDropdownIfUnauth=(res)=>{',
  '  setTimeout(()=>{'
);
const redirectIfUnauthLine = extractSingleLineFunction(
  uiSrc,
  'function _redirectIfUnauth(res){'
);
const uiBlock = extractFunction(uiSrc, 'populateModelDropdown');

eval(bootBlock.replace(
  '  const _bootActiveProfileUnauthRedirectBudget=(()=>{',
  '  globalThis._bootActiveProfileUnauthRedirectBudget=(()=>{'
));
eval(resolveBlock);
eval(bootDropdownBlock
  .replace(
  '  const _redirectBootModelDropdownIfUnauth=(res)=>{',
  '  globalThis._redirectBootModelDropdownIfUnauth=(res)=>{'
  )
  .replace(
    '  const _hydrateModelDropdown=({redirectIfUnauth=null}={})=>populateModelDropdown({',
    '  globalThis._hydrateModelDropdown=({redirectIfUnauth=null}={})=>populateModelDropdown({'
  )
  .replace(
    '  const _startModelDropdown=()=>{',
    '  globalThis._startModelDropdown=()=>{'
  )
  .replace(
    '  const _startBootModelDropdown=()=>{',
    '  globalThis._startBootModelDropdown=()=>{'
  )
);
eval(redirectIfUnauthLine.replace(
  'function _redirectIfUnauth(res){',
  'globalThis._redirectIfUnauth=function _redirectIfUnauth(res){'
));
eval(uiBlock);

async function runBootAttempt(attempt, redirects, storage, fetchQueue, jsonCalls) {
  const select = buildSelect();
  installGlobals(select, redirects, fetchQueue, jsonCalls);
  globalThis.sessionStorage = storage;

  const state = await _resolveActiveProfileBootstrapState({
    loadActiveProfile: attempt.loadActiveProfile,
    markerStorage: storage,
    getNextUrl: () => attempt.nextUrl || '/session/abc?workspace=test',
    redirectToLogin: (nextUrl) => {
      redirects.push(`login?next=${encodeURIComponent(nextUrl)}`);
    },
  });

  if (state.status !== 'recovery-redirect') {
    globalThis.S = {
      session: attempt.session || null,
      activeProfile: state.profile,
      activeProfileIsDefault: state.isDefault,
    };
    await populateModelDropdown({
      preferProfileDefaultOnFreshBoot: true,
      redirectIfUnauth: _redirectBootModelDropdownIfUnauth,
    });
  }

  return {
    state,
    storage: storage.snapshot(),
  };
}

(async () => {
  const storage = new FakeStorage(scenario.initialStorage || {});
  const redirects = [];
  const jsonCalls = {count: 0};
  const results = [];

  if (scenario.kind === 'boot-fallback-loop') {
    const firstLoad = async () => { const err = new Error('unauthorized'); err.status = 401; throw err; };
    results.push(await runBootAttempt(
      {
        loadActiveProfile: firstLoad,
        nextUrl: '/session/abc?workspace=test',
      },
      redirects,
      storage,
      [
        {kind: '401', response: {status: 401, ok: false, json: async () => ({active_provider: 'openai', default_model: 'gpt-4o', configured_model_badges: {}, groups: []})}},
      ],
      jsonCalls,
    ));

    const secondLoad = async () => { const err = new Error('unauthorized'); err.status = 401; throw err; };
    results.push(await runBootAttempt(
      {
        loadActiveProfile: secondLoad,
        nextUrl: '/session/abc?workspace=test',
      },
      redirects,
      storage,
      [
        {kind: '401', response: {status: 401, ok: false, json: async () => ({active_provider: 'openai', default_model: 'gpt-4o', configured_model_badges: {}, groups: []})}},
      ],
      jsonCalls,
    ));
  } else if (scenario.kind === 'boot-model-only') {
    results.push(await runBootAttempt(
      {
        loadActiveProfile: async () => ({name: 'default', is_default: true}),
        session: null,
      },
      redirects,
      storage,
      [
        {kind: '401', response: {status: 401, ok: false, json: async () => ({active_provider: 'openai', default_model: 'gpt-4o', configured_model_badges: {}, groups: []})}},
      ],
      jsonCalls,
    ));
  } else if (scenario.kind === 'boot-model-success') {
    results.push(await runBootAttempt(
      {
        loadActiveProfile: async () => ({name: 'default', is_default: true}),
        session: null,
      },
      redirects,
      storage,
      [
        {kind: 'json', response: buildModelResponse({
          active_provider: 'anthropic',
          default_model: 'claude-sonnet-4',
          configured_model_badges: {
            'claude-sonnet-4': {provider: 'anthropic'},
          },
          groups: [
            {
              provider: 'Anthropic',
              provider_id: 'anthropic',
              models: [{id: 'claude-sonnet-4', label: 'Claude Sonnet 4'}],
            },
          ],
        }).response},
      ],
      jsonCalls,
    ));
  } else if (scenario.kind === 'post-boot-refresh') {
    const select = buildSelect();
    installGlobals(select, redirects, [], jsonCalls);
    globalThis.sessionStorage = storage;
    globalThis.S = {session: null, activeProfile: 'default', activeProfileIsDefault: true};
    globalThis._bootActiveProfileUnauthRedirectBudget.spendOnFallback(storage);
    globalThis.fetch = async () => ({
      status: 401,
      ok: false,
      json: async () => ({active_provider: 'openai', default_model: 'gpt-4o', configured_model_badges: {}, groups: []}),
    });
    await globalThis._startBootModelDropdown();
    await globalThis._ensureModelDropdownReady();
  } else {
    throw new Error(`unknown scenario: ${scenario.kind}`);
  }

  process.stdout.write(JSON.stringify({
    redirects,
    results,
    jsonCalls: jsonCalls.count,
    storage: storage.snapshot(),
    activeProvider: globalThis.window._activeProvider || null,
    defaultModel: globalThis.window._defaultModel || null,
  }));
})();
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("boot-model-budget-driver") / "driver.js"
    path.write_text(_DRIVER, encoding="utf-8")
    return str(path)


def _run(driver_path, scenario):
    process = subprocess.run(
        [NODE, driver_path, str(BOOT_JS), str(UI_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout)


def test_boot_model_401_does_not_reopen_redirect_after_active_profile_fallback(driver_path):
    payload = _run(
        driver_path,
        {
            "kind": "boot-fallback-loop",
        },
    )

    assert payload["redirects"] == ["login?next=%2Fsession%2Fabc%3Fworkspace%3Dtest"]
    assert len(payload["results"]) == 2
    assert payload["results"][0]["state"]["status"] == "recovery-redirect"
    assert payload["results"][1]["state"]["status"] == "fallback"
    assert payload["results"][1]["storage"] == {}
    assert payload["jsonCalls"] == 0


def test_boot_model_401_consumes_budget_when_profile_load_succeeds(driver_path):
    first = _run(
        driver_path,
        {
            "kind": "boot-model-only",
        },
    )

    second = _run(
        driver_path,
        {
            "kind": "boot-model-only",
            "initialStorage": first["storage"],
        },
    )

    assert first["redirects"] == ["login?next=%2Fsession%2Fabc%3Fworkspace%3Dtest"]
    assert first["results"][0]["state"]["status"] == "resolved"
    assert first["storage"] == {BOOT_MARKER_KEY: "1"}
    assert first["jsonCalls"] == 0
    assert first["activeProvider"] is None
    assert first["defaultModel"] is None
    assert second["redirects"] == []
    assert second["results"][0]["state"]["status"] == "resolved"
    assert second["storage"] == {}
    assert second["jsonCalls"] == 0


def test_boot_model_success_still_stores_active_provider(driver_path):
    payload = _run(
        driver_path,
        {
            "kind": "boot-model-success",
        },
    )

    assert payload["redirects"] == []
    assert payload["activeProvider"] == "anthropic"
    assert payload["defaultModel"] == "claude-sonnet-4"
    assert payload["jsonCalls"] == 1


def test_post_boot_model_refresh_keeps_normal_401_redirect(driver_path):
    payload = _run(
        driver_path,
        {
            "kind": "post-boot-refresh",
        },
    )

    assert payload["redirects"] == ["login?next=%2Fsession%2Fabc%3Fworkspace%3Dtest"]
    assert payload["jsonCalls"] == 0
    assert payload["storage"] == {}
    assert "window._ensureModelDropdownReady=_startModelDropdown;" in BOOT_JS.read_text(encoding="utf-8")
