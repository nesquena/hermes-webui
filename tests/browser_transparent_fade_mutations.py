#!/usr/bin/env python3
"""Exercise recency-fade idempotence against the actual page and browser DOM."""
import os
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from browser_reconnect_scene_redraw import ROOT, INIT, fixture, session_route
from browser_conversation_lifecycle import _start_webui_server, _terminate_process


def main():
    with tempfile.TemporaryDirectory(prefix='fade-mutations-') as temp:
        state = Path(temp)
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TMPDIR') if k in os.environ}
        env.update(HOME=temp, HERMES_HOME=temp, HERMES_BASE_HOME=temp,
                   HERMES_WEBUI_STATE_DIR=str(state/'webui'),
                   HERMES_CONFIG_PATH=str(state/'config.yaml'),
                   HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_SKIP_ONBOARDING='1',
                   HERMES_WEBUI_AGENT_DIR=str(state/'no-agent'))
        proc, log, _, base = _start_webui_server(ROOT, env, state)
        try:
            with sync_playwright() as pw:
                for engine in ('chromium', 'webkit'):
                    browser = getattr(pw, engine).launch(headless=True)
                    try:
                        context = browser.new_context(bypass_csp=True)
                        context.add_init_script(INIT)
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e, errs=errors: errs.append(str(e)))
                        session = dict(session_id='fixture', title='Fade fixture', model='',
                                       workspace=temp, messages=[], message_count=0, tool_calls=[],
                                       active_stream_id='run-fixture', pending_user_message='Inspect fixture',
                                       pending_started_at=time.time(), runtime_journal_snapshot=fixture(10))
                        page.route('**/api/session?*', session_route(session, 'fixture', temp))
                        page.route('**/api/chat/stream/status?*', lambda r: r.fulfill(json={'active': True}))
                        page.goto(base)
                        deadline = time.monotonic() + 30
                        while not page.evaluate("typeof loadSession==='function'&&S._bootReady===true"):
                            assert time.monotonic() < deadline, 'boot timeout'
                            page.wait_for_timeout(50)
                        page.evaluate("()=>{window._chatActivityDisplayMode='transparent_stream';window._showThinking=true;window._simplifiedToolCalling=true}")
                        page.evaluate("()=>loadSession('fixture')")
                        page.wait_for_timeout(1000)
                        result = page.evaluate("""()=>{
                          const turn=document.getElementById('liveAssistantTurn');
                          const blocks=_assistantTurnBlocks(turn);
                          const rows=()=>Array.from(blocks.querySelectorAll(':scope > .transparent-event-row'));
                          const check=()=>rows().forEach((row,i,all)=>{
                            const expected=i===all.length-1?null:String(Math.min(5,all.length-1-i));
                            if(row.getAttribute('data-transparent-fade')!==expected)throw new Error('incorrect fade');
                          });
                          if(rows().length!==10)throw new Error('wrong fixture');
                          const observer=new MutationObserver(()=>{});
                          observer.observe(turn,{subtree:true,attributes:true,attributeFilter:['data-transparent-fade']});
                          _applyTransparentRowFading(turn);check();
                          const unchanged=observer.takeRecords().length;
                          if(unchanged!==0)throw new Error('unchanged fade mutated '+unchanged);
                          const newRow=document.createElement('div');newRow.className='transparent-event-row';
                          blocks.append(newRow);
                          _applyTransparentRowFading(turn);check();
                          const appended=observer.takeRecords().length;
                          if(appended!==5)throw new Error('wrong changed-row count '+appended);
                          turn.id='settled-fixture';turn.removeAttribute('data-live-assistant-turn');
                          _applyTransparentRowFading(turn);
                          if(rows().some(r=>r.hasAttribute('data-transparent-fade')))throw new Error('settlement stayed faded');
                          const cleared=observer.takeRecords().length;
                          if(cleared!==10)throw new Error('wrong settled clear count '+cleared);
                          _applyTransparentRowFading(turn);
                          if(observer.takeRecords().length!==0)throw new Error('settled fade mutated');
                          observer.disconnect();
                          return {unchanged,appended,cleared};
                        }""")
                        assert not errors, errors
                        print(engine, result, flush=True)
                        context.close()
                    finally:
                        browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    main()
