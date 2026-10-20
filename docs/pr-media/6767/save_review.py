"""Isolated Chromium reproduction for review 5522946238; no live tasks."""
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
OUT = Path(__file__).resolve().parent
BASELINE = '5dcb2fa54b04582f0ae012a983328d011c646701'
STATE = Path(tempfile.mkdtemp(prefix='cron-save-review-'))
(STATE / 'no-agent').mkdir()
(STATE / 'no-agent' / 'run_agent.py').write_text("raise ImportError('Isolated fixture')\n")
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')}
env.update(HERMES_WEBUI_PORT=str(port), HERMES_WEBUI_HOST='127.0.0.1',
           HERMES_HOME=str(STATE), HERMES_BASE_HOME=str(STATE),
           HERMES_WEBUI_STATE_DIR=str(STATE), HERMES_WEBUI_SKIP_ONBOARDING='1',
           HERMES_WEBUI_AGENT_DIR=str(STATE / 'no-agent'))
base = f'http://127.0.0.1:{port}'
log = (STATE / 'server.log').open('w')
proc = subprocess.Popen([sys.executable, 'server.py'], cwd=ROOT, env=env,
                        stdout=log, stderr=subprocess.STDOUT)
results = []
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(base + '/health', timeout=1).close()
            break
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError((STATE / 'server.log').read_text()[-2000:]) from None
            time.sleep(.5)
    else:
        raise RuntimeError('Isolated server did not become healthy')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        for version in ['save-before', 'save-fixed']:
            for width in [1440, 390]:
                for editing in [False, True]:
                    context = browser.new_context(viewport={'width': width, 'height': 1000})
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                    if version == 'save-before':
                        source = subprocess.check_output(['git', 'show', BASELINE + ':static/panels.js'], cwd=ROOT)
                        context.route('**/panels.js*', lambda route, request, source=source: route.fulfill(body=source, content_type='application/javascript'))
                    saved = dict(id='saved', name='Task deleted after save', prompt='Fixture task',
                                 schedule='0 8 * * *', state='active', enabled=True, profile='default')
                    other = dict(saved, id='other', name='Other task')
                    jobs = [saved, other] if editing else [other]
                    held = []
                    control = {'hold': False}

                    def route_cron(route, request, jobs=jobs, held=held, control=control, saved=saved):
                        path = request.url.split('/api/crons', 1)[1].split('?')[0]
                        if request.method != 'GET':
                            if path in ['/update', '/create']:
                                if path == '/create':
                                    jobs.insert(0, saved)
                                control['hold'] = True
                                route.fulfill(json={'id': 'saved', 'ok': True})
                            elif path == '/delete':
                                jobs[:] = [job for job in jobs if job['id'] != 'saved']
                                route.fulfill(json={'ok': True})
                            else:
                                raise AssertionError(path)
                        elif path == '':
                            if control['hold']:
                                control['hold'] = False
                                held.append((route, json.loads(json.dumps(jobs))))
                            else:
                                route.fulfill(json={'jobs': jobs, 'other_profile_count': 0})
                        elif path == '/recent':
                            route.fulfill(json={'completions': []})
                        elif path == '/history':
                            route.fulfill(json={'runs': [], 'total': 0})
                        else:
                            route.fulfill(json={'content': 'Fixture output'})

                    context.route('**/api/crons**', route_cron)
                    page.goto(base, wait_until='domcontentloaded')
                    page.wait_for_selector('#msg', timeout=20000)
                    page.wait_for_timeout(1600)
                    page.evaluate("async()=>{_applyTheme('dark');_applySkin('default');await switchPanel('tasks');clearInterval(_cronPollTimer);_cronPollTimer=null;}")
                    page.evaluate("editing=>{if(editing)openCronEdit(_cronList.find(j=>j.id==='saved'));else openCronCreate();}", editing)
                    page.fill('#cronFormPrompt', 'Fixture save')
                    page.evaluate("$('cronFormSchedule').value='0 8 * * *'")
                    page.evaluate("()=>{window.fixtureSave=saveCronForm();}")
                    for _ in range(100):
                        if held:
                            break
                        page.wait_for_timeout(50)
                    assert len(held) == 1, 'Save did not start its deferred read'
                    # For create, another accepted list makes the new task selectable.
                    if not editing:
                        page.evaluate('async()=>{await loadCrons();}')
                    page.evaluate("async()=>{openCronDetail('saved');showConfirmDialog=async()=>true;await deleteCurrentCron();}")
                    assert page.evaluate('_currentCronDetail') is None
                    route, stale_jobs = held.pop()
                    route.fulfill(json={'jobs': stale_jobs, 'other_profile_count': 0})
                    page.evaluate('async()=>{await window.fixtureSave;}')
                    visible = page.evaluate("()=>({id:_currentCronDetail&&_currentCronDetail.id,mode:_cronMode,run:$('btnRunTaskDetail').style.display,edit:$('btnEditTaskDetail').style.display,ids:_cronList.map(j=>j.id)})")
                    ghost = visible['id'] == 'saved' and visible['run'] != 'none' and visible['edit'] != 'none'
                    assert ghost == (version == 'save-before'), visible
                    assert visible['ids'] == ['other'], visible
                    assert not errors, errors
                    image = f'{version}-{width}-{"edit" if editing else "create"}.png'
                    page.screenshot(path=str(OUT / image), full_page=True)
                    results.append(dict(version=version, width=width, editing=editing,
                                        visible=visible, deleted_task_reopened=ghost, page_errors=errors, screenshot=image))
                    context.close()
        browser.close()
    (OUT / 'save_review_results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))
finally:
    proc.terminate()
    proc.wait(timeout=10)
    log.close()
