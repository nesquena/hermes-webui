"""Opening the model picker must not auto-focus the search input.

Why: the picker is a CHOOSER. Users open it to pick a model, not to search. On
phones the auto-focus pops the on-screen keyboard the instant the chip is
tapped, so the model list sits behind the IME and the user has to dismiss the
keyboard before they can tap a row. Upstream suppressed this for the Settings
picker on coarse pointers only; the local policy is uniform: NO device
auto-focuses.

What replaces it: the dropdown container receives focus, so arrow-key /
Enter / Escape navigation still works without any text field being focused
(and therefore without any IME). Focus is still restored for a user who is
already typing in the search box when a re-render happens.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", src)
    assert match, f"{name} not found"
    start = match.end()
    depth = 1
    pos = start
    while pos < len(src) and depth:
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"{name} body did not terminate"
    return src[start : pos - 1]


def test_renderer_never_auto_focuses_the_search_input():
    body = _function_body(UI_JS, "renderModelDropdown")
    # Single source of truth: the policy is hard-coded in the renderer, so no
    # call site can re-enable the keyboard pop by passing an option.
    assert "const _autoFocusSearch=false;" in body
    assert "opts.autoFocusSearch" not in body
    # The one surviving focus call is the typing-preservation path, not a
    # first-open autofocus.
    assert "if(_autoFocusSearch||_hadFocus) _si.focus();" in body


def test_no_call_site_passes_the_autofocus_option():
    # The option is gone from the API; a caller still passing it is dead plumbing
    # that would silently stop working if the renderer ever read it again.
    assert "autoFocusSearch:" not in UI_JS
    assert "autoFocusSearch!==false" not in UI_JS


def test_dropdown_container_is_focusable_and_receives_focus():
    body = _function_body(UI_JS, "renderModelDropdown")
    # A plain <div> is not focusable — focus() on it is a silent no-op, which
    # would leave arrow-key navigation dead once the search box stops grabbing
    # focus. tabindex="-1" makes it programmatically focusable only.
    assert "dd.setAttribute('tabindex','-1')" in body

    open_body = _function_body(UI_JS, "toggleModelDropdown")
    assert "dd.focus({preventScroll:true})" in open_body
    # Focus must land AFTER the phone path reparents the menu onto <body>
    # (_positionModelDropdown), or the reparent detaches the focused node.
    assert open_body.index("_positionModelDropdown();") < open_body.index("dd.focus({preventScroll:true})")
    # And it must never target the search input on open.
    assert "querySelector('.model-search-input')" not in open_body
    assert ".model-search-input').focus" not in open_body


def test_settings_picker_also_focuses_container_not_input():
    body = _function_body(UI_JS, "openSettingsModelDropdown")
    assert "querySelector('.model-search-input')" not in body
    assert "dd.focus({preventScroll:true})" in body


def test_keyboard_navigation_survives_container_focus():
    body = _function_body(UI_JS, "renderModelDropdown")
    # Shared handler, wired to BOTH the search input (user tapped it) and the
    # container (opened without autofocus).
    assert "const _modelPickerKey=(e)=>" in body
    assert "_si.addEventListener('keydown',e=>{_modelPickerKey(e);});" in body
    assert "dd._modelPickerKey=_modelPickerKey;" in body
    # Bound once and dispatched through the refreshed slot: dd survives every
    # re-render (only its children are replaced), so re-binding would stack
    # duplicate listeners while a first-render closure would point at the
    # detached search input.
    assert "if(!dd._modelPickerKeyBound){" in body
    # Only handle keys when the CONTAINER itself holds focus, so events bubbling
    # from the inputs are never handled twice.
    assert "if(e.target!==dd) return;" in body


def test_typing_focus_is_preserved_across_rerenders():
    body = _function_body(UI_JS, "renderModelDropdown")
    # A late live-model fetch / preferences refresh replaces the input the user
    # is typing in. _hadFocus alone can't see it (it compares against the NEW
    # input after the old one detached), so a pre-teardown snapshot exists.
    assert "_searchHadFocusBefore" in body
    assert "_ddHadFocusBefore" in body
    # The snapshot must be taken in the render entry, before the children are
    # torn down (the real statement, not the comment that mentions it).
    snapshot_idx = body.index("const _searchHadFocusBefore=")
    teardown_idx = body.index("    dd.innerHTML='';")
    assert snapshot_idx < teardown_idx, "snapshot must precede the child teardown"


# ---------------------------------------------------------------------------
# Behavioral layer (node driver).
#
# The source-contract tests above prove the renderer SAYS the right thing.
# These execute the real renderer against a minimal-DOM harness and prove it
# DOES the right thing — the same class of coverage the upstream behavioral
# suite (test_issue3691 / test_configured_model_picker_*) carries for every
# picker behavior except focus, which is exactly what this change is about.
#
# Harness deltas vs. the upstream minimal-DOM fixtures:
#   * focus() mirrors the browser contract and moves document.activeElement;
#   * addEventListener stores a CALLABLE LIST (wrapper + .list array), so the
#     renderer's own `_listeners.input()` direct-call style keeps working while
#     duplicate keyboard bindings become observable instead of silent;
#   * querySelector/querySelectorAll walk the real child tree (no _qs magic).
# ---------------------------------------------------------------------------

import json
import os
import shutil
import subprocess

import pytest

NODE = shutil.which("node")

_FOCUS_DRIVER = r"""
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

