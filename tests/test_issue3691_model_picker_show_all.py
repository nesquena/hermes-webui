"""Regression tests for #3691: provider-agnostic model-picker overflow groups."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import pytest

import api.config as config


REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class _FakeResponse:
    def __init__(self, payload: dict):
        self._buf = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._buf


@pytest.fixture(autouse=True)
def _clear_models_cache():
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _openrouter_group() -> dict:
    return next(g for g in config.get_available_models()["groups"] if g["provider_id"] == "openrouter")


def test_populate_model_dropdown_persists_extra_models_for_picker_runtime():
    assert "dataset.extraModels=JSON.stringify(g.extra_models)" in UI_JS, (
        "populateModelDropdown() must persist extra_models onto the optgroup so "
        "renderModelDropdown() can search hidden overflow models before expansion."
    )


def test_native_model_selectors_include_overflow_extra_models():
    """The non-picker native <select> model selectors (Settings / Cron / Profile /
    Auxiliary) must include g.extra_models, not just g.models — otherwise the
    server-side overflow split (#3691) silently hides every model beyond the first
    15 for any large provider in those selectors."""
    assert PANELS_JS.count("extra_models") >= 4, (
        "Settings/Cron/Profile/Auxiliary model selectors must each include g.extra_models "
        "so large-provider catalogs aren't truncated to the visible-15 picker cap."
    )
    assert "[...(g.models||[]),...(g.extra_models||[])]" in PANELS_JS, (
        "The composer-mirroring native selector must concat models + extra_models."
    )


def test_show_all_row_uses_i18n_key():
    assert "t('model_show_all_models',hiddenCount)" in UI_JS, (
        "The synthetic overflow row must use an i18n key instead of hardcoded English."
    )
    assert I18N_JS.count("model_show_all_models:") >= 10, (
        "model_show_all_models should be defined across the shipped locale blocks."
    )
    assert "Mostrar todos los {0} modelos" in I18N_JS
    assert "Afficher tous les {0} modèles" in I18N_JS


def test_openrouter_overflow_preserves_hidden_tail(monkeypatch):
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"},
            "providers": {"openrouter": {"api_key": "sk-or-test-key"}},
        },
        raising=False,
    )
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.fetch_openrouter_models = lambda: [
        ("anthropic/claude-sonnet-4.6", ""),
        ("openai/gpt-4o", ""),
    ]
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)

    payload = {
        "data": [
            {
                "id": f"vendor{i}/overflow-{i}:free",
                "name": f"Overflow {i}",
                "supported_parameters": [],
                "pricing": {"prompt": "0", "completion": "0"},
            }
            for i in range(40)
        ]
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(payload))

    group = _openrouter_group()
    total = len(group["models"]) + len(group.get("extra_models", []))
    capped_total = 2 + config._OPENROUTER_FREE_TIER_AUGMENT_CAP

    assert len(group["models"]) == config._MODEL_PICKER_VISIBLE_TARGET
    assert total == capped_total, "OpenRouter overflow models must move into extra_models within the capped augmentation budget."
    assert any(m["id"] == "vendor29/overflow-29:free" for m in group.get("extra_models", [])), (
        "The last capped free-tier model should land in extra_models once the visible picker cap is reached."
    )
    assert all(m["id"] != "vendor30/overflow-30:free" for bucket in ("models", "extra_models") for m in group.get(bucket, [])), (
        "Free-tier augmentation must stop at the configured cap instead of continuing through the whole live payload."
    )


def test_deduplicate_model_ids_includes_extra_models():
    groups = [
        {
            "provider": "Alpha",
            "provider_id": "alpha",
            "models": [{"id": "shared/model", "label": "Shared Model"}],
            "extra_models": [{"id": "alpha/only-extra", "label": "Alpha Extra"}],
        },
        {
            "provider": "Beta",
            "provider_id": "beta",
            "models": [{"id": "beta/visible", "label": "Beta Visible"}],
            "extra_models": [{"id": "shared/model", "label": "Shared Model"}],
        },
    ]

    config._deduplicate_model_ids(groups)

    assert groups[0]["models"][0]["id"] == "shared/model"
    assert groups[1]["extra_models"][0]["id"] == "@beta:shared/model"
    assert groups[1]["extra_models"][0]["label"] == "Shared Model (Beta)"


def test_openrouter_free_tier_selection_stays_visible_when_selected_id_is_bare():
    ordered = [
        {"id": f"@openrouter:vendor/model-{idx}", "label": f"Model {idx}"}
        for idx in range(config._MODEL_PICKER_VISIBLE_TARGET)
    ]
    ordered.append({"id": "@openrouter:vendor/selected-model:free", "label": "Selected Free"})

    visible, extra = config._split_picker_overflow_models(
        ordered,
        selected_model_id="vendor/selected-model:free",
        provider_id="openrouter",
        threshold=config._MODEL_PICKER_OVERFLOW_THRESHOLD,
        target=config._MODEL_PICKER_VISIBLE_TARGET,
    )

    assert any(m["id"] == "@openrouter:vendor/selected-model:free" for m in visible), (
        "A bare OpenRouter :free selection must stay visible when the selected model is in overflow."
    )
    assert all(m["id"] != "@openrouter:vendor/selected-model:free" for m in extra)


_DROPDOWN_DRIVER = r"""
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
  const sel = { id: 'modelSelect', children: [], options: [], value: selectedValue || '' };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) {
      og.appendChild(makeOption(model.id, model.label || model.id, og));
    }
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
function selectModelFromDropdown() {}

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

