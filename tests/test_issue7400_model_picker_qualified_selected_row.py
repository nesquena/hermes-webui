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
// static/panels.js hosts the Settings picker producer; optional so the
// composer-only cases keep working when it is not passed.
const panels = (() => {
  try { return fs.readFileSync(process.argv[4], 'utf8'); } catch (e) { return ''; }
})();

function extractFunc(name, src) {
  src = src || ui;
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let openParen = src.indexOf('(', start);
  let i = openParen + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < src.length) {
    if (src[i] === '(') parenDepth++;
    else if (src[i] === ')') parenDepth--;
    i++;
  }
  i = src.indexOf('{', i);
  let depth = 1;
  i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
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

function _selMatch(node, simple) {
  simple = String(simple || '').trim();
  if (!simple) return false;
  const comps = simple.match(/\.([\w-]+)|\[([^\]]+)\]|^([a-zA-Z][\w-]*)$/g) || [];
  if (!comps.length) return false;
  for (const part of comps) {
    if (part[0] === '.') {
      if (!(node.classList && node.classList.contains(part.slice(1)))) return false;
    } else if (part[0] === '[') {
      const m = part.slice(1, -1).match(/^([\w-]+)="([^"]*)"$/);
      if (!m) return false;
      const key = m[1].startsWith('data-') ? m[1].slice(5) : m[1];
      const want = m[2].replace(/\\(.)/g, '$1');
      const got = node.dataset ? node.dataset[key] : undefined;
      if (String(got === undefined || got === null ? '' : got) !== want) return false;
    } else {
      if (node.tagName !== part.toUpperCase()) return false;
    }
  }
  return true;
}

function _qsa(root, selector) {
  const parts = String(selector || '').trim().split(/\s+/).filter(Boolean);
  const out = [];
  if (!parts.length) return out;
  const last = parts[parts.length - 1];
  const stack = [...(root.children || [])];
  while (stack.length) {
    const n = stack.shift();
    if (n.children && n.children.length) stack.push(...n.children);
    if (!_selMatch(n, last)) continue;
    if (parts.length === 1) { out.push(n); continue; }
    let p = n.parentElement;
    while (p && p !== root) {
      if (_selMatch(p, parts[0])) { out.push(n); break; }
      p = p.parentElement;
    }
  }
  return out;
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
      if (child.parentElement) {
        const oldIdx = child.parentElement.children.indexOf(child);
        if (oldIdx >= 0) child.parentElement.children.splice(oldIdx, 1);
        const oldSel = child.parentElement.tagName === 'SELECT'
          ? child.parentElement
          : child.parentElement._ownerSelect;
        if (oldSel && oldSel.options) {
          const oi = oldSel.options.indexOf(child);
          if (oi >= 0) oldSel.options.splice(oi, 1);
        }
      }
      child.parentElement = this;
      child.parentNode = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        this._ownerSelect.options.push(child);
      }
      return child;
    },
    insertBefore(newChild, refChild) {
      newChild.parentElement = this;
      const idx = refChild ? this.children.indexOf(refChild) : -1;
      if (idx >= 0) this.children.splice(idx, 0, newChild);
      else this.children.push(newChild);
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
      if (this._qs && this._qs[selector]) return this._qs[selector];
      return _qsa(this, selector)[0] || null;
    },
    querySelectorAll(selector) { return _qsa(this, selector); },
    getAttribute(name) {
      if (name.startsWith('data-')) {
        return this.dataset ? this.dataset[name.slice(5)] : undefined;
      }
      return this[name];
    },
    setAttribute(name, value) { this[name] = value; },
    focus() { this._focused = true; },
  };
  Object.defineProperty(node, 'previousElementSibling', {
    get() {
      if (!this.parentElement) return null;
      const idx = this.parentElement.children.indexOf(this);
      return idx > 0 ? this.parentElement.children[idx - 1] : null;
    },
  });
  Object.defineProperty(node, 'offsetTop', { value: 0 });
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

function makeOption(value, label, parent, providerId, stamp) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  opt.parentNode = parent || null;
  // stamp === false reproduces a producer that never stamped its options (the
  // Settings population before #7400, a live row from an older client): the
  // select then exposes only the qualified routing value.
  if (stamp === false) return opt;
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

