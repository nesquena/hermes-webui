"""Real isolated Gateway lifecycle: cancel immediately after hidden post-tool prose."""
import json
import os
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
import browser_conversation_lifecycle as gate

POST_TOOL = 'Post-tool process checkpoint'


class CancelGateway(gate.DeterministicGateway):
    def __init__(self):
        super().__init__('terminal-error')
        self.post_tool = threading.Event()

    def _handler(self):
        parent = super()._handler()
        owner = self

        class Handler(parent):
            def _event(self, name, payload):
                super()._event(name, payload)
                if name == 'tool.completed':
                    if not owner.post_tool.wait(30):
                        return
                    super()._event('message.delta', {'event': 'message.delta', 'delta': POST_TOOL})

            def do_POST(self):
                if self.path.endswith('/stop'):
                    length = int(self.headers.get('Content-Length', '0'))
                    self.rfile.read(length)
                    self._json({'ok': True})
                    return
                super().do_POST()

        return Handler


def main():
    root = Path(__file__).resolve().parents[1]
    out = Path(os.environ['LIFECYCLE_ARTIFACT_DIR'])
    out.mkdir(parents=True, exist_ok=True)
    mode = os.environ.get('LIFECYCLE_ACTIVITY_MODE', 'compact_worklog')
    os.environ['LIFECYCLE_REASONING_FIRST'] = '1'
    gateway = CancelGateway()
    gateway.start()
    proc = log = None
    try:
        with tempfile.TemporaryDirectory(prefix='ownership-cancel-') as tmp:
            state = Path(tmp)
            agent = state / 'no-agent'
            agent.mkdir()
            (agent / 'run_agent.py').write_text('')
            workspace = state / 'workspace'
            workspace.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')}
            env.update(HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_STATE_DIR=str(state/'state'), HERMES_HOME=str(state/'home'), HERMES_BASE_HOME=str(state/'home'), HERMES_CONFIG_PATH=str(state/'home/config.yaml'), HERMES_WEBUI_SKIP_ONBOARDING='1', HERMES_WEBUI_AGENT_DIR=str(agent), HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace), HERMES_WEBUI_CHAT_BACKEND='gateway', HERMES_WEBUI_GATEWAY_BASE_URL=gateway.base_url, HERMES_WEBUI_GATEWAY_USE_RUNS_API='1', NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
            for key in ('API_SERVER_KEY', 'HERMES_WEBUI_PASSWORD', 'HERMES_WEBUI_EXTENSION_DIR', 'HERMES_WEBUI_EXTENSION_MANIFEST'):
                env.pop(key, None)
            proc, log, _, base = gate._start_webui_server(root, env, out)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                page = browser.new_page(base_url=base)
                errors = gate._capture_page_errors(page)
                requests = gate._capture_anchor_scene_requests(page)
                page.goto('/', wait_until='domcontentloaded')
                page.wait_for_selector('#msg')
                # Complete session creation before driving the composer; otherwise
                # boot/session initialization can supersede the first send.
                page.evaluate('async () => { await newSession(); }')
                page.evaluate("""mode => {
                    window._chatActivityDisplayMode=mode;
                    window.__cancelEvents=[];
                    const original=_dispatchExtensionTurnLifecycle;
                    window._dispatchExtensionTurnLifecycle=function(...args){window.__cancelEvents.push(args[0]);return original(...args);};
                    window.__insertions=[];
                    const append=Element.prototype.appendChild;
                    Element.prototype.appendChild=function(node){
                        const result=append.call(this,node);
                        if(node instanceof Element && node.matches('.assistant-segment')){
                            const record={hidden:node.hidden,classes:node.className};
                            window.__insertions.push(record);
                            if(window.__armCancel){window.__armCancel=false;record.cancel=true;queueMicrotask(()=>cancelStream('explicit-cancel'));}
                        }
                        return result;
                    };
                }""", mode)
                page.locator('#msg').fill(gate.PROMPT)
                page.locator('#btnSend').click()
                page.wait_for_selector('[data-anchor-row-role="thinking"]', timeout=30000)
                assert gateway.reasoning_ready.is_set()
                gateway.release_reasoning.set()
                page.wait_for_selector('[data-anchor-row-role="tool"]')
                page.evaluate('window.__armCancel=true')
                gateway.post_tool.set()
                try:
                    page.wait_for_function("() => window.__cancelEvents.includes('turn:cancel')", timeout=20000)
                except Exception:
                    (out/'failure-state.json').write_text(json.dumps(page.evaluate("() => ({events:window.__cancelEvents,insertions:window.__insertions,armed:window.__armCancel,stream:S.activeStreamId,busy:S.busy,text:document.body.innerText})"), indent=2))
                    (out/'fixture-events.json').write_text(json.dumps(gateway.emitted_events, indent=2))
                    (out/'errors.json').write_text(json.dumps(errors))
                    raise
                page.wait_for_function("() => !S.busy && !S.activeStreamId && !document.querySelector('#liveAssistantTurn')", timeout=15000)
                state_after = page.evaluate("""() => ({events:window.__cancelEvents,insertions:window.__insertions,messages:S.messages,streams:Object.keys(LIVE_STREAMS),text:document.querySelector('#msgInner').innerText})""")
                (out/'cancel-state.json').write_text(json.dumps(state_after, indent=2))
                assert state_after['events'].count('turn:cancel') == 1, state_after
                inserted = [r for r in state_after['insertions'] if r.get('cancel')]
                assert len(inserted) == 1 and inserted[0]['hidden'], state_after
                assert not state_after['streams'], state_after
                assert any(POST_TOOL in str(m.get('content', '')) for m in state_after['messages']), state_after
                sid = page.evaluate('S.session.session_id')
                scene = gate._wait_for_persisted_scene(base, sid, anchor_scene_requests=requests)
                page.reload(wait_until='domcontentloaded')
                page.wait_for_function("() => !S.busy && !S.activeStreamId")
                assert not page.locator('[data-live-assistant="1"]').count()
                assert not errors, errors
                (out/'result.json').write_text(json.dumps({'mode': mode, 'scene': scene, 'requests': requests, 'errors': errors, 'passed': True}, indent=2))
                browser.close()
        return 0
    finally:
        gateway.post_tool.set()
        gateway.close()
        gate._terminate_process(proc)
        if log:
            log.close()


if __name__ == '__main__':
    raise SystemExit(main())
