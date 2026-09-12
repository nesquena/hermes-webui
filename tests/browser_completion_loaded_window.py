#!/usr/bin/env python3
"""Completion must show canonical Markdown without waiting for word fades.

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
                            result = page.evaluate(r"""async words=>{
                              const history=Array.from({length:3300},(_,i)=>({role:i%2?'assistant':'user',content:'History '+i}));
                              S.messages=history.slice(3250);S.session.message_count=3300;
                              _messagesTruncated=true;_oldestIdx=3250;
                              renderMessages({preserveScroll:true});
                              const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                              const answer='## Completed answer\n\n**FORMATTED SENTINEL**\n\n'+('Buffered prose. '.repeat(words))+'\n\n- LAST ITEM';
                              source.emit('token',{text:answer},'run-fixture:1001');
                              await new Promise(r=>setTimeout(r,80));
                              window.__paginationFull={session_id:'fixture',messages:[...history,{role:'user',content:'Inspect fixture'},{role:'assistant',content:answer}],message_count:3302,tool_calls:[]};
                              const started=performance.now();
                              source.emit('done',{session:{session_id:'fixture',messages:[...history,{role:'user',content:'Inspect fixture'},{role:'assistant',content:answer}],message_count:3302,tool_calls:[]}},'run-fixture:1002');
                              const formatted=Array.from(document.querySelectorAll('#messages strong')).some(n=>n.textContent==='FORMATTED SENTINEL');
                              const tail=Array.from(document.querySelectorAll('#messages li')).some(n=>n.textContent==='LAST ITEM');
                              const busy=S.busy;
                              const elapsed=performance.now()-started;
                              source.emit('stream_end',{},'run-fixture:1003');
                              await new Promise(r=>setTimeout(r,1600));
                              if(!formatted||!tail||busy)throw new Error('completion waited for animation '+JSON.stringify({formatted,tail,busy,elapsed}));
                              if(Array.from(document.querySelectorAll('#messages h2')).filter(n=>n.textContent==='Completed answer').length!==1)throw new Error('duplicate or missing settled answer');
                              if(S.messages.length!==52||_oldestIdx!==3250||!_messagesTruncated)throw new Error('completion expanded history '+JSON.stringify({loaded:S.messages.length,offset:_oldestIdx}));
                              if(!document.querySelector('#loadOlderIndicator'))throw new Error('older history inaccessible');
                              if(S.messages[0].content!=='History 3250')throw new Error('loaded boundary lost');
                              return {elapsed,formatted,tail,busy,loaded:S.messages.length,nodes:document.querySelectorAll('#messages *').length};
                            }""", words)
                            print(json.dumps(dict(engine=engine,mode=mode,fade=fade,words=words,width=width,**result)),flush=True)
                            session.clear()
                            session.update(page.evaluate('window.__paginationFull'))
                            session.update(workspace=temp, model='', active_stream_id='')
                            page.evaluate("async()=>await loadSession('fixture',{force:true})")
                            assert page.evaluate("S.messages.length===52&&_oldestIdx===3250&&_messagesTruncated"), 'force refresh expanded loaded history'
                            page.evaluate("async()=>await _loadOlderMessages()")
                            assert page.evaluate("S.messages.length===3302&&_oldestIdx===0&&!_messagesTruncated"), 'explicit older load did not reveal history'
                            assert page.evaluate("S.messages[0].content==='History 0'&&S.messages.at(-1).content.includes('Completed answer')"), 'older load lost content'
                            page.evaluate("S.messages=window.__paginationFull.messages.slice(3250);_oldestIdx=3250;_messagesTruncated=true;renderMessages({preserveScroll:true})")
                            race=page.evaluate("""async()=>{
                              _captureSameSessionForceReloadHint('fixture');
                              _loadingSessionId='fixture';
                              const oldApi=api;let release;
                              api=()=>new Promise(resolve=>{release=resolve});
                              try{
                                const pending=_ensureMessagesLoaded('fixture',{force:true});
                                if(!release)throw new Error('reload request not reached');
                                S.messages=window.__paginationFull.messages.slice();
                                _oldestIdx=0;_messagesTruncated=false;
                                release({session:window.__paginationFull});
                                await pending;
                                return {loaded:S.messages.length,offset:_oldestIdx};
                              }finally{api=oldApi;_loadingSessionId=null;}
                            }""")
                            assert race['loaded']==3302 and race['offset']==0, race
                            assert not errors, errors
                            context.close()
                    finally:
                        browser.close()
        finally:
            _terminate_process(proc)
            log.close()


if __name__ == '__main__':
    main()
