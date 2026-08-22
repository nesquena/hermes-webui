"""Regression coverage for PWA-backed browser notifications (#3196)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

DESKTOP_BACKGROUND_NOTIFICATION_NAMES = (
    "_desktopBackgroundedForNotifications",
    "__hermesSetBackgrounded",
    "_isBackgroundedForBrowserNotification",
)


def _source_between(start_marker: str, end_marker: str) -> str:
    start = MESSAGES_JS.index(start_marker)
    end = MESSAGES_JS.index(end_marker, start)
    return MESSAGES_JS[start:end]


def test_browser_notifications_use_service_worker_when_available():
    assert "function _showPwaNotification" in MESSAGES_JS
    assert "navigator.serviceWorker.ready" in MESSAGES_JS
    assert "reg.showNotification" in MESSAGES_JS
    assert "new Notification" in MESSAGES_JS
    assert "function sendBrowserNotification" in MESSAGES_JS


def test_notification_payload_uses_completion_session_when_provided():
    assert "function _notificationOptions" in MESSAGES_JS
    assert "const sid=(options&&options.sid)||(S&&S.session&&S.session.session_id);" in MESSAGES_JS
    assert "_sessionUrlForSid(sid)" in MESSAGES_JS
    assert "data:{url}" in MESSAGES_JS
    assert "tag:sid?`hermes-${sid}`" in MESSAGES_JS
    assert "function _completionNotificationPreviewText" in MESSAGES_JS
    assert "_completionNotificationPreviewText(lastAsst," in MESSAGES_JS
    assert "sendBrowserNotification('Response complete',_completionPreview||'Task finished',{forceHidden:_wasEverBackgrounded,sid:completedSid})" in MESSAGES_JS
    assert "assistantText?assistantText.slice(0,100)" not in MESSAGES_JS
    assert "sendBrowserNotification('Approval required',d.description||'Tool approval needed',{sid:activeSid})" in MESSAGES_JS
    assert "sendBrowserNotification('Clarification needed',d.question||'Tool clarification needed',{sid:activeSid})" in MESSAGES_JS


def test_completion_notification_preview_uses_settled_message_not_live_prefix():
    """Background completion preview must not slice the live-stream accumulator."""
    assert "function _completionNotificationPreviewText" in MESSAGES_JS
    assert "String(msgContent(lastAssistantMessage)||'').trim()" in MESSAGES_JS
    assert "_assistantTurnAnchorSettledFinalAnswer" in MESSAGES_JS
    done_block = _source_between("source.addEventListener('done'", "source.addEventListener('stream_end'")
    assert "let lastAsst=null;" in done_block
    assert "d.session.messages" in done_block
    assert "liveDisplayText:typeof _streamDisplay==='function'?_streamDisplay():assistantText" in done_block


def test_completion_notification_fires_when_tab_was_hidden_during_stream():
    """#4416: a throttled background-tab SSE delivers `done` late (after the user
    returns, document.hidden=false), which silently dropped the completion
    notification. The done handler now passes forceHidden based on whether the
    tab was hidden at ANY point during the stream, and sendBrowserNotification
    bypasses ONLY the live visibility gate (not the user's enabled setting) on
    forceHidden — so a backgrounded stream notifies, a watched one stays silent."""
    # The per-stream hidden tracker exists and is wired at attach + done.
    assert "_STREAM_WAS_HIDDEN" in MESSAGES_JS
    assert "function _bindStreamHiddenTracker" in MESSAGES_JS
    # Entries are stream-owned ({streamId, wasHidden}) so a stale entry from a
    # non-`done` terminal path can't be mis-attributed to a later same-sid stream.
    assert "function _shouldForceCompletionNotification(sid, streamId){" in MESSAGES_JS
    assert "return wasHidden||wasBackgrounded;" in MESSAGES_JS
    assert "function _clearStreamHidden" in MESSAGES_JS
    assert "function _clearStreamNotificationBackground" in MESSAGES_JS
    # Done-path cleanup lives inside _shouldForceCompletionNotification(); the
    # activeSid call sites are the non-done terminal paths.
    assert "_clearStreamHidden(sid, streamId);" in MESSAGES_JS
    assert "_clearStreamNotificationBackground(sid, streamId);" in MESSAGES_JS
    assert MESSAGES_JS.count("_clearStreamHidden(activeSid, streamId)") >= 3
    assert MESSAGES_JS.count("_clearStreamNotificationBackground(activeSid, streamId)") >= 3
    # sendBrowserNotification honors forceHidden but still respects the
    # notifications-enabled setting (forceHidden is NOT the test-button force).
    assert "const forceHidden=!!(options&&options.forceHidden);" in MESSAGES_JS
    assert "if(!force&&!window._notificationsEnabled) return;" in MESSAGES_JS
    assert "function _isBackgroundedForBrowserNotification(){" in MESSAGES_JS
    assert "window.__hermesSetBackgrounded=(value)=>{" in MESSAGES_JS
    assert "if(!force&&!forceHidden&&!_isBackgroundedForBrowserNotification()) return;" in MESSAGES_JS


def test_desktop_background_notification_signal_stays_out_of_stream_visibility():
    stream_tracker = _source_between(
        "const LIVE_STREAMS={};",
        "function closeLiveStream(sessionId, streamId, source){",
    )
    deferred_recovery = _source_between(
        "function _reattachOrRestoreAfterDeferredStreamError(source){",
        "  // Bug A fix (#631):",
    )

    for name in DESKTOP_BACKGROUND_NOTIFICATION_NAMES:
        assert name not in stream_tracker
        assert name not in deferred_recovery


def test_rotated_done_notification_uses_continuation_session_for_tag_and_url():
    """
    #6689 re-gate: after auto-compression rotates the session id to a continuation,
    the completion notification's tag and click URL must point to the CONTINUATION
    session (completedSid), not the archived parent (activeSid).

    The done handler computes completedSid from the done payload and passes it to
    sendBrowserNotification. _notificationOptions derives the tag and data.url from
    options.sid, so the notification must carry completedSid. The hidden/background
    delivery gate (forceHidden) must still be respected — only the sid changes.
    """
    done_block = _source_between(
        "source.addEventListener('done'", "source.addEventListener('stream_end'"
    )
    # The notification call in the done block must use completedSid
    assert "sendBrowserNotification('Response complete'" in done_block
    assert "sid:completedSid" in done_block
    # The legacy activeSid must NOT appear in the response-complete call
    # (it may appear in approval/clarification calls — those are pre-rotation)
    response_complete_call = done_block[
        done_block.index("sendBrowserNotification('Response complete'")
        : done_block.index("sendBrowserNotification('Response complete'") + 200
    ]
    assert "sid:activeSid" not in response_complete_call, (
        "response-complete notification must not use activeSid (archived parent)"
    )
    # The preview helper must receive the completed session id
    assert "sessionId:completedSid" in done_block
    # forceHidden gate must still be present (hidden/background delivery preserved)
    assert "forceHidden:_wasEverBackgrounded" in done_block


def test_service_worker_handles_notification_clicks_without_hijacking_other_sessions():
    assert "notificationclick" in SW_JS
    assert "event.notification.close()" in SW_JS
    assert "clients.matchAll" in SW_JS
    assert "clients.openWindow" in SW_JS
    # Match the open tab on pathname, not the full href (query/hash differ).
    assert "samePath(client.url)" in SW_JS
    assert "new URL(clientUrl).pathname === targetPath" in SW_JS
    assert "targetClient.focus()" in SW_JS
    exact_idx = SW_JS.index("targetClient.focus()")
    open_idx = SW_JS.index("self.clients.openWindow(targetUrl)")
    navigate_idx = SW_JS.index("focusableClient.navigate(targetUrl)")
    assert exact_idx < open_idx < navigate_idx


def test_settings_expose_permission_and_test_controls():
    assert "notificationPermissionStatus" in INDEX_HTML
    assert 'id="notificationPermissionButtonWrap"' in INDEX_HTML
    assert 'id="notificationPermissionButton"' in INDEX_HTML
    assert "requestNotificationPermission()" in INDEX_HTML
    assert "sendBrowserNotification('Hermes test'" in INDEX_HTML
    assert "{force:true}" in INDEX_HTML
    assert "function updateNotificationPermissionStatus" in PANELS_JS
    assert "const btn=$('notificationPermissionButton');" in PANELS_JS
    assert "const btnWrap=$('notificationPermissionButtonWrap');" in PANELS_JS
    assert "btn.disabled=granted;" in PANELS_JS
    assert "btn.title=granted?'':label;" in PANELS_JS
    assert "if(btnWrap) btnWrap.title=label;" in PANELS_JS
    assert "notifications_permission_status" in PANELS_JS
    assert "btn.setAttribute('aria-label', label);" in PANELS_JS
    assert "btn.setAttribute('aria-disabled', granted?'true':'false');" in PANELS_JS
    assert "btn.setAttribute('aria-disabled','true');" in PANELS_JS


def test_granted_permission_branch_is_not_silent():
    fn = MESSAGES_JS[
        MESSAGES_JS.index("function requestNotificationPermission(){") :
        MESSAGES_JS.index("function sendBrowserNotification(", MESSAGES_JS.index("function requestNotificationPermission(){"))
    ]
    assert "if(Notification.permission==='granted'){" in fn
    granted_branch = fn[
        fn.index("if(Notification.permission==='granted'){") :
        fn.index("if(Notification.permission==='denied'){")
    ]
    assert "updateNotificationPermissionStatus()" in granted_branch
    assert "showToast(t('notifications_enabled_toast'),3000)" in granted_branch
    assert "return Promise.resolve('granted');" in granted_branch


def test_notification_i18n_and_changelog_entries_exist():
    for key in [
        "notifications_enable_btn",
        "notifications_test_btn",
        "notifications_permission_status",
        "notifications_enabled_toast",
        "notifications_denied",
        "notifications_unsupported",
    ]:
        assert key in I18N_JS
    assert "PWA notifications now use the service worker" in CHANGELOG
    assert "#3196" in CHANGELOG
    entry = next(
        line for line in CHANGELOG.splitlines()
        if "Notification permission controls now reflect the real browser state" in line
    )
    assert entry.count("#4118") == 1


_ROTATED_DONE_HARNESS = r"""
// Behavioral harness for the #6689 rotated-done notification regression.
// Loads the REAL static/messages.js into a vm sandbox, drives the REAL
// attachLiveStream() + its registered 'done' callback with a done payload whose
// session.session_id names the continuation session, and captures the delivery
// through the REAL sendBrowserNotification() -> _notificationOptions() chain
// into a direct `new Notification(...)` sink (navigator.serviceWorker absent).
// The real _sessionUrlForSid() is extracted from static/sessions.js so the
// tag/data.url composition is exercised against the shipped function.
'use strict';
const fs = require('fs');
const vm = require('vm');

const MESSAGES_PATH = process.argv[1];
const SESSIONS_PATH = process.argv[2];
const MESSAGES = fs.readFileSync(MESSAGES_PATH, 'utf8');
const SESSIONS = fs.readFileSync(SESSIONS_PATH, 'utf8');

// --- extract a real function body from source (balanced braces) ---
function extractFunction(src, marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('missing ' + marker);
  const brace = src.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('unbalanced ' + marker);
}
const SESSION_URL_FN = extractFunction(SESSIONS, 'function _sessionUrlForSid');

// --- minimal DOM/browser element stub ---
function makeElement() {
  const el = {
    nodeType: 1, tagName: 'DIV', className: '', dataset: {}, style: {},
    children: [], _children: [],
    appendChild(c) { this._children.push(c); this.children = this._children; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    remove() {}, addEventListener() {}, removeEventListener() {},
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    hasAttribute() { return false; }, getAttributeNames() { return []; }, toggleAttribute() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, replaceWith() {}, before() {}, after() {},
    insertBefore() {}, removeChild() {}, contains() { return false; },
    isConnected: false, focus() {}, blur() {}, click() {}, scrollIntoView() {},
    parentElement: null, parentNode: null, firstChild: null, lastChild: null,
    nextSibling: null, previousSibling: null, classList: { add() {}, remove() {}, contains() { return false; } },
  };
  Object.defineProperty(el, 'innerHTML', { get() { return this._inner || ''; }, set(v) { this._inner = String(v); } });
  Object.defineProperty(el, 'textContent', { get() { return this._text || ''; }, set(v) { this._text = String(v); } });
  return el;
}

function buildSandbox(scenario) {
  const captured = [];
  const sources = [];
  const sandbox = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, Math, JSON, URL, URLSearchParams, encodeURIComponent, decodeURIComponent,
    Map, Set, WeakMap, Promise, Number, String, Boolean, Object, Array, RegExp, Error, TypeError, parseInt, parseFloat, isNaN,
  };
  sandbox.window = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.removeEventListener = () => {};
  sandbox.dispatchEvent = () => {};
  sandbox.__captured = captured;
  sandbox.__sources = sources;

  const documentStub = {
    hidden: !!scenario.hiddenAtAttach,
    visibilityState: scenario.hiddenAtAttach ? 'hidden' : 'visible',
    baseURI: 'http://localhost:8787/',
    addEventListener() {}, removeEventListener() {},
    getElementById() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement() { return makeElement(); },
    createTextNode(t) { return { nodeType: 3, data: String(t), textContent: String(t) }; },
    createDocumentFragment() { return makeElement(); },
    hasFocus() { return true; },
    body: makeElement(),
    documentElement: makeElement(),
  };
  sandbox.document = documentStub;

  sandbox.location = {
    origin: 'http://localhost:8787',
    href: 'http://localhost:8787/session/parent',
    pathname: '/session/parent',
    search: '', hash: '',
  };
  sandbox.history = { replaceState() {}, pushState() {} };
  sandbox.navigator = {};
  sandbox.localStorage = {
    getItem() { return null; }, setItem() {}, removeItem() {},
  };
  sandbox.requestAnimationFrame = () => {};
  sandbox.cancelAnimationFrame = () => {};
  sandbox.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
  sandbox.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });

  sandbox.EventSource = class {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    constructor(url, opts) {
      this.url = url; this.opts = opts; this.readyState = sandbox.EventSource.CONNECTING;
      this._listeners = {};
      sources.push(this);
    }
    addEventListener(ev, fn) {
      (this._listeners[ev] = this._listeners[ev] || []).push(fn);
    }
    removeEventListener(ev, fn) {
      const arr = this._listeners[ev];
      if (!arr) return;
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    }
    close() { this.readyState = sandbox.EventSource.CLOSED; }
    dispatch(ev, data) {
      const arr = this._listeners[ev] || [];
      const event = { data, type: ev, lastEventId: '', target: this };
      for (const fn of arr.slice()) fn(event);
    }
  };

  sandbox.Notification = class {
    static permission = 'granted';
    constructor(title, options) {
      captured.push({ title, options });
    }
  };

  // Helpers that live in OTHER static files (ui.js / workspace.js / sessions.js
  // / i18n.js) and are referenced by the real messages.js code paths we drive.
  const noop = () => {};
  const ext = {
    $: () => null,
    t: (k) => k,
    api: async () => null,
    showToast: noop, setBusy: noop, setComposerStatus: noop, setStatus: noop,
    renderMessages: noop, syncTopbar: noop, renderSessionList: noop, loadDir: noop,
    showLiveRunStatus: noop, hideLiveRunStatus: noop,
    clearInflight: noop, clearInflightState: noop, markInflight: noop, saveInflightState: noop,
    _suspendSessionStreamForLiveChat: noop, _resumeSessionStreamAfterLiveChat: noop,
    _clearApprovalPendingForSession: noop, _clearClarifyPendingForSession: noop,
    stopApprovalPolling: noop, stopClarifyPolling: noop,
    hideApprovalCard: noop, hideClarifyCard: noop,
    ensureLiveWorklogShell: noop, appendThinking: noop, removeThinking: noop,
    finalizeThinkingCard: noop, clearLiveToolCards: noop,
    highlightCode: noop, addCopyButtons: noop, renderKatexBlocks: noop,
    _hydrateTodosFromSession: noop, clearVisibleMessageRowCache: noop,
    _mergeUsageForCtxIndicator: noop, _syncCtxIndicator: noop,
    _shouldFollowMessagesOnDomReplace: noop, _isMessagePaneNearBottom: () => false,
    _captureMessageScrollSnapshot: () => null,
    _armKeepSettledWorklogOpen: noop, _disarmKeepSettledWorklogOpen: noop,
    _renderMessagesWithScrollSnapshot: noop, scrollToBottom: noop,
    noteWorkspaceMutationsFromToolCalls: noop, autoReadLastAssistant: noop,
    queueSessionMessage: noop, updateQueueBadge: noop,
    _markSessionCompletionUnread: noop, _markSessionCompletedInList: noop,
    _setActiveSessionUrl: noop, _setSessionViewedCount: noop,
    _clearSessionCompletionUnread: noop, renderSessionListFromCache: noop,
    resetTurnWorkspaceMutations: noop, _resetStreamScrollFollow: noop,
    renderSessionArtifacts: noop, trackBackgroundError: noop,
    recordClientSSEError: noop, _deferStreamErrorIfOffline: () => false,
    msgContent: (m) => {
      if (!m || typeof m !== 'object') return '';
      if (Array.isArray(m.content)) {
        return m.content.filter((p) => p && p.type === 'text').map((p) => p.text || '').join('');
      }
      return typeof m.content === 'string' ? m.content : '';
    },
    _assistantTurnAnchorSettledFinalAnswer: () => undefined,
    _isPreservedCompressionTaskListMarkerOnlyText: () => false,
    _smdMediaTailFlush: noop, _smdMediaTailClear: noop, _smdClearParserIdentity: noop,
    _isSafeDataImageUri: () => false,
    assistantDisplayName: () => 'Hermes',
    setComposerStatus: noop,
    _isMessageReaderUnpinned: () => false,
  };
  Object.assign(sandbox, ext);
  return sandbox;
}

