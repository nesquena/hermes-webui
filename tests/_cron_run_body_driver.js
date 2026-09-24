// Behavioural driver for the cron-run body renderer in static/panels.js.
//
// The source-presence tests (test_issue2661_2629_frontend.py) assert the
// *shape* of `_renderCronRunBody` but pass even when a runtime detail is
// wrong. This driver runs the real function against a simulated DOM so the
// exact behaviour the 9/24 review probed is pinned:
//
//   CORE 1  - "View raw output" control exists and renders data.content
//   CORE 2  - toggling while the fetch is pending keeps the row open
//   SILENT 3- script jobs and empty responses stay on the raw view
//
// Usage: node <driver.js> <panels.js> <scenario-json>
const fs = require('fs');
const path = process.argv[2];
const scenario = JSON.parse(process.argv[3]);
const src = fs.readFileSync(path, 'utf8');

function extractFunc(name) {
  // Capture an optional ``async`` prefix so async functions stay async
  // (dropping it makes ``await`` a syntax error at eval time).
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const match = src.match(re);
  if (!match) throw new Error(name + ' not found');
  const start = match.index;
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// ---- simulated DOM ----------------------------------------------------
function makeClassList(initial) {
  const set = new Set(initial || []);
  return {
    add(c) { set.add(c); },
    remove(c) { set.delete(c); },
    contains(c) { return set.has(c); },
    toggle(c, on) {
      const want = on === undefined ? !set.has(c) : Boolean(on);
      if (want) set.add(c); else set.delete(c);
    },
    _set: set,
  };
}

function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    children: [],
    style: {},
    dataset: {},
    _attrs: {},
    _text: '',
    title: '',
    type: '',
    onclick: null,
    innerHTML: '',
    _parent: null,
  };
  // ``innerHTML = ''`` is how the renderer clears the body before it
  // re-mounts; the mock must drop the old children or stale elements
  // stay observable.
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._innerHTML || ''; },
    set(v) { el._innerHTML = String(v); if (el._innerHTML === '') el.children = []; },
  });
  // Keep ``className`` and ``classList`` in sync the way the DOM does,
  // so classification can read either.
  Object.defineProperty(el, '_classes', {
    value: makeClassList(),
    writable: true,
    enumerable: false,
  });
  Object.defineProperty(el, 'classList', { get() { return el._classes; } });
  Object.defineProperty(el, 'className', {
    get() { return Array.from(el._classes._set).join(' '); },
    set(v) {
      el._classes._set.clear();
      for (const c of String(v || '').split(/\s+/)) if (c) el._classes._set.add(c);
    },
  });
  Object.defineProperty(el, 'textContent', {
    get() {
      if (el.children.length === 0) return el._text;
      return el.children.map(c => c.textContent).join('');
    },
    set(v) { el._text = String(v); el.children = []; },
  });
  el.appendChild = (c) => { c._parent = el; el.children.push(c); return c; };
  el.remove = () => {
    el._removed = true;
    const p = el._parent;
    if (p) {
      const i = p.children.indexOf(el);
      if (i >= 0) p.children.splice(i, 1);
    }
  };
  el.setAttribute = (k, v) => { el._attrs[k] = String(v); };
  el.getAttribute = (k) => el._attrs[k];
  el.querySelector = (sel) => {
    const used = el._removed ? [] : el.children;
    for (const c of used) {
      if (sel === '.detail-run-body' && c._classes.contains('detail-run-body')) return c;
      if (sel === '.detail-expand-toggle' && c._classes.contains('detail-expand-toggle')) return c;
      const nested = c.querySelector(sel);
      if (nested) return nested;
    }
    return null;
  };
  el.querySelectorAll = () => [];
  return el;
}

// ---- globals the renderer expects ------------------------------------
const store = {};
global.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};

global.t = (key, ...args) => {
  const dict = {
    cron_run_response_label: 'Response',
    cron_run_show_prompt_context: 'Show prompt & context',
    cron_view_raw_output: 'View raw output',
    cron_view_response: 'View response',
    cron_view_full_output: 'View full output',
    cron_expand_output: 'Expand output',
    cron_collapse_output: 'Collapse output',
    loading: 'Loading',
  };
  const v = dict[key];
  return v === undefined ? key : v;
};

