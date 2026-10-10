"""Round-5 re-gate (maintainer review 2026-10-10T03:06:13Z + Greptile
2026-10-10T03:29:51Z) for the project-bindings dialog.

1. The add-workspace dropdown ran off the bottom of the viewport. The flip-up
   decision read ``menu.scrollHeight`` while the menu was still ``display:none``,
   so it always saw 0 and fell back to a 240px guess while the rendered box is
   capped at ``min(60vh, 320px)``; a list of ~7 rows (saved workspaces + Home +
   "Type a path…") therefore opened downward past the viewport edge and its last
   options could not be clicked or tapped. Fixed by laying the menu out first
   (fixed, trigger-width, ``visibility:hidden``) and measuring the real height
   before choosing up/down, then capping the box to the space available.
   (Greptile's P2 "Dropdown options extend offscreen" is the same root cause.)

2. The arrow highlight was invisible to screen readers: focus never leaves the
   trigger, which had neither ``aria-controls`` nor ``aria-activedescendant``,
   and the options had no ids. Fixed by giving the list and options ids and
   keeping the trigger's active option in step with the highlight.

3. Greptile P1 "Workspace edits get overwritten": ``_resolve_ws_list`` (the
   bindings save path) reads and rewrites the saved workspace list under
   ``_PROJECTS_CATALOG_LOCK``, but the workspace add / remove / rename / reorder
   endpoints did not share that lock, so a concurrent save could drop the other
   request's change. All four now hold the same lock around their
   read-modify-write pair.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

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


# Mini-DOM with LAYOUT AWARENESS, which is the whole point of finding 1: an
# element that is not displayed reports scrollHeight 0 (the real browser does the
# same for display:none), and the trigger's viewport rect / window.innerHeight are
# driven by __geom so a probe can reproduce the maintainer's 390x844 phone table.
_DOM_STUB = r"""
function assert(cond, msg) { if (!cond) throw new Error(msg); }

const __geom = { vh: 844, rect: { top: 530, bottom: 576, left: 20, width: 350 } };

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
    __contentHeight: 0,
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
  // display:none -> 0 (exactly the bug: the old code measured before adding
  // 'open', which is what makes the CSS display:block).
  Object.defineProperty(node, 'scrollHeight', {
    get() { return (node.__classSet && node.__classSet.has('open')) ? node.__contentHeight : 0; },
  });
  node.classList = _classList(node);
  return node;
}

const __doc = { click: [], scroll: [] };
const _document = {
  createElement: (tag) => makeElement(tag),
  addEventListener: (type, fn) => { if (type === 'click') __doc.click.push(fn); },
  removeEventListener: () => {},
};
globalThis.document = _document;
globalThis.window = { get innerHeight() { return __geom.vh; }, addEventListener: () => {} };
globalThis.t = (key) => key;
"""


def _options(n: int):
    return ", ".join(f"{{ value: '/ws/{i}', name: 'ws{i}' }}" for i in range(n))


# --- finding 1: the flip decision must use the real rendered height ----------
_GEOMETRY_PROBE = (
    _DOM_STUB
    + _combo_fn()
    + r"""
function openCombo(contentHeight) {
  const combo = _makeBindingsCombo({ value: '', options: [OPTS] });
  const trig = combo.el.children[0];
  const menu = combo.el.children[1];
  menu.__contentHeight = contentHeight;
  trig.onclick({ stopPropagation() {}, preventDefault() {} });
  return { combo, trig, menu };
}
function geometry(menu, contentHeight) {
  const cap = parseFloat(menu.style.maxHeight);
  const boxH = Math.min(contentHeight, isNaN(cap) ? Infinity : cap);
  if (menu.style.top === 'auto') {
    const bottomOff = parseFloat(menu.style.bottom);
    return { flipped: true, bottomEdge: __geom.vh - bottomOff, topEdge: __geom.vh - bottomOff - boxH, capPx: cap };
  }
  const topPx = parseFloat(menu.style.top);
  return { flipped: false, bottomEdge: topPx + boxH, topEdge: topPx, capPx: cap };
}

