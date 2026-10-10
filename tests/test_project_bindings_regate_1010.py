"""Regressions for the maintainer re-gate of PR #6836 on head ``fcb070d7``
(review 5476450595, 2026-10-09T23:55:01Z).

Three findings, one section each:

[SILENT]  **"No project" silently files the chat.** ``/api/session/new`` treated
    an explicit ``project_id: null`` like an omitted field, so a New Chat
    started from the sidebar's unassigned-only view was auto-assigned to a
    project and then hidden from the view that created it (master's handler
    preserves null). The server now auto-assigns only when the field is ABSENT,
    and the client sends an explicit null for the "No project" filter.

[SHOULD-FIX] **Partial write on the default-only path.** ``default_workspace``
    without ``workspaces`` re-resolves the project's STORED workspace list, so a
    stored path that has since been removed from disk made that second resolve
    raise AFTER the candidate had already been registered. The whole-request
    pre-flight now validates those stored paths too — but only when the second
    resolve can actually run.

[SHOULD-FIX] **Arrows commit instead of highlighting.** The dropdown's
    ArrowDown/ArrowUp branches called ``click()`` on the next row, so the first
    ArrowDown committed: on the add list (first row "Type a path…") it opened
    the path prompt, and a saved workspace was reachable only by wrapping
    ArrowUp from the end. Arrows now move the highlight; Enter/Space commits.

Also covers the LOW noun item: ``pb_set_default_title`` used the locale's
"sessions" word in 12 locales — the per-locale noun assertion for that key lives
in ``test_project_bindings_uigate_1009.py`` next to the label/hint one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1 — /api/session/new: null is "no project", absent is "auto-assign"
# ---------------------------------------------------------------------------

_WS = "D:/ws-regate-1010"
_AUTO_PID = "proj_regate_auto_1010"


@pytest.fixture
def session_new_env(monkeypatch):
    """Drive /api/session/new in-process with only the leaf lookups faked."""
    import api.routes as routes

    auto_calls: list[str] = []
    created: list[dict] = []
    responses: list[dict] = []

    monkeypatch.setattr(routes, "load_projects", lambda *a, **k: [])
    monkeypatch.setattr(routes, "save_projects", lambda ps: None)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "save_workspaces", lambda wss: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p, **_kw: Path(p))
    monkeypatch.setattr(
        routes, "_resolve_new_session_workspace", lambda *a, **k: _WS
    )
    monkeypatch.setattr(
        routes, "_worktree_default_from_config", lambda profile=None: False
    )
    monkeypatch.setattr(
        routes, "_session_model_state_from_request", lambda m, p: ("model-x", None)
    )
    monkeypatch.setattr(routes, "_validate_session_toolsets_shape", lambda v: None)

    def _auto_assign(workspace, profile=None):  # noqa: ARG001
        auto_calls.append(workspace)
        return _AUTO_PID

    monkeypatch.setattr(
        routes, "_auto_assign_project_for_workspace", _auto_assign
    )

    class _Sess:
        session_id = "s_regate_1010"
        messages: list = []
        profile = "default"

        def compact(self):
            return {}

    def _fake_new_session(**kw):
        created.append(dict(kw))
        return _Sess()

    monkeypatch.setattr(routes, "new_session", _fake_new_session)
    monkeypatch.setattr(routes, "public_session_projection", lambda row: row)
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
    def _drive(body):
        monkeypatch.setattr(routes, "read_body", lambda handler: dict(body))
        return routes.handle_post(
            SimpleNamespace(command="POST"),
            SimpleNamespace(path="/api/session/new"),
        )

    return SimpleNamespace(
        routes=routes,
        auto_calls=auto_calls,
        created=created,
        responses=responses,
        drive=_drive,
    )


def test_an_explicit_null_project_id_is_not_auto_assigned(session_new_env):
    """[SILENT] The "No project" view sends null; that must stay unassigned.

    Reproduced on fcb070d7: ``body.get("project_id") or None`` made null look
    like an omission, so the chat was filed under ``_AUTO_PID`` and the
    unassigned-only sidebar view (which filters on the project id) showed zero
    rows for the chat it had just created.
    """
    env = session_new_env
    env.drive({"workspace": _WS, "project_id": None})

    assert env.responses and env.responses[-1]["status"] == 200, env.responses
    assert env.created, "new_session was never called"
    assert env.created[-1]["project_id"] is None, env.created[-1]
    assert env.auto_calls == [], (
        "an explicit null must never trigger auto-assignment, got " + repr(env.auto_calls)
    )


def test_an_absent_project_id_still_auto_assigns(session_new_env):
    """The auto-assign feature itself must survive the fix (absent = opt in)."""
    env = session_new_env
    env.drive({"workspace": _WS})

    assert env.responses and env.responses[-1]["status"] == 200, env.responses
    assert env.auto_calls == [_WS], env.auto_calls
    assert env.created[-1]["project_id"] == _AUTO_PID, env.created[-1]


def test_an_explicit_project_id_is_used_verbatim(session_new_env):
    """A caller that names a project keeps it, and is not re-assigned."""
    env = session_new_env
    env.drive({"workspace": _WS, "project_id": "proj_named_1010"})

    assert env.responses and env.responses[-1]["status"] == 200, env.responses
    assert env.created[-1]["project_id"] == "proj_named_1010", env.created[-1]
    assert env.auto_calls == [], env.auto_calls


def test_an_explicit_empty_project_id_keeps_masters_meaning(session_new_env):
    """'' was falsy on master too (unassigned); it must not start assigning."""
    env = session_new_env
    env.drive({"workspace": _WS, "project_id": ""})

    assert env.responses and env.responses[-1]["status"] == 200, env.responses
    assert env.created[-1]["project_id"] is None, env.created[-1]
    assert env.auto_calls == [], env.auto_calls


def test_the_no_project_view_sends_an_explicit_null():
    """The client half: the unassigned view must not omit the field."""
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.index(
        "if(Object.prototype.hasOwnProperty.call(options,'project_id')){"
    )
    end = src.index("// Forward a pre-session toolset override", start)
    block = src[start:end]

    assert "reqBody.project_id=options.project_id;" in block, block
    assert "else if(_activeProject===NO_PROJECT_FILTER){" in block, block
    assert "reqBody.project_id=null;" in block, block
    assert "reqBody.project_id=_activeProject;" in block, block
    assert block.index("reqBody.project_id=null;") < block.index(
        "reqBody.project_id=_activeProject;"
    ), "the unassigned view is matched before the project filter"
    # The old guard omitted the field for the "No project" view entirely, which
    # is what let the server auto-assign it.
    assert "_activeProject&&_activeProject!==NO_PROJECT_FILTER" not in block, block


# ---------------------------------------------------------------------------
# 2 — the pre-flight must cover the stored workspaces the default-only path
#     re-resolves (otherwise a dead stored path writes before it 400s)
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
    monkeypatch.setattr(workspace, "_home_path", lambda: home)
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
    proj = {"project_id": "proj_regate_1010", "name": "regate1010", "profile": "default"}
    proj.update(extra)
    return proj


def test_a_default_only_bind_with_a_dead_stored_workspace_writes_nothing(
    bind_env, monkeypatch
):
    """[SHOULD-FIX] Reproduced on fcb070d7: 400, but the candidate was saved.

    The project still binds ``removed-workspace`` (deleted from disk). A
    default-only bind of a FRESH path resolves the candidate (registering it)
    and only then re-resolves the stored list, which raises on the dead path.
    The request must fail before anything is registered.
    """
    dead = str(bind_env.outside.parent / "removed-workspace")
    fresh = str(bind_env.outside)
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(workspaces=[dead], default_workspace=dead),
        {"project_id": "proj_regate_1010", "default_workspace": fresh},
    )

    assert handled is True, "the route must answer, not raise"
    assert responses[-1]["status"] == 400, responses
    assert bind_env.registry == [], (
        "the rejected bind must not leave the candidate registered: "
        + repr(bind_env.registry)
    )
    assert proj["workspaces"] == [dead], proj
    assert proj["default_workspace"] == dead, proj


def test_a_default_only_bind_whose_default_is_already_bound_still_saves(
    bind_env, monkeypatch
):
    """The already-bound case must keep working: its second resolve never runs.

    The stored form is established by an ordinary bind first, exactly as the
    route writes it, because the field block compares the canonicalized default
    against the stored strings.
    """
    good = str(bind_env.outside)
    dead = str(bind_env.outside.parent / "removed-workspace")
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(),
        {"project_id": "proj_regate_1010", "workspaces": [good]},
    )
    assert responses[-1].get("status", 200) == 200, responses
    stored = proj["workspaces"][0]
    assert stored == good, stored

    # The default is the bound workspace, and an unrelated stored path is gone
    # from disk: the field block short-circuits before re-resolving the list, so
    # this request must still succeed (and the pre-flight must not over-reach
    # and validate the dead stored path).
    proj["workspaces"] = [stored, dead]
    proj["default_workspace"] = stored
    handled, responses, proj = _drive_bind(
        monkeypatch,
        proj,
        {"project_id": "proj_regate_1010", "default_workspace": stored},
    )

    assert handled is True
    assert responses[-1].get("status", 200) == 200, responses
    assert proj["default_workspace"] == stored, proj
    assert proj["workspaces"] == [stored, dead], proj


def test_a_legacy_workspace_replacement_of_a_dead_binding_still_saves(
    bind_env, monkeypatch
):
    """Greptile P2 (2026-10-10T00:24:37Z): the legacy alias REPLACES the stored
    set before the default block runs, so replacing a deleted binding with a
    live one is valid and must not be rejected by the stored-path pre-flight."""
    good = str(bind_env.outside)
    dead = str(bind_env.outside.parent / "removed-workspace")
    handled, responses, proj = _drive_bind(
        monkeypatch,
        _project(workspaces=[dead], default_workspace=dead, workspace=dead),
        {
            "project_id": "proj_regate_1010",
            "workspace": good,
            "default_workspace": good,
        },
    )

    assert handled is True, "the route must answer, not raise"
    assert responses[-1].get("status", 200) == 200, responses
    assert proj["workspaces"] == [good], proj
    assert proj["workspace"] == good, proj
    assert proj["default_workspace"] == good, proj


# ---------------------------------------------------------------------------
# 3 — frontend: arrows move the highlight, Enter/Space commits
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


_HIGHLIGHT_PROBE = (
    _DOM_STUB
    + _COMBO_FN
    + r"""