// Callable listener list: the renderer calls `node._listeners.input()` as a
// plain function in places, while tests need the per-type array to detect
// duplicate bindings. A function carrying a .list array satisfies both.
function makeListenerList() {
  const list = [];
  const wrapper = function (...args) { for (const fn of list) fn(...args); };
  wrapper.list = list;
  return wrapper;
}

let _activeElement = null;

function _matchesClass(el, sel) {
  if (!sel || sel[0] !== '.') return false;
  return !!(el.classList && el.classList.contains(sel.slice(1)));
}

function makeNode(tag) {
  const _tag = String(tag || '').toUpperCase();
  const node = {
    tagName: _tag,
    // Browser focusability contract: form controls and links are focusable by
    // default; a plain <div> is NOT — focus() on it is a silent no-op unless
    // tabindex makes it programmatically focusable. Without this rule the
    // harness would "succeed" at focusing an unfocusable container and hide a
    // missing tabindex="-1".
    _focusable: ['INPUT', 'BUTTON', 'SELECT', 'TEXTAREA', 'A'].includes(_tag),
    children: [],
    parentElement: null,
    style: {},
    dataset: {},
    textContent: '',
    value: '',
    onclick: null,
    _listeners: {},
    _innerHTML: '',
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      return child;
    },
    addEventListener(type, handler) {
      if (!this._listeners[type]) this._listeners[type] = makeListenerList();
      this._listeners[type].list.push(handler);
    },
    dispatchEvent(type, event) {
      const ll = this._listeners[type];
      if (!ll) return;
      for (const fn of ll.list) fn(event || {});
    },
    _walk(pred, out) {
      for (const c of this.children || []) {
        if (pred(c)) out.push(c);
        if (c.children && c.children.length) c._walk(pred, out);
      }
      return out;
    },
    querySelector(selector) {
      const sels = String(selector).split(',').map((s) => s.trim());
      const found = this._walk((el) => sels.some((s) => _matchesClass(el, s)), []);
      return found.length ? found[0] : null;
    },
    querySelectorAll(selector) {
      const sels = String(selector).split(',').map((s) => s.trim());
      return this._walk((el) => sels.some((s) => _matchesClass(el, s)), []);
    },
    contains(node) {
      if (!node) return false;
      return this._walk((el) => el === node, []).length > 0;
    },
    setAttribute(name, value) { if (name === 'tabindex') this._focusable = true; this[name] = value; },
    hasAttribute(name) { return this[name] !== undefined && this[name] !== null; },
    removeAttribute(name) { if (name === 'tabindex') this._focusable = false; delete this[name]; },
    focus() { if (!this._focusable) return; this._focused = true; _activeElement = this; },
    blur() { this._focused = false; if (_activeElement === this) _activeElement = null; },
    click() {
      const event = { stopPropagation() {}, preventDefault() {} };
      const ll = this._listeners.click;
      if (ll) for (const fn of ll.list) fn(event);
      if (typeof this.onclick === 'function') this.onclick(event);
    },
  };
  node.classList = makeClassList();
  defineClassName(node);
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      const html = String(v || '');
      this._innerHTML = html;
      this.children = [];
      // The renderer builds subtrees as HTML STRINGS (search row, custom row)
      // and then queries inside them. Recreate the known element stubs from
      // their class markers so querySelector behaves like a browser.
      for (const cls of ['model-search-input', 'model-search-clear', 'model-custom-input', 'model-custom-btn']) {
        if (html.includes(cls)) {
          const child = makeNode(cls.includes('input') ? 'input' : 'button');
          child.className = cls;
          this.appendChild(child);
        }
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
      og.appendChild(makeOption(model.id, model.label || model.id, og));
    }
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
}

