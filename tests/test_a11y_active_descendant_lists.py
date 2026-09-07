"""Arrow-key-driven selection lists must announce what is selected.

Measured defect (18.08.2026): /slash command suggestions and working-directory
path suggestions have full arrow-key navigation, but the selected option was
marked ONLY with a CSS class. Focus stays in the text field, so the screen
reader announced nothing while moving through the list - the user did not know
what Enter would confirm (WCAG 4.1.2).

The repo has already solved exactly this problem for the model list (static/ui.js,
_highlightRow, author comment explicitly about WCAG 4.1.2): role="option" +
aria-selected on rows and role="combobox" + aria-activedescendant on the field.
These two lists were unaddressed copies of the same pattern, so instead of a
third copy of the logic we introduce a shared a11yActiveDescendantList helper.

The contract is COMPLETE and the tests guard it as a whole: aria-selected alone
is not enough, because without role="listbox"/"option" the screen reader does
not treat the element as a listbox, and without aria-activedescendant it will
not announce movement when focus does not move.

The tests EXECUTE real functions cut from the sources (node vm), rather than
checking for the presence of text, because the defect is about WHEN the
attributes appear: a source check would also pass on code that sets them at the
wrong moment.
"""

from pathlib import Path
import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function mkEl(tag) {
  return {
    tagName: (tag || 'div').toUpperCase(),
    children: [], attrs: {}, classes: new Set(), dataset: {}, id: '',
    style: {}, textContent: '', innerHTML: '', parentNode: null, listeners: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    get classList() {
      const s = this.classes;
      return { add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c),
               toggle: (c, on) => { if (on === undefined) { s.has(c) ? s.delete(c) : s.add(c); }
                                    else if (on) s.add(c); else s.delete(c); } };
    },
    descendants() { let o = []; for (const c of this.children) { o.push(c); o = o.concat(c.descendants()); } return o; },
    matches(sel) {
      if (sel === '.cmd-item') return this.classes.has('cmd-item');
      if (sel === '.ws-suggest-item') return this.classes.has('ws-suggest-item');
      throw new Error('atrapa nie zna selektora ' + sel);
    },
    querySelectorAll(sel) { return this.descendants().filter((d) => d.matches(sel)); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    scrollIntoView() {},
  };
}

