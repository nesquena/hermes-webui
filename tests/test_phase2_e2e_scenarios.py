"""End-to-end scenario coverage for the Phase 2 todo state pipeline.

Each scenario drives the **real** Phase 2 JavaScript helpers extracted
from static/ui.js, static/panels.js, and static/messages.js — there is
no Python mirror that could disagree with the source. The driver
provides a small high-level API (mount/emit/switch/snapshot) so each
scenario reads as a real user flow rather than a mock-heavy unit test.

Scenarios are grouped by category:

  • basic_lifecycle       — first write, status transitions, clearing
  • multi_session         — switching, deletion, cross-session events
  • event_robustness      — out-of-order, malformed, replay, RAF coalescing
  • user_content          — XSS, unicode, long content
  • render_scheduling     — hidden panel, re-show, performance sanity
  • compat_fallback       — legacy reverse-scan against pre-Phase-1 servers
  • realistic_workflows   — full multi-step plan-then-execute flows
  • persistence_recovery  — INFLIGHT priority, cold-load priority, reload

When a scenario fails the failure surface is granular: each scenario is
its own pytest case with the JS-side assertion text in the failure
message.
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
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ─────────────────────────────────────────────────────────────────────────────
# JavaScript driver
# ─────────────────────────────────────────────────────────────────────────────
#
# The driver loads three real source files, extracts the Phase 2 helpers
# we care about, and exposes a high-level scenario API. Each scenario is
# isolated by `reset()` so leakage between tests is impossible.

_DRIVER = r"""
const fs = require('fs');
const uiSrc      = fs.readFileSync(process.argv[1], 'utf8');
const panelsSrc  = fs.readFileSync(process.argv[2], 'utf8');
const messagesSrc= fs.readFileSync(process.argv[3], 'utf8');

