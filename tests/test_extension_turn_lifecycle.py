"""Behavioral coverage for extension turn-lifecycle subscriptions."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).parent.parent
STATIC = ROOT / "static"
EXTENSION_SETTINGS_JS = ROOT / "static" / "extension_settings.js"
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
UI_JS = (STATIC / "ui.js").read_text(encoding="utf-8")
SUPPORT_SCRIPTS = [
    (STATIC / name).read_text(encoding="utf-8")
    for name in ("i18n.js", "icons.js", "assistant_turn_anchors.js")
]
HARNESS_HTML = re.sub(r"<script\b[^>]*>.*?</script>", "", INDEX_HTML, flags=re.I | re.S)
HARNESS_HTML = re.sub(r"<link\b[^>]*>", "", HARNESS_HTML, flags=re.I)


_MOCK_EVENT_SOURCE = r"""
class MockEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances = [];
  constructor(url) {
    this.url = String(url);
    this.readyState = MockEventSource.OPEN;
    this.listeners = new Map();
    MockEventSource.instances.push(this);
  }
  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
  removeEventListener(name, listener) {
    this.listeners.set(name, (this.listeners.get(name) || []).filter(fn => fn !== listener));
  }
  async emit(name, payload) {
    const event = {type: name, data: JSON.stringify(payload), lastEventId: ''};
    await Promise.all((this.listeners.get(name) || []).map(listener => listener(event)));
  }
  close() { this.readyState = MockEventSource.CLOSED; }
}
window.EventSource = MockEventSource;
window.MockEventSource = MockEventSource;
"""


_STABILIZE_UNRELATED_UI = r"""
appendLiveCompressionCard = () => false;
resetTurnWorkspaceMutations = () => {};
_resetStreamScrollFollow = () => {};
ensureLiveWorklogShell = () => {};
_shouldUseLiveProseFade = () => false;
_shouldFollowMessagesOnDomReplace = () => false;
_isMessagePaneNearBottom = () => false;
_markSessionViewed = () => {};
setBusy = value => { S.busy = Boolean(value); };
setComposerStatus = () => {};
setStatus = () => {};
syncTopbar = () => {};
renderMessages = () => {};
renderSessionList = () => {};
renderSessionArtifacts = () => {};
loadDir = () => {};
playNotificationSound = () => {};
sendBrowserNotification = () => {};
api = async () => ({});
"""


_LIFECYCLE_SCENARIO = r"""
async ({kind}) => {
  const activeSid = 'session-a';
  const streamId = `stream-${kind}`;
  const lifecycle = [];
  const extension = window.hermesExt.register('lifecycle-probe');
  if (!extension) throw new Error('lifecycle probe did not register');
  for (const type of ['turn:start', 'turn:complete', 'turn:error', 'turn:cancel']) {
    extension.events.on(type, event => {
      const lastMessage = Array.isArray(S.messages) ? S.messages.at(-1) : null;
      lifecycle.push({
        event: {...event},
        activeStreamId: S.activeStreamId || null,
        busy: Boolean(S.busy),
        currentSid: S.session && S.session.session_id || null,
        lastContent: lastMessage && lastMessage.content || null,
      });
    });
  }

  S.session = {session_id: activeSid, messages: [{role: 'user', content: 'question'}]};
  S.messages = [{role: 'user', content: 'question'}];
  S.toolCalls = [];
  S.activeStreamId = streamId;
  S.busy = true;
  attachLiveStream(activeSid, streamId, []);
  const source = MockEventSource.instances.at(-1);
  if (!source) throw new Error('attachLiveStream did not construct EventSource');

  if (kind === 'done') {
    await source.emit('done', {
      status: 'completed',
      stream_id: streamId,
      session: {
        session_id: activeSid,
        messages: [{role: 'assistant', content: 'settled-done'}],
        tool_calls: [],
      },
    });
  } else if (kind === 'apperror') {
    await source.emit('apperror', {
      type: 'provider_error',
      status: 'provider_error',
      message: 'provider failed',
      session_id: activeSid,
      session: {
        session_id: activeSid,
        messages: [{role: 'assistant', content: 'settled-error'}],
      },
    });
  } else if (kind === 'apperror-cancel') {
    await source.emit('apperror', {
      type: 'interrupted',
      status: 'interrupted',
      message: 'interrupted',
      session_id: activeSid,
      session: {
        session_id: activeSid,
        messages: [{role: 'assistant', content: 'settled-interrupted'}],
      },
    });
  } else if (kind === 'cancel') {
    await source.emit('cancel', {
      type: 'cancelled',
      status: 'cancelled',
      session_id: activeSid,
      session: {
        session_id: activeSid,
        messages: [{role: 'assistant', content: 'settled-cancel'}],
      },
    });
  } else if (kind === 'connection-error') {
    const nativeSetTimeout = window.setTimeout;
    api = async url => String(url).includes('/api/chat/stream/status')
      ? {active: false, replay_available: false}
      : {};
    window.setTimeout = callback => {
      queueMicrotask(callback);
      return 1;
    };
    try {
      await source.emit('error', {});
      await new Promise((resolve, reject) => {
        const deadline = Date.now() + 2000;
        const poll = () => {
          if (lifecycle.some(entry => entry.event.type === 'turn:error')) return resolve();
          if (Date.now() >= deadline) return reject(new Error('connection terminal event not observed'));
          nativeSetTimeout(poll, 0);
        };
        poll();
      });
    } finally {
      window.setTimeout = nativeSetTimeout;
    }
  } else {
    throw new Error(`unknown lifecycle scenario: ${kind}`);
  }
  return lifecycle;
}
"""


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace = source.index("){", start) + 1
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"function {name} body not found")


def _event_listener_body(source: str, event_name: str) -> str:
    start = source.index(f"source.addEventListener('{event_name}'")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"event listener {event_name!r} body not found")


def _run_node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for extension lifecycle runtime tests")
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_registered_extension_receives_bounded_turn_lifecycle_events():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const assert = require('assert');
        const store = new Map();
        const loggedErrors = [];
        global.window = {{
          __HERMES_EXTENSION_CONFIG__: {{
            extensions: [
              {{id: 'alpha.ext', storage_owned: false}},
              {{id: 'beta.ext', storage_owned: false}},
            ],
          }},
          localStorage: {{
            getItem(key) {{ return store.has(key) ? store.get(key) : null; }},
            setItem(key, value) {{ store.set(key, String(value)); }},
            removeItem(key) {{ store.delete(key); }},
          }},
        }};
        global.console = {{
          error(...args) {{ loggedErrors.push(args.map(String).join(' ')); }},
        }};
        eval(fs.readFileSync({str(EXTENSION_SETTINGS_JS)!r}, 'utf8'));

        const alpha = window.hermesExt.register('alpha.ext');
        const beta = window.hermesExt.register('beta.ext');
        assert.deepStrictEqual(Object.keys(alpha).sort(), ['events', 'id', 'messages', 'settings', 'storage']);
        assert.strictEqual(Object.isFrozen(alpha.events), true);

        const alphaEvents = [];
        const betaEvents = [];
        const unsubscribeStart = alpha.events.on('turn:start', event => {{
          assert.strictEqual(Object.isFrozen(event), true);
          alphaEvents.push(event);
        }});
        alpha.events.on('turn:complete', () => {{ throw new Error('extension failure'); }});
        alpha.events.on('turn:complete', event => alphaEvents.push(event));
        alpha.events.on('turn:error', event => alphaEvents.push(event));
        alpha.events.on('turn:cancel', event => alphaEvents.push(event));
        beta.events.on('turn:complete', event => betaEvents.push(event));

        assert.strictEqual(typeof unsubscribeStart, 'function');
        assert.strictEqual(alpha.events.on('token', () => {{}}), null);
        assert.strictEqual(alpha.events.on('turn:start', null), null);

        const emit = window.HermesExtensionSettings._dispatchTurnLifecycle;
        assert.strictEqual(typeof emit, 'function');
        assert.strictEqual(emit('turn:start', {{sessionId: '', streamId: 'stream-a'}}), false);
        assert.strictEqual(emit('turn:start', {{sessionId: 'session-a', streamId: ''}}), false);
        assert.strictEqual(emit('token', {{sessionId: 'session-a', streamId: 'stream-a'}}), false);

        assert.strictEqual(emit('turn:start', {{
          sessionId: 'session-a', streamId: 'stream-a', startedAt: 10,
        }}), true);
        assert.strictEqual(emit('turn:start', {{
          sessionId: 'session-a', streamId: 'stream-a', startedAt: 11,
        }}), false);
        assert.strictEqual(emit('turn:complete', {{
          sessionId: 'session-a', streamId: 'stream-a', status: 'completed', endedAt: 20,
        }}), true);
        assert.strictEqual(emit('turn:error', {{
          sessionId: 'session-a', streamId: 'stream-a', status: 'late-error', endedAt: 21,
        }}), false);
        assert.strictEqual(emit('turn:cancel', {{
          sessionId: 'session-a', streamId: 'stream-a', status: 'late-cancel', endedAt: 22,
        }}), false);

        assert.strictEqual(emit('turn:start', {{sessionId: 'session-b', streamId: 'stream-b'}}), true);
        assert.strictEqual(emit('turn:error', {{
          sessionId: 'session-b', streamId: 'stream-b', status: 'provider_error',
        }}), true);
        assert.strictEqual(emit('turn:start', {{sessionId: 'session-c', streamId: 'stream-c'}}), true);
        assert.strictEqual(emit('turn:cancel', {{
          sessionId: 'session-c', streamId: 'stream-c', status: 'cancelled',
        }}), true);

        unsubscribeStart();
        unsubscribeStart();
        assert.strictEqual(emit('turn:start', {{sessionId: 'session-d', streamId: 'stream-d'}}), true);

        assert.deepStrictEqual(
          alphaEvents.map(event => [event.type, event.sessionId, event.streamId, event.status || null]),
            [
              ['turn:start', 'session-a', 'stream-a', null],
              ['turn:complete', 'session-a', 'stream-a', 'completed'],
              ['turn:start', 'session-b', 'stream-b', null],
              ['turn:error', 'session-b', 'stream-b', 'provider_error'],
              ['turn:start', 'session-c', 'stream-c', null],
              ['turn:cancel', 'session-c', 'stream-c', 'cancelled'],
            ],
        );
        assert.deepStrictEqual(
          betaEvents.map(event => [event.type, event.sessionId, event.streamId]),
          [['turn:complete', 'session-a', 'stream-a']],
        );
        assert.strictEqual(loggedErrors.length, 1);
        assert.match(loggedErrors[0], /alpha[.]ext.*turn:complete.*extension failure/);
        """
    )
    _run_node(script)


