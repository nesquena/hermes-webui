"""Behavioural tests for Phase 2 todo helpers — driven via real node.

Loads the actual function bodies from static/ui.js and static/panels.js
into a sandboxed node script and asserts behaviour against the live
implementation. Mirrors the pattern in test_renderer_js_behaviour.py:
when a test fails, you know the JS is broken — there is no Python
mirror that could disagree with the source.

Coverage:

  • _todosHash               — stability, change-detection, edge cases
  • _hydrateTodosFromSession — cold-load priority, INFLIGHT fallback,
                              null clear, render-cache reset
  • scheduleTodosRefresh     — RAF coalescing, panel-active guard,
                              non-RAF fallback (Node default)
  • todo_state listener      — full snapshot replace, session-id filter,
                              older-ts rejection, malformed payload
                              swallow, INFLIGHT mirror
  • _legacyTodosFromMessages — fallback when no signal received,
                              fast-path skip on non-todo payloads,
                              malformed JSON tolerance
  • loadTodos                — single source of truth, hash short-circuit,
                              repeated-empty short-circuit
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
PANELS_JS = REPO_ROOT / "static" / "panels.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
const fs = require('fs');
// node -e omits the usual scriptname slot, so argv[1] is the first
// user argument. We pass three paths in fixed order: ui.js, panels.js,
// messages.js.
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');
const panelsSrc = fs.readFileSync(process.argv[2], 'utf8');

// ── Stub global environment ────────────────────────────────────────────────
// We deliberately do NOT load all of ui.js — it pulls hundreds of DOM
// helpers, fetch wrappers, EventSource wiring, KaTeX, etc. that are
// irrelevant here. Instead we extract the functions under test by name
// and bring along just enough scaffolding to make them runnable.

// Minimal DOM stub — only what _todosPanelIsActive() and the renderer use.
let _panelActive = false;
let _panelInnerHTML = '';
function makePanel() {
  return {
    classList: {
      contains: (cls) => cls === 'active' && _panelActive,
    },
    set innerHTML(html) { _panelInnerHTML = html; },
    get innerHTML() { return _panelInnerHTML; },
  };
}
const _panelTodos = makePanel();
const _todoPanel = makePanel();
global.document = {
  getElementById: (id) => {
    if (id === 'panelTodos') return _panelTodos;
    if (id === 'todoPanel') return _todoPanel;
    return null;
  },
};
global.window = {};

// $('id') used by panels.js
global.$ = (id) => global.document.getElementById(id);

// esc(): match the real one closely enough for assertion purposes.
global.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// Stub i18n + icon helpers used by the renderer.
global.t = (k) => k;
global.li = (name, size) => `[${name}:${size||0}]`;

// State + INFLIGHT stubs.
global.S = {
  session: null,
  messages: [],
  todos: [],
  todoStateMeta: null,
};
global.INFLIGHT = {};

// requestAnimationFrame: by default off so scheduleTodosRefresh falls
// back to synchronous loadTodos(); tests can swap in a queueing impl.
let _rafQueue = [];
let _rafEnabled = false;
global.requestAnimationFrame = (cb) => {
  if (_rafEnabled) {
    _rafQueue.push(cb);
    return _rafQueue.length;
  }
  return undefined;
};
function flushRaf() {
  const q = _rafQueue;
  _rafQueue = [];
  for (const cb of q) cb();
}
function enableRaf() {
  _rafEnabled = true;
  global.requestAnimationFrame = (cb) => {
    _rafQueue.push(cb);
    return _rafQueue.length;
  };
}
function disableRaf() {
  _rafEnabled = false;
  global.requestAnimationFrame = undefined;
}

// ── Function extraction ────────────────────────────────────────────────────
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

// Hoist module-level lets (_todosLastRenderedHash, _todosRenderRafId)
// — extractFunc only pulls function bodies, but the helpers reference
// these. Define them globally so the extracted code resolves them.
let _todosLastRenderedHash = null;
let _todosRenderRafId = 0;

eval(extractFunc(uiSrc, '_todosHash'));
eval(extractFunc(uiSrc, '_todosPanelIsActive'));
eval(extractFunc(uiSrc, 'scheduleTodosRefresh'));
eval(extractFunc(uiSrc, '_resetTodosRenderCache'));
eval(extractFunc(uiSrc, '_hydrateTodosFromSession'));
eval(extractFunc(panelsSrc, 'loadTodos'));
eval(extractFunc(panelsSrc, '_legacyTodosFromMessages'));

// ── todo_state listener: extract the handler body manually ─────────────────
// It is registered as `source.addEventListener('todo_state', e=>{...});`,
// so we slice the arrow body and wrap it in a callable.
const messagesSrc = fs.readFileSync(process.argv[3], 'utf8');
function extractTodoStateHandler(src) {
  const start = src.indexOf("addEventListener('todo_state'");
  if (start < 0) throw new Error('todo_state listener not found');
  // Anchor on the arrow opening `e=>{` to find the body.
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

// activeSid + persistInflightState + scheduleTodosRefresh are closure
// vars in messages.js. Inject them as test-controlled globals.
//
// Note on S.session: production messages.js uses both `activeSid` (the
// SSE-side session id the stream is wired to) and `S.session.session_id`
// (the UI-side session the user is viewing) and only writes global state
// when both agree.  The driver mirrors that contract — `setActive(sid)`
// keeps both in sync — and individual tests that exercise the
// cross-session guard set them apart explicitly.
let activeSid = '';
let _persistCalls = 0;
function persistInflightState() { _persistCalls++; }
let _refreshCalls = 0;
const _origScheduleTodosRefresh = scheduleTodosRefresh;
function setActive(sid) {
  activeSid = sid;
  S.session = sid ? {session_id: sid} : null;
}
function _invokeTodoStateHandler(e) {
  // Wrap into a function so the body's `return` statements terminate
  // only the handler, not the whole driver. Each call gets fresh
  // locals for d / incomingTs / currentTs.
  const fn = new Function('e','S','INFLIGHT','activeSid','persistInflightState','scheduleTodosRefresh', _handlerBody);
  fn(e, S, INFLIGHT, activeSid, persistInflightState, () => { _refreshCalls++; _origScheduleTodosRefresh(); });
}

// ── Test runner ────────────────────────────────────────────────────────────
const results = [];
function it(name, fn) {
  try {
    fn();
    results.push({name, ok: true});
  } catch (e) {
    results.push({name, ok: false, err: String(e && e.stack || e)});
  }
}
function eq(a, b, msg) {
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A !== B) throw new Error((msg||'') + ' expected ' + B + ', got ' + A);
}
function truthy(v, msg) { if (!v) throw new Error(msg||'expected truthy'); }
function falsy(v, msg) { if (v) throw new Error(msg||'expected falsy'); }
function reset() {
  S.session = null; S.messages = []; S.todos = []; S.todoStateMeta = null;
  Object.keys(INFLIGHT).forEach(k => delete INFLIGHT[k]);
  _todosLastRenderedHash = null;
  _todosRenderRafId = 0;
  _rafQueue = [];
  _rafEnabled = false;
  global.requestAnimationFrame = undefined;
  _panelActive = false;
  _panelInnerHTML = '';
  activeSid = '';
  _persistCalls = 0;
  _refreshCalls = 0;
}

// ─── _todosHash ────────────────────────────────────────────────────────────
it('hash: empty list yields stable string', () => {
  reset();
  const h1 = _todosHash([]);
  const h2 = _todosHash([]);
  eq(h1, h2, 'stable across calls');
  truthy(h1.length > 0, 'non-empty result');
});

it('hash: id change is detected', () => {
  reset();
  const a = _todosHash([{id:'1', content:'x', status:'pending'}]);
  const b = _todosHash([{id:'2', content:'x', status:'pending'}]);
  truthy(a !== b, 'id change must change hash');
});

it('hash: content change is detected', () => {
  reset();
  const a = _todosHash([{id:'1', content:'foo', status:'pending'}]);
  const b = _todosHash([{id:'1', content:'bar', status:'pending'}]);
  truthy(a !== b, 'content change must change hash');
});

it('hash: status change is detected', () => {
  reset();
  const a = _todosHash([{id:'1', content:'x', status:'pending'}]);
  const b = _todosHash([{id:'1', content:'x', status:'in_progress'}]);
  truthy(a !== b, 'status change must change hash');
});

it('hash: list length change is detected', () => {
  reset();
  const a = _todosHash([{id:'1', content:'x', status:'pending'}]);
  const b = _todosHash([{id:'1', content:'x', status:'pending'},
                        {id:'2', content:'y', status:'pending'}]);
  truthy(a !== b, 'list length change must change hash');
});

it('hash: order change is detected', () => {
  reset();
  const a = _todosHash([{id:'1',content:'x',status:'pending'},
                        {id:'2',content:'y',status:'pending'}]);
  const b = _todosHash([{id:'2',content:'y',status:'pending'},
                        {id:'1',content:'x',status:'pending'}]);
  truthy(a !== b, 'reorder must change hash (priority is encoded as order)');
});

it('hash: missing fields are tolerated', () => {
  reset();
  const h = _todosHash([{}, {id:null, content:undefined, status:''}]);
  truthy(typeof h === 'string', 'must not throw on partial items');
});

it('hash: not-an-array yields empty string', () => {
  reset();
  eq(_todosHash(null), '');
  eq(_todosHash(undefined), '');
  eq(_todosHash({}), '');
});

// ─── _hydrateTodosFromSession ──────────────────────────────────────────────
it('hydrate: cold-load wins over inflight when cold ts is newer', () => {
  reset();
  INFLIGHT['s1'] = {
    todos: [{id:'old', content:'inflight', status:'pending'}],
    todoStateMeta: {ts: 100, source: 'tool', version: 1},
  };
  _hydrateTodosFromSession({
    session_id: 's1',
    todo_state: {todos: [{id:'new', content:'cold', status:'pending'}], version: 1, ts: 200},
  });
  eq(S.todos[0].id, 'new', 'newer cold-load must win');
  eq(S.todoStateMeta.source, 'cold-load');
});

it('hydrate: inflight wins over cold-load when inflight ts is newer', () => {
  reset();
  INFLIGHT['s1'] = {
    todos: [{id:'live', content:'live', status:'in_progress'}],
    todoStateMeta: {ts: 200, source: 'tool', version: 1},
  };
  _hydrateTodosFromSession({
    session_id: 's1',
    todo_state: {todos: [{id:'stale', content:'stale-cold', status:'pending'}], version: 1, ts: 100},
  });
  eq(S.todos[0].id, 'live',
     'a stale cold-load must NOT regress fresher INFLIGHT state on reload');
  eq(S.todoStateMeta.source, 'tool',
     'meta carries the inflight source, not the cold-load tag');
});

it('hydrate: tie ts prefers inflight (freshest in-tab edits)', () => {
  reset();
  INFLIGHT['s1'] = {
    todos: [{id:'inflight', content:'in', status:'pending'}],
    todoStateMeta: {ts: 100, source: 'tool', version: 1},
  };
  _hydrateTodosFromSession({
    session_id: 's1',
    todo_state: {todos: [{id:'cold', content:'co', status:'pending'}], version: 1, ts: 100},
  });
  eq(S.todos[0].id, 'inflight', 'tie goes to inflight');
});

it('hydrate: falls back to inflight when no cold-load', () => {
  reset();
  INFLIGHT['s1'] = {
    todos: [{id:'kept', content:'inflight', status:'pending'}],
    todoStateMeta: {ts: 1, source: 'tool', version: 1},
  };
  _hydrateTodosFromSession({session_id: 's1'});
  eq(S.todos[0].id, 'kept', 'inflight must be used when no cold-load');
});

it('hydrate: clears state when neither cold-load nor inflight', () => {
  reset();
  S.todos = [{id: '1', content: 'stale', status: 'pending'}];
  S.todoStateMeta = {ts: 0, source: 'cold-load', version: 1};
  _hydrateTodosFromSession({session_id: 's-fresh'});
  eq(S.todos, [], 'must reset to empty');
  eq(S.todoStateMeta, null, 'must reset meta to null sentinel');
});

it('hydrate: null session clears state (delete-session path)', () => {
  reset();
  S.todos = [{id:'1',content:'x',status:'pending'}];
  S.todoStateMeta = {ts: 1, source: 'tool', version: 1};
  _hydrateTodosFromSession(null);
  eq(S.todos, []);
  eq(S.todoStateMeta, null);
});

it('hydrate: resets render cache so next paint runs', () => {
  reset();
  _todosLastRenderedHash = 'stale-hash';
  _hydrateTodosFromSession({session_id: 's1'});
  eq(_todosLastRenderedHash, null,
     'cross-session navigation must invalidate the render hash');
});

it('hydrate: cold-load with empty todos yields explicit empty state', () => {
  reset();
  _hydrateTodosFromSession({
    session_id: 's1',
    todo_state: {todos: [], version: 1},
  });
  eq(S.todos, []);
  truthy(S.todoStateMeta !== null, 'empty cold-load is still an explicit signal');
});

// ─── scheduleTodosRefresh ──────────────────────────────────────────────────
it('schedule: with RAF coalesces multiple calls', () => {
  reset();
  enableRaf();
  _panelActive = true;
  scheduleTodosRefresh();
  scheduleTodosRefresh();
  scheduleTodosRefresh();
  // Only one RAF callback queued.
  eq(_rafQueue.length, 1, 'multiple schedule calls coalesce to one RAF tick');
});

it('schedule: skips render when panel inactive', () => {
  reset();
  enableRaf();
  _panelActive = false;
  S.todos = [{id:'1',content:'x',status:'pending'}];
  S.todoStateMeta = {ts:1,source:'tool',version:1};
  scheduleTodosRefresh();
  flushRaf();
  eq(_panelInnerHTML, '', 'inactive panel must not be touched');
});

it('schedule: paints when panel is active', () => {
  reset();
  enableRaf();
  _panelActive = true;
  S.todos = [{id:'1',content:'x',status:'pending'}];
  S.todoStateMeta = {ts:1,source:'tool',version:1};
  scheduleTodosRefresh();
  flushRaf();
  truthy(_panelInnerHTML.length > 0, 'active panel must render');
});

it('schedule: synchronous fallback when RAF missing', () => {
  reset();
  disableRaf();
  _panelActive = true;
  S.todos = [{id:'1',content:'x',status:'pending'}];
  S.todoStateMeta = {ts:1,source:'tool',version:1};
  scheduleTodosRefresh();
  truthy(_panelInnerHTML.length > 0, 'must render synchronously without RAF');
});

// ─── todo_state listener ───────────────────────────────────────────────────
function evt(payload) { return {data: JSON.stringify(payload)}; }

it('listener: full snapshot replaces (no merge)', () => {
  reset();
  setActive('s1');
  S.todos = [{id:'old', content:'old', status:'completed'}];
  S.todoStateMeta = {ts:1, source:'tool', version:1};
  _invokeTodoStateHandler(evt({
    session_id: 's1',
    todos: [{id:'new', content:'new', status:'pending'}],
    version: 1, ts: 2, source: 'tool',
  }));
  eq(S.todos.length, 1);
  eq(S.todos[0].id, 'new', 'snapshot replaces; never merges');
});

it('listener: drops events from a different session', () => {
  reset();
  setActive('s-active');
  S.todos = [{id:'keep', content:'keep', status:'pending'}];
  S.todoStateMeta = {ts:1, source:'tool', version:1};
  _invokeTodoStateHandler(evt({
    session_id: 's-other',
    todos: [{id:'leak', content:'leak', status:'pending'}],
    ts: 2,
  }));
  eq(S.todos[0].id, 'keep', 'cross-session events must not leak');
});

it('listener: drops events when S.session does not match activeSid', () => {
  // P1: payload.session_id may legitimately equal activeSid for an
  // event that was queued before the user navigated away.  By the time
  // the handler runs, S.session has already been swapped to a different
  // session, so writing global state would pollute the now-active view.
  reset();
  activeSid = 's1';
  S.session = {session_id: 's-other'};   // user already navigated
  S.todos = [{id:'keep', content:'keep', status:'pending'}];
  S.todoStateMeta = {ts:1, source:'tool', version:1};
  _invokeTodoStateHandler(evt({
    session_id: 's1',
    todos: [{id:'leak', content:'leak', status:'pending'}],
    ts: 2,
  }));
  eq(S.todos[0].id, 'keep',
     'late event must be dropped when UI has already moved on');
});

it('listener: drops events when no S.session is set yet', () => {
  // Page is mid-load: SSE wired up before the session GET resolved.
  // We cannot safely write S.todos for a session whose identity is
  // unknown; defer until S.session is set.
  reset();
  activeSid = 's1';
  S.session = null;
  S.todos = [];
  _invokeTodoStateHandler(evt({
    session_id: 's1',
    todos: [{id:'leak', content:'leak', status:'pending'}],
    ts: 2,
  }));
  eq(S.todos, [], 'must not write before session identity is settled');
});

it('listener: drops events with strictly older ts', () => {
  reset();
  setActive('s1');
  S.todoStateMeta = {ts: 100, source: 'tool', version: 1};
  S.todos = [{id:'newer', content:'newer', status:'pending'}];
  _invokeTodoStateHandler(evt({
    session_id: 's1', ts: 50,
    todos: [{id:'older', content:'older', status:'pending'}],
  }));
  eq(S.todos[0].id, 'newer', 'older event must not overwrite newer state');
});

it('listener: equal ts is allowed (compression after tool)', () => {
  reset();
  setActive('s1');
  S.todoStateMeta = {ts: 100, source: 'tool', version: 1};
  S.todos = [{id:'a',content:'a',status:'pending'}];
  _invokeTodoStateHandler(evt({
    session_id: 's1', ts: 100, source: 'compression',
    todos: [{id:'b',content:'b',status:'pending'}],
  }));
  eq(S.todos[0].id, 'b', 'equal-ts events apply (e.g. compression refresh)');
});

it('listener: malformed JSON is swallowed', () => {
  reset();
  setActive('s1');
  S.todos = [{id:'kept', content:'k', status:'pending'}];
  S.todoStateMeta = {ts:1,source:'tool',version:1};
  _invokeTodoStateHandler({data: '{not json'});
  eq(S.todos[0].id, 'kept', 'malformed payload must not corrupt state');
});

it('listener: non-array todos rejected', () => {
  reset();
  setActive('s1');
  S.todos = [{id:'kept', content:'k', status:'pending'}];
  S.todoStateMeta = {ts:1,source:'tool',version:1};
  _invokeTodoStateHandler(evt({session_id:'s1', todos: 'not-an-array', ts: 2}));
  eq(S.todos[0].id, 'kept');
});

it('listener: mirrors snapshot to INFLIGHT', () => {
  reset();
  setActive('s1');
  INFLIGHT['s1'] = {todos: null, todoStateMeta: null};
  _invokeTodoStateHandler(evt({
    session_id: 's1', ts: 2,
    todos: [{id:'x', content:'x', status:'pending'}],
  }));
  truthy(Array.isArray(INFLIGHT['s1'].todos), 'INFLIGHT.todos must mirror S.todos');
  eq(INFLIGHT['s1'].todos[0].id, 'x');
  truthy(INFLIGHT['s1'].todoStateMeta !== null, 'INFLIGHT meta must mirror');
});

it('listener: triggers persistence + render schedule', () => {
  reset();
  setActive('s1');
  INFLIGHT['s1'] = {todos: null, todoStateMeta: null};
  _invokeTodoStateHandler(evt({
    session_id: 's1', ts: 2,
    todos: [{id:'x', content:'x', status:'pending'}],
  }));
  truthy(_persistCalls >= 1, 'persistInflightState called');
  truthy(_refreshCalls >= 1, 'scheduleTodosRefresh called');
});

it('listener: empty session_id does not block valid event', () => {
  reset();
  setActive('s1');
  // Older servers might not tag session_id; we only filter when present.
  _invokeTodoStateHandler(evt({
    todos: [{id:'x', content:'x', status:'pending'}],
    ts: 2,
  }));
  eq(S.todos[0].id, 'x', 'untagged event must still apply');
});

// ─── _legacyTodosFromMessages ──────────────────────────────────────────────
it('legacy: returns empty when no signal in messages', () => {
  reset();
  S.messages = [
    {role: 'user', content: 'hi'},
    {role: 'assistant', content: 'hello'},
  ];
  eq(_legacyTodosFromMessages(), []);
});

it('legacy: scans S.messages when no S.session.messages', () => {
  reset();
  S.messages = [
    {role: 'tool', content: JSON.stringify({todos: [{id:'1', content:'a', status:'pending'}]})},
  ];
  eq(_legacyTodosFromMessages()[0].id, '1');
});

it('legacy: prefers S.session.messages when present', () => {
  reset();
  S.messages = [{role:'tool', content: JSON.stringify({todos:[{id:'fallback'}]})}];
  S.session = {messages: [{role:'tool', content: JSON.stringify({todos:[{id:'preferred'}]})}]};
  eq(_legacyTodosFromMessages()[0].id, 'preferred');
});

it('legacy: returns latest todo entry when multiple exist', () => {
  reset();
  S.messages = [
    {role:'tool', content: JSON.stringify({todos:[{id:'old', status:'completed'}]})},
    {role:'assistant', content:'thinking'},
    {role:'tool', content: JSON.stringify({todos:[{id:'new', status:'pending'}]})},
  ];
  eq(_legacyTodosFromMessages()[0].id, 'new');
});

it('legacy: skips non-todo tool payloads via fast-path', () => {
  reset();
  S.messages = [
    {role:'tool', content: JSON.stringify({todos:[{id:'1'}]})},
    {role:'tool', content: JSON.stringify({result: 'ok'})},
    {role:'tool', content: JSON.stringify({output: 'hello'})},
  ];
  eq(_legacyTodosFromMessages()[0].id, '1', 'fast-path skips non-todo payloads');
});

it('legacy: tolerates malformed JSON', () => {
  reset();
  S.messages = [
    {role:'tool', content: JSON.stringify({todos:[{id:'good'}]})},
    {role:'tool', content: '{"todos": broken'},
  ];
  eq(_legacyTodosFromMessages()[0].id, 'good');
});

it('legacy: handles non-string content gracefully', () => {
  reset();
  S.messages = [
    {role:'tool', content: null},
    {role:'tool', content: ['chunked']},
    {role:'tool', content: {todos: [{id:'obj'}]}},  // already-parsed dict
  ];
  // Object content is JSON.stringified inside the helper; the substring
  // guard sees `"todos"` and the parse succeeds.
  eq(_legacyTodosFromMessages()[0].id, 'obj');
});

// ─── loadTodos integration ─────────────────────────────────────────────────
it('loadTodos: prefers S.todos when meta present', () => {
  reset();
  _panelActive = true;
  S.messages = [
    // legacy fallback would pick this up, but we must NOT use it
    {role:'tool', content: JSON.stringify({todos:[{id:'legacy'}]})},
  ];
  S.todos = [{id:'live', content:'live', status:'pending'}];
  S.todoStateMeta = {ts: 1, source: 'tool', version: 1};
  loadTodos();
  truthy(_panelInnerHTML.indexOf('live') >= 0,
         'live snapshot must win over legacy when meta is present');
  truthy(_panelInnerHTML.indexOf('legacy') < 0);
});

it('loadTodos: falls through to legacy when meta null', () => {
  reset();
  _panelActive = true;
  S.messages = [
    {role:'tool', content: JSON.stringify({todos:[{id:'fallback', content:'from-legacy', status:'pending'}]})},
  ];
  S.todos = [];
  S.todoStateMeta = null;
  loadTodos();
  truthy(_panelInnerHTML.indexOf('from-legacy') >= 0,
         'must fall through to legacy reverse-scan');
});

it('loadTodos: hash short-circuits identical re-render', () => {
  reset();
  _panelActive = true;
  S.todos = [{id:'1', content:'x', status:'pending'}];
  S.todoStateMeta = {ts:1, source:'tool', version:1};
  loadTodos();
  const first = _panelInnerHTML;
  // Mutate the panel from outside; second loadTodos with same hash
  // must NOT touch innerHTML.
  _panelInnerHTML = '__user_modified__';
  loadTodos();
  eq(_panelInnerHTML, '__user_modified__',
     'identical hash must short-circuit and skip the innerHTML write');
});

it('loadTodos: empty state short-circuits on repeat', () => {
  reset();
  _panelActive = true;
  S.todos = [];
  S.todoStateMeta = {ts:1, source:'tool', version:1};
  loadTodos();
  const first = _panelInnerHTML;
  truthy(first.length > 0, 'first paint sets the empty state');
  _panelInnerHTML = '__user_modified__';
  loadTodos();
  eq(_panelInnerHTML, '__user_modified__',
     'repeated empty must short-circuit');
});

it('loadTodos: hash invalidation forces repaint', () => {
  reset();
  _panelActive = true;
  S.todos = [{id:'1', content:'x', status:'pending'}];
  S.todoStateMeta = {ts:1, source:'tool', version:1};
  loadTodos();
  // Status transition → fresh hash → must repaint.
  S.todos = [{id:'1', content:'x', status:'in_progress'}];
  loadTodos();
  truthy(_panelInnerHTML.indexOf('in_progress') >= 0,
         'status transition must produce a new render');
});

it('loadTodos: escapes user-controlled strings', () => {
  reset();
  _panelActive = true;
  S.todos = [{id:'1', content:'<script>alert(1)</script>', status:'pending'}];
  S.todoStateMeta = {ts:1, source:'tool', version:1};
  loadTodos();
  truthy(_panelInnerHTML.indexOf('<script>') < 0, 'must escape');
  truthy(_panelInnerHTML.indexOf('&lt;script&gt;') >= 0, 'must HTML-escape');
});

// ── Final report ──────────────────────────────────────────────────────────
console.log(JSON.stringify(results));
"""


