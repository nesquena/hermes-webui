#!/usr/bin/env python3
"""Real page/loadSession/renderers with deterministic session and SSE fixtures.

No agent, provider, production state, or credentials. Tests DOM reconstruction,
not a real provider/network reconnect.
"""
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

from browser_conversation_lifecycle import _start_webui_server, _terminate_process

ROOT = Path(os.environ.get('WEBUI_TEST_ROOT', Path(__file__).resolve().parent.parent))


def fixture(count):
    tools = [dict(name='terminal', tid=f'tool-{i}', args={'command': f'printf result-{i}'},
                  preview=f'result-{i}', snippet=f'result-{i}', done=True) for i in range(count)]
    rows = [dict(row_id=f'tool:{t["tid"]}', local_id=t['tid'], role='tool', kind='tool_call',
                 source_event_type='tool_complete', status='completed', order_index=i,
                 tool=t, payload=t) for i, t in enumerate(tools)]
    return dict(stream_id='run-fixture', last_seq=1000, last_event_id='run-fixture:1000',
                messages=[], tool_calls=tools, last_assistant_text='', last_reasoning_text='',
                anchor_activity_scene=dict(version='activity_scene_v1',
                                           identity=dict(session_id='fixture', stream_id='run-fixture', run_id='run-fixture'),
                                           activity_rows=rows))


INIT = """
// A controlling service worker bypasses Playwright's page route fixtures in
// WebKit. This test targets reconnect rendering, not PWA cache behavior.
if(window===window.top && 'serviceWorker' in navigator){
  navigator.serviceWorker.register=()=>Promise.reject(new Error('Disabled in reconnect harness'));
}
window.fixtureSources=[];
class FixtureEventSource {
  static OPEN=1; static CONNECTING=0; static CLOSED=2;
  constructor(url){this.url=String(url);this.readyState=1;this.listeners={};window.fixtureSources.push(this);}
  addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}
  removeEventListener(){}
  close(){this.readyState=2;}
  emit(name,data,id){for(const fn of this.listeners[name]||[])fn({data:JSON.stringify(data),lastEventId:id||''});}
}
window.EventSource=FixtureEventSource;
"""


def session_route(session, sid, workspace):
    def handle(route):
        query = parse_qs(urlsplit(route.request.url).query)
        requested = query.get('session_id', [''])[0]
        data = session if requested == sid else dict(
            session_id='idle-fixture', messages=[], message_count=0,
            tool_calls=[], workspace=workspace)
        route.fulfill(json={'session': data})
    return handle


