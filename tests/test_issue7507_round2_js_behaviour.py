"""Node behaviour tests for the #7507 round-2 browser fixes.

The source-text assertions in test_issue7507_picker_excludes.py prove the
strings exist; they cannot prove the OBSERVABLE behaviour the reviewer
asked for. This file spawns ``node`` on the real ``static/ui.js`` /
``static/panels.js`` / ``static/boot.js``, extracts the functions under
test, and drives them against a mocked DOM + fake fetch:

* ``_ensureModelOptionInDropdown`` must REFUSE to synthesize an option for
  an excluded model (non-session selection) while still honouring the
  running-session exception (``allowExcludedForActiveSession``).
* ``_fetchLiveModels`` must drop a live response that arrives after the
  invalidation epoch was bumped (``_liveModelFetchEpoch``).
* ``_addLiveModelsToSelect`` must drop excluded rows from a live payload,
  including the cached-``_liveModelCache`` path.
* ``_applySessionModelFallback`` must not land on an excluded model.
* The Settings-modal live fetch must not re-add an excluded id.

Mirrors tests/test_goal_command_js_behaviour.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3] || '';

// ── extract a top-level function by name (brace-matched) ──────────────────
function extractFunc(name, fromSrc) {
  const source = fromSrc || src;
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(source);
  if (!m) throw new Error(name + ' not found');
  const start = m.index;
  let i = source.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < source.length) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') depth--;
    i++;
  }
  return source.slice(start, i);
}

// ── mocked DOM ───────────────────────────────────────────────────────────
function makeSelect(id) {
  const listeners = {};
  return {
    id: id || 'modelSelect',
    innerHTML: '',
    options: [],
    value: '',
    dataset: {},
    selectedOptions: [],
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild(el) {
      this.options.push(el);
      if (this.options.length === 1) this.value = el.value;
      return el;
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
  };
}
function makeOption(value, provider, custom) {
  return {
    value: value,
    textContent: value,
    title: '',
    tagName: 'OPTION',
    dataset: custom ? { custom: '1', provider: provider || '' } : { provider: provider || '' },
  };
}
function makeOptgroup(label, provider, options) {
  return {
    tagName: 'OPTGROUP',
    label: label,
    dataset: { provider: provider },
    children: options,
    appendChild(o) { this.children.push(o); return o; },
  };
}
global.document = {
  createElement: (tag) => {
    if (String(tag).toLowerCase() === 'option') return { tagName: 'OPTION', value: '', textContent: '', title: '', dataset: {} };
    if (String(tag).toLowerCase() === 'optgroup') return { tagName: 'OPTGROUP', label: '', dataset: {}, children: [], appendChild(o){ this.children.push(o); return o; } };
    return { tagName: String(tag).toUpperCase(), dataset: {}, children: [], appendChild(o){ this.children.push(o); return o; } };
  },
  getElementById: () => null,
  baseURI: 'http://localhost/app/',
};
global.window = {};
global.localStorage = { _s: new Map(), getItem(k){ return this._s.has(k) ? this._s.get(k) : null; }, setItem(k,v){ this._s.set(k, String(v)); }, removeItem(k){ this._s.delete(k); } };
global.sessionStorage = { _s: new Map(), getItem(k){ return this._s.has(k) ? this._s.get(k) : null; }, setItem(k,v){ this._s.set(k, String(v)); }, removeItem(k){ this._s.delete(k); } };
global.navigator = { onLine: true };
global.console = console;

// ── globals ui.js expects ────────────────────────────────────────────────
const S = { session: null };
let _dynamicModelLabels = {};
let _liveModelCache = {};
let _liveModelFetchPending = new Set();
let _modelDropdownRequestSeq = 1;  // 1 == "the sequence a live populateModelDropdown started"
let _liveModelFetchEpoch = 0;
let _fetchCalls = [];
let _nextFetchResponse = () => ({ json: async () => ({ models: [] }) });
let _redirectCalls = 0;

const $ = () => null;
const _providerFromModelValue = (v) => {
  const value = String(v || '').trim();
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
};
function _bumpLiveModelFetchEpoch(){ _liveModelFetchEpoch++; return _liveModelFetchEpoch; }
const syncModelChip = () => {};
const syncSettingsModelChip = () => {};
const _refreshOpenModelDropdown = () => {};
const getModelLabel = (m) => String(m || '');
const _deduplicateModelPickerOptions = () => 0;
const _applyModelToDropdown = (modelId, sel, preferredProviderId) => {
  // Minimal stand-in: only resolves an option that already exists with the
  // exact value. Keeps the injected-option branch under test reachable.
  const found = Array.from(sel.options || []).find(o => String(o.value || '') === String(modelId));
  if (found) { sel.value = found.value; return found.value; }
  return null;
};
const _modelStateForSelect = (sel, modelId) => ({ model: String(modelId || ''), model_provider: null });
const _getOptionProviderId = (opt) => {
  if (!opt) return '';
  if (opt.dataset && opt.dataset.provider) return opt.dataset.provider;
  const group = opt.parentElement;
  if (group && group.tagName === 'OPTGROUP' && group.dataset && group.dataset.provider) return group.dataset.provider;
  const value = String(opt.value || '');
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
};
const _modelPickerOptionIdentity = (modelId, providerId) => {
  let value = String(modelId || '');
  const provider = String(providerId || '').trim();
  if (value.startsWith('@') && value.includes(':')) {
    const exactPrefix = provider ? `@${provider}:` : '';
    if (exactPrefix && value.toLowerCase().startsWith(exactPrefix.toLowerCase())) value = value.substring(exactPrefix.length);
    else value = value.substring(value.indexOf(':') + 1);
  }
  return value.split('/').pop().replace(/-/g, '.').toLowerCase();
};
const _findModelInDropdown = (modelId, sel, preferredProviderId) => {
  const options = Array.from(sel.options || []);
  const opts = options.map(o => o.value);
  if (opts.includes(modelId)) return modelId;
  return null;
};
const _redirectIfUnauth = () => { _redirectCalls++; return false; };
const populateModelDropdown = async () => {};
const _invalidateLiveModelCache = async () => {};

// Fetch mock: records the URL and defers to the scenario-controlled response.
global.fetch = async (url) => {
  _fetchCalls.push(String(url));
  return _nextFetchResponse();
};

// ── evaluate the functions under test in this scope ──────────────────────
eval(extractFunc('_bumpLiveModelFetchEpoch'));
eval(extractFunc('_pickerExcludesForProvider'));
eval(extractFunc('_collectKnownPickerProviders'));
eval(extractFunc('_isKnownPickerProvider'));
eval(extractFunc('_bareModelIdForExcludeMatch'));
eval(extractFunc('_modelIsPickerExcluded'));
eval(extractFunc('_ensureModelOptionInDropdown'));
eval(extractFunc('_addLiveModelsToSelect'));
eval(extractFunc('_fetchLiveModels'));
eval(extractFunc('_applySessionModelFallback'));
eval(extractFunc('_modelStateFromAppliedDropdown'));

// The functions under test emit console.debug("Live models loaded ...");
// route that to stderr so stdout stays valid JSON for the Python harness.
const _realConsoleDebug = console.debug.bind(console);
console.debug = (...args) => { process.stderr.write('[dbg] ' + args.map(String).join(' ') + '\n'); };

// ── helpers ──────────────────────────────────────────────────────────────
function buildSelect(options) {
  const sel = makeSelect('modelSelect');
  const _groups = [];
  for (const o of options) {
    if (o.group) {
      const og = makeOptgroup(o.group.label, o.group.provider, []);
      for (const m of o.group.models) {
        const opt = makeOption(m.id, o.group.provider);
        opt.parentElement = og;
        og.children.push(opt);
        sel.options.push(opt);
      }
      _groups.push(og);
    } else {
      const opt = makeOption(o.id, o.provider);
      opt.parentElement = null;
      sel.options.push(opt);
    }
  }
  sel._groups = _groups;
  // Every <option> created anywhere ends up in sel.options so assertions
  // can read a single list — the production code appends to whichever
  // optgroup it found or created.
  sel.querySelectorAll = (q) => (q === 'optgroup' ? (sel._groups || []) : []);
  sel.querySelector = (q) => (q === 'optgroup > option, option' ? (sel.options.find(Boolean) || null) : null);
  // New options (created by the code under test) get tracked on insert.
  const _track = (el) => { if (el && el.tagName === 'OPTION' && !sel.options.includes(el)) sel.options.push(el); if (el && el.tagName === 'OPTGROUP') { sel._groups.push(el); for (const c of el.children || []) _track(c); } return el; };
  for (const og of _groups) {
    const _ogAppend = og.appendChild.bind(og);
    og.appendChild = (o) => { _ogAppend(o); return _track(o); };
  }
  const _selAppend = sel.appendChild.bind(sel);
  sel.appendChild = (el) => { _selAppend(el); return _track(el); };
  sel._track = _track;
  return sel;
}

(async () => {
  const out = { scenario };

  if (scenario === 'ensure_refuses_excluded_default') {
    // Boot default for a new chat is excluded → no option synthesized.
    window._pickerExcludes = { openai: ['gpt-excluded'] };
    const sel = buildSelect([{ id: 'gpt-keep', provider: 'openai' }]);
    const result = _ensureModelOptionInDropdown('gpt-excluded', sel, 'openai');
    out.result = result;
    out.optionCount = sel.options.length;
    out.values = sel.options.map(o => o.value);
  }

  else if (scenario === 'ensure_allows_active_session_model') {
    // The RUNNING session's model is the one exception.
    window._pickerExcludes = { openai: ['gpt-excluded'] };
    const sel = buildSelect([{ id: 'gpt-keep', provider: 'openai' }]);
    const result = _ensureModelOptionInDropdown(
      'gpt-excluded', sel, 'openai', { allowExcludedForActiveSession: true });
    out.result = result;
    out.optionCount = sel.options.length;
    out.isCustom = (sel.options[sel.options.length - 1] || {}).dataset
      && sel.options[sel.options.length - 1].dataset.custom === '1';
  }

  else if (scenario === 'ensure_refuses_named_custom_excluded') {
    // @custom:alpha:chat-a must be matched against the bare 'chat-a' entry.
    window._pickerExcludes = { 'custom:alpha': ['chat-a'] };
    const sel = buildSelect([{ id: 'chat-keep', provider: 'custom:alpha' }]);
    out.result = _ensureModelOptionInDropdown('@custom:alpha:chat-a', sel, 'custom:alpha');
    out.values = sel.options.map(o => o.value);
  }

  else if (scenario === 'ensure_allows_when_no_policy') {
    // No policy configured → pre-existing behaviour must be preserved.
    window._pickerExcludes = {};
    const sel = buildSelect([]);
    const result = _ensureModelOptionInDropdown('gpt-any', sel, 'openai');
    out.result = result;
    out.optionCount = sel.options.length;
  }

  else if (scenario === 'fetch_drops_response_after_epoch_bump') {
    // Fetch is captured at epoch 0; the policy changes (bump) while the
    // response is in flight; the response must be dropped.
    window._pickerExcludes = { openai: ['gpt-excluded'] };
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-keep' }] } }]);
    _nextFetchResponse = () => ({
      json: async () => ({ models: [{ id: 'gpt-excluded', label: 'Excluded' }, { id: 'gpt-new', label: 'New' }] }),
    });
    const pending = _fetchLiveModels('openai', sel, 1);
    // Policy saved mid-flight → invalidation bumps the epoch.
    _bumpLiveModelFetchEpoch();
    await pending;
    out.optionValues = sel.options.map(o => o.value);
    out.cached = _liveModelCache['openai'] || null;
  }

  else if (scenario === 'fetch_applies_response_on_same_epoch') {
    // Control: same epoch → models ARE applied.
    window._pickerExcludes = { openai: ['gpt-excluded'] };
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-keep' }] } }]);
    _nextFetchResponse = () => ({
      json: async () => ({ models: [{ id: 'gpt-excluded', label: 'Excluded' }, { id: 'gpt-new', label: 'New' }] }),
    });
    await _fetchLiveModels('openai', sel, 1);
    out.optionValues = sel.options.map(o => o.value);
  }

  else if (scenario === 'add_live_models_drops_excluded') {
    // A live payload (incl. the cached one) must not contain an excluded id.
    window._pickerExcludes = { openai: ['gpt-excluded'] };
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-keep' }] } }]);
    _liveModelCache['openai'] = [
      { id: 'gpt-excluded', label: 'Excluded' },
      { id: 'gpt-new', label: 'New' },
    ];
    const added = _addLiveModelsToSelect('openai', _liveModelCache['openai'], sel);
    out.added = added;
    out.optionValues = sel.options.map(o => o.value);
  }

  // ── #7777 re-gate scenarios ────────────────────────────────────────────
  else if (scenario === 'r7777_colon_model_id_preserved') {
    // P4: `@opencode-zen:vendor/model:1` — the model half legitimately
    // contains a colon, so the prefix strip must not truncate it to `1`.
    window._pickerExcludes = { 'opencode-zen': ['vendor/model:1', 'gpt-keep'] };
    out.bare = _bareModelIdForExcludeMatch(
      '@opencode-zen:vendor/model:1', _collectKnownPickerProviders());
    out.excludedPrefixed = _modelIsPickerExcluded('@opencode-zen:vendor/model:1', 'opencode-zen');
    out.excludedBareAlias = _modelIsPickerExcluded('vendor/model:1', 'opencode-zen');
    out.keepStillVisible = _modelIsPickerExcluded('@opencode-zen:gpt-keep', 'opencode-zen') === false;
  }

  else if (scenario === 'r7777_case_folding_does_not_hide') {
    // P5: the policy is case-preserving. A lowercase exclusion must not
    // suppress the distinct catalog id MODEL-A.
    window._pickerExcludes = { openai: ['model-a'] };
    out.hidesLower = _modelIsPickerExcluded('model-a', 'openai');
    out.hidesUpper = _modelIsPickerExcluded('MODEL-A', 'openai');
    out.prefixedLower = _modelIsPickerExcluded('@openai:model-a', 'openai');
    out.prefixedUpper = _modelIsPickerExcluded('@openai:MODEL-A', 'openai');
  }

  else if (scenario === 'r7777_slash_id_not_over_filtered') {
    // P6: excluding the bare id `bar` must not remove the distinct valid
    // model `vendor/bar` unless `vendor` is a provider the picker renders.
    window._pickerExcludes = { openai: ['bar'] };
    out.vendorBarHidden = _modelIsPickerExcluded('vendor/bar', 'openai');
    out.bareBarHidden = _modelIsPickerExcluded('bar', 'openai');
  }

  else if (scenario === 'r7777_provider_punctuation_stays_distinct') {
    // P8: `custom:ab` and `custom:a-b` are different providers. Folding every
    // non-alphanumeric character made one provider's exclusions apply to the
    // other (#7777 P1 "distinct custom provider punctuation").
    window._pickerExcludes = { 'custom:ab': ['model-one'], 'custom:a-b': ['model-two'] };
    out.ab = Array.from(_pickerExcludesForProvider('custom:ab')).sort();
    out.aB = Array.from(_pickerExcludesForProvider('custom:a-b')).sort();
    // The dot family still folds: z.ai / zai / ZAI are one provider.
    window._pickerExcludes = { 'z.ai': ['shared-a'], 'zai': ['shared-b'] };
    out.dotAlias = Array.from(_pickerExcludesForProvider('z.ai')).sort();
  }

  else if (scenario === 'r7777_provider_less_policy_publishes_nothing') {
    // P8: without a provider context the old code unioned EVERY provider's
    // list, so an openai-only `shared` exclusion hid `@anthropic:shared`
    // (#7777 P1 "provider-less unions"). A provider-less match is now a
    // false positive: nothing is excluded and nothing is hidden.
    window._pickerExcludes = { openai: ['shared'] };
    out.policyWithoutContext = Array.from(_pickerExcludesForProvider('')).sort();
    out.hiddenWithoutContext = _modelIsPickerExcluded('shared', '');
    out.hiddenForOpenAI = _modelIsPickerExcluded('shared', 'openai');
    out.hiddenForAnthropic = _modelIsPickerExcluded('@anthropic:shared', 'anthropic');
  }

  else if (scenario === 'r7777_alias_exclusions_union') {
    // P3: a settings.json can carry both `zai` and `z.ai`, each with its
    // own slice of hides; the browser must union them.
    window._pickerExcludes = { 'zai': ['shared-a'], 'z.ai': ['shared-b'] };
    const canonical = _pickerExcludesForProvider('zai');
    const aliasForm = _pickerExcludesForProvider('z.ai');
    out.canonical = Array.from(canonical).sort();
    out.aliasForm = Array.from(aliasForm).sort();
  }

  else if (scenario === 'r7777_all_excluded_stays_untouched') {
    // P7: when EVERY option is excluded the fallback must not select the
    // first (forbidden) option; the selection is left untouched.
    window._pickerExcludes = { openai: ['gpt-a', 'gpt-b'] };
    window._defaultModel = '';
    window._activeProvider = 'openai';
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-a' }, { id: 'gpt-b' }] } }]);
    const state = _applySessionModelFallback(sel);
    out.state = state;
    out.selValue = sel.value;
  }

  else if (scenario === 'r7777_excluded_fallback_skips_when_eligible_exists') {
    // P7 control: with one eligible row present the fallback still lands on
    // it rather than the excluded first option.
    window._pickerExcludes = { openai: ['gpt-a'] };
    window._defaultModel = '';
    window._activeProvider = 'openai';
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-a' }, { id: 'gpt-keep' }] } }]);
    const state = _applySessionModelFallback(sel);
    out.state = state;
    out.selValue = sel.value;
  }

  else if (scenario === 'fallback_skips_excluded_default') {
    // The configured default is excluded → fall back to an eligible row.
    window._pickerExcludes = { openai: ['gpt-excluded'] };
    window._defaultModel = 'gpt-excluded';
    window._activeProvider = 'openai';
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-excluded' }, { id: 'gpt-keep' }] } }]);
    const state = _applySessionModelFallback(sel);
    out.state = state;
    out.selValue = sel.value;
  }

  else if (scenario === 'fallback_uses_default_when_not_excluded') {
    // Control: a non-excluded default still wins.
    window._pickerExcludes = { openai: ['gpt-other'] };
    window._defaultModel = 'gpt-default';
    window._activeProvider = 'openai';
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-default' }, { id: 'gpt-keep' }] } }]);
    const state = _applySessionModelFallback(sel);
    out.state = state;
    out.selValue = sel.value;
  }

  else if (scenario === 'fallback_first_option_without_default') {
    // CI regression: with NO configured default the function falls through
    // to the last-resort "first option" path, which also consults the
    // exclude helper. That helper must be at FUNCTION scope — the old
    // block-scoped declaration raised
    // "ReferenceError: _excluded is not defined" here.
    window._pickerExcludes = { openai: ['gpt-excluded'] };
    window._defaultModel = '';
    window._activeProvider = 'openai';
    const sel = buildSelect([{ group: { label: 'OpenAI', provider: 'openai', models: [{ id: 'gpt-excluded' }, { id: 'gpt-keep' }] } }]);
    const state = _applySessionModelFallback(sel);
    out.state = state;
    out.selValue = sel.value;
  }

  else {
    throw new Error('unknown scenario: ' + scenario);
  }
  process.stdout.write(JSON.stringify(out));
})().catch(e => {
  process.stderr.write(String((e && e.stack) || e));
  process.exit(1);
});
"""

