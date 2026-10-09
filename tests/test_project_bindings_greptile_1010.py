"""Regressions for the Greptile re-review of PR #6836 run on the post-master-merge
head ``05d2d53b`` (inline comments, 2026-10-09T21:47:48Z).

Five findings from that pass, one test class each:

P1  **Valid defaults cannot be saved.** ``default_workspace`` was resolved with
    ``resolve_trusted_workspace`` directly, so a path that is not in the saved
    list yet and lives outside home was rejected BEFORE the documented
    auto-registration could run, and a path pasted from Finder (surrounding
    quote pair) kept its quotes and failed. The default now goes through the
    same helper as ``workspaces``.
P2  **Invalid lists cause server errors.** ``workspaces`` reached
    ``_resolve_ws_list`` without a shape check, so a number/bool raised an
    uncaught ``TypeError`` (500) and a string iterated as characters.
P2  **Failed saves leave partial changes.** Registration of a workspace path is
    a side effect on the saved list; a rejected later entry — or a rejected
    ``reasoning_effort`` — used to leave the earlier paths registered.
P2  **Dropdown keys do nothing.** ``trigger.onkeydown`` tested ArrowDown in its
    first branch, so the "menu is open" ArrowDown branch was unreachable and
    Enter/Space were swallowed without choosing a row.
P2  **Closed dialogs stay in memory.** Each combobox's document click listener
    was anonymous and never removed; the dialog now destroys both on close.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# API side: drive /api/projects/bind in-process with the REAL workspace trust
# helpers (only the two registries + the home dir are faked) so the trust rule
# under test is the shipped one.
# ---------------------------------------------------------------------------


@pytest.fixture
def bind_env(monkeypatch, tmp_path):
    """A fake home + a fake workspace registry, real trust helpers."""
    import api.routes as routes
    import api.workspace as workspace

    home = tmp_path / "fake-home"
    home.mkdir()
    outside = tmp_path / "outside-home"
    outside.mkdir()
    registry: list[dict] = []

    def _load_ws(profile=None):  # noqa: ARG001
        return [dict(w) for w in registry]

    def _save_ws(items, profile=None):  # noqa: ARG001
        registry[:] = [dict(w) for w in items]

    monkeypatch.setattr(routes, "load_workspaces", _load_ws)
    monkeypatch.setattr(routes, "save_workspaces", _save_ws)
    monkeypatch.setattr(workspace, "load_workspaces", _load_ws)
    monkeypatch.setattr(workspace, "save_workspaces", _save_ws)
    # Make the fake home the ONLY home, so `outside` is genuinely outside it.
    monkeypatch.setattr(workspace, "_home_path", lambda: home)
    # Keep the boot-default carve-out from making tmp_path trusted by accident.
    monkeypatch.setattr(
        workspace, "_BOOT_DEFAULT_WORKSPACE", str(tmp_path / "no-boot-default")
    )
    return SimpleNamespace(home=home, outside=outside, registry=registry)


def _drive_bind(monkeypatch, project, body):
    """POST /api/projects/bind in-process; returns (handled, responses, project)."""
    import api.routes as routes

    projects = [project]
    monkeypatch.setattr(routes, "load_projects", lambda: projects)
    monkeypatch.setattr(
        routes, "save_projects", lambda ps: projects.__setitem__(slice(None), ps)
    )
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: dict(body))
    responses: list[dict] = []
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None, **kw: responses.append(
            {"payload": payload, "status": status}
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: responses.append(
            {"error": msg, "status": status}
        )
        or True,
    )
    handled = routes.handle_post(
        SimpleNamespace(command="POST"),
        SimpleNamespace(path="/api/projects/bind"),
    )
    return handled, responses, projects[0]


def _project(**extra):
    proj = {"project_id": "proj_g1010", "name": "g1010", "profile": "default"}
    proj.update(extra)
    return proj


def test_a_fresh_default_workspace_outside_home_is_registered_and_bound(
    bind_env, monkeypatch
):
    """Greptile P1: the default must go through the auto-registering helper.

    `outside` exists but is (with the fake home) outside home and not in the
    saved list, so the old direct `resolve_trusted_workspace` call raised
    "Path is outside the user home directory ..." and the save failed before
    the promise "auto-added if not in workspaces" could run.
    """
    outside = str(bind_env.outside)
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(),
        {"project_id": "proj_g1010", "default_workspace": outside},
    )

    assert handled is True
    assert responses and responses[-1].get("status", 200) == 200, responses
    assert proj["default_workspace"] == str(bind_env.outside)
    assert proj["workspaces"] == [str(bind_env.outside)]
    assert proj["workspace"] == str(bind_env.outside)  # legacy alias
    assert [w["path"] for w in bind_env.registry] == [outside]


def test_a_finder_quoted_default_workspace_is_accepted(bind_env, monkeypatch):
    """Greptile P1: `'<path>'` (Cmd+Option+C) must bind like the bare path."""
    outside = bind_env.outside
    quoted = f"'{outside}'"
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(),
        {"project_id": "proj_g1010", "default_workspace": quoted},
    )

    assert handled is True
    assert responses and responses[-1].get("status", 200) == 200, responses
    assert proj["default_workspace"] == str(outside)
    assert proj["workspaces"] == [str(outside)]


@pytest.mark.parametrize("bad_value", [7, True, {"a": 1}])
def test_a_non_list_workspaces_field_is_a_bad_request(bind_env, monkeypatch, bad_value):
    """Greptile P2: a number/bool/object must not reach the list iteration."""
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(),
        {"project_id": "proj_g1010", "workspaces": bad_value},
    )

    assert handled is True, "the route must answer, not raise"
    assert responses[-1]["status"] == 400, responses
    assert "list" in responses[-1]["error"]
    assert "workspaces" not in proj
    assert bind_env.registry == []


def test_a_rejected_second_workspace_registers_nothing(bind_env, monkeypatch):
    """Greptile P2: validation must complete before anything is registered."""
    good = str(bind_env.outside)
    missing = str(bind_env.outside.parent / "does-not-exist")
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(),
        {"project_id": "proj_g1010", "workspaces": [good, missing]},
    )

    assert handled is True
    assert responses[-1]["status"] == 400, responses
    assert bind_env.registry == [], "the good path must not be left registered"
    assert "workspaces" not in proj
    assert "default_workspace" not in proj


def test_a_rejected_reasoning_effort_registers_no_workspaces(bind_env, monkeypatch):
    """Greptile P2: a later field rejection must not leave earlier saves behind."""
    good = str(bind_env.outside)
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(),
        {
            "project_id": "proj_g1010",
            "workspaces": [good],
            "reasoning_effort": "absolutely-not-an-effort",
        },
    )

    assert handled is True
    assert responses[-1]["status"] == 400, responses
    assert "reasoning_effort" in responses[-1]["error"]
    assert bind_env.registry == []
    assert "workspaces" not in proj


# ---------------------------------------------------------------------------
# Frontend: a mini-DOM just big enough for the shipped combobox
# ---------------------------------------------------------------------------


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
    type: '',
    title: '',
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
    click() {
      // Real .click() dispatches a click event; the shipped rows listen via
      // onclick, so that is what the probe must drive.
      if (typeof node.onclick === 'function') {
        node.onclick({ stopPropagation() {}, preventDefault() {} });
      }
    },
    addEventListener() {},
  };
  Object.defineProperty(node, 'className', {
    get() { return node._className; },
    set(v) {
      node._className = String(v);
      // Keep the shared set in sync: the shipped code sets className directly
      // (row.className='ws-opt active'), and querySelectorAll/contains must see
      // the same classes a real DOM would.
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

// Document-level click listeners are tracked so destroy() can be observed.
const __docClick = [];
const _document = {
  createElement: (tag) => makeElement(tag),
  addEventListener: (type, fn) => { if (type === 'click') __docClick.push(fn); },
  removeEventListener: (type, fn) => {
    if (type !== 'click') return;
    const i = __docClick.indexOf(fn);
    if (i >= 0) __docClick.splice(i, 1);
  },
};
globalThis.document = _document;
globalThis.window = { innerHeight: 800, addEventListener: () => {} };
globalThis.t = (key) => key;
"""

