"""Focused page and producer regressions for #6673."""

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")


def _notification_region() -> str:
    return MESSAGES_JS[MESSAGES_JS.index("function _notificationOptions"):MESSAGES_JS.index("// ── /btw ephemeral stream")]


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        return {}
    temporary_script = None
    if len(script) > 7000:
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", delete=False) as handle:
            handle.write(script)
            temporary_script = Path(handle.name)
        command = [node, str(temporary_script)]
    else:
        command = [node, "-e", script]
    try:
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    finally:
        if temporary_script is not None:
            temporary_script.unlink(missing_ok=True)


def test_identity_capture_uses_only_canonical_journal_ids():
    capture = extract_function(MESSAGES_JS, "_captureNotificationEventIdentity")
    assert "return null" in capture
    assert "_notificationEventFallbackId" not in MESSAGES_JS
    assert "event.lastEventId" in capture
    assert "identity.length>_NOTIFICATION_IDENTITY_MAX_LENGTH" in capture


def test_identity_capture_preserves_opaque_event_id_bytes():
    capture = extract_function(MESSAGES_JS, "_captureNotificationEventIdentity")
    script = f"""
const _NOTIFICATION_IDENTITY_MAX_LENGTH = 512;
const capture = {capture};
console.log(JSON.stringify({{
  raw: capture('stream-6673', {{lastEventId:' opaque-event '}}),
  delivery: capture('stream-6673', {{lastEventId:''}}, {{notification_event_id:'stream-6673:delivery:1'}}),
}}));
"""
    assert _run_node(script) == {
        "raw": {"streamId": "stream-6673", "lastEventId": " opaque-event "},
        "delivery": {"streamId": "stream-6673", "lastEventId": "stream-6673:delivery:1"},
    }


def test_transition_registration_lookup_is_deadline_bounded():
    helper = extract_function(MESSAGES_JS, "_registrationAfterNotificationTransition")
    script = f"""
const _registrationAfterNotificationTransition = {helper};
const navigator = {{serviceWorker: {{getRegistration: () => new Promise(() => {{}})}}}};
const started = Date.now();
_registrationAfterNotificationTransition({{state:'activated'}}).then(value =>
  console.log(JSON.stringify({{value, elapsed: Date.now() - started}}))
);
"""
    result = _run_node(script)
    assert result["value"] is None
    assert 1900 <= result["elapsed"] < 2500