function makeSelect(groups, selectedValue, rootOption, unstamped, selectId) {
  const sel = {
    id: selectId || 'modelSelect', tagName: 'SELECT', children: [], options: [], selectedOptions: [],
    value: selectedValue || '',
    querySelectorAll(tag) {
      // _addLiveModelsToSelect(provider, models, sel) looks its optgroup up by
      // tag; every other caller wants the flat option list.
      if (String(tag || '').toLowerCase() === 'optgroup') {
        return this.children.filter(c => c.tagName === 'OPTGROUP');
      }
      return this.options.slice();
    },
    appendChild(child) {
      child.parentElement = this;
      child.parentNode = this;
      this.children.push(child);
      if (child.tagName === 'OPTION') this.options.push(child);
      return child;
    },
  };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    og.parentElement = sel;
    og.parentNode = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) {
      og.appendChild(makeOption(model.id, model.label || model.id, og, group.provider_id, !unstamped));
    }
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  // The real-sequence root injection (#7400 re-gate): an overflow model that
  // was picked via search lives at the <select> ROOT (populated by
  // _ensureModelOptionInDropdown), not inside its provider's optgroup.
  if (rootOption && rootOption.id) {
    const ro = makeOption(rootOption.id, rootOption.label || rootOption.id, sel, rootOption.provider || '', !unstamped);
    sel.children.push(ro);
    sel.options.push(ro);
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

const CSS = { escape: s => String(s || '').replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
const requestAnimationFrame = fn => { fn(); return 0; };

const payload = JSON.parse(process.argv[3]);
const dropdown = makeNode('div');
dropdown.classList.add('open');

// Settings save-path mode (#7400 re-gate): saveSettings() resolves its model
// select through $('settingsModel'), everything else through null-tolerant
// lookups.
let _settingsSaveSelect = null;
let modelSelect = null;

function $(id) {
  const _settingsPicker = payload.settingsPicker === true;
  if (id === 'settingsModel' && _settingsSaveSelect) return _settingsSaveSelect;
  if (id === (_settingsPicker ? 'settingsModelDropdown' : 'composerModelDropdown')) return dropdown;
  if (id === (_settingsPicker ? 'settingsModel' : 'modelSelect')) return modelSelect;
  return null;
}
const window = {
  _configuredModelBadges: payload.configuredBadges || {},
  _activeProvider: payload.activeProvider || '',
  _defaultModel: null,
  _showThinking: true,
  _workspaceTodosTab: false,
};
const S = { session: {} };
const _dynamicModelLabels = {};
function _applyModelToDropdown() { return false; }
function syncModelChip() {}
const document = { createElement(tag) { return makeNode(tag); }, documentElement: { dataset: {} } };
const localStorage = { getItem() { return null; }, setItem() {} };
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

// --- Settings save-path environment (#7400 re-gate) -------------------------
// The settings-save mode drives the REAL saveSettings() + the REAL
// _applySavedSettingsUi() with only the network boundary and the unrelated
// settings widgets stubbed, so the assertion is on what the client posts and
// what it mirrors afterwards — not on the shape of the source.
const _settingsPosts = [];
const _settingsOpenState = { model: '', provider: null };
function api(path, opts) { _settingsPosts.push({ path: String(path), body: String((opts && opts.body) || '') }); return Promise.resolve({}); }
function _enqueueSettingsPost(opts) { _settingsPosts.push({ path: '/api/preferences', body: String((opts && opts.body) || '') }); return Promise.resolve({ auth_enabled: false, password_auth_enabled: false }); }
function showToast() {}
function _speechPreferencesPayloadFromUi() { return {}; }
function _structuredCodeViewFromUi() { return {}; }
function _composerControlVisibilityPayload() { return {}; }
function _getComposerControlOrder() { return []; }
function _syncChatActivityDisplayModeControl() {}
function _syncTransparentEventTimestampsControl() {}
function _ensureComposerControlVisibilityState() {}
function _renderComposerControlChips() {}
function _renderComposerSituationalControlChips() {}
function _setComposerControlOrder(list) { return list || []; }
function _setSettingsAuthButtonsVisible() {}
function _resetSettingsPanelState() {}
function _hideSettingsPanel() {}
function _updateCurrentPasswordVisibility() {}
function _renderSettingsAuthStatus() {}
function _updateAuthWarningBadge() {}
function _updateAuthDisabledWarning() {}
function renderMessages() {}
var _settingsDirty = false;
var _pendingSettingsTargetPanel = null;
var _settingsPasswordAuthEnabled = false;
var _settingsHermesDefaultModelOnOpen = '';
var _settingsHermesDefaultModelProviderOnOpen = null;
var _settingsThemeOnOpen = '';
var _settingsSkinOnOpen = '';
var _settingsFontSizeOnOpen = '';

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_isEquivalentConfiguredModelEntry',
  '_qualifiedCatalogOptionMeta',
  '_stampQualifiedOptionMeta',
  '_modelStateForSelect',
  '_getOptionProviderId',
  '_captureModelDropdownSelection',
  '_addLiveModelsToSelect',
  'renderModelDropdown',
]) {
  eval(extractFunc(name, ui));
}
if (payload.mode === 'settings-stamp') eval(extractFunc('_populateSettingsModelOptions', panels));