function runScenario(scenario) {
  const sandbox = buildSandbox(scenario);
  vm.createContext(sandbox);
  vm.runInContext(SESSION_URL_FN, sandbox);
  vm.runInContext(MESSAGES, sandbox);
  const driver = `
    const S={session:{session_id:'parent',message_count:2},messages:[],toolCalls:[],busy:false,activeStreamId:'stream-1',todos:[],todoStateMeta:null};
    const INFLIGHT={};
    window._notificationsEnabled=${scenario.enabled};
    document.hidden=${scenario.hiddenAtAttach};
    _desktopBackgroundedForNotifications=${scenario.desktopBackgrounded};
    attachLiveStream('parent','stream-1');
    // The page returns to the foreground before 'done' arrives (throttled SSE).
    document.hidden=false;
    const __src=LIVE_STREAMS['parent'].source;
    if(!__src) throw new Error('no source wired');
    __src.dispatch('done', ${JSON.stringify(scenario.donePayload)});
    __out={
      captured: __captured.map(function(c){return {title:c.title, tag:c.options.tag, url:c.options.data.url, body:c.options.body};}),
      count: __captured.length,
      sessionAfterDone: (S.session&&S.session.session_id)||null,
      expectedUrl: location.origin + _sessionUrlForSid('${scenario.expectedSid}'),
    };
  `;
  vm.runInContext(driver, sandbox);
  return sandbox.__out;
}

