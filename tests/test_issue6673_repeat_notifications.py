"""Focused page and producer regressions for #6673."""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")


def test_identity_capture_uses_only_canonical_journal_ids():
    capture = extract_function(MESSAGES_JS, "_captureNotificationEventIdentity")
    assert "return null" in capture
    assert "_notificationEventFallbackId" not in MESSAGES_JS
    assert "event.lastEventId" in capture
    assert "identity.length>_NOTIFICATION_IDENTITY_MAX_LENGTH" in capture


def test_identity_capture_preserves_opaque_event_id_bytes():
    node = shutil.which("node")
    if node is None:
        return
    capture = extract_function(MESSAGES_JS, "_captureNotificationEventIdentity")
    script = f"""
const _NOTIFICATION_IDENTITY_MAX_LENGTH = 512;
const capture = {capture};
console.log(JSON.stringify({{
  raw: capture('stream-6673', {{lastEventId:' opaque-event '}}),
  delivery: capture('stream-6673', {{lastEventId:''}}, {{notification_event_id:'stream-6673:delivery:1'}}),
}}));
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "raw": {"streamId": "stream-6673", "lastEventId": " opaque-event "},
        "delivery": {"streamId": "stream-6673", "lastEventId": "stream-6673:delivery:1"},
    }


def test_page_notification_path_has_shared_owner_and_keeps_delivery_fallback():
    assert "sw.js?v=__WEBUI_VERSION__&notification_protocol=1" in INDEX_HTML
    assert "indexedDB.open(" in MESSAGES_JS
    assert "_NOTIFICATION_OWNER_STORE='event-identities'" in MESSAGES_JS
    assert "keyPath:['streamId','lastEventId']" in MESSAGES_JS
    assert "return [identity.streamId,identity.lastEventId]" in MESSAGES_JS
    assert "state:'pending'" in MESSAGES_JS
    assert "phase:'claimed'" in MESSAGES_JS
    assert "_markPageNotificationDisplaying" in MESSAGES_JS
    assert "phase:'displaying'" in MESSAGES_JS
    assert "current.phase!=='claimed'" in MESSAGES_JS
    assert "_notificationFailedPageKeys" in MESSAGES_JS
    assert "window.BroadcastChannel" in MESSAGES_JS
    assert "function _ensureNotificationFailureChannel()" in MESSAGES_JS
    assert "_ensureNotificationFailureChannel();" in MESSAGES_JS
    assert "_recordFailedPageNotification" in MESSAGES_JS
    assert "current.token===failedToken" in MESSAGES_JS
    assert "type:'failed',key,token:failureToken" in MESSAGES_JS
    assert "state==='delivered'" in MESSAGES_JS
    assert "phase:'delivered'" in MESSAGES_JS
    assert "expiresAt" in MESSAGES_JS
    assert "_settlePageNotification" in MESSAGES_JS
    assert "_releasePageNotification" in MESSAGES_JS
    assert "attempts>=2" in MESSAGES_JS
    assert "state:'failed'" in MESSAGES_JS
    assert "_settlePageNotification(identity,token,'failed')" in MESSAGES_JS
    assert "hermes.notification.present" in MESSAGES_JS
    assert "_displayedNotificationMatches" in MESSAGES_JS
    assert "_workerSupportsNotificationPresentation" in MESSAGES_JS
    assert "notification_protocol=1" in MESSAGES_JS
    assert "fetch(active.scriptURL" not in MESSAGES_JS
    assert "_reconcileWorkerTimeout" in MESSAGES_JS
    assert "status==='shown'||status==='duplicate'?status:" in MESSAGES_JS
    assert "renotify:true" in MESSAGES_JS


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
    assert "eventId:identity.lastEventId" in MESSAGES_JS


def test_message_channel_uses_window_owned_global():
    assert "typeof window.MessageChannel!=='function'" in MESSAGES_JS
    assert "channel=new window.MessageChannel()" in MESSAGES_JS


def test_page_owner_covers_registration_reply_and_storage_failure_modes():
    assert "if(!reg||!reg.active)return deliverDirect(null);" in MESSAGES_JS
    assert "_presentNotification(reg.active" in MESSAGES_JS
    assert "_displayedNotificationMatches(reg,opts,identity)" in MESSAGES_JS
    assert "capable===false?_deliverPageNotification" in MESSAGES_JS
    assert "request.onblocked=()=>reject" in MESSAGES_JS
    assert "tx.onabort=()=>finish('ambiguous')" in MESSAGES_JS
    assert "current.phase!=='claimed'||Number(current.expiresAt)>Date.now()" in MESSAGES_JS
    assert "current.state==='pending'&&!failed" in MESSAGES_JS
    assert "_releasePageNotification(identity,claim.token)" in MESSAGES_JS


def test_page_and_worker_share_the_same_event_identity_key():
    assert "eventId:identity.lastEventId" in MESSAGES_JS
    assert "data: {url, eventId}" in SW_JS
    assert "record.data.eventId===identity.lastEventId" in MESSAGES_JS


def test_page_owner_allows_one_native_display_and_retries_constructor_failure():
    node = shutil.which("node")
    if node is None:
        return
    notification_block = MESSAGES_JS[MESSAGES_JS.index("function _notificationOptions"):MESSAGES_JS.index("function requestNotificationPermission")]
    driver = textwrap.dedent(
        """
        (async () => {
          const identity = {streamId:'stream-6673', lastEventId:'stream-6673:1'};
          const nextIdentity = {streamId:'stream-6673', lastEventId:'stream-6673:2'};
          const failedIdentity = {streamId:'stream-6673', lastEventId:'stream-6673:3'};
          const options = value => _notificationOptions('body', {sid:'session-6673', eventIdentity:value});
          const sameIdentity = await Promise.all([
            _deliverPageNotification('Hermes', 'body', options(identity), identity, null),
            _deliverPageNotification('Hermes', 'body', options(identity), identity, null),
          ]);
          const next = await _deliverPageNotification('Hermes', 'body', options(nextIdentity), nextIdentity, null);
          const failed = await _deliverPageNotification('Hermes', 'body', options(failedIdentity), failedIdentity, null);
          const retry = await _deliverPageNotification('Hermes', 'body', options(failedIdentity), failedIdentity, null);
          const timeoutStatus = await _reconcileWorkerTimeout(
            {scriptURL:'https://webui.test/sw.js?notification_protocol=1'},
            {getNotifications: async () => []},
            options(identity), identity, 'Hermes', 'body'
          );
          const legacyTimeoutIdentity = {streamId:'stream-6673', lastEventId:'stream-6673:4'};
          const legacyTimeout = await _reconcileWorkerTimeout(
            {scriptURL:'https://webui.test/sw.js'},
            {getNotifications: async () => []},
            options(legacyTimeoutIdentity), legacyTimeoutIdentity, 'Hermes', 'body'
          );
          const persistedFailureIdentity = {streamId:'stream-6673', lastEventId:'stream-6673:5'};
          notificationState.releaseFailures = 3;
          const persistedFailure = await _deliverPageNotification('Hermes', 'body', options(persistedFailureIdentity), persistedFailureIdentity, null);
          _notificationFailedPageKeys.clear();
          const persistedRetry = await _deliverPageNotification('Hermes', 'body', options(persistedFailureIdentity), persistedFailureIdentity, null);
          const raceIdentity = {streamId:'stream-6673', lastEventId:'stream-6673:6'};
          rows.set(JSON.stringify([raceIdentity.streamId, raceIdentity.lastEventId]), {streamId:raceIdentity.streamId, lastEventId:raceIdentity.lastEventId, state:'pending', phase:'displaying', token:'new-token', expiresAt:0});
          _recordFailedPageNotification(raceIdentity, 'stale-token');
          const staleMarker = await _claimPageNotification(raceIdentity);
          rows.delete(JSON.stringify([raceIdentity.streamId, raceIdentity.lastEventId]));
          _notificationFailedPageKeys.clear();
          console.log(JSON.stringify({sameIdentity, next, failed, retry, timeoutStatus, legacyTimeout, persistedFailure, persistedRetry, staleMarker, attempts:notificationState.attempts, rows:[...rows.values()]}));
        })().catch(error => { console.error(error.stack || error); throw error; });
        """
    )
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const rows = new Map();
        const reserved = new Set();
        const mapKey = key => JSON.stringify(key);
        const notificationState = {{attempts:0, failFirstDisplay:true, failPersistedDisplay:true, releaseFailures:0}};
        function makeDb() {{
          const db = {{
            objectStoreNames: {{contains: () => true}},
            transaction() {{
              const tx = {{oncomplete:null, onerror:null, onabort:null, error:null}};
              const store = {{
                get(key) {{
                  const request = {{result:undefined, onsuccess:null, onerror:null}};
                  setTimeout(() => {{
                    request.result = rows.get(mapKey(key));
                    if (!request.result && reserved.has(mapKey(key))) request.result = {{state:'pending', expiresAt:Date.now()+10000}};
                    if (!request.result) reserved.add(mapKey(key));
                    request.onsuccess?.({{target:request}});
                  }}, 0);
                  return request;
                }},
                put(row) {{ const key = [row.streamId, row.lastEventId]; rows.set(mapKey(key), row); reserved.add(mapKey(key)); }},
                delete(key) {{
                  if (notificationState.releaseFailures > 0) {{ notificationState.releaseFailures -= 1; throw new Error('simulated release failure'); }}
                  rows.delete(mapKey(key)); reserved.delete(mapKey(key));
                }},
              }};
              setTimeout(() => tx.oncomplete?.(), 20);
              tx.objectStore = () => store;
              return tx;
            }},
            close() {{}},
          }};
          return db;
        }}
        const indexedDB = {{
          open() {{
            const request = {{result:makeDb(), onupgradeneeded:null, onsuccess:null, onerror:null, onblocked:null}};
            setTimeout(() => {{ request.onupgradeneeded?.({{target:request}}); request.onsuccess?.({{target:request}}); }}, 0);
            return request;
          }},
        }};
        const context = {{
          Promise, Date, Math, JSON, console, setTimeout, clearTimeout,
          indexedDB, rows,
          notificationState,
          fetch: async () => {{ throw new Error('capability source fetch is forbidden'); }},
          Notification: function(title, options) {{
            notificationState.attempts += 1;
            if (options.data.eventId === 'stream-6673:3' && notificationState.failFirstDisplay) {{
              notificationState.failFirstDisplay = false;
              throw new Error('simulated constructor failure');
            }}
            if (options.data.eventId === 'stream-6673:5' && notificationState.failPersistedDisplay) {{
              notificationState.failPersistedDisplay = false;
              throw new Error('simulated persistent constructor failure');
            }}
            return {{title, options}};
          }},
          window: {{indexedDB}},
          navigator: {{serviceWorker: {{getRegistration: () => Promise.resolve(null)}}}},
          location: {{origin:'https://webui.test', href:'https://webui.test/'}},
          S: {{session: {{session_id:'session-6673'}}}},
          _sessionUrlForSid: sid => '/?session=' + sid,
          assistantDisplayName: () => 'Hermes',
        }};
        vm.runInNewContext({json.dumps(notification_block + driver)}, context);
        """
    )
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert sorted(observed["sameIdentity"]) == ["ambiguous", "shown"], observed
    assert observed["next"] == "shown", observed
    assert observed["failed"] == "ambiguous", observed
    assert observed["retry"] == "shown", observed
    assert observed["timeoutStatus"] == "ambiguous", observed
    assert observed["legacyTimeout"] == "shown", observed
    assert observed["persistedFailure"] == "ambiguous", observed
    assert observed["persistedRetry"] == "shown", observed
    assert observed["staleMarker"]["status"] == "ambiguous", observed
    assert observed["attempts"] == 7, observed
    assert {row["lastEventId"] for row in observed["rows"]} == {"stream-6673:1", "stream-6673:2", "stream-6673:3", "stream-6673:4", "stream-6673:5"}
    assert all(row["state"] == "delivered" and row["phase"] == "delivered" for row in observed["rows"]), observed