// ── DOM + global scaffolding ───────────────────────────────────────────────
let _panelActive = false;
let _panelInnerHTML = '';
function makePanel() {
  return {
    classList: {
      contains: (cls) => cls === 'active' && _panelActive,
      add: () => {}, remove: () => {}, toggle: () => {},
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
global.$ = (id) => global.document.getElementById(id);
global.esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
global.t = (k) => k;
global.li = (name, size) => `[${name}:${size||0}]`;

global.S = { session:null, messages:[], todos:[], todoStateMeta:null };
global.INFLIGHT = {};

// localStorage stub for persistence-recovery scenarios.
const _ls = new Map();
global.localStorage = {
  getItem: (k) => _ls.has(k) ? _ls.get(k) : null,
  setItem: (k, v) => _ls.set(k, String(v)),
  removeItem: (k) => _ls.delete(k),
  clear: () => _ls.clear(),
};

// requestAnimationFrame: queue mode by default so scenarios can flush on
// demand. We treat absent RAF as a special path tested elsewhere.
let _rafQueue = [];
global.requestAnimationFrame = (cb) => { _rafQueue.push(cb); return _rafQueue.length; };
function flushRaf() {
  // Run until quiescent: a RAF callback may schedule another (we don't
  // expect it for todos, but defend against it).
  let safety = 16;
  while (_rafQueue.length && safety-- > 0) {
    const q = _rafQueue;
    _rafQueue = [];
    for (const cb of q) cb();
  }
}

// Hoist module-level state for the extracted helpers.
let _todosLastRenderedHash = null;
let _todosRenderRafId = 0;

// ── Function extraction (same shape as test_phase2_todo_behavior.py) ──────
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

eval(extractFunc(uiSrc,     '_todosHash'));
eval(extractFunc(uiSrc,     '_todosPanelIsActive'));
eval(extractFunc(uiSrc,     'scheduleTodosRefresh'));
eval(extractFunc(uiSrc,     '_resetTodosRenderCache'));
eval(extractFunc(uiSrc,     '_hydrateTodosFromSession'));
eval(extractFunc(panelsSrc, 'loadTodos'));
eval(extractFunc(panelsSrc, '_legacyTodosFromMessages'));

// todo_state listener body — extract the arrow body literally.
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

// activeSid + persistInflightState are closures in messages.js — inject
// them as named parameters so each scenario controls them explicitly.
let activeSid = '';
let _persistCalls = 0;
function persistInflightState() { _persistCalls++; }
function emitTodoStateRaw(eventLike) {
  const fn = new Function('e','S','INFLIGHT','activeSid','persistInflightState','scheduleTodosRefresh',
                          _handlerBody);
  fn(eventLike, S, INFLIGHT, activeSid, persistInflightState, scheduleTodosRefresh);
}

// ── High-level scenario API ────────────────────────────────────────────────
function reset() {
  S.session = null;
  S.messages = [];
  S.todos = [];
  S.todoStateMeta = null;
  Object.keys(INFLIGHT).forEach(k => delete INFLIGHT[k]);
  _todosLastRenderedHash = null;
  _todosRenderRafId = 0;
  _rafQueue = [];
  _panelActive = false;
  _panelInnerHTML = '';
  activeSid = '';
  _persistCalls = 0;
  _ls.clear();
}

// Mount a session: applies cold-load + INFLIGHT priority via the same
// _hydrateTodosFromSession helper that runs in production.  Always
// pre-seeds an INFLIGHT entry for the active session so the live
// listener can mirror snapshots there — mirrors what sendMessage does
// in production when it primes INFLIGHT before the first SSE event.
function mount(opts) {
  opts = opts || {};
  const sessionId = opts.sessionId || 's-default';
  if (!INFLIGHT[sessionId]) {
    INFLIGHT[sessionId] = {
      todos: opts.inflight ? (opts.inflight.todos || null) : null,
      todoStateMeta: opts.inflight ? (opts.inflight.todoStateMeta || null) : null,
      toolCalls: [], messages: [], uploaded: [],
    };
  } else if (opts.inflight) {
    if (opts.inflight.todos) INFLIGHT[sessionId].todos = opts.inflight.todos;
    if (opts.inflight.todoStateMeta) INFLIGHT[sessionId].todoStateMeta = opts.inflight.todoStateMeta;
  }
  S.session = {
    session_id: sessionId,
    messages: opts.sessionMessages || null,
  };
  S.messages = opts.messages || [];
  if (opts.todoState) S.session.todo_state = opts.todoState;
  activeSid = sessionId;
  _panelActive = opts.panelActive !== false;
  _hydrateTodosFromSession(S.session);
  if (_panelActive) loadTodos();  // initial paint
}

// Emit a todo_state SSE event for the active session.
function emit(payload) {
  emitTodoStateRaw({ data: JSON.stringify(payload) });
  // Render scheduling normally awaits a frame; flush so the test can
  // observe the result. Scenarios that specifically test the unflushed
  // case call flushRaf() themselves with custom expectations.
  flushRaf();
}

// Switch sessions: production runs _hydrateTodosFromSession at every
// settle point. Mirror that flow here.  Default panelActive is true
// when not explicitly set — production switches always render.
function switchTo(sessionId, opts) {
  opts = opts || {};
  if (opts.panelActive !== undefined) _panelActive = !!opts.panelActive;
  else if (!_panelActive) _panelActive = true;
  S.session = {
    session_id: sessionId,
    messages: opts.sessionMessages || null,
  };
  S.messages = opts.messages || [];
  if (opts.todoState) S.session.todo_state = opts.todoState;
  activeSid = sessionId;
  _hydrateTodosFromSession(S.session);
  if (_panelActive) loadTodos();
}

function deleteSession() {
  // Mirrors the delete-session path that calls _hydrateTodosFromSession(null).
  S.session = null;
  S.messages = [];
  activeSid = '';
  _hydrateTodosFromSession(null);
  if (_panelActive) loadTodos();
}

function setPanelActive(active) {
  _panelActive = !!active;
  if (active) {
    _resetTodosRenderCache();
    loadTodos();
  }
}

function snapshot() {
  return {
    todos: S.todos.slice(),
    meta: S.todoStateMeta && {...S.todoStateMeta},
    html: _panelInnerHTML,
    inflightTodos: (INFLIGHT[activeSid] && INFLIGHT[activeSid].todos) || null,
    persistCalls: _persistCalls,
    pendingRaf: _rafQueue.length,
  };
}

function eq(a, b, msg) {
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A !== B) throw new Error((msg || '') + ' expected ' + B + ', got ' + A);
}
function ok(v, msg) { if (!v) throw new Error(msg || 'expected truthy'); }
function notOk(v, msg) { if (v) throw new Error(msg || 'expected falsy'); }
function htmlContains(needle, msg) {
  if (_panelInnerHTML.indexOf(needle) < 0) {
    throw new Error((msg || 'panel HTML must contain') + ' ' + JSON.stringify(needle) +
                    '; actual=' + JSON.stringify(_panelInnerHTML.slice(0, 400)));
  }
}
function htmlOmits(needle, msg) {
  if (_panelInnerHTML.indexOf(needle) >= 0) {
    throw new Error((msg || 'panel HTML must NOT contain') + ' ' + JSON.stringify(needle));
  }
}

const results = [];
function scenario(category, name, body) {
  const key = category + '::' + name;
  try {
    reset();
    body();
    results.push({key, ok: true});
  } catch (e) {
    results.push({key, ok: false, err: String(e && e.stack || e)});
  }
}

function makeTodo(id, content, status) {
  return {id: String(id), content, status};
}
function todoPayload(todos, summary, ts) {
  // Match the canonical shape produced by api/todo_state.py.
  // ``ts`` is optional; the cold-load path uses it when present so the
  // frontend can reconcile cold-load vs. INFLIGHT by recency.
  const out = {todos, summary: summary || {total: todos.length}, version: 1};
  if (ts !== undefined) out.ts = ts;
  return out;
}

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 1 — Basic lifecycle
// ════════════════════════════════════════════════════════════════════════════

scenario('basic_lifecycle', 'empty session paints empty state', () => {
  mount();
  htmlContains('todos_no_active', 'empty state must show the i18n key');
});

scenario('basic_lifecycle', 'first todo write paints one item', () => {
  mount();
  emit({ session_id: 's-default', ts: 1, source: 'tool',
         ...todoPayload([makeTodo('1', 'plan', 'pending')]) });
  htmlContains('plan');
  htmlContains('pending');
});

scenario('basic_lifecycle', 'pending to in_progress transition', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','task','pending')]) });
  htmlContains('pending');
  emit({ session_id: 's-default', ts: 2,
         ...todoPayload([makeTodo('1','task','in_progress')]) });
  htmlContains('in_progress');
});

scenario('basic_lifecycle', 'in_progress to completed transition', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','task','in_progress')]) });
  emit({ session_id: 's-default', ts: 2,
         ...todoPayload([makeTodo('1','task','completed')]) });
  htmlContains('completed');
  htmlContains('text-decoration:line-through', 'completed should be struck through');
});