const scenarios = {
  rotated: {
    hiddenAtAttach: true,
    desktopBackgrounded: true,
    enabled: true,
    expectedSid: 'continuation',
    donePayload: JSON.stringify({
      status: 'completed',
      session: { session_id: 'continuation', messages: [{ role: 'assistant', content: 'Hello world' }] },
    }),
  },
  nonRotated: {
    hiddenAtAttach: true,
    desktopBackgrounded: true,
    enabled: true,
    expectedSid: 'parent',
    // No rotation: the done payload still names the parent session id, so the
    // notification must fall back to the activeSid tag/url.
    donePayload: JSON.stringify({
      status: 'completed',
      session: { session_id: 'parent', messages: [{ role: 'assistant', content: 'Hello world' }] },
    }),
  },
  disabled: {
    hiddenAtAttach: true,
    desktopBackgrounded: true,
    enabled: false,
    expectedSid: 'continuation',
    donePayload: JSON.stringify({
      status: 'completed',
      session: { session_id: 'continuation', messages: [{ role: 'assistant', content: 'Hello world' }] },
    }),
  },
  neverBackgrounded: {
    hiddenAtAttach: false,
    desktopBackgrounded: false,
    enabled: true,
    expectedSid: 'continuation',
    donePayload: JSON.stringify({
      status: 'completed',
      session: { session_id: 'continuation', messages: [{ role: 'assistant', content: 'Hello world' }] },
    }),
  },
};