// --- maintainer's case: 390x844 phone, 6 saved workspaces + "Type a path…" ---
// Below the trigger there are only 844-576-8 = 260px, and the real box is 280px
// (capped at min(60vh,320px) = 320px). The old 240px guess said "fits" and opened
// downward, ending 16px below the fold; the real height must flip it up.
__geom.vh = 844;
const phone = openCombo(280);
assert(phone.menu.classList.contains('open'), 'the phone menu must be open');
let g = geometry(phone.menu, 280);
assert(g.flipped, 'a 280px menu with only 260px below must flip UP, got ' + JSON.stringify(g));
assert(g.bottomEdge <= __geom.vh, 'the menu bottom must stay inside the viewport, got ' + JSON.stringify(g));
assert(g.topEdge >= 0, 'the flipped menu must not run off the top, got ' + JSON.stringify(g));

// --- crowding: neither side fits, so the box itself must shrink to the space --
// 400px viewport, trigger at 180..220 -> 172px below and 172px above. The menu
// must open downward AND be capped to those 172px (it scrolls internally) so its
// bottom edge stays on screen.
__geom.vh = 400;
__geom.rect = { top: 180, bottom: 220, left: 20, width: 350 };
const tight = openCombo(280);
g = geometry(tight.menu, 280);
assert(tight.menu.classList.contains('open'), 'the tight menu must be open');
assert(tight.menu.style.top !== 'auto', 'equal space on both sides keeps the down direction');
assert(tight.menu.style.maxHeight === '172px',
  'the box must be capped to the 172px actually available, got ' + tight.menu.style.maxHeight);
assert(g.bottomEdge <= __geom.vh, 'the capped menu bottom must fit, got ' + JSON.stringify(g));

// --- roomy viewport: unchanged behaviour (no needless flip, css cap kept) -----
__geom.vh = 900;
__geom.rect = { top: 200, bottom: 246, left: 20, width: 350 };
const roomy = openCombo(120);
g = geometry(roomy.menu, 120);
assert(!g.flipped, 'a short menu with room below stays downward');
assert(roomy.menu.style.maxHeight === '320px', 'never exceed the css cap (60vh=540 -> 320)');
assert(g.bottomEdge <= __geom.vh, 'roomy menu must fit');

console.log('ok');
"""
).replace("[OPTS]", f"[{_options(7)}]")


def test_add_dropdown_measures_the_real_height_before_flipping(tmp_path):
    """Maintainer must-fix 2026-10-10T03:06:13Z: open the add list with 5-7 saved
    workspaces at 390x844 and assert the menu's bottom is within the viewport."""
    assert _run_node(tmp_path, "combo_geometry.js", _GEOMETRY_PROBE).strip().endswith("ok")


# --- finding 2: the keyboard highlight must be announced ---------------------
_ARIA_PROBE = (
    _DOM_STUB
    + _combo_fn()
    + r"""
const combo = _makeBindingsCombo({ value: '', options: [{ value: '/ws/a', name: 'a' }, { value: '/ws/b', name: 'b' }] });
const trig = combo.el.children[0];
const menu = combo.el.children[1];
const rows = () => menu.querySelectorAll('.ws-opt');
const listId = menu.getAttribute('id');
assert(listId, 'the listbox must carry an id');
assert(trig.getAttribute('aria-controls') === listId,
  'the trigger must point at the list it controls, got ' + trig.getAttribute('aria-controls'));
assert(!trig.getAttribute('aria-activedescendant'), 'nothing is highlighted while closed');

trig.onkeydown({ key: 'ArrowDown', preventDefault() {} });
assert(menu.classList.contains('open'), 'ArrowDown opens the list');
rows().forEach((r, i) => assert(r.getAttribute('id'), 'option ' + i + ' must have an id'));
assert(rows()[0].getAttribute('id') !== rows()[1].getAttribute('id'), 'option ids must be unique');

trig.onkeydown({ key: 'ArrowDown', preventDefault() {} });
const first = rows().find((r) => r.classList.contains('active'));
assert(first, 'ArrowDown highlights a row');
assert(trig.getAttribute('aria-activedescendant') === first.getAttribute('id'),
  'the trigger must name the highlighted option, got ' + trig.getAttribute('aria-activedescendant'));

trig.onkeydown({ key: 'ArrowDown', preventDefault() {} });
const second = rows().find((r) => r.classList.contains('active'));
assert(second !== first, 'the second ArrowDown moves the highlight');
assert(trig.getAttribute('aria-activedescendant') === second.getAttribute('id'),
  'aria-activedescendant must follow the highlight');

trig.onkeydown({ key: 'Escape', preventDefault() {} });
assert(!menu.classList.contains('open'), 'Escape closes');
assert(!trig.getAttribute('aria-activedescendant'),
  'closing must clear aria-activedescendant, got ' + trig.getAttribute('aria-activedescendant'));

console.log('ok');
"""
)


