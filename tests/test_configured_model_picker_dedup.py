"""Regression coverage for duplicate configured model entries in the picker."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}
eval(extractFunc('_normalizeConfiguredModelKey'));
eval(extractFunc('_isEquivalentConfiguredModelEntry'));
const cases = JSON.parse(process.argv[3]);
const result = cases.map(c => _isEquivalentConfiguredModelEntry(c.modelId, c.badge, c.entries));
process.stdout.write(JSON.stringify(result));
"""


def _equivalent_cases(tmp_path, cases):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_picker_rows_preserve_provider_id_for_equivalence_check():
    """The synthesis loop must compare badge routes against real row providers."""
    ui = UI_JS_PATH.read_text(encoding="utf-8")

    assert "const providerId=child.dataset&&child.dataset.provider?child.dataset.provider:'';" in ui
    assert "providerId,modelsEndpointError,badge:_getConfiguredModelBadge" in ui
    assert "providerId,\n          modelsEndpointError," in ui
    assert "if(_isEquivalentConfiguredModelEntry(modelId,badge,_modelData)) continue;" in ui
    assert "_existingConfiguredKeys" not in ui


def test_named_custom_provider_routing_id_does_not_duplicate_picker_row(tmp_path):
    entries = [{"value": "model-a", "providerId": "custom:example"}]
    results = _equivalent_cases(
        tmp_path,
        [
            {
                "modelId": "@custom:example:model-a",
                "badge": {"provider": "custom:example"},
                "entries": entries,
            },
            {
                "modelId": "model-a",
                "badge": {"provider": "custom:example"},
                "entries": entries,
            },
        ],
    )

    assert results == [True, True]


def test_plain_provider_prefix_slash_model_id_does_not_duplicate_picker_row(tmp_path):
    """#7290: a `provider/model` badge key for a slash-bearing model id
    (e.g. commandcode/deepseek/deepseek-v4-flash) must dedupe against the bare
    model row of the same provider, instead of leaking as a duplicate entry."""
    grouped_row = [{"value": "deepseek/deepseek-v4-flash", "providerId": "commandcode"}]
    # A temporary/custom row synthesized from an ungrouped top-level OPTION
    # (`_ensureModelOptionInDropdown`): renderModelDropdown stores it with
    # providerId:'' and only the badge carries the provider identity, which is
    # exactly the shape that used to leak the duplicate.
    top_level_row = [
        {
            "value": "@commandcode:deepseek/deepseek-v4-flash",
            "providerId": "",
            "badge": {"provider": "commandcode"},
        }
    ]
    badgeless_top_level_row = [
        {
            "value": "deepseek/deepseek-v4-flash",
            "providerId": "",
            "badge": {"provider": "commandcode"},
        }
    ]
    results = _equivalent_cases(
        tmp_path,
        [
            {
                "modelId": "commandcode/deepseek/deepseek-v4-flash",
                "badge": {"provider": "commandcode"},
                "entries": grouped_row,
            },
            {
                "modelId": "@commandcode:deepseek/deepseek-v4-flash",
                "badge": {"provider": "commandcode"},
                "entries": grouped_row,
            },
            {
                "modelId": "commandcode/deepseek/deepseek-v4-flash",
                "badge": {"provider": "commandcode"},
                "entries": top_level_row,
            },
            {
                "modelId": "@commandcode:deepseek/deepseek-v4-flash",
                "badge": {"provider": "commandcode"},
                "entries": top_level_row,
            },
            {
                "modelId": "commandcode/deepseek/deepseek-v4-flash",
                "badge": {"provider": "commandcode"},
                "entries": badgeless_top_level_row,
            },
        ],
    )

    assert results == [True, True, True, True, True]


def test_plain_provider_prefix_top_level_row_other_provider_remains_distinct(tmp_path):
    """#7290 guard: reaching the top-level row's provider through its badge must
    not collapse the same model id from a different provider (#3360 family)."""
    results = _equivalent_cases(
        tmp_path,
        [
            {
                "modelId": "otherprovider/deepseek/deepseek-v4-flash",
                "badge": {"provider": "otherprovider"},
                "entries": [
                    {
                        "value": "@commandcode:deepseek/deepseek-v4-flash",
                        "providerId": "",
                        "badge": {"provider": "commandcode"},
                    }
                ],
            },
            {
                "modelId": "commandcode/deepseek/deepseek-v4-flash",
                "badge": {"provider": "commandcode"},
                "entries": [
                    {
                        "value": "@otherprovider:deepseek/deepseek-v4-flash",
                        "providerId": "",
                        "badge": {"provider": "otherprovider"},
                    }
                ],
            },
        ],
    )

    assert results == [False, False]