const el = {};
const ctx = {
  window: {},
  document: { activeElement: null, createElement: (t) => mkEl(t),
              getElementById: (id) => el[id] || null,
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
vm.runInContext("function $(id){return document.getElementById(id);} function t(k){return null;}", ctx);

function wytnij(plik, name) {
  const src = fs.readFileSync(plik, 'utf8');
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('brak funkcji ' + name + ' w ' + plik);
  let g = 0;
  for (let j = src.indexOf('{', start); j < src.length; j++) {
    if (src[j] === '{') g++;
    else if (src[j] === '}') { g--; if (!g) return src.slice(start, j + 1); }
  }
  throw new Error('nie domknalem ' + name);
}

// NOTE: `var`, not `let`. In Node vm, `let` creates a lexical binding that
// is NOT a property of the context - setting ctx._cmdSelectedIdx from outside
// would then be invisible to the function and the test would report false failures
// of working code (measured while building this test).
vm.runInContext('var _cmdSelectedIdx=-1;', ctx);
vm.runInContext(wytnij(CMD_PATH, '_syncCmdDropdownA11y'), ctx);
vm.runInContext(wytnij(CMD_PATH, 'navigateCmdDropdown'), ctx);
vm.runInContext(wytnij(CMD_PATH, 'hideCmdDropdown'), ctx);
vm.runInContext(wytnij(PAN_PATH, '_highlightWorkspaceSuggestion'), ctx);

const out = {};

// ── items podpowiedzi komend ──────────────────────────────────────────────
const field = mkEl('textarea'); el.msg = field;
const dd = mkEl('div'); el.cmdDropdown = dd;
dd.classList.add('open');
const poz = [];
for (let i = 0; i < 4; i++) {
  const it = mkEl('div'); it.classList.add('cmd-item');
  if (i === 0) it.classList.add('selected');
  dd.appendChild(it); poz.push(it);
}
ctx._cmdSelectedIdx = 0;
ctx._syncCmdDropdownA11y();
out.komendyStart = {
  rolaListy: dd.getAttribute('role'),
  wszystkieOption: poz.every((p) => p.getAttribute('role') === 'option'),
  rolaPola: field.getAttribute('role'),
  expanded: field.getAttribute('aria-expanded'),
  autocomplete: field.getAttribute('aria-autocomplete'),
  wskazujeWybrana: field.getAttribute('aria-activedescendant') === poz[0].id && !!poz[0].id,
  pierwszaSelected: poz[0].getAttribute('aria-selected'),
  pozostaleNieSelected: poz.slice(1).every((p) => p.getAttribute('aria-selected') === 'false'),
  wskazujeListe: field.getAttribute('aria-controls') === dd.id && !!dd.id,
};

ctx.navigateCmdDropdown(1);
out.commandsAfterArrow = {
  wskazujeDruga: field.getAttribute('aria-activedescendant') === poz[1].id,
  ariaMatchesCss: poz[1].classes.has('selected')
                  && poz[1].getAttribute('aria-selected') === 'true'
                  && !poz[0].classes.has('selected')
                  && poz[0].getAttribute('aria-selected') === 'false',
};

ctx._cmdSelectedIdx = 0;
ctx.navigateCmdDropdown(-1);
out.komendyZawijanie = { wskazujeOstatnia: field.getAttribute('aria-activedescendant') === poz[3].id };

ctx.hideCmdDropdown();
out.komendyPoUkryciu = {
  noReference: !field.getAttribute('aria-activedescendant'),
  expanded: field.getAttribute('aria-expanded'),
};

// ── items podpowiedzi sciezek ─────────────────────────────────────────────
const pathField = mkEl('input'); el.workspaceFormPath = pathField;
const box = mkEl('div'); el.workspaceFormPathSuggestions = box;
const sug = [];
for (let i = 0; i < 3; i++) { const it = mkEl('button'); it.classList.add('ws-suggest-item'); box.appendChild(it); sug.push(it); }

ctx._highlightWorkspaceSuggestion(1);
out.paths = {
  rolaListy: box.getAttribute('role'),
  rolaPola: pathField.getAttribute('role'),
  wskazujeWybrana: pathField.getAttribute('aria-activedescendant') === sug[1].id && !!sug[1].id,
  ariaMatchesCss: sug[1].classes.has('active')
                  && sug[1].getAttribute('aria-selected') === 'true'
                  && sug[0].getAttribute('aria-selected') === 'false',
};
const idBefore = sug[1].id;
ctx._highlightWorkspaceSuggestion(-1);
out.pathsWithoutSelection = { noReference: !pathField.getAttribute('aria-activedescendant') };
ctx._highlightWorkspaceSuggestion(1);
ctx._highlightWorkspaceSuggestion(1);
out.pathsIdempotency = { idsStable: sug[1].id === idBefore };

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    if not NODE:
        pytest.skip("node unavailable - cannot measure behavior")
    script = tmp_path_factory.mktemp("lists") / "harness.js"
    script.write_text(
        f"const A11Y_PATH = {json.dumps(str(REPO / 'static' / 'a11y-helpers.js'))};\n"
        f"const CMD_PATH = {json.dumps(str(REPO / 'static' / 'commands.js'))};\n"
        f"const PAN_PATH = {json.dumps(str(REPO / 'static' / 'panels.js'))};\n" + HARNESS,
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"harness padl: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestCommandSuggestions:
    def test_list_is_a_listbox_for_screen_readers(self, behaviour):
        s = behaviour["komendyStart"]
        assert s["rolaListy"] == "listbox", (
            "without role=listbox, the screen reader sees a set of divs, not a selection list"
        )
        assert s["wszystkieOption"], "options must have role=option"

    def test_input_is_linked_to_the_list(self, behaviour):
        s = behaviour["komendyStart"]
        assert s["rolaPola"] == "combobox"
        assert s["expanded"] == "true"
        assert s["autocomplete"] == "list"
        assert s["wskazujeListe"], "brak aria-controls laczacego field z items"

    def test_selected_option_is_announced_immediately(self, behaviour):
        """The list opens with the first option selected - before anyone presses an arrow key."""
        s = behaviour["komendyStart"]
        assert s["wskazujeWybrana"], (
            "without aria-activedescendant, the screen reader stays silent because focus remains in the field"
        )
        assert s["pierwszaSelected"] == "true"
        assert s["pozostaleNieSelected"]

    def test_arrow_key_moves_the_announced_option(self, behaviour):
        p = behaviour["commandsAfterArrow"]
        assert p["wskazujeDruga"], "the pointer must follow the selection"
        assert p["ariaMatchesCss"], (
            "what is visible (.selected class) and what is announced must be the same"
        )

    def test_list_wraparound_is_announced_too(self, behaviour):
        assert behaviour["komendyZawijanie"]["wskazujeOstatnia"]

    def test_collapsing_clears_the_stale_option_reference(self, behaviour):
        u = behaviour["komendyPoUkryciu"]
        assert u["noReference"], (
            "aria-activedescendant pointing to a removed element is a broken contract"
        )
        assert u["expanded"] == "false"


class TestPathSuggestions:
    def test_list_is_a_listbox(self, behaviour):
        s = behaviour["paths"]
        assert s["rolaListy"] == "listbox"
        assert s["rolaPola"] == "combobox"

    def test_highlighted_suggestion_is_announced(self, behaviour):
        s = behaviour["paths"]
        assert s["wskazujeWybrana"]
        assert s["ariaMatchesCss"]

    def test_no_selection_clears_the_reference(self, behaviour):
        assert behaviour["pathsWithoutSelection"]["noReference"]

    def test_repeated_call_keeps_identifiers_stable(self, behaviour):
        assert behaviour["pathsIdempotency"]["idsStable"], (
            "an unstable id breaks aria-activedescendant between refreshes"
        )


class TestEverySelectionChangePath:
    """The contract must be created everywhere the selection changes."""

    def test_command_list_stays_in_sync_in_three_places(self):
        # showing the list, arrow-key navigation, hiding the list (+ definition)
        assert COMMANDS_JS.count("_syncCmdDropdownA11y(") >= 4, (
            "the contract must be refreshed on list show, navigation, and hide"
        )

    def test_path_suggestions_clear_the_contract_on_close(self):
        idx = PANELS_JS.find("function closeWorkspacePathSuggestions(")
        assert idx > 0
        assert "a11yActiveDescendantList" in PANELS_JS[idx:idx + 700], (
            "closing the list must clear aria-activedescendant"
        )


class TestTranslations:
    def test_list_names_exist_in_every_locale(self):
        for key in ("slash_commands_list_aria", "workspace_path_suggestions_aria"):
            assert I18N_JS.count(key) >= 15, (
                f"{key}: {I18N_JS.count(key)} locales instead of 15"
            )