renderModelDropdown();
const initial = snapshot(dropdown);
// The show-all expander now lives inside a `.model-group-body` wrapper (#4279),
// so search the whole subtree rather than only direct children.
const initialShowAllRow = findInTree(dropdown, node => String(node._innerHTML || '').includes('Show all'));
const searchInput = dropdown.children[1].querySelector('.model-search-input');
searchInput.value = payload.searchTerm;
searchInput._listeners.input();
const searched = snapshot(dropdown);
initialShowAllRow.onclick({ stopPropagation() {} });
const searchInputAfterExpand = dropdown.children[1].querySelector('.model-search-input');
searchInputAfterExpand.value = '';
searchInputAfterExpand._listeners.input();
const expanded = snapshot(dropdown);

process.stdout.write(JSON.stringify({
  initial,
  searched,
  expanded,
  optionCountAfterExpand: modelSelect.children[0].children.length,
  hiddenDatasetAfterExpand: modelSelect.children[0].dataset.extraModels || '',
}));
"""

_INPLACE_DRIVER = r"""
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

// Extended DOM globals to enable in-place expansion path
const CSS = { escape: s => String(s || '').replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
const requestAnimationFrame = fn => { fn(); return 0; };

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
    insertBefore(newChild, refChild) {
      newChild.parentElement = this;
      const idx = refChild ? this.children.indexOf(refChild) : -1;
      if (idx >= 0) {
        this.children.splice(idx, 0, newChild);
      } else {
        this.children.push(newChild);
      }
      return newChild;
    },
    remove() {
      if (this.parentElement) {
        const idx = this.parentElement.children.indexOf(this);
        if (idx >= 0) this.parentElement.children.splice(idx, 1);
      }
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) {
      // Try the _qs cache first
      if (this._qs && this._qs[selector]) return this._qs[selector];
      // Handle attribute selectors and descendant selectors
      return querySelectorAllImpl(this, selector)[0] || null;
    },
    querySelectorAll(selector) {
      return querySelectorAllImpl(this, selector);
    },
    setAttribute(name, value) { this[name] = value; },
    focus() { this._focused = true; },
  };
  Object.defineProperty(node, 'offsetTop', {
    value: 0,
  });
  Object.defineProperty(node, 'scrollTop', {
    get() { return this._scrollTop || 0; },
    set(v) { this._scrollTop = v; },
  });
  Object.defineProperty(node, 'previousElementSibling', {
    get() {
      if (!this.parentElement) return null;
      const idx = this.parentElement.children.indexOf(this);
      return idx > 0 ? this.parentElement.children[idx - 1] : null;
    },
  });
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

