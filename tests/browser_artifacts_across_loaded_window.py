#!/usr/bin/env python3
"""Artifacts survive terminal settlement across the loaded-window boundary.

A mutation tool call BEFORE the loaded boundary (dropped by the settlement
slice) and one INSIDE it must both surface in the workspace Artifacts
projection after real done / apperror / cancel settlement, exactly once each,
while the transcript stays at the reader's loaded boundary.
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
    with tempfile.TemporaryDirectory(prefix='webui-artifacts-window-') as temp:
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
                for engine in os.environ.get('BROWSERS', 'chromium').split(','):
                    browser = getattr(pw, engine).launch(headless=True)
                    try:
                        for event in ['done', 'apperror', 'cancel']:
                            context = browser.new_context(viewport={'width': 1280, 'height': 844}, bypass_csp=True)
                            context.add_init_script(INIT)
                            page = context.new_page()
                            errors = []
                            page.on('pageerror', lambda e, errors=errors: errors.append(str(e)))
                            snapshot = fixture(0)
                            session = dict(session_id='fixture', title='Artifact window regression', model='',
                                           workspace=temp, messages=[], message_count=0, tool_calls=[],
                                           active_stream_id='run-fixture', pending_user_message='Inspect fixture',
                                           pending_started_at=time.time(), runtime_journal_snapshot=snapshot)
                            page.route('**/api/session?*', session_route(session, 'fixture', temp))
                            page.route('**/api/chat/stream/status?*', lambda r: r.fulfill(json={'active': True}))
                            page.goto(base, wait_until='load')
                            deadline = time.monotonic() + 30
                            while not page.evaluate("typeof loadSession==='function' && S._bootReady===true"):
                                assert time.monotonic() < deadline, errors
                                page.wait_for_timeout(50)
                            page.evaluate("window._virtualizeTranscript=false;window._chatActivityDisplayMode='compact_worklog';window._showThinking=true;window._simplifiedToolCalling=true")
                            page.evaluate("async()=>await loadSession('fixture')")
                            page.wait_for_timeout(400)
                            result = page.evaluate(r"""async (event)=>{
                              const history=Array.from({length:3300},(_,i)=>({role:i%2?'assistant':'user',content:'History '+i}));
                              // Old mutation lives BEFORE the loaded boundary (dropped
                              // head); new mutation inside the resident tail.
                              const oldMutation={role:'assistant',content:'History 3199',tool_calls:[{function:{name:'write_file',arguments:JSON.stringify({path:'/workspace/OLD_artifact.md'})}}]};
                              history[3199]=oldMutation;
                              const newMutation={role:'assistant',content:'Inspect fixture',tool_calls:[{function:{name:'write_file',arguments:JSON.stringify({path:'/workspace/NEW_artifact.md'})}}]};
                              const answer='Settled answer';
                              const full=[...history,newMutation,{role:'assistant',content:answer}];
                              S.messages=history.slice(3250);S.session.message_count=3302;
                              _messagesTruncated=true;_oldestIdx=3250;
                              renderMessages({preserveScroll:true});
                              const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                              const payload={session_id:'fixture',messages:full,message_count:3302,tool_calls:[]};
                              window.__paginationFull=payload;
                              source.emit(event,{session_id:'fixture',type:event==='cancel'?'cancelled':(event==='done'?'completed':'rate_limit'),message:'Synthetic terminal',session:payload},'run-fixture:1002');
                              await new Promise(r=>setTimeout(r,1500));
                              if(S.messages.length!==52||_oldestIdx!==3250||!_messagesTruncated)throw new Error('boundary changed '+JSON.stringify({loaded:S.messages.length,offset:_oldestIdx}));
                              const items=collectSessionArtifacts();
                              const paths=items.map(i=>i.path);
                              const oldCount=paths.filter(p=>p.endsWith('OLD_artifact.md')).length;
                              const newCount=paths.filter(p=>p.endsWith('NEW_artifact.md')).length;
                              renderSessionArtifacts();
                              const domOld=!!document.querySelector('[data-artifact-path$="OLD_artifact.md"]');
                              const domNew=!!document.querySelector('[data-artifact-path$="NEW_artifact.md"]');
                              return {loaded:S.messages.length,offset:_oldestIdx,oldCount,newCount,domOld,domNew,count:document.getElementById('workspaceArtifactsCount').textContent};
                            }""", event)
                            assert result['oldCount']==1, (event, result)
                            assert result['newCount']==1, (event, result)
                            assert result['domOld'] and result['domNew'], (event, result)
                            assert result['count']=='2', (event, result)
                            print(json.dumps(dict(event=event, engine=engine, **result)), flush=True)
                            # Full history load makes the head resident; the harvested
                            # registry must clear so the artifact is NOT double-counted.
                            session.clear()
                            session.update(page.evaluate('window.__paginationFull'))
                            session.update(workspace=temp, model='', active_stream_id='')
                            page.evaluate("async()=>await loadSession('fixture',{force:true})")
                            page.evaluate("async()=>await _loadOlderMessages()")
                            after = page.evaluate("""()=>{
                              const items=collectSessionArtifacts();
                              const paths=items.map(i=>i.path);
                              return {old:paths.filter(p=>p.endsWith('OLD_artifact.md')).length,
                                      new:paths.filter(p=>p.endsWith('NEW_artifact.md')).length,
                                      truncated:_messagesTruncated};
                            }""")
                            assert after['old']==1 and after['new']==1, ('double-count after full load', after)
                            assert not errors, errors
                            context.close()
                    finally:
                        browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    main()