const combo = _makeBindingsCombo({
  value: 'b',
  options: [{ value: 'a', name: 'A' }, { value: 'b', name: 'B' }, { value: 'c', name: 'C' }],
});
const trigger = combo.el.children[0];
const menu = combo.el.children[1];

function rows() { return menu.querySelectorAll('.ws-opt'); }
function activeName() {
  const r = rows().find((x) => x.classList.contains('active'));
  if (!r) return null;
  return (r.children[0] && r.children[0].textContent) || null;
}
function key(k) {
  const event = { key: k, _prevented: false, preventDefault() { event._prevented = true; } };
  trigger.onkeydown(event);
  return event._prevented;
}

// --- open, then move the highlight: nothing may be committed ---
key('ArrowDown');
assert(menu.classList.contains('open'), 'ArrowDown on a closed combo must open the menu');
assert(combo.getValue() === 'b', 'opening the menu must not change the value');
assert(activeName() === 'B', 'the current value starts highlighted, got ' + activeName());

key('ArrowDown');
assert(combo.getValue() === 'b',
  'ArrowDown must NOT commit a value, got ' + combo.getValue());
assert(menu.classList.contains('open'), 'ArrowDown must leave the menu open');
assert(activeName() === 'C', 'ArrowDown must move the highlight to C, got ' + activeName());

