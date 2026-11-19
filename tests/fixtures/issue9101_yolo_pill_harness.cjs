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
    classList: { contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c), toggle() {} },
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
    document: { getElementById: id => els[id] || (els[id] = makeEl(id)), querySelector() { return null; } },
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
    '  pillVisible: pill.style.display !== "none", pillSession: pill.getAttribute("data-yolo-session"),',
    '  gen: _loadSessionGeneration, loading: _loadingSessionId}; }',
    ...extraSrc,
  ].join('\n');
  vm.runInContext(src, ctx, { filename: name + '-harness.js' });
  return {
    name, ctx, calls, toasts, held, pillOnclick,
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

function createServer(sessions) {
  const server = {
    yolo: {},
    sessions,
    shouldHold: null,
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
        return { yolo_enabled: !!server.yolo[u.searchParams.get('session_id')] };
      }
      if (u.pathname === '/api/session/yolo' && method === 'POST' && server.failPost) {
        const e = new Error('relay busy');
        e.body = JSON.stringify({ error: 'relay busy', yolo_enabled: !!server.yolo[body.session_id] });
        return Promise.reject(e);
      }
      if (u.pathname === '/api/session/yolo' && method === 'POST') {
        // api/routes.py: enabled = bool(body.get("enabled", True))
        const enabled = body.enabled === undefined ? true : !!body.enabled;
        server.yolo[body.session_id] = enabled;
        return { ok: true, yolo_enabled: enabled };
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

const scenarios = {
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