# Scenario-specific extras live in the driver above; PANELS/BOOT wiring is
# checked by the Python source tests in test_issue7507_round2_regressions.py.
_DRIVER_SRC += ""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("ui_exclude_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run_scenario(driver_path, scenario, js_path=None):
    result = subprocess.run(
        [NODE, driver_path, str(js_path or UI_JS_PATH), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed for {scenario}: {result.stderr}")
    return json.loads(result.stdout)


# ── Finding 4: browser must not re-add an excluded model for a new chat ───


def test_ensure_refuses_to_synthesize_excluded_default(driver_path):
    out = _run_scenario(driver_path, "ensure_refuses_excluded_default")
    assert out["result"] is None, (
        "_ensureModelOptionInDropdown re-injected an excluded default for a new chat"
    )
    assert out["values"] == ["gpt-keep"]


def test_ensure_allows_running_session_model(driver_path):
    out = _run_scenario(driver_path, "ensure_allows_active_session_model")
    assert out["result"] == "@openai:gpt-excluded"
    assert out["optionCount"] == 2
    assert out["isCustom"] is True, (
        "the active-session exception must still mark the option data-custom=1"
    )


def test_ensure_matches_bare_id_against_named_custom_prefix(driver_path):
    out = _run_scenario(driver_path, "ensure_refuses_named_custom_excluded")
    assert out["result"] is None
    assert out["values"] == ["chat-keep"]


def test_ensure_unchanged_without_policy(driver_path):
    out = _run_scenario(driver_path, "ensure_allows_when_no_policy")
    # Provider-qualified synthesis is the pre-existing behaviour.
    assert out["result"] == "@openai:gpt-any"
    assert out["optionCount"] == 1


def test_fallback_skips_excluded_default(driver_path):
    out = _run_scenario(driver_path, "fallback_skips_excluded_default")
    assert out["selValue"] == "gpt-keep", (
        f"_applySessionModelFallback landed on an excluded model: {out.get('selValue')}"
    )


def test_fallback_uses_default_when_not_excluded(driver_path):
    out = _run_scenario(driver_path, "fallback_uses_default_when_not_excluded")
    assert out.get("selValue") == "gpt-default"


def test_fallback_first_option_without_default(driver_path):
    """The last-resort "first option" path must not raise when there is no
    configured default: the exclude helper lives at FUNCTION scope (a
    block-scoped const made the no-default path raise
    ``ReferenceError: _excluded is not defined`` in the CI browser gates)."""
    out = _run_scenario(driver_path, "fallback_first_option_without_default")
    # gpt-keep is the first NON-excluded option; gpt-excluded is skipped.
    assert out.get("selValue") == "gpt-keep", (
        f"first-option fallback should skip the excluded row: {out.get('selValue')!r}"
    )


# ── Finding 6: fetch epoch ────────────────────────────────────────────────


def test_fetch_drops_response_after_epoch_bump(driver_path):
    out = _run_scenario(driver_path, "fetch_drops_response_after_epoch_bump")
    assert out["optionValues"] == ["gpt-keep"], (
        f"stale live fetch back-filled the picker: {out.get('optionValues')}"
    )
    assert out["cached"] is None, (
        "a response dropped by the epoch guard must not be cached either"
    )


def test_fetch_applies_response_on_same_epoch(driver_path):
    out = _run_scenario(driver_path, "fetch_applies_response_on_same_epoch")
    # gpt-excluded is dropped by the policy filter; gpt-new is applied.
    assert out["optionValues"] == ["gpt-keep", "gpt-new"]


def test_add_live_models_drops_excluded_even_from_cache(driver_path):
    out = _run_scenario(driver_path, "add_live_models_drops_excluded")
    assert out["optionValues"] == ["gpt-keep", "gpt-new"]
    assert out["added"] == 1


# ── #7777 re-gate: seven P1 findings ──────────────────────────────────────


def test_r7777_colon_in_model_id_is_preserved(driver_path):
    """P4 (Colon Model IDs Reappear): stripping at the FINAL colon turned
    ``@opencode-zen:vendor/model:1`` into ``1``, so the browser never
    recognised the configured exclusion and re-injected the model through
    default / fallback / settings / live paths even though the server
    filtered it. The prefix strip is provider-id aware, mirroring
    ``api.config._strip_provider_prefix_from_model_id``."""
    out = _run_scenario(driver_path, "r7777_colon_model_id_preserved")
    assert out["bare"] == "vendor/model:1", out
    assert out["excludedPrefixed"] is True, (
        "the @provider:-prefixed rendering of an excluded id must stay hidden"
    )
    assert out["excludedBareAlias"] is True, (
        "the bare form of the same id must stay hidden"
    )


def test_r7777_case_preserving_matching(driver_path):
    """P5 (Case Folding Hides Models): the browser check lowercased the
    candidate before comparing it with the unchanged exclusion set, so a
    lowercase exclusion ``model-a`` suppressed the distinct catalog id
    ``MODEL-A`` only in the browser — a valid server-provided option
    disappeared from fallback and live-model paths."""
    out = _run_scenario(driver_path, "r7777_case_folding_does_not_hide")
    assert out["hidesLower"] is True
    assert out["hidesUpper"] is False, out
    assert out["prefixedLower"] is True
    assert out["prefixedUpper"] is False, out


def test_r7777_slash_id_is_not_over_filtered(driver_path):
    """P6 (Slash IDs Are Over-Filtered): the browser compared everything
    after the first slash with the exclusion set before checking whether
    the prefix was a known provider, so excluding the bare id ``bar`` also
    removed the distinct valid model ``vendor/bar`` from every producer."""
    out = _run_scenario(driver_path, "r7777_slash_id_not_over_filtered")
    assert out["bareBarHidden"] is True
    assert out["vendorBarHidden"] is False, out


def test_r7777_alias_exclusions_are_unioned(driver_path):
    """P3 (Alias exclusions are dropped): settings persistence allows
    ``zai`` and ``z.ai`` to coexist, each carrying a different slice of
    hides. Looking up only the first matching list ignored the other and
    let those models reappear in every catalog producer."""
    out = _run_scenario(driver_path, "r7777_alias_exclusions_union")
    assert out["canonical"] == ["shared-a", "shared-b"], out
    assert out["aliasForm"] == ["shared-a", "shared-b"], out


def test_r7777_all_excluded_fallback_leaves_selection_untouched(driver_path):
    """P7 (Excluded fallback becomes active): the ``||``-fallback selected
    the FIRST option when the eligibility scan found nothing, which is
    exactly the forbidden model when every option is excluded. It then
    became the active model state instead of leaving the picker without
    an eligible model."""
    out = _run_scenario(driver_path, "r7777_all_excluded_stays_untouched")
    assert out["state"] is None, out
    assert out["selValue"] != "gpt-a", out


def test_r7777_fallback_still_selects_an_eligible_row(driver_path):
    """Control for P7: with an eligible row present the fallback still
    lands on it rather than the excluded first option — the fix removes
    only the forbidden-activation path, not the fallback itself."""
    out = _run_scenario(driver_path, "r7777_excluded_fallback_skips_when_eligible_exists")
    assert out.get("selValue") == "gpt-keep", out


# ── P8: provider punctuation, provider-less policy, Settings fences ───────


def test_custom_provider_punctuation_stays_distinct(driver_path):
    """`custom:ab` and `custom:a-b` are different providers.

    Folding every non-alphanumeric character made one provider's exclusion list
    apply to the other's identically named model. Only the DOT family
    (z.ai / zai / ZAI) may fold.
    """
    out = _run_scenario(driver_path, "r7777_provider_punctuation_stays_distinct")
    assert out["ab"] == ["model-one"], out
    assert out["aB"] == ["model-two"], out
    assert out["dotAlias"] == ["shared-a", "shared-b"], out


def test_provider_less_policy_excludes_nothing(driver_path):
    """Without a provider context nothing may be excluded.

    The old aggregate union made an openai-only `shared` exclusion hide
    `@anthropic:shared`. A provider-less match is now a false positive, while
    the named-provider lookups keep working.
    """
    out = _run_scenario(driver_path, "r7777_provider_less_policy_publishes_nothing")
    assert out["policyWithoutContext"] == [], out
    assert out["hiddenWithoutContext"] is False, out
    assert out["hiddenForOpenAI"] is True, out
    assert out["hiddenForAnthropic"] is False, out


def test_settings_fallback_judges_each_option_by_its_own_provider():
    """#7777 P1: the settings fallback must not judge rows by the active provider.

    Source-shape assertion: the option loop has to read the provider off the
    option itself (`_getOptionProviderId`), because fallback rows can belong to
    providers other than the active one.
    """
    src = PANELS_JS_PATH.read_text(encoding="utf-8")
    assert "_optionProvider=(o)=>(typeof _getOptionProviderId==='function')" in src
    assert "_modelIsPickerExcluded(String(o.value||''),_optionProvider(o))" in src
    assert "_modelIsPickerExcluded(String(o.value||''),(models&&models.active_provider)" not in src


def test_settings_catalog_publication_is_fenced_by_the_policy_epoch():
    """#7777 P1: a stale /api/models response may not republish the policy.

    The request is captured under `_settingsCatalogEpoch`; when the epoch has
    moved (a picker_excludes save happened while the request was in flight) the
    option list is still usable but `window._pickerExcludes` must not be
    overwritten with the pre-save file — which is how it used to reset to `{}`.
    """
    src = PANELS_JS_PATH.read_text(encoding="utf-8")
    assert "const _settingsCatalogEpoch=" in src
    assert "_settingsCatalogStale" in src
    assert "if(!_settingsCatalogStale && models&&typeof models.picker_excludes==='object'" in src