def test_highlighted_option_is_exposed_as_the_active_descendant(tmp_path):
    """Greptile P2 2026-10-10T03:29:51Z: the arrow highlight must be announced."""
    assert _run_node(tmp_path, "combo_aria.js", _ARIA_PROBE).strip().endswith("ok")


# --- the dialog's Escape path must go through the component ------------------
_CLOSE_FROM_OUTSIDE_PROBE = (
    _DOM_STUB
    + _combo_fn()
    + r"""
const combo = _makeBindingsCombo({ value: '', options: [{ value: '/ws/a', name: 'a' }, { value: '/ws/b', name: 'b' }] });
const trig = combo.el.children[0];
const menu = combo.el.children[1];
trig.onkeydown({ key: 'ArrowDown', preventDefault() {} });
trig.onkeydown({ key: 'ArrowDown', preventDefault() {} });
assert(menu.classList.contains('open'), 'the menu is open');
assert(trig.getAttribute('aria-activedescendant'), 'a row is highlighted');
assert(_openBindingsCombo === combo, 'the combo owns the shared open slot');

// The dialog's capture-phase Escape handler only has the DOM node.
assert(typeof _closeBindingsComboMenu === 'function', '_closeBindingsComboMenu must exist');
assert(_closeBindingsComboMenu(menu) === true, 'closing reports that it closed something');
assert(!menu.classList.contains('open'), 'the class is gone');
assert(trig.getAttribute('aria-expanded') === 'false', 'aria-expanded is cleared');
assert(!trig.getAttribute('aria-activedescendant'),
  'aria-activedescendant must NOT keep pointing at a hidden option, got ' + trig.getAttribute('aria-activedescendant'));
assert(_openBindingsCombo === null, 'the shared open slot must be released');
assert(_closeBindingsComboMenu(menu) === true, 'a second call is still a no-op close');

// Fallback: a node whose owner is gone is still closed by class + aria-expanded.
const orphan = makeElement('div');
const staleTrigger = makeElement('div');
staleTrigger.className = 'project-bindings-combo-trigger';
staleTrigger.setAttribute('aria-expanded', 'true');
const staleWrap = makeElement('div');
staleWrap.className = 'project-bindings-combo';
staleWrap.appendChild(staleTrigger);
staleWrap.appendChild(orphan);
orphan.className = 'project-bindings-combo-menu open';
assert(_closeBindingsComboMenu(orphan) === true, 'the fallback closes the orphan menu');
assert(!orphan.classList.contains('open'), 'the orphan class is gone');
assert(staleTrigger.getAttribute('aria-expanded') === 'false', 'the orphan trigger is collapsed');

console.log('ok');
"""
)


def test_dialog_escape_closes_through_the_component(tmp_path):
    """Greptile P2 2026-10-10T04:21:38Z: the dialog's Escape path closed the menu
    by dropping the CSS class alone, so aria-activedescendant kept naming a hidden
    option and the shared open-combo slot was never released."""
    assert _run_node(tmp_path, "combo_close_outside.js", _CLOSE_FROM_OUTSIDE_PROBE).strip().endswith("ok")


def test_close_open_combo_delegates_to_the_component():
    """Source guard: the dialog's helper must not hand-roll the DOM teardown."""
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.index("function _closeOpenCombo(")
    end = src.index("\n  }", start)
    body = src[start:end]
    assert "_closeBindingsComboMenu(menu)" in body, body
    # the hand-rolled teardown must be gone from the dialog helper
    assert "classList.remove('open')" not in body, body
    assert "aria-expanded" not in body, body