function querySelectorAllImpl(node, selector) {
  const results = [];
  const stack = [node];

  while (stack.length) {
    const n = stack.shift();
    if (n.children && n.children.length) {
      stack.push(...n.children);
    }

    // Simple class selector: .foo
    if (selector.startsWith('.') && !selector.includes('[') && !selector.includes(' ')) {
      const className = selector.slice(1);
      if (n.className && n.className.includes(className)) {
        results.push(n);
      }
    }
    // Attribute selector: .foo[data-bar="baz"]
    else if (selector.includes('[') && !selector.includes(' ')) {
      const match = selector.match(/^\.([^\[]+)\[data-([^\]=]+)="([^\]]+)"\]$/);
      if (match) {
        const [, className, dataKey, dataVal] = match;
        if (n.className && n.className.includes(className) &&
            n.dataset && n.dataset[dataKey] === dataVal) {
          results.push(n);
        }
      }
    }
    // Descendant selector: .foo .bar
    else if (selector.includes(' ')) {
      const parts = selector.split(' ').filter(Boolean);
      if (parts.length === 2) {
        const [parentSel, childSel] = parts;
        // Find all ancestors matching parentSel
        let parent = n.parentElement;
        let hasParent = false;
        while (parent) {
          if (isMatch(parent, parentSel)) {
            hasParent = true;
            break;
          }
          parent = parent.parentElement;
        }
        // If we found a matching ancestor, check if this node matches childSel
        if (hasParent && isMatch(n, childSel)) {
          results.push(n);
        }
      }
    }
  }

  return results;
}

function isMatch(node, selector) {
  // Simple class selector: .foo
  if (selector.startsWith('.') && !selector.includes('[')) {
    const className = selector.slice(1);
    return node.className && node.className.includes(className);
  }
  // Attribute selector: .foo[data-bar="baz"]
  if (selector.includes('[')) {
    const match = selector.match(/^\.([^\[]+)\[data-([^\]=]+)="([^\]]+)"\]$/);
    if (match) {
      const [, className, dataKey, dataVal] = match;
      return node.className && node.className.includes(className) &&
             node.dataset && node.dataset[dataKey] === dataVal;
    }
  }
  return false;
}

function makeOption(value, label, parent) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  return opt;
}

function makeSelect(groups, selectedValue) {
  const sel = { id: 'modelSelect', children: [], options: [], value: selectedValue || '' };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) {
      og.appendChild(makeOption(model.id, model.label || model.id, og));
    }
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
function selectModelFromDropdown() {}

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

eval(extractConst('_expandOverflowGroup'));

renderModelDropdown();
const initial = snapshot(dropdown);
const initialShowAllRow = findInTree(dropdown, node => String(node._innerHTML || '').includes('Show all'));
// Click show-all FIRST (before any search) so the in-place path runs on a
// fresh DOM with .model-opt-more still present. Searching first would trigger
// a full re-render that removes .model-opt-more, making the stale onclick
// reference fall into the full-rerender fallback instead of in-place.
initialShowAllRow.onclick({ stopPropagation() {} });
const expanded = snapshot(dropdown);
// Now type a search, then clear it, to verify the hiddenByDefault sync
// keeps the group fully expanded through the search→clear cycle.
const searchInput = dropdown.children[1].querySelector('.model-search-input');
searchInput.value = payload.searchTerm;
searchInput._listeners.input();
const searched = snapshot(dropdown);
searchInput.value = '';
searchInput._listeners.input();
const cleared = snapshot(dropdown);

// After clearing, measure the rendered group body — not the hidden <select>.
// _appendOverflowOptionsToGroup appends to the <select> unconditionally, so
// counting <option> elements there is always 4 whether or not the
// hiddenByDefault/hiddenCount sync is present. The regression the fix prevents
// is the rendered dropdown snapping back to the capped view + a fresh "Show all"
// expander, so we must check the live DOM rows inside the .model-group-body.
const groupWrapper = querySelectorAllImpl(dropdown, '.model-group-body[data-group="openrouter"]')[0] || null;
const clearedRenderedModelCount = groupWrapper ? querySelectorAllImpl(groupWrapper, '.model-opt').length : -1;
const clearedHasMoreButton = groupWrapper ? querySelectorAllImpl(groupWrapper, '.model-opt-more').length > 0 : false;

