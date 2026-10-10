"""Real header click and incremental SSE tool completion, with isolated state."""
import json
import os
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright
import browser_conversation_lifecycle as gate


class RegateGateway(gate.DeterministicGateway):
    def __init__(self):
        self.release_tool = threading.Event()
        self.release_update = threading.Event()
        super().__init__('normal')

    def _handler(self):
        parent = super()._handler()
        owner = self

        class Handler(parent):
            def _event(self, name, payload):
                if name in ('tool.started', 'tool.completed'):
                    payload = {**payload, 'tool': 'terminal'}
                if name == 'tool.started':
                    payload = {**payload, 'args': {'command': '\n'.join(f'line-{i}' for i in range(150))}}
                if name == 'tool.completed':
                    payload = {**payload, 'preview': 'UPDATED RESULT\n' + '\n'.join(f'result-{i}' for i in range(150))}
                super()._event(name, payload)
                if name == 'tool.completed':
                    if not owner.release_update.wait(30):
                        raise RuntimeError('second tool update release timed out')
                    # Preview completion is first-write-wins; args are an
                    # updateable field on the same stable tool identity.
                    super()._event(name, {**payload, 'args': {
                        'command': 'SECOND UPDATE\n' + '\n'.join(f'line-{i}' for i in range(150))
                    }})
                if name == 'tool.started':
                    if not owner.release_tool.wait(30):
                        raise RuntimeError('tool release timed out')
        return Handler


def main():
    root = Path(__file__).resolve().parents[1]
    out = Path(os.environ['LIFECYCLE_ARTIFACT_DIR'])
    out.mkdir(parents=True, exist_ok=True)
    mode = os.environ.get('LIFECYCLE_ACTIVITY_MODE', 'compact_worklog')
    opened = os.environ.get('REGATE_OPEN', '1') == '1'
    lease = os.environ.get('REGATE_LEASE', '0') == '1'
    if lease:
        os.environ['LIFECYCLE_REASONING_FIRST'] = '1'
    gateway = RegateGateway()
    gateway.start()
    proc = log = None
    try:
        with tempfile.TemporaryDirectory(prefix='pr7478-regate-') as tmp:
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
                page = browser.new_page(base_url=base, viewport={
                    'width': int(os.environ.get('REGATE_WIDTH', '1280')), 'height': 900,
                })
                errors = gate._capture_page_errors(page)
                page.goto('/', wait_until='domcontentloaded')
                page.wait_for_selector('#msg')
                # Boot settings are asynchronous and otherwise overwrite our
                # selected presentation mode mid-stream with the saved default.
                page.wait_for_function("() => typeof window._chatActivityDisplayMode === 'string'")
                page.evaluate('async () => { await newSession(); }')
                page.evaluate('mode => {window._chatActivityDisplayMode=mode;}', mode)
                if lease:
                    page.evaluate('''() => {
                        const arm=window.setTimeout, cancel=window.clearTimeout;
                        window.__leases=new Map();
                        window.setTimeout=(fn,ms,...args)=>{
                            const id=arm(fn,ms,...args);
                            if(ms===600000)__leases.set(id,fn);
                            return id;
                        };
                        window.clearTimeout=id=>{__leases.delete(id);cancel(id);};
                        window.__expireLease=()=>{
                            if(__leases.size!==1)throw Error('expected exactly one lease');
                            const [id,fn]=[...__leases][0];cancel(id);__leases.delete(id);fn();
                            if(__leases.size!==1||window._liveAnchorRegistries.size!==1)throw Error('live lease expired');
                        };
                    }''')
                page.locator('#msg').fill(gate.PROMPT)
                page.locator('#btnSend').click()
                if lease:
                    page.wait_for_selector('[data-anchor-row-role="thinking"]')
                    page.evaluate('() => {__expireLease();__expireLease();__expireLease();}')
                    gateway.release_reasoning.set()
                card = page.locator('#liveAssistantTurn .tool-card').first
                card.wait_for(state='attached')
                (out/'before-tool.html').write_text(page.locator('#liveAssistantTurn').inner_html())
                if opened:
                    card.locator('.tool-card-header').click()
                before = card.evaluate('el => {window.__oldTool=el; const b=el.querySelector(".tool-card-detail"); if(el.classList.contains("open"))b.scrollTop=50; return {open:el.classList.contains("open"),scroll:b.scrollTop,text:el.innerText};}')
                gateway.release_tool.set()
                page.wait_for_function('() => {const el=document.querySelector("#liveAssistantTurn .tool-card");return el && el.textContent.includes("UPDATED RESULT");}')
                after = card.evaluate('el => ({open:el.classList.contains("open"),scroll:el.querySelector(".tool-card-detail").scrollTop,text:el.textContent,oldConnected:window.__oldTool.isConnected,count:document.querySelectorAll("#liveAssistantTurn .tool-card").length})')
                result = {'mode': mode, 'opened': opened, 'before': before, 'after': after, 'errors': errors}
                (out/'row-state.json').write_text(json.dumps(result, indent=2))
                assert before['open'] == opened, result
                assert after['open'] == opened, result
                assert after['scroll'] == before['scroll'], result
                assert after['count'] == 1, result
                if mode == 'compact_worklog':
                    assert not after['oldConnected'], result
                assert page.evaluate('chatActivityMode()') == mode
                gateway.release_update.set()
                page.wait_for_function('() => document.querySelector("#liveAssistantTurn .tool-card")?.textContent.includes("SECOND UPDATE")')
                second = card.evaluate('el => ({mode:chatActivityMode(),open:el.classList.contains("open"),scroll:el.querySelector(".tool-card-detail").scrollTop,count:document.querySelectorAll("#liveAssistantTurn .tool-card").length})')
                (out/'second-update.json').write_text(json.dumps(second, indent=2))
                assert second == {'mode': mode, 'open': opened, 'scroll': before['scroll'], 'count': 1}, second
                # One real click must toggle once on the replacement, not twice.
                card.locator('.tool-card-header').click()
                assert card.evaluate('el=>el.classList.contains("open")') != opened
                card.locator('.tool-card-header').click()
                gateway.release_settle.set()
                gateway.release_terminal.set()
                page.wait_for_function('() => !S.busy && Object.keys(LIVE_STREAMS).length===0', timeout=20000)
                if lease:
                    assert page.evaluate('() => window.__leases.size===0 && window._liveAnchorRegistries.size===0')
                assert not errors, errors
                browser.close()
        return 0
    finally:
        gateway.release_tool.set()
        gateway.release_update.set()
        gateway.close()
        gate._terminate_process(proc)
        if log:
            log.close()


if __name__ == '__main__':
    raise SystemExit(main())
