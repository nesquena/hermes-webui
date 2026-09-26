"""Real-renderer scene-budget/owner regression; isolated temporary state only."""
import os
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright
from browser_conversation_lifecycle import _start_webui_server, _terminate_process

ROOT = Path(__file__).resolve().parent.parent
COUNT = '.transparent-event-row,.assistant-segment,.msg-row[data-role="user"],.transparent-earlier-steps,.transparent-event-controls'
SETUP = """() => {
 window._virtualizeTranscript=true;
 window._chatActivityDisplayMode='transparent_stream';window._transparentStream=true;
 S.session={session_id:'budget-ownership'};
 window.makePair=i=>[{role:'user',content:'Request '+i},{role:'assistant',content:'Answer '+i,
 _anchor_stream_id:'budget-'+i,_anchor_activity_scene:{version:'activity_scene_v1',stream_id:'budget-'+i,
 final_answer:'Answer '+i,activity_rows:Array.from({length:12},(_,j)=>({row_id:'owner-'+i+'-step-'+j,
 local_id:'owner-'+i+'-step-'+j,role:'tool',status:'completed',tool:{id:i+'-'+j,name:'terminal',args:{command:'printf '+j},snippet:'result '+j,done:true}}))}}];
 S.messages=Array.from({length:70},(_,i)=>makePair(i)).flat();
 clearMessageRenderCache();renderMessages();
} """

def check(page):
    page.evaluate(SETUP)
    assert page.evaluate('S.messages.length') > 100
    print('cold',page.locator(COUNT).count(),page.evaluate("({budget:_transparentScenePageSize(),segments:document.querySelectorAll('.assistant-segment').length,virtual:_messageVirtualWindowKey})"),flush=True)
    assert page.locator(COUNT).count() < 190
    initial = page.evaluate('_transparentScenePageSize()')
    # A cold render and an actual virtual-window scroll must use the same budget.
    page.locator('#messages').hover()
    page.mouse.wheel(0,-100000)
    page.wait_for_timeout(350)
    page.evaluate("_scrollPinned=false;_messageUserUnpinned=true;document.getElementById('messages').scrollTop=0;renderMessages({preserveScroll:true})")
    page.wait_for_timeout(150)
    assert page.locator(COUNT).count() < 190
    assert page.evaluate('_transparentScenePageSize()') == initial, 'budget changed across virtual remount'
    page.evaluate("S.messages=makePair(-1).concat(S.messages);clearMessageRenderCache();renderMessages({preserveScroll:true})")
    assert page.locator(COUNT).count() < 190
    assert page.evaluate('_transparentScenePageSize()') == initial
    # Keep the attached old handler, replace the scene at exactly its source index.
    result=page.evaluate("""() => {
      const button=document.querySelector('.transparent-earlier-steps');
      const idx=Number(button.getAttribute('data-anchor-owner-idx'));
      const segment=document.querySelector('.assistant-segment[data-msg-idx="'+idx+'"]');
      const replacement=makePair(999)[1];S.messages[idx]=replacement;
      button.click();
      const ids=[...segment.closest('.assistant-turn').querySelectorAll('[data-anchor-row-id]')].map(n=>n.getAttribute('data-anchor-row-id'));
      return ids;
    }""")
    assert result and all(x.startswith('owner-999-') for x in result), result
    # Simulate the real prepend reindexing of retained DOM before its handler runs.
    result=page.evaluate("""() => {
      const button=document.querySelector('.transparent-earlier-steps');
      const oldIdx=Number(button.getAttribute('data-anchor-owner-idx'));
      const segment=document.querySelector('.assistant-segment[data-msg-idx="'+oldIdx+'"]');
      const expected=S.messages[oldIdx]._anchor_activity_scene.activity_rows[0].row_id.split('-step-')[0];
      S.messages=makePair(-2).concat(S.messages);
      segment.setAttribute('data-msg-idx',String(oldIdx+2));button.setAttribute('data-anchor-owner-idx',String(oldIdx+2));
      button.click();
      return {expected,ids:[...segment.closest('.assistant-turn').querySelectorAll('[data-anchor-row-id]')].map(n=>n.getAttribute('data-anchor-row-id'))};
    }""")
    assert result['ids'] and all(x.startswith(result['expected']+'-step-') for x in result['ids']), result

def main():
    artifacts=Path('/tmp/hermes-scene-budget-evidence');artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='scene-budget-') as temp:
        env={k:os.environ[k] for k in ('PATH','SYSTEMROOT','TMPDIR') if k in os.environ}
        env.update(HOME=temp,HERMES_HOME=temp,HERMES_BASE_HOME=temp,HERMES_WEBUI_STATE_DIR=temp+'/webui',HERMES_CONFIG_PATH=temp+'/config.yaml',HERMES_WEBUI_HOST='127.0.0.1',HERMES_WEBUI_SKIP_ONBOARDING='1',HERMES_WEBUI_AGENT_DIR=temp+'/no-agent',HERMES_WEBUI_DEFAULT_WORKSPACE=temp)
        agent=Path(temp)/'no-agent';agent.mkdir();(agent/'run_agent.py').write_text('')
        proc,log,_,base=_start_webui_server(ROOT,env,artifacts)
        try:
            with sync_playwright() as pw:
                for engine in os.environ.get('BROWSERS','chromium,webkit').split(','):
                    if os.environ.get('SCENE_ENGINE') and os.environ['SCENE_ENGINE'] != engine: continue
                    browser=getattr(pw,engine).launch(headless=True)
                    for width,height in [(1440,1000),(820,900),(390,844)]:
                        page=browser.new_page(viewport={'width':width,'height':height},bypass_csp=True)
                        page.goto(base,wait_until='load');page.wait_for_function("() => typeof S!=='undefined' && S._bootReady===true")
                        check(page)
                        print(engine,width,'PASS: 140 source rows, bounded remount/prepend, replacement/reindexed click',flush=True)
                        page.close()
                    browser.close()
        finally:
            _terminate_process(proc);log.close()

if __name__=='__main__':main()