const dropdown = makeNode('div');
const chip = makeNode('button');
const mobileAction = makeNode('div');
const modelSelect = makeSelect(payload.groups, payload.selectedValue);

function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'composerModelChip') return chip;
  if (id === 'composerMobileModelAction') return mobileAction;
  if (id === 'modelSelect') return modelSelect;
  return null;
}
const window = { _configuredModelBadges: payload.configuredBadges || {} };
const document = {
  createElement(tag) { return makeNode(tag); },
  get activeElement() { return _activeElement; },
};
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
let _lastSelected = null;
function selectModelFromDropdown(value, provider) {
  _lastSelected = { value: value, provider: provider || null };
}
function closeModelDropdown() {
  dropdown.classList.remove('open');
  chip.classList.remove('active');
  if (mobileAction) mobileAction.classList.remove('active');
}
function _positionModelDropdown() {}
function _syncModelDropdownStickyInsets() {}
function _nudgeModelRowBelowStickyStack() {}

for (const name of [
  '_modelPickerContractRuns',
  '_modelPickerCompareRuns',
  '_modelPickerCompareContract',
  '_modelPickerSortableId',
  '_compareModelPickerEntries',
  '_sortModelPickerEntries',
  '_sortModelPickerOptions',
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_isEquivalentConfiguredModelEntry',
  'renderModelDropdown',
  'toggleModelDropdown',
]) {
  eval(extractFunc(name));
}

