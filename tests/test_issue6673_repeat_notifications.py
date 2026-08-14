"""Behavioral coverage for repeated same-session notification replacement (#6673)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "issue6673_repeat_notification.json").read_text(
        encoding="utf-8",
    ),
)
NODE = shutil.which("node")


def _notification_helper_source() -> str:
    start = MESSAGES_JS.index("function _notificationOptions")
    end = MESSAGES_JS.index("function requestNotificationPermission", start)
    return MESSAGES_JS[start:end]


def _run_node(script: str) -> dict:
    if NODE is None:
        pytest.skip("node executable is required for notification behavior checks")
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".js",
        encoding="utf-8",
        delete=False,
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, (
        f"notification behavior driver failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(result.stdout)


def _driver(
    *,
    delivery: str,
    sends: list[dict],
    permission: str = "granted",
    via_public_sender: bool = False,
) -> dict:
    notification_options = _notification_helper_source()
    show_notification = extract_function(MESSAGES_JS, "_showPwaNotification")
    request_permission = extract_function(MESSAGES_JS, "requestNotificationPermission")
    send_notification = extract_function(MESSAGES_JS, "sendBrowserNotification")
    registration = """\
const delivery = {calls: [], entries: new Map(), toasts: 0};
function recordNotification(method, title, options) {
  return Promise.resolve().then(() => {
    const existing = delivery.entries.has(options.tag);
    delivery.entries.set(options.tag, {title, options});
    if (!existing || options.renotify) delivery.toasts += 1;
    delivery.calls.push({method, title, options});
  });
}
"""
    delivery_setup = (
        """
navigator = {
  serviceWorker: {
    getRegistration: () => Promise.resolve({
      active: {},
      showNotification: (title, options) =>
        recordNotification('service-worker', title, options),
    }),
  },
};
"""
        if delivery == "service-worker"
        else (
            """
navigator = {
  serviceWorker: {
    getRegistration: () => Promise.resolve({
      active: null,
      showNotification: (title, options) =>
        recordNotification('service-worker', title, options),
    }),
  },
};
"""
            if delivery == "inactive-service-worker"
            else (
                """
navigator = {
  serviceWorker: {
    getRegistration: () => Promise.reject(new Error('registration rejected')),
  },
};
                """
                if delivery == "rejected-service-worker"
                else (
                    """
navigator = {
  serviceWorker: {
    getRegistration: () => Promise.resolve({
      active: {},
      showNotification: () => Promise.reject(new Error('showNotification rejected')),
    }),
  },
};
"""
                    if delivery == "rejected-show-notification"
                    else "navigator = {};"
                )
            )
        )
    )
    return _run_node(
        f"""
let navigator = {{}};
{notification_options}
{show_notification}
{request_permission}
{send_notification}
{registration}
globalThis.S = {{session: {{}}}};
globalThis.location = {{
  origin: 'https://webui.test',
  href: 'https://webui.test/',
}};
globalThis._sessionUrlForSid = sid => '/?session=' + encodeURIComponent(sid);
globalThis.assistantDisplayName = () => 'Hermes';
globalThis.window = {{_notificationsEnabled: true}};
globalThis._isBackgroundedForBrowserNotification = () => true;
globalThis.showToast = () => {{}};
globalThis.Notification = function(title, options) {{
  return recordNotification('direct', title, options);
}};
globalThis.Notification.permission = {json.dumps(permission)};
globalThis.Notification.requestPermission = () => Promise.resolve('granted');
globalThis.window.Notification = globalThis.Notification;
globalThis.t = key => key;
{delivery_setup}

