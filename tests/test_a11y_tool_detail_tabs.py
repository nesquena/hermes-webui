"""The Full/Output tabs in tool cards must say which one is selected.

Measured defect (18.08.2026): the bar has role="tablist" and two role="tab", but the
selected state existed ONLY as the CSS class `.active`. A screen reader therefore
said "Output tab" without saying whether it is on - information available only to
a sighted user (WCAG 4.1.2). Also missing: aria-controls, a name for the bar, and
the arrow-key navigation the tab role requires (ARIA APG: Tabs).

This is the THIRD copy of the same pattern in this file - the bar is created in
three places (twice via createElement, once in the card's HTML template). Hence a
SINGLE function `_syncTransparentDetailTabsA11y` declares the state, called from
every path, rather than attributes duplicated across three templates: otherwise a
fourth copy would be born mute again.

The tests check BEHAVIOUR (they execute the code in node) rather than the presence
of text in the source, because the fix is about WHEN the attributes appear - a grep
over the source would also pass on code that sets them only after a click.

Two traps of this fix, pinned down here for good:
1. INITIAL STATE. Before anyone clicks, the bar must already be declared - the
   first version of the previous fix (Settings > Extensions) called the helper
   after an early `return`, so the contract never came into being at all.
2. THE TEMPLATE PATH. When the detail arrives as ready HTML that ALREADY contains
   the bar, the "append the bar" condition is false - and that is exactly the bar
   without a declared state. The sync must sit OUTSIDE that condition.
"""

from pathlib import Path
import json
import re
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")

NODE = shutil.which("node")

# Scenario and assertions executed in Node: a minimal DOM stub + REAL functions
# extracted from ui.js source (not copies, so the test cannot diverge from the code).
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function mkEl(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(),
    children: [], attrs: {}, classes: new Set(), dataset: {}, id: '',
    textContent: '', parentNode: null, listeners: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c) { c.parentNode = this; this.children.unshift(c); return c; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    get firstChild() { return this.children[0] || null; },
    get classList() {
      const s = this.classes;
      return { add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
               toggle: (c, on) => { if (on === undefined) { s.has(c) ? s.delete(c) : s.add(c); }
                                    else if (on) s.add(c); else s.delete(c); } };
    },
    descendants() { let o = []; for (const c of this.children) { o.push(c); o = o.concat(c.descendants()); } return o; },
    matches(sel) {
      if (sel === '[role="tab"]') return this.getAttribute('role') === 'tab';
      if (sel === '.transparent-detail-mode') return this.classes.has('transparent-detail-mode');
      if (sel === '.transparent-detail-modes') return this.classes.has('transparent-detail-modes');
      if (sel === '.tool-card-detail') return this.classes.has('tool-card-detail');
      if (sel === '.transparent-event-row') return this.classes.has('transparent-event-row');
      throw new Error('atrapa nie zna selektora ' + sel);
    },
    querySelectorAll(sel) { return this.descendants().filter((d) => d.matches(sel)); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    closest(sel) { let n = this; while (n) { if (n.matches && n.matches(sel)) return n; n = n.parentNode; } return null; },
    focus() { ctx.document.activeElement = this; },
    click() { if (this._onclick) this._onclick(); },
  };
}

const ctx = {
  window: {},
  document: { activeElement: null, getElementById: () => null, createElement: (t) => mkEl(t),
              querySelectorAll: () => [], querySelector: () => null,
              addEventListener: () => {}, readyState: 'complete', body: mkEl('body') },
  console, Math, JSON, String, Number, Boolean, Array, Object, Date, RegExp, Set, Map,
  requestAnimationFrame: (fn) => fn(),
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  MutationObserver: function () { return { observe() {}, disconnect() {} }; },
  location: { href: 'http://127.0.0.1/' },
};
ctx.window.document = ctx.document;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(A11Y_PATH, 'utf8'), ctx);

const uiSrc = fs.readFileSync(UI_PATH, 'utf8');
function wytnij(name) {
  const start = uiSrc.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing function ' + name + ' in ui.js');
  let g = 0;
  for (let j = uiSrc.indexOf('{', start); j < uiSrc.length; j++) {
    if (uiSrc[j] === '{') g++;
    else if (uiSrc[j] === '}') { g--; if (!g) return uiSrc.slice(start, j + 1); }
  }
  throw new Error('did not close ' + name);
}
vm.runInContext(wytnij('_syncTransparentDetailTabsA11y'), ctx);
vm.runInContext(wytnij('_setTransparentDetailMode'), ctx);