def test_plain_provider_prefix_same_model_other_provider_remains_distinct(tmp_path):
    """#7290 guard: the plain `provider/model` routing dedup must NOT collapse
    the same model id from a different provider (#3360 family)."""
    entries = [{"value": "deepseek/deepseek-v4-flash", "providerId": "other-provider"}]
    results = _equivalent_cases(
        tmp_path,
        [
            {
                "modelId": "commandcode/deepseek/deepseek-v4-flash",
                "badge": {"provider": "commandcode"},
                "entries": entries,
            },
        ],
    )
    assert results == [False]


def test_same_model_id_from_another_provider_remains_distinct(tmp_path):
    entries = [{"value": "model-a", "providerId": "custom:primary"}]
    results = _equivalent_cases(
        tmp_path,
        [
            {
                "modelId": "@custom:backup:model-a",
                "badge": {"provider": "custom:backup"},
                "entries": entries,
            },
            {
                "modelId": "model-a",
                "badge": {"provider": "custom:backup"},
                "entries": entries,
            },
        ],
    )

    assert results == [False, False]


_TOP_LEVEL_OPTION_DRIVER = r"""
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
  let depth = 1;
  i = ui.indexOf('{', i);
  if (i < 0) throw new Error(name + ' body not found');
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
  // _ensureModelOptionInDropdown appends a direct child OPTION to the select and
  // renderModelDropdown walks select.children, so keep it in both collections.
  sel.appendChild=function(option){option.parentElement=sel;sel.children.push(option);sel.options.push(option);};
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

function walk(dd) {
  const out = [];
  const stack = [...(dd.children || [])];
  while (stack.length) {
    const n = stack.shift();
    out.push(n);
    if (n.children && n.children.length) stack.push(...n.children);
  }
  return out;
}

const payload = JSON.parse(process.argv[3]);
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
function t(key, ...args) {
  if (key === 'model_show_all_models') return `Show all ${args[0]} models`;
  return key;
}
function li() { return 'x'; }
function getModelLabel(v) { return String(v || ''); }
function closeModelDropdown() {}
function syncModelChip() {}
function _refreshOpenModelDropdown() {}
function _deduplicateModelPickerOptions() { return 0; }

for (const name of [
  '_normalizeConfiguredModelKey',
  '_getConfiguredModelBadge',
  '_providerFromModelValue',
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

_ensureModelOptionInDropdown(payload.requested, modelSelect, payload.requestedProvider);
renderModelDropdown();

const nodes = walk(dropdown);
const rowIds = [];
for (const n of nodes) {
  const html = String(n._innerHTML || '');
  if (typeof n.onclick !== 'function' || !html.includes('model-opt-id')) continue;
  const match = html.match(/class="model-opt-id">([^<]*)</);
  if (match) rowIds.push(match[1]);
}
process.stdout.write(JSON.stringify({
  rowIds,
  selected: modelSelect.value,
  optionValues: modelSelect.options.map((o) => o.value),
  markup: nodes.map((n) => String(n._innerHTML || '')).join('\n'),
}));
"""


def _render_top_level_option(tmp_path, payload):
    driver = tmp_path / "driver_top.js"
    driver.write_text(_TOP_LEVEL_OPTION_DRIVER, encoding="utf-8")
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_temporary_top_level_option_row_does_not_duplicate_configured_entry(tmp_path):
    """#7290 (temporary/custom row path): a top-level OPTION synthesized by
    `_ensureModelOptionInDropdown` is stored with providerId:'' and its provider
    identity only lives on the badge. Rendering the picker afterwards must not
    add the plain `provider/model` configured key as a second row."""
    data = _render_top_level_option(
        tmp_path,
        {
            "groups": [
                {
                    "provider": "OpenRouter",
                    "provider_id": "openrouter",
                    "models": [{"id": "other-model"}],
                }
            ],
            "selectedValue": "@commandcode:deepseek/deepseek-v4-flash",
            "requested": "@commandcode:deepseek/deepseek-v4-flash",
            "requestedProvider": "commandcode",
            "configuredBadges": {
                "@commandcode:deepseek/deepseek-v4-flash": {
                    "provider": "commandcode",
                    "role": "fallback",
                    "label": "Fallback 1",
                },
                "commandcode/deepseek/deepseek-v4-flash": {
                    "provider": "commandcode",
                    "role": "fallback",
                    "label": "Fallback 1",
                },
            },
        },
    )

    model_rows = [row_id for row_id in data["rowIds"] if "deepseek-v4-flash" in row_id]
    assert model_rows == ["@commandcode:deepseek/deepseek-v4-flash"]
    assert "commandcode/deepseek/deepseek-v4-flash" not in data["markup"]
    # The temporary/custom option the user picked survives the re-render.
    assert data["selected"] == "@commandcode:deepseek/deepseek-v4-flash"
    assert "@commandcode:deepseek/deepseek-v4-flash" in data["optionValues"]