(async () => {{
  for (const send of {json.dumps(sends)}) {{
    if ({json.dumps(via_public_sender)}) {{
      await sendBrowserNotification(send.title, send.body, {{...send.options, force: true}});
    }} else {{
      await _showPwaNotification(send.title, send.body, send.options);
    }}
  }}
  console.log(JSON.stringify({{
    calls: delivery.calls,
    toasts: delivery.toasts,
    entryCount: delivery.entries.size,
    tags: [...delivery.entries.keys()],
  }}));
}})().catch(error => {{
  console.error(error.stack || error);
  process.exitCode = 1;
}});
""",
    )


def _send(title: str, body: str, *, sid: str | None = FIXTURE["sid"]) -> dict:
    options = {} if sid is None else {"sid": sid}
    return {"title": title, "body": body, "options": options}


def _two_subscriber_driver(*, worker_response: str = "shown") -> dict:
    notification_options = _notification_helper_source()
    show_notification = extract_function(MESSAGES_JS, "_showPwaNotification")
    request_permission = extract_function(MESSAGES_JS, "requestNotificationPermission")
    send_notification = extract_function(MESSAGES_JS, "sendBrowserNotification")
    page_source = json.dumps(
        "\n".join(
            [notification_options, show_notification, request_permission, send_notification],
        ),
    )
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const attempts = [];
        const claims = new Set();
        const entries = new Map();
        let directFallbacks = 0;
        const workerResponse = {json.dumps(worker_response)};

        function recordNative(title, options, identity) {{
          attempts.push({{title, options, identity}});
          const existing = entries.has(options.tag);
          entries.set(options.tag, {{title, options}});
          return {{existing, renotify: options.renotify}};
        }}

        const worker = {{
          showNotification(title, options) {{
            recordNative(title, options, null);
            return Promise.resolve();
          }},
          postMessage(message, transfer) {{
            const port = transfer && transfer[0];
            if (!port || message.type !== 'hermes.notification.claim') return;
            const identity = message.identity;
            const key = JSON.stringify([identity.streamId, identity.lastEventId]);
            if (claims.has(key)) {{
              port.postMessage({{status: 'duplicate'}});
              return;
            }}
            claims.add(key);
            if (workerResponse === 'fallback-owner') {{
              port.postMessage({{status: 'fallback-owner'}});
              return;
            }}
            if (workerResponse === 'ambiguous') {{
              port.postMessage({{status: 'ambiguous'}});
              return;
            }}
            recordNative(message.title, message.options, identity);
            port.postMessage({{status: 'shown'}});
          }},
        }};

        class Port {{
          constructor() {{ this.peer = null; this.onmessage = null; }}
          postMessage(data) {{
            const peer = this.peer;
            queueMicrotask(() => {{
              if (peer && typeof peer.onmessage === 'function') peer.onmessage({{data}});
            }});
          }}
          close() {{}}
          start() {{}}
        }}
        class MessageChannel {{
          constructor() {{
            this.port1 = new Port();
            this.port2 = new Port();
            this.port1.peer = this.port2;
            this.port2.peer = this.port1;
          }}
        }}

        function makePage() {{
          const registration = {{
            active: worker,
            showNotification: (title, options) => worker.showNotification(title, options),
          }};
          const context = {{
            Promise, MessageChannel, queueMicrotask, setTimeout, clearTimeout,
            navigator: {{serviceWorker: {{getRegistration: () => Promise.resolve(registration)}}}},
            location: {{origin: 'https://webui.test', href: 'https://webui.test/'}},
            S: {{session: {{}}}},
            _sessionUrlForSid: sid => '/?session=' + encodeURIComponent(sid),
            assistantDisplayName: () => 'Hermes',
            window: {{_notificationsEnabled: true}},
            _isBackgroundedForBrowserNotification: () => true,
            showToast: () => {{}},
            t: key => key,
            Notification: function(title, options) {{
              directFallbacks += 1;
              recordNative(title, options, null);
              return {{}};
            }},
          }};
          context.Notification.permission = 'granted';
          context.window.Notification = context.Notification;
          vm.runInNewContext({page_source}, context);
          return async function send(identity) {{
            await context.sendBrowserNotification(
              'Response complete',
              'The task finished.',
              {{sid: '{FIXTURE['sid']}', forceHidden: true, eventIdentity: identity}},
            );
          }};
        }}

        (async () => {{
          const pageA = makePage();
          const pageB = makePage();
          const first = {{streamId: 'stream-6673', lastEventId: 'stream-6673:1'}};
          const second = {{streamId: 'stream-6673', lastEventId: 'stream-6673:2'}};
          await Promise.all([pageA(first), pageB(first)]);
          await Promise.all([pageA(second), pageB(second)]);
          console.log(JSON.stringify({{
            attempts: attempts.length,
            attemptsByIdentity: attempts.map(item => item.identity),
            directFallbacks,
            tags: [...entries.keys()],
            claims: [...claims],
          }}));
        }})().catch(error => {{
          console.error(error.stack || error);
          process.exitCode = 1;
        }});
        """,
    )
    return _run_node(script)


