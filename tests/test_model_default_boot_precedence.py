"""
Regression coverage for model selector drift on fresh browser boot.

A stale browser-persisted model (localStorage) must not suppress the configured
profile/server default on page load. Restored sessions may still apply their own
session model later through loadSession(). The boot fix must not make browser
model-state writes pointless or let later model-list refreshes reset a live
in-page selection.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


_DRIVER_SRC = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let openParen = ui.indexOf('(', start);
  let i = openParen + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < ui.length) {
    if (ui[i] === '(') parenDepth++;
    else if (ui[i] === ')') parenDepth--;
    i++;
  }
  i = ui.indexOf('{', i);
  let depth = 1;
  i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}

const calls = {syncModelChip: 0, renderModelDropdown: 0, positionModelDropdown: 0, liveFetches: []};
let modelSelect;
let apiModels;

function makeSelect(options, initialValue) {
  const sel = {id: 'modelSelect', options: [], selectedIndex: -1, selectedOptions: []};
  Object.defineProperty(sel, 'value', {
    get() { return this._value || ''; },
    set(v) {
      this._value = v;
      const idx = this.options.findIndex(o => o.value === v);
      this.selectedIndex = idx;
      this.selectedOptions = idx >= 0 ? [this.options[idx]] : [];
    }
  });
  Object.defineProperty(sel, 'innerHTML', {
    get() { return ''; },
    set(_v) {
      this.options = [];
      this.value = '';
    }
  });
  sel.querySelector = function(_selector) { return this.options[0] || null; };
  sel.appendChild = function(node) {
    const incoming = node && node.tagName === 'OPTGROUP' ? node.children : [node];
    for (const opt of incoming || []) {
      opt.parentElement = opt.parentElement || node;
      this.options.push(opt);
      if (this.selectedIndex < 0) this.value = opt.value;
      else if (this._value === opt.value) this.value = opt.value;
    }
  };
  for (const item of options || []) {
    const group = {tagName: 'OPTGROUP', dataset: {provider: item.provider || ''}, children: []};
    const opt = {tagName: 'OPTION', value: item.value, textContent: item.label || item.value, title: '', dataset: {}, parentElement: group};
    group.children.push(opt);
    sel.appendChild(group);
  }
  sel.value = initialValue || '';
  return sel;
}

function $(id) {
  if (id === 'modelSelect') return modelSelect;
  if (id === 'composerModelDropdown') return {classList: {contains(){ return false; }}};
  return null;
}
function t(key) { return key; }
function getModelLabel(v) { return v; }
function syncModelChip() { calls.syncModelChip++; }
function renderModelDropdown() { calls.renderModelDropdown++; }
function _positionModelDropdown() { calls.positionModelDropdown++; }
function _redirectIfUnauth() { return false; }
function _fetchLiveModels(provider, _sel) { calls.liveFetches.push(provider); }

const document = {
  baseURI: 'http://127.0.0.1/hermes/',
  createElement(tag) {
    const upper = tag.toUpperCase();
    if (upper === 'OPTGROUP') {
      return {
        tagName: 'OPTGROUP',
        label: '',
        dataset: {},
        children: [],
        appendChild(opt) { opt.parentElement = this; this.children.push(opt); },
      };
    }
    return {tagName: upper, value: '', textContent: '', title: '', dataset: {}, parentElement: null};
  },
};
const localStorage = {getItem(){return null;}, setItem(){}, removeItem(){}};
const window = {_defaultModel: null, _activeProvider: null, _configuredModelBadges: {}};
let _dynamicModelLabels = {};
let _liveModelFetchPending = new Set();
let _liveModelCache = {};

for (const name of [
  '_getOptionProviderId', '_providerFromModelValue', '_modelStateForSelect',
  '_captureModelDropdownSelection', '_findModelInDropdown', '_refreshOpenModelDropdown',
  '_applyModelToDropdown', '_reconcileModelDropdownSelection', 'populateModelDropdown'
]) {
  eval(extractFunc(name));
}

const args = JSON.parse(process.argv[3]);
apiModels = args.apiModels;
modelSelect = makeSelect(args.initialOptions, args.initialValue);
var S = {session: args.session || null};

fetch = async function(url) {
  const href = String(url);
  if (href.includes('api/models')) {
    return {json: async () => apiModels};
  }
  throw new Error('unexpected fetch ' + href);
};

populateModelDropdown(args.opts || {}).then(() => {
  process.stdout.write(JSON.stringify({
    selectValue: modelSelect.value,
    selectedProvider: modelSelect.selectedOptions[0] ? _getOptionProviderId(modelSelect.selectedOptions[0]) : null,
    defaultModel: window._defaultModel,
    activeProvider: window._activeProvider,
    calls,
  }));
}).catch(err => {
  console.error(err && err.stack || err);
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def populate_driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("model_default_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def test_boot_settings_applies_default_without_deleting_browser_model_state():
    snippet = _boot_default_apply_snippet()
    assert "Fresh page boot must prefer the profile/server default" in snippet
    assert "if(sel&&typeof _applyModelToDropdown==='function')" in snippet
    assert "if(sel&&!savedState&&typeof _applyModelToDropdown==='function')" not in BOOT_JS
    assert "_clearPersistedModelState" not in snippet
    assert "localStorage.removeItem('hermes-webui-model')" not in snippet
    assert "localStorage.removeItem('hermes-webui-model-state')" not in snippet


def test_boot_model_dropdown_explicitly_requests_profile_default_precedence():
    assert "const _hydrateModelDropdown=({redirectIfUnauth=null}={})=>populateModelDropdown({" in BOOT_JS
    assert "preferProfileDefaultOnFreshBoot:true" in BOOT_JS
    # #2726 invariant: boot path must keep profile/server default ahead of stale
    # browser-persisted state when a default exists. Post-#2716 cherry-pick onto
    # post-stage-batch11 master uses `stateToApply` pattern rather than the
    # original `allowBootSavedModelOverride` variable name. Semantics preserved —
    # check for the actual gate predicate that's equivalent.
    assert (
        "const allowBootSavedModelOverride=!window._defaultModel" in BOOT_JS
        or "!window._defaultModel?savedState:null" in BOOT_JS
    ), "boot.js missing the !window._defaultModel gate for saved-state override"


def test_populate_model_dropdown_reconciles_selection_after_rebuild():
    assert "const previousSelection=_captureModelDropdownSelection(sel);" in UI_JS
    assert "_reconcileModelDropdownSelection(sel,data,previousSelection,opts);" in UI_JS
    snippet = _reconcile_selection_snippet()
    assert "preferProfileDefaultOnFreshBoot" in snippet
    # #4363: each branch now routes through _applyOrEnsure, which delegates to
    # _ensureModelOptionInDropdown so a cross-provider model missing from a
    # partially-rebuilt catalog is injected as a custom option instead of the
    # browser silently snapping the <select> to its first <option>. The
    # branch ORDER + per-branch model/provider arguments are unchanged.
    assert "_applyOrEnsure(data.default_model, data.active_provider||null)" in snippet
    assert "_applyOrEnsure(activeSession.model, activeSession.model_provider||null)" in snippet
    assert "_applyOrEnsure(previousState.model, previousState.model_provider||null)" in snippet
    # the helper must fall back to injecting the missing option, not return null
    assert "_ensureModelOptionInDropdown(modelId, sel, providerId)" in snippet
    assert "_readPersistedModelState()" not in snippet
    assert "localStorage.getItem('hermes-webui-model')" not in snippet


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_non_boot_model_refresh_preserves_current_in_page_selection(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={},
        session=None,
    )

    assert got["selectValue"] == "@expensive:gpt-5.5"
    assert got["selectedProvider"] == "expensive"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_boot_model_refresh_prefers_profile_default_over_stale_selection(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session=None,
    )

    assert got["selectValue"] == "@safe:gpt-4o-mini"
    assert got["selectedProvider"] == "safe"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_session_model_wins_over_boot_default_and_previous_selection(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@expensive:gpt-5.5",
        opts={"preferProfileDefaultOnFreshBoot": True},
        session={"model": "@work:glm-5.1", "model_provider": "work"},
    )

    assert got["selectValue"] == "@work:glm-5.1"
    assert got["selectedProvider"] == "work"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_non_boot_refresh_does_not_reapply_default_when_previous_model_disappears(populate_driver_path):
    got = _run_populate_driver(
        populate_driver_path,
        initial_value="@removed:gpt-old",
        opts={},
        session=None,
        api_groups=[
            {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
        ],
        initial_options=[
            {"provider": "removed", "value": "@removed:gpt-old", "label": "Old"},
        ],
    )

    # The old value is gone, so the browser/select fallback can land on the
    # refreshed first option. The important invariant is that the profile default
    # is not reapplied as a special policy outside fresh boot.
    assert got["selectValue"] == "@safe:gpt-4o-mini"
    assert got["selectedProvider"] == "safe"


def _run_populate_driver(
    driver_path: str,
    *,
    initial_value: str,
    opts: dict,
    session: dict | None,
    api_groups: list[dict] | None = None,
    initial_options: list[dict] | None = None,
):
    groups = api_groups or [
        {"provider": "Safe", "provider_id": "safe", "models": [{"id": "@safe:gpt-4o-mini", "label": "GPT-4o mini"}]},
        {"provider": "Expensive", "provider_id": "expensive", "models": [{"id": "@expensive:gpt-5.5", "label": "GPT-5.5"}]},
        {"provider": "Work", "provider_id": "work", "models": [{"id": "@work:glm-5.1", "label": "GLM-5.1"}]},
    ]
    payload = {
        "initialValue": initial_value,
        "initialOptions": initial_options
        or [
            {"provider": "expensive", "value": "@expensive:gpt-5.5", "label": "GPT-5.5"},
            {"provider": "safe", "value": "@safe:gpt-4o-mini", "label": "GPT-4o mini"},
            {"provider": "work", "value": "@work:glm-5.1", "label": "GLM-5.1"},
        ],
        "apiModels": {
            "active_provider": "safe",
            "default_model": "@safe:gpt-4o-mini",
            "configured_model_badges": {},
            "groups": groups,
        },
        "opts": opts,
        "session": session,
    }
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(REPO / "static" / "ui.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


def _boot_default_apply_snippet() -> str:
    marker = "// Fresh page boot must prefer the profile/server default"
    start = BOOT_JS.index(marker)
    return BOOT_JS[start - 120 : start + 700]


def _reconcile_selection_snippet() -> str:
    marker = "function _reconcileModelDropdownSelection"
    start = UI_JS.index(marker)
    end = UI_JS.index("function _providerQualifiedModelValueForSelect", start)
    return UI_JS[start:end]
