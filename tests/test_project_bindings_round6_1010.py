"""Round-6 re-review of the merge-pushed heads (Greptile 2026-10-10T05:08-09Z).

1. P1 "Project chat opens elsewhere" (``static/sessions.js:2051``):
   ``newSession`` flagged a workspace as INHERITED whenever the current session
   had one, even when the resolved workspace came from an EXPLICIT source — a
   project binding merged into ``options``, or the quick-create
   ``options.workspace``. The server uses that flag to recover a DELETED
   inherited path by falling back to the last workspace
   (``resolve_implicit_workspace_with_recovery``), so a project chat whose bound
   directory was gone silently opened in another directory instead of failing
   the binding. The behaviour regressions live next to the existing
   ``newSession`` driver in ``tests/test_issue4755_profile_default_workspace.py``.

2. P2 "Settings controls are unnamed" (``static/sessions.js:10708``): the
   combobox triggers carried no accessible name. Their visible field labels are
   sibling ``div``s with no id link, and the add-workspace combo has no visible
   label at all, so a screen reader could not tell a model change from a
   workspace add. ``_makeBindingsCombo`` now accepts ``ariaLabel`` and the dialog
   passes ``pb_add_workspace_title`` / ``pb_field_model`` (both already present
   in all 15 locale blocks — no new key).
"""

from __future__ import annotations

import re
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


# Mini-DOM (same shape as the round-5 probe's): enough for _makeBindingsCombo to
# construct and open. scrollHeight is a getter that reports 0 until the element
# carries 'open', mirroring the real display:none behaviour.
_DOM_STUB = r"""
function assert(cond, msg) { if (!cond) throw new Error(msg); }

const __geom = { vh: 900, rect: { top: 200, bottom: 246, left: 20, width: 350 } };

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
    __contentHeight: 120,
    appendChild(child) { child.parentNode = node; node.children.push(child); return child; },
    setAttribute(k, v) { node.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attrs, k) ? node.attrs[k] : null; },
    removeAttribute(k) { delete node.attrs[k]; },
    getBoundingClientRect() {
      return { top: __geom.rect.top, bottom: __geom.rect.bottom, left: __geom.rect.left, width: __geom.rect.width };
    },
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
  Object.defineProperty(node, 'scrollHeight', {
    get() { return (node.__classSet && node.__classSet.has('open')) ? node.__contentHeight : 0; },
  });
  node.classList = _classList(node);
  return node;
}

const _document = {
  createElement: (tag) => makeElement(tag),
  addEventListener: () => {},
  removeEventListener: () => {},
};
globalThis.document = _document;
globalThis.window = { get innerHeight() { return __geom.vh; }, addEventListener: () => {} };
globalThis.t = (key) => key;
"""


# --- finding 2: every combobox trigger must carry an accessible name ---------
_NAMING_PROBE = (
    _DOM_STUB
    + _combo_fn()
    + r"""
// Passing a name wires it onto the trigger.
const named = _makeBindingsCombo({
  value: '',
  options: [{ value: '/ws/a', name: 'a' }],
  placeholder: 'Add workspace…',
  ariaLabel: 'Add workspace',
});
const namedTrig = named.el.children[0];
assert(namedTrig.getAttribute('role') === 'combobox', 'the trigger keeps its combobox role');
assert(namedTrig.getAttribute('aria-label') === 'Add workspace',
  'the trigger must carry the accessible name it was given, got ' + namedTrig.getAttribute('aria-label'));
// opening must not clobber it
namedTrig.onclick({ stopPropagation() {}, preventDefault() {} });
assert(namedTrig.getAttribute('aria-expanded') === 'true', 'the menu opens');
assert(namedTrig.getAttribute('aria-label') === 'Add workspace',
  'the accessible name survives the open/close cycle');

// A combo without a name does not get an empty aria-label invented for it.
const bare = _makeBindingsCombo({ value: '', options: [{ value: '/ws/a', name: 'a' }] });
const bareTrig = bare.el.children[0];
assert(!bareTrig.getAttribute('aria-label'),
  'no ariaLabel option means no attribute, got ' + bareTrig.getAttribute('aria-label'));

console.log('ok');
"""
)


def test_combobox_trigger_gets_the_accessible_name_it_was_given(tmp_path):
    """Greptile P2 2026-10-10T05:08:46Z: the triggers had no accessible name."""
    assert _run_node(tmp_path, "combo_naming.js", _NAMING_PROBE).strip().endswith("ok")


def _call_block(marker: str) -> str:
    """The _makeBindingsCombo({...}) call site introduced by `marker`."""
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.index(marker)
    end = src.index("\n  });", start)
    return src[start:end]


def test_dialog_combos_pass_localized_accessible_names():
    """Source guard: both dialog comboboxes must name themselves from i18n and the
    add-workspace one (which has NO visible label) must not be skipped."""
    add_block = _call_block("const addCombo=_makeBindingsCombo({")
    model_block = _call_block("const modelCombo=_makeBindingsCombo({")
    assert "ariaLabel:t('pb_add_workspace_title')" in add_block, add_block
    assert "ariaLabel:t('pb_field_model')" in model_block, model_block
    # the shipped component must actually consume the option
    assert "if(o&&o.ariaLabel) trigger.setAttribute('aria-label',o.ariaLabel);" in _combo_fn()


def test_both_accessible_name_keys_exist_in_every_locale():
    """No new i18n key was introduced, and the two reused ones are localized in
    every locale block (a missing key falls back to the raw id)."""
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in ("pb_add_workspace_title", "pb_field_model"):
        assert len(re.findall(rf"^\s*{key}:", i18n, flags=re.M)) == 15, key
