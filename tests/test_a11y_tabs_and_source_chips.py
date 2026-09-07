"""Tabs and source chips must say the same thing they show.

Two findings measured 18.08.2026 on the running application (Edge + CDP), both
from the same family: the information existed ONLY as appearance.

1. The Settings > Extensions bar had role="tablist" and three role="tab", but ZERO
   aria-selected, ZERO aria-controls and a tablist without a name. The active tab
   was encoded only in a CSS class (extensions-tab-active), so a screen reader
   said "Gallery tab" without saying WHICH one is current. WCAG 4.1.2.
   The neighbouring bar (workspace-panel-tabs) was correct in HTML — so the defect
   is TWO COPIES OF THE SAME PATTERN DRIFTING APART, not missing knowledge. Hence
   the fix introduces a shared a11yTablist helper instead of adding attributes in
   a third place, and adds the arrow-key navigation the tab role implies (ARIA APG).

2. Conversation source chips (9px) had contrast below the 4.5:1 threshold for
   EVERY one of the four brand colours. Measured on a real list row:
   telegram 2.92:1, discord 2.99:1, slack 1.10:1, claude_code 3.40:1.
   Slack at 1.1:1 was practically indistinguishable from the background. The fix
   lightens each brand hue by exactly as much as that hue needs, instead of
   flattening everything to grey — the chip still reads as "Telegram blue".

Evidence after the fix (same apparatus): contrast 4.76 / 4.66 / 4.98 / 4.76,
the full tab contract from the FIRST entry into the section (without clicking),
right arrow moves focus Gallery -> Installed and switches the panel, exactly one
tab has aria-selected=true and exactly one is in the Tab order.
"""

from pathlib import Path

import json
import re
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(SRC, 'utf8');

