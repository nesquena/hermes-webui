"""Executable ownership regressions for the Files-tree "Set as New Workspace" flow.

The static pins in ``test_dir_set_as_workspace_menu.py`` prove the wiring exists.
They cannot prove what happens when the user moves to another session or profile
*while an await is pending*, which is the defect this file pins: the action
resolves a path, registers a folder and asks for confirmation across three
round-trips, and each of those can land after the foreground session changed.

So these run the real functions. ``_setDirectoryAsWorkspace`` and
``switchToWorkspace`` are sliced out of ``static/ui.js`` / ``static/panels.js`` and
executed in a Node ``vm`` with mocked API/dialog responses released by hand — the
same technique as ``test_media_preview_session_lifetime.py``. Every stale case is
paired with a same-session control that must still complete, so the guard cannot
pass by never doing anything.

Not proved here: browser behaviour. No menu renders, no click is dispatched, and
these mocks are ours — they show our intent, not a browser's contract.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
UI = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
PANELS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

_LONG_PRESS_START = "    // Touch long-press state. The timer handle lives in the module-level"
_LONG_PRESS_END = "    el.ondragstart="


def _slice_function(src: str, name: str) -> str:
    """Return a top-level `function name(...){...}` / `async function ...` by brace balance."""
    marker = f"function {name}("
    start = src.index(marker)
    if src[max(0, start - 6):start] == "async ":
        start -= 6
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces while slicing {name}()")


def _sources() -> dict[str, str]:
    return {
        "helpers": "\n".join(
            _slice_function(UI, n)
            for n in ("_foregroundTurnToken", "_foregroundTurnTokenIsCurrent",
                      "_foregroundIdentityIsCurrent")
        ),
        "longPressState": UI[UI.index("let _treeRenderGeneration=0;"):UI.index("function renderFileTree(){")],
        "setAsWorkspace": _slice_function(UI, "_setDirectoryAsWorkspace"),
        "switchToWorkspace": _slice_function(PANELS, "switchToWorkspace"),
        "longPressHandlers": UI[UI.index(_LONG_PRESS_START):UI.index(_LONG_PRESS_END)],
    }


def _run(driver: str, payload: dict) -> dict:
    proc = subprocess.run(
        [NODE, "-e", driver, json.dumps(payload)],
        capture_output=True, text=True, timeout=90,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node failed rc={proc.returncode}\n{proc.stderr}\n{proc.stdout}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    if "error" in result:
        raise AssertionError(f"driver error: {result['error']}")
    return result


# ── Ownership across the three awaits ────────────────────────────────────────

_OWNERSHIP_DRIVER = r"""
const vm = require('vm');
const P = JSON.parse(process.argv[1]);
const src = P.sources;
const scenario = P.scenario;
// Exactly one step is held open; every other response resolves immediately, so the
// flow always reaches its end and a hang means the guard did not exist.
const HOLD = scenario.hold || null;
const CONFIRM = scenario.confirm !== false;
let dialogCalls = 0;

const calls = [], toasts = [], statuses = [];
const pending = {};
// Exposed on the vm context so code running *inside* the context can reach them.
function defer(name) { return new Promise(r => { pending[name] = r; }); }
function release(name, val) { const r = pending[name]; delete pending[name]; if (r) r(val); }
function fakeEl() {
  return {style:{}, dataset:{}, classList:{add(){},remove(){},contains(){return false;}},
    setAttribute(){}, appendChild(){}, addEventListener(){}, remove(){},
    querySelectorAll(){return [];}, getBoundingClientRect(){return {width:140,height:300};}};
}