def _keyed_edge_driver(
    *,
    worker_response: str = "shown",
    permission: str = "granted",
    backgrounded: bool = True,
) -> dict:
    notification_options = _notification_helper_source()
    show_notification = extract_function(MESSAGES_JS, "_showPwaNotification")
    request_permission = extract_function(MESSAGES_JS, "requestNotificationPermission")
    send_notification = extract_function(MESSAGES_JS, "sendBrowserNotification")
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const workerResponse = {json.dumps(worker_response)};
        const permission = {json.dumps(permission)};
        const permissionResult = permission === 'default' ? 'granted' : permission;
        const trace = [];
        let claimAttempts = 0;
        let directFallbacks = 0;

        class Port {{
          constructor() {{ this.peer = null; this.onmessage = null; }}
          postMessage(data) {{
            const peer = this.peer;
            queueMicrotask(() => peer?.onmessage?.({{data}}));
          }}
          close() {{}}
          start() {{}}
        }}
        class MessageChannel {{
          constructor() {{
            this.port1 = new Port();
            this.port2 = new Port();
            this.port1.peer = this.port2;
            this.port2.peer = this.port1;
          }}
        }}

        const worker = {{
          postMessage(message, transfer) {{
            claimAttempts += 1;
            trace.push('claim');
            const port = transfer && transfer[0];
            if (!port || workerResponse === 'timeout') return;
            const status = workerResponse === 'unknown' ? 'mystery' : workerResponse;
            port.postMessage({{status}});
          }},
        }};
        let navigator;
        if (workerResponse === 'missing') {{
          navigator = {{}};
        }} else {{
          navigator = {{
            serviceWorker: {{
              getRegistration: () => Promise.resolve({{
                active: workerResponse === 'inactive' ? null : worker,
              }}),
            }},
          }};
        }}

        const context = {{
          Promise, MessageChannel, queueMicrotask, setTimeout, clearTimeout,
          navigator,
          location: {{origin: 'https://webui.test', href: 'https://webui.test/'}},
          S: {{session: {{}}}},
          _sessionUrlForSid: sid => '/?session=' + encodeURIComponent(sid),
          assistantDisplayName: () => 'Hermes',
          window: {{_notificationsEnabled: true}},
          _isBackgroundedForBrowserNotification: () => {json.dumps(backgrounded)},
          showToast: () => {{}},
          t: key => key,
          Notification: function() {{ directFallbacks += 1; return {{}}; }},
        }};
        context.Notification.permission = permission;
        context.Notification.requestPermission = () => {{
          trace.push('permission');
          return Promise.resolve(permissionResult);
        }};
        context.window.Notification = context.Notification;
        vm.runInNewContext(
          {json.dumps(notification_options + show_notification + request_permission + send_notification)},
          context,
        );

        (async () => {{
          const result = await context.sendBrowserNotification(
            'Response complete',
            'The task finished.',
            {{sid: '{FIXTURE['sid']}', forceHidden: {json.dumps(backgrounded)}, eventIdentity: {{streamId: 'stream-6673', lastEventId: 'stream-6673:edge'}}}},
          );
          console.log(JSON.stringify({{
            result: result === undefined ? 'undefined' : result,
            claimAttempts,
            directFallbacks,
            trace,
          }}));
        }})().catch(error => {{
          console.error(error.stack || error);
          process.exitCode = 1;
        }});
        """,
    )
    return _run_node(script)


def _no_worker_two_page_driver(mode: str) -> dict:
    notification_options = _notification_helper_source()
    show_notification = extract_function(MESSAGES_JS, "_showPwaNotification")
    request_permission = extract_function(MESSAGES_JS, "requestNotificationPermission")
    send_notification = extract_function(MESSAGES_JS, "sendBrowserNotification")
    page_source = json.dumps(
        "\n".join([notification_options, show_notification, request_permission, send_notification]),
    )
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const records = new Set();
        let directDeliveries = 0;
        const directTags = [];
        const mode = {json.dumps(mode)};
        function makeDb() {{
          return {{
            objectStoreNames: {{contains: () => true}},
            createObjectStore: () => ({{}}),
            transaction: () => {{
              const tx = {{oncomplete: null, onerror: null, onabort: null, error: null}};
              tx.objectStore = () => ({{
                add(value) {{
                  const request = {{onsuccess: null, onerror: null, error: null}};
                  queueMicrotask(() => {{
                    const key = JSON.stringify([value.streamId, value.lastEventId]);
                    if (records.has(key)) {{
                      request.error = {{name: 'ConstraintError'}};
                      let prevented = false;
                      request.onerror?.({{preventDefault: () => {{prevented = true;}}}});
                      tx.error = request.error;
                      tx.onerror?.({{preventDefault: () => {{}}}});
                      if (prevented) queueMicrotask(() => tx.oncomplete?.({{}}));
                      else tx.onabort?.({{}});
                      return;
                    }}
                    records.add(key);
                    request.onsuccess?.({{}});
                    queueMicrotask(() => tx.oncomplete?.({{}}));
                  }});
                  return request;
                }},
              }});
              return tx;
            }},
            close: () => {{}},
          }};
        }}
        const indexedDB = {{
          open() {{
            const request = {{onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null, result: makeDb()}};
            queueMicrotask(() => {{
              request.onupgradeneeded?.({{target: {{result: request.result}}}});
              request.onsuccess?.({{target: {{result: request.result}}}});
            }});
            return request;
          }},
        }};
        class Port {{
          constructor() {{ this.peer = null; this.onmessage = null; }}
          postMessage(data) {{ this.peer?.onmessage?.({{data}}); }}
          close() {{}}
          start() {{}}
        }}
        class MessageChannel {{
          constructor() {{ this.port1 = new Port(); this.port2 = new Port(); this.port1.peer = this.port2; this.port2.peer = this.port1; }}
        }}
        function makePage() {{
          let registration;
          if (mode === 'rejected') registration = {{getRegistration: () => Promise.reject(new Error('registration rejected'))}};
          else if (mode === 'missing') registration = undefined;
          else if (mode === 'missing-api') registration = {{getRegistration: () => Promise.resolve({{active: {{}}}})}};
          else if (mode === 'activation-delay') {{
            registration = {{
              getRegistration: () => new Promise(resolve => setTimeout(
                () => resolve({{active: null, installing: {{}}}}),
                2100,
              )),
            }};
          }} else registration = {{getRegistration: () => Promise.resolve({{active: null}})}};
          const context = {{
            Promise, MessageChannel, indexedDB, queueMicrotask, setTimeout, clearTimeout,
            navigator: registration ? {{serviceWorker: registration}} : {{}},
            location: {{origin: 'https://webui.test', href: 'https://webui.test/'}},
            S: {{session: {{}}}},
            _sessionUrlForSid: sid => '/?session=' + encodeURIComponent(sid),
            assistantDisplayName: () => 'Hermes',
            window: {{_notificationsEnabled: true}},
            _isBackgroundedForBrowserNotification: () => true,
            showToast: () => {{}}, t: key => key,
            Notification: function(_title, options) {{
              directDeliveries += 1;
              directTags.push(options.tag);
              return {{}};
            }},
          }};
          context.Notification.permission = 'granted';
          context.window.Notification = context.Notification;
          vm.runInNewContext({page_source}, context);
          return identity => context.sendBrowserNotification(
            'Response complete', 'The task finished.',
            {{sid: '{FIXTURE['sid']}', forceHidden: true, eventIdentity: identity}},
          );
        }}
        (async () => {{
          const pageA = makePage();
          const pageB = makePage();
          const first = {{streamId: 'stream-6673', lastEventId: 'stream-6673:1'}};
          const second = {{streamId: 'stream-6673', lastEventId: 'stream-6673:2'}};
          const firstResults = await Promise.all([pageA(first), pageB(first)]);
          const secondResults = await Promise.all([pageA(second), pageB(second)]);
          console.log(JSON.stringify({{firstResults, secondResults, directDeliveries, directTags, records: [...records]}}));
        }})().catch(error => {{ console.error(error.stack || error); process.exitCode = 1; }});
        """,
    )
    return _run_node(script)


