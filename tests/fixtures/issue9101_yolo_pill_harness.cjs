'use strict';
// #9101 driver: the YOLO pill must show only the OPEN session's server state,
// and its "click to disable" must never enable YOLO or approve anything.
//
// Usage: node issue9101_yolo_pill_harness.cjs <scenario> [repo-root]
// Exit 0 = invariant held, 1 = invariant violated, 2 = harness error.
//
// Each "tab" is an isolated vm context running the REAL code:
//   - loadSession() from static/sessions.js;
//   - the whole "YOLO mode state" section of static/messages.js (its module
//     state, the read and the pill renderer);
//   - the whole "YOLO mode toggle" section of static/commands.js;
//   - the pill's real inline onclick, read from static/index.html.
// Sections are sliced by their comment headers and parse-checked with
// vm.Script (no eval). Only rendering/IO helpers are stubbed as no-ops. All
// tabs of a scenario share one fake server that keeps one YOLO flag per
// session, like tools.approval._session_yolo; a response can be HELD: the
// server answers when the request is issued, delivery is deferred.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const which = process.argv[2];
const root = path.resolve(process.argv[3] || path.join(__dirname, '..', '..'));

function readStatic(name) {
  return fs.readFileSync(path.join(root, 'static', name), 'utf8');
}

function parses(src) {
  try { new vm.Script(src); return true; } catch (e) { if (e instanceof SyntaxError) return false; throw e; }
}

// Full source of a top-level `[async ]function <name>(...) {...}`: the shortest
// slice from the keyword to a `}` that parses whole.
function extractFunction(src, name) {
  const m = new RegExp('^(async\\s+)?function\\s+' + name + '\\s*\\(', 'm').exec(src);
  if (!m) throw new Error('function not found: ' + name);
  let idx = src.indexOf('{', m.index);
  for (;;) {
    idx = src.indexOf('}', idx + 1);
    if (idx < 0) throw new Error('no parseable end for ' + name);
    const slice = src.slice(m.index, idx + 1);
    if (parses('(' + slice + '\n)')) return slice;
  }
}

// A contiguous top-level section between two comment/declaration markers.
function extractSection(src, startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error('section start not found: ' + startMarker);
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error('section end not found: ' + endMarker);
  const slice = src.slice(start, end);
  if (!parses(slice)) throw new Error('section does not parse: ' + startMarker);
  return slice;
}

// Helpers loadSession calls unguarded; all are rendering/IO side effects
// irrelevant to YOLO state.
const NOOP_STUBS = [
  '_rearmActiveSessionStream', '_sessionVisitHasUnreadState', '_acknowledgeSessionVisit',
  'stopApprovalPolling', 'hideApprovalCard', 'startApprovalPolling',
  '_captureSameSessionForceReloadHint', '_setActiveSessionUrl', 'syncTopbar', 'renderMessages',
  'updateSendBtn', 'setStatus', 'setComposerStatus', 'updateQueueBadge', '_deferWorkspaceRefreshForSession',
  'setBusy', '_mergePendingSessionMessage', '_renderRuntimeJournalAnchorActivityScene',
  '_normalizeInflightReplayCursorForReattach', 'clearLiveToolCards', 'appendThinking', '_inflightNamesOtherStream',
  '_sessionProfileMismatchFromError', 'renderSessionListFromCache',
  '_hermesNotifySessionOpen', '_resolveSessionIdFromSidebarLineage',
  '_clearSameSessionForceReloadHint', '_clearDeferredActiveSessionExternalRefresh',
  '_resolveSessionModelForDisplaySoon', '_serverLiveSnapshotInflight', '_selectLiveRecoveryInflight',
  '_isMessagingSession', '_hideHandoffHint', '_handleComposerDraftPostFailure',
];

function makeEl(id) {
  const classes = new Set();
  return {
    id, style: { display: '' }, title: '', innerHTML: '', value: '', attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    classList: {
      contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, force) => { const on = force === undefined ? !classes.has(c) : !!force; if (on) classes.add(c); else classes.delete(c); return on; },
    },
  };
}

function createTab(server, name, overrides = null, extraSrc = []) {
  const sessionsJs = readStatic('sessions.js');
  const messagesJs = readStatic('messages.js');
  const commandsJs = readStatic('commands.js');
  const indexHtml = readStatic('index.html');
  const pillTag = /<button[^>]*id="yoloPill"[^>]*>/.exec(indexHtml);
  if (!pillTag) throw new Error('yoloPill not found in index.html');
  const onclickMatch = /onclick="([^"]*)"/.exec(pillTag[0]);
  if (!onclickMatch) throw new Error('yoloPill has no onclick');
  const pillOnclick = onclickMatch[1].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  const els = {};
  const calls = [];
  const toasts = [];
  const held = [];
  const base = {
    console, Promise, JSON, Object, Array, Number, String, Boolean, Set, Map, Date, Math, Error, RegExp, URL,
    encodeURIComponent, decodeURIComponent, setTimeout: () => 0, clearTimeout() {},
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    document: {
      getElementById: id => els[id] || (els[id] = makeEl(id)), querySelector() { return null; },
      querySelectorAll: sel => (sel.charAt(0) === '#' ? [base.document.getElementById(sel.slice(1))] : []),
      addEventListener() {}, hidden: false,
    },
    INFLIGHT: {},
    t: k => k,
  };
  for (const n of NOOP_STUBS) base[n] = function () { return undefined; };
  base.showToast = (msg) => { toasts.push(msg); };
  base.$ = id => base.document.getElementById(id);
  base.api = async (p, opts = {}) => {
    const method = (opts && opts.method) || 'GET';
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push({ tab: name, method, path: p, body });
    const answer = server.handle(method, p, body);
    if (server.shouldHold && server.shouldHold(name, method, p)) {
      return new Promise(resolve => held.push({ tab: name, method, path: p, answer, release: () => resolve(answer) }));
    }
    return answer;
  };
  base._ensureMessagesLoaded = async () => { base.S.messages = []; };
  base.S = { session: null, messages: [], busy: false, activeStreamId: null, toolCalls: [] };
  // The tab's EventSources (the real startSessionStream() opens one when a
  // scenario loads it; see streamTab()). The server pushes frames into them.
  const sources = [];
  base.EventSource = function FakeEventSource(url) {
    this.url = String(url); this.listeners = {}; this.readyState = 1; this.tab = name;
    sources.push(this); server.sources.push(this);
    setImmediate(() => server.sendInitial(this));
  };
  base.EventSource.prototype.addEventListener = function (ev, fn) { this.listeners[ev] = fn; };
  base.EventSource.prototype.close = function () { this.readyState = 2; };
  base.window = base;
  if (overrides) overrides(base);
  const ctx = vm.createContext(new Proxy(base, {
    has() { return true; },
    get(t, k) { return k in t ? t[k] : undefined; },
    set(t, k, v) { t[k] = v; return true; },
  }));
  const src = [
    'let _loadingSessionId = null;',
    'let _loadSessionGeneration = 0;',
    'let _messageUserUnpinned = false; let _scrollPinned = true;',
    'let _messagesTruncated = false; let _oldestIdx = 0; let _loadingOlder = false;',
    extractFunction(sessionsJs, 'loadSession'),
    extractSection(messagesJs, '// ── YOLO mode state ──', 'async function toggleYoloFromApproval('),
    extractSection(commandsJs, '// ── YOLO mode toggle ──', '// ── Branch / fork command'),
    'function __state(){ const pill=$("yoloPill"); return {sid: S.session && S.session.session_id, yolo: _yoloEnabled,',
    '  pillVisible: pill.style.display !== "none" && !pill.classList.contains("composer-control-hidden"),',
    '  pillSession: pill.getAttribute("data-yolo-session"),',
    '  gen: _loadSessionGeneration, loading: _loadingSessionId}; }',
    ...extraSrc,
  ].join('\n');
  vm.runInContext(src, ctx, { filename: name + '-harness.js' });
  return {
    name, ctx, calls, toasts, held, pillOnclick, sources,
    loadSession: (sid, opts) => vm.runInContext('loadSession', ctx)(sid, opts),
    cmdYolo: () => vm.runInContext('cmdYolo', ctx)(),
    clickPill: () => {
      const pill = ctx.$('yoloPill');
      if (pill.style.display === 'none') throw new Error('pill not visible; a user cannot click it');
      return vm.runInContext(pillOnclick, ctx, { filename: name + '-pill-onclick.js' });
    },
    state: () => vm.runInContext('__state()', ctx),
  };
}

// The fake server mirrors api/route_approvals.py (#9101): every observed change
// of a session's flag takes the next number of one counter; answers carry
// {yolo_enabled, yolo_revision, yolo_epoch}. A restart clears the flags and
// the counter and starts a new epoch. It also models session-stream frames (a
// `yolo-changed` push per moved revision and an `initial` snapshot) that the
// real server doesn't send yet: the client ignores them, as on master, and the
// scenarios below prove they would be harmless and pin the follow-up's cases.
const reject = (status, payload) => Promise.reject(Object.assign(new Error(payload.error || 'error'), { status, body: JSON.stringify(payload) }));
function createServer(sessions) {
  const server = {
    yolo: {},
    sessions,
    shouldHold: null,
    counter: 0, recorded: {}, epoch: 'boot-1', boots: 1, sources: [], holdFrames: false, frames: [],
    snapshot(sid) {
      const enabled = !!server.yolo[sid];
      const entry = server.recorded[sid];
      if (entry ? entry.enabled !== enabled : enabled) {
        server.counter += 1;
        server.recorded[sid] = { rev: server.counter, enabled };
        server.push(sid, 'yolo-changed', server.state(sid));
      }
      return server.state(sid);
    },
    state(sid) {
      const entry = server.recorded[sid];
      return { yolo_enabled: !!server.yolo[sid], yolo_revision: entry ? entry.rev : 0, yolo_epoch: server.epoch };
    },
    streamSid: es => new URL('http://x/' + es.url).searchParams.get('session_id'),
    push(sid, event, data) {
      for (const es of server.sources) {
        if (es.readyState !== 1 || server.streamSid(es) !== sid) continue;
        const frame = () => { if (es.readyState === 1 && es.listeners[event]) es.listeners[event]({ data: JSON.stringify({ session_id: sid, ...data }) }); };
        if (server.holdFrames) server.frames.push(frame); else setImmediate(frame);
      }
    },
    releaseFrames() { const f = server.frames.splice(0); f.forEach(fn => fn()); },
    sendInitial(es) {
      const sid = server.streamSid(es);
      if (es.readyState === 1 && es.listeners.initial) es.listeners.initial({ data: JSON.stringify({ session_id: sid, ...server.snapshot(sid) }) });
    },
    restart() {
      server.yolo = {}; server.recorded = {}; server.counter = 0; server.boots += 1; server.epoch = 'boot-' + server.boots;
      for (const es of server.sources) if (es.readyState === 1) setImmediate(() => server.sendInitial(es)); // EventSource reconnects
    },
    handle(method, p, body) {
      const u = new URL('http://x' + p);
      if (u.pathname === '/api/session' && method === 'GET') {
        const sid = u.searchParams.get('session_id');
        const meta = server.sessions[sid] || {};
        return { session: { session_id: sid, title: sid, message_count: 0, messages: [], active_stream_id: meta.active_stream_id || null } };
      }
      if (u.pathname === '/api/session/new' && method === 'POST') {
        return { session: { session_id: server.newSessionId || 'N', title: 'Untitled', workspace: body.workspace, messages: [] } };
      }
      if (u.pathname === '/api/session/yolo' && method === 'GET') {
        if (server.yoloGetBody) return server.yoloGetBody;
        if (server.failGet) return reject(500, { error: 'read failed' });
        return server.snapshot(u.searchParams.get('session_id'));
      }
      if (u.pathname === '/api/session/yolo' && method === 'POST' && server.failPost) {
        const e = new Error('relay busy');
        e.body = JSON.stringify({ error: 'relay busy', yolo_enabled: !!server.yolo[body.session_id] });
        return Promise.reject(e);
      }
      if (u.pathname === '/api/session/yolo' && method === 'POST') {
        // api/routes.py: `enabled` must be a boolean; expected_revision/epoch
        // make the write conditional (409 yolo_changed when it moved).
        if (typeof body.enabled !== 'boolean') return reject(400, { error: 'enabled is required and must be true or false' });
        const sid = body.session_id;
        if (body.expected_revision !== undefined && body.expected_revision !== null) {
          const cur = server.snapshot(sid);
          if (cur.yolo_revision !== body.expected_revision || (body.expected_epoch != null && body.expected_epoch !== cur.yolo_epoch)) {
            return reject(409, { ok: false, code: 'yolo_changed', error: 'YOLO changed since it was read; nothing was toggled.', ...cur });
          }
        }
        server.yolo[sid] = body.enabled;
        return { ok: true, ...server.snapshot(sid) };
      }
      return {};
    },
  };
  return server;
}