const context = {
  console, setTimeout, clearTimeout, Promise, JSON, Array, Object, String, Number,
  Boolean, Error, RegExp, Math,
  calls, toasts, statuses, release,
  S: {session:{session_id:'A', workspace:'/ws/A', profile:'default', model:'m', model_provider:null},
      activeProfile:'default', busy:false, messages:[{role:'user'}]},
  _workspaceList: [{path:'/ws/A'}],
  _workspaceEscapeGrantForPath: () => null,
  _isReadOnlySession: () => false,
  _currentPanel: 'files',
  _previewDirty: !!scenario.previewDirty,
  t: (k, a) => (a === undefined ? k : k + ':' + a),
  showToast: m => toasts.push(m),
  setStatus: m => statuses.push(m),
  getWorkspaceFriendlyName: p => String(p).split('/').filter(Boolean).pop() || p,
  renderWorkspacesPanel: l => { context.__rendered = l; },
  // Call 1 is the action's own switch prompt; call 2 is switchToWorkspace's
  // dirty-preview discard prompt (only reached when _previewDirty is set).
  showConfirmDialog: () => {
    dialogCalls++;
    if (HOLD === 'confirm' && dialogCalls === 1) return defer('confirm');
    if (HOLD === 'discard' && dialogCalls === 2) return defer('discard');
    return Promise.resolve(CONFIRM);
  },
  api: (url, opts) => {
    let body = null;
    try { body = opts && opts.body ? JSON.parse(opts.body) : null; } catch (e) { body = opts && opts.body; }
    calls.push({url, body});
    if (url === '/api/file/path') {
      return HOLD === 'path' ? defer('path') : Promise.resolve({path:'/ws/A/child'});
    }
    if (url === '/api/workspaces/add') {
      return HOLD === 'add' ? defer('add') : Promise.resolve({workspaces:[{path:'/ws/A/child'}]});
    }
    if (url === '/api/session/update') {
      return HOLD === 'update' ? defer('update') : Promise.resolve({ok: true});
    }
    return Promise.resolve({});
  },
  syncTopbar(){}, closeWsDropdown(){}, bumpWorkspaceTreeGen(){},
  loadDir: async () => {}, loadMemory: async () => {}, newSession: async () => {},
  cancelEditMode(){}, clearPreview(){}, $: () => null,
  document: {body:{appendChild(){}}, addEventListener(){}, removeEventListener(){},
             querySelectorAll(){return [];}, createElement: fakeEl, baseURI:'http://x/'},
  window: {innerWidth:390, innerHeight:680, _newChatOnWorkspaceSwitch:false},
};
context.window.document = context.document;
vm.createContext(context);

vm.runInContext(src.helpers + '\n' + src.longPressState + '\n'
  + src.setAsWorkspace + '\n' + src.switchToWorkspace, context);

vm.runInContext(`
__run = async function(scenario){
  const HOLD = scenario.hold || null;
  const item = {type:'dir', path:'child', name:'child'};
  const tick = () => new Promise(r => setTimeout(r, 0));
  const flow = _setDirectoryAsWorkspace(item);
  // Let the flow advance to (and park on) the held step.
  await tick(); await tick(); await tick();
  const away = scenario.away;
  if (away === 'session') {
    S.session = {session_id:'B', workspace:'/ws/B', profile:'default', model:'m', model_provider:null};
  } else if (away === 'profile') {
    S.activeProfile = 'other';
  } else if (away === 'workspace') {
    S.session.workspace = '/ws/A/moved';
  }
  if (HOLD === 'path') release('path', {path:'/ws/A/child'});
  else if (HOLD === 'add') release('add', {workspaces:[{path:'/ws/A/child'}]});
  else if (HOLD === 'confirm') release('confirm', true);
  else if (HOLD === 'discard') release('discard', true);
  else if (HOLD === 'update') release('update', {ok: true});
  await flow;
  return {calls, toasts, statuses,
          finalWorkspace: S.session && S.session.workspace,
          finalSessionId: S.session && S.session.session_id};
};`, context);

