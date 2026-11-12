import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'docs/pr-media/6767'
OUT.mkdir(parents=True, exist_ok=True)
STATE = Path(tempfile.mkdtemp(prefix='cron-visual-'))
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
BASE = f'http://127.0.0.1:{port}'
# Explicit inert Agent root prevents discovery of the real installed Agent.
(STATE/'no-agent').mkdir()
(STATE/'no-agent'/'run_agent.py').write_text("raise ImportError('Browser fixture: Agent execution disabled')\n")
env = os.environ.copy()
for k in list(env):
    if k.endswith('_API_KEY'):
        env.pop(k, None)
env.update(HERMES_WEBUI_PORT=str(port), HERMES_WEBUI_HOST='127.0.0.1',
           HERMES_HOME=str(STATE), HERMES_BASE_HOME=str(STATE),
           HERMES_WEBUI_STATE_DIR=str(STATE), HERMES_WEBUI_SKIP_ONBOARDING='1',
           HERMES_WEBUI_AGENT_DIR=str(STATE/'no-agent'))
log = (STATE/'server.log').open('w')
proc = subprocess.Popen([sys.executable, 'server.py'], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
jobs = [dict(id='reading', name='Morning summary', prompt='Summarise the sample notes.',
             schedule_display='Every day at 08:00', state='active', enabled=True, profile='default',
             next_run_at='2026-10-15T08:00:00+01:00', last_run_at='2026-10-14T08:00:00+01:00', deliver='local'),
        dict(id='finished', name='Sample backup check', prompt='Check the sample backup report.',
             schedule_display='Every hour', state='active', enabled=True, profile='default', deliver='local')]
results=[]
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE+'/health', timeout=1).close()
            break
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError((STATE/'server.log').read_text()[-2500:]) from None
            time.sleep(.5)
    else:
        raise RuntimeError('Isolated server did not become healthy')
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        for version in ['before', 'review-before', 'after']:
            for width in [1440,390]:
                ctx=browser.new_context(viewport={'width':width,'height':1000},locale='en-GB',timezone_id='Europe/London')
                page=ctx.new_page()
                errors=[]
                page.on('pageerror',lambda e, errors=errors: errors.append(str(e)))
                if version!='after':
                    for filename in ['panels.js','style.css']:
                        ref = '3179f556' if version == 'before' else '9eb8f947'
                        content=subprocess.check_output(['git','show',ref+':static/'+filename],cwd=ROOT)
                        ctx.route('**/'+filename+'*', lambda r, request, content=content, filename=filename: r.fulfill(body=content,content_type='text/css' if filename.endswith('css') else 'application/javascript'))
                completed={'value':False}
                action_control={'hold_next':False,'held':[]}
                def cron_route(route, request, completed=completed, action_control=action_control):
                    path=route.request.url.split('/api/crons',1)[1].split('?')[0]
                    if route.request.method!='GET':
                        if path in ['/pause','/resume']:
                            jobs[0]['state']='paused' if path=='/pause' else 'active'
                            action_control['hold_next']=True
                            route.fulfill(json={'ok':True})
                            return
                        raise RuntimeError('Unexpected cron write')
                    if path=='' and action_control['hold_next']:
                        action_control['hold_next']=False
                        action_control['held'].append(route)
                        return
                    if path=='/recent':
                        data={'completions':[dict(job_id='finished',name='Sample backup check',completed_at=1791960000,status='success',toast_notifications=False)] if completed['value'] else []}
                        completed['value']=False
                    elif path=='/history':
                        data={'runs':[{'filename':'2026-10-14_08-00-00.md','size':128,'modified':1791960000}], 'total':1}
                    elif path in ['/run','/output','/history/content']:
                        data={'content':'# Response\n\nSample run output.\n\nThe report is complete. This open output must remain visible when another task finishes.'}
                    else:
                        data={'jobs':jobs,'other_profile_count':0}
                    route.fulfill(json=data)
                ctx.route('**/api/crons**',cron_route)
                page.goto(BASE,wait_until='domcontentloaded')
                page.wait_for_selector('#msg',timeout=20000)
                page.wait_for_timeout(1800)
                page.evaluate("async()=>{await switchPanel('tasks');_cronNewJobIds.add('reading');updateCronBadge();await loadCrons();openCronDetail(_cronList.find(j=>j.id==='reading'));}")
                page.wait_for_timeout(800)

                page.wait_for_selector('#cron-det-run-reading-0',state='attached',timeout=5000)
                page.locator('#cron-det-run-reading-0 .detail-run-head').click()
                page.wait_for_timeout(500)

                # Mark the selected task unread without rebuilding its already-open detail.
                page.evaluate("()=>{_cronNewJobIds.add('reading');updateCronBadge();$('cronDetailRuns').classList.add('has-new-run');}")
                completed['value']=True
                page.evaluate('''async()=>{
                  if(typeof _runCronPollTick==='function')await _runCronPollTick();
                  else {
                    clearInterval(_cronPollTimer);_cronPollTimer=null;
                    const original=setInterval;let tick;
                    window.setInterval=(fn)=>{tick=fn;return 1;};
                    try{startCronPolling();}finally{window.setInterval=original;}
                    await tick();
                  }
                }''')
                page.wait_for_timeout(500)
                metrics=page.evaluate("""()=>({open:document.querySelector('#cron-det-run-reading-0').classList.contains('open'),output:document.querySelector('#cron-det-run-reading-0 .detail-run-body').textContent,detailColor:getComputedStyle($('cronDetailRuns')).borderColor,unread:[..._cronNewJobIds],dots:[...document.querySelectorAll('.cron-new-dot')].map(e=>getComputedStyle(e).backgroundColor),overflow:document.documentElement.scrollWidth>innerWidth})""")
                if version=='after':
                    assert metrics['open'],metrics
                    assert 'Sample run output' in metrics['output'],metrics
                    assert metrics['dots'] and all(c=='rgb(229, 62, 62)' for c in metrics['dots']),metrics
                if version=='review-before':
                    assert not metrics['open'] and 'Sample run output' not in metrics['output'],metrics
                assert not errors,errors
                # Show the sidebar on phone for the list image; capture detail separately.
                if width==390:
                    page.locator('#btnHamburger').click()
                    page.wait_for_timeout(400)
                page.screenshot(path=str(OUT/f'{version}-{width}-list.png'))
                if width==390:
                    page.evaluate("()=>{if(typeof closeMobileSidebar==='function')closeMobileSidebar();}")
                    page.wait_for_timeout(400)
                page.locator('#cronDetailRuns').scroll_into_view_if_needed()
                page.screenshot(path=str(OUT/f'{version}-{width}-output.png'))
                action_results=[]
                if version=='after':
                    for action,state in [('cronPause','paused'),('cronResume','active')]:
                        page.evaluate("action=>{window.fixtureActionPromise=window[action]('reading');}",action)
                        for _ in range(50):
                            if action_control['held']:break
                            page.wait_for_timeout(100)
                        assert action_control['held'],'Action list refresh was not intercepted'
                        completed['value']=True
                        page.evaluate('async()=>{await _runCronPollTick();}')
                        for _ in range(50):
                            actual=page.evaluate('_currentCronDetail.state')
                            if actual==state:break
                            page.wait_for_timeout(100)
                        assert actual==state,(action,actual)
                        action_control['held'].pop().fulfill(json={'jobs':jobs,'other_profile_count':0})
                        page.evaluate('async()=>{await window.fixtureActionPromise;}')
                        controls=page.evaluate("()=>({state:_currentCronDetail.state,pause:$('btnPauseTaskDetail').style.display,resume:$('btnResumeTaskDetail').style.display})")
                        assert controls['state']==state,controls
                        assert controls['pause']==('none' if state=='paused' else ''),controls
                        assert controls['resume']==('' if state=='paused' else 'none'),controls
                        page.screenshot(path=str(OUT/f'action-{state}-{width}.png'))
                        action_results.append(controls)
                prompt_results=[]
                if version=='after':
                    for older_first in [False,True]:
                        for action,state in [('cronPause','paused'),('cronResume','active')]:
                            page.evaluate("action=>{window.fixtureActionPromise=window[action]('reading');}",action)
                            for _ in range(50):
                                if action_control['held']:break
                                page.wait_for_timeout(100)
                            assert len(action_control['held'])==1
                            page.locator('[onclick^="toggleCronPromptExpanded"]').click()
                            action_control['hold_next']=True
                            page.evaluate('()=>{window.fixturePollPromise=loadCrons(false,{background:true});}')
                            for _ in range(50):
                                if len(action_control['held'])==2:break
                                page.wait_for_timeout(100)
                            assert len(action_control['held'])==2
                            older,latest=action_control['held']
                            action_control['held'].clear()
                            ordered=[(older,'fixtureActionPromise'),(latest,'fixturePollPromise')]
                            if not older_first:ordered.reverse()
                            for route,promise in ordered:
                                route.fulfill(json={'jobs':jobs,'other_profile_count':0})
                                page.evaluate('async name=>{await window[name];}',promise)
                            controls=page.evaluate("()=>({state:_currentCronDetail.state,pause:$('btnPauseTaskDetail').style.display,resume:$('btnResumeTaskDetail').style.display})")
                            assert controls['state']==state,controls
                            assert controls['pause']==('none' if state=='paused' else ''),controls
                            assert controls['resume']==('' if state=='paused' else 'none'),controls
                            order='older-first' if older_first else 'latest-first'
                            page.screenshot(path=str(OUT/f'prompt-{state}-{order}-{width}.png'))
                            prompt_results.append(dict(controls,order=order))
                assert not errors,errors
                results.append(dict(version=version,width=width,metrics=metrics,page_errors=errors,action_races=action_results,prompt_races=prompt_results))
                ctx.close()
        browser.close()
    (OUT/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
finally:
    proc.terminate()
    try:proc.wait(timeout=5)
    except subprocess.TimeoutExpired:proc.kill();proc.wait()
    log.close()