const tick = async (n = 20) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

function check(cond, msg, ctx) {
  if (!cond) { const e = new Error(msg); e.violation = true; e.ctx = ctx; throw e; }
}
const yoloGet = sid => (tab, method, p) => method === 'GET' && p === '/api/session/yolo?session_id=' + sid;
const posts = tab => tab.calls.filter(c => c.method === 'POST' && c.path === '/api/session/yolo').map(c => c.body);

// Stand-ins for the approval card owned by `sid`: the card's YOLO action
// approves the displayed command ("once") and enables YOLO.
function visibleCardOverrides(server, sid) {
  return (base) => {
    base._captureApprovalResponseOwner = () => ({ sid, approvalId: 'ap-' + sid + '-1' });
    base._approvalResponseOwnerIsCurrent = () => true;
    base.toggleYoloFromApproval = async () => {
      server.answered.push({ session_id: sid, approval_id: 'ap-' + sid + '-1', choice: 'once', yolo: true });
      server.yolo[sid] = true;
      return true;
    };
  };
}

// The four blank-page entry points that mint a session with POST
// /api/session/new and install it into S.session without loadSession().
const BLANK_PAGE_INSTALLERS = [
  ['panels.js', 'promptWorkspacePath'], ['panels.js', 'switchToWorkspace'],
  ['ui.js', 'promptNewFile'], ['ui.js', 'promptNewFolder'],
];
function blankPageInstallerSrc() {
  return BLANK_PAGE_INSTALLERS.map(([file, fn]) => extractFunction(readStatic(file), fn));
}
const blankPageStubs = (base) => {
  base.showPromptDialog = async () => null; // the user cancels the follow-up prompt
  base.renderSessionList = async () => {};
  base._workspaceCreateTargetLabel = () => '';
};

// A pill painted for A stays on screen when the open session is dropped
// without a re-render (the delete paths set S.session=null); a blank-page
// action then installs a brand-new session N (server YOLO off).
async function blankPageInstallKeepsStalePill(fn) {
  const server = createServer({ A: {} });
  const tab = createTab(server, 'tab1', blankPageStubs, blankPageInstallerSrc());
  await tab.loadSession('A');
  await tick();
  await tab.cmdYolo();
  await tick();
  check(tab.state().pillVisible && server.yolo.A === true, 'precondition: /yolo enabled A and shows its pill', tab.state());
  tab.ctx.S.session = null;
  tab.ctx.S._profileDefaultWorkspace = '/ws';
  // switchToWorkspace continues into a workspace mutation after the install;
  // a busy flag makes it stop right after the install with a toast.
  if (fn === 'switchToWorkspace') tab.ctx.S.busy = true;
  await vm.runInContext(fn, tab.ctx)();
  await tick();
  const st = tab.state();
  const out = { entry: fn, state: st, server: { ...server.yolo }, calls: tab.calls.map(c => c.method + ' ' + c.path) };
  check(st.sid === 'N', `precondition: ${fn} installed the new session`, out);
  check(!st.pillVisible && st.yolo === false && !server.yolo.N,
    `${fn} installed N (server yolo[N]=false) but pill visible=${st.pillVisible} (painted for ${st.pillSession}), mirror=${st.yolo}`, out);
  return out;
}

// The stream's real `apperror` listener, attached to a fake EventSource for
// stream `streamId` of session `activeSid`. Closure state is declared here;
// rendering/anchor helpers are stubbed by apperrorStubs.
function apperrorAttachSrc() {
  const src = readStatic('messages.js');
  const start = src.indexOf("source.addEventListener('apperror',e=>{");
  if (start < 0) throw new Error('apperror listener not found');
  let idx = start;
  for (;;) {
    idx = src.indexOf('});', idx + 1);
    if (idx < 0) throw new Error('no parseable end for the apperror listener');
    const slice = src.slice(start, idx + 3);
    if (parses(slice)) {
      return 'function __attachApperror(source, activeSid, streamId){\n' +
        '  let assistantText=""; let _terminalStateReached=false; let _persistTimer=null;\n' +
        '  let _streamFinalized=false; let _anchorRegistry=null;\n' + slice + '\n}';
    }
  }
}
const APPERROR_NOOPS = [
  '_clearStreamEndRecovery', '_steerSettleTerminal', '_cancelThrottledSnapshotTimer', '_clearAnchorProseIncrementalNode',
  '_cancelAnimationFramePendingStreamRender', '_streamFadeCleanupReduceMotionListener', '_smdEndParser',
  'finalizeThinkingCard', '_clearOwnerInflightState', '_clearStreamHidden', '_clearStreamNotificationBackground',
  '_clearApprovalForOwner', '_clearClarifyForOwner', '_flushReasoningToAnchor', '_applyToAnchor',
  '_scheduleAnchorRegistryCleanup', 'removeThinking', 'clearCompressionUi', '_attachProjectedAnchorSceneToLastAssistant',
  '_markSessionViewed', '_setActivePaneIdleIfOwner', 'renderSessionList', '_dispatchExtensionTurnLifecycle',
  '_retrySettledAnchorScene', 'trackBackgroundError',
];
const apperrorStubs = (base) => {
  for (const n of APPERROR_NOOPS) base[n] = function () { return undefined; };
  base._bailOutOfTerminalEventsFromStaleStream = () => false;
  base._streamRecoveryControlMessageText = () => false;
  base._carryForwardEphemeralTurnFields = (prev, next) => next;
  base._filterRecoveryControlMessages = m => m;
  base._settledAnchorRetryOwnerKey = () => null;
  base._restoreSettledSession = async () => false;
};
function fakeEventSource() {
  return { readyState: 1, listeners: {}, close() { this.readyState = 2; }, addEventListener(ev, fn) { this.listeners[ev] = fn; } };
}

// A tab whose loadSession() opens the REAL per-session stream
// (startSessionStream from messages.js) on the fake EventSource above.
const STREAM_NOOPS = ['_stopHiddenActiveStreamPoll', '_startHiddenActiveStreamPoll', '_handleBgTaskCompleteEvent'];
function streamTab(server, name, overrides = null, extraSrc = []) {
  const messagesJs = readStatic('messages.js');
  return createTab(server, name, (base) => {
    for (const n of STREAM_NOOPS) base[n] = function () { return undefined; };
    base._chatStreamActiveForSession = () => false;
    base._apiUrl = p => p;
    if (overrides) overrides(base);
  }, [
    'let _sessionEventSource = null; let _sessionStreamSessionId = null;',
    'let _sessionStreamHiddenSid = null; let _sessionStreamReconnectTimer = null;',
    extractFunction(messagesJs, 'startSessionStream'),
    extractFunction(messagesJs, 'stopSessionStream'),
    ...extraSrc,
  ]);
}
// The composer-footer visibility settings from static/boot.js, run for real.
function bootVisibilitySrc() {
  const bootJs = readStatic('boot.js');
  return [
    extractSection(bootJs, 'const _COMPOSER_CONTROL_TOGGLE_DEFS=[', 'function _sanitizeComposerControlOrder('),
    extractFunction(bootJs, '_setComposerControlHidden'),
    extractFunction(bootJs, '_applyComposerFooterVisibilitySettings'),
  ];
}
const synced = (tab, server, sid) => tab.state().pillVisible === !!server.yolo[sid];
const yoloReads = (tab, sid) => tab.calls.filter(c => c.method === 'GET' && c.path === '/api/session/yolo?session_id=' + sid).length;

// The approval card's real response path (respondApproval and its owner
// helpers from messages.js); rendering and head reconciliation are stubbed.
function approvalSrc(sid) {
  const messagesJs = readStatic('messages.js');
  return [
    `let _approvalSessionId=${JSON.stringify(sid)}, _approvalCurrentId="ap-${sid}-1";`,
    `let _approvalDisplayedOwner={sid:${JSON.stringify(sid)},approvalId:"ap-${sid}-1",runId:"",mirrorToken:""};`,
    'let _approvalResponding=null, _approvalClearedOwner=null; const _approvalPendingBySession=new Map();',
    ...['_approvalOwnerIdentityMatches', '_captureApprovalResponseOwner', '_approvalResponseOwnerIsCurrent',
      '_applyApprovalYoloProjection', 'respondApproval'].map(n => extractFunction(messagesJs, n)),
  ];
}
function approvalStubs(base) {
  base._approvalResponseMatches = () => false;
  for (const n of ['_unmarkApprovalDismissed', '_setApprovalControlsDisabled', '_releaseApprovalResponseOwner',
    '_restoreFailedApprovalResponse', 'hideApprovalCard', '_clearApprovalPendingForSession']) base[n] = () => {};
  base._reconcileApprovalToHead = async () => true;
  base._approvalMirrorOwnerFor = () => ({ runId: '', mirrorToken: '' });
  base._isGatewayUnavailableOrphan = () => false;
}