global.esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
global.jsArg = (s) => JSON.stringify(String(s == null ? '' : s));

global.document = {
  createElement: makeEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  addEventListener: () => {},
};

// The run-row elements the toggle / fetch path operate on.
let fetchCount = 0;
const rowItem = makeEl('div');
rowItem._classes.add('detail-run-item');
const rowBody = makeEl('div');
rowBody._classes.add('detail-run-body');
rowItem.appendChild(rowBody);
const rowToggle = makeEl('button');
rowToggle._classes.add('detail-expand-toggle');
rowItem.appendChild(rowToggle);

global.document.querySelector = (sel) => (sel === '#run1 .detail-run-body' ? rowBody : null);
global.document.getElementById = (id) => (id === 'run1' ? rowItem : null);

// ---- evaluate the real functions ------------------------------------
// Functions that only exist after a given fix are eval'd opportunistically:
// a missing one is recorded as ``scenario.missing`` so the caller can
// distinguish "behaviour regressed" from "the new helper does not exist
// on this revision" during reverse verification.
const missing = [];
function evalOrThrow(srcText, label) {
  try {
    return (0, eval)(srcText);
  } catch (e) {
    throw new Error('eval ' + label + ' failed: ' + e.message);
  }
}

evalOrThrow(extractFunc('_isCronScriptJob'), '_isCronScriptJob');
evalOrThrow(extractFunc('_formatCronRunUsageStrip'), '_formatCronRunUsageStrip');
evalOrThrow(extractFunc('_renderCronRunBody'), '_renderCronRunBody');
try {
  evalOrThrow(extractFunc('_appendCronRawOutputControl'), '_appendCronRawOutputControl');
} catch (e) {
  missing.push('_appendCronRawOutputControl');
  global._appendCronRawOutputControl = function () { throw new Error('not implemented'); };
}
evalOrThrow(extractFunc('toggleCronRunExpanded'), 'toggleCronRunExpanded');
evalOrThrow(extractFunc('_loadRunContent'), '_loadRunContent');
evalOrThrow(extractFunc('_cronRunExpandKey'), '_cronRunExpandKey');

// Expansion state helpers are module-level; provide storage-backed
// equivalents that match the production implementations.
global._cronPanelExpandKey = (jobId, suffix) => `hermes-webui-cron-${suffix}-expanded-${encodeURIComponent(String(jobId || ''))}`;
global._cronExpansionGet = (key) => { try { return localStorage.getItem(key) === '1'; } catch (_) { return false; } };
global._cronExpansionSet = (key, expanded) => { try { localStorage.setItem(key, expanded ? '1' : '0'); } catch (_) { } };
global._cronRunBodyCache = {};

let _currentCronDetail = scenario.currentCronDetail || null;
Object.defineProperty(global, '_currentCronDetail', {
  get() { return _currentCronDetail; },
  set(v) { _currentCronDetail = v; },
});

// api() returns a never-resolving promise for the pending-fetch scenario
// so the toggle can fire while the fetch is still in flight.
global.api = (url) => {
  fetchCount += 1;
  if (scenario.pendingFetch) {
    return new Promise(() => { });  // never settles
  }
  return Promise.resolve(scenario.apiResponse);
};

// ---- serialise the rendered body for assertions ----------------------
// Only the direct children of the run body are classified: nested
// elements (the <pre> inside the response block, the disclosure's
// inner <pre>) are covered by their parent entry.
function classify(node) {
  if (node.className === 'cron-run-response-block') {
    const pre = node.children[1];
    return { kind: 'response-block', text: pre && pre.children[0] ? pre.children[0].textContent : '' };
  }
  if (node._classes.contains('cron-run-context-disclosure')) {
    const pre = node.children[1];
    return { kind: 'context-disclosure', text: pre && pre.children[0] ? pre.children[0].textContent : '' };
  }
  if (node.className === 'cron-run-pre') {
    return { kind: 'pre', text: node.children[0] ? node.children[0].textContent : '' };
  }
  if (node.tagName === 'BUTTON') {
    return { kind: 'button', label: node.textContent, removed: !!node._removed };
  }
  if (node.className === 'cron-run-usage-strip cron-run-usage-footer') {
    return { kind: 'usage', text: node.textContent };
  }
  return { kind: 'other', className: node.className, tag: node.tagName };
}