scenario('basic_lifecycle', 'add new item to existing list', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','first','pending')]) });
  emit({ session_id: 's-default', ts: 2,
         ...todoPayload([
           makeTodo('1','first','completed'),
           makeTodo('2','second','pending'),
         ]) });
  htmlContains('first');
  htmlContains('second');
});

scenario('basic_lifecycle', 'remove item by replacing list', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([
           makeTodo('1','one','pending'),
           makeTodo('2','two','pending'),
         ]) });
  htmlContains('one'); htmlContains('two');
  emit({ session_id: 's-default', ts: 2,
         ...todoPayload([makeTodo('2','two','pending')]) });
  htmlOmits('one', 'removed item must disappear');
  htmlContains('two');
});

scenario('basic_lifecycle', 'cancelled status renders distinct style', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','abandoned','cancelled')]) });
  htmlContains('cancelled');
});

scenario('basic_lifecycle', 'explicit empty list returns to empty state', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','xenon-task','pending')]) });
  htmlContains('xenon-task');
  emit({ session_id: 's-default', ts: 2, ...todoPayload([]) });
  htmlContains('todos_no_active', 'empty list must paint empty state');
  htmlOmits('xenon-task', 'cleared item must not linger');
});

scenario('basic_lifecycle', 'list of all completed still renders', () => {
  // Edge case: an "everything done" snapshot should not be confused
  // with no signal. Meta is set, list is non-empty.
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([
           makeTodo('1','one','completed'),
           makeTodo('2','two','completed'),
         ]) });
  htmlContains('one'); htmlContains('two');
});

scenario('basic_lifecycle', 'large list of 50 items renders cleanly', () => {
  mount();
  const items = [];
  for (let i = 0; i < 50; i++) items.push(makeTodo(String(i), 'task ' + i, 'pending'));
  emit({ session_id: 's-default', ts: 1, ...todoPayload(items) });
  htmlContains('task 0');
  htmlContains('task 49');
  ok(_panelInnerHTML.length > 1000, 'large list should produce substantial HTML');
});

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 2 — Multi-session navigation
// ════════════════════════════════════════════════════════════════════════════

scenario('multi_session', 'switching to fresh session clears todos', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','alpha-task','pending')]) });
  htmlContains('alpha-task');
  switchTo('s-fresh');
  htmlContains('todos_no_active', 'switching to a fresh session must clear');
  htmlOmits('alpha-task');
});

scenario('multi_session', 'switching to session with cold-load todo_state', () => {
  mount();
  switchTo('s-cold', {
    todoState: todoPayload([makeTodo('99','from-server','in_progress')]),
  });
  htmlContains('from-server');
  htmlContains('in_progress');
  eq(S.todoStateMeta.source, 'cold-load');
});

scenario('multi_session', 'switching to session with INFLIGHT only', () => {
  // Pre-seed INFLIGHT for the target session, then switch.
  INFLIGHT['s-inflight'] = {
    todos: [makeTodo('7','restored','pending')],
    todoStateMeta: {ts: 50, source: 'tool', version: 1},
    toolCalls: [], messages: [], uploaded: [],
  };
  switchTo('s-inflight');
  htmlContains('restored', 'INFLIGHT must rehydrate when no cold-load');
});

scenario('multi_session', 'cold-load wins over INFLIGHT when cold ts is newer', () => {
  INFLIGHT['s-mixed'] = {
    todos: [makeTodo('inflight','stale','pending')],
    todoStateMeta: {ts: 1, source: 'tool', version: 1},
    toolCalls: [], messages: [], uploaded: [],
  };
  switchTo('s-mixed', {
    todoState: todoPayload([makeTodo('cold','fresh','in_progress')], null, 100),
  });
  htmlContains('fresh', 'newer cold-load is the authoritative server view');
  htmlOmits('stale');
});