// r9/r9c: first sync overlapping a restart. The install read (answered by the
// old process) and a read sent after the restart (new process) both leave
// before the view knows any epoch; the old answer lands first. Across a
// restart answers follow the pre-#9101 rules (here: delivery order), so the
// new process's answer, landing last, is shown with no extra read; reads fail
// from then on. on=true: old OFF, new ON; on=false: old ON, new OFF.
async function firstSyncOverlap(on) {
  const server = createServer({ B: {} });
  const tab = createTab(server, 'tab1');
  if (!on) server.yolo.B = true;
  server.shouldHold = yoloGet('B');
  await tab.loadSession('B'); await tick();
  const oldRead = tab.held.shift();
  check(!!oldRead, 'precondition: install read held', tab.calls);
  server.restart(); if (on) server.yolo.B = true;
  const fresh = vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
  const newRead = tab.held.shift();
  check(!!newRead, 'precondition: post-restart read held', tab.calls);
  server.shouldHold = null;
  oldRead.release(); await tick();
  const afterOld = tab.state();
  check(afterOld.pillVisible === !on, 'precondition: the old answer set the view', afterOld);
  server.failGet = true;
  const readsBefore = yoloReads(tab, 'B');
  newRead.release(); await fresh; await tick(40);
  const out = { afterOld, after: tab.state(), extraReads: yoloReads(tab, 'B') - readsBefore, server: server.state('B') };
  check(out.after.pillVisible === on && out.after.yolo === on && synced(tab, server, 'B'), on
    ? 'YOLO is on (new process, answered last) but the pill is hidden'
    : 'YOLO is off (new process, answered last) but the old ON pill stayed', out);
  check(out.extraReads === 0, `expected no extra read, saw ${out.extraReads}`, out);
  return out;
}

