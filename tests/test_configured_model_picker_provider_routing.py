"""Regression coverage for PR #6221 provider-qualified configured fallback rows."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")


_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unterminated: ' + name);
}

eval([
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_modelPickerOptionIdentity',
  '_deduplicateModelPickerOptions',
  '_modelStateForSelect',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_ensureModelOptionInDropdown',
].map(name => extractFunction(uiSrc, name)).join('\n'));

globalThis._refreshOpenModelDropdown = () => {};
globalThis.syncModelChip = () => {};

globalThis.document = {
  createElement(tag) {
    return {
      tagName: String(tag).toUpperCase(),
      value: '',
      textContent: '',
      dataset: {},
      parentElement: null,
    };
  },
};
globalThis.getModelLabel = value => String(value || '');
globalThis.window = { _configuredModelBadges: {
  '@custom:backup:model-a': {provider: 'custom:backup', role: 'fallback', label: 'Fallback 1'},
} };

const primary = {
  value: 'model-a',
  textContent: 'model-a',
  dataset: {},
  parentElement: {tagName: 'OPTGROUP', dataset: {provider: 'custom:primary'}},
};
const options = [primary];
let selectedIndex = 0;
Object.defineProperty(primary, 'selected', {
  get() { return selectedIndex === 0; },
  set(value) { if (value) selectedIndex = 0; },
});
const select = {
  id: 'modelSelect',
  options,
  querySelectorAll() { return []; },
  appendChild(option) {
    option.parentElement = null;
    options.push(option);
  },
  get selectedOptions() { return selectedIndex >= 0 ? [options[selectedIndex]] : []; },
  get value() { return selectedIndex >= 0 ? options[selectedIndex].value : ''; },
  set value(value) { selectedIndex = options.findIndex(option => option.value === value); },
};

const requested = '@custom:backup:model-a';
const applied = _ensureModelOptionInDropdown(requested, select, 'custom:backup');
const state = _modelStateForSelect(select, select.value);
process.stdout.write(JSON.stringify({
  applied,
  state,
  options: options.map(option => ({value: option.value, provider: _getOptionProviderId(option)})),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_provider_qualified_missing_fallback_cannot_resolve_to_other_provider():
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["state"] == {
        "model": "model-a",
        "model_provider": "custom:backup",
    }
    assert payload["options"][-1] == {
        "value": "@custom:backup:model-a",
        "provider": "custom:backup",
    }


# Colon-bearing model id (e.g. "model-a:free") synthesized as a missing-catalog
# fallback "@custom:backup:model-a:free". Regression for the #6221 re-gate: the
# provider must come from the option's authoritative data-provider, NOT a
# last-colon reparse (which returned the malformed "custom:backup:model-a").
_COLON_DRIVER = _DRIVER.replace(
    "'@custom:backup:model-a': {provider: 'custom:backup', role: 'fallback', label: 'Fallback 1'},",
    "'@custom:backup:model-a:free': {provider: 'custom:backup', role: 'fallback', label: 'Fallback 1'},",
).replace(
    "const requested = '@custom:backup:model-a';",
    "const requested = '@custom:backup:model-a:free';",
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_colon_bearing_missing_fallback_keeps_authoritative_provider():
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", _COLON_DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    # Provider must be "custom:backup" — NOT the last-colon misparse
    # "custom:backup:model-a".
    assert payload["state"] == {
        "model": "model-a:free",
        "model_provider": "custom:backup",
    }
    assert payload["options"][-1] == {
        "value": "@custom:backup:model-a:free",
        "provider": "custom:backup",
    }

# Non-default model of a named custom_providers[] entry. The catalog render
# paths build the option with the qualified "@custom:<slug>:<model>" value and
# NEVER set data-model (that attribute is only set by the fallback injection
# path _ensureModelOptionInDropdown). Regression for #6884: the returned model
# id must be the stripped bare name ("sol"), not the raw dropdown value.
_NON_DEFAULT_CUSTOM_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unterminated: ' + name);
}

eval([
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_modelStateForSelect',
].map(name => extractFunction(uiSrc, name)).join('\n'));

globalThis.document = {
  createElement(tag) {
    return {
      tagName: String(tag).toUpperCase(),
      value: '',
      textContent: '',
      dataset: {},
      parentElement: null,
    };
  },
};
globalThis.getModelLabel = value => String(value || '');
globalThis.window = { _configuredModelBadges: {} };

const group = {tagName: 'OPTGROUP', dataset: {provider: 'custom:hetmer.net'}};
const luna = {
  value: 'luna',
  textContent: 'luna',
  dataset: {},
  parentElement: group,
};
const sol = {
  value: '@custom:hetmer.net:sol',
  textContent: 'sol',
  dataset: {},  // no data-model — normal catalog render path
  parentElement: group,
};
// Colon-bearing model id on the SAME normal catalog render path (no
// data-model). Regression for the #6884 re-gate: the prefix to strip must
// come from the option metadata's authoritative provider (custom:hetmer.net),
// NOT from the last-colon value reparse (which would yield the malformed
// "custom:hetmer.net:model-a" prefix and truncate the model to "free").
const modelAFree = {
  value: '@custom:hetmer.net:model-a:free',
  textContent: 'model-a:free',
  dataset: {},  // no data-model — normal catalog render path
  parentElement: group,
};
const localhostGroup = {tagName: 'OPTGROUP', dataset: {provider: 'custom:localhost:11434'}};
const llama = {
  value: '@custom:localhost:11434:llama3.2',
  textContent: 'llama3.2',
  dataset: {},
  parentElement: localhostGroup,
};
const select = {
  id: 'modelSelect',
  options: [luna, sol, modelAFree, llama],
  querySelectorAll() { return []; },
  get selectedOptions() { return [sol]; },
  get value() { return sol.value; },
  set value(value) {},
};

process.stdout.write(JSON.stringify({
  nonDefault: _modelStateForSelect(select, '@custom:hetmer.net:sol'),
  defaultUnprefixed: _modelStateForSelect(select, 'luna'),
  colonBearingModel: _modelStateForSelect(select, '@custom:hetmer.net:model-a:free'),
  localhostEndpoint: _modelStateForSelect(select, '@custom:localhost:11434:llama3.2'),
  missingOptionCustomInput: _modelStateForSelect(select, '@custom:localhost:11434:mistral-custom'),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_non_default_named_custom_provider_model_strips_qualified_prefix():
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", _NON_DEFAULT_CUSTOM_DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    # #6884: the normally-rendered option has no data-model, so the qualified
    # "@custom:hetmer.net:sol" value must be stripped to the bare model id.
    assert payload["nonDefault"] == {
        "model": "sol",
        "model_provider": "custom:hetmer.net",
    }
    # Sanity: the unprefixed default option still returns its value as-is.
    assert payload["defaultUnprefixed"] == {
        "model": "luna",
        "model_provider": "custom:hetmer.net",
    }
    # Colon-bearing model id on the same normal catalog render path: the strip
    # prefix comes from the option metadata provider (custom:hetmer.net), so
    # the full "model-a:free" survives — not just the "free" suffix (re-gate).
    assert payload["colonBearingModel"] == {
        "model": "model-a:free",
        "model_provider": "custom:hetmer.net",
    }
    # Endpoint-style custom provider (e.g. host:port derived from base_url authority):
    # preserves full custom:localhost:11434 for both catalog and missing-option paths.
    assert payload["localhostEndpoint"] == {
        "model": "llama3.2",
        "model_provider": "custom:localhost:11434",
    }
    assert payload["missingOptionCustomInput"] == {
        "model": "mistral-custom",
        "model_provider": "custom:localhost:11434",
    }


# OpenRouter preset dropdown entries are rendered by the catalog with the
# provider prefix baked into the option value ("openrouter/@preset/<name>") and
# NEVER set data-model (that attribute is only set by the fallback injection
# path _ensureModelOptionInDropdown). Regression for #6936: the model id sent
# must be the qualified "@preset/<name>" with model_provider "openrouter" — NOT
# the raw "openrouter/@preset/<name>" value (which the backend rejects with
# HTTP 400 'openrouter/ is not a valid model ID').
_OPENROUTER_PRESET_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unterminated: ' + name);
}

eval([
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_providerQualifiedPresetRest',
  '_modelStateForSelect',
].map(name => extractFunction(uiSrc, name)).join('\n'));

globalThis.document = {
  createElement(tag) {
    return {
      tagName: String(tag).toUpperCase(),
      value: '',
      textContent: '',
      dataset: {},
      parentElement: null,
    };
  },
};
globalThis.getModelLabel = value => String(value || '');
globalThis.window = { _configuredModelBadges: {} };

const openrouterGroup = {tagName: 'OPTGROUP', dataset: {provider: 'openrouter'}};
const preset = {
  value: 'openrouter/@preset/deepseek-v4-flash',
  textContent: '@preset/deepseek-v4-flash',
  dataset: {},  // no data-model — normal catalog render path
  parentElement: openrouterGroup,
};
const select = {
  id: 'modelSelect',
  options: [preset],
  querySelectorAll() { return []; },
  get selectedOptions() { return [preset]; },
  get value() { return preset.value; },
  set value(value) {},
};

// Sanity: a vendor-prefixed model id under a slash-bearing provider group must
// NOT be stripped — its first-slash prefix ('kilo') does not match the group
// provider ('kilo/minimax') and the remainder is not '@'-qualified.
const kiloGroup = {tagName: 'OPTGROUP', dataset: {provider: 'kilo/minimax'}};
const kilo = {
  value: 'kilo/minimax/minimax-m3',
  textContent: 'kilo/minimax/minimax-m3',
  dataset: {},
  parentElement: kiloGroup,
};
const kiloSelect = {
  id: 'modelSelect',
  options: [kilo],
  querySelectorAll() { return []; },
  get selectedOptions() { return [kilo]; },
  get value() { return kilo.value; },
  set value(value) {},
};

// Non-OpenRouter custom provider with @preset/... must NOT be stripped (#6946):
const customGroup = {tagName: 'OPTGROUP', dataset: {provider: 'custom:acme'}};
const customPreset = {
  value: 'custom:acme/@preset/blue',
  textContent: 'custom:acme/@preset/blue',
  dataset: {},
  parentElement: customGroup,
};
const customSelect = {
  id: 'modelSelect',
  options: [customPreset],
  querySelectorAll() { return []; },
  get selectedOptions() { return [customPreset]; },
  get value() { return customPreset.value; },
  set value(value) {},
};

process.stdout.write(JSON.stringify({
  preset: _modelStateForSelect(select, 'openrouter/@preset/deepseek-v4-flash'),
  vendorPrefixed: _modelStateForSelect(kiloSelect, 'kilo/minimax/minimax-m3'),
  customPreset: _modelStateForSelect(customSelect, 'custom:acme/@preset/blue'),
  restCustom: _providerQualifiedPresetRest('custom:acme/@preset/blue', 'custom:acme'),
  restAlias: _providerQualifiedPresetRest('openrouter/@alias/foo', 'openrouter'),
  restEmpty: _providerQualifiedPresetRest('openrouter/@preset/', 'openrouter'),
  restValid: _providerQualifiedPresetRest('openrouter/@preset/blue', 'openrouter'),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_openrouter_preset_entry_strips_provider_prefix_from_model_id():
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", _OPENROUTER_PRESET_DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    # #6936: the catalog-rendered preset option carries "openrouter/" baked into
    # its value, so the model id must be the qualified "@preset/<name>" with the
    # provider split out — not the raw "openrouter/@preset/<name>" value.
    assert payload["preset"] == {
        "model": "@preset/deepseek-v4-flash",
        "model_provider": "openrouter",
    }
    # Sanity: vendor-prefixed model ids must keep their full value untouched.
    assert payload["vendorPrefixed"] == {
        "model": "kilo/minimax/minimax-m3",
        "model_provider": "kilo/minimax",
    }
    # #6946 finding 1: non-OpenRouter custom provider must NOT be stripped.
    assert payload["customPreset"] == {
        "model": "custom:acme/@preset/blue",
        "model_provider": "custom:acme",
    }
    assert payload["restCustom"] is None
    assert payload["restAlias"] is None
    assert payload["restEmpty"] is None
    assert payload["restValid"] == "@preset/blue"


# Round-trip: send -> persist -> restore for a colon-bearing named
# custom-provider model. The send path emits the bare id ("model-a:free");
# on restore, _findModelInDropdown reverse-matches the bare id back to the
# qualified catalog option ("@custom:hetmer.net:model-a:free") and the
# dropdown re-renders. _isSelectedModelRow must normalize both sides to the
# same model/provider identity so exactly ONE row renders as active /
# "Selected" — with the model id intact, not truncated.
_ROUND_TRIP_DRIVER = r"""
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