function serialize(el) {
  return el.children.map(classify);
}

async function main() {
  const payload = scenario.payload;
  const result = { fetchCount: 0, missing };

  if (scenario.mode === 'render') {
    // Directly exercise _renderCronRunBody at a known expansion state.
    global._cronExpansionSet(global._cronRunExpandKey(scenario.jobId, scenario.filename), scenario.expanded);
    rowBody.innerHTML = '';
    try {
      _renderCronRunBody(rowBody, payload, scenario.jobId, scenario.filename);
    } catch (e) {
      // A helper that does not exist on this revision throws; record it
      // so the test can report "no raw control" instead of crashing.
      result.renderError = e.message;
    }
    result.rendered = serialize(rowBody);
    result.expandedClass = rowBody._classes.contains('expanded');
  } else if (scenario.mode === 'render-then-click-raw') {
    // Render response-first, then click "View raw output" and check the
    // verbatim artifact is mounted, then click "View response" back.
    // Production populates the cache inside _loadRunContent before it
    // renders, so the driver pre-populates it the same way.
    global._cronExpansionSet(global._cronRunExpandKey(scenario.jobId, scenario.filename), true);
    global._cronRunBodyCache[global._cronRunExpandKey(scenario.jobId, scenario.filename)] = payload;
    rowBody.innerHTML = '';
    try {
      _renderCronRunBody(rowBody, payload, scenario.jobId, scenario.filename);
    } catch (e) {
      result.renderError = e.message;
    }
    result.beforeClick = serialize(rowBody);
    const rawBtn = rowBody.children.find(c => c.tagName === 'BUTTON' && c.textContent.indexOf('raw output') >= 0);
    result.hasRawButton = !!rawBtn;
    if (rawBtn) rawBtn.onclick();
    result.afterRawClick = serialize(rowBody);
    const backBtn = rowBody.children.find(c => c.tagName === 'BUTTON' && c.textContent.indexOf('View response') >= 0);
    result.hasBackButton = !!backBtn;
    if (backBtn) backBtn.onclick();
    result.afterBackClick = serialize(rowBody);
  } else if (scenario.mode === 'toggle-pending') {
    // 1. user clicks the run head → _loadRunContent starts the fetch
    //    (pending promise) and marks the row open.
    rowItem._classes.remove('open');
    rowBody.innerHTML = '';
    const loadPromise = _loadRunContent(scenario.jobId, scenario.filename, 'run1');
    await Promise.resolve();
    await Promise.resolve();
    result.openAfterLoad = rowItem._classes.contains('open');
    result.fetchAfterLoad = fetchCount;
    // 2. while the fetch is pending, the user clicks the expand toggle.
    global._cronExpansionSet(global._cronRunExpandKey(scenario.jobId, scenario.filename), false);
    rowItem._classes.add('open');
    toggleCronRunExpanded(scenario.jobId, scenario.filename, 'run1');
    result.openAfterToggle = rowItem._classes.contains('open');
    result.fetchAfterToggle = fetchCount;
    result.storedExpanded = global._cronExpansionGet(global._cronRunExpandKey(scenario.jobId, scenario.filename));
    // 3. the fetch resolves and must render the toggled (expanded) state.
    if (scenario.resolveFetch) {
      global._cronRunBodyCache[global._cronRunExpandKey(scenario.jobId, scenario.filename)] = payload;
      _renderCronRunBody(rowBody, payload, scenario.jobId, scenario.filename);
    }
    result.rendered = serialize(rowBody);
  }

  result.fetchCount = fetchCount;
  process.stdout.write(JSON.stringify(result));
}

main().catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
});
