#!/usr/bin/env python3
"""A bounded force-refresh must not re-hide concurrently loaded older history.

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
                        for mode, fade, words, width in [(mode, fade, words, width)
                                for mode in ['compact_worklog', 'transparent_stream']
                                for fade in [False]
                                for words, width in [(0, 1280), (200, 390)]]:
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
                            results=page.evaluate("""async()=>{
                              const history=Array.from({length:300},(_,i)=>({role:i%2?'assistant':'user',content:'History '+i}));
                              const results=[];const oldApi=api;
                              for(const scenario of ['bounded','already-full','expanded-again','switch','new-generation','failure','revision','unchanged']){
                                S.session={session_id:'fixture',message_count:300};S.messages=history.slice(250);
                                _messagesTruncated=true;_oldestIdx=250;_loadSessionGeneration=100;
                                _captureSameSessionForceReloadHint('fixture');_loadingSessionId='fixture';
                                const requests=[];
                                api=url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}));
                                try{
                                  const pending=_ensureMessagesLoaded('fixture',{force:true,loadGeneration:100});
                                  const caught=pending.catch(e=>e.message);
                                  if(requests.length!==1)throw new Error('request not reached');
                                  if(scenario!=='unchanged'){
                                    const offset=scenario==='already-full'?0:200;
                                    S.messages=history.slice(offset);_oldestIdx=offset;_messagesTruncated=offset>0;
                                  }
                                  requests[0].resolve({session:{session_id:'fixture',messages:history.slice(250),message_count:300,_messages_offset:250,_messages_truncated:true,tool_calls:[]}});
                                  await Promise.resolve();await Promise.resolve();
                                  if(scenario==='unchanged'){
                                    await caught;
                                    if(requests.length!==1||S.messages.length!==50)throw new Error('ordinary refresh changed');
                                  }else{
                                    if(requests.length!==2||requests[1].url.includes('msg_limit'))throw new Error('missing canonical catch-up');
                                    if(scenario==='expanded-again'){S.messages=history.slice(100);_oldestIdx=100;}
                                    if(scenario==='switch'){S.session={session_id:'other'};S.messages=[{role:'user',content:'OTHER'}];_loadingSessionId='other';}
                                    if(scenario==='new-generation'){_loadSessionGeneration=101;S.messages=[{role:'user',content:'NEW LOAD'}];}
                                    if(scenario==='failure')requests[1].reject(new Error('synthetic unavailable'));
                                    else requests[1].resolve({session:{session_id:'fixture',messages:history,message_count:300,tool_calls:[],...(scenario==='revision'?{regeneration_revision:'revised'}:{})}});
                                    await caught;
                                    if(scenario==='switch'){if(S.messages[0].content!=='OTHER')throw new Error('cross-session overwrite');}
                                    else if(scenario==='new-generation'){if(S.messages[0].content!=='NEW LOAD')throw new Error('stale generation overwrite');}
                                    else{
                                      const offset=scenario==='already-full'||scenario==='revision'?0:scenario==='expanded-again'?100:200;
                                      if(S.messages.length!==300-offset||_oldestIdx!==offset||S.messages[0].content!=='History '+offset)throw new Error('loaded history lost '+JSON.stringify({scenario,loaded:S.messages.length,offset:_oldestIdx}));
                                    }
                                  }
                                  results.push({scenario,loaded:S.messages.length,requests:requests.length});
                                }finally{api=oldApi;_loadingSessionId=null;}
                              }
                              return results;
                            }""")
                            for result in results:
                                print(json.dumps(dict(engine=engine,mode=mode,width=width,**result)),flush=True)
                            assert not errors, errors
                            context.close()
                    finally:
                        browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    main()
