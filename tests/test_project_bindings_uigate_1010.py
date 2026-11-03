"""UX round-4 re-gate (maintainer review 2026-10-10T02:01:36Z): two interaction
bugs in the bindings dialog's custom combobox.

1. Two dropdowns could be open at once — the trigger's click handler
   stopPropagation()s, so the sibling combo's document-level close never fired
   and both triggers stayed aria-expanded="true" (the model menu then covered the
   default-workspace row). Fixed with a module-level "current open combo" that
   _open() closes before opening itself.
2. The position:fixed menu detached when the dialog scrolled — it is placed once
   from the trigger's viewport rect and nothing listened for scroll. Fixed by
   closing on a capture-phase scroll (native <select> behaviour), while ignoring
   the menu's OWN internal scrolling (its list is height-capped and
   _setHighlight() scrollIntoView()s the highlighted row).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_COMBO_SLOT_DECL = "let _openBindingsCombo=null;"
_COMBO_END_MARKER = "\n// Modal dialog for editing a project's bindings"


def _combo_fn() -> str:
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.index(_COMBO_SLOT_DECL)
    end = src.index(_COMBO_END_MARKER, start)
    return src[start:end]


def _run_node(tmp_path: Path, name: str, script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is required for the frontend behavior probe")
    script_path = tmp_path / name
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


# Same mini-DOM as the earlier combo probes, extended to record the document
# scroll listener (with its capture flag) so the probe can dispatch one.
_DOM_STUB = r"""
function assert(cond, msg) { if (!cond) throw new Error(msg); }

function _classList(node) {
  const set = node.__classSet || (node.__classSet = new Set());
  const sync = () => { node._className = [...set].join(' '); };
  return {
    add: (...xs) => { xs.forEach((x) => x && set.add(x)); sync(); },
    remove: (...xs) => { xs.forEach((x) => set.delete(x)); sync(); },
    contains: (x) => set.has(x),
    toggle: (x, f) => { const on = f === undefined ? !set.has(x) : !!f; if (on) set.add(x); else set.delete(x); sync(); return on; },
  };
}

function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    parentNode: null,
    _className: '',
    dataset: {},
    style: {},
    attrs: {},
    textContent: '',
    innerHTML: '',
    scrollHeight: 0,
    appendChild(child) { child.parentNode = node; node.children.push(child); return child; },
    setAttribute(k, v) { node.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attrs, k) ? node.attrs[k] : null; },
    getBoundingClientRect() { return { top: 10, bottom: 30, left: 5, width: 120 }; },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, '');
      return node.children.filter((c) => (c._className || '').split(/\s+/).includes(cls));
    },
    closest() { return node.parentNode; },
    contains(other) { let p = other; while (p) { if (p === node) return true; p = p.parentNode; } return false; },
    click() { if (typeof node.onclick === 'function') node.onclick({ stopPropagation() {}, preventDefault() {} }); },
    addEventListener() {},
  };
  Object.defineProperty(node, 'className', {
    get() { return node._className; },
    set(v) {
      node._className = String(v);
      const set = node.__classSet || (node.__classSet = new Set());
      set.clear();
      String(v).split(/\s+/).forEach((c) => { if (c) set.add(c); });
    },
  });
  Object.defineProperty(node, 'innerHTML', {
    get() { return node._innerHTML || ''; },
    set(v) { node._innerHTML = String(v); node.children = []; },
  });
  node.classList = _classList(node);
  return node;
}

const __doc = { click: [], scroll: [] };
const _document = {
  createElement: (tag) => makeElement(tag),
  addEventListener: (type, fn, capture) => {
    if (type === 'click') __doc.click.push(fn);
    else if (type === 'scroll') __doc.scroll.push({ fn, capture: !!capture });
  },
  removeEventListener: (type, fn, capture) => {
    const bucket = type === 'click' ? __doc.click : (type === 'scroll' ? __doc.scroll : null);
    if (!bucket) return;
    const i = bucket.findIndex((x) => (x && x.fn ? x.fn === fn : x === fn));
    if (i >= 0) bucket.splice(i, 1);
  },
};
globalThis.document = _document;
globalThis.window = { innerHeight: 800, addEventListener: () => {} };
globalThis.t = (key) => key;
"""


_ONE_POPUP_PROBE = (
    _DOM_STUB
    + _combo_fn()
    + r"""
const wsCombo = _makeBindingsCombo({ value: '', options: [{ value: '/ws/a', name: 'a' }] });
const modelCombo = _makeBindingsCombo({ value: '', options: [{ value: 'm', name: 'M' }] });
const trig = (c) => c.el.children[0];
const menu = (c) => c.el.children[1];
const expanded = (c) => trig(c).getAttribute('aria-expanded');
const clickTrigger = (c) => trig(c).onclick({ stopPropagation() {}, preventDefault() {} });
function key(c, k) {
  const e = { key: k, preventDefault() {} };
  trig(c).onkeydown(e);
}