def test_page_owner_attempts_once_when_owner_storage_is_unavailable():
    node = shutil.which("node")
    if node is None:
        return
    notification_block = MESSAGES_JS[MESSAGES_JS.index("function _notificationOptions"):MESSAGES_JS.index("function requestNotificationPermission")]
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const identity = {{streamId:'stream-6673', lastEventId:'stream-6673:unavailable-storage'}};
        const requestErrorIdentity = {{streamId:'stream-6673', lastEventId:'stream-6673:request-error'}};
        const upgradeAbortIdentity = {{streamId:'stream-6673', lastEventId:'stream-6673:upgrade-abort'}};
        const blockedIdentity = {{streamId:'stream-6673', lastEventId:'stream-6673:blocked'}};
        const notificationState = {{attempts:0}};
        const options = _options => _notificationOptions('body', {{sid:'session-6673', eventIdentity:_options}});
        const context = {{
          Promise, Date, Math, JSON, console, setTimeout, clearTimeout,
          notificationState,
          Notification: function(title, options) {{
            notificationState.attempts += 1;
            return {{title, options}};
          }},
          window: {{}},
          navigator: {{serviceWorker: {{getRegistration: () => Promise.resolve(null)}}}},
          location: {{origin:'https://webui.test', href:'https://webui.test/'}},
          S: {{session: {{session_id:'session-6673'}}}},
          _sessionUrlForSid: sid => '/?session=' + sid,
          assistantDisplayName: () => 'Hermes',
        }};
        vm.runInNewContext({json.dumps(notification_block)}, context);
        context._showPwaNotification('Hermes', 'body', {{eventIdentity:identity}})
          .then(firstStatus => {{
            context.window.indexedDB = {{
              open: () => {{
                const request = {{result:undefined, error:new Error('storage unavailable'), onupgradeneeded:null, onsuccess:null, onerror:null, onblocked:null}};
                setTimeout(() => request.onerror?.({{target:request}}), 0);
                return request;
              }},
            }};
            return context._showPwaNotification('Hermes', 'body', {{eventIdentity:requestErrorIdentity}})
              .then(secondStatus => {{
                context.window.indexedDB = {{
                  open: () => {{
                    const request = {{
                      result: {{objectStoreNames: {{contains: () => false}}, createObjectStore: () => {{}}}},
                      error: new Error('AbortError'),
                      onupgradeneeded:null, onsuccess:null, onerror:null, onblocked:null,
                    }};
                    setTimeout(() => {{
                      request.onupgradeneeded?.({{target:request}});
                      request.result = undefined;
                      request.onerror?.({{target:request}});
                    }}, 0);
                    return request;
                  }},
                }};
                return context._showPwaNotification('Hermes', 'body', {{eventIdentity:upgradeAbortIdentity}})
                  .then(thirdStatus => {{
                    context.window.indexedDB = {{
                      open: () => {{
                        const request = {{result:undefined, error:null, onupgradeneeded:null, onsuccess:null, onerror:null, onblocked:null}};
                        setTimeout(() => request.onblocked?.({{target:request}}), 0);
                        return request;
                      }},
                    }};
                    return context._showPwaNotification('Hermes', 'body', {{eventIdentity:blockedIdentity}})
                      .then(fourthStatus => console.log(JSON.stringify({{firstStatus, secondStatus, thirdStatus, fourthStatus, attempts:notificationState.attempts}})));
                  }});
              }});
          }})
          .catch(error => {{ console.error(error.stack || error); process.exitCode = 1; }});
        """
    )
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {"firstStatus": "shown", "secondStatus": "shown", "thirdStatus": "ambiguous", "fourthStatus": "ambiguous", "attempts": 2}, observed


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
    node = shutil.which("node")
    if node is None:
        return
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
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"seq": 4, "id": "stream-6673:4"}
