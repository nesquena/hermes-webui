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