scenario('multi_session', 'INFLIGHT wins over cold-load when inflight ts is newer', () => {
  // P1: a stale cold-load (e.g. session GET cached an older snapshot)
  // must NOT regress fresher INFLIGHT state captured by the live SSE
  // stream. Reload of a still-running session would otherwise show a
  // visible rollback until the next todo_state event arrives.
  INFLIGHT['s-fresh-inflight'] = {
    todos: [makeTodo('live','live-state','in_progress')],
    todoStateMeta: {ts: 200, source: 'tool', version: 1},
    toolCalls: [], messages: [], uploaded: [],
  };
  switchTo('s-fresh-inflight', {
    todoState: todoPayload([makeTodo('stale','stale-cold','pending')], null, 100),
  });
  htmlContains('live-state', 'fresher INFLIGHT must not be regressed by stale cold-load');
  htmlOmits('stale-cold');
});

scenario('multi_session', 'tsless cold-load beats stale timestamped INFLIGHT', () => {
  // Regression for the "shows an old todo list" bug. When a context
  // compression/rebuild strips the timestamp off the latest todo tool
  // message, the server-derived cold-load carries the CORRECT latest
  // todos but no `ts` (coldTs=0). A stale earlier todo list persisted in
  // INFLIGHT still has a real timestamp. The old "strict >" rule made
  // the stale INFLIGHT win (0 > 150 is false), rendering a historical
  // list. coldTs===0 must now let the authoritative settled view win.
  INFLIGHT['s-tsless'] = {
    todos: [makeTodo('old','historical-list','completed')],
    todoStateMeta: {ts: 150, source: 'tool', version: 1},
    toolCalls: [], messages: [], uploaded: [],
  };
  switchTo('s-tsless', {
    // cold-load with the current list but NO ts (compression-stripped)
    todoState: todoPayload([makeTodo('new','current-list','in_progress')]),
  });
  htmlContains('current-list', 'tsless cold-load is the authoritative settled view');
  htmlOmits('historical-list', 'stale INFLIGHT must not win when cold-load has no ts');
  eq(S.todoStateMeta.source, 'cold-load', 'meta must reflect the cold-load win');
});

scenario('multi_session', 'session deletion clears the panel', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','toberemoved','pending')]) });
  htmlContains('toberemoved');
  deleteSession();
  htmlContains('todos_no_active');
  eq(S.todoStateMeta, null, 'meta sentinel must reset to null');
});

scenario('multi_session', 'event for session A is dropped when on session B', () => {
  mount({sessionId: 's-A'});
  emit({ session_id: 's-A', ts: 1,
         ...todoPayload([makeTodo('1','a-task','pending')]) });
  htmlContains('a-task');
  switchTo('s-B');
  // Spurious event from another tab/session.
  emitTodoStateRaw({data: JSON.stringify({
    session_id: 's-A', ts: 99,
    ...todoPayload([makeTodo('leak','LEAKED','pending')]),
  })});
  flushRaf();
  htmlOmits('LEAKED', 'cross-session event must not leak into session B');
});

scenario('multi_session', 'round-trip A -> B -> A preserves A via INFLIGHT', () => {
  mount({sessionId: 's-A'});
  emit({ session_id: 's-A', ts: 1,
         ...todoPayload([makeTodo('1','a-state','pending')]) });
  // Switching away should leave INFLIGHT for s-A populated by the
  // earlier listener emit + persist call. Because production restores
  // S.todos from session.todo_state OR INFLIGHT on switch back, the
  // round-trip needs the listener to have stored the snapshot.
  ok(INFLIGHT['s-A'] && Array.isArray(INFLIGHT['s-A'].todos),
     'INFLIGHT for s-A must hold the live snapshot');
  switchTo('s-B');
  htmlContains('todos_no_active');
  switchTo('s-A');
  htmlContains('a-state', 'returning to s-A must restore the todo');
});

scenario('multi_session', 'switching back to session sees latest cold-load', () => {
  // First visit primes INFLIGHT, then a server-side change is reflected
  // via cold-load when we return.  The cold-load ts must be newer than
  // the INFLIGHT one so the recency-aware hydrator picks it.
  mount({sessionId: 's-A'});
  emit({ session_id: 's-A', ts: 1,
         ...todoPayload([makeTodo('1','old','pending')]) });
  switchTo('s-B');
  // Simulate the agent advancing the todo while we were on s-B.
  switchTo('s-A', {
    todoState: todoPayload([makeTodo('1','old','completed')], null, 200),
  });
  htmlContains('completed', 'cold-load must reflect server-side advance');
});

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 3 — Event robustness
// ════════════════════════════════════════════════════════════════════════════

scenario('event_robustness', 'multiple events same frame coalesce to one paint', () => {
  mount();
  // Mute the auto-flush emit() does so we can observe RAF queue state.
  emitTodoStateRaw({data: JSON.stringify({
    session_id: 's-default', ts: 1,
    ...todoPayload([makeTodo('1','a','pending')]),
  })});
  emitTodoStateRaw({data: JSON.stringify({
    session_id: 's-default', ts: 2,
    ...todoPayload([makeTodo('1','a','in_progress')]),
  })});
  emitTodoStateRaw({data: JSON.stringify({
    session_id: 's-default', ts: 3,
    ...todoPayload([makeTodo('1','a','completed')]),
  })});
  // Three emits, one queued RAF — coalesced.
  eq(_rafQueue.length, 1, 'RAF coalescing failed');
  flushRaf();
  htmlContains('completed', 'final state should be the latest snapshot');
});