function makeEl(tag){
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    _attrs: {}, _classes: new Set(), children: [], parentElement: null,
    id: '', textContent: '', hidden: false, _focused: false,
    _listeners: {},
    setAttribute(k, v){ this._attrs[k] = String(v); },
    getAttribute(k){ return k in this._attrs ? this._attrs[k] : null; },
    hasAttribute(k){ return k in this._attrs; },
    removeAttribute(k){ delete this._attrs[k]; },
    classList: null,
    appendChild(c){ c.parentElement = this; this.children.push(c); return c; },
    addEventListener(typ, fn){ (this._listeners[typ] = this._listeners[typ] || []).push(fn); },
    dispatch(typ, ev){ (this._listeners[typ] || []).forEach(fn => fn(ev)); },
    focus(){ ctx.document.activeElement = this; },
    click(){ this.dispatch('click', {}); if (this._onclick) this._onclick(); },
    querySelectorAll(sel){ return descendants(this).filter(e => matches(e, sel)); },
    querySelector(sel){ return this.querySelectorAll(sel)[0] || null; },
    closest(sel){ let e = this; while (e) { if (matches(e, sel)) return e; e = e.parentElement; } return null; },
    dataset: {},
  };
  el.classList = {
    add: c => el._classes.add(c), remove: c => el._classes.delete(c),
    contains: c => el._classes.has(c),
    toggle: (c, on) => { if (on) el._classes.add(c); else el._classes.delete(c); },
  };
  Object.defineProperty(el, 'className', {
    get(){ return Array.from(el._classes).join(' '); },
    set(v){ el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}
function descendants(root){
  const out = [];
  (function walk(n){ n.children.forEach(c => { out.push(c); walk(c); }); })(root);
  return out;
}
function matches(el, sel){
  return String(sel).split(',').map(s => s.trim()).some(one => {
    let m = one.match(/^\[role="([^"]+)"\]$/);
    if (m) return el.getAttribute('role') === m[1];
    m = one.match(/^\.([\w-]+)$/);
    if (m) return el._classes.has(m[1]);
    m = one.match(/^\[([\w-]+)="([^"]+)"\]$/);
    if (m) return el.getAttribute(m[1]) === m[2];
    m = one.match(/^\[([\w-]+)\]$/);
    if (m) return el.hasAttribute(m[1]);
    return el.tagName === one.toUpperCase();
  });
}
const root = makeEl('body');
const ctx = {
  console, Date, Math,
  setInterval(){ return 1; }, clearInterval(){}, setTimeout(){ return 1; }, clearTimeout(){},
  document: {
    body: root, activeElement: null, readyState: 'complete',
    getElementById(id){ return descendants(root).find(e => e.id === id) || null; },
    querySelector(sel){ return descendants(root).filter(e => matches(e, sel))[0] || null; },
    querySelectorAll(sel){ return descendants(root).filter(e => matches(e, sel)); },
    createElement(tag){ return makeEl(tag); },
    addEventListener(){},
  },
  window: { addEventListener(){} },
  location: { pathname: '/' },
  t(k){ return k; },
  fetch(){ return Promise.reject(new Error('no network')); },
  __makeEl: makeEl, __root: root,
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const out = vm.runInContext(SCENARIO, ctx);
console.log(JSON.stringify(out));
"""


def _run_js(scenario):
    if not NODE:
        pytest.skip("node niedostepny")
    script = (
        HARNESS
        .replace("SRC", json.dumps(str(REPO / "static" / "a11y-helpers.js")))
        .replace("SCENARIO", json.dumps(scenario))
    )
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, f"node failed: {r.stderr[-2500:]}"
    return json.loads(r.stdout)


BUDOWA_PASKA = """
  const bar = __makeEl('div');
  bar.setAttribute('role', 'tablist');
  __root.appendChild(bar);
  const keys = ['gallery', 'installed', 'diagnostics'];
  const panele = {};
  keys.forEach(k => {
    const tab = __makeEl('button');
    tab.setAttribute('role', 'tab');
    tab.dataset.extensionsTab = k;
    tab.textContent = k;
    bar.appendChild(tab);
    const panel = __makeEl('div');
    panel.dataset.extensionsPane = k;
    __root.appendChild(panel);
    panele[k] = panel;
  });
  const options = (activeKey) => ({
    label: 'Extension views', activeKey: activeKey,
    keyOf: b => b.dataset.extensionsTab,
    panelFor: b => panele[b.dataset.extensionsTab],
  });
"""


class TestTablistHelperDeclaresTheWholeContract:

    def test_every_tab_declares_selected_state(self):
        """Without aria-selected, the screen reader does not know which tab is current."""
        out = _run_js("(() => {" + BUDOWA_PASKA + """
          a11yTablist(bar, options('gallery'));
          const tabs = bar.querySelectorAll('[role="tab"]');
          return {
            states: tabs.map(x => x.getAttribute('aria-selected')),
            name: bar.getAttribute('aria-label'),
            controls: tabs.map(x => x.getAttribute('aria-controls')),
            roleP: tabs.map(x => document.getElementById(x.getAttribute('aria-controls')).getAttribute('role')),
          };
        })()""")
        assert out["states"] == ["true", "false", "false"], (
            "Exactly one tab is current, and EVERY tab must declare its state."
        )
        assert out["name"], "an unnamed tablist does not say what this set is"
        assert all(out["controls"]), "each tab must point to its panel"
        assert out["roleP"] == ["tabpanel"] * 3

    def test_roving_tabindex_keeps_one_stop_in_tab_order(self):
        """A keyboard user must not have to go through EVERY tab."""
        out = _run_js("(() => {" + BUDOWA_PASKA + """
          a11yTablist(bar, options('installed'));
          return {ti: bar.querySelectorAll('[role="tab"]').map(x => x.getAttribute('tabindex'))};
        })()""")
        assert out["ti"] == ["-1", "0", "-1"], (
            "Only the current tab is in the Tab order; the set is traversed with arrow keys."
        )

    def test_arrow_keys_move_and_activate(self):
        """Arrow keys are part of the tab role contract (ARIA APG: Tabs)."""
        out = _run_js("(() => {" + BUDOWA_PASKA + """
          a11yTablist(bar, options('gallery'));
          const tabs = bar.querySelectorAll('[role="tab"]');
          let klikniety = null;
          tabs.forEach(x => x.addEventListener('click', () => { klikniety = x.dataset.extensionsTab; }));
          tabs[0].focus();
          let zablokowane = false;
          bar.dispatch('keydown', {key: 'ArrowRight', preventDefault(){ zablokowane = true; }, stopPropagation(){}});
          const poPrawo = document.activeElement.dataset.extensionsTab;
          bar.dispatch('keydown', {key: 'Home', preventDefault(){}, stopPropagation(){}});
          const poHome = document.activeElement.dataset.extensionsTab;
          tabs[0].focus();
          bar.dispatch('keydown', {key: 'ArrowLeft', preventDefault(){}, stopPropagation(){}});
          const poLewo = document.activeElement.dataset.extensionsTab;
          return {poPrawo, poHome, poLewo, klikniety, zablokowane};
        })()""")
        assert out["poPrawo"] == "installed", "Right Arrow moves to the next tab"
        assert out["poLewo"] == "diagnostics", "from the first tab, Left Arrow wraps to the last tab"
        assert out["poHome"] == "gallery"
        assert out["klikniety"], (
            "An arrow-key move must SWITCH the panel — in this app, click performs "
            "the switch, so changing focus alone would leave ARIA and the view out of sync."
        )
        assert out["zablokowane"] is True, (
            "Without preventDefault, the arrow key also scrolls the page under the user."
        )

    def test_helper_is_idempotent(self):
        """Called after every switch — it must not multiply listeners or attributes."""
        out = _run_js("(() => {" + BUDOWA_PASKA + """
          a11yTablist(bar, options('gallery'));
          a11yTablist(bar, options('gallery'));
          a11yTablist(bar, options('diagnostics'));
          const tabs = bar.querySelectorAll('[role="tab"]');
          tabs[0].focus();
          let count = 0;
          tabs.forEach(x => x.addEventListener('click', () => { count++; }));
          bar.dispatch('keydown', {key: 'ArrowRight', preventDefault(){}, stopPropagation(){}});
          return {states: tabs.map(x => x.getAttribute('aria-selected')), clicks: count,
                  listeners: (bar._listeners.keydown || []).length};
        })()""")
        assert out["states"] == ["false", "false", "true"], "state comes from activeKey, it does not accumulate"
        assert out["listeners"] == 1, "the keyboard listener is attached EXACTLY once"
        assert out["clicks"] == 1, "one key press = one switch"

    def test_state_is_never_left_undeclared(self):
        """Fail closed: without keyOf and without isActive, the state still MUST be declared."""
        out = _run_js("(() => {" + BUDOWA_PASKA + """
          a11yTablist(bar, {label: 'x'});
          return {states: bar.querySelectorAll('[role="tab"]').map(x => x.getAttribute('aria-selected'))};
        })()""")
        assert None not in out["states"], (
            "Missing selection information is worse than 'false' — the screen reader stays silent about state."
        )


class TestExtensionsTabsUseTheSharedMechanism:

    def test_aria_contract_is_declared_where_the_bar_becomes_visible(self):
        """The tab contract must not depend on whether any panel rendered.

        The first version of this fix called the helper in loadExtensionsPanel AFTER
        `if(!target) return;`, where target is the DIAGNOSTICS panel — so entering the
        Gallery tab left the function before attributes were set.
        Measured in the working app: the tabs still had no aria-selected.

        The correct place is switchSettingsSection, where the tab bar becomes visible,
        and it must be OUTSIDE the skipLazyLoad branch: navigation from settings search
        shows the same tab bar without running the loader. The visibility condition and
        the accessibility condition must be the same condition.
        """
        idx = PANELS_JS.find("function switchSettingsSection(")
        assert idx > 0, "missing switchSettingsSection"
        koniec = PANELS_JS.find("\n}", idx)
        body = PANELS_JS[idx:koniec]
        kod = "\n".join(
            line for line in body.splitlines()
            if not line.strip().startswith(("//", "*", "/*"))
        )
        assert "_extensionsSyncTabsA11y()" in kod, (
            "The ARIA contract must be created where the Extensions section becomes visible."
        )
        poz_sync = kod.find("_extensionsSyncTabsA11y()")
        poz_lazy = kod.find("if(!(opts&&opts.skipLazyLoad)){")
        poz_koniec_lazy = kod.find("}", kod.find("loadExtensionsPanel();"))
        assert poz_lazy > 0 and poz_koniec_lazy > poz_lazy
        assert poz_sync > poz_koniec_lazy, (
            "The call sits in the skipLazyLoad branch — entering from settings search "
            "would show the tab bar without declared state."
        )

    def test_both_paths_use_one_entry_point(self):
        """A contract applied on one path drifts apart on the other."""
        assert "function _extensionsSyncTabsA11y()" in PANELS_JS
        idx = PANELS_JS.find("function switchExtensionsTab(")
        body = PANELS_JS[idx:PANELS_JS.find("\n}", idx)]
        assert "_extensionsSyncTabsA11y()" in body, (
            "Tab switching must go through the same path as the first render."
        )
        # helper called from one place on each path, with no copied attributes
        assert "setAttribute('aria-selected'" not in body, (
            "A copied attribute-setting block next to the shared helper is a recipe for drift."
        )

    def test_tablist_label_is_translated_everywhere(self):
        """Repo wymaga pokrycia klucza we wszystkich locale (nie tylko 'en')."""
        occurrences = len(re.findall(r"settings_extensions_tabs_aria\s*:", I18N_JS))
        sections = len(re.findall(r"^\s{2}'?[A-Za-z-]+'?\s*:\s*\{", I18N_JS, re.M))
        assert occurrences >= 15, (
            f"the key is in {occurrences} locales, and the file has {sections} language sections — "
            "a missing translation is our debt, not the author's."
        )


class TestSourceChipContrast:
    """A brand color does not exempt you from the readability threshold (WCAG 1.4.3)."""

    def test_chip_colours_come_from_one_shared_rule(self):
        idx = STYLE_CSS.find(".session-source-chip[data-source-key]")
        assert idx > 0, "missing the shared rule that computes the chip color"
        blok = STYLE_CSS[idx:idx + 400]
        assert "color-mix" in blok and "var(--source-hue)" in blok, (
            "The color must be COMPUTED from the brand hue, not hardcoded separately per source."
        )

    def test_every_source_declares_its_hue(self):
        for zrodlo in ("telegram", "discord", "slack", "claude_code"):
            m = re.search(rf'\[data-source-key="{zrodlo}"\][^{{]*\{{([^}}]*)\}}', STYLE_CSS)
            assert m, f"missing hue declaration for source {zrodlo}"
            assert "--source-hue" in m.group(1), (
                f"{zrodlo} must provide the hue through a variable, otherwise it falls out of the shared rule"
            )

    def test_no_alpha_on_chip_text_colour(self):
        """Alpha on text multiplies through the background and quietly eats contrast.

        That is exactly how these chips dropped to 1.1-3.4:1: the colors were given as
        rgba(...,0.85) on a semi-transparent background, so the measured contrast was
        much lower than the color value alone suggested.
        """
        for zrodlo in ("telegram", "discord", "slack", "claude_code"):
            m = re.search(rf'\[data-source-key="{zrodlo}"\][^{{]*\{{([^}}]*)\}}', STYLE_CSS)
            assert m, f"missing hue declaration for source {zrodlo}"
            hue = re.search(r"--source-hue:\s*([^;]+);", m.group(1))
            assert hue, f"{zrodlo}: missing --source-hue"
            assert "rgba" not in hue.group(1).lower(), (
                f"{zrodlo}: hue with alpha — contrast will be lower than it looks"
            )

    def test_unknown_source_is_legible_by_default(self):
        """A new source must be legible BEFORE measurement (fail safe)."""
        idx = STYLE_CSS.find(".session-source-chip[data-source-key]")
        blok = STYLE_CSS[idx:idx + 400]
        m = re.search(r"var\(--source-tint-keep,\s*(\d+)%\)", blok)
        assert m, "missing default value for --source-tint-keep"
        assert int(m.group(1)) <= 50, (
            "The default value must match the DARKEST hue (Slack, 50%), "
            "otherwise a newly added source may be illegible until someone measures it."
        )