(async () => {
  try {
    const r = await vm.runInContext('__run(' + JSON.stringify(scenario) + ')', context);
    r.adoptedList = context.__rendered || null;
    r.heldReleased = Object.keys(pending).length === 0;
    process.stdout.write(JSON.stringify(r));
  } catch (e) {
    process.stdout.write(JSON.stringify({error: String((e && e.stack) || e)}));
  }
})();
"""


def _urls(result: dict) -> list[str]:
    return [c["url"] for c in result["calls"]]


STALE_CASES = [
    ("path", "session"),
    ("path", "profile"),
    ("path", "workspace"),
    ("add", "session"),
    ("confirm", "session"),
]

# These two change the foreground *after* the action handed control to
# switchToWorkspace, so they exercise that function's own awaits. Without its
# internal re-checks the caller's single pre-call check is not enough — which is
# the case the review called out explicitly.
IN_SWITCH_CASES = [
    ("update", "session", False),
    ("discard", "session", True),
]


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("hold,away", STALE_CASES)
def test_stale_action_never_retargets_the_new_foreground(hold, away):
    """If the foreground changed while an await was pending, act on nothing."""
    result = _run(_OWNERSHIP_DRIVER,
                  {"sources": _sources(),
                   "scenario": {"hold": hold, "away": away, "confirm": True}})
    urls = _urls(result)

    assert "/api/session/update" not in urls, "stale action mutated a session it did not start on"
    assert result["finalWorkspace"] != "/ws/A/child", "stale action rewired the new foreground session"
    assert "/api/session/new" not in urls, "stale action auto-created a session"
    if away == "session":
        assert result["finalSessionId"] == "B", "the switch must not replace the session the user selected"
    if away == "profile":
        assert "/api/workspaces/add" not in urls, "registered under a profile the user had left"


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("hold,away,preview_dirty", IN_SWITCH_CASES)
def test_foreground_change_inside_the_switch_itself_is_ignored(hold, away, preview_dirty):
    """The switch's own awaits must re-check too, not just the caller's one check.

    `update` holds the /api/session/update round-trip; `discard` holds the
    dirty-preview modal that precedes it. In both the user moves to another
    conversation mid-flight: nothing may be written, and the newly selected
    session must keep its own workspace.
    """
    result = _run(_OWNERSHIP_DRIVER,
                  {"sources": _sources(),
                   "scenario": {"hold": hold, "away": away, "confirm": True,
                                "previewDirty": preview_dirty}})
    assert result["finalSessionId"] == "B"
    assert result["finalWorkspace"] == "/ws/B", "the new session was rewired mid-switch"
    update = next((c for c in result["calls"] if c["url"] == "/api/session/update"), None)
    if update is not None:
        assert update["body"]["session_id"] == "A", (
            "the update must be issued for the session the action started on"
        )


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("hold", ["path", "add", "confirm", "update"])
def test_same_session_control_still_completes(hold):
    """Positive control: with nothing changed the action must still register and switch."""
    result = _run(_OWNERSHIP_DRIVER,
                  {"sources": _sources(),
                   "scenario": {"hold": hold, "away": None, "confirm": True}})
    urls = _urls(result)

    assert urls == ["/api/file/path", "/api/workspaces/add", "/api/session/update"], urls
    update = next(c for c in result["calls"] if c["url"] == "/api/session/update")
    assert update["body"]["session_id"] == "A"
    assert update["body"]["workspace"] == "/ws/A/child"
    assert result["finalWorkspace"] == "/ws/A/child"


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_registration_survives_declining_the_switch():
    """Declining the switch keeps the registration — the documented coherent state."""
    result = _run(_OWNERSHIP_DRIVER,
                  {"sources": _sources(),
                   "scenario": {"hold": None, "away": None, "confirm": False}})
    urls = _urls(result)
    assert "/api/workspaces/add" in urls
    assert "/api/session/update" not in urls
    assert result["finalWorkspace"] == "/ws/A"


# ── Long-press ownership ─────────────────────────────────────────────────────

_LONG_PRESS_DRIVER = r"""
const vm = require('vm');
const P = JSON.parse(process.argv[1]);
const src = P.sources;

const timers = [];
const opened = [];
let cleared = 0;

function fakeEl() {
  const handlers = {};
  return {style:{}, dataset:{}, isConnected:true,
    classList:{add(){},remove(){},contains(){return false;}},
    setAttribute(){}, appendChild(){}, remove(){},
    addEventListener(n, f){ handlers[n] = f; }, handlers,
    getBoundingClientRect(){return {width:140,height:300};}};
}

const context = {
  console, Promise, JSON, Array, Object, String, Number, Boolean, Error, RegExp, Math,
  S:{session:{session_id:'A', workspace:'/ws/A', profile:'default'}, activeProfile:'default', busy:false},
  _workspaceList: [],
  _workspaceEscapeGrantForPath: () => null,
  _showFileContextMenu: (e, item) => opened.push(item && item.path),
  _workspaceContextMenuItem: () => fakeEl(),
  _setWsDragData(){}, _clearWorkspaceMoveDragOver(){}, _clearWsDragData(){},
  _workspaceParentDir: p => String(p).split('/').slice(0, -1).join('/'),
  setTimeout: (fn, ms) => { timers.push({fn, ms, cleared:false}); return timers.length; },
  clearTimeout: id => { if (timers[id - 1]) { timers[id - 1].cleared = true; cleared++; } },
  document: {createElement: fakeEl},
  t: k => k,
};
vm.createContext(context);
vm.runInContext(src.helpers + '\n' + src.longPressState, context);