def test_one_event_alerts_once_across_two_background_subscribers():
    result = _two_subscriber_driver()

    assert result["attempts"] == 2, (
        "one owner per distinct SSE identity is required across two subscribers; "
        f"observed {result}"
    )
    assert [item["lastEventId"] for item in result["attemptsByIdentity"]] == [
        "stream-6673:1",
        "stream-6673:2",
    ]
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_next_distinct_event_alerts_again_across_two_subscribers():
    result = _two_subscriber_driver()

    assert [item["lastEventId"] for item in result["attemptsByIdentity"]] == [
        "stream-6673:1",
        "stream-6673:2",
    ]
    assert result["claims"] == [
        '["stream-6673","stream-6673:1"]',
        '["stream-6673","stream-6673:2"]',
    ]
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_claim_owner_alone_uses_direct_fallback_after_worker_rejection():
    result = _two_subscriber_driver(worker_response="fallback-owner")

    assert result["directFallbacks"] == 2


def test_ambiguous_keyed_delivery_fails_closed_without_direct_fallback():
    result = _two_subscriber_driver(worker_response="ambiguous")

    assert result["directFallbacks"] == 0


def test_visible_tab_does_not_claim_an_ineligible_keyed_event():
    result = _keyed_edge_driver(backgrounded=False)

    assert result == {
        "result": "undefined",
        "claimAttempts": 0,
        "directFallbacks": 0,
        "trace": [],
    }