function makeClassList(initial) {
  const set = new Set(initial || []);
  return {
    _set: set,
    add(cls) { set.add(cls); },
    remove(cls) { set.delete(cls); },
    contains(cls) { return set.has(cls); },
    toggle(cls, force) {
      if (force === true) { set.add(cls); return true; }
      if (force === false) { set.delete(cls); return false; }
      if (set.has(cls)) { set.delete(cls); return false; }
      set.add(cls);
      return true;
    },
  };
}

function defineClassName(node) {
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList = makeClassList(String(v || '').split(/\s+/).filter(Boolean)); },
  });
}

function makeNode(tag) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    parentElement: null,
    textContent: '',
    value: '',
    tabIndex: 0,
    onclick: null,
    _listeners: {},
    _innerHTML: '',
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        this._ownerSelect.options.push(child);
      }
      return child;
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) { return this._qs ? this._qs[selector] || null : null; },
    setAttribute(name, value) { this[name] = value; },
    focus() { this._focused = true; },
  };
  node.classList = makeClassList();
  defineClassName(node);
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v || '');
      this.children = [];
      this._qs = {};
      if (this.tagName === 'DIV' && this._innerHTML.includes('model-search-input')) {
        const input = makeNode('input');
        input.className = 'model-search-input';
        const clear = makeNode('button');
        clear.className = 'model-search-clear';
        this._qs['.model-search-input'] = input;
        this._qs['.model-search-clear'] = clear;
      } else if (this.tagName === 'DIV' && this._innerHTML.includes('model-custom-input')) {
        const input = makeNode('input');
        input.className = 'model-custom-input';
        const btn = makeNode('button');
        btn.className = 'model-custom-btn';
        this._qs['.model-custom-input'] = input;
        this._qs['.model-custom-btn'] = btn;
      }
    },
  });
  return node;
}