def test_dropdown_geometry_and_aria_wiring_are_in_the_shipped_component():
    """Source guard for the two dropdown fixes."""
    fn = _combo_fn()
    # measured before the flip decision: the menu is laid out (open class) and
    # hidden, THEN its scrollHeight is read.
    assert "menu.style.visibility='hidden';" in fn, fn
    assert fn.index("menu.classList.add('open');") < fn.index("Math.min(menu.scrollHeight||cssCap"), fn
    assert "menu.style.maxHeight=Math.min(cssCap, avail)+'px';" in fn, fn
    assert "menu.style.visibility='';" in fn, fn
    assert "const avail=Math.max(flipUp?spaceAbove:spaceBelow, 120);" in fn, fn
    # a11y wiring
    assert "menu.setAttribute('id',_cid+'-list');" in fn, fn
    assert "trigger.setAttribute('aria-controls',_cid+'-list');" in fn, fn
    assert "row.setAttribute('id',_cid+'-opt-'+i);" in fn, fn
    assert "_syncActiveDescendant();" in fn, fn
    assert fn.count("_syncActiveDescendant();") >= 2, fn


# --- finding 3: the workspace endpoints share the catalog lock ---------------
class _ProbeLock:
    """Records acquisition depth so a save can prove it ran under the lock."""

    def __init__(self) -> None:
        self.depth = 0
        self.entries = 0

    def __enter__(self):
        self.depth += 1
        self.entries += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        return False

    def held(self) -> bool:
        return self.depth > 0


def _handler():
    h = MagicMock()
    h.wfile = MagicMock()
    return h


def _routes():
    return importlib.import_module("api.routes")


def _patch_lock_and_store(monkeypatch, routes, probe):
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", probe)
    monkeypatch.setattr(routes, "load_workspaces", lambda *a, **k: [{"path": "/ws/a", "name": "a"}])
    saved = []

    def _save(wss, *a, **k):
        assert probe.held(), "save_workspaces ran OUTSIDE _PROJECTS_CATALOG_LOCK"
        saved.append(list(wss))

    monkeypatch.setattr(routes, "save_workspaces", _save)
    return saved


def test_workspace_remove_holds_the_catalog_lock(monkeypatch):
    routes = _routes()
    probe = _ProbeLock()
    saved = _patch_lock_and_store(monkeypatch, routes, probe)
    routes._handle_workspace_remove(_handler(), {"path": "/ws/a"})
    assert probe.entries == 1, "the remove endpoint must take the shared lock"
    assert saved == [[]], saved


def test_workspace_rename_holds_the_catalog_lock(monkeypatch):
    routes = _routes()
    probe = _ProbeLock()
    saved = _patch_lock_and_store(monkeypatch, routes, probe)
    routes._handle_workspace_rename(_handler(), {"path": "/ws/a", "name": "renamed"})
    assert probe.entries == 1, "the rename endpoint must take the shared lock"
    assert saved[0][0]["name"] == "renamed", saved


def test_workspace_reorder_holds_the_catalog_lock(monkeypatch):
    routes = _routes()
    probe = _ProbeLock()
    saved = _patch_lock_and_store(monkeypatch, routes, probe)
    routes._handle_workspace_reorder(_handler(), {"paths": ["/ws/a"]})
    assert probe.entries == 1, "the reorder endpoint must take the shared lock"
    assert [w["path"] for w in saved[0]] == ["/ws/a"], saved


def test_workspace_add_holds_the_catalog_lock(tmp_path, monkeypatch):
    routes = _routes()
    probe = _ProbeLock()
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", probe)
    monkeypatch.setattr(routes, "_is_blocked_system_path", lambda p: False)
    monkeypatch.setattr(routes, "validate_workspace_to_add", lambda p, **k: Path(p))
    monkeypatch.setattr(routes, "load_workspaces", lambda *a, **k: [])
    saved = []

    def _save(wss, *a, **k):
        assert probe.held(), "save_workspaces ran OUTSIDE _PROJECTS_CATALOG_LOCK"
        saved.append(list(wss))

    monkeypatch.setattr(routes, "save_workspaces", _save)
    routes._handle_workspace_add(_handler(), {"path": str(tmp_path)})
    assert probe.entries == 1, "the add endpoint must take the shared lock"
    assert saved and saved[0][0]["path"] == str(Path(str(tmp_path))), saved