key('ArrowDown');
assert(activeName() === 'A', 'ArrowDown must wrap to the first row, got ' + activeName());
assert(combo.getValue() === 'b', 'wrapping must not commit either');

key('ArrowUp');
assert(activeName() === 'C', 'ArrowUp must wrap to the last row, got ' + activeName());

// --- Enter commits the highlighted row ---
const prevented = key('Enter');
assert(prevented === true, 'Enter must be consumed by the combobox');
assert(combo.getValue() === 'c', 'Enter must commit the highlighted row, got ' + combo.getValue());
assert(!menu.classList.contains('open'), 'committing closes the menu');

// --- Space commits too ---
key('ArrowDown');            // reopen; value 'C' is highlighted
key('ArrowUp');              // C -> B
key(' ');
assert(combo.getValue() === 'b', 'Space must commit like Enter, got ' + combo.getValue());
assert(!menu.classList.contains('open'), 'Space commits and closes');

// --- Escape still closes without committing ---
key('ArrowDown');            // reopen; value 'B' is highlighted
key('ArrowUp');              // B -> A (highlight only)
key('Escape');
assert(!menu.classList.contains('open'), 'Escape closes the dropdown');
assert(combo.getValue() === 'b', 'Escape must not commit, got ' + combo.getValue());

// --- the reported bug: the add list opens with the "type a path" row ---
const add = _makeBindingsCombo({
  value: '',
  options: [
    { value: '__custom_path__', name: 'Type a path' },
    { value: '/ws/saved', name: 'saved' },
  ],
});
const aTrigger = add.el.children[0];
const aMenu = add.el.children[1];
function aRows() { return aMenu.querySelectorAll('.ws-opt'); }
function aActiveName() {
  const r = aRows().find((x) => x.classList.contains('active'));
  if (!r) return null;
  return (r.children[0] && r.children[0].textContent) || null;
}
let commits = 0;
add.setOnChange(() => { commits += 1; });
function aKey(k) {
  const event = { key: k, _prevented: false, preventDefault() { event._prevented = true; } };
  aTrigger.onkeydown(event);
  return event._prevented;
}

aKey('ArrowDown');
assert(aMenu.classList.contains('open'), 'the add list opens');
assert(add.getValue() === '' && commits === 0, 'opening must not pick anything');
aKey('ArrowDown');
assert(aActiveName() === 'Type a path',
  'the first arrow press highlights the first row, got ' + aActiveName());
