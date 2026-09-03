"""
Regression tests for #7400 re-gate: a selected provider-qualified model row
(@custom:omni:model routing id on a non-active provider's catalog group) must
receive `model-opt active` and the `Selected` badge.

Root cause: _modelStateForSelect(sel, sel.value) resolves the SELECTED option
to its canonical (bare model, owning provider) pair via the dataset.model /
dataset.provider metadata stamped by the catalog population and overflow
paths. renderModelDropdown() compared that canonical state against each
candidate row's RAW m.value — for a provider-qualified row the raw value is
the routing id (@provider:model), not the bare model, so the selected row
never matched. The raw-value fallback only kept the group open; it never
restored row identity.

Fix: _isSelectedModelRow() now canonicalizes the candidate row with the same
_qualifiedCatalogOptionMeta() the stamping paths use, then requires BOTH the
bare model AND the owning provider to match (so two providers offering the
same bare model still disambiguate by provider).

These tests drive the real renderModelDropdown() via Node with a DOM stub,
and the option-creation paths apply the real catalog / overflow
metadata-stamping logic (_qualifiedCatalogOptionMeta + _appendOverflowOptionsToGroup),
so drift between test payloads and production stamping is caught immediately.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Provider-qualified ids as shipped by the server dedupe for non-active
# providers (same bare model offered by two providers).
BARE_MODEL = "antigravity/gemini-3.7-flash-tiered"
QUALIFIED_CUSTOM = f"@custom:omni:{BARE_MODEL}"
QUALIFIED_OTHER = f"@custom:other:{BARE_MODEL}"

_DRIVER = r"""
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

function makeOption(value, label, parent, providerId) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  // Real catalog stamping (#7241/#7400): provider-qualified option ids get the
  // bare model + owning provider derived by the same helper the population
  // loop and _appendOverflowOptionsToGroup use.
  const meta = _qualifiedCatalogOptionMeta(value, providerId || (parent && parent.dataset && parent.dataset.provider) || '');
  if (meta) {
    opt.dataset.model = meta.model;
    opt.dataset.provider = meta.provider;
  }
  return opt;
}

function makeSelect(groups, selectedValue) {
  const sel = { id: 'modelSelect', children: [], options: [], selectedOptions: [], value: selectedValue || '' };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) {
      og.appendChild(makeOption(model.id, model.label || model.id, og, group.provider_id));
    }
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  const selOpt = sel.options.find(o => String(o.value || '') === String(selectedValue || ''));
  if (selOpt) sel.selectedOptions = [selOpt];
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
  if (key === 'model_badge_selected') return 'Selected';
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
  '_isEquivalentConfiguredModelEntry',
  '_qualifiedCatalogOptionMeta',
  '_modelStateForSelect',
  '_getOptionProviderId',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

// Build the select AFTER the real helpers are eval'd — makeOption's metadata
// stamping calls _qualifiedCatalogOptionMeta.
const modelSelect = makeSelect(payload.groups, payload.selectedValue);

renderModelDropdown();
const initial = snapshot(dropdown);

// If the selected row sits in the overflow tail of a capped group, expand via
// the show-all row (real _appendOverflowOptionsToGroup path) and re-snapshot.
const showAllRow = findInTree(dropdown, node => String(node._innerHTML || '').includes('Show all'));
let afterExpand = null;
if (showAllRow && showAllRow.onclick) {
  showAllRow.onclick({ stopPropagation() {} });
  afterExpand = snapshot(dropdown);
}

process.stdout.write(JSON.stringify({ initial, afterExpand }));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("driver_7400") / "driver.js"
    p.write_text(_DRIVER, encoding="utf-8")
    return str(p)