// Compile the real handler block *inside* the context: the module-level `let`s it
// closes over are global-lexical bindings, not properties of globalThis, so a
// function compiled in the host realm could not see them.
vm.runInContext(
  '__wire = function(el, item){'
  + ' const f = new Function("el", "item", ' + JSON.stringify(P.handlerSource) + ');'
  + ' return f(el, item); };', context);

vm.runInContext(`
__fire = function(el, scenario){
  const item = scenario === 'readonly' ? {type:'file', path:'child'} : {type:'dir', path:'child'};
  __wire(el, item);
  el.handlers.touchstart({changedTouches:[{clientX:10, clientY:10}]});
  if (scenario === 'detached') el.isConnected = false;
  if (scenario === 'switched') S.session = {session_id:'B', workspace:'/ws/B', profile:'default'};
  if (scenario === 'profileSwitched') S.activeProfile = 'other';
  if (scenario === 'rerendered') { _cancelTreeRowLongPress(); _treeRenderGeneration++; }
};`, context);

if (P.scenario === 'readonly') {
  // A read-only external grant: right-click on such a row deliberately shows
  // nothing, so the long-press path must reach the same conclusion.
  vm.runInContext('_workspaceEscapeGrantForPath = p => ({path:p});', context);
}

context.__el = fakeEl();
vm.runInContext('__fire(__el, ' + JSON.stringify(P.scenario) + ')', context);

// Fire whatever the 500ms gesture armed.
for (const t of timers) { if (!t.cleared) t.fn(); }
process.stdout.write(JSON.stringify({opened, ms: timers.length ? timers[0].ms : null, cleared}));
"""


def _long_press(scenario: str) -> dict:
    payload = {
        "sources": _sources(),
        "scenario": scenario,
        "handlerSource": _sources()["longPressHandlers"] + "\n return rowSuppressesMenu;",
    }
    return _run(_LONG_PRESS_DRIVER, payload)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_long_press_control_opens_the_menu():
    """Positive control: an undisturbed 500ms press opens the row menu."""
    result = _long_press("control")
    assert result["ms"] == 500
    assert result["opened"] == ["child"]


@pytest.mark.skipif(NODE is None, reason="node not available")
@pytest.mark.parametrize("scenario", ["detached", "switched", "profileSwitched", "readonly"])
def test_long_press_on_stale_row_opens_nothing(scenario):
    """A timer that outlives its row's ownership must not open a menu.

    Left unguarded, `detached`/`switched` is the path that let a hold in session A
    reach A's relative path against B's handlers — where Delete would delete B's
    file. `readonly` is the empty menu right-click already suppresses.
    """
    assert _long_press(scenario)["opened"] == [], f"{scenario}: stale long-press opened a menu"


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_tree_rerender_cancels_a_pending_long_press():
    """A re-render clears the gesture whose row it is about to detach."""
    result = _long_press("rerendered")
    assert result["cleared"] >= 1, "a re-render must clear the pending long-press timer"
    assert result["opened"] == []


# ── Menu fits the viewport; CSS keeps iOS from adding its own callout ────────

def test_menu_position_is_measured_not_guessed():
    """The old fixed 100px guess is gone; placement happens after the items exist."""
    body = UI[UI.index("function _showFileContextMenu("):UI.index("async function _setDirectoryAsWorkspace(")]
    assert "getBoundingClientRect()" in body, "menu must be measured before it is placed"
    assert "e.clientY+100>vh" not in body, "the fixed 100px height guess is still there"
    assert "maxHeight" in body and "overflowY" in body, "an over-tall menu must cap and scroll"
    assert body.index("document.body.appendChild(menu)") < body.index("menu.getBoundingClientRect()")


def test_file_rows_carry_the_touch_callout_suppression():
    """iOS shows its selection callout beside a long-press menu without this trio."""
    css = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    rule = css[css.index("\n  .file-item{"):]
    rule = rule[: rule.index("}")]
    assert "-webkit-touch-callout:none" in rule
    assert "user-select:none" in rule
    assert "-webkit-tap-highlight-color:transparent" in rule


def test_reduced_motion_disables_the_row_long_press_animation():
    css = (REPO / "static" / "style.css").read_text(encoding="utf-8")
    block = css[css.index("@media (prefers-reduced-motion:reduce)"):]
    block = block[: block.index("\n  }")]
    assert ".file-item.long-pressing" in block, "reduced-motion must cover the tree row state"