const out = {};
for (const name of Object.keys(scenarios)) {
  try {
    out[name] = runScenario(scenarios[name]);
  } catch (err) {
    out[name] = { error: String(err && err.stack || err) };
  }
}
console.log(JSON.stringify(out, null, 2));
"""

@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_rotated_done_notification_delivers_continuation_tag_and_url():
    """#6689 re-gate: BEHAVIORAL regression for the rotated-done notification.

    The previous round's
    `test_rotated_done_notification_uses_continuation_session_for_tag_and_url`
    only slices messages.js source; this test replaces that oracle as the
    primary proof by EXECUTING the shipped code:

      - the real `attachLiveStream('parent', 'stream-1')` runs in a node vm and
        wires the real EventSource `done` listener;
      - the page is hidden + desktop-backgrounded at attach time and returns to
        the foreground before `done` arrives, so the #4416 forceHidden delivery
        gate is exercised (a late, throttled SSE must still notify);
      - the done payload carries `session.session_id='continuation'`, so the
        handler resolves completedSid to the continuation id;
      - the delivery flows through the REAL sendBrowserNotification ->
        _notificationOptions -> _sessionUrlForSid chain into a captured
        `new Notification` sink (direct path: no service worker in the sandbox).

    Assertions:
      - exactly ONE delivery with tag == 'hermes-continuation' and
        data.url == location.origin + _sessionUrlForSid('continuation')
        (computed inside the harness against the shipped _sessionUrlForSid);
      - non-rotated control (payload still names the parent sid) must fall back
        to tag == 'hermes-parent' / the parent url;
      - notifications-disabled control delivers nothing;
      - never-backgrounded control (watched stream) delivers nothing.

    Mutation sensitivity (verified manually): reverting the done-path call to
    `sid:activeSid` makes the rotated scenario deliver tag 'hermes-parent'
    instead of 'hermes-continuation', failing this test.
    """
    res = subprocess.run(
        [
            "node",
            "-e",
            _ROTATED_DONE_HARNESS,
            str(ROOT / "static" / "messages.js"),
            str(ROOT / "static" / "sessions.js"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())

    rotated = out["rotated"]
    assert "error" not in rotated, rotated.get("error")
    assert rotated["count"] == 1, "rotated done must deliver exactly one notification"
    assert rotated["sessionAfterDone"] == "continuation", (
        "done handler must settle onto the continuation session"
    )
    delivery = rotated["captured"][0]
    assert delivery["title"] == "Response complete"
    assert delivery["tag"] == "hermes-continuation", (
        "notification tag must point at the CONTINUATION session, not the "
        "archived parent (got " + repr(delivery["tag"]) + ")"
    )
    assert delivery["url"] == rotated["expectedUrl"], (
        "notification click URL must equal location.origin + "
        "_sessionUrlForSid('continuation')"
    )
    assert rotated["expectedUrl"] == "http://localhost:8787/session/continuation"

    non_rotated = out["nonRotated"]
    assert "error" not in non_rotated, non_rotated.get("error")
    assert non_rotated["count"] == 1
    assert non_rotated["captured"][0]["tag"] == "hermes-parent"
    assert non_rotated["captured"][0]["url"] == non_rotated["expectedUrl"]
    assert non_rotated["expectedUrl"] == "http://localhost:8787/session/parent"

    disabled = out["disabled"]
    assert "error" not in disabled, disabled.get("error")
    assert disabled["count"] == 0, "notifications-disabled must not deliver"

    watched = out["neverBackgrounded"]
    assert "error" not in watched, watched.get("error")
    assert watched["count"] == 0, (
        "a stream the user watched (never hidden/backgrounded) must not deliver"
    )
