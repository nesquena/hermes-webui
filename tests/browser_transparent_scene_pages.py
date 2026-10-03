"""Real-renderer bounded server-scene disclosure gate; no user state.
Run with .venv/bin/python tests/browser_transparent_scene_pages.py.
"""
import os
import json
import hashlib
from pathlib import Path
import tempfile
import threading
from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import (
    _start_webui_server, _terminate_process, DeterministicGateway, FINAL_TEXT,
)

ROOT = Path(__file__).resolve().parent.parent
SETUP = """() => {
 window._chatActivityDisplayMode='transparent_stream'; window._transparentStream=true;
 S.session={session_id:'scene-pages'};
 const rows=Array.from({length:127},(_,i)=>({row_id:'step-'+i,local_id:'step-'+i,
 role:'tool',status:'completed',tool:{id:'tool-'+i,name:'terminal',args:{command:'printf '+i},snippet:'result '+i,done:true}}));
 window.sceneMessage={role:'assistant',content:'FINAL ANSWER',_anchor_stream_id:'scene-run',
 _anchor_activity_scene:{version:'activity_scene_v1',stream_id:'scene-run',final_answer:'FINAL ANSWER',activity_rows:rows}};
 S.messages=[sceneMessage];
 clearMessageRenderCache(); renderMessages();
 window.seg=document.querySelector('.assistant-segment');
} """
ROWS = "() => [...document.querySelectorAll('.transparent-event-row[data-anchor-row-id]')].map(n=>n.getAttribute('data-anchor-row-id'))"

class SceneCheckpoint(threading.Event):
    """Allow slow CI browsers to consume the large fixture before settlement."""
    def wait(self, timeout=None):
        return super().wait(timeout=120 if timeout == 30 else timeout)


class SceneGateway(DeterministicGateway):
    """Exercise real Gateway/SSE/renderer handoff with a long tool scene."""
    def __init__(self, mode):
        super().__init__(mode)
        self.release_settle = SceneCheckpoint()
        self.release_terminal = SceneCheckpoint()

    def _handler(self):
        handler=super()._handler()
        original=handler._event
        def event(instance, name, payload):
            original(instance,name,payload)
            if name=='tool.completed':
                for i in range(126):
                    tool={'tool':'terminal','tool_call_id':f'continuity-{i}'}
                    original(instance,'tool.started',dict(tool,event='tool.started',status='running',args={'command':f'printf {i}'}))
                    original(instance,'tool.completed',dict(tool,event='tool.completed',status='completed',preview=f'continuity result {i}'))
        handler._event=event
        return handler


def live_settle(page, gateway):
    # Keep diagnostics local to this synthetic fixture. Record the snapshots
    # production actually chose, without taking extra geometry snapshots or
    # changing timing, pin state, row selection, or the continuity assertions.
    page.evaluate("""() => {
      const trace=[];
      const originals=new Map();
      const snapshot=s=>s?{pinned:s.pinned,reader:s.transparentSceneReader}:null;
      for(const name of ['_captureMessageScrollSnapshot','_retainTransparentSceneReader','clearLiveToolCards']){
        const original=window[name];
        if(typeof original!=='function') throw Error('Missing diagnostic hook '+name);
        originals.set(name,original);
        window[name]=function(...args){
          const result=original.apply(this,args);
          trace.push({name,time:performance.now(),pinned:_scrollPinned,
            unpinned:_messageUserUnpinned,
            snapshot:snapshot(name==='_captureMessageScrollSnapshot'?result:args[0])});
          if(trace.length>128) trace.shift();
          return result;
        };
      }
      window.__sceneSettleDiagnostics={trace,restore(){
        for(const [name,original] of originals) window[name]=original;
        delete window.__sceneSettleDiagnostics;
      }};
    }""")
    try:
        return _live_settle(page, gateway)
    except Exception:
        print('SCENE SETTLE DIAGNOSTICS', json.dumps(page.evaluate("""() => ({
          trace:window.__sceneSettleDiagnostics.trace,
          pinned:_scrollPinned,unpinned:_messageUserUnpinned,
          busy:S.busy,activeStreamId:S.activeStreamId,
          rows:[...document.querySelectorAll('.transparent-event-row[data-anchor-row-id]')]
            .map(row=>row.getAttribute('data-anchor-row-id')),
          scenes:(S.messages||[]).filter(m=>m._anchor_activity_scene).map(m=>{
            const scene=m._anchor_activity_scene;
            const state=_transparentScenePages.get(scene);
            return {streamId:scene.stream_id,start:state&&state.start,
              rows:(scene.activity_rows||[]).map(row=>row.row_id||row.local_id)};
          })
        })""")), flush=True)
        raise
    finally:
        page.evaluate('() => window.__sceneSettleDiagnostics.restore()')


