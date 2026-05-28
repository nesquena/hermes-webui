"""Round-trip + recovery tests for the INFLIGHT localStorage persistence path.

P2 follow-up to the earlier review:

* the previous `phase2_e2e_scenarios` driver stubbed ``persistInflightState``
  as a counter, leaving the real ``saveInflightState → localStorage →
  loadInflightState`` path entirely untested;
* it also did not cover the SSE reconnect path: a stream that drops
  mid-todo-update and then reattaches via journal replay must restore
  the latest snapshot from the persisted INFLIGHT bucket, otherwise
  the user sees a flicker / stale list until the next live emit.

This file loads the actual ``_compactInflightState``, ``saveInflightState``,
``loadInflightState``, ``clearInflightState`` from ``static/ui.js``
into a Node sandbox with a tiny in-memory localStorage shim, and runs
the round-trip + reconnect scenarios end-to-end against that wiring.

Each driver test asserts on observable state (the deserialized snapshot,
the panel HTML after re-hydration), not on internals or formatting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
PANELS_JS = REPO_ROOT / "static" / "panels.js"
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');
const panelsSrc = fs.readFileSync(process.argv[2], 'utf8');
const messagesSrc = fs.readFileSync(process.argv[3], 'utf8');

// ── In-memory localStorage shim ────────────────────────────────────────────
class FakeStorage {
  constructor(){ this._m = {}; this.quotaBytes = Infinity; }
  getItem(k){ return Object.prototype.hasOwnProperty.call(this._m,k)?this._m[k]:null; }
  setItem(k,v){
    const next = String(v);
    const used = Object.values(this._m).reduce((a,b)=>a+String(b).length,0);
    const old = this._m[k] ? String(this._m[k]).length : 0;
    if (used - old + next.length > this.quotaBytes) {
      const err = new Error('quota');
      err.name = 'QuotaExceededError';
      throw err;
    }
    this._m[k] = next;
  }
  removeItem(k){ delete this._m[k]; }
}
let storage = new FakeStorage();
global.localStorage = storage;

// ── Stub everything ui.js's INFLIGHT helpers actually use ──────────────────
global.window = {};
global.document = {
  getElementById: () => ({
    classList: {contains: () => false},
    set innerHTML(_) {},
    get innerHTML() { return ''; },
  }),
};
global.$ = (id) => global.document.getElementById(id);
global.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
global.t = (k) => k;
global.li = (name, size) => '[' + name + ':' + (size||0) + ']';

// State + INFLIGHT.
global.S = {session:null, messages:[], todos:[], todoStateMeta:null};
global.INFLIGHT = {};

// ── Function extraction: a pragmatic compromise.  We need access to
//     the real INFLIGHT_STATE_KEY constant *and* a handful of helpers,
//     so for these tests we eval the relevant span of ui.js verbatim.
//     The span is anchored on the constant declaration and the
//     clearInflightState definition, both of which are stable names.
function spanInclusive(src, fromMarker, toMarker) {
  const start = src.indexOf(fromMarker);
  if (start < 0) throw new Error('marker not found: ' + fromMarker);
  const tailStart = src.indexOf(toMarker, start);
  if (tailStart < 0) throw new Error('marker not found: ' + toMarker);
  // include the entire `function clearInflightState` body
  let i = src.indexOf('{', tailStart) + 1;
  let depth = 1;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
const persistenceSpan = spanInclusive(
  uiSrc,
  "const INFLIGHT_STATE_KEY",
  "function clearInflightState"
);
eval(persistenceSpan);

// Now extract by name for the rest of the helpers under test.
function extractFunc(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
let _todosLastRenderedHash = null;
let _todosRenderRafId = 0;
let _panelActive = false;
let _panelInnerHTML = '';
function makePanel() {
  return {
    classList: {contains: (cls) => cls === 'active' && _panelActive},
    set innerHTML(html) { _panelInnerHTML = html; },
    get innerHTML() { return _panelInnerHTML; },
  };
}
const _panelTodos = makePanel();
const _todoPanel = makePanel();
global.document.getElementById = (id) => {
  if (id === 'panelTodos') return _panelTodos;
  if (id === 'todoPanel') return _todoPanel;
  return null;
};
global.requestAnimationFrame = undefined;  // synchronous fallback

eval(extractFunc(uiSrc, '_todosHash'));
eval(extractFunc(uiSrc, '_todosPanelIsActive'));
eval(extractFunc(uiSrc, 'scheduleTodosRefresh'));
eval(extractFunc(uiSrc, '_resetTodosRenderCache'));
eval(extractFunc(uiSrc, '_hydrateTodosFromSession'));
eval(extractFunc(panelsSrc, 'loadTodos'));
eval(extractFunc(panelsSrc, '_legacyTodosFromMessages'));

// ── todo_state SSE listener ────────────────────────────────────────────────
function extractTodoStateHandler(src) {
  const start = src.indexOf("addEventListener('todo_state'");
  if (start < 0) throw new Error('todo_state listener not found');
  const bodyStart = src.indexOf('{', src.indexOf('=>', start));
  let depth = 1; let i = bodyStart + 1;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(bodyStart + 1, i - 1);
}
const _handlerBody = extractTodoStateHandler(messagesSrc);
let activeSid = '';
function setActive(sid) {
  activeSid = sid;
  S.session = sid ? {session_id: sid} : null;
}
function _persistInflightState() {
  // Mirror what messages.js's persistInflightState does in production:
  // pull the current INFLIGHT[activeSid] snapshot and route through the
  // real saveInflightState() so we exercise the real localStorage path.
  if (activeSid && INFLIGHT[activeSid]) {
    saveInflightState(activeSid, INFLIGHT[activeSid]);
  }
}
function _invokeTodoStateHandler(e) {
  const fn = new Function(
    'e','S','INFLIGHT','activeSid','persistInflightState','scheduleTodosRefresh',
    _handlerBody);
  fn(e, S, INFLIGHT, activeSid, _persistInflightState,
     () => { /* sync render is handled below */ });
}

// ── Test runner ────────────────────────────────────────────────────────────
const results = [];
function it(name, fn) {
  try { fn(); results.push({name, ok: true}); }
  catch (e) { results.push({name, ok: false, err: String(e && e.stack || e)}); }
}
function eq(a, b, msg) {
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A !== B) throw new Error((msg||'') + ' expected ' + B + ', got ' + A);
}
function truthy(v, msg) { if (!v) throw new Error(msg||'expected truthy'); }
function reset() {
  S.session = null; S.messages = []; S.todos = []; S.todoStateMeta = null;
  Object.keys(INFLIGHT).forEach(k => delete INFLIGHT[k]);
  _todosLastRenderedHash = null;
  _todosRenderRafId = 0;
  _panelActive = false;
  _panelInnerHTML = '';
  activeSid = '';
  storage = new FakeStorage();
  global.localStorage = storage;
}

// ─── Persistence round-trip ────────────────────────────────────────────────

it('round-trip: live emit → persist → load gives identical snapshot', () => {
  reset();
  setActive('s-A');
  INFLIGHT['s-A'] = {todos:null, todoStateMeta:null,
    messages:[], toolCalls:[], uploaded:[], streamId:'r1'};
  _invokeTodoStateHandler({data: JSON.stringify({
    session_id: 's-A',
    ts: 1234.5,
    source: 'tool',
    version: 1,
    todos: [{id:'1', content:'plan', status:'in_progress'},
            {id:'2', content:'next', status:'pending'}],
    summary: {total: 2, in_progress: 1, pending: 1},
  })});

  // localStorage now holds the real persisted blob.
  const restored = loadInflightState('s-A');
  truthy(restored, 'loadInflightState must return the persisted entry');
  eq(restored.todos.length, 2, 'todos array round-tripped');
  eq(restored.todos[0].id, '1');
  eq(restored.todos[1].status, 'pending');
  truthy(restored.todoStateMeta, 'todoStateMeta survived round-trip');
  eq(restored.todoStateMeta.ts, 1234.5);
  eq(restored.todoStateMeta.source, 'tool');
});

it('round-trip: missing todoStateMeta is restored as null sentinel', () => {
  reset();
  saveInflightState('s-empty', {
    streamId: 'r1', messages: [], toolCalls: [], uploaded: [],
    todos: null, todoStateMeta: null,
  });
  const restored = loadInflightState('s-empty');
  truthy(restored, 'entry persisted even without todoStateMeta');
  eq(restored.todos, null, 'null todos preserved (sentinel for "never seen")');
  eq(restored.todoStateMeta, null);
});

it('round-trip: streamId mismatch returns null (cross-stream guard)', () => {
  reset();
  saveInflightState('s-A', {
    streamId: 'r1', messages: [], toolCalls: [], uploaded: [],
    todos: [{id:'1', content:'x', status:'pending'}],
    todoStateMeta: {ts:1, source:'tool', version:1},
  });
  const same = loadInflightState('s-A', 'r1');
  truthy(same && Array.isArray(same.todos));
  const cross = loadInflightState('s-A', 'r-OTHER');
  eq(cross, null,
     'cross-stream load must return null so a new stream cannot inherit '
     + 'a stale snapshot from a different run.');
});

it('round-trip: clear removes the entry', () => {
  reset();
  saveInflightState('s-bye', {
    streamId: 'r', messages: [], toolCalls: [], uploaded: [],
    todos: [{id:'1', content:'x', status:'pending'}],
    todoStateMeta: {ts:1, source:'tool', version:1},
  });
  truthy(loadInflightState('s-bye'));
  clearInflightState('s-bye');
  eq(loadInflightState('s-bye'), null);
});

it('round-trip: corrupted JSON in storage falls back gracefully', () => {
  reset();
  // Simulate a partial write or storage corruption.
  storage._m['hermes-webui-inflight-state'] = '{not json';
  // Must not throw — the helper must swallow the parse error.
  const out = loadInflightState('s-anything');
  eq(out, null, 'corrupted storage must not crash callers');
  // And a subsequent save must succeed (the wrapper recovers).
  saveInflightState('s-fresh', {
    streamId: 'r', messages: [], toolCalls: [], uploaded: [],
    todos: [{id:'1', content:'x', status:'pending'}],
    todoStateMeta: {ts:1, source:'tool', version:1},
  });
  truthy(loadInflightState('s-fresh'),
         'save must rebuild storage after corruption');
});

// ─── SSE reconnect / mid-stream resume ─────────────────────────────────────

it('reconnect: hard reload after persisted live emit re-hydrates UI', () => {
  reset();
  // Simulate live stream:
  setActive('s-live');
  INFLIGHT['s-live'] = {todos:null, todoStateMeta:null,
    messages:[], toolCalls:[], uploaded:[], streamId:'r-live'};
  _invokeTodoStateHandler({data: JSON.stringify({
    session_id: 's-live', ts: 100, source: 'tool',
    todos: [{id:'1', content:'mid-flight', status:'in_progress'}],
  })});

  // Simulate hard reload: drop in-memory state, keep localStorage.
  S.session = null; S.todos = []; S.todoStateMeta = null;
  Object.keys(INFLIGHT).forEach(k => delete INFLIGHT[k]);

  // Mirror what sessions.js does on restore: pull the persisted entry
  // back into INFLIGHT[sid] and call _hydrateTodosFromSession.
  const restored = loadInflightState('s-live');
  truthy(restored, 'persisted snapshot must survive the simulated reload');
  INFLIGHT['s-live'] = {
    streamId: restored.streamId,
    messages: restored.messages || [],
    toolCalls: restored.toolCalls || [],
    uploaded: restored.uploaded || [],
    todos: restored.todos,
    todoStateMeta: restored.todoStateMeta,
  };
  setActive('s-live');
  _hydrateTodosFromSession(S.session);

  eq(S.todos[0].id, '1', 'reload must restore live snapshot from INFLIGHT');
  eq(S.todoStateMeta.source, 'tool',
     'meta must restore so loadTodos uses the live path, not legacy fallback');

  _panelActive = true;
  loadTodos();
  truthy(_panelInnerHTML.indexOf('mid-flight') >= 0,
         'panel must paint the restored snapshot after reload');
});

it('reconnect: replay of older todo_state events does not regress fresher snapshot', () => {
  // Production SSE replay can deliver historical todo_state events
  // again on reconnect.  The strict-older-ts guard inside the listener
  // must reject them so the latest persisted snapshot wins.
  reset();
  setActive('s-replay');
  INFLIGHT['s-replay'] = {todos:null, todoStateMeta:null,
    messages:[], toolCalls:[], uploaded:[], streamId:'r-replay'};
  // Live: reaches ts=200 with the real "completed" state.
  _invokeTodoStateHandler({data: JSON.stringify({
    session_id: 's-replay', ts: 200,
    todos: [{id:'1', content:'done', status:'completed'}],
  })});
  eq(S.todos[0].status, 'completed');

  // Replay: the journal replays an older ts=100 "pending" event.
  _invokeTodoStateHandler({data: JSON.stringify({
    session_id: 's-replay', ts: 100,
    todos: [{id:'1', content:'pending', status:'pending'}],
  })});
  eq(S.todos[0].status, 'completed',
     'older replay must not regress fresher state');

  // Persisted snapshot must also still hold the fresher state.
  const restored = loadInflightState('s-replay');
  eq(restored.todos[0].status, 'completed',
     'INFLIGHT must mirror the fresher state through the replay');
});

it('reconnect: cross-session replay during reload picks the right session', () => {
  // Two sessions persisted concurrently; reload must restore each
  // independently without bleed-through.
  reset();
  saveInflightState('s-A', {
    streamId: 'rA', messages: [], toolCalls: [], uploaded: [],
    todos: [{id:'A', content:'task-A', status:'pending'}],
    todoStateMeta: {ts:1, source:'tool', version:1},
  });
  saveInflightState('s-B', {
    streamId: 'rB', messages: [], toolCalls: [], uploaded: [],
    todos: [{id:'B', content:'task-B', status:'in_progress'}],
    todoStateMeta: {ts:2, source:'tool', version:1},
  });
  const A = loadInflightState('s-A');
  const B = loadInflightState('s-B');
  eq(A.todos[0].id, 'A');
  eq(B.todos[0].id, 'B');
  truthy(A.todos[0] !== B.todos[0],
         'sessions must not share array references after deserialize');
});

console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def driver_results():
    out = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS), str(PANELS_JS), str(MESSAGES_JS)],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"node driver failed (rc={out.returncode}):\n"
            f"STDOUT:\n{out.stdout}\n"
            f"STDERR:\n{out.stderr}"
        )
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError(
            f"could not parse driver output as JSON: {exc}\n"
            f"raw stdout:\n{out.stdout}"
        )


_DECLARED = [
    "round-trip: live emit → persist → load gives identical snapshot",
    "round-trip: missing todoStateMeta is restored as null sentinel",
    "round-trip: streamId mismatch returns null (cross-stream guard)",
    "round-trip: clear removes the entry",
    "round-trip: corrupted JSON in storage falls back gracefully",
    "reconnect: hard reload after persisted live emit re-hydrates UI",
    "reconnect: replay of older todo_state events does not regress fresher snapshot",
    "reconnect: cross-session replay during reload picks the right session",
]


def _id(name):
    return name.replace(" ", "_").replace(":", "").replace("/", "")


@pytest.mark.parametrize("name", _DECLARED, ids=_id)
def test_inflight_persistence(driver_results, name):
    matches = [r for r in driver_results if r["name"] == name]
    assert matches, f"{name!r} missing from driver output"
    r = matches[0]
    assert r["ok"], f"{name}\n  {r.get('err','(no detail)')}"


def test_all_declared_tests_ran(driver_results):
    actual = {r["name"] for r in driver_results}
    declared = set(_DECLARED)
    missing = declared - actual
    extra = actual - declared
    assert not missing and not extra, (
        f"Driver/declaration mismatch:\n"
        f"  missing from driver output: {sorted(missing)}\n"
        f"  not in _DECLARED:           {sorted(extra)}"
    )