@pytest.mark.parametrize("mode", ["missing", "missing-api", "inactive", "rejected", "activation-delay"])
def test_no_worker_modes_share_page_claim_and_alert_each_distinct_identity(mode: str):
    result = _no_worker_two_page_driver(mode)

    assert sorted(result["firstResults"]) == ["duplicate", "shown"]
    assert sorted(result["secondResults"]) == ["duplicate", "shown"]
    assert result["directDeliveries"] == 2
    assert result["directTags"] == [FIXTURE["expected_tag"], FIXTURE["expected_tag"]]
    assert result["records"] == [
        '["stream-6673","stream-6673:1"]',
        '["stream-6673","stream-6673:2"]',
    ]


@pytest.mark.parametrize("worker_response", ["timeout", "unknown"])
def test_keyed_delivery_fails_closed_on_lost_or_unknown_worker_response(worker_response: str):
    result = _keyed_edge_driver(worker_response=worker_response)

    assert result["result"] == "ambiguous"
    assert result["claimAttempts"] == 1
    assert result["directFallbacks"] == 0


def test_keyed_permission_is_resolved_before_claim_and_denial_blocks_claim():
    granted = _keyed_edge_driver(permission="default")
    denied = _keyed_edge_driver(permission="denied")

    assert granted["trace"] == ["permission", "claim"]
    assert granted["result"] == "shown"
    assert denied["trace"] == []
    assert denied["result"] == "undefined"
    assert denied["claimAttempts"] == 0