scenario('event_robustness', 'duplicate snapshot short-circuits render', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','x','pending')]) });
  const html1 = _panelInnerHTML;
  // Externally mutate the panel to detect whether a second render runs.
  _panelInnerHTML = '__sentinel__';
  emit({ session_id: 's-default', ts: 2,  // newer ts but same content
         ...todoPayload([makeTodo('1','x','pending')]) });
  eq(_panelInnerHTML, '__sentinel__',
     'identical snapshot must short-circuit the renderer');
});

scenario('event_robustness', 'older ts event is rejected', () => {
  mount();
  emit({ session_id: 's-default', ts: 100,
         ...todoPayload([makeTodo('1','newer','in_progress')]) });
  htmlContains('newer');
  emit({ session_id: 's-default', ts: 50,  // older
         ...todoPayload([makeTodo('1','older','pending')]) });
  htmlContains('newer', 'older event must not overwrite newer state');
  htmlOmits('older');
});

scenario('event_robustness', 'equal ts is allowed (compression after tool)', () => {
  mount();
  emit({ session_id: 's-default', ts: 100, source: 'tool',
         ...todoPayload([makeTodo('1','a','pending')]) });
  emit({ session_id: 's-default', ts: 100, source: 'compression',
         ...todoPayload([makeTodo('1','b','in_progress')]) });
  htmlContains('b', 'equal-ts compression refresh must apply');
});

scenario('event_robustness', 'malformed JSON is silently swallowed', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','kept','pending')]) });
  // Junk event.
  emitTodoStateRaw({data: '{not valid json'});
  flushRaf();
  htmlContains('kept', 'malformed payload must not corrupt rendered state');
});

scenario('event_robustness', 'non-array todos field is rejected', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','kept','pending')]) });
  emitTodoStateRaw({data: JSON.stringify({
    session_id: 's-default', ts: 2,
    todos: 'not an array',
  })});
  flushRaf();
  htmlContains('kept');
});

scenario('event_robustness', 'session_id mismatch is dropped', () => {
  mount({sessionId: 's-active'});
  emitTodoStateRaw({data: JSON.stringify({
    session_id: 's-OTHER', ts: 1,
    ...todoPayload([makeTodo('leak','LEAKED','pending')]),
  })});
  flushRaf();
  htmlOmits('LEAKED');
  htmlContains('todos_no_active', 'state untouched by the wrong-session event');
});

scenario('event_robustness', 'missing session_id is accepted (legacy server)', () => {
  // Pre-Phase-1 servers may not tag events. We only filter when the
  // tag is present.
  mount({sessionId: 's-active'});
  emitTodoStateRaw({data: JSON.stringify({
    ts: 1,
    ...todoPayload([makeTodo('1','untagged','pending')]),
  })});
  flushRaf();
  htmlContains('untagged');
});

scenario('event_robustness', 'replay of identical journal events is idempotent', () => {
  // Simulate SSE journal replay: same snapshot delivered twice with
  // the same ts. Production behaviour is "second is a no-op render".
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','x','pending')]) });
  _panelInnerHTML = '__sentinel__';
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','x','pending')]) });
  eq(_panelInnerHTML, '__sentinel__', 'replay must be idempotent');
});

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 4 — User content (XSS / unicode / size)
// ════════════════════════════════════════════════════════════════════════════

scenario('user_content', 'HTML in content is escaped', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','<script>alert("xss")</script>','pending')]) });
  htmlOmits('<script>');
  htmlContains('&lt;script&gt;');
});

scenario('user_content', 'HTML in id is escaped', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('<img src=x onerror=alert(1)>','x','pending')]) });
  htmlOmits('<img src=x');
  htmlContains('&lt;img src=x onerror=alert(1)&gt;');
});

scenario('user_content', 'unicode + emoji content renders as-is', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([
           makeTodo('1','研究 A股市场（行情数据）🔍','in_progress'),
           makeTodo('2','分析 ETF 流动性📈','pending'),
         ]) });
  htmlContains('研究 A股市场');
  htmlContains('🔍');
  htmlContains('📈');
});

scenario('user_content', 'very long content does not crash renderer', () => {
  mount();
  const longText = 'X'.repeat(2000);
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1', longText, 'pending')]) });
  htmlContains('XXXXXXXXXX', 'long content must render');
});

