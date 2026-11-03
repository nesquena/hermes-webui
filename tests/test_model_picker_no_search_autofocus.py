"""Opening the model picker must focus the right thing for the device.

Why: the picker is a CHOOSER. On a phone, grabbing the search box pops the
on-screen keyboard the instant the chip is tapped, so the model list sits behind
the IME. On a desktop, the opposite is true (#8530): a user expects to type the
model name immediately, and IME input needs a real text field to compose into.

One rule, both pickers (_modelPickerFocusTarget):
  * device with a keyboard in play (desktop / tablet + hardware keyboard) ->
    the search input takes focus AFTER the dropdown is open and positioned;
  * touch-only device -> the dropdown container takes focus, so no IME appears.

The container is still a real focus target, and a printable character typed
while it holds focus is redirected into the search box instead of reaching the
global j/k session switcher (which would change the chat behind the open menu).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


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
    # The one surviving focus call hands focus to the NEW input after a rebuild
    # (the pre-render node is detached); it is not a first-open autofocus.
    assert "if(_autoFocusSearch||_hadFocus||_searchHadFocusBefore){" in body
    assert "_siNow.focus()" in body


def test_no_call_site_passes_the_autofocus_option():
    # The option is gone from the API; a caller still passing it is dead plumbing
    # that would silently stop working if the renderer ever read it again.
    assert "autoFocusSearch:" not in UI_JS
    assert "autoFocusSearch!==false" not in UI_JS


def test_focus_target_depends_on_the_device_not_the_call_site():
    helper = _function_body(UI_JS, "_modelPickerFocusTarget")
    prefers = _function_body(UI_JS, "_modelPickerPrefersSearchFocus")
    # boot.js owns device detection (#3076): reuse it instead of a second
    # coarse-pointer sniff inside the picker.
    assert "_isTouchOnlyDevice" in prefers
    assert "matchMedia" not in prefers
    assert "querySelector('.model-search-input')" in helper
    # Touch-only falls back to the container, which stays focusable.
    assert "return (typeof dd.focus==='function')?dd:null;" in helper
    # Both open paths go through the helper, AFTER the dropdown is shown —
    # focusing a display:none node is a no-op (the #8530 report).
    toggle = _function_body(UI_JS, "toggleModelDropdown")
    assert "_focusModelPicker(dd);" in toggle
    assert toggle.index("dd.classList.add('open');") < toggle.index("_focusModelPicker(dd);")
    assert toggle.index("_positionModelDropdown();") < toggle.index("_focusModelPicker(dd);")
    settings = _function_body(UI_JS, "openSettingsModelDropdown")
    assert "_focusModelPicker(dd)" in settings
    assert settings.index("dd.classList.add('open');") < settings.index("_focusModelPicker(dd)")


def test_dropdown_container_is_focusable_and_labelled():
    body = _function_body(UI_JS, "renderModelDropdown")
    # A plain <div> is not focusable — focus() on it is a silent no-op, which
    # would leave arrow-key navigation dead on touch-only devices.
    assert "dd.setAttribute('tabindex','-1')" in body
    # And a focused node with no name makes a screen reader announce nothing.
    assert "dd.setAttribute('role','group')" in body
    assert "dd.setAttribute('aria-label',_pickerLabel)" in body
    assert "const _pickerLabel=opts.label||'Model picker';" in body
    # The announced name is the picker's EXISTING label, not an invented string.
    assert "renderModelDropdown({label:'Conversation model'})" in UI_JS
    assert "label:t('settings_label_model')" in UI_JS


def test_container_focus_ring_is_deliberately_suppressed():
    # Decided on purpose: the highlighted row already shows the keyboard
    # position, so Chromium's default ring around the whole menu is noise.
    assert ".model-dropdown:focus-visible{outline:none;}" in STYLE_CSS


def test_model_dropdown_is_treated_as_an_interactive_swipe_target():
    # The global j/k session switcher and the PWA sidebar gesture both consult
    # this predicate; without .model-dropdown a key/swipe inside the open menu
    # acts on the page behind it.
    body = _function_body(BOOT_JS, "_isInteractiveSwipeTarget")
    assert ".model-dropdown" in body
    assert ".img-lightbox" in body  # pre-existing selector must survive


def test_printable_key_on_the_container_is_redirected_into_search():
    body = _function_body(UI_JS, "renderModelDropdown")
    assert "if(e.target!==dd) return;" in body
    # Printable only: navigation keys and shortcuts keep their behaviour.
    assert "e.key.length===1" in body
    assert "!e.ctrlKey&&!e.metaKey&&!e.altKey" in body
    assert "e.preventDefault();" in body
    assert "e.stopPropagation()" in body
    assert "si.dispatchEvent(new Event('input',{bubbles:true}))" in body


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
// Device rule under test: boot.js owns it in production, and the picker must not
// grow a second sniffer. Flipped per phase below.
let __touchOnly = true;

function _isTouchOnlyDevice() { return __touchOnly; }

function detachTree(node) {
  node._attached = false;
  for (const child of node.children || []) detachTree(child);
}

function attachTree(node, attached) {
  node._attached = attached;
  for (const child of node.children || []) attachTree(child, attached);
}

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
    _attached: false,
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
      // Attach the whole subtree: a child of an in-document node is in document
      // too, which is exactly the browser rule the focus() guard relies on.
      attachTree(child, !!this._attached);
      this.children.push(child);
      return child;
    },
    addEventListener(type, handler) {
      if (!this._listeners[type]) this._listeners[type] = makeListenerList();
      this._listeners[type].list.push(handler);
    },
    dispatchEvent(typeOrEvent, maybeEvent) {
      // Browser-shaped dispatch: either (type, event), as the older driver code
      // calls it, or a single Event object whose .type names the listeners — the
      // production code dispatches `new Event('input',{bubbles:true})` and a
      // harness that only understood (type, event) would silently swallow it.
      let type;
      let event;
      if (typeof typeOrEvent === 'string') { type = typeOrEvent; event = maybeEvent || {}; }
      else if (typeOrEvent && typeof typeOrEvent === 'object') { type = typeOrEvent.type; event = typeOrEvent; }
      if (!type) return;
      const ll = this._listeners[type];
      if (!ll) return;
      for (const fn of ll.list) fn(event);
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
    getAttribute(name) { return this.hasAttribute(name) ? String(this[name]) : null; },
    removeAttribute(name) { if (name === 'tabindex') this._focusable = false; delete this[name]; },
    // Detachment matters: a full re-render replaces the input node, and a browser
    // ignores focus() on a node that is no longer in the document. Without this
    // the harness would keep "focusing" the stale node and hide the bug.
    focus() { if (!this._focusable || !this._attached) return; this._focused = true; _activeElement = this; },
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
      // Children built as HTML strings are replaced wholesale: detach the old
      // subtree so focus() on a stale node behaves like a browser (no-op).
      for (const child of this.children) detachTree(child);
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
// Attach the roots: everything appended under them counts as in-document, so a
// node replaced by a re-render is the only thing that becomes unfocusable.
dropdown._attached = true;
chip._attached = true;
mobileAction._attached = true;
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
  '_modelPickerPrefersSearchFocus',
  '_modelPickerFocusTarget',
  '_focusModelPicker',
  'renderModelDropdown',
  'toggleModelDropdown',
]) {
  eval(extractFunc(name));
}

(async () => {
  const out = {};

  // ===== touch-only device: open focuses the CONTAINER ====================
  // A phone must not raise the IME over the list, so the container takes focus
  // (no text field focused => no software keyboard).
  __touchOnly = true;
  await toggleModelDropdown();
  out.dropdownHasOpenClass = dropdown.classList.contains('open');
  out.dropdownHasTabindex = dropdown.hasAttribute('tabindex');
  out.dropdownRole = dropdown.getAttribute('role');
  out.dropdownAriaLabel = dropdown.getAttribute('aria-label');
  out.openActiveIsDropdown = document.activeElement === dropdown;
  out.openActiveIsSearchInput = !!(
    document.activeElement && document.activeElement.classList.contains('model-search-input')
  );

  // ===== printable key on the container goes to the search box ============
  // This is the regression: the key used to bubble to the global j/k session
  // switcher, changing the chat behind the open menu, while the filter stayed
  // empty.
  let keyDownPrevented = false;
  let keyDownStopped = false;
  dropdown.dispatchEvent('keydown', {
    key: 'k', target: dropdown,
    preventDefault() { keyDownPrevented = true; },
    stopPropagation() { keyDownStopped = true; },
  });
  const siAfterKey = dropdown.querySelector('.model-search-input');
  out.printableKeyPrevented = keyDownPrevented;
  out.printableKeyStopped = keyDownStopped;
  out.printableKeyActiveIsSearchInput = document.activeElement === siAfterKey;
  out.printableKeyLandedInValue = !!siAfterKey && siAfterKey.value === 'k';
  // The dispatched `input` event must run the REAL filter — 'k' matches nothing
  // in this payload, so every row disappears.
  out.printableKeyFilteredRows = dropdown.querySelectorAll('.model-opt').length === 0;

  // clear the term so the navigation checks below have rows again
  siAfterKey.value = '';
  siAfterKey.dispatchEvent(new Event('input', { bubbles: true }));
  out.clearRestoredRows = dropdown.querySelectorAll('.model-opt').length >= 4;

  // ===== desktop: reopen focuses the SEARCH input =========================
  // Type immediately (#8530), and IME composition has a real text field.
  __touchOnly = false;
  siAfterKey.dispatchEvent('keydown', {
    key: 'Escape', target: siAfterKey, preventDefault() {}, stopPropagation() {},
  });
  out.escapeFromInputCloses = !dropdown.classList.contains('open');
  await toggleModelDropdown();
  const si = dropdown.querySelector('.model-search-input');
  out.searchInputExists = !!si;
  out.desktopOpenActiveIsSearchInput = document.activeElement === si;

  // ===== explicit search intent survives a re-render ======================
  si.focus();
  out.manualFocusActiveIsSearchInput = document.activeElement === si;
  renderModelDropdown(); // top-level re-render (late fetch / prefs refresh)
  const si2 = dropdown.querySelector('.model-search-input');
  out.rerenderReplacedInputNode = !!si2 && si2 !== si;
  out.rerenderActiveIsSearchInput = document.activeElement === si2;

  // ===== repeated renders do not duplicate keyboard handling ==============
  dropdown.focus();
  renderModelDropdown();
  renderModelDropdown();
  out.rerenderKeepsContainerFocus = document.activeElement === dropdown;
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

  // ===== close / reopen follows the device rule again =====================
  __touchOnly = false;
  await toggleModelDropdown();
  out.reopenDesktopActiveIsSearchInput = (
    document.activeElement === dropdown.querySelector('.model-search-input')
  );
  dropdown.dispatchEvent('keydown', {
    key: 'Escape', target: dropdown, preventDefault() {}, stopPropagation() {},
  });
  __touchOnly = true;
  await toggleModelDropdown();
  out.reopenTouchActiveIsContainer = document.activeElement === dropdown;

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
def test_touch_only_open_focuses_the_container_not_the_search_input(_focus_driver_out):
    out = _focus_driver_out
    assert out["dropdownHasOpenClass"], "open path did not run"
    assert out["dropdownHasTabindex"], (
        "container must stay programmatically focusable (tabindex=-1); without it focus() is a no-op"
    )
    assert out["openActiveIsDropdown"], "touch-only open must focus the dropdown container"
    assert not out["openActiveIsSearchInput"], "touch-only open must NOT focus the search input (IME pop)"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_container_is_announced_by_a_screen_reader(_focus_driver_out):
    out = _focus_driver_out
    assert out["dropdownRole"] == "group", "the focus target needs a role, or a screen reader says nothing"
    assert out["dropdownAriaLabel"] == "Conversation model", (
        "the announced name must be the picker's existing label, not an invented string"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_printable_key_on_container_lands_in_search_and_is_not_a_shortcut(_focus_driver_out):
    out = _focus_driver_out
    # The regression: a printable key bubbled to the global j/k session switcher
    # and changed the conversation behind the open menu.
    assert out["printableKeyPrevented"], "a printable key on the container must preventDefault()"
    assert out["printableKeyStopped"], (
        "it must stopPropagation() too — otherwise sessions.js still runs the j/k handler"
    )
    assert out["printableKeyActiveIsSearchInput"], "focus must move into the search box"
    assert out["printableKeyLandedInValue"], "the character must be inserted into the search value"
    assert out["printableKeyFilteredRows"], "the dispatched input event must run the real filter"
    assert out["clearRestoredRows"], "clearing the query must restore the rows (harness sanity)"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_desktop_open_focuses_the_search_input(_focus_driver_out):
    out = _focus_driver_out
    assert out["escapeFromInputCloses"], "Escape from the input must close the picker"
    assert out["desktopOpenActiveIsSearchInput"], (
        "on a device with a keyboard the search box must take focus so the first "
        "character (and IME input) works immediately (#8530)"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_explicit_search_intent_focuses_the_input(_focus_driver_out):
    out = _focus_driver_out
    assert out["searchInputExists"], "harness sanity: search input must exist after open"
    assert out["manualFocusActiveIsSearchInput"], (
        "explicit focus() on the search input must land on the input — a user who wants to search still can"
    )
    # A background re-render replaces the input node; focus must follow to the
    # NEW node or a user mid-word gets silently dropped to <body>. The harness
    # refuses focus() on a detached node, so focusing the stale one cannot pass.
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
def test_close_and_reopen_follows_the_device_rule(_focus_driver_out):
    out = _focus_driver_out
    assert out["reopenDesktopActiveIsSearchInput"], (
        "reopening on a device with a keyboard must focus the search box again"
    )
    assert out["reopenTouchActiveIsContainer"], (
        "reopening on a touch-only device must focus the container again (no IME)"
    )