function scena() {
  const row = mkEl('div'); row.classList.add('transparent-event-row');
  const detail = mkEl('div'); detail.classList.add('tool-card-detail');
  detail.setAttribute('data-transparent-detail-mode', 'full');
  const modes = mkEl('div'); modes.classList.add('transparent-detail-modes');
  modes.setAttribute('role', 'tablist');
  const full = mkEl('span');
  full.classList.add('transparent-detail-mode'); full.classList.add('active');
  full.setAttribute('role', 'tab'); full.setAttribute('data-mode', 'full'); full.textContent = 'Full';
  const output = mkEl('span');
  output.classList.add('transparent-detail-mode');
  output.setAttribute('role', 'tab'); output.setAttribute('data-mode', 'output'); output.textContent = 'Output';
  modes.appendChild(full); modes.appendChild(output);
  detail.appendChild(modes); row.appendChild(detail);
  return { row, detail, modes, full, output };
}

const out = {};
let s = scena();
ctx._syncTransparentDetailTabsA11y(s.detail, 'full');
out.start = {
  fullSelected: s.full.getAttribute('aria-selected'),
  outputSelected: s.output.getAttribute('aria-selected'),
  tablistNazwa: !!(s.modes.getAttribute('aria-label') || s.modes.getAttribute('aria-labelledby')),
  controlsWskazujePanel: s.full.getAttribute('aria-controls') === s.detail.id && !!s.detail.id,
  panelRola: s.detail.getAttribute('role'),
  panelNazwanyAktywna: s.detail.getAttribute('aria-labelledby') === s.full.id,
  tabindexFull: s.full.getAttribute('tabindex'),
  tabindexOutput: s.output.getAttribute('tabindex'),
};

ctx._setTransparentDetailMode(s.output, 'output');
out.afterSwitch = {
  fullSelected: s.full.getAttribute('aria-selected'),
  outputSelected: s.output.getAttribute('aria-selected'),
  ariaMatchesCss: s.output.classList.contains('active') === (s.output.getAttribute('aria-selected') === 'true')
                  && s.full.classList.contains('active') === (s.full.getAttribute('aria-selected') === 'true'),
  panelNamedOutput: s.detail.getAttribute('aria-labelledby') === s.output.id,
  tabindexFull: s.full.getAttribute('tabindex'),
  tabindexOutput: s.output.getAttribute('tabindex'),
  mode: s.detail.getAttribute('data-transparent-detail-mode'),
};

ctx._setTransparentDetailMode(s.full, 'full');
out.powrot = {
  fullSelected: s.full.getAttribute('aria-selected'),
  panelNazwanyFull: s.detail.getAttribute('aria-labelledby') === s.full.id,
};

const before = (s.modes.listeners.keydown || []).length;
ctx._syncTransparentDetailTabsA11y(s.detail, 'full');
ctx._syncTransparentDetailTabsA11y(s.detail, 'full');
out.idempotencja = {
  listenersBefore: before,
  listenersAfter: (s.modes.listeners.keydown || []).length,
  fullSelected: s.full.getAttribute('aria-selected'),
};

s = scena();
ctx._syncTransparentDetailTabsA11y(s.detail, 'full');
s.output._onclick = () => ctx._setTransparentDetailMode(s.output, 'output');
ctx.document.activeElement = s.full;
const keydown = (s.modes.listeners.keydown || [])[0];
out.arrows = { listener: typeof keydown === 'function' };
if (typeof keydown === 'function') {
  keydown({ key: 'ArrowRight', preventDefault() {}, stopPropagation() {} });
  out.arrows.focusOnOutput = ctx.document.activeElement === s.output;
  out.arrows.modeAfterArrow = s.detail.getAttribute('data-transparent-detail-mode');
}