function makeOption(value, label, parent) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  return opt;
}

function makeSelect(groups, selectedValue) {
  const sel = { id: 'modelSelect', children: [], options: [], _value: selectedValue || '' };
  Object.defineProperty(sel, 'value', {get(){return sel._value;}, set(v){sel._value=String(v||'');}});
  Object.defineProperty(sel, 'selectedOptions', {get(){const o=sel.options.find(x=>x.value===sel._value);return o?[o]:[];}});
  sel.appendChild=function(option){option.parentElement=null;sel.options.push(option);};
  sel.querySelectorAll=function(){return [];};
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    for (const model of group.models || []) og.appendChild(makeOption(model.id, model.label || model.id, og));
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
}

function snapshot(dd) {
  const out = [];
  const walk = (node) => {
    for (const child of (node.children || [])) {
      out.push({
        className: child.className,
        textContent: child.textContent,
        html: child._innerHTML || '',
      });
      if (child.children && child.children.length) walk(child);
    }
  };
  walk(dd);
  return out;
}

const payload = JSON.parse(process.argv[3]);
const dropdown = makeNode('div');
dropdown.classList.add('open');
const modelSelect = makeSelect(payload.groups, payload.selectedValue || payload.groups[0].models[0].id);

function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'modelSelect') return modelSelect;
  return null;
}

const window = { _configuredModelBadges: payload.configuredBadges || {} };
const document = { createElement(tag) { return makeNode(tag); } };
function esc(v) { return String(v || ''); }
function t(key, ...args) {
  if (key === 'model_show_all_models') return `Show all ${args[0]} models`;
  return key;
}
function li() { return 'x'; }
function getModelLabel(v) { return String(v || ''); }
function _providerFromModelValue(v) {
  const value = String(v || '');
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
}
function _normalizeConfiguredModelKey(v) { return String(v || '').toLowerCase(); }
function _getConfiguredModelBadge(value, badgeMap) { return badgeMap[value] || null; }
function closeModelDropdown() {}
function syncModelChip() {}
function _refreshOpenModelDropdown() {}
function _deduplicateModelPickerOptions() { return 0; }
async function selectModelFromDropdown(value, provider) {
  _ensureModelOptionInDropdown(value, modelSelect, provider);
  window.__picked=_modelStateForSelect(modelSelect,modelSelect.value);
}