function collectOptions(sel) {
  const out = [];
  for (const child of (sel.children || [])) {
    if (child.tagName === 'OPTION') out.push(child);
    if (child.tagName === 'OPTGROUP') {
      for (const o of (child.children || [])) if (o.tagName === 'OPTION') out.push(o);
    }
  }
  return out.map(o => ({
    value: o.value,
    datasetModel: o.dataset ? (o.dataset.model || null) : null,
    datasetProvider: o.dataset ? (o.dataset.provider || null) : null,
    title: o.title || '',
  }));
}

// Producer modes drive the REAL option-creation paths, so deleting the metadata
// stamping in static/ui.js or static/panels.js fails these cases.
if (payload.mode === 'settings-stamp') {
  const settingsSel = makeSelect([], '', null, true);
  const created = _populateSettingsModelOptions(settingsSel, payload.groups);
  process.stdout.write(JSON.stringify({ mode: payload.mode, created, emitted: collectOptions(settingsSel) }));
  process.exit(0);
}
if (payload.mode === 'live-stamp') {
  const liveSel = makeSelect(payload.groups, payload.selectedValue, null, true);
  const added = _addLiveModelsToSelect(payload.liveProvider, payload.liveModels, liveSel);
  process.stdout.write(JSON.stringify({ mode: payload.mode, added, emitted: collectOptions(liveSel) }));
  process.exit(0);
}

// Settings save path (#7400 re-gate): the Settings panel opens with a
// provider-qualified row selected, the user hits Save. Drive the REAL
// saveSettings() so the posted default-model pair and the mirrored
// window._activeProvider / _settingsHermesDefaultModelProviderOnOpen are the
// observable result.
if (payload.mode === 'settings-save') {
  _settingsSaveSelect = makeSelect(payload.groups, payload.selectedValue, null, false, 'settingsModel');
  _settingsHermesDefaultModelOnOpen = payload.openedModel || '';
  _settingsHermesDefaultModelProviderOnOpen = payload.openedProvider || null;
  eval(extractFunc('_applySavedSettingsUi', panels));
  eval(extractFunc('saveSettings', panels));
  saveSettings(false).then(() => {
    process.stdout.write(JSON.stringify({
      mode: payload.mode,
      posts: _settingsPosts,
      activeProvider: window._activeProvider === undefined ? null : window._activeProvider,
      defaultModelMirror: window._defaultModel === undefined ? null : window._defaultModel,
      providerMirror: _settingsHermesDefaultModelProviderOnOpen === undefined ? null : _settingsHermesDefaultModelProviderOnOpen,
      modelMirror: _settingsHermesDefaultModelOnOpen === undefined ? null : _settingsHermesDefaultModelOnOpen,
      selectValue: _settingsSaveSelect.value,
    }));
  }).catch(err => {
    process.stdout.write(JSON.stringify({ mode: payload.mode, error: String(err && err.message || err) }));
  });
} else {

// Build the select AFTER the real helpers are eval'd — makeOption's metadata
// stamping calls _qualifiedCatalogOptionMeta.
modelSelect = makeSelect(payload.groups, payload.selectedValue, payload.rootOption, payload.unstamped, payload.selectId);

renderModelDropdown(payload.renderOpts);
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
}
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("driver_7400") / "driver.js"
    p.write_text(_DRIVER, encoding="utf-8")
    return str(p)


