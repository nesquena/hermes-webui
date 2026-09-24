"""Runtime coverage for foreground ownership of chat EventSource handlers."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised only in minimal environments
    sync_playwright = None


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("Playwright is unavailable")
    with sync_playwright() as playwright:
        if not shutil.which("node") or not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium is unavailable")
        instance = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield instance
        instance.close()


def test_switch_closes_background_handlers_and_preserves_foreground_state(browser, base_url):
    """Exercise the production-composed attach/switch path in a real page.

    Session A owns a live source, then session B becomes the foreground and owns
    a second source.  A's transport must be closed and removed before a delayed
    A completion can run any of its captured handlers.  Its inflight snapshot is
    retained for reattach, while B's transcript, status, routing/TPS metadata,
    tool state, queue marker, active stream, and source identity stay unchanged.
    """
    page = browser.new_page(
        viewport={"width": 1024, "height": 720},
        bypass_csp=True,
    )
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true && "
            "typeof attachLiveStream === 'function'",
            timeout=15_000,
        )
        result = page.evaluate(
            """
            async () => {
              class FakeEventSource {
                static CONNECTING = 0;
                static OPEN = 1;
                static CLOSED = 2;
                static instances = [];
                constructor(url) {
                  this.url = String(url);
                  this.readyState = FakeEventSource.OPEN;
                  this.listeners = new Map();
                  FakeEventSource.instances.push(this);
                }
                addEventListener(name, callback) {
                  const callbacks = this.listeners.get(name) || [];
                  callbacks.push(callback);
                  this.listeners.set(name, callbacks);
                }
                close() { this.readyState = FakeEventSource.CLOSED; }
                emit(name, payload) {
                  if (this.readyState === FakeEventSource.CLOSED) return false;
                  const event = {data: JSON.stringify(payload || {})};
                  for (const callback of this.listeners.get(name) || []) callback(event);
                  return true;
                }
              }
              window.EventSource = FakeEventSource;

              for (const sid of Object.keys(LIVE_STREAMS)) closeLiveStream(sid);
              S.session = {session_id:'session-a', pending_started_at:1};
              S.messages = [
                {role:'user', content:'question-a'},
                {role:'assistant', content:'partial-a', _live:true},
              ];
              S.toolCalls = [{name:'tool-a', done:false}];
              S.activeStreamId = 'stream-a';
              attachLiveStream('session-a', 'stream-a');
              await Promise.resolve();
              const sourceA = FakeEventSource.instances.at(-1);

              S.session = {session_id:'session-b', pending_started_at:2};
              S.messages = [{
                role:'assistant',
                content:'foreground-b',
                _turnTps:17,
                _gatewayRouting:{route:'gateway-b'},
              }];
              S.toolCalls = [{name:'tool-b', done:false}];
              S.activeStreamId = 'stream-b';
              attachLiveStream('session-b', 'stream-b');
              await Promise.resolve();
              const sourceB = FakeEventSource.instances.at(-1);
              const bTokenDelivered = sourceB.emit('token', {text:' token-b'});
              await new Promise(resolve => setTimeout(resolve, 0));

              S.sendQueue = [{id:'queued-b', text:'next-b'}];
              setComposerStatus('B streaming');
              const statusNode = document.getElementById('composerStatus');
              const before = {
                sessionId:S.session.session_id,
                activeStreamId:S.activeStreamId,
                messages:JSON.stringify(S.messages),
                toolCalls:JSON.stringify(S.toolCalls),
                queue:JSON.stringify(S.sendQueue),
                status:statusNode ? statusNode.textContent : '',
                sourceIsB:LIVE_STREAMS['session-b']?.source === sourceB,
              };

              // A completes after the switch. A real closed EventSource does not
              // dispatch this queued callback; FakeEventSource enforces that
              // transport contract while still retaining the captured listeners.
              const delivered = sourceA.emit('done', {
                session:{session_id:'session-a', messages:[{role:'assistant', content:'done-a'}]},
                usage:{total_tokens:99},
                gateway_routing:{route:'gateway-a'},
              });
              await new Promise(resolve => setTimeout(resolve, 0));

              const after = {
                sessionId:S.session.session_id,
                activeStreamId:S.activeStreamId,
                messages:JSON.stringify(S.messages),
                toolCalls:JSON.stringify(S.toolCalls),
                queue:JSON.stringify(S.sendQueue),
                status:statusNode ? statusNode.textContent : '',
                sourceIsB:LIVE_STREAMS['session-b']?.source === sourceB,
              };
              return {
                delivered,
                bTokenDelivered,
                sourceAClosed:sourceA.readyState === FakeEventSource.CLOSED,
                sourceARemoved:!LIVE_STREAMS['session-a'],
                sourceBOpen:sourceB.readyState === FakeEventSource.OPEN,
                inflightAPreserved:!!INFLIGHT['session-a'],
                inflightAReattach:INFLIGHT['session-a']?.reattach === true,
                foregroundUnchanged:JSON.stringify(before) === JSON.stringify(after),
                before,
                after,
              };
            }
            """
        )
        assert result["delivered"] is False
        assert result["bTokenDelivered"] is True
        assert result["sourceAClosed"] is True
        assert result["sourceARemoved"] is True
        assert result["sourceBOpen"] is True
        assert result["inflightAPreserved"] is True
        assert result["inflightAReattach"] is True
        assert result["foregroundUnchanged"] is True, result
    finally:
        page.close()


def test_late_reconnect_registration_after_switch_keeps_only_foreground_stream(browser, base_url):
    """A reconnect attach awaits a status probe before opening its EventSource.

    If the user switches from A to B while A's probe is pending, A's late
    registration must not leave a background transport (and its handler
    closures) alive next to B's foreground stream.
    """
    page = browser.new_page(
        viewport={"width": 1024, "height": 720},
        bypass_csp=True,
    )
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true && "
            "typeof attachLiveStream === 'function'",
            timeout=15_000,
        )
        result = page.evaluate(
            """
            async () => {
              class FakeEventSource {
                static CONNECTING = 0;
                static OPEN = 1;
                static CLOSED = 2;
                static instances = [];
                constructor(url) {
                  this.url = String(url);
                  this.readyState = FakeEventSource.OPEN;
                  FakeEventSource.instances.push(this);
                }
                addEventListener() {}
                close() { this.readyState = FakeEventSource.CLOSED; }
              }
              window.EventSource = FakeEventSource;
              for (const sid of Object.keys(LIVE_STREAMS)) closeLiveStream(sid);

              let releaseProbe;
              const probe = new Promise(resolve => { releaseProbe = resolve; });
              const originalApi = window.api;
              window.api = async (path, opts) => {
                if (String(path).includes('/api/chat/stream/status')) {
                  await probe;
                  return {active:true};
                }
                return originalApi(path, opts);
              };
              try {
                S.session = {session_id:'session-a', pending_started_at:1};
                S.messages = [];
                S.activeStreamId = 'stream-a';
                attachLiveStream('session-a', 'stream-a', [], {reconnecting:true});
                await Promise.resolve();
                const openedBeforeSwitch = FakeEventSource.instances.length;

                S.session = {session_id:'session-b', pending_started_at:2};
                S.activeStreamId = 'stream-b';
                attachLiveStream('session-b', 'stream-b');
                await new Promise(resolve => setTimeout(resolve, 0));
                const sourceB = LIVE_STREAMS['session-b']?.source;

                releaseProbe();
                await new Promise(resolve => setTimeout(resolve, 0));
                await new Promise(resolve => setTimeout(resolve, 0));
                const sourceA = FakeEventSource.instances.find(s => s.url.includes('stream-a'));
                return {
                  openedBeforeSwitch,
                  sourceAOpened:!!sourceA,
                  sourceAClosed:!!sourceA && sourceA.readyState === FakeEventSource.CLOSED,
                  liveKeys:Object.keys(LIVE_STREAMS).sort(),
                  sourceBRetained:!!sourceB && LIVE_STREAMS['session-b']?.source === sourceB,
                  sourceBOpen:!!sourceB && sourceB.readyState === FakeEventSource.OPEN,
                  inflightAReattach:INFLIGHT['session-a']?.reattach === true,
                };
              } finally {
                window.api = originalApi;
              }
            }
            """
        )
        assert result["openedBeforeSwitch"] == 0, result
        assert result["sourceAOpened"] is True, result
        assert result["liveKeys"] == ["session-b"], result
        assert result["sourceAClosed"] is True, result
        assert result["sourceBRetained"] is True, result
        assert result["sourceBOpen"] is True, result
        assert result["inflightAReattach"] is True, result
    finally:
        page.close()


def test_late_registration_during_load_window_uses_navigation_target(browser, base_url):
    """A late registration can land while loadSession(B) is still awaiting metadata.

    loadSession() names its destination in ``_loadingSessionId`` and closes other
    streams synchronously, but only replaces ``S.session`` after its fetch. A
    registration that lands in that window must arbitrate against the navigation
    target, not the stale ``S.session`` row, or it keeps the session the user left
    open with handlers bound to the foreground pane.
    """
    page = browser.new_page(
        viewport={"width": 1024, "height": 720},
        bypass_csp=True,
    )
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true && "
            "typeof attachLiveStream === 'function'",
            timeout=15_000,
        )
        result = page.evaluate(
            """
            async () => {
              class FakeEventSource {
                static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
                static instances = [];
                constructor(url) {
                  this.url = String(url);
                  this.readyState = FakeEventSource.OPEN;
                  FakeEventSource.instances.push(this);
                }
                addEventListener() {}
                close() { this.readyState = FakeEventSource.CLOSED; }
              }
              window.EventSource = FakeEventSource;
              for (const sid of Object.keys(LIVE_STREAMS)) closeLiveStream(sid);
              let releaseProbe;
              const probe = new Promise(resolve => { releaseProbe = resolve; });
              const originalApi = window.api;
              window.api = async (path, opts) => {
                if (String(path).includes('/api/chat/stream/status')) {
                  await probe;
                  return {active:true};
                }
                return originalApi(path, opts);
              };
              try {
                S.session = {session_id:'session-a', pending_started_at:1};
                S.messages = [];
                S.activeStreamId = 'stream-a';
                attachLiveStream('session-a', 'stream-a', [], {reconnecting:true});
                await Promise.resolve();
                // loadSession('session-b') before its metadata lands.
                _loadingSessionId = 'session-b';
                closeOtherLiveStreams('session-b');
                releaseProbe();
                await new Promise(resolve => setTimeout(resolve, 0));
                await new Promise(resolve => setTimeout(resolve, 0));
                const sourceA = FakeEventSource.instances.find(s => s.url.includes('stream-a'));
                // B's idle metadata lands; nothing else would close A.
                S.session = {session_id:'session-b'};
                S.activeStreamId = null;
                _loadingSessionId = null;
                await new Promise(resolve => setTimeout(resolve, 0));
                return {
                  sourceAOpened:!!sourceA,
                  sourceAOpen:!!sourceA && sourceA.readyState === FakeEventSource.OPEN,
                  liveKeys:Object.keys(LIVE_STREAMS).sort(),
                  inflightAReattach:INFLIGHT['session-a']?.reattach === true,
                };
              } finally {
                window.api = originalApi;
                _loadingSessionId = null;
                for (const sid of Object.keys(LIVE_STREAMS)) closeLiveStream(sid);
              }
            }
            """
        )
        assert result["sourceAOpened"] is True, result
        assert result["sourceAOpen"] is False, result
        assert result["liveKeys"] == [], result
        assert result["inflightAReattach"] is True, result
    finally:
        page.close()


def test_new_chat_during_pending_load_keeps_its_own_stream(browser, base_url):
    """New Chat started while loadSession(B) is still awaiting metadata.

    newSession() must supersede the pending load: clear ``_loadingSessionId`` and
    bump the load generation, so the new chat's stream registration is not
    arbitrated against the abandoned B load (which would close it as soon as it
    opens) and B's stale metadata can't replace the new chat.
    """
    page = browser.new_page(
        viewport={"width": 1024, "height": 720},
        bypass_csp=True,
    )
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true && "
            "typeof attachLiveStream === 'function' && typeof newSession === 'function'",
            timeout=15_000,
        )
        result = page.evaluate(
            """
            async () => {
              class FakeEventSource {
                static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
                static instances = [];
                constructor(url) {
                  this.url = String(url);
                  this.readyState = FakeEventSource.OPEN;
                  FakeEventSource.instances.push(this);
                }
                addEventListener() {}
                close() { this.readyState = FakeEventSource.CLOSED; }
              }
              window.EventSource = FakeEventSource;
              for (const sid of Object.keys(LIVE_STREAMS)) closeLiveStream(sid);
              const originalApi = window.api;
              window.api = async (path, opts) => {
                const p = String(path);
                if (p.includes('/api/session/new')) {
                  return {session:{session_id:'session-c', title:'Untitled', messages:[],
                                   workspace:(S.session&&S.session.workspace)||'', model:''}};
                }
                if (p.includes('/api/chat/stream/status')) return {active:true};
                return originalApi(path, opts);
              };
              const genBefore = _loadSessionGeneration;
              try {
                // loadSession('session-b') has named its target and is awaiting metadata.
                _loadingSessionId = 'session-b';
                await newSession(false, {worktree:false});
                const afterNew = {
                  loadingSessionId:_loadingSessionId,
                  genBumped:_loadSessionGeneration > genBefore,
                  sid:S.session && S.session.session_id,
                };
                // The first send in the new chat registers its stream.
                S.activeStreamId = 'stream-c';
                attachLiveStream('session-c', 'stream-c', []);
                await new Promise(resolve => setTimeout(resolve, 0));
                await new Promise(resolve => setTimeout(resolve, 0));
                const sourceC = FakeEventSource.instances.find(s => s.url.includes('stream-c'));
                return {
                  ...afterNew,
                  sourceCOpened:!!sourceC,
                  sourceCOpen:!!sourceC && sourceC.readyState === FakeEventSource.OPEN,
                  liveKeys:Object.keys(LIVE_STREAMS).sort(),
                };
              } finally {
                window.api = originalApi;
                _loadingSessionId = null;
                for (const sid of Object.keys(LIVE_STREAMS)) closeLiveStream(sid);
              }
            }
            """
        )
        assert result["sid"] == "session-c", result
        assert result["loadingSessionId"] is None, result
        assert result["genBumped"] is True, result
        assert result["sourceCOpened"] is True, result
        assert result["sourceCOpen"] is True, result
        assert result["liveKeys"] == ["session-c"], result
    finally:
        page.close()