_COMBO_FN = (
    (REPO_ROOT / "static" / "sessions.js")
    .read_text(encoding="utf-8")
    .split("function _makeBindingsCombo(o){", 1)[1]
    .split("\n// Modal dialog for editing a project's bindings", 1)[0]
)
_COMBO_FN = "function _makeBindingsCombo(o){" + _COMBO_FN


_KEYBOARD_PROBE = (
    _DOM_STUB
    + _COMBO_FN
    + r"""
const combo = _makeBindingsCombo({
  value: 'b',
  options: [{ value: 'a', name: 'A' }, { value: 'b', name: 'B' }, { value: 'c', name: 'C' }],
});
const trigger = combo.el.children[0];
const menu = combo.el.children[1];

function key(k) {
  const event = { key: k, _prevented: false, preventDefault() { event._prevented = true; } };
  trigger.onkeydown(event);
  return event._prevented;
}

assert(!menu.classList.contains('open'), 'the menu starts closed');
key('ArrowDown');
assert(menu.classList.contains('open'), 'ArrowDown on a closed combo must open the menu');

// The reported bug: with the menu OPEN, the first branch ate ArrowDown and the
// later "menu is open" branch never ran, so the highlight could not move.
key('ArrowDown');
assert(combo.getValue() === 'c',
  'ArrowDown with the menu open must move to the next option, got ' + combo.getValue());
assert(!menu.classList.contains('open'), 'choosing an option closes the menu');

// Ensure the move wraps rather than dead-ending.
key('ArrowDown');            // reopen (value 'c')
key('ArrowDown');            // c -> a (wraps)
assert(combo.getValue() === 'a', 'ArrowDown must wrap around the option list, got ' + combo.getValue());

key('ArrowDown');            // reopen (value 'a')
key('ArrowUp');
assert(combo.getValue() === 'c', 'ArrowUp must wrap backwards, got ' + combo.getValue());

// Enter/Space must CONFIRM the highlighted row instead of being swallowed.
key('ArrowDown');            // reopen
assert(menu.classList.contains('open'), 'the menu reopened');
const prevented = key('Enter');
assert(prevented === true, 'Enter must be consumed by the combobox');
assert(!menu.classList.contains('open'),
  'Enter must confirm the highlighted option and close the menu');
assert(combo.getValue() === 'c', 'Enter keeps the highlighted value, got ' + combo.getValue());

key('ArrowDown');            // reopen
key(' ');
assert(!menu.classList.contains('open'), 'Space must confirm like Enter');

// Escape still closes without choosing.
key('ArrowDown');
key('Escape');
assert(!menu.classList.contains('open'), 'Escape closes the dropdown');
console.log('ok');
"""
)