def _run(driver_path, groups, selected_value):
    result = subprocess.run(
        [NODE, driver_path, str(REPO / "static" / "ui.js"),
         json.dumps({"groups": groups, "selectedValue": selected_value})],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


def _active_rows(snap):
    """Rows carrying `model-opt active` (the selected-row marker)."""
    return [item for item in snap if "model-opt" in item["className"] and "active" in item["className"]]


def _row_model_ids(snap):
    """The `.model-opt-id` values rendered inside each row's innerHTML."""
    ids = []
    for item in snap:
        if "model-opt" not in item["className"]:
            continue
        marker = 'class="model-opt-id">'
        start = item["html"].find(marker)
        if start >= 0:
            rest = item["html"][start + len(marker):]
            end = rest.find("<")
            ids.append(rest[:end] if end >= 0 else rest)
    return ids


def _two_provider_groups():
    """Two providers offering the SAME bare model: the active provider ships
    the bare id, the non-active custom provider ships the qualified routing id
    (@custom:omni:model) — the #7400 reported composition."""
    return [
        {
            "provider": "Custom Omni",
            "provider_id": "custom:omni",
            "models": [{"id": QUALIFIED_CUSTOM, "label": BARE_MODEL}],
        },
        {
            "provider": "Default",
            "provider_id": "",
            "models": [{"id": BARE_MODEL, "label": BARE_MODEL}],
        },
    ]


def test_selected_qualified_row_gets_active_and_selected_badge(driver_path):
    out = _run(driver_path, _two_provider_groups(), QUALIFIED_CUSTOM)

    active = _active_rows(out["initial"])
    assert len(active) == 1, (
        "exactly one row must be marked active — the provider-qualified option "
        f"that is actually selected; got {[a['className'] for a in active]}"
    )
    row_html = active[0]["html"]
    assert "model-opt-badge--selected" in row_html, (
        "the selected provider-qualified row must carry the Selected badge"
    )
    assert "Selected" in row_html
    # The active row must be the custom:omni row — the one whose rendered id is
    # the qualified routing id, NOT the bare row of the other provider.
    active_ids = _row_model_ids([active[0]])
    assert QUALIFIED_CUSTOM in active_ids, (
        f"the active row's rendered id must be the provider-qualified id "
        f"{QUALIFIED_CUSTOM}; got {active_ids}"
    )


def test_qualified_selected_row_does_not_steal_bare_row_active(driver_path):
    """Two providers with the same bare model: when the QUALIFIED option is
    selected, the other provider's bare row must NOT be marked active."""
    out = _run(driver_path, _two_provider_groups(), QUALIFIED_CUSTOM)

    active = _active_rows(out["initial"])
    assert len(active) == 1, f"exactly one active row expected; got {active}"
    active_ids = _row_model_ids(active)
    assert QUALIFIED_CUSTOM in active_ids, active_ids
    assert BARE_MODEL not in active_ids, (
        "the bare row of the other provider must stay inactive when the "
        f"qualified option is selected; active ids={active_ids}"
    )


def test_bare_selected_row_still_matches(driver_path):
    """Regression guard: the plain (bare) selection path keeps working — when
    the active provider's bare option is selected, ITS row is active and the
    qualified row is not."""
    out = _run(driver_path, _two_provider_groups(), BARE_MODEL)

    active = _active_rows(out["initial"])
    assert len(active) == 1, f"bare selection must mark exactly one row active; got {active}"
    active_ids = _row_model_ids(active)
    assert BARE_MODEL in active_ids, active_ids
    assert QUALIFIED_CUSTOM not in active_ids, active_ids


def test_qualified_row_overflows_are_canonicalized(driver_path):
    """Provider-qualified overflow rows (extra_models of a capped custom group)
    revealed via the show-all expander must also match by canonical identity."""
    groups = [
        {
            "provider": "Custom Omni",
            "provider_id": "custom:omni",
            "models": [{"id": "custom:omni:visible-a", "label": "Visible A"}],
            "extra_models": [{"id": QUALIFIED_CUSTOM, "label": BARE_MODEL}],
        },
        {
            "provider": "Default",
            "provider_id": "",
            "models": [{"id": BARE_MODEL, "label": BARE_MODEL}],
        },
    ]
    out = _run(driver_path, groups, QUALIFIED_CUSTOM)

    snap = out["afterExpand"] or out["initial"]
    active = _active_rows(snap)
    # The qualified overflow row must be the single active one after reveal.
    assert len(active) == 1, (
        "the provider-qualified overflow row must become the active row after "
        f"show-all reveal; got {[a['className'] for a in active]}"
    )
    assert "model-opt-badge--selected" in active[0]["html"]
    active_ids = _row_model_ids(active)
    assert QUALIFIED_CUSTOM in active_ids, active_ids
    assert BARE_MODEL not in active_ids, active_ids