def test_all_four_workspace_handlers_write_under_the_catalog_lock():
    """Source guard: every read-modify-write pair in the workspace endpoints must
    sit inside the same lock the bindings save path uses."""
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for name in (
        "_handle_workspace_add",
        "_handle_workspace_remove",
        "_handle_workspace_rename",
        "_handle_workspace_reorder",
    ):
        start = src.index(f"def {name}(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        assert "with _PROJECTS_CATALOG_LOCK:" in body, f"{name} does not take the shared lock"
        assert body.index("with _PROJECTS_CATALOG_LOCK:") < body.index("load_workspaces("), (
            f"{name} reads the workspace list before acquiring the lock"
        )
        assert body.index("with _PROJECTS_CATALOG_LOCK:") < body.index("save_workspaces("), (
            f"{name} saves the workspace list outside the lock"
        )


# --- finding 4: the sweep's live-binding read shares the catalog lock --------
# Greptile P1 2026-10-10T08:08:52Z ("Another save stops filing"):
# _auto_assign_live_binding read the projects catalog with no lock, while every
# mutation does load -> modify -> save (and save_projects writes with mode 'w',
# i.e. truncate-then-write), so a read that slipped into that window parsed an
# empty file, load_projects returned [], and the sweep stopped as though its
# project had been deleted - leaving chats unassigned while auto_assign was
# still ON. The read now holds _PROJECTS_CATALOG_LOCK.
def test_auto_assign_live_binding_reads_the_catalog_under_the_lock(monkeypatch):
    routes = _routes()
    probe = _ProbeLock()
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", probe)

    def _load():
        assert probe.held(), (
            "load_projects ran OUTSIDE _PROJECTS_CATALOG_LOCK - a concurrent "
            "save can truncate the file mid-read and the sweep stops filing"
        )
        return [{"project_id": "p1", "auto_assign": True, "workspaces": ["/ws/a"]}]

    monkeypatch.setattr(routes, "load_projects", _load)
    live = routes._auto_assign_live_binding("p1")
    assert probe.entries == 1, "the live-binding read must take the shared lock"
    assert live == (True, {"/ws/a"}), live


def test_auto_assign_live_binding_still_fails_closed(monkeypatch):
    """The lock must not soften the fail-CLOSED contract: a catalog that cannot
    be read (or that no longer has the row) still answers None, so the sweep
    stops instead of filing a chat it cannot prove is still wanted."""
    routes = _routes()
    monkeypatch.setattr(routes, "_PROJECTS_CATALOG_LOCK", _ProbeLock())
    monkeypatch.setattr(routes, "load_projects", lambda: [])
    assert routes._auto_assign_live_binding("gone") is None

    def _boom():
        raise RuntimeError("catalog unreadable")

    monkeypatch.setattr(routes, "load_projects", _boom)
    assert routes._auto_assign_live_binding("gone") is None

    monkeypatch.setattr(
        routes,
        "load_projects",
        lambda: [{"project_id": "p1", "auto_assign": False, "workspaces": ["/ws/a"]}],
    )
    assert routes._auto_assign_live_binding("p1") == (False, {"/ws/a"})


def test_auto_assign_live_binding_takes_the_lock_in_source():
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    start = src.index("def _auto_assign_live_binding(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert "with _PROJECTS_CATALOG_LOCK:" in body, "the live read does not take the shared lock"
    assert "_auto_assign_live_binding_locked(project_id)" in body, (
        "the wrapper must do its catalog read inside the shared lock"
    )
    # The reader itself is the function that touches the catalog, and it must
    # NOT take the lock: its callers (this wrapper, and
    # _auto_assign_claim_session's read+assign critical section) hold it.
    l_start = src.index("def _auto_assign_live_binding_locked(")
    l_end = src.index("\ndef ", l_start + 1)
    reader = src[l_start:l_end]
    assert "load_projects()" in reader, "the catalog read must live in the reader"
    assert "with _PROJECTS_CATALOG_LOCK:" not in reader, (
        "the reader must not take the lock itself - it runs inside the caller's "
        "critical section"
    )
