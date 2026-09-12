#!/usr/bin/env python3
"""Terminal error and cancellation preserve the loaded history window.

Synthetic sessions/transport; no provider, credentials or production state.
Run with the repository test interpreter; BROWSERS defaults to chromium,webkit.
"""
import json
import os
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import _start_webui_server, _terminate_process
from browser_reconnect_scene_redraw import INIT, fixture, session_route

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory(prefix='webui-scene-energy-') as temp:
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
                for engine in os.environ.get('BROWSERS', 'chromium,webkit').split(','):
                    browser = getattr(pw, engine).launch(headless=True)
                    try:
                        for event, mode, fade, words, width in [(event, mode, fade, words, width)
                                for event in (['cancel'] if os.environ['CASE'].startswith('get') else ['apperror','cancel'])
                                for mode in ['compact_worklog']
                                for fade in [False]
                                for words, width in [(0, 390)]]:
                            context = browser.new_context(viewport={'width': width, 'height': 844}, bypass_csp=True)
                            context.add_init_script(INIT)
                            page = context.new_page()
                            errors = []
                            page.on('pageerror', lambda e, errors=errors: errors.append(str(e)))
                            snapshot = fixture(0)
                            session = dict(session_id='fixture', title='Energy regression', model='',
                                           workspace=temp, messages=[], message_count=0, tool_calls=[],
                                           active_stream_id='run-fixture', pending_user_message='Inspect fixture',
                                           pending_started_at=time.time(), runtime_journal_snapshot=snapshot)
                            page.route('**/api/session?*', session_route(session, 'fixture', temp))
                            page.route('**/api/chat/stream/status?*', lambda r: r.fulfill(json={'active': True}))
                            page.goto(base, wait_until='load')
                            deadline = time.monotonic()+30
                            while not page.evaluate("typeof loadSession==='function' && S._bootReady===true"):
                                assert time.monotonic() < deadline, errors
                                page.wait_for_timeout(50)
                            page.evaluate("([mode,fade])=>{window._virtualizeTranscript=false;window._chatActivityDisplayMode=mode;window._fadeTextEffect=fade;window._showThinking=true;window._simplifiedToolCalling=true}", [mode,fade])
                            page.evaluate("async()=>await loadSession('fixture')")
                            page.wait_for_timeout(500)
                            result=page.evaluate(r"""async ([event,kind])=>{
                              const history=Array.from({length:200},(_,i)=>({role:i%2?'assistant':'user',content:'History '+i}));
                              S.messages=history.slice(150);S.session.message_count=200;
                              _messagesTruncated=true;_oldestIdx=150;renderMessages({preserveScroll:true});
                              const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                              const full={session_id:'fixture',messages:[...history,{role:'assistant',content:'Canonical partial output'}],message_count:201};
                              if(kind==='revision')full.regeneration_revision='new';
                              if(kind==='boundary')full.messages[150]={role:'user',content:'Changed history'};
                              if(kind==='full'){S.messages=history.slice();_oldestIdx=0;_messagesTruncated=false;}
                              if(kind==='partial'){full.messages=full.messages.slice(148);full._messages_offset=148;full._messages_truncated=true;}
                              const oldApi=api;let release=null;
                              if(kind.startsWith('get'))api=(url,...args)=>url.startsWith('/api/session?')?new Promise(resolve=>{release=resolve}):oldApi(url,...args);
                              try{
                                source.emit(event,{session_id:'fixture',type:'cancelled',message:'Synthetic terminal',...(kind.startsWith('get')?{}:{session:full})},'run-fixture:1002');
                                if(kind.startsWith('get')){
                                  if(!release)throw new Error('cancel GET not reached');
                                  if(kind==='get-switch'){S.session={session_id:'other'};S.messages=[{role:'user',content:'Other session'}];_oldestIdx=0;_messagesTruncated=false;}
                                  if(kind==='get-older'){S.messages=history.slice(100);_oldestIdx=100;}
                                  release({session:full});
                                }
                                await new Promise(resolve=>setTimeout(resolve,100));
                                const expected=kind==='get-switch'?1:kind==='get-older'?101:['revision','boundary','full'].includes(kind)?201:51;
                                const offset=kind==='get-switch'||['revision','boundary','full'].includes(kind)?0:kind==='get-older'?100:150;
                                if(S.messages.length!==expected||_oldestIdx!==offset||_messagesTruncated!==(offset>0))throw new Error(JSON.stringify({kind,event,loaded:S.messages.length,expected,offset:_oldestIdx,truncated:_messagesTruncated}));
                                return {kind,event,loaded:S.messages.length,offset:_oldestIdx};
                              }finally{api=oldApi;}
                            }""",[event,os.environ["CASE"]])
                            print(json.dumps(dict(engine=engine,**result)),flush=True)
                            assert not errors, errors
                            context.close()
                    finally:
                        browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    for case in ["revision", "boundary", "full", "partial", "get", "get-switch", "get-older"]:
        os.environ["CASE"] = case
        main()