def _run(driver_path, groups, selected_value, root_option=None, **extra):
    payload = {"groups": groups, "selectedValue": selected_value, "rootOption": root_option}
    payload.update(extra)
    result = subprocess.run(
        [NODE, driver_path, str(REPO / "static" / "ui.js"), json.dumps(payload),
         str(REPO / "static" / "panels.js")],
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


def _live_root_injected_groups():
    """The composer picker after a live/overflow pick: the qualified model was
    injected at the <select> ROOT (it appears once), the provider group carries a
    different model, and the active provider offers the same bare model."""
    return [
        {
            "provider": "Custom Omni",
            "provider_id": "custom:omni",
            "models": [{"id": "custom:omni:other-model", "label": "Other"}],
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


def test_selected_root_injected_overflow_show_all_keeps_single_active(driver_path):
    """REAL sequence (#7400 re-gate): an overflow model picked via search is
    injected at the <select> ROOT (its own active row + Selected badge), while
    the provider optgroup still advertises it as overflow. Clicking the
    group's Show-all expander must MOVE the option into the optgroup and keep
    exactly ONE active row and ONE Selected badge — the in-place reveal must
    not build a second active row while the stale root row stays rendered."""
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
    out = _run(
        driver_path,
        groups,
        QUALIFIED_CUSTOM,
        root_option={"id": QUALIFIED_CUSTOM, "label": BARE_MODEL, "provider": "custom:omni"},
    )

    assert out["initial"] is not None and out["afterExpand"] is not None, (
        "the root-injected selected option must render its row AND the group "
        "must advertise a Show-all expander to click"
    )
    for label, snap in (("before", out["initial"]), ("after", out["afterExpand"])):
        active = _active_rows(snap)
        assert len(active) == 1, (
            f"{label} Show-all reveal must keep exactly ONE active row — the "
            f"root-injected option must not duplicate inside the group; "
            f"got {[a['className'] for a in active]}"
        )
        assert "model-opt-badge--selected" in active[0]["html"] and "Selected" in active[0]["html"], (
            f"{label}: the single active row must carry the Selected badge"
        )
        ids = _row_model_ids(snap)
        # The qualified id must appear exactly once across ALL rendered rows —
        # no orphan root row left behind next to the group row.
        assert ids.count(QUALIFIED_CUSTOM) == 1, (
            f"{label}: qualified row must render exactly once; ids={ids}"
        )
    active_after = _row_model_ids(_active_rows(out["afterExpand"]))
    assert BARE_MODEL not in active_after, (
        "the bare row of the other provider must stay inactive after the reveal"
    )


def _settings_payload_extra(**kw):
    """The Settings picker renders through the SAME renderModelDropdown() row
    comparison as the composer, but resolves its nodes via the Settings ids."""
    extra = {
        "settingsPicker": True,
        "selectId": "settingsModel",
        "renderOpts": {
            "selectId": "settingsModel",
            "dropdownId": "settingsModelDropdown",
            "autoFocusSearch": False,
        },
    }
    extra.update(kw)
    return extra


def test_settings_unstamped_qualified_option_keeps_single_active_row_and_badge(driver_path):
    """Settings producer contract (#7400 re-gate): a select that BEGINS with an
    unstamped provider-qualified option (no dataset.model — the shape the
    Settings population left behind, and what any producer skipping
    _stampQualifiedOptionMeta still produces) must render exactly ONE active row
    carrying the Selected badge. Before the fix the selected value stayed raw
    (@custom:omni:model) while every candidate row was canonicalized to the bare
    model, so the row rendered 0 active / 0 Selected."""
    out = _run(driver_path, _two_provider_groups(), QUALIFIED_CUSTOM,
               unstamped=True, **_settings_payload_extra())

    active = _active_rows(out["initial"])
    assert len(active) == 1, (
        "the Settings picker must mark exactly one row active for an unstamped "
        f"selected qualified option; got {[a['className'] for a in active]}"
    )
    row_html = active[0]["html"]
    assert "model-opt-badge--selected" in row_html and "Selected" in row_html, (
        "the active Settings row must carry the Selected badge"
    )
    active_ids = _row_model_ids(active)
    assert QUALIFIED_CUSTOM in active_ids, active_ids
    assert BARE_MODEL not in active_ids, (
        f"the other provider's bare row must stay inactive; ids={active_ids}"
    )


def test_live_model_unstamped_qualified_option_keeps_single_active_row_and_badge(driver_path):
    """Live-model case (#7400 re-gate): the composer picker with an unstamped
    qualified option — the shape a live-fetched row had before
    _addLiveModelsToSelect stamped dataset.model (and what an older client or a
    third-party producer still emits). Exactly one active row + Selected badge."""
    out = _run(driver_path, _live_root_injected_groups(), QUALIFIED_CUSTOM,
               unstamped=True,
               rootOption={"id": QUALIFIED_CUSTOM, "label": BARE_MODEL, "provider": "custom:omni"})

    for label, snap in (("initial", out["initial"]), ("expanded", out["afterExpand"] or out["initial"])):
        active = _active_rows(snap)
        assert len(active) == 1, (
            f"{label}: an unstamped live-model option that is selected must keep "
            f"exactly ONE active row; got {[a['className'] for a in active]}"
        )
        assert "model-opt-badge--selected" in active[0]["html"], (
            f"{label}: the active live-model row must carry the Selected badge"
        )
        active_ids = _row_model_ids(active)
        assert QUALIFIED_CUSTOM in active_ids, active_ids
        assert BARE_MODEL not in active_ids, (
            f"{label}: the other provider's bare row must stay inactive; ids={active_ids}"
        )


def test_settings_population_stamps_qualified_row_metadata(driver_path):
    """Mutation guard for the Settings producer: _populateSettingsModelOptions()
    (the REAL function loadSettingsPanel() calls) must stamp the bare model and
    the owning provider on a provider-qualified option. Deleting its
    _stampQualifiedOptionMeta call turns this red."""
    out = _run(driver_path, _two_provider_groups(), "", mode="settings-stamp")

    assert out["created"] == 2, f"both catalog rows must be created; got {out['created']}"
    by_value = {entry["value"]: entry for entry in out["emitted"]}
    qualified = by_value[QUALIFIED_CUSTOM]
    assert qualified["datasetModel"] == BARE_MODEL, (
        "the Settings population must expose the bare model for a qualified row; "
        f"got {qualified['datasetModel']!r}"
    )
    assert qualified["datasetProvider"] == "custom:omni", qualified["datasetProvider"]
    # A plain row stays unstamped: the metadata is the qualified-row contract,
    # not a blanket rewrite of every option.
    assert by_value[BARE_MODEL]["datasetModel"] is None, by_value[BARE_MODEL]


def test_live_model_insertion_stamps_qualified_row_metadata(driver_path):
    """Mutation guard for the live-model producer: _addLiveModelsToSelect() (the
    REAL function the live fetch calls) must stamp the bare model plus the
    owning provider. Before the fix it wrote dataset.provider only, so a
    selected live row could not be resolved by _modelStateForSelect()."""
    groups = [
        {"provider": "Custom Omni", "provider_id": "custom:omni",
         "models": [{"id": "custom:omni:other-model", "label": "Other"}]},
    ]
    out = _run(driver_path, groups, "", mode="live-stamp",
               liveProvider="custom:omni",
               liveModels=[
                   {"id": QUALIFIED_CUSTOM, "label": BARE_MODEL},
                   {"id": "custom:omni:plain-live", "label": "Plain live"},
               ])

    assert out["added"] == 2, f"both live rows must be inserted; got {out['added']}"
    by_value = {entry["value"]: entry for entry in out["emitted"]}
    live = by_value[QUALIFIED_CUSTOM]
    assert live["datasetModel"] == BARE_MODEL, (
        f"the live row must expose its bare model; got {live['datasetModel']!r}"
    )
    assert live["datasetProvider"] == "custom:omni", live["datasetProvider"]
    # Unqualified live ids keep the pre-existing shape (no bare-model stamp).
    plain_live = by_value["custom:omni:plain-live"]
    assert plain_live["datasetModel"] is None, plain_live
    # Finding 1 (#7400 re-gate): the provider stamp stays UNCONDITIONAL, so an
    # unqualified live row must keep datasetProvider == the live provider (the
    # assertion test_chat_start_provider_fallback.py relies on).
    assert plain_live["datasetProvider"] == "custom:omni", plain_live


COLON_BARE = "qwen3:32b"
COLON_QUALIFIED = f"@ollama:{COLON_BARE}"
COLON_PROVIDER_BARE = "model-a:free"
COLON_PROVIDER_QUALIFIED = f"@custom:backup:{COLON_PROVIDER_BARE}"


def _root_injected_groups(provider_id, provider_label, qualified_id, bare_model, other_id):
    """Composer picker after a live/overflow pick: the qualified model sits at
    the <select> ROOT (it is absent from every group's model list), its owning
    group advertises it as overflow so a Show-all expander renders, and a second
    provider offers an unrelated model."""
    return [
        {
            "provider": provider_label,
            "provider_id": provider_id,
            "models": [{"id": other_id, "label": "Other"}],
            "extra_models": [{"id": qualified_id, "label": bare_model}],
        },
        {
            "provider": "Custom Omni",
            "provider_id": "custom:omni",
            "models": [{"id": "custom:omni:unrelated", "label": "Unrelated"}],
        },
    ]


def _assert_single_active_qualified_row(snap, qualified_id, label):
    active = _active_rows(snap)
    assert len(active) == 1, (
        f"{label}: exactly one row must be active for a root-injected routing id; "
        f"got {[a['className'] for a in active]}"
    )
    assert active[0]["html"].count("model-opt-badge--selected") == 1, (label, active[0]["html"])
    assert "Selected" in active[0]["html"], (label, active[0]["html"])
    assert _row_model_ids(active) == [qualified_id], (label, _row_model_ids(active))
    ids = _row_model_ids(snap)
    assert ids.count(qualified_id) == 1, (
        f"{label}: the qualified id must render exactly once (no orphan root row "
        f"next to the group row); ids={ids}"
    )


def test_root_injected_colon_model_id_keeps_single_active_before_and_after_move(driver_path):
    """Finding 4 (#7400 re-gate), colon INSIDE the model id: the root <option>
    for @ollama:qwen3:32b must expose dataset.provider ("ollama") instead of
    being reparsed by the ambiguous last-colon fallback (which yields provider
    "ollama:qwen3" / model "32b"). One active row + one Selected badge before
    AND after the root-to-optgroup Show-all move."""
    groups = _root_injected_groups(
        "ollama", "Ollama", COLON_QUALIFIED, COLON_BARE, "ollama:llama3.3:70b")
    out = _run(driver_path, groups, COLON_QUALIFIED,
               root_option={"id": COLON_QUALIFIED, "label": COLON_BARE, "provider": "ollama"})

    assert out["initial"] is not None and out["afterExpand"] is not None, (
        "the root-injected selected option must render its row AND its group must "
        "advertise a Show-all expander to click"
    )
    for label, snap in (("before", out["initial"]), ("after", out["afterExpand"])):
        _assert_single_active_qualified_row(snap, COLON_QUALIFIED, label)


def test_root_injected_colon_provider_id_keeps_single_active_before_and_after_move(driver_path):
    """Finding 4 (#7400 re-gate), colon INSIDE the provider id: the root
    <option> for @custom:backup:model-a:free belongs to provider
    "custom:backup" with model "model-a:free" — the last-colon fallback would
    claim provider "custom:backup:model-a" / model "free" and drop the selected
    state. Same single-active-row + single-Selected-badge contract."""
    groups = _root_injected_groups(
        "custom:backup", "Custom Backup", COLON_PROVIDER_QUALIFIED,
        COLON_PROVIDER_BARE, "custom:backup:other")
    out = _run(driver_path, groups, COLON_PROVIDER_QUALIFIED,
               root_option={"id": COLON_PROVIDER_QUALIFIED,
                            "label": COLON_PROVIDER_BARE, "provider": "custom:backup"})

    assert out["initial"] is not None and out["afterExpand"] is not None, (
        "the root-injected selected option must render its row AND its group must "
        "advertise a Show-all expander to click"
    )
    for label, snap in (("before", out["initial"]), ("after", out["afterExpand"])):
        _assert_single_active_qualified_row(snap, COLON_PROVIDER_QUALIFIED, label)


def test_settings_save_qualified_cross_provider_row_posts_canonical_pair(driver_path):
    """Finding 3 (#7400 re-gate): the Settings panel was opened on the DEFAULT
    provider's bare row; the user picks the provider-qualified row owned by a
    custom provider offering the SAME bare model and hits Save. The client must
    POST the canonical pair (bare model + owning provider) and must NOT wipe
    window._activeProvider / _settingsHermesDefaultModelProviderOnOpen to null."""
    out = _run(driver_path, _two_provider_groups(), QUALIFIED_CUSTOM,
               mode="settings-save", openedModel=BARE_MODEL, openedProvider=None)

    assert "error" not in out, out
    model_posts = [post for post in out["posts"] if post["path"] == "/api/default-model"]
    assert len(model_posts) == 1, out["posts"]
    assert json.loads(model_posts[0]["body"]) == {"model": BARE_MODEL, "provider": "custom:omni"}, model_posts[0]
    assert out["selectValue"] == QUALIFIED_CUSTOM, out
    assert out["activeProvider"] == "custom:omni", out
    assert out["providerMirror"] == "custom:omni", out
    assert out["modelMirror"] == BARE_MODEL, out
    assert out["defaultModelMirror"] == BARE_MODEL, out