for (const name of [
  '_modelPickerOptionIdentity',
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_isEquivalentConfiguredModelEntry',
  '_getOptionProviderId',
  '_modelStateForSelect',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_ensureModelOptionInDropdown',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

// Send path: the picked state carries the bare, intact model id.
const sent = _modelStateForSelect(modelSelect, '@custom:hetmer.net:model-a:free');
// Restore path: _findModelInDropdown reverse-matches the bare id to the
// qualified catalog option, the picker restores sel.value to it and
// re-renders the dropdown.
const restoreValue = _findModelInDropdown(sent.model, modelSelect, sent.model_provider);
modelSelect.value = restoreValue;
renderModelDropdown();
const rows = snapshot(dropdown).filter(n => String(n.className || '').includes('model-opt'));
const activeRows = rows.filter(n => String(n.className || '').includes(' active'));
process.stdout.write(JSON.stringify({
  sent,
  restoreValue,
  activeCount: activeRows.length,
  activeRow: activeRows.length ? {
    className: activeRows[0].className,
    text: activeRows[0].textContent,
    html: activeRows[0].html,
  } : null,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_colon_bearing_custom_provider_round_trip_restores_selected_row(tmp_path):
    driver = tmp_path / "round_trip_driver.js"
    driver.write_text(_ROUND_TRIP_DRIVER, encoding="utf-8")
    payload = {
        "groups": [
            {
                "provider": "hetmer.net",
                "provider_id": "custom:hetmer.net",
                "models": [
                    {"id": "luna", "label": "luna"},
                    {"id": "@custom:hetmer.net:model-a:free", "label": "model-a:free"},
                ],
            }
        ],
        "configuredBadges": {},
        "selectedValue": "luna",
    }
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)

    # Send path: bare model id, intact (NOT truncated to "free").
    assert actual["sent"] == {
        "model": "model-a:free",
        "model_provider": "custom:hetmer.net",
    }
    # Restore path: the bare id maps back to the qualified catalog option.
    assert actual["restoreValue"] == "@custom:hetmer.net:model-a:free"
    # Exactly one row renders as active/"Selected", with the intact model id.
    assert actual["activeCount"] == 1, actual
    assert "model-opt-badge--selected" in actual["activeRow"]["html"]
    assert "model-a:free" in actual["activeRow"]["html"]
    assert ">free<" not in actual["activeRow"]["html"].replace("model-a:free", "")


# Full #6936 round-trip (production-shaped): the user selects the RAW catalog
# preset option ("openrouter/@preset/<name>" — the value the backend would
# reject), the outgoing state persists "@preset/<name>" + provider "openrouter",
# and after a catalog rebuild/reconcile the SAME provider-aware semantic
# identity is used by the reverse lookup, the dedup survivor rule and the
# selected-row check. One option must remain (the REAL catalog row), it must be
# the row that gets the Selected badge / active state, and the outgoing state
# must stay canonical. The vendor-prefixed non-strip control (kilo/minimax) is
# kept so the preset stripping never leaks into ordinary vendor ids.
_OPENROUTER_ROUNDTRIP_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function extractFunc(name) {
  const marker = 'function ' + name + '(';
  const start = uiSrc.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = uiSrc.indexOf('{', uiSrc.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < uiSrc.length; i++) {
    if (uiSrc[i] === '{') depth += 1;
    else if (uiSrc[i] === '}') {
      depth -= 1;
      if (depth === 0) return uiSrc.slice(start, i + 1);
    }
  }
  throw new Error('unterminated: ' + name);
}

function makeClassList(initial) {
  const _set = new Set(initial || []);
  return {
    _set,
    add(...names) { names.forEach(n => _set.add(n)); },
    remove(...names) { names.forEach(n => _set.delete(n)); },
    contains(name) { return _set.has(name); },
    toggle(name, force) {
      if (force === undefined) { _set.has(name) ? _set.delete(name) : _set.add(name); }
      else { force ? _set.add(name) : _set.delete(name); }
    },
  };
}

function defineClassName(node) {
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList = makeClassList(String(v || '').split(/\s+/).filter(Boolean)); },
  });
}

function makeNode(tag) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    parentElement: null,
    textContent: '',
    value: '',
    tabIndex: 0,
    onclick: null,
    _listeners: {},
    _innerHTML: '',
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        this._ownerSelect.options.push(child);
      }
      return child;
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) { return this._qs ? this._qs[selector] || null : null; },
    setAttribute(name, value) { this[name] = value; },
    focus() { this._focused = true; },
  };
  node.classList = makeClassList();
  defineClassName(node);
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v || '');
      this.children = [];
      this._qs = {};
      if (this.tagName === 'DIV' && this._innerHTML.includes('model-search-input')) {
        const input = makeNode('input');
        input.className = 'model-search-input';
        const clear = makeNode('button');
        clear.className = 'model-search-clear';
        this._qs['.model-search-input'] = input;
        this._qs['.model-search-clear'] = clear;
      } else if (this.tagName === 'DIV' && this._innerHTML.includes('model-custom-input')) {
        const input = makeNode('input');
        input.className = 'model-custom-input';
        const btn = makeNode('button');
        btn.className = 'model-custom-btn';
        this._qs['.model-custom-input'] = input;
        this._qs['.model-custom-btn'] = btn;
      }
    },
  });
  return node;
}

function makeOption(value, label, parent) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  return opt;
}

function makeSelect(groups, selectedValue) {
  const sel = { id: 'modelSelect', children: [], options: [], _value: selectedValue || '' };
  Object.defineProperty(sel, 'value', {get(){return sel._value;}, set(v){sel._value=String(v||'');}});
  Object.defineProperty(sel, 'selectedOptions', {get(){const o=sel.options.find(x=>x.value===sel._value);return o?[o]:[];}});
  sel.appendChild=function(option){option.parentElement=null;sel.children.push(option);sel.options.push(option);};
  sel.querySelectorAll=function(){return sel.children.filter(c=>c.tagName==='OPTGROUP');};
  sel.removeChild=function(option){const i=sel.children.indexOf(option);if(i>=0)sel.children.splice(i,1);const j=sel.options.indexOf(option);if(j>=0)sel.options.splice(j,1);};
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    for (const model of group.models || []) og.appendChild(makeOption(model.id, model.label || model.id, og));
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
}

function findInTree(dd, pred) {
  const stack = [...(dd.children || [])];
  while (stack.length) {
    const n = stack.shift();
    if (pred(n)) return n;
    if (n.children && n.children.length) stack.push(...n.children);
  }
  return null;
}

const dropdown = makeNode('div');
dropdown.classList.add('open');

function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'modelSelect') return modelSelect;
  return null;
}

