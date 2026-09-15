#!/usr/bin/env python3
"""Missed-completion recovery preserves the loaded window.

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
                        for mode, fade, words, width, scenario in [(mode, fade, words, width, scenario)
                                for mode in ['compact_worklog', 'transparent_stream']
                                for fade in [False]
                                for words, width in [(0, 1280), (200, 390)]
                                for scenario in os.environ.get('SCENARIOS','normal,full,revised,changed-boundary,older-during-get,switch,replacement,inline-replacement,error-replacement,timeout-replacement').split(',')]:
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
                            result=page.evaluate("""async scenario=>{
                              const history=Array.from({length:3300},(_,i)=>({role:i%2?'assistant':'user',content:'History '+i}));
                              S.messages=history.slice(3250);S.session.message_count=3300;
                              _messagesTruncated=true;_oldestIdx=3250;renderMessages({preserveScroll:true});
                              const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                              const answer='## RECOVERED ANSWER';
                              if(scenario==='inline-replacement')document.querySelector('#liveAssistantTurn')?.remove();
                              else source.emit('token',{text:answer},'run-fixture:1001');
                              await new Promise(r=>setTimeout(r,80));
                              if(scenario==='full'){S.messages=history.slice();_oldestIdx=0;_messagesTruncated=false;}
                              const oldApi=api;const oldStatus=setComposerStatus;const oldTimeout=window.setTimeout;const lateStatus=[];let requested=0,restoreTimeout=null,watchdogFired=false;
                              // Capture the real watchdog callback; fire it while its GET is pending.
                              if(scenario==='timeout-replacement')window.setTimeout=(fn,ms,...args)=>{if(ms===8000&&requested>=6){restoreTimeout=()=>fn(...args);return 0;}return oldTimeout(fn,[1500,3000,5000,8000,12000,20000].includes(ms)?0:ms,...args)};
                              setComposerStatus=(...args)=>{if(S.activeStreamId==='replacement')lateStatus.push(args);return oldStatus(...args)};
                              api=async(url,...args)=>{
                                if(['error-replacement','timeout-replacement'].includes(scenario)&&url.startsWith('/api/chat/stream/status?'))return {active:false};
                                if(!url.startsWith('/api/session?'))return oldApi(url,...args);
                                requested++;
                                if(scenario==='timeout-replacement'&&!restoreTimeout)return {};
                                await new Promise(r=>setTimeout(r,0));
                                if(requested===1&&scenario==='older-during-get'){S.messages=history.slice(3200);_oldestIdx=3200;}
                                if(scenario==='switch'){S.session={session_id:'other'};S.messages=[{role:'user',content:'OTHER SESSION'}];S.activeStreamId='other-run';INFLIGHT.other={streamId:'other-run',messages:S.messages};}
                                if(scenario.endsWith('replacement')){S.activeStreamId='replacement';S.messages=[{role:'user',content:'NEW TURN'}];INFLIGHT.fixture={streamId:'replacement',messages:S.messages};}
                                if(scenario==='timeout-replacement'){
                                  if(!restoreTimeout)throw new Error('restore watchdog not armed');
                                  Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'hidden'});
                                  watchdogFired=true;restoreTimeout();
                                }
                                const messages=[...history,{role:'user',content:'Inspect fixture'},{role:'assistant',content:answer}];
                                if(scenario==='changed-boundary')messages[3250]={role:'user',content:'REVISED HISTORY'};
                                return {session:{session_id:'fixture',messages,message_count:3302,tool_calls:[],...(scenario==='revised'?{regeneration_revision:'new'}:{})}};
                              };
                              try{
                                const started=performance.now();
                                source.emit(['error-replacement','timeout-replacement'].includes(scenario)?'error':'stream_end',{},'run-fixture:1002');
                                const deadline=performance.now()+3000;
                                while((!requested||(scenario==='timeout-replacement'&&!watchdogFired)||(S.busy&&!(scenario==='switch'||scenario.endsWith('replacement'))))&&performance.now()<deadline)await new Promise(r=>setTimeout(r,20));
                                await new Promise(r=>setTimeout(r,40));
                                if(scenario==='timeout-replacement'&&!watchdogFired)throw new Error('watchdog path not exercised');
                                if((scenario==='switch'||scenario.endsWith('replacement'))){
                                  const text=scenario==='switch'?'OTHER SESSION':'NEW TURN';
                                  if(S.messages[0].content!==text||!S.busy||S.activeStreamId!==(scenario==='switch'?'other-run':'replacement'))throw new Error('recovery overwrote successor '+scenario);
                                  if(lateStatus.length)throw new Error('stale recovery changed successor composer '+JSON.stringify(lateStatus));
                                  return {scenario,staleIgnored:true};
                                }
                                if(S.busy||!requested)throw new Error('recovery did not settle');
                                const expectedOffset=['full','revised','changed-boundary'].includes(scenario)?0:scenario==='older-during-get'?3200:3250;
                                if(S.messages.length!==3302-expectedOffset||_oldestIdx!==expectedOffset||_messagesTruncated!==(expectedOffset>0))throw new Error('recovery expanded history '+JSON.stringify({loaded:S.messages.length,offset:_oldestIdx}));
                                if(!document.querySelector('#messages').textContent.includes('RECOVERED ANSWER'))throw new Error('missing recovered answer');
                                return {scenario,loaded:S.messages.length,offset:_oldestIdx,elapsed:performance.now()-started};
                              }finally{api=oldApi;setComposerStatus=oldStatus;window.setTimeout=oldTimeout;}
                            }""",scenario)
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
