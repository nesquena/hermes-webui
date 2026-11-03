"""Isolated Chromium reproductions for review 5522230057; no live jobs."""
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
BASELINE = '448539d4793e5267fec06079ade04d0335cfa4c7'
STATE = Path(tempfile.mkdtemp(prefix='cron-review-'))
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


def controls(page):
    return page.evaluate("""()=>({state:_currentCronDetail&&_currentCronDetail.state,
      pause:$('btnPauseTaskDetail').style.display,resume:$('btnResumeTaskDetail').style.display})""")


def wait_held(page, held, count):
    for _ in range(100):
        if len(held) >= count:
            return
        page.wait_for_timeout(50)
    raise AssertionError(f'Expected {count} held responses; got {len(held)}')


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
        for version in ['review-before', 'review-fixed']:
            for width in [1440, 390]:
                for theme in ['dark', 'light']:
                    context = browser.new_context(viewport={'width': width, 'height': 1000},
                                                  locale='en-GB', timezone_id='Europe/London')
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                    if version == 'review-before':
                        source = subprocess.check_output(['git', 'show', BASELINE + ':static/panels.js'], cwd=ROOT)
                        context.route('**/panels.js*', lambda route, request, source=source: route.fulfill(body=source, content_type='application/javascript'))
                    jobs = [dict(id='reading', name='Morning summary', prompt='Sample task', state='active', enabled=True, profile='default'),
                            dict(id='other', name='Sample backup check', prompt='Sample task', state='active', enabled=True, profile='default')]
                    held = []
                    control = {'hold': False, 'fail': False, 'completed': False, 'lists': 0}

                    def route_cron(route, request, jobs=jobs, held=held, control=control):
                        path = request.url.split('/api/crons', 1)[1].split('?')[0]
                        if request.method != 'GET':
                            assert path in ['/pause', '/resume'], path
                            jobs[0]['state'] = 'paused' if path == '/pause' else 'active'
                            control['hold'] = True
                            route.fulfill(json={'ok': True})
                        elif path == '/recent':
                            route.fulfill(json={'completions': [dict(job_id='other', name='Sample backup check', completed_at=1791960000,
                                                                   status='success', toast_notifications=False)] if control['completed'] else []})
                            control['completed'] = False
                        elif path == '':
                            control['lists'] += 1
                            if control['hold']:
                                control['hold'] = False
                                held.append(route)
                            elif control['fail']:
                                control['fail'] = False
                                route.fulfill(status=503, json={'error': 'Fixture temporary failure'})
                            else:
                                route.fulfill(json={'jobs': jobs, 'other_profile_count': 0})
                        elif path == '/history':
                            route.fulfill(json={'runs': [], 'total': 0})
                        else:
                            route.fulfill(json={'content': 'Fixture output'})

                    context.route('**/api/crons**', route_cron)
                    page.goto(base, wait_until='domcontentloaded')
                    page.wait_for_selector('#msg', timeout=20000)
                    page.wait_for_timeout(1600)
                    page.evaluate("async theme=>{_applyTheme(theme);_applySkin('default');await switchPanel('tasks');clearInterval(_cronPollTimer);_cronPollTimer=null;openCronDetail('reading');}", theme)
                    page.wait_for_timeout(300)
                    assert page.evaluate("document.documentElement.classList.contains('dark')") == (theme == 'dark')
                    failures = []
                    for action, state in [('cronPause', 'paused'), ('cronResume', 'active')]:
                        page.evaluate("action=>{window.fixtureAction=window[action]('reading');}", action)
                        wait_held(page, held, 1)
                        control['fail'] = True
                        control['completed'] = True
                        page.evaluate('async()=>{await _runCronPollTick();}')
                        page.wait_for_timeout(300)
                        held.pop().fulfill(json={'jobs': jobs, 'other_profile_count': 0})
                        page.evaluate('async()=>{await window.fixtureAction;}')
                        actual = controls(page)
                        failures.append(actual)
                        if version == 'review-fixed':
                            assert actual == dict(state=state, pause='none' if state == 'paused' else '', resume='' if state == 'paused' else 'none'), actual
                        else:
                            assert actual['state'] != state, actual
                        # Restore the baseline before exercising the opposite action.
                        if state == 'paused':
                            page.screenshot(path=str(OUT / f'{version}-503-{width}-{theme}.png'))
                        page.evaluate('async()=>{await loadCrons();}')
                    away = []
                    for action, state in [('cronPause', 'paused'), ('cronResume', 'active')]:
                        page.evaluate("action=>{window.fixtureAction=window[action]('reading');}", action)
                        wait_held(page, held, 1)
                        page.evaluate("()=>{openCronDetail('other');openCronDetail('reading');}")
                        control['completed'] = True
                        page.evaluate('async()=>{await _runCronPollTick();}')
                        page.wait_for_timeout(300)
                        actual = controls(page)
                        away.append(actual)
                        assert (actual['state'] == state) == (version == 'review-fixed'), actual
                        held.pop().fulfill(json={'jobs': jobs, 'other_profile_count': 0})
                        page.evaluate('async()=>{await window.fixtureAction;await loadCrons();}')
                    removed = []
                    for empty in [False, True]:
                        page.evaluate("async()=>{await loadCrons();openCronDetail('reading');}")
                        snapshot = jobs[:]
                        jobs[:] = [] if empty else [jobs[1]]
                        page.evaluate('async()=>{await loadCrons(false,{background:true});}')
                        actual = page.evaluate("()=>({cleared:_currentCronDetail===null,run:$('btnRunTaskDetail').style.display,edit:$('btnEditTaskDetail').style.display})")
                        removed.append(actual)
                        assert actual['cleared'] == (version == 'review-fixed'), actual
                        if version == 'review-fixed':
                            assert actual['run'] == actual['edit'] == 'none', actual
                        jobs[:] = snapshot
                    # Measure actual browser scroll, including scroll while a GET is held.
                    extras = [dict(jobs[1], id=f'fixture-{i}', name=f'Sample job {i}') for i in range(60)]
                    jobs.extend(extras)
                    page.evaluate('async()=>{await loadCrons();}')
                    if width == 390:
                        page.locator('#btnHamburger').click()
                        page.wait_for_timeout(300)
                    control['hold'] = True
                    page.evaluate("()=>{window.fixtureScroll=loadCrons(false,{background:true});}")
                    wait_held(page, held, 1)
                    previous = page.evaluate("()=>{$('cronList').scrollTop=250;return $('cronList').scrollTop;}")
                    assert previous > 0, previous
                    held.pop().fulfill(json={'jobs': jobs, 'other_profile_count': 0})
                    page.evaluate('async()=>{await window.fixtureScroll;}')
                    after = page.evaluate("$('cronList').scrollTop")
                    if version == 'review-fixed':
                        assert after == previous, (previous, after)
                    assert not errors, errors
                    results.append(dict(version=version, width=width, theme=theme, failed_poll_actions=failures,
                                        away_and_back=away, removed=removed, scroll_before=previous, scroll_after=after,
                                        page_errors=errors))
                    context.close()
        browser.close()
    (OUT / 'review_fixes_results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log.close()