// The component must expose a close so a sibling can be shut (see _open).
assert(typeof wsCombo.close === 'function', 'the combo must expose close()');

// --- bug 1: clicking a second trigger must close the first menu ---
clickTrigger(wsCombo);
assert(menu(wsCombo).classList.contains('open'), 'the workspace menu opens');
assert(expanded(wsCombo) === 'true', 'an open trigger reports aria-expanded=true');
clickTrigger(modelCombo);
assert(menu(modelCombo).classList.contains('open'), 'the model menu opens');
assert(!menu(wsCombo).classList.contains('open'),
  'the workspace menu must NOT stay open behind the model menu (two popups at once)');
assert(expanded(wsCombo) === 'false',
  'the closed sibling must report aria-expanded=false, got ' + expanded(wsCombo));
assert(expanded(modelCombo) === 'true', 'the model trigger is the expanded one');

// --- bug 1, keyboard path: ArrowDown on add, then ArrowDown on model ---
key(modelCombo, 'Escape');
assert(!menu(modelCombo).classList.contains('open'), 'Escape closes');
assert(expanded(modelCombo) === 'false', 'Escape clears aria-expanded');
key(wsCombo, 'ArrowDown');
assert(menu(wsCombo).classList.contains('open'), 'ArrowDown opens the workspace list');
key(modelCombo, 'ArrowDown');
assert(menu(modelCombo).classList.contains('open'), 'ArrowDown opens the model list');
assert(!menu(wsCombo).classList.contains('open'),
  'opening the model list by keyboard must close the workspace list too');
assert(expanded(wsCombo) === 'false', 'the keyboard-closed sibling is collapsed');

// --- bug 2: an ancestor scroll must close the detached menu ---
assert(__doc.scroll.length >= 1, 'a document scroll listener must be registered');
assert(__doc.scroll.some((h) => h.capture === true),
  'the scroll listener must be registered in the capture phase (native select behaviour)');
const dialogScroller = makeElement('div');
assert(menu(modelCombo).classList.contains('open'), 'precondition: the model menu is open');
__doc.scroll.forEach((h) => h.fn({ target: dialogScroller }));
assert(!menu(modelCombo).classList.contains('open'),
  'scrolling the dialog (or page) must close the menu, not leave it floating');
assert(expanded(modelCombo) === 'false', 'closing on scroll clears aria-expanded');

// --- bug 2, the menu's OWN scrolling must NOT close it ---
key(modelCombo, 'ArrowDown');
assert(menu(modelCombo).classList.contains('open'), 'the menu reopened');
__doc.scroll.forEach((h) => h.fn({ target: menu(modelCombo) }));
assert(menu(modelCombo).classList.contains('open'),
  "the menu's own list scroll (height-capped, scrollIntoView) must keep it open");

// --- destroy drops the scroll listener too ---
const before = __doc.scroll.length;
modelCombo.destroy();
assert(__doc.scroll.length === before - 1, 'destroy() must remove the scroll listener, got ' + __doc.scroll.length);
console.log('ok');
"""
)


def test_only_one_bindings_popup_can_be_open_and_scroll_closes_it(tmp_path):
    """Maintainer UX re-gate 2026-10-10T02:01:36Z: one popup at a time, and the
    fixed-position menu must not detach when the dialog scrolls."""
    assert _run_node(tmp_path, "combo_one_popup.js", _ONE_POPUP_PROBE).strip().endswith("ok")


def test_the_single_popup_slot_is_module_level_and_wired():
    """Source guard: the shared slot, the sibling close, and the capture scroll
    listener must all be present in the shipped component."""
    fn = _combo_fn()
    assert _COMBO_SLOT_DECL in fn, fn[:400]
    # _open closes the previous owner before taking the slot.
    assert "_openBindingsCombo.close()" in fn, fn
    assert "_openBindingsCombo=api;" in fn, fn
    # _close releases the slot only when it still owns it.
    assert "if(_openBindingsCombo===api) _openBindingsCombo=null;" in fn, fn
    # close is exposed on the returned API.
    assert "close:_close," in fn, fn
    # the scroll listener is capture-phase and skips the menu's own scrolling.
    assert "document.addEventListener('scroll',_onDocScroll,true);" in fn, fn
    assert "document.removeEventListener('scroll',_onDocScroll,true);" in fn, fn
    assert "!menu.contains(e.target)" in fn, fn