process.stdout.write(JSON.stringify({
  inPlacePath: true,
  initialShowAll: initial.some(item => String(item.html || '').includes('Show all')),
  expandedHasShowAll: expanded.some(item => String(item.html || '').includes('Show all')),
  clearedHasShowAll: cleared.some(item => String(item.html || '').includes('Show all')),
  clearedRenderedModelCount,
  clearedHasMoreButton,
}));
"""

_INPLACE_ENDPOINT_ERROR_DRIVER = r"""
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

const CSS = { escape: s => String(s || '').replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
const requestAnimationFrame = fn => { fn(); return 0; };

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
  const sel = { id: 'modelSelect', children: [], options: [], value: selectedValue || '' };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    if (group.modelsEndpointError) og.dataset.modelsEndpointError = JSON.stringify(group.modelsEndpointError);
    for (const model of group.models || []) {
      og.appendChild(makeOption(model.id, model.label || model.id, og));
    }
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
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
function selectModelFromDropdown() {}

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

renderModelDropdown();

// Target the errored group's wrapper specifically by data-group attribute.
// A plain walk() that overwrites on every .model-group-body ends on the last
// wrapper in DOM order (the selected/open Anthropic group), giving a false
// "open" result regardless of whether _hasEndpointError fired.
let errWrap = null;
const walk = (node) => {
  for (const child of (node.children || [])) {
    if (child.className && child.className.includes('model-group-body') &&
        child.dataset && child.dataset.group === 'openrouter') {
      errWrap = child;
    }
    if (child.children && child.children.length) walk(child);
  }
};
walk(dropdown);

process.stdout.write(JSON.stringify({
  foundWrapper: !!errWrap,
  groupRendersOpen: !!errWrap && errWrap.style.display !== 'none',
}));
"""

_INPLACE_PREEXISTING_DRIVER = r"""
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

const CSS = { escape: s => String(s || '').replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
const requestAnimationFrame = fn => { fn(); return 0; };

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
    insertBefore(newChild, refChild) {
      newChild.parentElement = this;
      const idx = refChild ? this.children.indexOf(refChild) : -1;
      if (idx >= 0) {
        this.children.splice(idx, 0, newChild);
      } else {
        this.children.push(newChild);
      }
      return newChild;
    },
    remove() {
      if (this.parentElement) {
        const idx = this.parentElement.children.indexOf(this);
        if (idx >= 0) this.parentElement.children.splice(idx, 1);
      }
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) {
      // Try the _qs cache first
      if (this._qs && this._qs[selector]) return this._qs[selector];
      // Handle attribute selectors and descendant selectors
      return querySelectorAllImpl(this, selector)[0] || null;
    },
    querySelectorAll(selector) {
      return querySelectorAllImpl(this, selector);
    },
    setAttribute(name, value) { this[name] = value; },
    focus() { this._focused = true; },
  };
  Object.defineProperty(node, 'offsetTop', {
    value: 0,
  });
  Object.defineProperty(node, 'scrollTop', {
    get() { return this._scrollTop || 0; },
    set(v) { this._scrollTop = v; },
  });
  Object.defineProperty(node, 'previousElementSibling', {
    get() {
      if (!this.parentElement) return null;
      const idx = this.parentElement.children.indexOf(this);
      return idx > 0 ? this.parentElement.children[idx - 1] : null;
    },
  });
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

function querySelectorAllImpl(node, selector) {
  const results = [];
  const stack = [node];

  while (stack.length) {
    const n = stack.shift();
    if (n.children && n.children.length) {
      stack.push(...n.children);
    }

    if (selector.startsWith('.') && !selector.includes('[') && !selector.includes(' ')) {
      const className = selector.slice(1);
      if (n.className && n.className.includes(className)) {
        results.push(n);
      }
    }
    else if (selector.includes('[') && !selector.includes(' ')) {
      const match = selector.match(/^\.([^\[]+)\[data-([^\]=]+)="([^\]]+)"\]$/);
      if (match) {
        const [, className, dataKey, dataVal] = match;
        if (n.className && n.className.includes(className) &&
            n.dataset && n.dataset[dataKey] === dataVal) {
          results.push(n);
        }
      }
    }
    else if (selector.includes(' ')) {
      const parts = selector.split(' ').filter(Boolean);
      if (parts.length === 2) {
        const [parentSel, childSel] = parts;
        let parent = n.parentElement;
        let hasParent = false;
        while (parent) {
          if (isMatch(parent, parentSel)) {
            hasParent = true;
            break;
          }
          parent = parent.parentElement;
        }
        if (hasParent && isMatch(n, childSel)) {
          results.push(n);
        }
      }
    }
    // Bare tag-name selector: e.g. 'option'. Required so
    // _appendOverflowOptionsToGroup's querySelectorAll('option') finds
    // pre-injected <option> elements — without this, existingByValue stays
    // empty and the function never returns 0, so the extraModels.length guard
    // is never exercised.
    else if (!selector.startsWith('.') && !selector.includes('[') && !selector.includes(' ')) {
      if (n.tagName && n.tagName === selector.toUpperCase()) {
        results.push(n);
      }
    }
  }

  return results;
}

function isMatch(node, selector) {
  if (selector.startsWith('.') && !selector.includes('[')) {
    const className = selector.slice(1);
    return node.className && node.className.includes(className);
  }
  if (selector.includes('[')) {
    const match = selector.match(/^\.([^\[]+)\[data-([^\]=]+)="([^\]]+)"\]$/);
    if (match) {
      const [, className, dataKey, dataVal] = match;
      return node.className && node.className.includes(className) &&
             node.dataset && node.dataset[dataKey] === dataVal;
    }
  }
  if (!selector.startsWith('.') && !selector.includes('[')) {
    return node.tagName && node.tagName === selector.toUpperCase();
  }
  return false;
}

function makeOption(value, label, parent) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  return opt;
}