def test_combobox_keyboard_navigation_moves_and_confirms(tmp_path):
    """Greptile P2: ArrowDown/Enter must work while the dropdown is open."""
    assert _run_node(tmp_path, "combo_keys.js", _KEYBOARD_PROBE).strip().endswith("ok")


_DESTROY_PROBE = (
    _DOM_STUB
    + _COMBO_FN
    + r"""
const first = _makeBindingsCombo({ value: 'a', options: [{ value: 'a', name: 'A' }] });
const second = _makeBindingsCombo({ value: 'a', options: [{ value: 'a', name: 'A' }] });
assert(__docClick.length === 2,
  'each combo installs exactly one document click listener, got ' + __docClick.length);
assert(typeof first.destroy === 'function', 'the combo must expose destroy()');
first.destroy();
assert(__docClick.length === 1,
  'destroy() must remove the combo\'s own listener, got ' + __docClick.length);
second.destroy();
assert(__docClick.length === 0, 'both listeners must be removable, got ' + __docClick.length);
// destroy() twice must not throw or remove somebody else's listener.
first.destroy();
assert(__docClick.length === 0, 'destroy() must be idempotent');
console.log('ok');
"""
)


def test_combobox_destroy_removes_its_document_listener(tmp_path):
    """Greptile P2: a closed dialog must not keep its click listeners."""
    assert _run_node(tmp_path, "combo_destroy.js", _DESTROY_PROBE).strip().endswith("ok")


def test_the_dialog_destroys_both_comboboxes_when_it_closes():
    """The leak is only fixed if the dialog actually calls destroy()."""
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    dialog = src[
        src.index("function _showProjectBindingsDialog(proj){") :
        src.index("function _startProjectRename(proj, chip){")
    ]
    close_fn = dialog[
        dialog.index("function _closeBindingsDialog(){") :
        dialog.index("function _closeOpenCombo(){")
    ]
    assert "addCombo.destroy()" in close_fn
    assert "modelCombo.destroy()" in close_fn
    assert "overlay.remove()" in close_fn, "the overlay must still be removed"
