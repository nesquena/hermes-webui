#!/usr/bin/env python3
"""Synthetic real-browser disclosure audit; no production settings/data."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'tests'))
from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import _start_webui_server,_terminate_process
from browser_reconnect_scene_redraw import INIT,fixture,session_route

def main():
    results=[]
    with tempfile.TemporaryDirectory(prefix='worklog-audit-') as temp:
        state=Path(temp)
        env={k:os.environ[k] for k in ('PATH','SYSTEMROOT','TMPDIR') if k in os.environ}
        env.update(HOME=temp,HERMES_HOME=temp,HERMES_BASE_HOME=temp,HERMES_WEBUI_STATE_DIR=str(state/'webui'),HERMES_CONFIG_PATH=str(state/'config.yaml'),HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_SKIP_ONBOARDING='1',HERMES_WEBUI_AGENT_DIR=str(state/'no-agent'))
        proc,log,_,base=_start_webui_server(ROOT,env,state)
        try:
            with sync_playwright() as pw:
                for engine in os.environ.get('BROWSERS','chromium,webkit').split(','):
                    browser=getattr(pw,engine).launch(headless=True)
                    try:
                        context=browser.new_context(viewport={'width':390,'height':844},bypass_csp=True)
                        context.add_init_script(INIT)
                        page=context.new_page(); errors=[]
                        page.on('pageerror',lambda e,errs=errors:errs.append(str(e)))
                        session=dict(session_id='fixture',title='Disclosure fixture',model='',workspace=temp,messages=[],message_count=0,tool_calls=[],active_stream_id='run-fixture',pending_user_message='Inspect fixture',pending_started_at=time.time(),runtime_journal_snapshot=fixture(3))
                        page.route('**/api/session?*',session_route(session,'fixture',temp))
                        page.route('**/api/chat/stream/status?*',lambda r:r.fulfill(json={'active':True}))
                        page.goto(base,wait_until='load')
                        deadline=time.monotonic()+30
                        while not page.evaluate("typeof loadSession==='function'&&S._bootReady===true"):
                            assert time.monotonic()<deadline,errors
                            page.wait_for_timeout(50)
                        page.evaluate("window._chatActivityDisplayMode='compact_worklog';window._virtualizeTranscript=false;window._showThinking=true;window._simplifiedToolCalling=true")
                        page.evaluate("async()=>await loadSession('fixture')")
                        page.wait_for_timeout(500)
                        result=page.evaluate(r'''async()=>{
                          const group=()=>document.querySelector('#liveAssistantTurn [data-anchor-scene-owner="1"]');
                          const g=group();if(!g)throw new Error('no live group');
                          const toggle=g.querySelector('.tool-worklog-summary,.tool-call-group-summary');
                          if(!g.classList.contains('tool-call-group-collapsed'))toggle.click();
                          toggle.click();
                          const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                          for(let i=0;i<3;i++){
                            source.emit('token',{text:'Progress '+i+' '},'run-fixture:'+(1001+i*3));
                            source.emit('tool',{name:'terminal',tid:'new-'+i,args:{command:'true'}},'run-fixture:'+(1002+i*3));
                            source.emit('tool_complete',{name:'terminal',tid:'new-'+i,preview:'OK'},'run-fixture:'+(1003+i*3));
                            await new Promise(r=>setTimeout(r,100));
                          }
                          const liveOpen=!group().classList.contains('tool-call-group-collapsed');
                          const scene=JSON.parse(JSON.stringify(_projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode())));
                          scene.activity_rows=_anchorSceneRowsForRendering(scene,{settled:false});
                          const history=Array.from({length:20},(_,i)=>({role:i%2?'assistant':'user',content:'History '+i}));
                          history[19]={role:'assistant',content:'Final answer',_anchor_stream_id:'run-fixture',_anchor_activity_scene:scene};
                          S.activeStreamId=null;S.busy=false;S.session.message_count=20;S.messages=history.slice(10);_oldestIdx=10;_messagesTruncated=true;
                          renderMessages({preserveScroll:true});
                          const settled=()=>document.querySelector('[data-anchor-settled-scene-owner="1"][data-anchor-stream-id="run-fixture"]');
                          if(!settled())throw new Error('no settled group');
                          if(!settled().classList.contains('tool-call-group-collapsed'))settled().querySelector('.tool-worklog-summary,.tool-call-group-summary').click();
                          settled().querySelector('.tool-worklog-summary,.tool-call-group-summary').click();
                          const initialKey=settled().getAttribute('data-activity-disclosure-key');
                          const savedBefore=_readActivityDisclosureState(initialKey);
                          // Remove live-key inheritance to model an already-historical turn,
                          // whose manual choice belongs to its settled group.
                          localStorage.removeItem(_activityDisclosureStorageKey('live:run-fixture'));
                          renderMessages({preserveScroll:true});
                          const sameWindowOpen=!settled().classList.contains('tool-call-group-collapsed');
                          S.messages=history;_oldestIdx=0;_messagesTruncated=false;
                          renderMessages({preserveScroll:true});
                          const expandedKey=settled().getAttribute('data-activity-disclosure-key');
                          const expandedWindowOpen=!settled().classList.contains('tool-call-group-collapsed');
                          // Explicit closed beats the default, except during the height-stable settle frame.
                          settled().querySelector('button').click();
                          window._worklogDetailsExpandedByDefault=true;
                          renderMessages({preserveScroll:true});
                          const savedClosed=!settled().classList.contains('open');
                          _armKeepSettledWorklogOpen('run-fixture');renderMessages({preserveScroll:true});
                          const forcedOpen=settled().classList.contains('open');
                          _disarmKeepSettledWorklogOpen();
                          if(!_collapseJustSettledWorklogInPlace('run-fixture'))throw new Error('collapse path not reached');
                          const collapsedAfterSettle=!settled().classList.contains('open');
                          // HTML cache round trip loses JS row stash; current row lookup must survive.
                          const copy=settled().cloneNode(true);settled().replaceWith(copy);
                          const recoveredRows=(_deferredWorklogRowsFromGroup(copy)||[]).length;
                          copy.querySelector('button').click();
                          const materialized=copy.querySelectorAll('[data-anchor-scene-row="1"]').length;
                          // Session-local choices must not leak even if a stream id is reused.
                          S.session.session_id='other';window._worklogDetailsExpandedByDefault=false;
                          renderMessages({preserveScroll:true});
                          const isolated=!settled().classList.contains('open');
                          S.session.session_id='fixture';renderMessages({preserveScroll:true});
                          const restored=settled().classList.contains('open');
                          if(!savedClosed||!forcedOpen||!collapsedAfterSettle||!recoveredRows||!materialized||!isolated||!restored)throw new Error('disclosure boundary regression '+JSON.stringify({savedClosed,forcedOpen,collapsedAfterSettle,recoveredRows,materialized,isolated,restored}));
                          return {liveOpen,savedBefore,sameWindowOpen,initialKey,expandedKey,expandedWindowOpen,savedClosed,forcedOpen,collapsedAfterSettle,recoveredRows,materialized,isolated,restored};
                        }''')
                        result.update(engine=engine,errors=errors)
                        print(json.dumps(result),flush=True);results.append(result)
                        context.close()
                    finally:browser.close()
        finally:_terminate_process(proc);log.close()
    assert all(r['liveOpen'] and r['sameWindowOpen'] and r['expandedWindowOpen'] and not r['errors'] for r in results),results
if __name__=='__main__':main()