def main():
    tool_count = int(os.environ.get('TOOL_COUNT', '470'))
    snapshot = fixture(tool_count)
    stream_id = snapshot['stream_id']
    sid = 'fixture'
    with tempfile.TemporaryDirectory(prefix='webui-reconnect-') as temp:
        state = Path(temp)
        env = {k:os.environ[k] for k in ('PATH','SYSTEMROOT','TMPDIR') if k in os.environ}
        env.update(HOME=temp, HERMES_HOME=temp, HERMES_BASE_HOME=temp,
                   HERMES_WEBUI_STATE_DIR=str(state/'webui'), HERMES_CONFIG_PATH=str(state/'config.yaml'),
                   HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_SKIP_ONBOARDING='1',
                   HERMES_WEBUI_AGENT_DIR=str(state/'no-agent'))
        proc, log, _, base = _start_webui_server(ROOT, env, state)
        results = []
        try:
            with sync_playwright() as pw:
                for engine in os.environ.get('BROWSERS','chromium,webkit').split(','):
                    browser = getattr(pw,engine).launch(headless=True)
                    try:
                        for mode in os.environ.get('MODES','compact_worklog,transparent_stream,hide_all_activity').split(','):
                            for width in [1280,390]:
                                context = browser.new_context(viewport={'width':width,'height':844},bypass_csp=True)
                                context.add_init_script(INIT)
                                page = context.new_page()

                                errors=[]
                                page.on('pageerror',lambda e, errors=errors:errors.append(str(e)))
                                session = dict(session_id=sid,title='Reconnect performance fixture',model='',
                                               workspace=temp,messages=[],message_count=0,tool_calls=[],
                                               active_stream_id=stream_id,pending_user_message='Inspect the fixture',
                                               pending_started_at=1,runtime_journal_snapshot=snapshot)
                                page.route('**/api/session?*',session_route(session,sid,temp))
                                page.route('**/api/chat/stream/status?*',lambda r:r.fulfill(json={'active':True}))
                                page.goto(base,wait_until='load')
                                # WebKit's wait_for_function uses eval, blocked by
                                # the app CSP even with bypass_csp. Inspector
                                # evaluation still works; wait on actual boot state.
                                deadline=time.monotonic()+30
                                while not page.evaluate("typeof loadSession==='function' && S._bootReady === true"):
                                    assert time.monotonic()<deadline, ('boot timeout', errors)
                                    page.wait_for_timeout(50)
                                page.evaluate("""mode=>{
                                  window._chatActivityDisplayMode=mode;window._showThinking=true;
                                  window._simplifiedToolCalling=true;
                                  window.redrawCount=0;
                                  const render=window._renderLiveAnchorActivitySceneForStream;
                                  window._renderLiveAnchorActivitySceneForStream=function(...args){window.redrawCount++;return render(...args);};
                                }""",mode)
                                measured=page.evaluate("""async sid=>{
                                  const t=performance.now();await loadSession(sid);
                                  const loadMs=performance.now()-t;
                                  const deadline=performance.now()+5000;
                                  while(!fixtureSources.some(s=>s.url.includes('api/chat/stream?'))&&performance.now()<deadline){
                                    await new Promise(r=>setTimeout(r,10));
                                  }
                                  return {ms:loadMs,redraws:window.redrawCount,
                                    tools:document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="tool"]').length,
                                    busy:S.busy,
                                    sources:fixtureSources.filter(s=>s.url.includes('api/chat/stream?')).map(s=>s.url)};
                                }""",sid)
                                result=dict(engine=engine,mode=mode,width=width,
                                            **{k:v for k,v in measured.items() if k!='sources'})
                                print(json.dumps(result),flush=True)
                                results.append(result)
                                if not os.environ.get('MEASURE_BASELINE'):
                                    assert measured['redraws']<=2,result
                                expected=0 if mode=='hide_all_activity' else tool_count
                                assert measured['tools']==expected,result
                                assert measured['busy'],result
                                assert measured['sources'], (result, errors)
                                cursor=parse_qs(urlsplit(measured['sources'][-1]).query)
                                assert cursor['after_seq']==[str(snapshot['last_seq'])], (result,errors)
                                if mode!='hide_all_activity':
                                    # Subsequent real SSE handler updates still paint the existing owner.
                                    updated=page.evaluate("""({sid,stream})=>{
                                      const source=fixtureSources.findLast(s=>s.url.includes('api/chat/stream?')&&s.readyState===1);
                                      if(!source)throw new Error('missing chat stream');
                                      source.emit('tool',{name:'terminal',tid:'after-reconnect',args:{command:'printf later'},preview:'later'},stream+':1001');
                                      source.emit('tool_complete',{name:'terminal',tid:'after-reconnect',preview:'LATER RESULT',duration:1},stream+':1002');
                                      return {rows:document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="tool"]').length,
                                        result:document.querySelector('#liveAssistantTurn').textContent.includes('LATER RESULT')};
                                    }""",{'sid':sid,'stream':stream_id})
                                    assert updated=={'rows':expected+1,'result':True},updated
                                    switched=page.evaluate("""async sid=>{
                                      await loadSession('idle-fixture');
                                      await loadSession(sid);
                                      return {rows:document.querySelectorAll('#liveAssistantTurn [data-anchor-row-role="tool"]').length,
                                        result:document.querySelector('#liveAssistantTurn').textContent.includes('LATER RESULT')};
                                    }""",sid)
                                    assert switched==updated,switched
                                artifact=os.environ.get('SCREENSHOT_DIR')
                                if artifact:
                                    Path(artifact).mkdir(parents=True,exist_ok=True)
                                    page.screenshot(path=str(Path(artifact)/f'{engine}-{mode}-{width}.png'))
                                assert not errors,errors
                                context.close()
                    finally:
                        browser.close()
            output=os.environ.get('METRICS_FILE')
            if output:
                Path(output).write_text(json.dumps(results,indent=2))
        finally:
            _terminate_process(proc)
            log.close()


if __name__=='__main__':
    main()