def test_live_stream_bridge_forwards_normalized_lifecycle_details():
    bridge = _function_body(MESSAGES_JS, "_dispatchExtensionTurnLifecycle")
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const calls = [];
        global.window = {{
          HermesExtensionSettings: {{
            _dispatchTurnLifecycle(type, details) {{
              calls.push([type, details]);
              return 'delivered';
            }},
          }},
        }};
        eval({json.dumps(bridge)});

        assert.strictEqual(
          _dispatchExtensionTurnLifecycle(
            'turn:error', 'session-a', 'stream-a', {{status: 'connection_lost'}},
          ),
          'delivered',
        );
        assert.deepStrictEqual(calls, [[
          'turn:error',
          {{sessionId: 'session-a', streamId: 'stream-a', status: 'connection_lost'}},
        ]]);

        delete window.HermesExtensionSettings;
        assert.strictEqual(
          _dispatchExtensionTurnLifecycle('turn:start', 'session-b', 'stream-b'),
          false,
        );

        window.HermesExtensionSettings = {{
          _dispatchTurnLifecycle() {{ throw new Error('broken extension runtime'); }},
        }};
        global.console = {{error() {{}}}};
        assert.strictEqual(
          _dispatchExtensionTurnLifecycle('turn:start', 'session-c', 'stream-c'),
          false,
        );
        """
    )
    _run_node(script)


@pytest.fixture(scope="module")
def lifecycle_browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield browser
        browser.close()


def _run_lifecycle_scenario(browser, kind: str) -> list[dict]:
    page = browser.new_page()
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html"
            if route.request.url == "http://harness.test/"
            else "text/plain",
            body=HARNESS_HTML if route.request.url == "http://harness.test/" else "",
        ),
    )
    page.add_init_script(_MOCK_EVENT_SOURCE)
    try:
        page.goto("http://harness.test/", wait_until="domcontentloaded")
        page.evaluate(
            """
            window.__HERMES_EXTENSION_CONFIG__ = {
              extensions: [{id: 'lifecycle-probe', storage_owned: false}],
            };
            """
        )
        for script in SUPPORT_SCRIPTS:
            page.add_script_tag(content=script)
        page.add_script_tag(content=EXTENSION_SETTINGS_JS.read_text(encoding="utf-8"))
        page.add_script_tag(content=UI_JS)
        page.add_script_tag(content=MESSAGES_JS)
        page.evaluate(_STABILIZE_UNRELATED_UI)
        return page.evaluate(_LIFECYCLE_SCENARIO, {"kind": kind})
    finally:
        page.close()


@pytest.mark.parametrize(
    ("kind", "terminal_type", "terminal_status", "last_content"),
    [
        ("done", "turn:complete", "completed", "settled-done"),
        ("apperror", "turn:error", "provider_error", "settled-error"),
        ("apperror-cancel", "turn:cancel", "interrupted", "settled-interrupted"),
        ("cancel", "turn:cancel", "cancelled", "settled-cancel"),
        (
            "connection-error",
            "turn:error",
            "connection_lost",
            "**Connection interrupted:** The browser lost the live SSE connection before the response finished. If the worker completed, reopening this session should restore the settled transcript.",
        ),
    ],
)
def test_real_sse_terminal_callback_observes_settled_core_state(
    lifecycle_browser,
    kind,
    terminal_type,
    terminal_status,
    last_content,
):
    lifecycle = _run_lifecycle_scenario(lifecycle_browser, kind)

    assert [entry["event"]["type"] for entry in lifecycle] == [
        "turn:start",
        terminal_type,
    ]
    start, terminal = lifecycle
    assert start["activeStreamId"] == f"stream-{kind}"
    assert start["busy"] is True
    assert terminal["event"]["sessionId"] == "session-a"
    assert terminal["event"]["streamId"] == f"stream-{kind}"
    assert terminal["event"]["status"] == terminal_status
    assert terminal["activeStreamId"] is None
    assert terminal["busy"] is False
    assert terminal["currentSid"] == "session-a"
    assert terminal["lastContent"] == last_content


def test_live_stream_terminal_paths_use_original_stream_owner_identity():
    attach_start = MESSAGES_JS.index("function attachLiveStream(")
    attach_body = MESSAGES_JS[attach_start : MESSAGES_JS.index("\nfunction transcript(", attach_start)]
    done = _event_listener_body(MESSAGES_JS, "done")
    application_error = _event_listener_body(MESSAGES_JS, "apperror")
    cancel = _event_listener_body(MESSAGES_JS, "cancel")
    connection_error = _function_body(MESSAGES_JS, "_handleStreamError")

    start_dispatch = attach_body.index("_dispatchExtensionTurnLifecycle('turn:start',activeSid,streamId")
    dead_reconnect_return = attach_body.index("_scheduleAnchorRegistryCleanup(120000);")
    event_source_attach = attach_body.index("_wireSSE(new EventSource", start_dispatch)
    assert dead_reconnect_return < start_dispatch < event_source_attach
    assert "_dispatchExtensionTurnLifecycle('turn:complete',activeSid,streamId" in done
    assert "_dispatchExtensionTurnLifecycle(_extensionErrorType,activeSid,streamId" in application_error
    assert "_dispatchExtensionTurnLifecycle('turn:cancel',activeSid,streamId" in cancel
    assert "_dispatchExtensionTurnLifecycle('turn:error',activeSid,streamId" in connection_error

    assert done.index("_setActivePaneIdleIfOwner()") < done.index(
        "_dispatchExtensionTurnLifecycle('turn:complete'"
    )
    assert application_error.index("renderSessionList()") < application_error.index(
        "_dispatchExtensionTurnLifecycle(_extensionErrorType"
    )
    assert cancel.index("await api(") < cancel.index(
        "_dispatchExtensionTurnLifecycle('turn:cancel'"
    )
    assert cancel.index("_setActivePaneIdleIfOwner()") < cancel.index(
        "_dispatchExtensionTurnLifecycle('turn:cancel'"
    )
    assert connection_error.index("_setActivePaneIdleIfOwner()") < connection_error.index(
        "_dispatchExtensionTurnLifecycle('turn:error'"
    )

    assert "_dispatchExtensionTurnLifecycle('turn:complete',completedSid" not in done
    assert "_dispatchExtensionTurnLifecycle(_extensionErrorType,d.session_id" not in application_error
    assert "_dispatchExtensionTurnLifecycle('turn:cancel',_cancelData.session_id" not in cancel
