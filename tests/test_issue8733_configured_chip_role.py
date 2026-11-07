"""Regression coverage for #8733: the Configured chip showed the raw routing key.

The Configured section of the composer/Settings model picker rendered its badge
chip from ``badgeKeyMap.get(m.badge)``, so a row whose configured badge matched
an ``@provider:model`` routing spelling showed e.g.
``@CUSTOM:MAGPIE:CODEX/GPT-6.1-SOL (MAGPIE)`` instead of the readable role the
group rows use ("Primary", "Fallback 2").  The raw key has to stay reachable
(the row subtitle and the chip's ``title``), but the chip itself must carry the
role label only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
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
  let i = ui.indexOf('{', start);
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
      const previousParent = child.parentElement;
      if (previousParent && previousParent !== this) {
        previousParent.children = previousParent.children.filter(item => item !== child);
      }
      child.parentElement = this;
      child.parentNode = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        if (!this._ownerSelect.options.includes(child)) this._ownerSelect.options.push(child);
        child._ownerSelect = this._ownerSelect;
      } else if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
        if (!this.options.includes(child)) this.options.push(child);
        child._ownerSelect = this;
      }
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter(item => item !== child);
      const owner = this.tagName === 'OPTGROUP' ? this._ownerSelect : this;
      if (owner && owner.options) owner.options = owner.options.filter(item => item !== child);
      child.parentElement = null;
      child.parentNode = null;
      return child;
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) { return this._qs ? this._qs[selector] || null : null; },
    setAttribute(name, value) { this[name] = value; },
    hasAttribute(name) { return this[name] !== undefined && this[name] !== null; },
    focus() { this._focused = true; },
    click() {
      const event = { stopPropagation() {}, preventDefault() {} };
      if (this._listeners.click) this._listeners.click(event);
      if (typeof this.onclick === 'function') this.onclick(event);
    },
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
    for (const model of group.models || []) {
      const opt = makeOption(model.id, model.label || model.id, og);
      og.appendChild(opt);
    }
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
}

function walk(node, out) {
  for (const child of (node.children || [])) {
    out.push(child);
    if (child.children && child.children.length) walk(child, out);
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
function esc(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
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
function closeModelDropdown() {}
function selectFromDropdown() {}

for (const name of [
  '_normalizeConfiguredModelKey',
  '_getConfiguredModelBadge',
  '_getOptionProviderId',
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_isEquivalentConfiguredModelEntry',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

renderModelDropdown();

const rows = [];
for (const node of walk(dropdown, [])) {
  const html = String(node._innerHTML || '');
  if (!html.includes('model-opt-name')) continue;
  if (typeof node.onclick !== 'function') continue;
  const nameMatch = html.match(/<span class="model-opt-name">([\s\S]*?)<\/span>/);
  const idMatch = html.match(/<span class="model-opt-id">([\s\S]*?)<\/span>/);
  const chipMatch = html.match(/<span class="model-opt-badge model-opt-badge--([^"]*)"([^>]*)>([\s\S]*?)<\/span>/);
  rows.push({
    name: nameMatch ? nameMatch[1] : '',
    subtitle: idMatch ? idMatch[1] : '',
    chipRole: chipMatch ? chipMatch[1] : '',
    chipTitle: chipMatch && /title="([^"]*)"/.test(chipMatch[2])
      ? chipMatch[2].match(/title="([^"]*)"/)[1]
      : null,
    chipText: chipMatch ? chipMatch[3] : null,
    html,
  });
}
process.stdout.write(JSON.stringify({ rows }));
"""


def _render_dropdown(tmp_path, payload):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["rows"]


# Three badge shapes the Configured section can produce:
#   * a plain configured model id (label carries the role),
#   * a provider-qualified key,
#   * an `@provider:model` routing spelling (the shape from the issue).
_PAYLOAD = {
    "groups": [
        {
            "provider": "OpenAI",
            "provider_id": "openai",
            "models": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}],
        },
        {
            "provider": "Magpie",
            "provider_id": "custom:magpie",
            "models": [{"id": "codex/gpt-6.1-sol"}],
        },
    ],
    "configuredBadges": {
        "gpt-4o": {"provider": "openai", "role": "primary", "label": "Primary"},
        "openai/gpt-4o-mini": {"provider": "openai", "role": "fallback", "label": "Fallback 1"},
        "@custom:magpie:codex/gpt-6.1-sol": {
            "provider": "custom:magpie",
            "role": "fallback",
            "label": "Fallback 2",
        },
    },
    "selectedValue": "",
}

_CASES = [
    # (row name, raw configured key, expected chip text)
    ("gpt-4o", "gpt-4o", "Primary"),
    ("gpt-4o-mini", "openai/gpt-4o-mini", "Fallback 1"),
    ("codex/gpt-6.1-sol", "@custom:magpie:codex/gpt-6.1-sol", "Fallback 2"),
]


def _row(rows, name):
    matches = [row for row in rows if row["name"] == name]
    assert len(matches) == 1, f"expected one Configured row named {name!r}, got {matches}"
    return matches[0]


def test_configured_chip_shows_role_label_never_the_routing_key(tmp_path):
    rows = _render_dropdown(tmp_path, _PAYLOAD)

    for name, raw_key, expected_chip in _CASES:
        row = _row(rows, name)
        assert row["chipText"] == expected_chip, (
            f"Configured chip for {name} should show the readable role "
            f"{expected_chip!r}, not the matched config key ({row['chipText']!r})"
        )
        assert "@" not in row["chipText"], (
            f"Configured chip for {name} leaked the routing spelling: {row['chipText']!r} "
            f"(raw key was {raw_key!r})"
        )


def test_configured_chip_keeps_raw_key_in_title_and_subtitle(tmp_path):
    rows = _render_dropdown(tmp_path, _PAYLOAD)

    for name, raw_key, _expected_chip in _CASES:
        row = _row(rows, name)
        assert row["chipTitle"] == raw_key, (
            f"the chip's title must preserve the matched config key {raw_key!r} "
            f"so removing it from the label loses nothing (got {row['chipTitle']!r})"
        )
        assert raw_key in row["subtitle"], (
            f"the row subtitle must keep the config key {raw_key!r} visible "
            f"(got {row['subtitle']!r})"
        )


def test_no_configured_chip_in_the_dropdown_contains_an_at_sign(tmp_path):
    rows = _render_dropdown(tmp_path, _PAYLOAD)

    chips = [(row["name"], row["chipText"]) for row in rows if row["chipText"] is not None]
    assert chips, "no Configured badge chips rendered"
    offenders = [(name, text) for name, text in chips if "@" in text]
    assert offenders == [], f"Configured chips must never render a routing key: {offenders}"