def test_registration_first_presenter_covers_rows_one_through_thirty_four():
    identity = {"streamId": "stream-6673", "lastEventId": "stream-6673:1"}
    cases = [
        {"name": "1-exact-duplicate", "worker": "duplicate", "records": [identity], "expected": "duplicate", "registration": 0, "direct": 0},
        {"name": "2-worker-shown", "worker": "shown", "expected": "shown", "registration": 0, "direct": 0},
        {"name": "3-worker-invalid-registration", "worker": "invalid", "expected": "shown", "registration": 1, "direct": 0},
        {"name": "4-worker-invalid-constructor", "worker": "invalid", "registrationAvailable": False, "expected": "shown", "registration": 0, "direct": 1},
        {"name": "5-nonterminal-exact", "worker": "invalid", "records": [identity], "expected": "duplicate", "registration": 0, "direct": 0},
        {"name": "6-timeout-exact", "worker": "timeout", "records": [identity], "expected": "duplicate", "registration": 0, "direct": 0},
        {"name": "7-timeout-registration", "worker": "timeout", "expected": "shown", "registration": 1, "direct": 0},
        {"name": "8-timeout-constructor", "worker": "timeout", "registrationAvailable": False, "expected": "shown", "registration": 0, "direct": 1},
        {"name": "9-displayed-records-reject", "worker": "invalid", "recordsReject": True, "expected": "ambiguous", "registration": 0, "direct": 0},
        {"name": "10-no-message-channel", "channel": False, "expected": "shown", "registration": 1, "direct": 0},
        {"name": "11-post-message-throws", "postThrow": True, "expected": "shown", "registration": 1, "direct": 0},
        {"name": "12-registration-inactive", "worker": "invalid", "inactive": True, "expected": "shown", "registration": 0, "direct": 1},
        {"name": "13-registration-rejects", "registrationReject": True, "expected": "shown", "registration": 0, "direct": 1},
        {"name": "14-no-service-worker", "noServiceWorker": True, "expected": "shown", "registration": 0, "direct": 1},
        {"name": "15-registration-only", "worker": "invalid", "channel": False, "constructorAvailable": False, "expected": "shown", "registration": 1, "direct": 0},
        {"name": "16-both-unavailable", "registrationAvailable": False, "constructorAvailable": False, "expected": "ambiguous", "registration": 0, "direct": 1},
        {"name": "17-registration-show-rejects", "worker": "invalid", "registrationShowReject": True, "expected": "ambiguous", "registration": 1, "direct": 0},
        {"name": "18-distinct-event", "worker": "invalid", "records": [{"streamId": "stream-6673", "lastEventId": "stream-6673:older"}], "expected": "shown", "registration": 1, "direct": 0},
        {"name": "19-delivery-only-identity", "worker": "invalid", "identityMode": "delivery", "expected": "shown", "registration": 1, "direct": 0},
        {"name": "20-malformed-registration", "identityMode": "invalid", "expected": "shown", "registration": 1, "direct": 0},
        {"name": "21-malformed-constructor", "identityMode": "invalid", "registrationAvailable": False, "expected": "shown", "registration": 0, "direct": 1},
        {"name": "22-manual-unkeyed", "unkeyed": True, "expected": "shown", "registration": 1, "direct": 0},
        {"name": "23-sender-foreground", "sender": True, "expected": "blocked", "registration": 0, "direct": 0},
        {"name": "24-capable-worker", "worker": "shown", "expected": "shown", "expectedTerminal": "worker", "registration": 0, "direct": 0},
        {"name": "25-degraded-two-context-shared-owner", "twoContexts": True, "channel": False, "constructorAvailable": False, "expected": "shown", "registration": 1, "direct": 0},
        {"name": "26-malformed-inactive-registration", "identityMode": "invalid", "inactive": True, "expected": "shown", "registration": 0, "direct": 1},
        {"name": "27-sender-registration-without-page-interface", "sender": True, "force": True, "channel": False, "notificationInterface": False, "constructorAvailable": False, "expected": "shown", "registration": 1, "direct": 0},
        {"name": "28-sender-disabled", "sender": True, "notificationsEnabled": False, "expected": "blocked", "registration": 0, "direct": 0},
        {"name": "29-sender-denied", "sender": True, "permission": "denied", "expected": "blocked", "registration": 0, "direct": 0},
        {"name": "30-registration-present-missing-indexeddb", "worker": "invalid", "channel": False, "indexedDBAvailable": False, "constructorAvailable": False, "expected": "shown", "registration": 1, "direct": 0},
        {"name": "31-no-worker-no-indexeddb-boundary", "channel": False, "indexedDBAvailable": False, "registrationAvailable": False, "constructorAvailable": False, "ownerBoundary": "no-shared-owner", "expected": "ambiguous", "registration": 0, "direct": 1},
        {"name": "32-owner-storage-request-error", "worker": "invalid", "indexedDBMode": "request-error", "expected": "ambiguous", "registration": 0, "direct": 0},
        {"name": "33-owner-storage-upgrade-abort", "worker": "invalid", "indexedDBMode": "upgrade-abort", "expected": "ambiguous", "registration": 0, "direct": 0},
        {"name": "34-owner-storage-blocked", "worker": "invalid", "indexedDBMode": "blocked", "expected": "ambiguous", "registration": 0, "direct": 0},
    ]
    notification_region = _notification_region()
    driver = textwrap.dedent(
        """
        async function runCase(config) {
          if (config.twoContexts) {
            const ownerRows = new Map();
            const transactionQueue = [];
            let transactionActive = false;
            const ownerKey = key => JSON.stringify(key);
            const pumpTransaction = () => {
              if (transactionActive || !transactionQueue.length) return;
              transactionActive = true;
              const {tx, request, key} = transactionQueue.shift();
              queueMicrotask(() => {
                request.result = ownerRows.get(ownerKey(key));
                request.onsuccess?.();
                queueMicrotask(() => {
                  if (tx.write?.kind === 'put') ownerRows.set(ownerKey([tx.write.value.streamId, tx.write.value.lastEventId]), tx.write.value);
                  if (tx.write?.kind === 'delete') ownerRows.delete(ownerKey(tx.write.key));
                  tx.oncomplete?.();
                  transactionActive = false;
                  pumpTransaction();
                });
              });
            };
            const ownerDb = {
              objectStoreNames: {contains: () => true},
              close() {},
              transaction() {
                const tx = {write: null};
                const store = {
                  get(key) {
                    const request = {};
                    transactionQueue.push({tx, request, key});
                    pumpTransaction();
                    return request;
                  },
                  put(value) { tx.write = {kind: 'put', value}; },
                  delete(key) { tx.write = {kind: 'delete', key}; },
                };
                tx.objectStore = () => store;
                return tx;
              },
            };
            const indexedDB = {
              open() {
                const request = {};
                queueMicrotask(() => { request.result = ownerDb; request.onupgradeneeded?.(); request.onsuccess?.(); });
                return request;
              },
            };
            const makeContext = state => {
              const registration = {
                active: {postMessage() {}},
                getNotifications: async () => [],
                showNotification: async () => { state.registrationDisplays += 1; },
              };
              const Notification = function () {
                state.constructorAttempts += 1;
                if (config.constructorAvailable === false) throw new Error('constructor unavailable');
                return {};
              };
              Notification.permission = 'granted';
              const context = {
                Promise, Date, Math, JSON, console, setTimeout, clearTimeout, queueMicrotask,
                Notification,
                window: {MessageChannel: undefined, indexedDB, _notificationsEnabled: true, Notification},
                document: {hidden: false, visibilityState: 'visible', hasFocus: () => true},
                navigator: {serviceWorker: {getRegistration: () => Promise.resolve(registration)}},
                location: {origin: 'https://webui.test', href: 'https://webui.test/'},
                S: {session: {session_id: 'session-6673'}},
                _sessionUrlForSid: sid => '/?session=' + sid,
                assistantDisplayName: () => 'Hermes',
                _isBackgroundedForBrowserNotification: () => false,
              };
              vm.createContext(context);
              vm.runInContext(BLOCK, context);
              return context;
            };
            const states = [{registrationDisplays: 0, constructorAttempts: 0}, {registrationDisplays: 0, constructorAttempts: 0}];
            const contexts = states.map(makeContext);
            const options = {eventIdentity: {streamId: 'stream-6673', lastEventId: 'stream-6673:1'}};
            const statuses = await Promise.all(contexts.map(context => context._showPwaNotification('Hermes', 'body', options)));
            return {
              name: config.name,
              status: statuses.includes('shown') ? 'shown' : statuses[0],
              statuses,
              registrationDisplays: states.reduce((total, state) => total + state.registrationDisplays, 0),
              constructorAttempts: states.reduce((total, state) => total + state.constructorAttempts, 0),
              ownerEvents: [...ownerRows.values()],
              ownerBoundary: 'shared-owner',
            };
          }
          const state = {registrationDisplays: 0, constructorAttempts: 0};
          const ownerRows = new Map();
          const ownerKey = key => JSON.stringify(key);
          const ownerDb = {
            objectStoreNames: {contains: () => true},
            close() {},
            transaction() {
              const tx = {};
              const store = {
                get(key) {
                  const request = {};
                  queueMicrotask(() => { request.result = ownerRows.get(ownerKey(key)); request.onsuccess?.(); });
                  return request;
                },
                put(value) {
                  ownerRows.set(ownerKey([value.streamId, value.lastEventId]), value);
                  queueMicrotask(() => tx.oncomplete?.());
                },
                delete(key) {
                  ownerRows.delete(ownerKey(key));
                  queueMicrotask(() => tx.oncomplete?.());
                },
              };
              tx.objectStore = () => store;
              return tx;
            },
          };
          const indexedDB = {
            open() {
              const request = {};
              queueMicrotask(() => {
                if (config.indexedDBMode === 'request-error') { request.error = new Error('owner storage request failed'); request.onerror?.(); return; }
                if (config.indexedDBMode === 'blocked') { request.onblocked?.(); return; }
                request.result = ownerDb;
                request.onupgradeneeded?.();
                if (config.indexedDBMode === 'upgrade-abort') { request.result = undefined; request.error = new Error('owner storage upgrade aborted'); request.onerror?.(); return; }
                request.onsuccess?.();
              });
              return request;
            },
          };
          class Port {
            constructor() { this.peer = null; this.onmessage = null; }
            postMessage(data) { queueMicrotask(() => this.peer?.onmessage?.({data})); }
            close() {}
            start() {}
          }
          class MessageChannel {
            constructor() { this.port1 = new Port(); this.port2 = new Port(); this.port1.peer = this.port2; this.port2.peer = this.port1; }
          }
          const identity = config.identityMode === 'delivery'
            ? {streamId:'stream-6673', lastEventId:'stream-6673:delivery:1'}
            : {streamId:'stream-6673', lastEventId:'stream-6673:1'};
          const registration = {
            active: config.inactive ? null : {postMessage(_data, ports) {
              if (config.postThrow) throw new Error('post failed');
              if (config.worker !== 'timeout') {
                state.terminalPath = 'worker';
                queueMicrotask(() => ports?.[0]?.postMessage({status: config.worker || 'invalid'}));
              }
            }},
            getNotifications: async () => {
              if (config.recordsReject) throw new Error('records unavailable');
              return (config.records || []).map(record => ({data: {eventId: record.lastEventId}}));
            },
            showNotification: async () => {
              state.registrationDisplays += 1;
              if (config.registrationShowReject) throw new Error('registration display failed');
            },
          };
              const Notification = function () {
                state.constructorAttempts += 1;
                if (config.constructorAvailable === false) throw new Error('constructor unavailable');
                return {};
              };
              Notification.permission = config.permission || 'granted';
          const worker = config.noServiceWorker ? undefined : {
            getRegistration: () => config.registrationReject ? Promise.reject(new Error('registration unavailable')) : Promise.resolve(config.registrationAvailable === false ? null : registration),
          };
          const windowObject = {
                MessageChannel: config.channel === false ? undefined : MessageChannel,
                _notificationsEnabled: config.notificationsEnabled !== false,
                ...(config.notificationInterface === false ? {} : {Notification}),
          };
          if (config.indexedDBAvailable !== false) windowObject.indexedDB = indexedDB;
          const context = {
            Promise, Date, Math, JSON, console, setTimeout, clearTimeout, queueMicrotask,
            ...(config.notificationInterface === false ? {} : {Notification}),
                window: windowObject,
                document: {hidden: config.hidden === true, visibilityState: config.hidden === true ? 'hidden' : 'visible', hasFocus: () => true},
            navigator: {serviceWorker: worker},
            location: {origin:'https://webui.test', href:'https://webui.test/'},
            S: {session: {session_id:'session-6673'}},
                _sessionUrlForSid: sid => '/?session=' + sid,
                assistantDisplayName: () => 'Hermes',
                _isBackgroundedForBrowserNotification: () => false,
          };
          vm.createContext(context);
          vm.runInContext(BLOCK, context);
          const options = config.unkeyed ? {} : {eventIdentity: config.identityMode === 'invalid' ? {streamId:'', lastEventId:''} : identity};
          try {
            const status = await (config.sender
                  ? context.sendBrowserNotification('Hermes', 'body', {...options, force:config.force === true})
                  : context._showPwaNotification('Hermes', 'body', options));
                return {name: config.name, status: status === undefined ? 'blocked' : status, registrationDisplays: state.registrationDisplays, constructorAttempts: state.constructorAttempts, terminalPath: state.terminalPath, ownerBoundary: config.ownerBoundary};
          } catch (error) {
            return {name: config.name, status:'rejected', registrationDisplays: state.registrationDisplays, constructorAttempts: state.constructorAttempts, error:String(error.message || error)};
          }
        }
        const results = [];
        for (const config of CASES) results.push(await runCase(config));
        console.log(JSON.stringify(results));
        """
    )
    script = f"const vm = require('vm');\nconst BLOCK = {json.dumps(notification_region)};\nconst CASES = {json.dumps(cases)};\n(async () => {{\n{driver}\n}})().catch(error => {{ console.error(error.stack || error); process.exitCode = 1; }});"
    results = _run_node(script)
    assert results
    for observed, expected in zip(results, cases, strict=True):
        assert observed["name"] == expected["name"]
        assert observed["status"] == expected["expected"], observed
        assert observed["registrationDisplays"] == expected["registration"], observed
        assert observed["constructorAttempts"] == expected["direct"], observed
        if expected.get("expectedTerminal"):
            assert observed["terminalPath"] == expected["expectedTerminal"], observed
        if expected.get("ownerBoundary"):
            assert observed["ownerBoundary"] == expected["ownerBoundary"], observed
        if expected.get("twoContexts"):
            assert sorted(observed["statuses"]) == ["ambiguous", "shown"], observed
            assert len(observed["ownerEvents"]) == 1, observed
            assert observed["ownerEvents"][0]["streamId"] == "stream-6673", observed
            assert observed["ownerEvents"][0]["lastEventId"] == "stream-6673:1", observed
            assert observed["ownerEvents"][0]["state"] == "delivered", observed
    assert "indexedDB.open(" in MESSAGES_JS
    assert "_claimPageNotification" in MESSAGES_JS
    assert "hermes-notification-failures" in MESSAGES_JS