function makeSelect(groups, selectedValue) {
  const sel = {
    id: 'modelSelect', tagName: 'SELECT', children: [], options: [], value: selectedValue || '',
    querySelectorAll(selector) { return querySelectorAllImpl(this, selector); },
    querySelector(selector) { return querySelectorAllImpl(this, selector)[0] || null; },
  };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    og.parentNode = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) {
      og.appendChild(makeOption(model.id, model.label || model.id, og));
    }
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

const payload = JSON.parse(process.argv[3]);
const dropdown = makeNode('div');
dropdown.classList.add('open');
const modelSelect = makeSelect(payload.groups, payload.selectedValue || payload.groups[0].models[0].id);

// Pre-inject one overflow model as an <option> in the optgroup to simulate
// _ensureModelOptionInDropdown having already added it. Both overflow models
// are pre-injected so _appendOverflowOptionsToGroup returns 0 new appends,
// exercising the extraModels.length guard (not the return-value guard).
if (payload.preexistingModelIds) {
  const og = modelSelect.children[0];
  for (const mid of payload.preexistingModelIds) {
    const preexisting = payload.groups[0].extra_models.find(m => m.id === mid);
    if (preexisting && og) {
      const opt = makeOption(preexisting.id, preexisting.label || preexisting.id, og);
      og.appendChild(opt);
    }
  }
}

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
function selectModelFromDropdown() {}

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

eval(extractConst('_expandOverflowGroup'));

renderModelDropdown();
const initialShowAllRow = findInTree(dropdown, node => String(node._innerHTML || '').includes('Show all'));
initialShowAllRow.onclick({ stopPropagation() {} });