def _live_settle(page,gateway):
    gateway.activity_ready.clear();gateway.release_settle.clear()
    gateway.final_prefix_ready.clear();gateway.release_terminal.clear()
    page.evaluate("S.session=null;S.messages=[];clearMessageRenderCache();renderMessages()")
    page.locator('#msg').fill('Run deterministic long scene continuity fixture')
    page.locator('#btnSend').click()
    assert gateway.activity_ready.wait(15), 'Gateway live checkpoint missing'
    try:
        page.wait_for_function("() => document.querySelectorAll('#liveAssistantTurn .transparent-event-row').length>=128",timeout=60000,polling=100)
    except Exception:
        print('LIVE CHECKPOINT',page.evaluate("({rows:document.querySelectorAll('#liveAssistantTurn .transparent-event-row').length,busy:S.busy,text:document.getElementById('msgInner').innerText.slice(-1800)})"),flush=True)
        raise
    page.wait_for_timeout(500)
    # Genuine wheel intent, then select a live row intersecting the viewport.
    page.locator('#messages').hover()
    page.mouse.wheel(0,-1800)
    page.wait_for_timeout(350)
    before=page.evaluate("""() => {
      const viewport=document.getElementById('messages').getBoundingClientRect();
      const rows=[...document.querySelectorAll('#liveAssistantTurn .transparent-event-row[data-anchor-row-id]')];
      const row=rows.find(r=>r.getBoundingClientRect().top>=viewport.top+40&&r.getBoundingClientRect().bottom<viewport.bottom-40);
      if(!row) throw Error('No live reader row');
      return {id:row.getAttribute('data-anchor-row-id'),y:row.getBoundingClientRect().top};
    }""")
    artifact=Path("/tmp/hermes-scene-pages-evidence")/f"{page.context.browser.browser_type.name}-{page.viewport_size['width']}"
    page.screenshot(path=str(artifact)+"-live.png")
    gateway.release_settle.set()
    assert gateway.final_prefix_ready.wait(10)
    gateway.release_terminal.set()
    page.wait_for_function("() => !S.busy&&!S.activeStreamId&&!document.querySelector('#liveAssistantTurn')",timeout=20000,polling=100)
    page.wait_for_timeout(350)
    after=page.evaluate("""id => {
      const row=[...document.querySelectorAll('.transparent-event-row[data-anchor-row-id]')].find(r=>r.getAttribute('data-anchor-row-id')===id);
      return row?{y:row.getBoundingClientRect().top}:null;
    }""",before['id'])
    assert after is not None, ('live reader row evicted at settle',before)
    assert abs(after['y']-before['y'])<=4, ('live reader jumped at settle',before,after)
    assert FINAL_TEXT in page.locator('#msgInner').inner_text()
    page.screenshot(path=str(artifact)+"-settled.png")
    return {"row":before['id'],"before_y":before['y'],"after_y":after['y'],"delta":abs(after['y']-before['y'])}


