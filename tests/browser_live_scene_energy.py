#!/usr/bin/env python3
"""Real renderer regression: unchanged activity must not rebuild on SSE prose.

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
                        for mode in ['compact_worklog', 'transparent_stream']:
                            context = browser.new_context(viewport={'width': 390, 'height': 844}, bypass_csp=True)
                            context.add_init_script(INIT)
                            page = context.new_page()
                            errors = []
                            page.on('pageerror', lambda e, errs=errors: errs.append(str(e)))
                            snapshot = fixture(100)
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
                            page.evaluate("mode=>{window._chatActivityDisplayMode=mode;window._showThinking=true;window._simplifiedToolCalling=true}", mode)
                            page.evaluate("async()=>await loadSession('fixture')")
                            page.wait_for_timeout(500)
                            result = page.evaluate("""async()=>{
                              const select=()=>Array.from(document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="tool"]'));
                              const before=select();
                              const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                              if(!source||before.length!==100)throw new Error('fixture not ready');
                              const nestedGroup=before[0].closest('.tool-group');
                              if(nestedGroup&&!nestedGroup.classList.contains('open')) nestedGroup.querySelector('.tool-group-head').click();
                              const detail=before[0].querySelector('.tool-card-detail');
                              if(detail) detail.style.display='block';
                              const range=document.createRange();
                              range.selectNodeContents(before[0]);
                              const selection=window.getSelection();
                              selection.removeAllRanges();selection.addRange(range);
                              const selectedText=selection.toString();
                              const mutations=[];const observer=new MutationObserver(records=>{for(const r of records){for(const n of r.removedNodes){if(n===before[0]||(n.contains&&n.contains(before[0])))mutations.push({target:r.target.className,node:n.className});}}});observer.observe(document.body,{childList:true,subtree:true});
                              let builds=0;
                              const build=window.buildToolCard;
                              window.buildToolCard=function(...args){builds++;return build(...args)};
                              // Exercise the real text event, throttle and scene projection.
                              for(let i=0;i<5;i++){
                                source.emit('token',{text:'sample text '},'run-fixture:'+(1001+i));
                                await new Promise(r=>setTimeout(r,60));
                              }
                              await new Promise(r=>setTimeout(r,300));
                              const textBuilds=builds;
                              const retained=before.every((node,i)=>select()[i]===node&&node.isConnected);
                              observer.disconnect();
                              if(mutations.length)throw new Error('unchanged tool detached '+JSON.stringify(mutations));
                              if(!selectedText||selection.toString()!==selectedText)throw new Error('selection lost');
                              if(detail&&(!detail.isConnected||detail.style.display!=='block'))throw new Error('detail lost');
                              selection.removeAllRanges();
                              const snapshots=[];
                              const snapshot=window.snapshotLiveTurnHtmlForSession;
                              window.snapshotLiveTurnHtmlForSession=function(...args){snapshots.push(document.querySelector('#liveAssistantTurn').textContent);return snapshot(...args)};
                              let paints=0;
                              const paint=window._renderLiveAnchorActivitySceneForStream;
                              window._renderLiveAnchorActivitySceneForStream=function(...args){paints++;return paint(...args)};
                              source.emit('tool',{name:'terminal',tid:'later',args:{command:'printf later'},preview:'later'},'run-fixture:1006');
                              source.emit('tool_complete',{name:'terminal',tid:'later',preview:'LATER RESULT',duration:1},'run-fixture:1007');
                              const completed=select().length===101&&document.querySelector('#liveAssistantTurn').textContent.includes('LATER RESULT');
                              if(paints!==2)throw new Error('tool pair scene paints: '+paints+' (expected 2)');
                              if(!snapshots.at(-1).includes('LATER RESULT'))throw new Error('snapshot preceded completed paint');
                              source.emit('token',{text:'Before orphan '},'run-fixture:1008');
                              paints=0;
                              source.emit('tool_complete',{name:'terminal',tid:'orphan',preview:'ORPHAN ERROR',is_error:true},'run-fixture:1009');
                              if(paints!==1)throw new Error('orphan paints '+paints);
                              if(!snapshots.at(-1).includes('Before orphan')||!snapshots.at(-1).includes('ORPHAN ERROR'))throw new Error('pending prose or orphan missing from snapshot');
                              if(select().length!==102)throw new Error('orphan missing');
                              source.emit('tool_complete',{name:'terminal',tid:'orphan',snippet:'CORRECTED ORPHAN'},'run-fixture:1010');
                              if(select().length!==102||!snapshots.at(-1).includes('CORRECTED ORPHAN'))throw new Error('correction stale '+JSON.stringify({count:select().length,tail:snapshots.at(-1).slice(-1200)}));
                              const previous=S.activeStreamId;
                              const oldPaints=paints;
                              S.activeStreamId='replacement';
                              source.emit('tool',{name:'terminal',tid:'stale'},'run-fixture:1011');
                              S.activeStreamId=previous;
                              if(paints!==oldPaints||select().length!==102)throw new Error('stale stream painted');
                              window._renderLiveAnchorActivitySceneForStream=paint;
                              window.snapshotLiveTurnHtmlForSession=snapshot;
                              return {textBuilds,retained,completed};
                            }""")
                            print(json.dumps(dict(engine=engine, mode=mode, **result)), flush=True)
                            assert result == dict(textBuilds=0, retained=True, completed=True), result
                            # Corrections may mutate an existing row object; identity alone
                            # is not a valid cache key. Reordering/removal and mode switches
                            # must also invalidate ownership without duplicating tool rows.
                            page.evaluate("""()=>{
                              const scene=JSON.parse(JSON.stringify(_projectLiveAnchorActivitySceneForStream('run-fixture',chatActivityMode())));
                              scene.activity_rows=_anchorSceneRowsForRendering(scene,{settled:false});
                              const row=scene.activity_rows.find(r=>r.role==='tool');
                              row.tool.snippet='CORRECTED RESULT';
                              row.tool.preview='CORRECTED RESULT';
                              row.tool.args={command:'printf corrected'};
                              renderLiveAnchorActivityScene('run-fixture',scene,{sessionId:'fixture'});
                              if(!document.querySelector('#liveAssistantTurn').textContent.includes('CORRECTED RESULT'))throw new Error('stale correction');
                              const rows=scene.activity_rows.filter(r=>r.role==='tool').slice(0,3).reverse();
                              scene.activity_rows=[rows[0],{row_id:'between',local_id:'between',role:'prose',text:'Between tools',source_event_type:'token'},...rows.slice(1)];
                              for(let i=0;i<2;i++)renderLiveAnchorActivityScene('run-fixture',scene,{sessionId:'fixture'});
                              const ids=Array.from(document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="tool"]')).map(n=>n.getAttribute('data-anchor-row-id'));
                              if(JSON.stringify(ids)!==JSON.stringify(rows.map(r=>r.row_id)))throw new Error('reorder/removal failed');
                              window._chatActivityDisplayMode=chatActivityMode()==='compact_worklog'?'transparent_stream':'compact_worklog';
                              renderLiveAnchorActivityScene('run-fixture',scene,{sessionId:'fixture'});
                              if(document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="tool"]').length!==3)throw new Error('mode switch duplicated rows');
                            }""")
                            # Check computed animation behavior, not merely CSS source.
                            page.evaluate("""()=>{
                              const dot=document.createElement('span');dot.className='tool-card-running-dot';dot.id='energy-dot';document.body.append(dot);
                              if(getComputedStyle(dot).animationName!=='wlpulse')throw new Error('missing running pulse');
                            }""")
                            page.emulate_media(reduced_motion='reduce')
                            assert page.evaluate("getComputedStyle(document.getElementById('energy-dot')).animationName") == 'none'
                            assert not errors, errors
                            context.close()
                    finally:
                        browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    main()
