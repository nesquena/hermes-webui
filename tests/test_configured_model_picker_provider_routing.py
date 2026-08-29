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

renderModelDropdown();
const backupRow=findInTree(dropdown,node=>String(node._innerHTML||'').includes('@custom:backup:model-a'));
if(!backupRow||typeof backupRow.onclick!=='function') throw new Error('backup row not rendered');
backupRow.onclick();
process.stdout.write(JSON.stringify({
  picked:window.__picked,
  options:modelSelect.options.map(o=>({value:o.value,provider:_getOptionProviderId(o)})),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_rendered_missing_fallback_row_click_persists_its_own_provider(tmp_path):
    driver = tmp_path / "rendered_click_driver.js"
    driver.write_text(_RENDERED_CLICK_DRIVER, encoding="utf-8")
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
                "label": "Fallback 1",
                "provider": "custom:backup",
            }
        },
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

    assert actual["picked"] == {
        "model": "model-a",
        "model_provider": "custom:backup",
    }
    assert actual["options"][-1] == {
        "value": "@custom:backup:model-a",
        "provider": "custom:backup",
    }