(async () => {
  const out = {};
  // --- initial open focuses the container, never the search input ---------
  await toggleModelDropdown();
  out.dropdownHasOpenClass = dropdown.classList.contains('open');
  out.dropdownHasTabindex = dropdown.hasAttribute('tabindex');
  out.openActiveIsDropdown = document.activeElement === dropdown;
  out.openActiveIsSearchInput = !!(
    document.activeElement && document.activeElement.classList.contains('model-search-input')
  );

  // --- explicit search intent focuses the input, survives a re-render -----
  const si = dropdown.querySelector('.model-search-input');
  out.searchInputExists = !!si;
  si.focus();
  out.manualFocusActiveIsSearchInput = document.activeElement === si;
  renderModelDropdown(); // background re-render (late fetch / prefs refresh)
  const si2 = dropdown.querySelector('.model-search-input');
  out.rerenderActiveIsSearchInput = document.activeElement === si2;
  out.rerenderReplacedInputNode = !!si2 && si2 !== si;
  dropdown.focus();
  renderModelDropdown();
  out.rerenderKeepsContainerFocus = document.activeElement === dropdown;

  // --- repeated renders do not duplicate keyboard handling ---------------
  dropdown.focus();
  renderModelDropdown();
  renderModelDropdown();
  const keydownLL = dropdown._listeners['keydown'];
  out.keydownListenerCount = keydownLL ? keydownLL.list.length : 0;
  const rows = dropdown.querySelectorAll('.model-opt');
  out.visibleRowCount = rows.length;
  const ARROW = { key: 'ArrowDown', target: dropdown, preventDefault() {}, stopPropagation() {} };
  dropdown.dispatchEvent('keydown', ARROW);
  out.containerArrowDownHighlightsFirst = rows.length > 0 && rows[0].classList.contains('is-highlighted');
  // Precision probe: the SECOND press must land exactly on row 1. With stacked
  // listeners each extra wrapper re-enters the same slot handler, so one press
  // advances several rows and the highlight skips past row 1 entirely.
  dropdown.dispatchEvent('keydown', ARROW);
  out.containerTwoPressesLandOnSecond = (
    rows.length >= 4
    && rows[1].classList.contains('is-highlighted')
    && !rows[0].classList.contains('is-highlighted')
    && !rows[2].classList.contains('is-highlighted')
    && !rows[3].classList.contains('is-highlighted')
  );
  // Shared contract: the input keeps navigating too (by design), one step.
  const si3 = dropdown.querySelector('.model-search-input');
  si3.focus();
  si3.dispatchEvent('keydown', { key: 'ArrowDown', target: si3, preventDefault() {}, stopPropagation() {} });
  out.inputArrowDownSingleStep = (
    rows.length >= 4
    && rows[2].classList.contains('is-highlighted')
    && !rows[1].classList.contains('is-highlighted')
    && !rows[3].classList.contains('is-highlighted')
  );
  si3.dispatchEvent('keydown', {
    key: 'Enter', target: si3, preventDefault() {}, stopPropagation() {},
  });
  out.enterPicksHighlightedRow = !!_lastSelected && _lastSelected.value === payload.thirdModelId;
  si3.dispatchEvent('keydown', {
    key: 'Escape', target: si3, preventDefault() {}, stopPropagation() {},
  });
  out.escapeClosesDropdown = !dropdown.classList.contains('open');

  // --- close / reopen still focuses the container -------------------------
  await toggleModelDropdown();
  out.reopenActiveIsDropdown = document.activeElement === dropdown;

  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""

_FOCUS_PAYLOAD = {
    "groups": [
        {
            "provider": "OpenRouter",
            "provider_id": "openrouter",
            "models": [
                {"id": "openrouter/visible-one", "label": "Visible One"},
                {"id": "openrouter/visible-two", "label": "Visible Two"},
                {"id": "openrouter/visible-three", "label": "Visible Three"},
                {"id": "openrouter/visible-four", "label": "Visible Four"},
            ],
        }
    ],
    "selectedValue": "openrouter/visible-one",
    "configuredBadges": {},
    "secondModelId": "openrouter/visible-two",
    "thirdModelId": "openrouter/visible-three",
}


@pytest.fixture(scope="module")
def _focus_driver_out(tmp_path_factory):
    driver_dir = tmp_path_factory.mktemp("model_picker_focus_driver")
    driver_path = driver_dir / "driver.js"
    driver_path.write_text(_FOCUS_DRIVER, encoding="utf-8")
    # Same env hygiene as the rest of the webui suite: the injected node
    # channel FD must not leak into the driver subprocess.
    env = {k: v for k, v in os.environ.items() if k != "NODE_CHANNEL_FD"}
    result = subprocess.run(
        [NODE, str(driver_path), str(REPO / "static" / "ui.js"), json.dumps(_FOCUS_PAYLOAD)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"focus driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_open_focuses_the_container_not_the_search_input(_focus_driver_out):
    out = _focus_driver_out
    assert out["dropdownHasOpenClass"], "open path did not run"
    assert out["dropdownHasTabindex"], (
        "container must stay programmatically focusable (tabindex=-1); without it focus() is a no-op"
    )
    assert out["openActiveIsDropdown"], "initial open must focus the dropdown container"
    assert not out["openActiveIsSearchInput"], "initial open must NOT focus the search input (IME pop)"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_explicit_search_intent_focuses_the_input(_focus_driver_out):
    out = _focus_driver_out
    assert out["searchInputExists"], "harness sanity: search input must exist after open"
    assert out["manualFocusActiveIsSearchInput"], (
        "explicit focus() on the search input must land on the input — a user who wants to search still can"
    )
    # A background re-render replaces the input node; focus must follow to the
    # NEW node or a user mid-word gets silently dropped to <body>.
    assert out["rerenderReplacedInputNode"], "harness sanity: the re-render really replaced the input node"
    assert out["rerenderActiveIsSearchInput"], "re-render must preserve search-input focus (typing user)"
    assert out["rerenderKeepsContainerFocus"], "re-render must preserve container focus (arrow-key user)"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_repeated_renders_do_not_duplicate_keyboard_handling(_focus_driver_out):
    out = _focus_driver_out
    assert out["keydownListenerCount"] == 1, (
        f"expected exactly one container keydown listener after repeated renders, "
        f"got {out['keydownListenerCount']} — duplicate bindings stack on the surviving container"
    )
    assert out["visibleRowCount"] >= 4, "harness sanity: need >=4 rows to observe single-step navigation"
    # One ArrowDown must highlight the first row; the second must land exactly
    # on the second. Each stacked listener wrapper would re-enter the same slot
    # handler and skip rows — the highlight would fly past row 1 entirely.
    assert out["containerArrowDownHighlightsFirst"], "ArrowDown on the container must highlight the first row"
    assert out["containerTwoPressesLandOnSecond"], (
        "two ArrowDown presses must land exactly on the second row (stacked listeners skip rows)"
    )
    assert out["inputArrowDownSingleStep"], "ArrowDown from the search input must advance exactly one row"
    assert out["enterPicksHighlightedRow"], "Enter must pick the highlighted row"
    assert out["escapeClosesDropdown"], "Escape must close the dropdown"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_close_and_reopen_still_focuses_the_container(_focus_driver_out):
    out = _focus_driver_out
    assert out["reopenActiveIsDropdown"], "reopening after close must focus the container again"