def test_public_sender_repeats_same_session_toast_with_stable_tag():
    result = _driver(
        delivery="service-worker",
        sends=[
            _send(FIXTURE["title"], FIXTURE["body"]),
            _send(FIXTURE["title"], "The task finished again."),
        ],
        via_public_sender=True,
    )

    assert result["toasts"] == 2
    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_service_worker_delivery_preserves_grouping():
    result = _driver(
        delivery="service-worker",
        sends=[_send("Approval required", "Approve the tool call.")],
    )

    call = result["calls"][0]
    assert call["method"] == "service-worker"
    assert call["options"]["tag"] == FIXTURE["expected_tag"]
    assert call["options"]["renotify"] is True
    assert call["options"]["data"]["url"] == FIXTURE["expected_url"]


def test_direct_fallback_preserves_payload():
    result = _driver(
        delivery="direct",
        sends=[_send("Clarification needed", "Choose a value.")],
    )

    call = result["calls"][0]
    assert call["method"] == "direct"
    assert call["title"] == "Clarification needed"
    assert call["options"]["body"] == "Choose a value."
    assert call["options"]["tag"] == FIXTURE["expected_tag"]
    assert call["options"]["renotify"] is True
    assert call["options"]["data"]["url"] == FIXTURE["expected_url"]


def test_reported_two_test_clicks_request_second_toast_with_stable_tag():
    result = _driver(
        delivery="direct",
        sends=[
            _send(FIXTURE["title"], FIXTURE["body"]),
            _send(FIXTURE["title"], "The task finished again."),
        ],
        via_public_sender=True,
    )

    assert [call["method"] for call in result["calls"]] == ["direct", "direct"]
    assert result["toasts"] == 2
    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_direct_fallback_when_show_notification_rejects():
    result = _driver(
        delivery="rejected-show-notification",
        sends=[_send("Clarification needed", "Choose a value.")],
        via_public_sender=True,
    )

    call = result["calls"][0]
    assert call["method"] == "direct"
    assert call["options"]["tag"] == FIXTURE["expected_tag"]
    assert call["options"]["renotify"] is True


def test_public_sender_awaits_delivery_after_permission_grant():
    result = _driver(
        delivery="service-worker",
        sends=[_send(FIXTURE["title"], FIXTURE["body"])],
        permission="default",
        via_public_sender=True,
    )

    assert [call["method"] for call in result["calls"]] == ["service-worker"]
    assert result["tags"] == [FIXTURE["expected_tag"]]


@pytest.mark.parametrize("delivery", ["inactive-service-worker", "rejected-service-worker"])
def test_page_claim_delivers_when_service_worker_registration_unavailable(delivery: str):
    mode = "inactive" if delivery == "inactive-service-worker" else "rejected"
    result = _no_worker_two_page_driver(mode)
    assert result["directDeliveries"] == 2


def test_categories_and_denial_keep_existing_behavior():
    result = _driver(
        delivery="service-worker",
        sends=[
            _send("Approval required", "Approve the tool call."),
            _send("Clarification needed", "Choose a value."),
        ],
    )

    assert [call["title"] for call in result["calls"]] == [
        "Approval required",
        "Clarification needed",
    ]
    assert all(call["options"]["tag"] == FIXTURE["expected_tag"] for call in result["calls"])

    denied = _driver(
        delivery="service-worker",
        sends=[_send(FIXTURE["title"], FIXTURE["body"])],
        permission="denied",
        via_public_sender=True,
    )
    assert denied["calls"] == []


def test_grouping_keeps_one_notification_entry():
    result = _driver(
        delivery="service-worker",
        sends=[_send(FIXTURE["title"], f"send {index}") for index in range(3)],
    )

    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["expected_tag"]]


def test_repeated_missing_session_id_uses_generic_grouping_tag():
    result = _driver(
        delivery="service-worker",
        sends=[
            _send("Approval required", "Approve the tool call.", sid=None),
            _send("Approval required", "Approve it again.", sid=None),
        ],
        via_public_sender=True,
    )

    assert result["toasts"] == 2
    assert result["entryCount"] == 1
    assert result["tags"] == [FIXTURE["generic_tag"]]
    assert result["calls"][-1]["options"]["data"]["url"] == FIXTURE["generic_url"]