// Check if preexisting models are now visible - look through all innerHTML or textContent
const idsToFind = new Set(payload.preexistingModelIds || []);
const foundIds = new Set();
let showAllGone = false;
const walk = (node, depth=0) => {
  for (const mid of idsToFind) {
    if (node._innerHTML && node._innerHTML.includes(mid)) {
      foundIds.add(mid);
    }
    if (node.textContent && String(node.textContent).includes(mid)) {
      foundIds.add(mid);
    }
  }
  if (node.children && node.children.length) {
    for (const child of node.children) walk(child, depth+1);
  }
};
walk(dropdown);
const preexistingVisible = foundIds.size === idsToFind.size;
const expanded = findInTree(dropdown, node => String(node._innerHTML || '').includes('Show all'));
showAllGone = !expanded;

process.stdout.write(JSON.stringify({
  preexistingVisible,
  showAllGone,
}));
"""


@pytest.fixture(scope="module")
def _driver_paths(tmp_path_factory):
    driver_dir = tmp_path_factory.mktemp("issue3691_drivers")
    dropdown_path = driver_dir / "driver.js"
    dropdown_path.write_text(_DROPDOWN_DRIVER, encoding="utf-8")
    inplace_path = driver_dir / "driver_inplace.js"
    inplace_path.write_text(_INPLACE_DRIVER, encoding="utf-8")
    endpoint_error_path = driver_dir / "driver_endpoint_error.js"
    endpoint_error_path.write_text(_INPLACE_ENDPOINT_ERROR_DRIVER, encoding="utf-8")
    preexisting_path = driver_dir / "driver_preexisting.js"
    preexisting_path.write_text(_INPLACE_PREEXISTING_DRIVER, encoding="utf-8")
    return {
        "dropdown": str(dropdown_path),
        "inplace": str(inplace_path),
        "endpoint_error": str(endpoint_error_path),
        "preexisting": str(preexisting_path),
    }


@pytest.fixture(scope="module")
def _dropdown_driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("issue3691_dropdown_driver") / "driver.js"
    path.write_text(_DROPDOWN_DRIVER, encoding="utf-8")
    return str(path)


def _run_dropdown_driver(driver_path: str, payload: dict | None = None) -> dict:
    if payload is None:
        payload = {
            "groups": [
                {
                    "provider": "OpenRouter",
                    "provider_id": "openrouter",
                    "models": [
                        {"id": "openrouter/visible-one", "label": "Visible One"},
                        {"id": "openrouter/visible-two", "label": "Visible Two"},
                    ],
                    "extra_models": [
                        {"id": "openrouter/overflow-one", "label": "Overflow One"},
                        {"id": "openrouter/overflow-two", "label": "Overflow Two"},
                    ],
                }
            ],
            "searchTerm": "overflow-two",
        }
    result = subprocess.run(
        [NODE, driver_path, str(REPO / "static" / "ui.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_picker_shows_generic_expander_and_searches_hidden_overflow(_dropdown_driver_path):
    out = _run_dropdown_driver(_dropdown_driver_path)

    initial_html = "\n".join(item["html"] for item in out["initial"])
    searched_html = "\n".join(item["html"] for item in out["searched"])
    expanded_html = "\n".join(item["html"] for item in out["expanded"])

    assert "Show all 2 models" in initial_html, (
        "The picker should render a synthetic show-all row when extra_models are present."
    )
    assert "openrouter/overflow-two" in searched_html, (
        "Filtering must already match hidden overflow models before the group is expanded."
    )
    assert "Show all 2 models" not in expanded_html, (
        "Once expanded, the synthetic row should disappear and the live options should take over."
    )
    assert out["optionCountAfterExpand"] == 4, (
        "Expanding a capped group must append the hidden models into the live optgroup."
    )
    assert out["hiddenDatasetAfterExpand"] == "[]", (
        "After expansion the optgroup should no longer advertise a hidden overflow tail."
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_picker_preserves_backend_decorated_nous_heading_without_double_count(
    _dropdown_driver_path,
):
    payload = {
        "groups": [
            {
                "provider": "Nous (2 of 4)",
                "provider_id": "nous",
                "models": [
                    {"id": "@nous:visible-one", "label": "Visible One"},
                    {"id": "@nous:visible-two", "label": "Visible Two"},
                ],
                "extra_models": [
                    {"id": "@nous:hidden-one", "label": "Hidden One"},
                    {"id": "@nous:hidden-two", "label": "Hidden Two"},
                ],
            }
        ],
        "searchTerm": "",
    }
    out = _run_dropdown_driver(_dropdown_driver_path, payload)

    # The decorated overflow count ("Nous (2 of 4)") belongs on the GROUP HEADING
    # (rendered via textContent), NOT stamped onto every per-row provider chip.
    heading_text = "\n".join(item["textContent"] for item in out["initial"])
    row_html = "\n".join(item["html"] for item in out["initial"])

    assert "Nous (2 of 4) (4)" not in heading_text, (
        "Backend-decorated Nous headings must not get a second frontend count suffix."
    )
    assert "Nous (2 of 4)" in heading_text, (
        "The picker should preserve the backend-crafted Nous heading verbatim when overflow exists."
    )
    # Regression (#3691 row-chip leak): the per-row provider chip must NEVER carry
    # the "(N of M)" overflow count. As of the collapsible-groups UX pass, the
    # per-row provider chip is also suppressed entirely when a row sits under its
    # own provider heading (the heading already names the provider, so repeating it
    # on every row is pure noise) — the chip is kept only for hoisted/search rows.
    assert "(2 of 4)" not in row_html, (
        "The per-row provider chip must not carry the overflow count; it belongs on the heading only."
    )
    assert 'class="model-opt-provider">Nous (2 of 4)<' not in row_html, (
        "A row chip must never show the decorated overflow label."
    )
    # Under its own provider heading, rows carry NO redundant provider chip.
    assert 'class="model-opt-provider"' not in row_html, (
        "Rows under their own provider heading should not repeat the provider chip (de-noise UX)."
    )
    # After expand, the heading must not double-count ("Nous (2 of 4) (4)") — it should
    # strip the backend decoration and show the plain rendered-row count. (#3691)
    expanded_heading_text = "\n".join(item["textContent"] for item in out["expanded"])
    assert "(2 of 4) (4)" not in expanded_heading_text, (
        "Expanded heading must not append a second count onto the decorated overflow label."
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_picker_excludes_configured_hidden_models_from_show_all_count(
    _dropdown_driver_path,
):
    payload = {
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [
                    {"id": "openrouter/visible-one", "label": "Visible One"},
                ],
                "extra_models": [
                    {"id": "openrouter/overflow-one", "label": "Overflow One"},
                    {"id": "openrouter/overflow-two", "label": "Overflow Two"},
                ],
            }
        ],
        "configuredBadges": {
            "openrouter/overflow-two": {
                "label": "Primary",
                "role": "primary",
                "provider": "openrouter",
            }
        },
        "searchTerm": "",
    }
    out = _run_dropdown_driver(_dropdown_driver_path, payload)

    initial_html = "\n".join(item["html"] for item in out["initial"])
    assert "Show all 1 models" in initial_html, (
        "Configured overflow models should be excluded from the provider-group "
        "show-all count once they are lifted into the Configured section."
    )
    assert "Show all 2 models" not in initial_html, (
        "The show-all label must not over-report configured hidden overflow models."
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_inplace_expand_then_search_clear_preserves_expanded_group(_driver_paths):
    """Test that in-place expansion path preserves expanded state through search→clear cycle.

    Verifies the _modelData.hiddenByDefault flip (ui.js:2378-2381) and hiddenCount=0
    sync (ui.js:2383-2385) so the group doesn't snap back to capped view after clearing.
    """
    payload = {
        "groups": [
            # A second group is selected so the openrouter group is NOT the
            # selected group and can only remain expanded via the hiddenByDefault/
            # _prevHasSearch machinery being tested — not via _selectedGroupKey.
            {
                "provider": "Anthropic",
                "provider_id": "anthropic",
                "models": [
                    {"id": "anthropic/claude", "label": "Claude"},
                ],
                "extra_models": [],
            },
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [
                    {"id": "openrouter/visible-one", "label": "Visible One"},
                    {"id": "openrouter/visible-two", "label": "Visible Two"},
                ],
                "extra_models": [
                    {"id": "openrouter/overflow-one", "label": "Overflow One"},
                    {"id": "openrouter/overflow-two", "label": "Overflow Two"},
                ],
            },
        ],
        "selectedValue": "anthropic/claude",
        "searchTerm": "overflow",
    }
    result = subprocess.run(
        [NODE, _driver_paths["inplace"], str(REPO / "static" / "ui.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node inplace driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    out = json.loads(result.stdout)

    assert out["initialShowAll"], "Overflow group should show 'Show all' row initially"
    assert not out["expandedHasShowAll"], "After in-place expansion, 'Show all' row should be gone"
    assert not out["clearedHasShowAll"], "After search→clear, 'Show all' row should still be gone"
    assert not out["clearedHasMoreButton"], (
        "After search→clear, no 'Show all' expander should reappear inside the group body"
    )
    assert out["clearedRenderedModelCount"] == 4, (
        "After search→clear following in-place expansion, all 4 models must remain rendered "
        "in the .model-group-body (2 visible + 2 overflow); a missing hiddenByDefault sync "
        "causes the group to snap back to 2 capped rows"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_inplace_endpoint_error_group_renders_open(_driver_paths):
    """Test that groups with endpoint error render open by default.

    Verifies the _hasEndpointError gate (ui.js:2552-2555) that opens groups when models
    failed to fetch, so the user sees the error hint.
    """
    payload = {
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [
                    {"id": "openrouter/visible-one", "label": "Visible One"},
                ],
                "extra_models": [
                    {"id": "openrouter/overflow-one", "label": "Overflow One"},
                    {"id": "openrouter/overflow-two", "label": "Overflow Two"},
                ],
                "modelsEndpointError": {"message": "fetch failed"},
            },
            # A second group whose model is selected so the openrouter group is NOT
            # the selected group and won't auto-open via _selectedGroupKey matching.
            # Without _hasEndpointError the openrouter wrapper gets display:none.
            {
                "provider": "Anthropic",
                "provider_id": "anthropic",
                "models": [
                    {"id": "anthropic/claude-3-5-sonnet", "label": "Claude 3.5 Sonnet"},
                ],
                "extra_models": [],
            },
        ],
        "selectedValue": "anthropic/claude-3-5-sonnet",
        "searchTerm": "",
    }
    result = subprocess.run(
        [NODE, _driver_paths["endpoint_error"], str(REPO / "static" / "ui.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node endpoint_error driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    out = json.loads(result.stdout)

    assert out["foundWrapper"], (
        "Expected a .model-group-body wrapper to be rendered"
    )
    assert out["groupRendersOpen"], (
        "Groups with endpoint error should render open by default so the user sees the error hint"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_inplace_expand_with_preexisting_options_reveals_them(_driver_paths):
    """Test that in-place expansion reveals pre-existing overflow options.

    Verifies the extraModels.length guard (ui.js:2325) which prevents bailing on 0
    newly-created options when some overflow models already exist as <option>s.
    """
    payload = {
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [
                    {"id": "openrouter/visible-one", "label": "Visible One"},
                ],
                "extra_models": [
                    {"id": "openrouter/overflow-one", "label": "Overflow One"},
                    {"id": "openrouter/overflow-two", "label": "Overflow Two"},
                ],
            }
        ],
        "preexistingModelIds": ["openrouter/overflow-one", "openrouter/overflow-two"],
        "searchTerm": "",
    }
    result = subprocess.run(
        [NODE, _driver_paths["preexisting"], str(REPO / "static" / "ui.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node preexisting driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    out = json.loads(result.stdout)

    assert out["preexistingVisible"], (
        "Pre-existing overflow option should be revealed after in-place expansion"
    )
    assert out["showAllGone"], (
        "After expansion, the 'Show all' row should be gone even when some overflow options pre-existed"
    )