def main():
    artifacts=Path('/tmp/hermes-scene-pages-evidence'); artifacts.mkdir(exist_ok=True)
    sources=['static/ui.js','static/sessions.js','static/messages.js','tests/browser_transparent_scene_pages.py']
    hashes={str(p):hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sources}
    results=[]
    with tempfile.TemporaryDirectory(prefix='scene-pages-') as temp:
        env=dict({k:os.environ[k] for k in ('PATH','SYSTEMROOT','TMPDIR') if k in os.environ}, HOME=temp,HERMES_HOME=temp,HERMES_BASE_HOME=temp,
                 HERMES_WEBUI_STATE_DIR=temp+'/webui',HERMES_CONFIG_PATH=temp+'/config.yaml',
                 HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_SKIP_ONBOARDING='1',
                 HERMES_WEBUI_AGENT_DIR=temp+'/no-agent',HERMES_WEBUI_DEFAULT_WORKSPACE=temp)
        gateway=SceneGateway('normal');gateway.start()
        agent=Path(temp)/'no-agent';agent.mkdir()
        (agent/'run_agent.py').write_text('')
        env.update(HERMES_WEBUI_CHAT_BACKEND='gateway',HERMES_WEBUI_GATEWAY_BASE_URL=gateway.base_url,
                   HERMES_WEBUI_GATEWAY_USE_RUNS_API='1',NO_PROXY='127.0.0.1,localhost',no_proxy='127.0.0.1,localhost')
        proc,log,_,base=_start_webui_server(ROOT,env,artifacts)
        try:
            with sync_playwright() as pw:
                for engine in os.environ.get('BROWSERS','chromium,webkit').split(','):
                    if os.environ.get('SCENE_ENGINE') and os.environ['SCENE_ENGINE']!=engine: continue
                    browser=getattr(pw,engine).launch(headless=True)
                    for label,width,height in [('desktop',1440,1000),('narrow',800,900),('mobile',390,844)]:
                        if os.environ.get('SCENE_VIEWPORT') and os.environ['SCENE_VIEWPORT']!=label: continue
                        if os.environ.get('VIEWPORTS') and label not in os.environ['VIEWPORTS'].split(','): continue
                        page=browser.new_page(viewport={'width':width,'height':height}, bypass_csp=True)
                        page.goto(base,wait_until='load')
                        page.wait_for_function("() => typeof S!=='undefined' && S._bootReady===true")
                        page.evaluate(SETUP)
                        initial=page.evaluate(ROWS); assert len(initial)==30, initial
                        page.locator('.transparent-earlier-steps').first.click()
                        found=page.evaluate(ROWS)
                        page.screenshot(path=str(artifacts/f'{engine}-{label}.png'))
                        assert 0<len(found)<=40, ('reveal unbounded',len(found))
                        page.locator('[data-transparent-expand-all]').press('Enter')
                        assert page.evaluate(ROWS)==found, 'Expand all must not navigate either page control'
                        assert page.locator('.transparent-event-row .tool-card:not(.open)').count()==0
                        assert page.locator('.transparent-event-row .tool-card-detail').count()==len(found)
                        page.locator('[data-transparent-collapse-all]').click()
                        assert page.evaluate(ROWS)==found
                        assert page.locator('.transparent-event-row .tool-card.open').count()==0
                        page.locator('[data-scene-page-direction="later"]').click()
                        assert page.evaluate(ROWS)==initial, 'later must return to original tail'
                        page.locator('.transparent-earlier-steps').first.click()
                        seen=set(initial+found)
                        for _ in range(10):
                            earlier=page.locator('[data-anchor-earlier-steps="1"]:not([data-scene-page-direction="later"])')
                            if not earlier.count(): break
                            earlier.first.click(); batch=page.evaluate(ROWS)
                            assert 0<len(batch)<=40
                            nums=[int(x.split('-')[-1]) for x in batch]; assert nums==sorted(nums)
                            seen.update(batch)
                        assert seen=={f'step-{i}' for i in range(127)}, len(seen)
                        page.evaluate("_renderSettledAnchorSceneTransparentForMessage(sceneMessage,seg,0)")
                        assert len(page.evaluate(ROWS))<=40
                        # innerHTML cache round-trip discards listeners and JS stashes.
                        cached=page.evaluate(ROWS)
                        page.evaluate("const root=document.getElementById('msgInner');root.innerHTML=root.innerHTML;_rehydrateTransparentStreamDom(root);window.seg=root.querySelector('.assistant-segment')")
                        assert page.evaluate(ROWS)==cached
                        page.locator('[data-scene-page-direction="later"]').click()
                        assert page.evaluate(ROWS)!=cached
                        card=page.locator('.transparent-event-row .tool-card').first
                        card.locator('.tool-card-header').click()
                        assert card.locator('.tool-card-detail').count()==1
                        assert 'result ' in card.inner_text()
                        assert page.locator('.msg-body').last.inner_text()=='FINAL ANSWER'
                        page.evaluate("sceneMessage=structuredClone(sceneMessage);S.messages=[sceneMessage];_armKeepSettledWorklogOpen('scene-run');_renderSettledAnchorSceneTransparentForMessage(sceneMessage,seg,0);_disarmKeepSettledWorklogOpen()")
                        assert len(page.evaluate(ROWS))==30
                        # Failed tool detail stays accessible; the turn-level user
                        # toggle must not turn page navigation into row destruction.
                        page.evaluate("sceneMessage._anchor_activity_scene.terminal_state='error';sceneMessage._anchor_activity_scene.activity_rows[120].tool.is_error=true;sceneMessage._anchor_activity_scene.activity_rows[120].tool.snippet='ERROR fixture';_renderSettledAnchorSceneTransparentForMessage(sceneMessage,seg,0)")
                        error=page.locator('[data-anchor-row-id="step-120"]')
                        error.locator('.tool-card-header').click()
                        assert 'ERROR fixture' in error.inner_text()
                        role=page.locator('.assistant-turn .msg-role.assistant').first
                        role.press('Enter')
                        assert page.locator('.assistant-turn').first.get_attribute('data-transparent-turn-collapsed')=='1'
                        role.press('Space')
                        assert page.locator('.assistant-turn').first.get_attribute('data-transparent-turn-collapsed')=='0'
                        assert error.locator('.tool-card-detail').count()==1
                        # Many moderate server scenes are below the per-turn cap;
                        # the entire mounted transcript must still stay bounded.
                        page.evaluate("""() => {
                          const template=sceneMessage;
                          S.messages=Array.from({length:24},(_,i)=>[
                            {role:'user',content:'Request '+i},
                            {...structuredClone(template),content:'Answer '+i,
                             _anchor_stream_id:'moderate-'+i,
                             _anchor_activity_scene:{...structuredClone(template._anchor_activity_scene),
                               stream_id:'moderate-'+i, final_answer:'Answer '+i,
                               activity_rows:template._anchor_activity_scene.activity_rows.slice(0,12)}}
                          ]).flat();
                          clearMessageRenderCache();renderMessages();
                        }""")
                        total=page.locator('.transparent-event-row,.assistant-segment,.msg-row[data-role="user"],.transparent-earlier-steps,.transparent-event-controls').count()
                        assert total<190, ('many moderate scenes exceeded transcript bound',total)
                        # Paging remains complete even when the global budget reduces
                        # moderate scenes below their normal twelve-row page.
                        turn=page.locator('.assistant-turn').last
                        accessed=set(turn.locator('[data-anchor-row-id]').evaluate_all("nodes=>nodes.map(n=>n.getAttribute('data-anchor-row-id'))"))
                        for _ in range(12):
                            previous=turn.locator('.transparent-earlier-steps:not([data-scene-page-direction])')
                            if not previous.count(): break
                            previous.press('Enter')
                            accessed.update(turn.locator('[data-anchor-row-id]').evaluate_all("nodes=>nodes.map(n=>n.getAttribute('data-anchor-row-id'))"))
                            assert page.locator('.transparent-event-row,.assistant-segment,.msg-row[data-role="user"],.transparent-earlier-steps,.transparent-event-controls').count()<190
                        assert accessed=={f'step-{i}' for i in range(12)}
                        print(engine,label,'mounted total',total,flush=True)
                        if os.environ.get('SCENE_SKIP_LIFECYCLE')!='1':
                            continuity=live_settle(page,gateway)
                            results.append(dict(engine=engine,viewport=label,continuity=continuity))
                            (artifacts/'results.json').write_text(json.dumps(dict(source_hashes=hashes,cases=results),indent=2))
                        print(engine,label,'PASS: ordered access, later/cache/controls/replacement, global scene bound; lifecycle '+('SKIPPED' if os.environ.get('SCENE_SKIP_LIFECYCLE')=='1' else 'PASS'))
                        page.close()
                    browser.close()
        finally:
            _terminate_process(proc); log.close();gateway.close()

if __name__=='__main__': main()