scenario('user_content', 'quotes in content are escaped safely', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1', `it's "important" & complex`, 'pending')]) });
  htmlContains('&#39;'); // single quote
  htmlContains('&quot;'); // double quote
  htmlContains('&amp;');  // ampersand
});

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 5 — Render scheduling
// ════════════════════════════════════════════════════════════════════════════

scenario('render_scheduling', 'hidden panel is not painted', () => {
  mount({panelActive: false});
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','hidden','pending')]) });
  // Panel was inactive across the whole flow.
  notOk(_panelInnerHTML.indexOf('hidden') >= 0,
        'inactive panel must not receive the snapshot DOM');
});

scenario('render_scheduling', 'panel re-show repaints latest snapshot', () => {
  mount({panelActive: false});
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','queued','pending')]) });
  // Now activate the panel and trigger a fresh paint via the same
  // production path (panel-switch handler calls loadTodos()).
  setPanelActive(true);
  htmlContains('queued', 're-shown panel must paint the latest snapshot');
});

scenario('render_scheduling', 'large list 200 items renders bounded', () => {
  // Catch O(n^2) regressions without flaking on slow CI hardware.
  // We compare rendering time at two list sizes; under the linear
  // contract, the ratio stays bounded by a small constant + warmup
  // noise.  An O(n^2) regression would push the ratio toward 16
  // (since 200 / 50 = 4 and 4^2 = 16).
  mount();
  function build(n) {
    const items = [];
    for (let i = 0; i < n; i++) {
      items.push(makeTodo(String(i), 'task ' + i,
        i % 4 === 0 ? 'completed' : 'pending'));
    }
    return items;
  }
  function measure(n) {
    const items = build(n);
    const start = Date.now();
    emit({ session_id: 's-default', ts: n, ...todoPayload(items) });
    return Math.max(1, Date.now() - start);  // floor 1ms to avoid div-by-zero
  }
  // Warmup so JIT noise doesn't dominate the smaller measurement.
  measure(50);
  const small = measure(50);
  const large = measure(200);
  // Linear: ratio ~4 + warmup noise.  Quadratic: ratio ~16.  We allow
  // up to 8 to leave plenty of slack for cold caches and CI jitter.
  const ratio = large / small;
  ok(ratio < 8,
     'render must scale ~linearly with list size; '
     + 'small=' + small + 'ms large=' + large + 'ms ratio=' + ratio.toFixed(2));
  htmlContains('task 199');
});

scenario('render_scheduling', 'rapid 100 events coalesce to one paint', () => {
  mount();
  // No flush mid-stream. 100 emits produce a single RAF callback.
  for (let i = 0; i < 100; i++) {
    emitTodoStateRaw({data: JSON.stringify({
      session_id: 's-default', ts: i + 1,
      ...todoPayload([makeTodo('1', 'iter ' + i, 'pending')]),
    })});
  }
  eq(_rafQueue.length, 1, 'all 100 events must share one RAF tick');
  flushRaf();
  htmlContains('iter 99', 'final paint shows the latest snapshot only');
});

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 6 — Compatibility / legacy fallback
// ════════════════════════════════════════════════════════════════════════════

scenario('compat_fallback', 'no signal + no messages yields empty state', () => {
  mount({messages: []});
  htmlContains('todos_no_active');
});

scenario('compat_fallback', 'legacy: single tool message reverse-scan', () => {
  mount({
    messages: [
      {role: 'user', content: 'plan something'},
      {role: 'assistant', content: 'sure'},
      {role: 'tool', content: JSON.stringify({
        todos: [makeTodo('1','legacy-task','pending')],
      })},
    ],
  });
  // S.todoStateMeta is null because we have no event/cold-load signal.
  // loadTodos must fall through to the reverse-scan.
  eq(S.todoStateMeta, null);
  htmlContains('legacy-task', 'legacy fallback should populate the panel');
});

scenario('compat_fallback', 'legacy: multiple writes — newest wins', () => {
  mount({
    messages: [
      {role: 'tool', content: JSON.stringify({todos: [makeTodo('1','old','completed')]})},
      {role: 'assistant', content: 'thinking'},
      {role: 'tool', content: JSON.stringify({todos: [makeTodo('1','new','in_progress')]})},
    ],
  });
  htmlContains('new');
  htmlOmits('old');
});

scenario('compat_fallback', 'legacy: skips non-todo tool messages', () => {
  mount({
    messages: [
      {role: 'tool', content: JSON.stringify({todos: [makeTodo('1','target','pending')]})},
      {role: 'tool', content: JSON.stringify({result: 'fs read ok'})},
      {role: 'tool', content: JSON.stringify({output: 'shell stdout'})},
    ],
  });
  htmlContains('target');
});

scenario('compat_fallback', 'legacy + first live event upgrades source-of-truth', () => {
  // Start in legacy mode, then a Phase-1 server begins emitting events.
  mount({
    messages: [
      {role: 'tool', content: JSON.stringify({todos: [makeTodo('1','legacy','pending')]})},
    ],
  });
  htmlContains('legacy');
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','live','in_progress')]) });
  htmlContains('live');
  htmlOmits('legacy');
  ok(S.todoStateMeta !== null, 'live event must promote out of legacy mode');
});

scenario('compat_fallback', 'session.messages preferred over S.messages in legacy', () => {
  mount({
    sessionMessages: [
      {role: 'tool', content: JSON.stringify({todos: [makeTodo('1','session-msg','pending')]})},
    ],
    messages: [
      {role: 'tool', content: JSON.stringify({todos: [makeTodo('1','top-level','pending')]})},
    ],
  });
  htmlContains('session-msg');
  htmlOmits('top-level');
});

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 7 — Realistic workflows
// ════════════════════════════════════════════════════════════════════════════

scenario('realistic_workflows', 'plan-then-execute four-step flow', () => {
  mount();
  // Step 1: agent plans 4 items.
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([
           makeTodo('1','setup project','pending'),
           makeTodo('2','run tests','pending'),
           makeTodo('3','fix failures','pending'),
           makeTodo('4','open PR','pending'),
         ]) });
  htmlContains('setup project');

  // Step 2..5: in_progress one at a time, then complete.
  for (let i = 1; i <= 4; i++) {
    const todos = [];
    for (let k = 1; k <= 4; k++) {
      let status = 'pending';
      if (k < i) status = 'completed';
      else if (k === i) status = 'in_progress';
      todos.push(makeTodo(String(k), ['setup project','run tests','fix failures','open PR'][k-1], status));
    }
    emit({ session_id: 's-default', ts: i + 1, ...todoPayload(todos) });
    htmlContains(['setup project','run tests','fix failures','open PR'][i-1]);
  }

  // Final: all completed.
  emit({ session_id: 's-default', ts: 10,
         ...todoPayload([
           makeTodo('1','setup project','completed'),
           makeTodo('2','run tests','completed'),
           makeTodo('3','fix failures','completed'),
           makeTodo('4','open PR','completed'),
         ]) });
  // All four completed lines must be struck through.
  const struck = (_panelInnerHTML.match(/text-decoration:line-through/g) || []).length;
  eq(struck, 4, 'four completed items should each render strikethrough');
});

scenario('realistic_workflows', 'plan revision: cancel one + add new', () => {
  mount();
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([
           makeTodo('1','approach A','in_progress'),
           makeTodo('2','approach B','pending'),
         ]) });
  // User decides A is wrong; agent cancels A, picks B, adds C.
  emit({ session_id: 's-default', ts: 2,
         ...todoPayload([
           makeTodo('1','approach A','cancelled'),
           makeTodo('2','approach B','in_progress'),
           makeTodo('3','approach C','pending'),
         ]) });
  htmlContains('cancelled');
  htmlContains('approach C');
});

scenario('realistic_workflows', 'long burst: 20 tools then todo write', () => {
  // Real session shape: agent does many tool calls before/after a todo
  // write. Only the todo write affects the panel; non-todo events do
  // not even reach this listener.
  mount();
  for (let i = 0; i < 20; i++) {
    // These would be 'tool' events in production; we only simulate
    // the todo_state subset of the SSE traffic relevant to this panel.
  }
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','final task','in_progress')]) });
  htmlContains('final task');
});

// ════════════════════════════════════════════════════════════════════════════
// CATEGORY 8 — Persistence + recovery
// ════════════════════════════════════════════════════════════════════════════

scenario('persistence_recovery', 'live emit triggers persistInflightState', () => {
  mount();
  INFLIGHT['s-default'] = {todos: null, todoStateMeta: null,
                           toolCalls: [], messages: [], uploaded: []};
  const before = _persistCalls;
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','x','pending')]) });
  ok(_persistCalls > before, 'persistInflightState must run on each live emit');
});

scenario('persistence_recovery', 'INFLIGHT mirror has the latest snapshot', () => {
  mount();
  INFLIGHT['s-default'] = {todos: null, todoStateMeta: null,
                           toolCalls: [], messages: [], uploaded: []};
  emit({ session_id: 's-default', ts: 1,
         ...todoPayload([makeTodo('1','live','in_progress')]) });
  ok(Array.isArray(INFLIGHT['s-default'].todos),
     'INFLIGHT.todos must mirror the live snapshot');
  eq(INFLIGHT['s-default'].todos[0].id, '1');
  ok(INFLIGHT['s-default'].todoStateMeta !== null);
});

scenario('persistence_recovery', 'reload simulation restores via INFLIGHT', () => {
  // Phase-1 of a "reload" cycle: live state is captured into INFLIGHT.
  mount();
  INFLIGHT['s-active'] = {todos: null, todoStateMeta: null,
                          toolCalls: [], messages: [], uploaded: []};
  S.session.session_id = 's-active'; activeSid = 's-active';
  emit({ session_id: 's-active', ts: 1,
         ...todoPayload([makeTodo('1','before-reload','in_progress')]) });

  // Phase-2: simulate the page reload by clearing in-memory S, then
  // re-mounting from cold-load + INFLIGHT.
  const persisted = JSON.parse(JSON.stringify(INFLIGHT['s-active']));
  S.todos = []; S.todoStateMeta = null; S.session = null; activeSid = '';
  // Re-pre-seed INFLIGHT (mimicking loadInflightState).
  INFLIGHT['s-active'] = persisted;

  switchTo('s-active');  // No cold-load; INFLIGHT must rehydrate.
  htmlContains('before-reload',
               'reload-then-reattach must restore via INFLIGHT');
});

// ── Final report ──────────────────────────────────────────────────────────
console.log('__BEGIN_RESULTS__');
console.log(JSON.stringify(results));
console.log('__END_RESULTS__');
"""


def _run_driver():
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS), str(PANELS_JS), str(MESSAGES_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"driver exited {proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\n\nSTDERR:\n{proc.stderr}"
        )
    out = proc.stdout
    begin = out.index("__BEGIN_RESULTS__")
    end = out.index("__END_RESULTS__")
    payload = out[begin:end].split("\n", 1)[1].strip()
    return json.loads(payload)


@pytest.fixture(scope="module")
def scenarios():
    return _run_driver()


# Scenario keys are declared up-front so pytest can parametrize at
# collection time. If a scenario disappears from the driver output, the
# `test_all_declared_scenarios_ran` guard surfaces it.
_DECLARED = [
    # basic_lifecycle
    "basic_lifecycle::empty session paints empty state",
    "basic_lifecycle::first todo write paints one item",
    "basic_lifecycle::pending to in_progress transition",
    "basic_lifecycle::in_progress to completed transition",
    "basic_lifecycle::add new item to existing list",
    "basic_lifecycle::remove item by replacing list",
    "basic_lifecycle::cancelled status renders distinct style",
    "basic_lifecycle::explicit empty list returns to empty state",
    "basic_lifecycle::list of all completed still renders",
    "basic_lifecycle::large list of 50 items renders cleanly",
    # multi_session
    "multi_session::switching to fresh session clears todos",
    "multi_session::switching to session with cold-load todo_state",
    "multi_session::switching to session with INFLIGHT only",
    "multi_session::cold-load wins over INFLIGHT when cold ts is newer",
    "multi_session::INFLIGHT wins over cold-load when inflight ts is newer",
    "multi_session::tsless cold-load beats stale timestamped INFLIGHT",
    "multi_session::session deletion clears the panel",
    "multi_session::event for session A is dropped when on session B",
    "multi_session::round-trip A -> B -> A preserves A via INFLIGHT",
    "multi_session::switching back to session sees latest cold-load",
    # event_robustness
    "event_robustness::multiple events same frame coalesce to one paint",
    "event_robustness::duplicate snapshot short-circuits render",
    "event_robustness::older ts event is rejected",
    "event_robustness::equal ts is allowed (compression after tool)",
    "event_robustness::malformed JSON is silently swallowed",
    "event_robustness::non-array todos field is rejected",
    "event_robustness::session_id mismatch is dropped",
    "event_robustness::missing session_id is accepted (legacy server)",
    "event_robustness::replay of identical journal events is idempotent",
    # user_content
    "user_content::HTML in content is escaped",
    "user_content::HTML in id is escaped",
    "user_content::unicode + emoji content renders as-is",
    "user_content::very long content does not crash renderer",
    "user_content::quotes in content are escaped safely",
    # render_scheduling
    "render_scheduling::hidden panel is not painted",
    "render_scheduling::panel re-show repaints latest snapshot",
    "render_scheduling::large list 200 items renders bounded",
    "render_scheduling::rapid 100 events coalesce to one paint",
    # compat_fallback
    "compat_fallback::no signal + no messages yields empty state",
    "compat_fallback::legacy: single tool message reverse-scan",
    "compat_fallback::legacy: multiple writes — newest wins",
    "compat_fallback::legacy: skips non-todo tool messages",
    "compat_fallback::legacy + first live event upgrades source-of-truth",
    "compat_fallback::session.messages preferred over S.messages in legacy",
    # realistic_workflows
    "realistic_workflows::plan-then-execute four-step flow",
    "realistic_workflows::plan revision: cancel one + add new",
    "realistic_workflows::long burst: 20 tools then todo write",
    # persistence_recovery
    "persistence_recovery::live emit triggers persistInflightState",
    "persistence_recovery::INFLIGHT mirror has the latest snapshot",
    "persistence_recovery::reload simulation restores via INFLIGHT",
]


def _id(key):
    return key.replace("::", "__").replace(" ", "_").replace("-", "_").replace(":", "")


@pytest.mark.parametrize("key", _DECLARED, ids=_id)
def test_scenario(scenarios, key):
    matches = [r for r in scenarios if r["key"] == key]
    assert matches, f"scenario {key!r} missing from driver output"
    r = matches[0]
    assert r["ok"], f"{key}\n  {r.get('err','(no detail)')}"


def test_all_declared_scenarios_ran(scenarios):
    """Drift-detector: declared keys must equal driver-reported keys."""
    declared = set(_DECLARED)
    actual = {r["key"] for r in scenarios}
    missing = declared - actual
    extra = actual - declared
    assert not missing and not extra, (
        f"\n  missing from driver output: {sorted(missing)}\n"
        f"  not in _DECLARED:           {sorted(extra)}"
    )