def _run_node():
    # NODE is guaranteed non-None here by the module-level pytestmark
    # skipif; the assert narrows the type for static analyzers too.
    assert NODE is not None
    proc = subprocess.run(
        [
            NODE,
            "-e",
            _DRIVER,
            str(UI_JS),
            str(PANELS_JS),
            str(REPO_ROOT / "static" / "messages.js"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node driver failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
        )
    # The driver prints exactly one JSON line at the end.
    last_line = proc.stdout.strip().splitlines()[-1]
    return json.loads(last_line)


@pytest.fixture(scope="module")
def driver_results():
    return _run_node()


# Expand each individual JS test into its own pytest case so the
# failure surface is granular.

def _id(name):
    """Sanitize a JS test name into a pytest-friendly id."""
    return name.replace(":", "_").replace(" ", "_")


# Run once to discover the test names; pytest parametrize requires the
# id list at collection time.
_DISCOVERED_NAMES = [
    "hash: empty list yields stable string",
    "hash: id change is detected",
    "hash: content change is detected",
    "hash: status change is detected",
    "hash: list length change is detected",
    "hash: order change is detected",
    "hash: missing fields are tolerated",
    "hash: not-an-array yields empty string",
    "hydrate: cold-load wins over inflight when cold ts is newer",
    "hydrate: inflight wins over cold-load when inflight ts is newer",
    "hydrate: tie ts prefers inflight (freshest in-tab edits)",
    "hydrate: falls back to inflight when no cold-load",
    "hydrate: clears state when neither cold-load nor inflight",
    "hydrate: null session clears state (delete-session path)",
    "hydrate: resets render cache so next paint runs",
    "hydrate: cold-load with empty todos yields explicit empty state",
    "schedule: with RAF coalesces multiple calls",
    "schedule: skips render when panel inactive",
    "schedule: paints when panel is active",
    "schedule: synchronous fallback when RAF missing",
    "listener: full snapshot replaces (no merge)",
    "listener: drops events from a different session",
    "listener: drops events when S.session does not match activeSid",
    "listener: drops events when no S.session is set yet",
    "listener: drops events with strictly older ts",
    "listener: equal ts is allowed (compression after tool)",
    "listener: malformed JSON is swallowed",
    "listener: non-array todos rejected",
    "listener: mirrors snapshot to INFLIGHT",
    "listener: triggers persistence + render schedule",
    "listener: empty session_id does not block valid event",
    "legacy: returns empty when no signal in messages",
    "legacy: scans S.messages when no S.session.messages",
    "legacy: prefers S.session.messages when present",
    "legacy: returns latest todo entry when multiple exist",
    "legacy: skips non-todo tool payloads via fast-path",
    "legacy: tolerates malformed JSON",
    "legacy: handles non-string content gracefully",
    "loadTodos: prefers S.todos when meta present",
    "loadTodos: falls through to legacy when meta null",
    "loadTodos: hash short-circuits identical re-render",
    "loadTodos: empty state short-circuits on repeat",
    "loadTodos: hash invalidation forces repaint",
    "loadTodos: escapes user-controlled strings",
]


@pytest.mark.parametrize("test_name", _DISCOVERED_NAMES, ids=_id)
def test_js_behaviour(driver_results, test_name):
    matches = [r for r in driver_results if r["name"] == test_name]
    assert matches, f"JS test '{test_name}' was not present in driver output"
    r = matches[0]
    assert r["ok"], f"JS test '{test_name}' failed:\n{r.get('err','(no detail)')}"


def test_all_declared_tests_ran(driver_results):
    """Guard against driver-side bugs that silently skip tests."""
    actual = {r["name"] for r in driver_results}
    declared = set(_DISCOVERED_NAMES)
    missing = declared - actual
    extra = actual - declared
    assert not missing and not extra, (
        f"Driver/declaration mismatch:\n"
        f"  missing from driver output: {sorted(missing)}\n"
        f"  not in DISCOVERED_NAMES:    {sorted(extra)}"
    )