const scenarios = {
  // (r1) KNOWN GAP, as on master (follow-up issue): a /yolo in one tab should
  // reach another tab that has the session open, through `yolo-changed` on the
  // existing session stream. The client does not apply frames (applying them
  // in delivery order breaks r2d; ordering them needs the revision).
  async r1_cross_tab_yolo_reaches_open_tab_via_session_stream() {
    const server = createServer({ B: {} });
    const tab1 = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab1.loadSession('B');
    await tab2.loadSession('B');
    await tick();
    check(tab1.sources.length === 1 && tab2.sources.length === 1, 'precondition: each tab opened one session stream', { t1: tab1.sources.length, t2: tab2.sources.length });
    await tab2.cmdYolo();
    await tick();
    const afterEnable = tab1.state();
    await tab1.cmdYolo();
    await tick();
    const out = { tab1_after_tab2_enable: afterEnable, tab2_after_tab1_disable: tab2.state(), server: { ...server.yolo } };
    check(afterEnable.pillVisible === true && server.yolo.B === false && tab2.state().pillVisible === false,
      'a /yolo in one tab did not reach the other open tab', out);
    return out;
  },

  // (r2) KNOWN GAP, as on master (follow-up issue): an older answer must never
  // override a newer change: tab1's pill disable answer (revision n) arrives
  // after tab2's enable frame (n+1).
  async r2_older_write_answer_never_overrides_newer_frame() {
    const server = createServer({ B: {} });
    const tab1 = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab1.loadSession('B');
    await tab2.loadSession('B');
    await tick();
    await tab1.cmdYolo();
    await tick();
    check(tab1.state().pillVisible && server.yolo.B === true, 'precondition: tab1 enabled B', tab1.state());
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'POST' && p === '/api/session/yolo';
    const click = tab1.clickPill();
    await tick();
    check(tab1.held.length === 1 && server.yolo.B === false, 'precondition: tab1 disable applied, answer in flight', tab1.calls);
    server.shouldHold = null;
    await tab2.cmdYolo();
    await tick();
    check(server.yolo.B === true, 'precondition: tab2 re-enabled B', { ...server.yolo });
    tab1.held.shift().release();
    await click;
    await tick();
    const out = { tab1: tab1.state(), server: { ...server.yolo } };
    check(synced(tab1, server, 'B'), `older disable answer hid the pill while server yolo[B]=${server.yolo.B}`, out);
    return out;
  },

  // (r2b-r2e) revision order within one server run, per request path. KNOWN
  // GAPS, as on master (follow-up issue), except r2d (frames are ignored).
  // (r2b) a read's newer answer is applied after an older /yolo answer that
  // landed after the read was sent (master's read-sequence rule drops it).
  async r2b_newer_read_answer_overrides_older_write_answer() {
    const server = createServer({ B: {} });
    const tab1 = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab1.loadSession('B'); await tab2.loadSession('B'); await tick();
    server.holdFrames = true;
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'POST' && p === '/api/session/yolo';
    const cmd = tab1.cmdYolo(); await tick();
    const enable = tab1.held.shift();
    check(enable && enable.answer.yolo_enabled, 'precondition: tab1 enable answer (ON) held', tab1.calls);
    server.shouldHold = null;
    await tab2.cmdYolo(); await tick();
    check(server.yolo.B === false, 'precondition: tab2 disabled B', server.state('B'));
    server.shouldHold = yoloGet('B');
    const read = vm.runInContext('_fetchYoloState("B")', tab1.ctx); await tick();
    const newer = tab1.held.shift();
    check(newer && !newer.answer.yolo_enabled, 'precondition: tab1 read (OFF, newer) held', tab1.calls);
    server.shouldHold = null;
    enable.release(); await cmd; await tick();
    newer.release(); await read; await tick();
    const mid = tab1.state();
    server.holdFrames = false; server.releaseFrames(); await tick();
    const out = { mid, after: tab1.state(), server: server.state('B') };
    check(!mid.pillVisible && synced(tab1, server, 'B'), 'the newer read was dropped after an older /yolo answer: pill shown while OFF', out);
    return out;
  },

  // (r2c) r2 with /yolo: its older answer never overrides a newer frame.
  async r2c_older_yolo_answer_never_overrides_newer_frame() {
    const server = createServer({ B: {} });
    const tab1 = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab1.loadSession('B'); await tab2.loadSession('B'); await tick();
    await tab1.cmdYolo(); await tick();
    check(tab1.state().pillVisible && server.yolo.B === true, 'precondition: tab1 enabled B', tab1.state());
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'POST' && p === '/api/session/yolo';
    const cmd = tab1.cmdYolo(); await tick();
    check(tab1.held.length === 1 && server.yolo.B === false, 'precondition: tab1 /yolo disable applied, answer in flight', tab1.calls);
    server.shouldHold = null;
    await tab2.cmdYolo(); await tick();
    check(server.yolo.B === true, 'precondition: tab2 re-enabled B', server.state('B'));
    tab1.held.shift().release(); await cmd; await tick();
    const out = { tab1: tab1.state(), server: server.state('B') };
    check(synced(tab1, server, 'B'), 'older /yolo answer hid the pill while the server is ON', out);
    return out;
  },

  // (r2d) an older `yolo-changed` frame (another tab's enable, delivered
  // late) never overrides a newer read.
  async r2d_older_frame_never_overrides_newer_read() {
    const server = createServer({ B: {} });
    const tab1 = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab1.loadSession('B'); await tab2.loadSession('B'); await tick();
    server.holdFrames = true;
    await tab2.cmdYolo(); await tick();
    await tab2.cmdYolo(); await tick();
    check(server.yolo.B === false && server.frames.length === 4, 'precondition: enable and disable frames held', server.frames.length);
    await vm.runInContext('_fetchYoloState("B")', tab1.ctx); await tick();
    const frames = server.frames.splice(0); server.holdFrames = false;
    frames.slice(0, 2).forEach(f => f()); await tick();
    const mid = tab1.state();
    frames.slice(2).forEach(f => f()); await tick();
    const out = { mid, after: tab1.state(), server: server.state('B') };
    check(!mid.pillVisible && synced(tab1, server, 'B'), 'an older ON frame showed the pill after a newer OFF read', out);
    return out;
  },

  // (r2e) an older approval-card YOLO answer never overrides a newer read.
  async r2e_older_approval_answer_never_overrides_newer_read() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1', approvalStubs, approvalSrc('B'));
    await tab.loadSession('B'); await tick();
    tab.ctx.$('approvalCard').classList.add('visible');
    server.yolo.B = true; server.snapshot('B');
    const handle = server.handle;
    server.handle = (method, p, body) => {
      if (method === 'POST' && p === '/api/approval/respond') {
        const payload = { ok: false, error: 'relay failed', ...server.state('B') };
        return { then(_ok, fail) { fail(Object.assign(new Error('relay failed'), { status: 502, body: JSON.stringify(payload) })); } };
      }
      return handle(method, p, body);
    };
    server.shouldHold = (name, method, p) => p === '/api/approval/respond';
    const approval = vm.runInContext('respondApproval("once",{yolo:true})', tab.ctx); await tick();
    const older = tab.held.shift();
    check(!!older, 'precondition: approval answer (ON) held', tab.calls);
    server.shouldHold = null;
    server.yolo.B = false;
    await vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    check(!tab.state().pillVisible, 'precondition: newer OFF read shown', tab.state());
    older.release();
    try { await approval; } catch (_) { /* the card's own error path */ }
    await tick();
    const out = { after: tab.state(), server: server.state('B') };
    check(synced(tab, server, 'B'), 'an older approval ON answer showed the pill after a newer OFF read', out);
    return out;
  },

  // (r3) KNOWN GAP, as on master (follow-up issue): a server restart clears
  // YOLO: the reconnecting session stream's snapshot (new epoch, revision 0)
  // should replace the stale "on" view.
  async r3_restart_snapshot_clears_stale_on_pill() {
    const server = createServer({ B: {} });
    const tab = streamTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.B === true, 'precondition: pill on', tab.state());
    server.restart();
    await tick();
    const out = { state: tab.state(), server: { ...server.yolo }, epoch: server.epoch };
    check(!out.state.pillVisible, 'after a server restart the pill still shows YOLO on', out);
    return out;
  },

  // (r5) KNOWN GAP, as on master (follow-up issue): a failed read on install
  // should not hide an ON flag: the session stream's `initial` snapshot would
  // show it.
  async r5_failed_install_read_keeps_on_pill_via_snapshot() {
    const server = createServer({ B: {} });
    server.yolo.B = true;
    server.failGet = true;
    const tab = streamTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    const out = { state: tab.state(), calls: tab.calls.map(c => c.method + ' ' + c.path) };
    check(out.state.pillVisible === true, 'server yolo[B]=true but the failed read left the pill hidden', out);
    return out;
  },

  // (r5b) the "hide YOLO" composer setting must not hide the pill while YOLO
  // is on; while it is off the setting still hides the control.
  async r5b_hide_setting_keeps_on_pill_visible() {
    const server = createServer({ B: {} });
    server.yolo.B = true;
    const tab = createTab(server, 'tab1', (base) => { base._composerControlVisibility = { hide_composer_yolo: true }; }, bootVisibilitySrc());
    const apply = () => vm.runInContext('_applyComposerFooterVisibilitySettings', tab.ctx)();
    apply();
    await tab.loadSession('B');
    await tick();
    const afterLoad = tab.state();
    apply();
    const afterReapply = tab.state();
    const ariaHiddenWhenOn = tab.ctx.$('yoloPill').getAttribute('aria-hidden');
    await tab.cmdYolo();
    await tick();
    const pill = tab.ctx.$('yoloPill');
    const out = { afterLoad, afterReapply, ariaHiddenWhenOn, afterDisable: tab.state(), hiddenClassWhenOff: pill.classList.contains('composer-control-hidden'),
      ariaHiddenWhenOff: pill.getAttribute('aria-hidden') };
    check(afterLoad.pillVisible && afterReapply.pillVisible, 'hide_composer_yolo hid the pill while YOLO is on', out);
    check(ariaHiddenWhenOn !== 'true', 'the ON pill is shown but still aria-hidden', out);
    check(!out.afterDisable.pillVisible && out.hiddenClassWhenOff && out.ariaHiddenWhenOff === 'true', 'with YOLO off the setting no longer hides the control', out);
    return out;
  },

  // (r5c) design review: with YOLO on, the hide setting shrinks the pill to
  // its ⚡ icon (`yolo-pill-compact`; CSS hides the label) instead of doing
  // nothing; without the setting, or with YOLO off, the pill is not compact.
  // The ON pill always carries an explicit accessible name (the localized
  // tooltip), so the compact pill does not depend on its hidden label text.
  async r5c_hide_setting_shrinks_on_pill_to_icon() {
    const server = createServer({ B: {} });
    server.yolo.B = true;
    const vis = { hide_composer_yolo: true };
    const tab = createTab(server, 'tab1', (base) => { base._composerControlVisibility = vis; }, bootVisibilitySrc());
    const apply = () => vm.runInContext('_applyComposerFooterVisibilitySettings', tab.ctx)();
    const pill = tab.ctx.$('yoloPill');
    const look = () => ({ visible: tab.state().pillVisible, compact: pill.classList.contains('yolo-pill-compact'),
      ariaLabel: pill.getAttribute('aria-label'), ariaKey: pill.getAttribute('data-i18n-aria-label'), title: pill.title });
    apply();
    await tab.loadSession('B');
    await tick();
    const onWithSetting = look();
    vis.hide_composer_yolo = false;
    apply();
    const onWithoutSetting = look();
    vis.hide_composer_yolo = true;
    apply();
    const settingBackOn = look();
    await tab.cmdYolo();
    await tick();
    const offWithSetting = look();
    const out = { onWithSetting, onWithoutSetting, settingBackOn, offWithSetting, server_B: server.yolo.B };
    check(onWithSetting.visible && onWithSetting.compact, 'YOLO on + hide setting: the pill is not the compact ⚡ pill', out);
    check(onWithoutSetting.visible && !onWithoutSetting.compact, 'YOLO on without the hide setting: the pill is still compact', out);
    check(settingBackOn.visible && settingBackOn.compact, 'turning the hide setting back on did not shrink the ON pill', out);
    check(!offWithSetting.visible && !offWithSetting.compact && server.yolo.B === false, 'YOLO off: the hidden pill keeps the compact class', out);
    for (const s of [onWithSetting, onWithoutSetting, settingBackOn]) {
      check(s.ariaLabel === 'yolo_pill_title_active' && s.ariaKey === 'yolo_pill_title_active',
        'the ON pill has no explicit accessible name (aria-label from yolo_pill_title_active)', out);
      check(s.title === 'yolo_pill_title_active', 'the ON pill lost its tooltip', out);
    }
    return out;
  },

  // (r7) a /yolo whose status read is answered "off", then YOLO is turned on
  // and disabled with the pill (another tab), must not post an enable.
  // KNOWN GAP, as on master (follow-up issue): /yolo is master's code and
  // posts unconditionally, so the stale enable lands (server and pill ON).
  // The server's expected_revision/expected_epoch 409 is ready for it.
  async r7_stale_yolo_read_cannot_undo_later_pill_disable() {
    const server = createServer({ B: {} });
    const tab1 = createTab(server, 'tab1');
    const tab2 = createTab(server, 'tab2');
    await tab1.loadSession('B');
    await tab2.loadSession('B');
    await tick();
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'GET' && p === '/api/session/yolo?session_id=B';
    const stale = tab1.cmdYolo();
    await tick();
    check(tab1.held.length === 1, 'precondition: tab1 /yolo read answered "off", delivery held', tab1.calls);
    server.shouldHold = null;
    await tab2.cmdYolo();
    await tick();
    await tab2.clickPill();
    await tick();
    check(server.yolo.B === false, 'precondition: tab2 enabled then disabled with the pill', { ...server.yolo });
    tab1.held.shift().release();
    await stale;
    await tick();
    const out = { server: { ...server.yolo }, tab1: tab1.state(), tab1_posts: posts(tab1), toasts: tab1.toasts };
    check(server.yolo.B === false && !out.tab1.pillVisible && tab1.toasts.some(m => String(m).startsWith('YOLO: ')),
      `stale /yolo read re-enabled B after the pill disable: ${JSON.stringify(out)}`, out);
    return out;
  },

  // (r8) an answer requested BEFORE a server restart (old epoch) that lands
  // after the new epoch reached the view over a stream (the reconnect's
  // `initial` snapshot, another tab's `yolo-changed` frame) must not override
  // it. KNOWN GAP, as on master (follow-up issue): the client applies no
  // frames, so the late read wins.
  async r8_old_epoch_answer_never_overrides_new_epoch() {
    const out = {};
    const fetchB = tab => vm.runInContext('_fetchYoloState("B")', tab.ctx);
    // (1) late OFF read; new epoch ON via the reconnect's `initial` snapshot.
    {
      const server = createServer({ B: {} });
      const tab = streamTab(server, 'tab1');
      await tab.loadSession('B'); await tick();
      server.shouldHold = yoloGet('B');
      const late = fetchB(tab); await tick();
      check(tab.held.length === 1, 'precondition: old-epoch read held', tab.calls);
      server.shouldHold = null;
      server.restart(); server.yolo.B = true; await tick();
      const before = tab.state();
      tab.held.shift().release(); await late; await tick();
      out.initial = { before, after: tab.state(), server: server.state('B') };
      check(before.pillVisible && synced(tab, server, 'B'), 'old-epoch OFF read hid the new-epoch ON pill (initial snapshot)', out);
    }
    // (2) late OFF read; new epoch ON via another tab's `yolo-changed` frame.
    {
      const server = createServer({ B: {} });
      const tab1 = streamTab(server, 'tab1');
      const tab2 = streamTab(server, 'tab2');
      await tab1.loadSession('B'); await tab2.loadSession('B'); await tick();
      server.shouldHold = (name, method, p) => name === 'tab1' && method === 'GET' && p === '/api/session/yolo?session_id=B';
      const late = fetchB(tab1); await tick();
      server.shouldHold = null;
      server.restart(); await tick();
      await tab2.cmdYolo(); await tick();
      const before = tab1.state();
      tab1.held.shift().release(); await late; await tick();
      out.frame = { before, after: tab1.state(), server: server.state('B') };
      check(before.pillVisible && synced(tab1, server, 'B'), 'old-epoch OFF read hid the new-epoch ON pill (yolo-changed)', out);
    }
    // (3) late ON read; the restart's OFF arrives via `initial`. Reads are held
    // from then on, so only the late answer itself can move the view.
    {
      const server = createServer({ B: {} });
      server.yolo.B = true;
      const tab = streamTab(server, 'tab1');
      await tab.loadSession('B'); await tick();
      server.shouldHold = yoloGet('B');
      const late = fetchB(tab); await tick();
      const lateOn = tab.held.shift();
      check(!!lateOn && lateOn.answer.yolo_enabled, 'precondition: old-epoch ON read held', tab.calls);
      server.restart(); await tick();
      const before = tab.state();
      const readsBefore = yoloReads(tab, 'B');
      lateOn.release(); await late; await tick();
      out.lateOn = { before, after: tab.state(), extraReads: yoloReads(tab, 'B') - readsBefore, server: server.state('B') };
      check(!before.pillVisible && synced(tab, server, 'B'), 'old-epoch ON read showed the pill after the restart snapshot cleared YOLO', out);
      check(out.lateOn.extraReads === 0, `expected no extra read, saw ${out.lateOn.extraReads}`, out);
    }
    return out;
  },

  // (r8c) KNOWN GAP, as on master b1935597c040 (follow-up issue): a late
  // old-epoch OFF read lands after a LATER read of the same tab showed the new
  // epoch's ON. Both are reads with the same read sequence and no revision
  // orders two epochs, so delivery order decides: the pill hides while ON.
  async r8c_old_epoch_read_after_newer_read_never_hides_on() {
    const out = {};
    const fetchB = tab => vm.runInContext('_fetchYoloState("B")', tab.ctx);
    {
      const server = createServer({ B: {} });
      const tab1 = createTab(server, 'tab1');
      const tab2 = createTab(server, 'tab2');
      await tab1.loadSession('B'); await tab2.loadSession('B'); await tick();
      server.shouldHold = (name, method, p) => name === 'tab1' && method === 'GET' && p === '/api/session/yolo?session_id=B';
      const late = fetchB(tab1); await tick();
      server.shouldHold = null;
      server.restart(); await tick();
      await tab2.cmdYolo(); await tick();
      await fetchB(tab1); await tick();
      const before = tab1.state();
      tab1.held.shift().release(); await late; await tick();
      out.read = { before, after: tab1.state(), server: server.state('B') };
      check(before.pillVisible && synced(tab1, server, 'B'), 'old-epoch OFF read hid the new-epoch ON pill (later read)', out);
    }
    return out;
  },

  // (r8d) KNOWN GAP, as on master (follow-up issue): a /yolo POST answered ON
  // by the old process lands after the restart cleared YOLO and is applied:
  // the pill shows ON while the server is OFF (its click only disables, #9116).
  async r8d_old_epoch_post_answer_never_shows_on_after_restart() {
    const out = {};
    {
      const server = createServer({ B: {} });
      const tab = streamTab(server, 'tab1');
      await tab.loadSession('B'); await tick();
      server.shouldHold = (name, method, p) => method === 'POST' && p === '/api/session/yolo';
      const cmd = tab.cmdYolo(); await tick();
      check(tab.held.length === 1 && server.yolo.B === true, 'precondition: old-epoch enable answered, held', tab.calls);
      server.shouldHold = null;
      server.restart(); await tick();
      const before = tab.state();
      tab.held.shift().release(); await cmd; await tick();
      out.post = { before, after: tab.state(), server: server.state('B') };
      check(!before.pillVisible && synced(tab, server, 'B'), 'old-epoch ON answer showed the pill after the restart cleared YOLO', out);
    }
    return out;
  },

  // (r8e) a read sent before a restart AND before a same-session refresh lands
  // after the refresh's read showed the new epoch: it belongs to an older load
  // generation and is dropped (the pre-#9101 generation rule).
  async r8e_old_epoch_read_from_an_older_load_is_dropped() {
    const server = createServer({ B: {} });
    server.yolo.B = true;
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B'); await tick();
    server.shouldHold = yoloGet('B');
    const late = vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    const lateOn = tab.held.shift();
    check(!!lateOn && lateOn.answer.yolo_enabled, 'precondition: old-epoch ON read held', tab.calls);
    server.shouldHold = null;
    server.restart();
    await tab.loadSession('B', { force: true, externalRefreshReason: 'session-updated', keepStaleUntilLoaded: true }); await tick();
    const before = tab.state();
    check(before.gen === 2, 'precondition: the refresh started a newer load', before);
    lateOn.release(); await late; await tick();
    const out = { before, after: tab.state(), server: server.state('B') };
    check(!before.pillVisible && synced(tab, server, 'B'), 'an older load\'s old-epoch ON read showed the pill after the refresh read OFF', out);
    return out;
  },

  // (r8b) counterpart: an answer requested AFTER a restart (new epoch) is
  // accepted over the stale pre-restart view, for a read and a pill click.
  async r8b_post_restart_answer_is_accepted() {
    const out = {};
    {
      const server = createServer({ B: {} });
      const tab = createTab(server, 'tab1');
      await tab.loadSession('B'); await tick();
      await tab.cmdYolo(); await tick();
      check(tab.state().pillVisible, 'precondition: pill on (old epoch)', tab.state());
      server.restart(); await tick();
      const stale = tab.state();
      await vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
      out.read = { stale, after: tab.state(), server: server.state('B') };
      check(stale.pillVisible && !out.read.after.pillVisible, 'a post-restart read (new epoch, OFF) was not applied', out);
    }
    {
      const server = createServer({ B: {} });
      const tab = createTab(server, 'tab1');
      await tab.loadSession('B'); await tick();
      await tab.cmdYolo(); await tick();
      server.restart(); server.yolo.B = true; await tick();
      server.yolo.B = true;
      await tab.clickPill(); await tick();
      out.pill = { after: tab.state(), server: server.state('B'), posts: posts(tab) };
      check(!out.pill.after.pillVisible && server.yolo.B === false, 'a post-restart pill disable answer (new epoch) was not applied', out);
    }
    return out;
  },

  // (r9) first sync overlapping a restart: the new process's ON lands last
  // and is shown, without any extra read.
  async r9_first_sync_overlap_shows_the_last_answer() {
    return firstSyncOverlap(true);
  },

  // (r9c) the mirror: the new process's OFF lands last and is applied.
  async r9c_first_sync_overlap_applies_the_last_off() {
    return firstSyncOverlap(false);
  },

  // (r9b) KNOWN GAP, as on master (follow-up issue): an approval-card YOLO
  // answer requested before a restart (old epoch, OFF) that lands after the
  // view holds the new epoch's ON should be dropped; it is applied.
  async r9b_old_epoch_approval_answer_after_new_epoch_is_dropped() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1', approvalStubs, approvalSrc('B'));
    await tab.loadSession('B'); await tick();
    tab.ctx.$('approvalCard').classList.add('visible');
    const handle = server.handle;
    server.handle = (method, p, body) => {
      if (method === 'POST' && p === '/api/approval/respond') {
        // A thenable, so the held rejection is not unhandled while held.
        const payload = { ok: false, error: 'relay failed', ...server.state('B') };
        return { then(_ok, fail) { fail(Object.assign(new Error('relay failed'), { status: 502, body: JSON.stringify(payload) })); } };
      }
      return handle(method, p, body);
    };
    server.shouldHold = (name, method, p) => p === '/api/approval/respond';
    const approval = vm.runInContext('respondApproval("once",{yolo:true})', tab.ctx); await tick();
    const old = tab.held.shift();
    check(!!old, 'precondition: old-epoch approval answer held', tab.calls);
    server.shouldHold = null;
    server.restart(); server.yolo.B = true;
    await vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    const before = tab.state();
    check(before.pillVisible, 'precondition: new-epoch ON shown', before);
    old.release();
    try { await approval; } catch (_) { /* the card's own error path */ }
    await tick();
    const out = { before, after: tab.state(), server: server.state('B') };
    check(out.after.pillVisible && synced(tab, server, 'B'), 'old-epoch approval OFF answer hid the new-epoch ON pill', out);
    return out;
  },

  // (r11) 20 late old-epoch reads that land after the view holds the new
  // epoch's ON cause no extra read (where they leave the view is r8c's gap).
  async r11_late_old_epoch_answers_cause_no_reads() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    const fetchB = () => vm.runInContext('_fetchYoloState("B")', tab.ctx);
    await tab.loadSession('B'); await tick();
    server.shouldHold = yoloGet('B');
    for (let i = 0; i < 20; i++) fetchB();
    await tick();
    const late = tab.held.splice(0);
    check(late.length === 20, 'precondition: 20 old-epoch reads held', tab.calls.length);
    server.restart(); server.yolo.B = true;
    server.shouldHold = null;
    await fetchB(); await tick();
    check(tab.state().pillVisible, 'precondition: the new epoch ON is shown', tab.state());
    const base = yoloReads(tab, 'B');
    for (const r of late) { r.release(); await tick(2); }
    await tick(40);
    const out = { extraReads: yoloReads(tab, 'B') - base, after: tab.state(), server: server.state('B') };
    check(out.extraReads === 0, `20 late answers caused ${out.extraReads} extra reads`, out);
    return out;
  },

  // (r11c) KNOWN GAP, as on master (follow-up issue): two late answers from
  // ONE old process, its OFF (newer revision) then its ON (older revision),
  // land after the new run's OFF. Delivery order (master) shows the stale ON
  // (r14 is the same delivery with the new run ON, which delivery order gets
  // right; the old run's revision order gets r14 wrong). Expected: OFF, no
  // extra read.
  async r11c_old_process_answers_keep_their_revision_order() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    const fetchB = () => vm.runInContext('_fetchYoloState("B")', tab.ctx);
    server.yolo.B = true;
    await tab.loadSession('B'); await tick();
    server.shouldHold = yoloGet('B');
    fetchB(); server.yolo.B = false; fetchB(); await tick();
    const [lateOn, lateOff] = tab.held.splice(0);
    check(lateOn && lateOff && lateOn.answer.yolo_enabled && !lateOff.answer.yolo_enabled, 'precondition: old-epoch ON and OFF reads held', tab.calls);
    server.restart();
    server.shouldHold = null;
    await fetchB(); await tick();
    check(!tab.state().pillVisible, 'precondition: the new epoch OFF is shown', tab.state());
    const base = yoloReads(tab, 'B');
    lateOff.release(); await tick();
    lateOn.release(); await tick(40);
    const out = { extraReads: yoloReads(tab, 'B') - base, after: tab.state(), server: server.state('B') };
    check(!out.after.pillVisible && synced(tab, server, 'B'), "the old process's older ON was shown after its newer OFF", out);
    check(out.extraReads === 0, `expected no extra read, saw ${out.extraReads}`, out);
    return out;
  },

  // (r12) KNOWN GAP, as on master (follow-up issue; review round 4's probe):
  // the view shows the new epoch's ON; a late old-epoch ON lands, then a read
  // of the new epoch's OFF (sent before the restart turned YOLO back on)
  // lands last: the pill hides while the server is ON. Reads fail meanwhile.
  async r12_late_old_on_then_stale_new_off_never_hides_on() {
    const server = createServer({ B: {} });
    server.yolo.B = true;
    const tab = createTab(server, 'tab1');
    const fetchB = () => vm.runInContext('_fetchYoloState("B")', tab.ctx);
    await tab.loadSession('B'); await tick();
    server.shouldHold = yoloGet('B');
    fetchB(); await tick();
    const oldOn = tab.held.shift();
    server.restart();
    fetchB(); await tick();
    const staleOff = tab.held.shift();
    check(!!oldOn && !!staleOff, 'precondition: old ON and new OFF reads held', tab.calls);
    server.yolo.B = true;
    server.shouldHold = null;
    await fetchB(); await tick();
    const before = tab.state();
    server.failGet = true;
    oldOn.release(); await tick();
    staleOff.release(); await tick(40);
    const out = { before, after: tab.state(), server: server.state('B') };
    check(before.pillVisible && synced(tab, server, 'B'), 'the pill hid while the server is ON', out);
    return out;
  },

  // (r13) review round 5: a /yolo POST answered ON by the old process and a
  // later old-process read (OFF) are both held over a restart; the new run's
  // ON is shown, then the POST lands, then the read. Master's read-sequence
  // rule drops the read (the POST answer was applied after it was sent) and
  // ends ON, synced; the old run's revision order would apply it.
  async r13_late_old_post_then_old_newer_read() {
    const server = createServer({ B: {} });
    const tab = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab.loadSession('B'); await tab2.loadSession('B'); await tick();
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'POST' && p === '/api/session/yolo';
    const pendingCmd = tab.cmdYolo(); await tick();
    const oldPostOn = tab.held.shift();
    check(oldPostOn && oldPostOn.answer.yolo_enabled, 'precondition: old enable answered ON held', tab.calls);
    server.shouldHold = null;
    await tab2.cmdYolo(); await tick();
    check(server.yolo.B === false, 'precondition: other tab disabled old process', server.state('B'));
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'GET' && p === '/api/session/yolo?session_id=B';
    const pendingRead = vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    const oldReadOff = tab.held.shift();
    check(oldReadOff && !oldReadOff.answer.yolo_enabled, 'precondition: old later read answered OFF held', tab.calls);
    server.shouldHold = null;
    server.restart(); await tab2.cmdYolo(); await tick();
    await vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    const before = tab.state();
    check(server.yolo.B && before.pillVisible, 'precondition: new process ON shown', before);
    oldPostOn.release(); await pendingCmd; await tick();
    const afterOldPost = tab.state();
    oldReadOff.release(); await pendingRead; await tick();
    const out = { before, afterOldPost, after: tab.state(), server: server.state('B'), oldPost: oldPostOn.answer, oldRead: oldReadOff.answer };
    check(synced(tab, server, 'B'), 'late old-process read hid pill after old-process POST switched epoch back', out);
    return out;
  },

  // (r14) review round 5, read-only mirror of r11c: two old-process reads (ON
  // rev n, then OFF rev n+1) held over a restart; the new run is ON. The OFF
  // lands, then the ON. Delivery order (master) ends ON, synced with the
  // server; the old run's revision order would drop the ON.
  async r14_old_reads_revision_order_mirror() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    const fetchB = () => vm.runInContext('_fetchYoloState("B")', tab.ctx);
    server.yolo.B = true;
    await tab.loadSession('B'); await tick();
    server.shouldHold = yoloGet('B');
    const pendingOn = fetchB(); server.yolo.B = false; const pendingOff = fetchB(); await tick();
    const [lateOn, lateOff] = tab.held.splice(0);
    check(lateOn && lateOff && lateOn.answer.yolo_enabled && !lateOff.answer.yolo_enabled, 'precondition: old ON then OFF reads held', tab.calls);
    server.restart(); server.yolo.B = true;
    server.shouldHold = null;
    await fetchB(); await tick();
    const before = tab.state();
    check(before.pillVisible, 'precondition: current server ON shown', before);
    lateOff.release(); await pendingOff; await tick();
    const afterOff = tab.state();
    lateOn.release(); await pendingOn; await tick();
    const out = { before, afterOff, after: tab.state(), server: server.state('B'), lateOn: lateOn.answer, lateOff: lateOff.answer };
    check(synced(tab, server, 'B'), 'old-process revision check rejected final ON and left pill hidden with current server ON', out);
    return out;
  },

  // (r15) the writes-only variant of r13: an old-process /yolo enable answer
  // (rev n) and a later pill-disable answer (rev n+1) are held over a restart;
  // the new run is ON. The disable lands first, then the enable. Delivery
  // order (master) ends ON, synced with the server; the old run's revision
  // order would drop the enable.
  async r15_late_old_writes_after_epoch_change() {
    const server = createServer({ B: {} });
    const tab = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab.loadSession('B'); await tab2.loadSession('B'); await tick();
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'POST' && p === '/api/session/yolo';
    const cmd = tab.cmdYolo(); await tick();
    const oldEnable = tab.held.shift();
    await vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    check(oldEnable && oldEnable.answer.yolo_enabled && tab.state().pillVisible, 'precondition: old enable (ON) held, pill shown', tab.calls);
    const click = tab.clickPill(); await tick();
    const oldDisable = tab.held.shift();
    check(oldDisable && !oldDisable.answer.yolo_enabled && oldDisable.answer.yolo_revision > oldEnable.answer.yolo_revision,
      'precondition: old pill disable (OFF, newer revision) held', tab.calls);
    server.shouldHold = null;
    server.restart(); await tab2.cmdYolo(); await tick();
    await vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    const before = tab.state();
    check(server.yolo.B && before.pillVisible, 'precondition: new process ON shown', before);
    oldDisable.release(); await click; await tick();
    const afterDisable = tab.state();
    oldEnable.release(); await cmd; await tick();
    const out = { before, afterDisable, after: tab.state(), server: server.state('B'), oldEnable: oldEnable.answer, oldDisable: oldDisable.answer };
    check(synced(tab, server, 'B'), "the old run's revision order dropped its late enable: pill hidden while the server is ON", out);
    return out;
  },

  // (r16) review round 6 (Codex, extended_probe.cjs, copied verbatim): an
  // UNOBSERVED restart. tab1's session stream is reconnecting, so the tab never
  // sees the new run's snapshot. An old-run enable answer (rev 1) and a later
  // old-run OFF read (rev 2) are held; the server restarts and another tab
  // enables YOLO; both land. Master's read-sequence rule drops the read (the
  // enable was applied after it was sent) and ends ON, synced with the server.
  async r16_unobserved_restart_old_post_then_old_newer_read() {
    const server = createServer({ B: {} });
    const tab = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab.loadSession('B'); await tab2.loadSession('B'); await tick();
    // A real EventSource error with CONNECTING (0) waits for native retry.
    tab.sources[0].readyState = 0; tab.sources[0].onerror();
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'POST' && p === '/api/session/yolo';
    const cmd = tab.cmdYolo(); await tick();
    const oldEnable = tab.held.shift();
    check(oldEnable && oldEnable.answer.yolo_enabled, 'old enable ON held', tab.calls);
    server.shouldHold = null;
    await tab2.cmdYolo(); await tick();
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'GET' && p === '/api/session/yolo?session_id=B';
    const read = vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    const oldRead = tab.held.shift();
    check(oldRead && !oldRead.answer.yolo_enabled && oldRead.answer.yolo_revision > oldEnable.answer.yolo_revision, 'old newer OFF held', tab.calls);
    server.shouldHold = null;
    server.restart(); await tab2.cmdYolo(); await tick();
    const before = tab.state();
    oldEnable.release(); await cmd; await tick();
    const afterPost = tab.state();
    oldRead.release(); await read; await tick();
    const out = {before, afterPost, after: tab.state(), server: server.state('B'), oldEnable:oldEnable.answer, oldRead:oldRead.answer};
    check(synced(tab, server, 'B'), 'unobserved restart: late old OFF read hid pill while current process ON', out);
    return out;
  },

  // (r17) review round 6 (Codex), the writes-only variant of r16: an old-run
  // /yolo enable and a later old-run pill disable are held over an unobserved
  // restart, the new run is ON; the disable lands first, then the enable.
  // Master's delivery order ends ON, synced with the server.
  async r17_unobserved_restart_old_writes() {
    const server = createServer({ B: {} });
    const tab = streamTab(server, 'tab1');
    const tab2 = streamTab(server, 'tab2');
    await tab.loadSession('B'); await tab2.loadSession('B'); await tick();
    tab.sources[0].readyState = 0; tab.sources[0].onerror();
    server.shouldHold = (name, method, p) => name === 'tab1' && method === 'POST' && p === '/api/session/yolo';
    const cmd = tab.cmdYolo(); await tick();
    const oldEnable = tab.held.shift();
    await vm.runInContext('_fetchYoloState("B")', tab.ctx); await tick();
    const click = tab.clickPill(); await tick();
    const oldDisable = tab.held.shift();
    check(oldEnable && oldDisable && oldEnable.answer.yolo_enabled && !oldDisable.answer.yolo_enabled, 'old write answers held', tab.calls);
    server.shouldHold = null;
    server.restart(); await tab2.cmdYolo(); await tick();
    const before = tab.state();
    oldDisable.release(); await click; await tick();
    const afterDisable = tab.state();
    oldEnable.release(); await cmd; await tick();
    const out = {before, afterDisable, after: tab.state(), server: server.state('B'), oldEnable:oldEnable.answer, oldDisable:oldDisable.answer};
    check(synced(tab, server, 'B'), 'unobserved restart: old revision order rejected final ON and left pill hidden', out);
    return out;
  },

  // (r21) review round 8 (Codex `older-reads`): tab1's /yolo reads OFF and two
  // ordinary tab1 reads (send, /goal) answered OFF are held; another client
  // enables YOLO; one old read lands before the /yolo POST answer, the other
  // after it (during any recovery read). YOLO is ON on the server, so the
  // pill must end visible. Master's /yolo posts enable (already ON) and shows
  // its answer; the old reads are older than that write and are dropped.
  async r21_older_off_reads_around_yolo_never_hide_its_on() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    // A held 409 (if any) is a rejected promise until released: mark it handled.
    const handle = server.handle;
    server.handle = (m, p, b) => { const a = handle(m, p, b); if (a && a.catch) a.catch(() => {}); return a; };
    const fetchB = () => vm.runInContext('_fetchYoloState("B")', tab.ctx);
    await tab.loadSession('B'); await tick();
    server.shouldHold = (name, m, p) => name === 'tab1' && m === 'GET' && p === '/api/session/yolo?session_id=B';
    const cmd = tab.cmdYolo(); await tick();
    const status = tab.held.shift();
    const read1 = fetchB(); const read2 = fetchB(); await tick();
    const [old1, old2] = tab.held.splice(0);
    check(status && old1 && old2 && !old1.answer.yolo_enabled && !old2.answer.yolo_enabled, 'precondition: all old reads OFF', tab.calls);
    server.shouldHold = null;
    await server.handle('POST', '/api/session/yolo', { session_id: 'B', enabled: true });
    server.shouldHold = (name, m, p) => name === 'tab1' && p.startsWith('/api/session/yolo');
    status.release(); await tick();
    const response = tab.held.shift();
    check(response && response.method === 'POST', 'precondition: POST answer held', tab.calls);
    old1.release(); await read1; await tick();
    const beforeAnswer = tab.state();
    response.release(); await cmd; await tick();
    const afterAnswer = tab.state();
    const recovery = tab.held.shift();
    check(!recovery || recovery.answer.yolo_enabled, 'precondition: a recovery read, if any, answered ON', recovery);
    old2.release(); await read2; await tick();
    const afterOld2 = tab.state();
    if (recovery) { recovery.release(); await tick(); }
    const out = { beforeAnswer, afterAnswer, afterOld2, after: tab.state(), server: server.state('B'),
      recovery: recovery && recovery.answer, posts: posts(tab), toasts: tab.toasts };
    check(synced(tab, server, 'B'), 'older OFF reads around /yolo hid the pill while the server is ON', out);
    return out;
  },

  // Control: a streaming load's read shows the session's own state (proves
  // the harness drives the real read path end to end).
  async control_streaming_load_shows_own_state() {
    const server = createServer({ A: { active_stream_id: 'stream-a' } });
    server.yolo.A = true;
    const tab = createTab(server, 'tab1');
    await tab.loadSession('A');
    await tick();
    const st = tab.state();
    check(st.sid === 'A' && st.pillVisible === true, 'control: streaming A should show its own YOLO=on', st);
    return { state: st };
  },

  // (a) A's late read paints A's state onto B; the "click to disable" pill
  // then enables YOLO for B.
  async a_late_cross_session_read_then_pill_click_enables_B() {
    const server = createServer({ A: { active_stream_id: 'stream-a' }, B: {} });
    server.yolo.A = true;
    server.shouldHold = yoloGet('A');
    const tab = createTab(server, 'tab1');
    await tab.loadSession('A');
    check(tab.held.length === 1, 'precondition: A YOLO read is in flight', tab.calls);
    await tab.loadSession('B');
    tab.held.shift().release();
    await tick();
    const st = tab.state();
    const out = { pill_onclick: tab.pillOnclick, after_switch: st, server: { ...server.yolo } };
    if (st.pillVisible) {
      await tab.clickPill();
      await tick();
      out.after_click = { state: tab.state(), server: { ...server.yolo }, posts: posts(tab) };
    }
    check(!st.pillVisible && st.yolo === false && !server.yolo.B,
      `B (server yolo[B]=false) shows A's late YOLO state: pill visible=${st.pillVisible}; click => server yolo[B]=${!!server.yolo.B}`, out);
    return out;
  },

  // (a2) streaming B with B's approval card visible: A's late answer lands
  // after B's own; the pill click must not approve B's displayed command.
  async a2_late_read_then_pill_click_approves_B_visible_card() {
    const server = createServer({ A: { active_stream_id: 'stream-a' }, B: { active_stream_id: 'stream-b' } });
    server.yolo.A = true;
    server.answered = [];
    server.shouldHold = yoloGet('A');
    const tab = createTab(server, 'tab1', visibleCardOverrides(server, 'B'));
    await tab.loadSession('A');
    await tab.loadSession('B');
    await tick();
    tab.ctx.$('approvalCard').classList.add('visible');
    tab.held.shift().release();
    await tick();
    const st = tab.state();
    const out = { after_late_answer: st, server_before_click: { ...server.yolo } };
    if (st.pillVisible) {
      await tab.clickPill();
      await tick();
      out.after_click = { server: { ...server.yolo }, approvals_answered: server.answered, posts: posts(tab) };
    }
    check(!st.pillVisible && server.answered.length === 0 && !server.yolo.B,
      `B shows A's YOLO pill (visible=${st.pillVisible}); click answered ${JSON.stringify(server.answered)}`, out);
    return out;
  },

  // (e) any stale-ON pill (here: a server restart cleared the in-memory flag)
  // must not turn the pill's "disable" click into an enable.
  async e_stale_on_pill_click_after_server_restart_enables() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.B === true, 'precondition: pill visible after /yolo enable', tab.state());
    server.yolo = {};
    const before = posts(tab).length;
    await tab.clickPill();
    await tick();
    const after = posts(tab).slice(before);
    const out = { posts_from_click: after, server: { ...server.yolo }, state: tab.state(), toasts: tab.toasts };
    check(!server.yolo.B && after.length > 0 && after.every(b => b.session_id === 'B' && b.enabled === false),
      `pill click (intent: disable) sent ${JSON.stringify(after)} and left server yolo[B]=${!!server.yolo.B}`, out);
    return out;
  },

  // (e2) same stale-ON pill while B's approval card is visible: the click
  // must neither approve the displayed command nor enable YOLO.
  async e2_stale_on_pill_click_with_visible_card_approves() {
    const server = createServer({ B: {} });
    server.answered = [];
    const tab = createTab(server, 'tab1', visibleCardOverrides(server, 'B'));
    await tab.loadSession('B');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.B === true, 'precondition: pill visible after /yolo enable', tab.state());
    server.yolo = {};
    tab.ctx.$('approvalCard').classList.add('visible');
    const before = posts(tab).length;
    await tab.clickPill();
    await tick();
    const after = posts(tab).slice(before);
    const out = { posts_from_click: after, approvals_answered: server.answered, server: { ...server.yolo } };
    check(server.answered.length === 0 && !server.yolo.B && after.every(b => b.enabled === false),
      `pill click (intent: disable) answered ${JSON.stringify(server.answered)}, posted ${JSON.stringify(after)}, server yolo[B]=${!!server.yolo.B}`, out);
    return out;
  },

  // (b) a read answered before the user's /yolo OFF lands after it: it must
  // not re-show a "click to disable" pill (whose click re-enabled YOLO).
  async b_stale_read_overwrites_toggle_off_then_click_enables() {
    const server = createServer({ B: { active_stream_id: 'stream-b' } });
    server.yolo.B = true;
    server.shouldHold = yoloGet('B');
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    check(tab.held.length === 1, 'precondition: B YOLO read in flight', tab.calls);
    const stale = tab.held.shift();
    server.shouldHold = null;
    await tab.cmdYolo();
    await tick();
    check(server.yolo.B === false && !tab.state().pillVisible, 'precondition: /yolo turned it off', { s: server.yolo, st: tab.state() });
    stale.release();
    await tick();
    const st = tab.state();
    const out = { after_stale_answer: st, server: { ...server.yolo } };
    if (st.pillVisible) {
      await tab.clickPill();
      await tick();
      out.after_click = { state: tab.state(), server: { ...server.yolo }, posts: posts(tab) };
    }
    check(!st.pillVisible && !server.yolo.B,
      `stale read re-showed the pill while server yolo[B]=false; click => server yolo[B]=${!!server.yolo.B}`, out);
    return out;
  },

  // (b2) the hiding direction: YOLO stays ON but a stale read hides the pill.
  async b2_stale_read_hides_toggle_on() {
    const server = createServer({ B: { active_stream_id: 'stream-b' } });
    server.shouldHold = yoloGet('B');
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    const stale = tab.held.shift();
    server.shouldHold = null;
    await tab.cmdYolo();
    await tick();
    check(server.yolo.B === true && tab.state().pillVisible, 'precondition: enabled + visible', tab.state());
    stale.release();
    await tick();
    const st = tab.state();
    const out = { state: st, server: { ...server.yolo } };
    check(st.pillVisible === !!server.yolo.B, `pill visible=${st.pillVisible} but server yolo[B]=${!!server.yolo.B}`, out);
    return out;
  },

  // (c) a same-session background refresh (session-updated SSE, poll,
  // idle-reconcile: loadSession(sid,{force:true})) must not hide an ON flag.
  async c_same_session_refresh_hides_enabled() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.B === true, 'precondition', tab.state());
    await tab.loadSession('B', { force: true, externalRefreshReason: 'session-updated', keepStaleUntilLoaded: true });
    await tick();
    const st = tab.state();
    const out = { state: st, server: { ...server.yolo } };
    check(st.pillVisible === true, `after same-session refresh pill visible=${st.pillVisible} while server yolo[B]=true`, out);
    return out;
  },

  // (c2) page reload / fresh tab / idle switch onto a session with YOLO ON.
  async c2_fresh_load_of_idle_session_hides_enabled() {
    const server = createServer({ B: {} });
    server.yolo.B = true;
    const tab = createTab(server, 'fresh-tab');
    await tab.loadSession('B');
    await tick();
    const st = tab.state();
    const out = { state: st, calls: tab.calls.map(c => c.method + ' ' + c.path) };
    check(st.pillVisible === true, `fresh load of idle B: pill visible=${st.pillVisible} while server yolo[B]=true`, out);
    return out;
  },

  // (d) no cross-tab sync: tab2 keeps a stale "on" pill after tab1 turned
  // YOLO off. Clicking that pill must not turn YOLO back on.
  async d_cross_tab_stale_pill_click_reenables() {
    const server = createServer({ B: {} });
    const tab1 = createTab(server, 'tab1');
    const tab2 = createTab(server, 'tab2');
    await tab1.loadSession('B');
    await tab2.loadSession('B');
    await tick();
    await tab2.cmdYolo();
    await tick();
    await tab1.cmdYolo();
    await tick();
    check(server.yolo.B === false, 'precondition: tab1 turned it off', server.yolo);
    const tab2Stale = tab2.state();
    const out = { tab2_after_tab1_disable: tab2Stale };
    if (tab2Stale.pillVisible) {
      const before = posts(tab2).length;
      await tab2.clickPill();
      await tick();
      out.tab2_click_posts = posts(tab2).slice(before);
    }
    out.server_final = { ...server.yolo };
    check(!server.yolo.B && (out.tab2_click_posts || []).every(b => b.enabled === false),
      `after tab1 disabled and tab2 clicked its stale "disable" pill, server yolo[B]=${!!server.yolo.B}`, out);
    return out;
  },

  // Control: a pill disable the server rejects keeps the authoritative state
  // from the error payload (the pill stays, YOLO is still ON).
  async control_failed_pill_disable_keeps_authoritative_state() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.B === true, 'precondition: pill visible after /yolo enable', tab.state());
    server.failPost = true;
    await tab.clickPill();
    await tick();
    const st = tab.state();
    const out = { state: st, server: { ...server.yolo }, toasts: tab.toasts };
    check(st.pillVisible === true && server.yolo.B === true && tab.toasts.includes('YOLO: relay busy'),
      `failed disable: pill visible=${st.pillVisible}, server yolo[B]=${!!server.yolo.B}, toasts=${JSON.stringify(tab.toasts)}`, out);
    return out;
  },

  // (f) the open session changes without loadSession() (a compression
  // continuation swap): a pill painted for A must not enable YOLO for the
  // new session N, and must not write to N at all.
  // (g) an explicit /yolo enable for the open session A, made while a switch to
  // B is still loading, must not be dropped: if B's load then fails, A stays
  // open with YOLO on, so its "click to disable" pill must still show.
  async g_enable_during_failed_switch_keeps_open_session_pill() {
    const server = createServer({ A: { active_stream_id: 'stream-a' }, B: {} });
    const handle = server.handle;
    let rejectB = null;
    server.handle = (method, p, body) => {
      if (method === 'GET' && p.startsWith('/api/session?session_id=B&')) {
        return new Promise((_, reject) => { rejectB = () => reject(Object.assign(new Error('metadata unavailable'), { status: 500 })); });
      }
      return handle(method, p, body);
    };
    const tab = createTab(server, 'tab1');
    await tab.loadSession('A');
    await tick();
    const switching = tab.loadSession('B');
    await tick();
    check(typeof rejectB === 'function', 'precondition: B metadata request in flight', tab.calls);
    await tab.cmdYolo();
    await tick();
    check(server.yolo.A === true, 'precondition: /yolo enabled A on the server', { server: { ...server.yolo } });
    rejectB();
    await switching;
    await tick();
    const st = tab.state();
    const out = { after_failed_switch: st, server: { ...server.yolo }, toasts: tab.toasts };
    check(st.sid === 'A' && st.pillVisible === true && st.yolo === true,
      `A stays open with server yolo[A]=true but pill visible=${st.pillVisible}, mirror=${st.yolo}`, out);
    return out;
  },

  // (h) a switch whose metadata fetch fails leaves the previous session open:
  // its YOLO flag (still on at the server) must be read back, not left hidden.
  async h_failed_switch_reads_back_open_session_flag() {
    const server = createServer({ A: { active_stream_id: 'stream-a' }, B: {} });
    server.yolo.A = true;
    const handle = server.handle;
    let rejectB = null;
    server.handle = (method, p, body) => {
      if (method === 'GET' && p.startsWith('/api/session?session_id=B&')) {
        return new Promise((_, reject) => { rejectB = () => reject(Object.assign(new Error('metadata unavailable'), { status: 500 })); });
      }
      return handle(method, p, body);
    };
    const tab = createTab(server, 'tab1');
    await tab.loadSession('A');
    await tick();
    check(tab.state().pillVisible === true, 'precondition: A shows its YOLO pill', tab.state());
    const switching = tab.loadSession('B');
    await tick();
    check(typeof rejectB === 'function', 'precondition: B metadata request in flight', tab.calls);
    rejectB();
    await switching;
    await tick();
    const st = tab.state();
    const out = { after_failed_switch: st, server: { ...server.yolo } };
    check(st.sid === 'A' && st.pillVisible === true && st.yolo === true,
      `A stays open with server yolo[A]=true but pill visible=${st.pillVisible}, mirror=${st.yolo}`, out);
    return out;
  },

  async f_pill_click_after_untracked_session_swap_enables_new_session() {
    const server = createServer({ A: {}, N: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('A');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.A === true, 'precondition: /yolo enabled A and shows its pill', tab.state());
    tab.ctx.S.session = { session_id: 'N', title: 'N', messages: [] };
    const before = posts(tab).length;
    await tab.clickPill();
    await tick();
    const after = posts(tab).slice(before);
    const st = tab.state();
    const out = { posts_from_click: after, server: { ...server.yolo }, state: st };
    check(!server.yolo.N && after.every(b => b.session_id === 'A' && b.enabled === false) && !st.pillVisible,
      `pill painted for A, clicked on N: posted ${JSON.stringify(after)}, server yolo[N]=${!!server.yolo.N}, pill visible=${st.pillVisible}`, out);
    return out;
  },

  // (i) two overlapping /yolo commands (the second typed before the first's
  // read answered) both read "off" and both POST enabled:true. The second must
  // post nothing; a /yolo after the first settled must still toggle.
  async i_overlapping_yolo_commands_both_enable() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    await Promise.all([tab.cmdYolo(), tab.cmdYolo()]);
    await tick();
    const overlap = posts(tab);
    const out = { posts_from_overlap: overlap, server: { ...server.yolo }, state: tab.state(), toasts: tab.toasts };
    check(overlap.length === 1 && overlap[0].enabled === true && server.yolo.B === true && out.state.pillVisible,
      `two overlapping /yolo posted ${JSON.stringify(overlap)}`, out);
    await tab.cmdYolo();
    await tick();
    out.later_posts = posts(tab).slice(overlap.length);
    out.later_state = tab.state();
    check(out.later_posts.length === 1 && out.later_posts[0].enabled === false && server.yolo.B === false && !out.later_state.pillVisible,
      `a /yolo after the first settled posted ${JSON.stringify(out.later_posts)}`, out);
    return out;
  },

  // (i2) a second /yolo while the first's enable POST is still in flight reads
  // the half-applied "on" and posts a disable; the first's late answer then
  // paints an "on" pill over a server that is off.
  async i2_yolo_while_first_post_in_flight_desyncs_pill() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    server.shouldHold = (name, method, p) => method === 'POST' && p === '/api/session/yolo';
    const first = tab.cmdYolo();
    await tick();
    check(tab.held.length === 1 && server.yolo.B === true, 'precondition: first enable POST in flight', tab.calls);
    server.shouldHold = null;
    await tab.cmdYolo();
    await tick();
    tab.held.shift().release();
    await first;
    await tick();
    const st = tab.state();
    const out = { posts: posts(tab), server: { ...server.yolo }, state: st };
    check(out.posts.length === 1 && server.yolo.B === true && st.pillVisible === true,
      `second /yolo during the first's POST: posted ${JSON.stringify(out.posts)}, server yolo[B]=${!!server.yolo.B}, pill visible=${st.pillVisible}`, out);
    return out;
  },

  // (i3) the in-flight guard is per session: a /yolo for B issued while A's
  // /yolo is still waiting must still toggle B (here: disable B's YOLO).
  async i3_yolo_for_other_session_while_first_in_flight_is_dropped() {
    const server = createServer({ A: {}, B: {} });
    server.yolo.B = true;
    const tab = createTab(server, 'tab1');
    await tab.loadSession('A');
    await tick();
    server.shouldHold = yoloGet('A');
    const first = tab.cmdYolo();
    await tick();
    check(tab.held.length === 1, 'precondition: A\'s /yolo read is in flight', tab.calls);
    server.shouldHold = null;
    await tab.loadSession('B');
    await tick();
    const before = posts(tab).length;
    await tab.cmdYolo();
    await tick();
    const forB = posts(tab).slice(before);
    const out = { posts_for_b: forB, server: { ...server.yolo }, state: tab.state() };
    tab.held.shift().release();
    await first;
    await tick();
    out.after_a_settles = { server: { ...server.yolo }, state: tab.state() };
    check(forB.length === 1 && forB[0].session_id === 'B' && forB[0].enabled === false && server.yolo.B === false,
      `/yolo on B while A's is in flight posted ${JSON.stringify(forB)}, server yolo[B]=${!!server.yolo.B}`, out);
    check(!out.after_a_settles.state.pillVisible && out.after_a_settles.state.sid === 'B',
      'B stays open with its pill hidden after A\'s stale /yolo settles', out);
    return out;
  },

  // (j) a malformed GET /api/session/yolo answer (a non-boolean flag) must
  // not paint a "click to disable" pill for a session whose YOLO is off.
  async j_malformed_read_paints_on_pill() {
    const out = {};
    for (const value of ['yes', 1, 'false']) {
      const server = createServer({ B: {} });
      server.yoloGetBody = { yolo_enabled: value };
      const tab = createTab(server, 'tab-' + String(value));
      await tab.loadSession('B');
      await tick();
      out[JSON.stringify(value)] = tab.state();
    }
    check(Object.values(out).every(st => !st.pillVisible && st.yolo === false),
      `malformed yolo_enabled answers painted the pill: ${JSON.stringify(out)}`, out);
    return out;
  },

  // (j2) the hiding direction: a malformed answer to a same-session re-read
  // must leave an ON view alone instead of turning it off.
  async j2_malformed_read_hides_on_pill() {
    const server = createServer({ B: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.B === true, 'precondition: pill visible after /yolo enable', tab.state());
    const out = {};
    for (const value of [0, null, 'true']) {
      server.yoloGetBody = { yolo_enabled: value };
      await tab.loadSession('B', { force: true, externalRefreshReason: 'session-updated', keepStaleUntilLoaded: true });
      await tick();
      out[JSON.stringify(value)] = tab.state();
    }
    check(Object.values(out).every(st => st.pillVisible && st.yolo === true),
      `malformed re-reads hid the pill while server yolo[B]=true: ${JSON.stringify(out)}`, out);
    return out;
  },

  // (k1-k4) a blank-page action mints a new session without loadSession():
  // the previous session's ON pill must not stay on screen for it.
  async k1_prompt_workspace_path_install_keeps_stale_pill() { return blankPageInstallKeepsStalePill('promptWorkspacePath'); },
  async k2_switch_to_workspace_install_keeps_stale_pill() { return blankPageInstallKeepsStalePill('switchToWorkspace'); },
  async k3_prompt_new_file_install_keeps_stale_pill() { return blankPageInstallKeepsStalePill('promptNewFile'); },
  async k4_prompt_new_folder_install_keeps_stale_pill() { return blankPageInstallKeepsStalePill('promptNewFolder'); },

  // (k5) an `apperror` after a compression rotation installs the continuation
  // session N (an EXISTING session) from d.session. A's pill must go at once,
  // and N's own server flag must be read back.
  async k5_apperror_rotation_keeps_stale_pill_and_skips_read() {
    const server = createServer({ A: { active_stream_id: 'stream-a' }, N: {} });
    server.yolo.N = true;
    const tab = createTab(server, 'tab1', apperrorStubs, [apperrorAttachSrc()]);
    await tab.loadSession('A');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.A === true, 'precondition: /yolo enabled A and shows its pill', tab.state());
    const source = fakeEventSource();
    vm.runInContext('__attachApperror', tab.ctx)(source, 'A', 'stream-a');
    server.shouldHold = yoloGet('N');
    source.listeners.apperror({ data: JSON.stringify({
      type: 'error', message: 'provider failed', session_id: 'N', old_session_id: 'A', new_session_id: 'N',
      continuation_session_id: 'N', session: { session_id: 'N', title: 'N', messages: [] },
    }) });
    await tick();
    const during = tab.state();
    const out = { while_read_in_flight: during, reads_of_N: tab.held.length, server: { ...server.yolo } };
    check(during.sid === 'N', 'precondition: apperror installed the continuation N', out);
    check(!during.pillVisible || during.pillSession === 'N',
      `after rotation to N the pill painted for ${during.pillSession} is still visible`, out);
    check(tab.held.length === 1, `rotation to N did not read N's YOLO flag (server yolo[N]=true)`, out);
    tab.held.shift().release();
    await tick();
    out.after_read = tab.state();
    check(out.after_read.pillVisible && out.after_read.pillSession === 'N' && out.after_read.yolo === true,
      `N's flag (server yolo[N]=true) not shown after the read: ${JSON.stringify(out.after_read)}`, out);
    return out;
  },

  // Control: an `apperror` for the open session itself (no rotation) keeps
  // its YOLO view and issues no extra YOLO read.
  async control_apperror_same_session_keeps_view_without_read() {
    const server = createServer({ A: { active_stream_id: 'stream-a' } });
    const tab = createTab(server, 'tab1', apperrorStubs, [apperrorAttachSrc()]);
    await tab.loadSession('A');
    await tick();
    await tab.cmdYolo();
    await tick();
    check(tab.state().pillVisible && server.yolo.A === true, 'precondition: /yolo enabled A and shows its pill', tab.state());
    const source = fakeEventSource();
    vm.runInContext('__attachApperror', tab.ctx)(source, 'A', 'stream-a');
    const before = tab.calls.length;
    source.listeners.apperror({ data: JSON.stringify({
      type: 'error', message: 'provider failed', session_id: 'A', session: { session_id: 'A', title: 'A', messages: [] },
    }) });
    await tick();
    const st = tab.state();
    const reads = tab.calls.slice(before).filter(c => c.path.startsWith('/api/session/yolo'));
    const out = { state: st, yolo_requests_after_error: reads };
    check(st.sid === 'A' && st.pillVisible && st.pillSession === 'A' && reads.length === 0,
      `same-session apperror: pill visible=${st.pillVisible}, yolo requests=${JSON.stringify(reads)}`, out);
    return out;
  },

  // Control: the /yolo in-flight guard is released on every exit: after a
  // failed POST and after a read made stale by a session switch.
  async control_yolo_guard_released_after_failure_and_stale_switch() {
    const server = createServer({ A: {}, B: {} });
    const tab = createTab(server, 'tab1');
    await tab.loadSession('B');
    await tick();
    server.failPost = true;
    await tab.cmdYolo();
    await tick();
    server.failPost = false;
    await tab.cmdYolo();
    await tick();
    const afterFailure = posts(tab);
    check(afterFailure.length === 2 && afterFailure[1].enabled === true && server.yolo.B === true,
      `/yolo after a failed /yolo posted ${JSON.stringify(afterFailure)}`, { posts: afterFailure, toasts: tab.toasts });
    server.shouldHold = yoloGet('B');
    const stale = tab.cmdYolo();
    await tick();
    server.shouldHold = null;
    await tab.loadSession('A');
    await tick();
    tab.held.shift().release();
    await stale;
    await tick();
    await tab.cmdYolo();
    await tick();
    const last = posts(tab).slice(afterFailure.length);
    // B's own guard must be free again too: a /yolo on B after its stale exit posts.
    await tab.loadSession('B');
    await tick();
    await tab.cmdYolo();
    await tick();
    const again = posts(tab).slice(afterFailure.length + last.length);
    const out = { posts_after_stale: last, posts_on_B_again: again, server: { ...server.yolo }, state: tab.state() };
    check(last.length === 1 && last[0].session_id === 'A' && last[0].enabled === true && server.yolo.A === true,
      `/yolo on A after a stale /yolo on B posted ${JSON.stringify(last)}`, out);
    check(again.length === 1 && again[0].session_id === 'B' && again[0].enabled === false && server.yolo.B === false,
      `/yolo on B after its own stale /yolo posted ${JSON.stringify(again)}`, out);
    return out;
  },

  // (l) /yolo toggles from its own GET: a malformed answer (no boolean flag)
  // must not be inverted into a POST, whichever state the server is in.
  async l_malformed_read_never_toggles() {
    const out = {};
    for (const start of [false, true]) {
      for (const body of [{}, { yolo_enabled: null }, { yolo_enabled: 'yes' }, { yolo_enabled: 0 }, { yolo_enabled: 1 }]) {
        const server = createServer({ B: {} });
        server.yolo.B = start;
        const tab = createTab(server, 'tab-' + start + '-' + JSON.stringify(body));
        await tab.loadSession('B');
        await tick();
        server.yoloGetBody = body;
        await tab.cmdYolo();
        await tick();
        server.yoloGetBody = null;
        await tab.cmdYolo();
        await tick();
        const sent = posts(tab);
        out[start + ' ' + JSON.stringify(body)] = { posts: sent, server_B: server.yolo.B, toasts: tab.toasts.slice() };
      }
    }
    // After each malformed read, the next well-formed /yolo is the only POST, and it
    // is the inverse of the real state (so the guard was released too).
    check(Object.entries(out).every(([k, v]) => v.posts.length === 1 && v.posts[0].enabled === !k.startsWith('true')),
      `malformed /yolo reads posted: ${JSON.stringify(out)}`, out);
    return out;
  },
};

(async () => {
  const fn = scenarios[which];
  if (!fn) { console.log(JSON.stringify({ error: 'unknown scenario', available: Object.keys(scenarios) })); process.exit(2); }
  try {
    const out = await fn();
    console.log(JSON.stringify({ scenario: which, invariant: 'held', detail: out }));
    process.exit(0);
  } catch (e) {
    if (e.violation) {
      console.log(JSON.stringify({ scenario: which, invariant: 'VIOLATED', message: e.message, detail: e.ctx }));
      process.exit(1);
    }
    console.log(JSON.stringify({ scenario: which, harness_error: String((e && e.stack) || e) }));
    process.exit(2);
  }
})();