assert(add.getValue() === '' && commits === 0,
  'ArrowDown over the custom row must NOT commit (that opened the path prompt)');
assert(aMenu.classList.contains('open'), 'the add list stays open');
aKey('ArrowDown');
assert(aActiveName() === 'saved',
  'ArrowDown must reach the saved workspace, got ' + aActiveName());
assert(commits === 0, 'moving must not commit');
aKey('Enter');
assert(commits === 1 && add.getValue() === '/ws/saved',
  'Enter commits the highlighted row, got value=' + add.getValue() + ' commits=' + commits);
assert(!aMenu.classList.contains('open'), 'committing closes the add list');
console.log('ok');
"""
)


def test_arrows_move_the_highlight_and_enter_commits(tmp_path):
    """[SHOULD-FIX]: arrows must not commit; Enter/Space must."""
    assert _run_node(tmp_path, "combo_highlight.js", _HIGHLIGHT_PROBE).strip().endswith(
        "ok"
    )


def test_the_moved_highlight_is_scrolled_into_view():
    """Greptile P2 (2026-10-10T00:24:37Z): a highlight moved past the visible
    rows must scroll into view, or Enter commits an option the keyboard user
    cannot see (the menu is height-capped and scrolls)."""
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    fn = src[
        src.index("function _setHighlight(row){") : src.index(
            "function _moveHighlight(delta){"
        )
    ]
    assert "scrollIntoView" in fn, fn
    # Guarded so the layout-less mini-DOM probes keep working.
    assert "typeof row.scrollIntoView==='function'" in fn, fn
    assert "block:'nearest'" in fn, fn


# ---------------------------------------------------------------------------
# 4 — the sweep must fail CLOSED when it cannot confirm who owns a row
# ---------------------------------------------------------------------------


def _row(**extra):
    row = SimpleNamespace(
        session_id="s_failclosed",
        read_only=False,
        source_tag="",
        raw_source="",
        session_source="",
    )
    for key, value in extra.items():
        setattr(row, key, value)
    return row


def _broken_state_db(monkeypatch, tmp_path, name="broken-state.db"):
    """Point state.db at an unreadable sqlite file (exists, cannot be queried)."""
    import api.models as models

    path = tmp_path / name
    path.write_bytes(b"this is not a sqlite database\n" * 8)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: path)
    return path


def test_an_unreadable_state_db_makes_the_sweep_skip_the_row(monkeypatch, tmp_path):
    """Greptile P2 (2026-10-10T00:47:29Z): an ownership that cannot be confirmed
    must not authorise a write — AGENTS.md: unknown is not allowed.

    Pre-fix the guard returned False on a failed lookup, so the sweep filed a row
    it could not prove was not a delegated child.
    """
    import api.routes as routes

    _broken_state_db(monkeypatch, tmp_path)
    assert routes._state_db_session_source_strict("s_failclosed") is None
    # ...while the historical seam still collapses it to "" for every other caller.
    assert routes._state_db_session_source("s_failclosed") == ""
    assert routes._auto_assign_target_is_view_only(_row(), "s_failclosed") is True


def test_a_missing_state_db_also_fails_closed(monkeypatch, tmp_path):
    """No state.db at all is "cannot confirm" too, not "nothing recorded"."""
    import api.models as models
    import api.routes as routes

    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "nope.db")
    assert routes._state_db_session_source_strict("s_failclosed") is None
    assert routes._auto_assign_target_is_view_only(_row(), "s_failclosed") is True


def test_a_readable_state_db_keeps_the_previous_decisions(monkeypatch, tmp_path):
    """Control: a real state.db still files ordinary rows and skips children."""
    import sqlite3

    import api.models as models
    import api.routes as routes

    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
    conn.execute("INSERT INTO sessions (id, source) VALUES ('s_child', 'subagent')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: path)

    assert routes._state_db_session_source_strict("s_child") == "subagent"
    assert routes._state_db_session_source_strict("s_absent") == ""
    assert routes._auto_assign_target_is_view_only(_row(), "s_child") is True
    assert routes._auto_assign_target_is_view_only(_row(), "s_absent") is False
    # The source tag / read-only short-circuits still win before any lookup.
    assert routes._auto_assign_target_is_view_only(_row(read_only=True), "s_absent") is True
    assert routes._auto_assign_target_is_view_only(_row(source_tag="Subagent"), "s_absent") is True


def test_a_lookup_that_raises_is_skipped_too(monkeypatch):
    """Even an exception escaping the strict probe must not file the row."""
    import api.routes as routes

    def _boom(sid):
        raise RuntimeError("state.db exploded")

    monkeypatch.setattr(routes, "_state_db_session_source_strict", _boom)
    assert routes._auto_assign_target_is_view_only(_row(), "s_failclosed") is True