const s2 = scena();
const zapas = ctx.a11yTablist;
ctx.a11yTablist = undefined;
ctx._syncTransparentDetailTabsA11y(s2.detail, 'output');
out.bezHelpera = {
  outputSelected: s2.output.getAttribute('aria-selected'),
  fullSelected: s2.full.getAttribute('aria-selected'),
};
ctx.a11yTablist = zapas;

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    """Runs the contract in Node and returns the measured behavior."""
    if not NODE:
        pytest.skip("node unavailable - cannot measure behavior")
    folder = tmp_path_factory.mktemp("tabs")
    script = folder / "harness.js"
    script.write_text(
        f"const A11Y_PATH = {json.dumps(str(REPO / 'static' / 'a11y-helpers.js'))};\n"
        f"const UI_PATH = {json.dumps(str(REPO / 'static' / 'ui.js'))};\n" + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"harness crashed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestInitialState:
    """The most common case: a screen reader user reads the card and nobody has clicked anything."""

    def test_selected_tab_is_declared(self, behaviour):
        s = behaviour["start"]
        assert s["fullSelected"] == "true", (
            "without aria-selected, the screen reader will not say which view is selected"
        )
        assert s["outputSelected"] == "false"

    def test_tab_bar_has_a_name(self, behaviour):
        assert behaviour["start"]["tablistNazwa"], (
            "an unnamed set of tabs sounds like a group of loose controls in a screen reader"
        )

    def test_tabs_point_at_the_panel(self, behaviour):
        s = behaviour["start"]
        assert s["controlsWskazujePanel"], "missing aria-controls"
        assert s["panelRola"] == "tabpanel"
        assert s["panelNazwanyAktywna"], "the panel must be named by the active tab"

    def test_the_set_is_traversed_with_arrows_not_tab(self, behaviour):
        s = behaviour["start"]
        assert (s["tabindexFull"], s["tabindexOutput"]) == ("0", "-1"), (
            "roving tabindex: only the active tab is in the Tab order"
        )


class TestAfterSwitching:
    def test_selected_state_moves_to_the_new_tab(self, behaviour):
        p = behaviour["afterSwitch"]
        assert p["outputSelected"] == "true"
        assert p["fullSelected"] == "false"

    def test_aria_does_not_drift_from_the_visuals(self, behaviour):
        assert behaviour["afterSwitch"]["ariaMatchesCss"], (
            "what is visible (the .active class) and what is announced (aria-selected) "
            "must be the same state"
        )

    def test_shared_panel_renames_to_the_active_tab(self, behaviour):
        assert behaviour["afterSwitch"]["panelNamedOutput"], (
            "both views share one container; if the name stayed on 'Full', "
            "the screen reader would lie about what it shows after moving to 'Output'"
        )

    def test_keyboard_focus_follows_the_selection(self, behaviour):
        p = behaviour["afterSwitch"]
        assert (p["tabindexFull"], p["tabindexOutput"]) == ("-1", "0")

    def test_the_view_actually_switches(self, behaviour):
        assert behaviour["afterSwitch"]["mode"] == "output", (
            "the accessibility fix must not break view switching itself"
        )

    def test_returning_to_the_first_tab_works(self, behaviour):
        assert behaviour["powrot"]["fullSelected"] == "true"
        assert behaviour["powrot"]["panelNazwanyFull"]


class TestArrowKeyNavigation:
    """Arrow keys are part of the tab role contract (ARIA APG: Tabs)."""

    def test_the_keyboard_listener_is_attached(self, behaviour):
        assert behaviour["arrows"]["listener"]

    def test_arrow_key_moves_focus_and_switches(self, behaviour):
        assert behaviour["arrows"]["focusOnOutput"]
        assert behaviour["arrows"]["modeAfterArrow"] == "output", (
            "expected automatic activation: the arrow key switches the view immediately"
        )


class TestRobustness:
    def test_repeated_call_does_not_duplicate_listeners(self, behaviour):
        i = behaviour["idempotencja"]
        assert i["listenersAfter"] == i["listenersBefore"], (
            "synchronization is called from several render paths, so it must "
            "be idempotent - otherwise one Arrow-key press will fire N times"
        )
        assert i["fullSelected"] == "true"

    def test_selected_state_still_appears_without_the_helper(self, behaviour):
        b = behaviour["bezHelpera"]
        assert b["outputSelected"] == "true" and b["fullSelected"] == "false", (
            "when the browser has an old cache without a11yTablist, the fallback path "
            "must still declare which tab is selected"
        )


class TestEveryRenderPath:
    """The tab bar is created in 3 places - each one must declare state."""

    def test_sync_is_called_from_every_path(self):
        calls = UI_JS.count("_syncTransparentDetailTabsA11y(")
        # 1 definition + 1 from the switcher + 2 render paths
        assert calls >= 4, (
            f"only {calls} occurrences - the tab bar is created in several places, "
            "each one must declare state, otherwise some cards will be mute"
        )

    def test_sync_sits_outside_the_tab_bar_append_condition(self):
        """The ready-template path does not enter the 'append tab bar' block.

        When the detail comes from HTML that ALREADY has the bar, the condition
        `!detail.querySelector('.transparent-detail-modes')` is false -
        and that is exactly the bar that has no declared state.
        """
        for blok in re.findall(
            r"if\(detail&&!detail\.querySelector\('\.transparent-detail-modes'\)\)\{"
            r"(.*?)\n(\s*)\}", UI_JS, re.S
        ):
            assert "_syncTransparentDetailTabsA11y" not in blok[0], (
                "synchronization cannot live ONLY inside the tab-bar append "
                "condition - the template path would skip it"
            )
        assert re.search(
            r"if\(detail\)\{\s*\n\s*_syncTransparentDetailTabsA11y\(", UI_JS
        ), "missing unconditional synchronization for render paths"


class TestTranslations:
    def test_tab_bar_name_exists_in_every_locale(self):
        occurrences = I18N_JS.count("tool_detail_tabs_aria")
        assert occurrences >= 15, (
            f"key in {occurrences} locales - the repo requires coverage in all 15"
        )

    def test_helper_uses_the_translation_key(self):
        idx = UI_JS.find("function _syncTransparentDetailTabsA11y(")
        assert idx > 0
        assert "tool_detail_tabs_aria" in UI_JS[idx:idx + 1200], (
            "the tab bar name must be translated, not hardcoded in English"
        )