const window = { _configuredModelBadges: {} };
const document = { createElement(tag) { return makeNode(tag); } };
function esc(v) { return String(v || ''); }
function t(key, ...args) {
  if (key === 'model_show_all_models') return `Show all ${args[0]} models`;
  return key;
}
function li() { return 'x'; }
function getModelLabel(v) { return String(v || ''); }
function _providerFromModelValue(v) {
  const value = String(v || '');
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
}
function _normalizeConfiguredModelKey(v) { return String(v || '').toLowerCase(); }
function _getConfiguredModelBadge(value, badgeMap) { return badgeMap[value] || null; }
function closeModelDropdown() {}
function syncModelChip() {}
function _refreshOpenModelDropdown() {}
async function selectModelFromDropdown(value, provider) {
  _ensureModelOptionInDropdown(value, modelSelect, provider);
  window.__picked=_modelStateForSelect(modelSelect,modelSelect.value);
}

for (const name of [
  '_providerQualifiedPresetRest',
  '_modelPickerOptionIdentity',
  '_modelPickerCanonicalIdentity',
  '_getOptionProviderId',
  '_modelStateForSelect',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_ensureModelOptionInDropdown',
  '_deduplicateModelPickerOptions',
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_isEquivalentConfiguredModelEntry',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

// ---- Catalog renders the RAW preset option under the openrouter group ----
const groups = [{
  provider: 'OpenRouter',
  provider_id: 'openrouter',
  models: [{id: 'openrouter/@preset/deepseek-v4-flash', label: '@preset/deepseek-v4-flash'}],
}];

// 1) Select the raw catalog preset -> outgoing canonical state.
let modelSelect = makeSelect(groups, 'openrouter/@preset/deepseek-v4-flash');
const outgoing = _modelStateForSelect(modelSelect, modelSelect.value);

// 2) Rebuild/reconcile the catalog: a fresh select with the same real row and
//    a leftover synthetic @openrouter: row injected by an earlier failed
//    apply (dataset.custom, orphaned outside the optgroup, like the real
//    _ensureModelOptionInDropdown appendChild path).
modelSelect = makeSelect(groups, '');
const synthetic = makeOption('@openrouter:@preset/deepseek-v4-flash', '@preset/deepseek-v4-flash');
synthetic.dataset.custom = '1';
synthetic.dataset.provider = 'openrouter';
synthetic.dataset.model = '@preset/deepseek-v4-flash';
modelSelect.appendChild(synthetic);

// Reverse lookup of the persisted canonical state must hit the REAL catalog row.
const resolved = _findModelInDropdown('@preset/deepseek-v4-flash', modelSelect, 'openrouter');
// Dedup must prefer the real row over the synthetic one: exactly one option
// remains and it is the catalog row.
const dedupRemoved = _deduplicateModelPickerOptions(modelSelect, resolved || '');
const remainingOptions = modelSelect.options.map(o => o.value);
// Applying the persisted state must reselect the real row.
const applied = _ensureModelOptionInDropdown('@preset/deepseek-v4-flash', modelSelect, 'openrouter');
const appliedState = _modelStateForSelect(modelSelect, modelSelect.value);

// 3) Rich picker: the real row must be active and carry the Selected badge.
renderModelDropdown();
const realRow = findInTree(dropdown, node =>
  String(node._innerHTML || '').includes('openrouter/@preset/deepseek-v4-flash'));
const badgeHtml = realRow ? realRow._innerHTML : '';
const active = realRow && realRow.classList && realRow.classList.contains('active');
// The synthetic orphan must NOT appear as a rendered row.
const synthRow = findInTree(dropdown, node =>
  String(node._innerHTML || '').includes('@openrouter:@preset/deepseek-v4-flash'));

// 4) Vendor-prefixed control: kilo/minimax stays untouched end-to-end.
const kiloGroup = {provider: 'Kilo/MiniMax', provider_id: 'kilo/minimax', models: [{id: 'kilo/minimax/minimax-m3', label: 'kilo/minimax/minimax-m3'}]};
const kiloSelect = makeSelect([kiloGroup], 'kilo/minimax/minimax-m3');
const kiloOutgoing = _modelStateForSelect(kiloSelect, kiloSelect.value);
const kiloResolved = _findModelInDropdown('kilo/minimax/minimax-m3', kiloSelect, 'kilo/minimax');

process.stdout.write(JSON.stringify({
  outgoing,
  resolved,
  dedupRemoved,
  remainingOptions,
  applied,
  appliedState,
  active,
  selectedBadge: badgeHtml.includes('model-opt-badge--selected'),
  synthRowRendered: !!synthRow,
  kiloOutgoing,
  kiloResolved,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_openrouter_preset_round_trip_uses_one_semantic_identity():
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", _OPENROUTER_ROUNDTRIP_DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    # Outgoing stays canonical: @preset/<name> + openrouter, never the raw value.
    assert payload["outgoing"] == {
        "model": "@preset/deepseek-v4-flash",
        "model_provider": "openrouter",
    }
    # Reverse lookup of the persisted state hits the REAL catalog row.
    assert payload["resolved"] == "openrouter/@preset/deepseek-v4-flash"
    # Dedup collapses the synthetic duplicate; one option remains (the real row).
    assert payload["dedupRemoved"] >= 1
    assert payload["remainingOptions"] == ["openrouter/@preset/deepseek-v4-flash"]
    # Reconcile reselects the real row; outgoing state stays canonical.
    assert payload["applied"] == "openrouter/@preset/deepseek-v4-flash"
    assert payload["appliedState"] == {
        "model": "@preset/deepseek-v4-flash",
        "model_provider": "openrouter",
    }
    # The real row is active and carries the Selected badge; the synthetic
    # orphan is gone from the rendered picker.
    assert payload["active"] is True
    assert payload["selectedBadge"] is True
    assert payload["synthRowRendered"] is False
    # Vendor-prefixed control: kilo/minimax is never stripped.
    assert payload["kiloOutgoing"] == {
        "model": "kilo/minimax/minimax-m3",
        "model_provider": "kilo/minimax",
    }
    assert payload["kiloResolved"] == "kilo/minimax/minimax-m3">>>> upstream/master


_RENDERED_CLICK_DRIVER = r"""

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

function extractConst(name) {
  const re = new RegExp('const\\s+' + name + '\\s*=');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found as const');
  const eqIdx = ui.indexOf('=', start + name.length);
  let i = ui.indexOf('{', eqIdx);
  if (i < 0) throw new Error(name + ' arrow body not found');
  let depth = 1;
  i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  if (ui[i] === ';') i++;
  return ui.slice(start, i);
}

function makeClassList(initial) {
  const set = new Set(initial || []);
  return {
    _set: set,
    add(cls) { set.add(cls); },
    remove(cls) { set.delete(cls); },
    contains(cls) { return set.has(cls); },
    toggle(cls, force) {
      if (force === true) { set.add(cls); return true; }
      if (force === false) { set.delete(cls); return false; }
      if (set.has(cls)) { set.delete(cls); return false; }
      set.add(cls);
      return true;
    },
  };
}

function defineClassName(node) {
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList = makeClassList(String(v || '').split(/\s+/).filter(Boolean)); },
  });
}

function makeNode(tag) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    parentElement: null,
    textContent: '',
    value: '',
    tabIndex: 0,
    onclick: null,
    _listeners: {},
    _innerHTML: '',
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        this._ownerSelect.options.push(child);
      }
      return child;
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) { return this._qs ? this._qs[selector] || null : null; },
    setAttribute(name, value) { this[name] = value; },
    focus() { this._focused = true; },
  };
  node.classList = makeClassList();
  defineClassName(node);
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v || '');
      this.children = [];
      this._qs = {};
      if (this.tagName === 'DIV' && this._innerHTML.includes('model-search-input')) {
        const input = makeNode('input');
        input.className = 'model-search-input';
        const clear = makeNode('button');
        clear.className = 'model-search-clear';
        this._qs['.model-search-input'] = input;
        this._qs['.model-search-clear'] = clear;
      } else if (this.tagName === 'DIV' && this._innerHTML.includes('model-custom-input')) {
        const input = makeNode('input');
        input.className = 'model-custom-input';
        const btn = makeNode('button');
        btn.className = 'model-custom-btn';
        this._qs['.model-custom-input'] = input;
        this._qs['.model-custom-btn'] = btn;
      }
    },
  });
  return node;
}

function makeOption(value, label, parent) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  return opt;
}

function makeSelect(groups, selectedValue) {
  const sel = { id: 'modelSelect', children: [], options: [], _value: selectedValue || '' };
  Object.defineProperty(sel, 'value', {get(){return sel._value;}, set(v){sel._value=String(v||'');}});
  Object.defineProperty(sel, 'selectedOptions', {get(){const o=sel.options.find(x=>x.value===sel._value);return o?[o]:[];}});
  sel.appendChild=function(option){option.parentElement=null;sel.options.push(option);};
  sel.querySelectorAll=function(){return [];};
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) og.appendChild(makeOption(model.id, model.label || model.id, og));
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
}

function snapshot(dd) {
  // Recurse into collapsible group bodies (#4279): rows + the show-all expander
  // now live inside `.model-group-body` wrappers rather than as direct children
  // of the dropdown, so a flat children map would miss them.
  const out = [];
  const walk = (node) => {
    for (const child of (node.children || [])) {
      out.push({
        className: child.className,
        textContent: child.textContent,
        html: child._innerHTML || '',
      });
      if (child.children && child.children.length) walk(child);
    }
  };
  walk(dd);
  return out;
}

// Find a node anywhere in the dropdown subtree whose innerHTML matches.
function findInTree(dd, pred) {
  const stack = [...(dd.children || [])];
  while (stack.length) {
    const n = stack.shift();
    if (pred(n)) return n;
    if (n.children && n.children.length) stack.push(...n.children);
  }
  return null;
}

const payload = JSON.parse(process.argv[3]);
const dropdown = makeNode('div');
dropdown.classList.add('open');
const modelSelect = makeSelect(payload.groups, payload.selectedValue || payload.groups[0].models[0].id);

function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'modelSelect') return modelSelect;
  return null;
}
const window = { _configuredModelBadges: payload.configuredBadges || {} };
const document = { createElement(tag) { return makeNode(tag); } };
const escSource = ui.match(/^const esc=(.*);$/m);
if (!escSource) throw new Error('production esc helper not found');
const esc = eval(escSource[1]);
function t(key, ...args) {
  if (key === 'model_show_all_models') return `Show all ${args[0]} models`;
  return key;
}
function li() { return 'x'; }
function getModelLabel(v) { return (payload.labels || {})[v] || String(v || ''); }
function _providerFromModelValue(v) {
  const value = String(v || '');
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
}
function _normalizeConfiguredModelKey(v) { return String(v || '').toLowerCase(); }
function _getConfiguredModelBadge(value, badgeMap) { return badgeMap[value] || null; }
function closeModelDropdown() {}
function syncModelChip() {}
function _refreshOpenModelDropdown() {}
function _deduplicateModelPickerOptions() { return 0; }
async function selectModelFromDropdown(value, provider) {
  _ensureModelOptionInDropdown(value, modelSelect, provider);
  window.__picked=_modelStateForSelect(modelSelect,modelSelect.value);
}

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_isEquivalentConfiguredModelEntry',
  '_getOptionProviderId',
  '_modelStateForSelect',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_ensureModelOptionInDropdown',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

const initialSelection=_modelStateForSelect(modelSelect,modelSelect.value);
renderModelDropdown();
const backupRow=findInTree(dropdown,node=>String(node._innerHTML||'').includes('<span class="model-opt-id">@custom:backup:model-a</span>'));
if(!backupRow||typeof backupRow.onclick!=='function') throw new Error('backup row not rendered');
backupRow.onclick();
const backupPicked=window.__picked;
const catalogRow=findInTree(dropdown,node=>String(node._innerHTML||'').includes('<span class="model-opt-id">gpt-6-sol</span>'));
if(!catalogRow||typeof catalogRow.onclick!=='function') throw new Error('catalog row not rendered');
catalogRow.onclick();
const catalogPicked=window.__picked;
process.stdout.write(JSON.stringify({
  initialSelection,
  backupPicked,
  catalogPicked,
  rows:snapshot(dropdown).filter(row=>String(row.className||'').split(/\s+/).includes('model-opt')).map(row=>row.html),
  options:modelSelect.options.map(o=>({value:o.value,provider:_getOptionProviderId(o)})),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_configured_picker_keeps_friendly_title_and_exact_selection_routing(tmp_path):
    driver = tmp_path / "rendered_click_driver.js"
    driver.write_text(_RENDERED_CLICK_DRIVER, encoding="utf-8")
    payload = {
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": "gpt-6-sol", "label": "R&D <safe>"}],
            },
            {
                "provider": "Primary",
                "provider_id": "custom:primary",
                "models": [{"id": "model-a", "label": "Model A"}],
            }
        ],
        "configuredBadges": {
            "gpt-6-sol": {"role": "primary", "label": "Primary", "provider": "openai"},
            "@custom:backup:model-a": {
                "role": "fallback",
                "label": "Fallback 1",
                "provider": "custom:backup",
            }
        },
        "labels": {"@custom:backup:model-a": "model-a"},
        "selectedValue": "model-a",
    }
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)

    assert actual["initialSelection"] == {
        "model": "model-a",
        "model_provider": "custom:primary",
    }
    catalog_row = next(
        row for row in actual["rows"]
        if '<span class="model-opt-id">gpt-6-sol</span>' in row
    )
    assert '<span class="model-opt-name">R&amp;D &lt;safe&gt;</span>' in catalog_row, (
        "configured catalog title was not escaped exactly once"
    )
    assert '&amp;amp;' not in catalog_row
    assert '<span class="model-opt-badge model-opt-badge--primary">gpt-6-sol (openai)</span>' in catalog_row
    assert actual["catalogPicked"] == {
        "model": "gpt-6-sol",
        "model_provider": "openai",
    }
    fallback_row = next(
        row for row in actual["rows"]
        if '<span class="model-opt-id">@custom:backup:model-a</span>' in row
    )
    assert '<span class="model-opt-name">model-a</span>' in fallback_row
    assert (
        '<span class="model-opt-badge model-opt-badge--fallback">'
        '@custom:backup:model-a (backup)</span>' in fallback_row
    )
    assert actual["backupPicked"] == {
        "model": "model-a",
        "model_provider": "custom:backup",
    }
    assert actual["options"][-1] == {
        "value": "@custom:backup:model-a",
        "provider": "custom:backup",
    }


# ── same-normalized matrix ────────────────────────────────────────────────────
# A badge-owned @commandcode:model-a row must be equivalent only to
# same-provider candidates; bare, slash-prefixed, and at-prefixed aliases from
# otherprovider must not be collapsed into it (#7290 CR re-gate).

_SAME_NORM_MATRIX_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[1], 'utf8');

function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unterminated: ' + name);
}

eval([
  '_normalizeConfiguredModelKey',
  '_getOptionProviderId',
  '_isEquivalentConfiguredModelEntry',
].map(name => extractFunction(ui, name)).join('\n'));

// One existing row: badge-owned @commandcode:model-a
const entries = [{
  value: '@commandcode:model-a',
  providerId: '',
  badge: { provider: 'commandcode', label: 'CommandCode A' },
}];

function check(modelId, badgeProvider) {
  const badge = { provider: badgeProvider, label: 'X' };
  return _isEquivalentConfiguredModelEntry(modelId, badge, entries);
}

const results = {
  // same provider — must be equivalent
  same_at:        check('@commandcode:model-a', 'commandcode'),
  same_slash:     check('commandcode/model-a', 'commandcode'),
  same_bare:      check('model-a', 'commandcode'),
  // otherprovider — must NOT be equivalent
  other_bare:     check('model-a', 'otherprovider'),
  other_slash:    check('otherprovider/model-a', 'otherprovider'),
  other_at:       check('@otherprovider:model-a', 'otherprovider'),
};
process.stdout.write(JSON.stringify(results));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_normalized_badge_row_is_equivalent_only_to_same_provider():
    """@commandcode:model-a row must not suppress otherprovider/model-a aliases."""
    result = subprocess.run(
        [NODE, "-e", _SAME_NORM_MATRIX_DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    r = result.stdout.strip()
    payload = __import__("json").loads(r)

    # same-provider candidates are equivalent (correct suppression)
    assert payload["same_at"], "same-provider @commandcode:model-a must be equivalent"
    assert payload["same_slash"], "commandcode/model-a must be equivalent"
    assert payload["same_bare"], "bare model-a under commandcode must be equivalent"

    # otherprovider aliases must NOT be collapsed
    assert not payload["other_bare"], (
        "bare model-a under otherprovider must not be equivalent to @commandcode:model-a"
    )
    assert not payload["other_slash"], (
        "otherprovider/model-a must not be equivalent to @commandcode:model-a"
    )
    assert not payload["other_at"], (
        "@otherprovider:model-a must not be equivalent to @commandcode:model-a"
    )


# ── composed two-provider renderModelDropdown dedup control ───────────────────
# After _ensureModelOptionInDropdown adds a missing-catalog fallback for
# @custom:backup:model-a alongside an existing @custom:primary:model-a,
# renderModelDropdown must produce exactly one configured row per provider.

_TWO_PROVIDER_DEDUP_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(process.argv[3]);

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

function makeNode(tag) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [], dataset: {}, style: {}, parentElement: null,
    textContent: '', value: '', tabIndex: 0, onclick: null,
    _listeners: {}, _innerHTML: '',
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        this._ownerSelect.options.push(child);
      }
      return child;
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(sel) { return this._qs ? this._qs[sel] || null : null; },
    setAttribute(name, value) { this[name] = value; },
    focus() {},
  };
  const set = new Set();
  node.classList = {
    _set: set,
    add(c) { set.add(c); },
    remove(c) { set.delete(c); },
    contains(c) { return set.has(c); },
    toggle(c, f) {
      if (f === true) { set.add(c); return true; }
      if (f === false) { set.delete(c); return false; }
      if (set.has(c)) { set.delete(c); return false; }
      set.add(c); return true;
    },
  };
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList._set.clear(); String(v||'').split(/\s+/).filter(Boolean).forEach(c=>node.classList._set.add(c)); },
  });
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v || ''); this.children = []; this._qs = {};
      if (this.tagName === 'DIV' && this._innerHTML.includes('model-search-input')) {
        const inp = makeNode('input'); inp.className = 'model-search-input';
        const clr = makeNode('button'); clr.className = 'model-search-clear';
        this._qs['.model-search-input'] = inp; this._qs['.model-search-clear'] = clr;
      } else if (this.tagName === 'DIV' && this._innerHTML.includes('model-custom-input')) {
        const inp = makeNode('input'); inp.className = 'model-custom-input';
        const btn = makeNode('button'); btn.className = 'model-custom-btn';
        this._qs['.model-custom-input'] = inp; this._qs['.model-custom-btn'] = btn;
      }
    },
  });
  return node;
}

function makeSelect(groups, selectedValue) {
  const sel = { id: 'modelSelect', children: [], options: [], _value: selectedValue || '' };
  Object.defineProperty(sel, 'value', {get(){return sel._value;},set(v){sel._value=String(v||'');}});
  Object.defineProperty(sel, 'selectedOptions', {get(){const o=sel.options.find(x=>x.value===sel._value);return o?[o]:[];}});
  sel.appendChild=function(opt){opt.parentElement=null;sel.options.push(opt);};
  sel.querySelectorAll=function(){return [];};
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    for (const model of group.models || []) {
      const opt = makeNode('option');
      opt.value = model.id; opt.textContent = model.label || model.id; opt.parentElement = og;
      og.appendChild(opt);  // _ownerSelect.options.push already called inside appendChild
    }
    sel.children.push(og);
    // Do NOT push og.children again: appendChild already pushed each option via _ownerSelect hook
  }
  return sel;
}

const dropdown = makeNode('div');
dropdown.classList.add('open');
const modelSelect = makeSelect(payload.groups, payload.selectedValue);

function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'modelSelect') return modelSelect;
  return null;
}
const window = { _configuredModelBadges: payload.configuredBadges || {} };
const document = { createElement(tag) { return makeNode(tag); } };
function esc(v) { return String(v || ''); }
function t(key,...args) { if(key==='model_show_all_models') return `Show all ${args[0]} models`; return key; }
function li() { return 'x'; }
function getModelLabel(v) { return String(v || ''); }
function _normalizeConfiguredModelKey(v) { return String(v||'').toLowerCase(); }
function _getConfiguredModelBadge(value, badgeMap) { return (badgeMap||{})[value] || null; }
function closeModelDropdown() {}
function syncModelChip() {}
function _refreshOpenModelDropdown() {}
function _deduplicateModelPickerOptions() { return 0; }
async function selectModelFromDropdown(value, provider) {
  _ensureModelOptionInDropdown(value, modelSelect, provider);
  window.__picked = _modelStateForSelect(modelSelect, modelSelect.value);
}

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_providerFromModelValue',
  '_modelPickerOptionIdentity',
  '_isEquivalentConfiguredModelEntry',
  '_getOptionProviderId',
  '_modelStateForSelect',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_ensureModelOptionInDropdown',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

// Ensure the backup fallback is present before rendering
_ensureModelOptionInDropdown('@custom:backup:model-a', modelSelect, 'custom:backup');
renderModelDropdown();

// The canonical state is modelSelect.options — renderModelDropdown reads from it
// and _isEquivalentConfiguredModelEntry decides which configured rows to add.
// Both providers must survive: model-a (custom:primary) and
// @custom:backup:model-a (custom:backup).
const opts = modelSelect.options.map(o => ({
  value: o.value,
  provider: _getOptionProviderId(o),
}));
process.stdout.write(JSON.stringify({ opts }));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_two_provider_composed_dedup_renders_exactly_one_row_per_provider(tmp_path):
    """_ensureModelOptionInDropdown + renderModelDropdown = exactly 1 row per provider."""
    driver = tmp_path / "two_provider_dedup_driver.js"
    driver.write_text(_TWO_PROVIDER_DEDUP_DRIVER, encoding="utf-8")
    payload = {
        "groups": [
            {
                "provider": "Primary",
                "provider_id": "custom:primary",
                "models": [{"id": "model-a", "label": "Model A"}],
            }
        ],
        "configuredBadges": {
            "@custom:backup:model-a": {
                "role": "fallback",
                "label": "Fallback A",
                "provider": "custom:backup",
            }
        },
        "selectedValue": "model-a",
    }
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS), __import__("json").dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    actual = __import__("json").loads(result.stdout)

    opts = actual.get("opts", [])
    # model-a (primary) and @custom:backup:model-a (backup) must each appear once
    primary_opts = [o for o in opts if o["value"] == "model-a"]
    backup_opts  = [o for o in opts if o["value"] == "@custom:backup:model-a"]
    assert len(primary_opts) == 1, (
        f"expected exactly 1 primary option, got {len(primary_opts)}: {primary_opts}"
    )
    assert len(backup_opts) == 1, (
        f"expected exactly 1 backup option, got {len(backup_opts)}: {backup_opts}"
    )
    if backup_opts:
        assert backup_opts[0]["provider"] == "custom:backup", (
            "backup option must carry custom:backup provider identity"
        )
