#!/usr/bin/env python3
"""Real Chromium dirty projection and attachment lifetime gate; synthetic SSE only."""
import json
import os
from pathlib import Path
import tempfile
import time

from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import _start_webui_server, _terminate_process
from browser_reconnect_scene_redraw import INIT, fixture, session_route

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='webui-dirty-projection-') as temp:
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
                browser = pw.chromium.launch(headless=True)
                try:
                    for mode in ['compact_worklog', 'transparent_stream']:
                        context = browser.new_context(viewport={'width': 1280, 'height': 900}, bypass_csp=True)
                        context.add_init_script(INIT)
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda e, errors=errors: errors.append(str(e)))
                        source = (ROOT/'static/assistant_turn_anchors.js').read_text().replace(
                            'function _activitySceneRow(event, index, mode){',
                            'function _activitySceneRow(event, index, mode){ window.fixtureRowBuilds=(window.fixtureRowBuilds||0)+1;')
                        page.route('**/static/assistant_turn_anchors.js*', lambda r, request, source=source: r.fulfill(body=source, content_type='application/javascript'))
                        messages_source = (ROOT/'static/messages.js').read_text().replace(
                            'function _wireSSE(source){',
                            'function _wireSSE(source){ window.fixtureWire=_wireSSE;')
                        page.route('**/static/messages.js*', lambda r, request, source=messages_source: r.fulfill(body=source, content_type='application/javascript'))
                        session = dict(session_id='fixture', title='Projection regression', model='',
                                       workspace=temp, messages=[], message_count=0, tool_calls=[],
                                       active_stream_id='run-fixture', pending_user_message='Inspect fixture',
                                       pending_started_at=time.time(), runtime_journal_snapshot=fixture(100))
                        page.route('**/api/session?*', session_route(session, 'fixture', temp))
                        page.route('**/api/chat/stream/status?*', lambda r: r.fulfill(json={'active': True}))
                        page.goto(base, wait_until='load')
                        page.wait_for_function("typeof loadSession==='function' && S._bootReady===true")
                        page.evaluate("""mode=>{
                          window._chatActivityDisplayMode=mode;window._showThinking=true;
                          window._simplifiedToolCalling=true;window.fixtureProjectors=[];
                          const api=window.HermesAssistantTurnAnchors;
                          if(api.createAssistantTurnAnchorActivityProjector){
                            window.HermesAssistantTurnAnchors={...api,createAssistantTurnAnchorActivityProjector(...args){
                              const p=api.createAssistantTurnAnchorActivityProjector(...args);fixtureProjectors.push(p);return p;
                            }};
                          }
                        }""", mode)
                        page.evaluate("async()=>await loadSession('fixture')")
                        page.wait_for_timeout(500)
                        result = page.evaluate("""async()=>{
                          const rows=()=>Array.from(document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="tool"]'));
                          const before=rows();if(before.length!==100)throw Error('fixture not ready');
                          const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                          _projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode());
                          window.fixtureRowBuilds=0;
                          let created=0,replaced=0;
                          const observer=new MutationObserver(records=>{for(const r of records){
                            created+=r.addedNodes.length;replaced+=r.removedNodes.length;
                          }});observer.observe(document.querySelector('#liveAssistantTurn'),{childList:true,subtree:true});
                          for(let i=0;i<5;i++){
                            source.emit('token',{text:'ordinary prose '},'run-fixture:'+(1001+i));
                            await new Promise(r=>setTimeout(r,120));
                          }
                          await new Promise(r=>setTimeout(r,300));observer.disconnect();
                          const builds=window.fixtureRowBuilds;
                          if(builds>20)throw Error('unbounded projection: '+builds+' row builds');
                          if(!before.every((node,i)=>rows()[i]===node&&node.isConnected))throw Error('DOM reuse lost');
                          const current=fixtureProjectors.at(-1);
                          if(!current)throw Error('missing attachment-owned projector');
                          const metrics=current.metrics();
                          // Detach releases all rows. A fresh attachment on the same
                          // registry must survive repeated stale-owner disposal.
                          closeLiveStream('fixture');
                          if(current.metrics().retainedRows!==0)throw Error('detach retained projection');
                          attachLiveStream('fixture','run-fixture',[],{reconnecting:true});
                          for(let i=0;i<100&&!LIVE_STREAMS.fixture;i++)await new Promise(r=>setTimeout(r,50));
                          const replacement=fixtureProjectors.at(-1);
                          _projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode());
                          current.dispose();current.invalidate();
                          if(current.project({mode:chatActivityMode()})!==null)throw Error('disposed owner revived');
                          if(replacement===current||replacement.metrics().retainedRows===0)throw Error('replacement lost '+JSON.stringify({same:replacement===current,metrics:replacement.metrics(),live:!!LIVE_STREAMS.fixture,registry:!!_liveAnchorRegistries.get('run-fixture'),count:fixtureProjectors.length,mode:chatActivityMode()}));
                          const freshSource=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                          freshSource.emit('tool',{name:'terminal',tid:'after',args:{command:'printf done'}},'run-fixture:2000');
                          freshSource.emit('tool_complete',{name:'terminal',tid:'after',snippet:'DONE',preview:'DONE'},'run-fixture:2001');
                          freshSource.emit('interim_assistant',{text:'Interim boundary'},'run-fixture:2002');
                          freshSource.emit('token',{text:'Next prose'},'run-fixture:2003');
                          await new Promise(r=>setTimeout(r,300));
                          const actual=_projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode());
                          const oracle=HermesAssistantTurnAnchors.projectAssistantTurnAnchorActivityScene(_liveAnchorRegistries.get('run-fixture'),{mode:chatActivityMode()});
                          if(JSON.stringify(actual)!==JSON.stringify(oracle))throw Error('live scene differs from full oracle');
                          const oldDispose=LIVE_STREAMS.fixture.dispose;
                          const nextSource=new EventSource('api/chat/stream?fixture');
                          fixtureWire(nextSource);
                          const sameClosure=fixtureProjectors.at(-1);
                          _projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode());
                          oldDispose(false);
                          nextSource.emit('token',{text:' AFTER STALE DISPOSE'},'run-fixture:2004');
                          await new Promise(r=>setTimeout(r,300));
                          const afterStale=_projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode());
                          if(!afterStale.activity_rows.some(r=>r.text.includes('AFTER STALE DISPOSE')))throw Error('stale dispose affected replacement');
                          if(replacement.metrics().retainedRows!==0||sameClosure.metrics().retainedRows===0)throw Error('same-closure reconnect lifetime');
                          nextSource.emit('done',{session:{session_id:'fixture',messages:[{role:'user',content:'Inspect fixture'},{role:'assistant',content:'Final fixture answer'}],message_count:2}},'run-fixture:2005');
                          await new Promise(r=>setTimeout(r,2200));
                          if(sameClosure.metrics().retainedRows!==0)throw Error('terminal retained projection');
                          closeLiveStream('fixture');
                          if(fixtureProjectors.some(p=>p.metrics().retainedRows!==0))throw Error('attachment state leaked');
                          return {builds,created,replaced,metrics,domRetained:true,detach:true,reconnect:true,staleDispose:true};
                        }""")
                        assert not errors, errors
                        print(json.dumps(dict(mode=mode, **result)), flush=True)
                        context.close()
                finally:
                    browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    main()