def test_producers_leave_delivery_frames_without_journal_ids_unkeyed():
    streaming = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    gateway = (ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")
    assert "fallback_event_seq" not in streaming
    assert "fallback_event_seq" not in gateway
    assert ":fallback:" not in streaming
    assert ":fallback:" not in gateway
    assert "queue_item = (event, data, event_id) if hasattr(q, \"subscribe_with_snapshot\")" in streaming
    assert "queue_item = (event, data, event_id) if hasattr(q, \"subscribe_with_snapshot\")" in gateway


def test_journal_failure_keeps_notification_delivery_identity_out_of_sse_cursor():
    streaming = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
    gateway = (ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")
    assert 'data["notification_event_id"]' in streaming
    assert 'data["notification_event_id"]' in gateway
    assert 'f"{stream_id}:delivery:{_delivery_event_seq}"' in streaming
    assert 'f"{stream_id}:delivery:{delivery_event_seq}"' in gateway
    assert "notification_event_id" in MESSAGES_JS
    assert "_captureNotificationEventIdentity(streamId,e,d)" in MESSAGES_JS


def test_notification_present_protocol_carries_canonical_event_data():
    assert "protocolVersion:1" in MESSAGES_JS
    assert "eventId:identity.lastEventId" in MESSAGES_JS
    assert "data:{url,..." in MESSAGES_JS


def test_message_channel_uses_window_owned_global():
    assert "typeof window.MessageChannel!=='function'" in MESSAGES_JS
    assert "channel=new window.MessageChannel()" in MESSAGES_JS


def test_page_and_worker_share_the_same_event_identity_key():
    assert "eventId:identity.lastEventId" in MESSAGES_JS
    assert "data: {url, eventId}" in SW_JS
    assert "record.data.eventId===identity.lastEventId" in MESSAGES_JS


def test_journal_less_sse_frames_reset_sticky_eventsource_ids():
    assert "def _sse_with_reset_id" in ROUTES_PY
    session_start = ROUTES_PY.index("def _handle_session_run_journal_stream_for_session")
    session_end = ROUTES_PY.find("\ndef ", session_start + 1)
    session_handler = ROUTES_PY[session_start:session_end if session_end >= 0 else None]
    assert "event_id = queued_event_id if has_queued_event_id else" in session_handler
    assert "_sse_with_reset_id(handler, event, data)" in session_handler


def test_worker_presentation_queues_release_settled_tag_state():
    assert "const trackedOperation = operation.catch(() => {});" in SW_JS
    assert "notificationPresentationByTag.delete(tag);" in SW_JS


def test_replay_cursor_accepts_only_current_canonical_positive_ids():
    cursor = extract_function(MESSAGES_JS, "_rememberRunJournalCursor")
    script = f"""
const cursor = {cursor};
let _lastRunJournalSeq = 0;
let _lastRunJournalEventId = '';
const streamId = 'stream-6673';
const activeSid = 'session-6673';
const INFLIGHT = {{[activeSid]: {{}}}};
const _throttledPersist = () => {{}};
for (const value of ['stream-6673:2', 'stream-6673:fallback:3', 'other:4', 'stream-6673:-1', 'stream-6673:NaN', 'stream-6673:4']) {{
  cursor({{lastEventId: value}});
}}
console.log(JSON.stringify({{seq:_lastRunJournalSeq, id:_lastRunJournalEventId}}));
"""
    assert _run_node(script) == {"seq": 4, "id": "stream-6673:4"}
