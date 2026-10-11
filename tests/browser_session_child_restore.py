"""Real server/page/loadSession gate for taller-than-viewport delegated trees.

Conversations are API-imported. Only sidebar metadata is synthetically overlaid
on the sessions payload; navigation, grouping, virtualization and CSS are real.
No Agent, provider calls, credentials or live state. --repo selects a control.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=ROOT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    state = args.output / 'state'
    state.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    env = {k: os.environ[k] for k in ['PATH', 'TMPDIR', 'LANG', 'PLAYWRIGHT_BROWSERS_PATH'] if k in os.environ}
    env.update(HOME=str(state), HERMES_HOME=str(state), HERMES_BASE_HOME=str(state),
               HERMES_CONFIG_PATH=str(state / 'config.yaml'), HERMES_WEBUI_STATE_DIR=str(state),
               HERMES_WEBUI_HOST='127.0.0.1', HERMES_WEBUI_PORT=str(port),
               HERMES_WEBUI_AGENT_DIR=str(state / 'no-agent'), HERMES_WEBUI_SKIP_ONBOARDING='1',
               HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONPATH='', SSH_ASKPASS='', GIT_ASKPASS='', GIT_TERMINAL_PROMPT='0')
    server_python = os.environ.get('HERMES_WEBUI_SERVER_PYTHON', str(ROOT / '.venv/bin/python'))
    results, errors = [], []
    with (args.output / 'server.log').open('w') as log:
        proc = subprocess.Popen([server_python, 'server.py'], cwd=args.repo, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 40
            while True:
                try:
                    urllib.request.urlopen(base + '/health', timeout=1).close()
                    break
                except OSError:
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('isolated server did not become healthy; see server.log') from None
                    time.sleep(.1)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
                page = ctx.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                # Block any external transport, including telemetry/catalogs.
                ctx.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
                page.goto(base)
                page.wait_for_selector('#msg')
                page.wait_for_timeout(1000)
                seeds = page.evaluate('''async () => {
                  const sessions=[];
                  for(let i=0;i<200;i++){
                    const r=await fetch('/api/session/import',{method:'POST',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify({title:'Restore conversation '+i,messages:[{role:'user',content:'hello'},{role:'assistant',content:'hello back'}]})});
                    if(!r.ok) throw new Error('import failed');
                    sessions.push((await r.json()).session);
                  }
                  return sessions;
                }''')
                payload = {'sessions': []}

                def overlay(route):
                    route.fulfill(json=payload)

                ctx.route('**/api/sessions?*', overlay)
                ctx.route('**/api/sessions', overlay)
                scenes = [(80, 0, 'compact', 0), (80, 0, 'compact', 2000),
                          (80, 79, 'compact', 0), (80, 79, 'compact', 2000),
                          (20, 10, 'detailed', 0), (30, 29, 'compact', 2000),
                          (12, 11, 'detailed', 100000), (40, 39, 'compact', 2000)]
                for mobile in [False, True]:
                    page.set_viewport_size({'width': 390 if mobile else 1440, 'height': 900})
                    if mobile:
                        page.evaluate('_openMobileSidebarFromGesture()')
                        assert page.locator('.sidebar').is_visible()
                    for count, index, density, start in scenes:
                        now = time.time() - 86400
                        parents = [dict(s, title=f'Conversation {i:03}', updated_at=now-i*60,
                                        last_message_at=now-i*60, created_at=now-i*60,
                                        session_source='webui', profile='default') for i, s in enumerate(seeds[:120])]
                        parent = parents[100]
                        children = [dict(s, title=f'Delegated child {i:02}', updated_at=parent['updated_at'],
                                         last_message_at=parent['updated_at'], created_at=parent['updated_at'],
                                         profile='default', parent_session_id=parent['session_id'],
                                         relationship_type='child_session', raw_source='subagent', session_source='other')
                                    for i, s in enumerate(seeds[120:120+count])]
                        payload.update(sessions=parents+children, webui_session_count=120+count)
                        target = children[index]['session_id']
                        page.evaluate('''async ({first,parent,density}) => {
                          window._sidebarDensity=density;
                          $('sessionSearch').value='';_expandedChildSessionKeys.clear();
                          await loadSession(first,{skipLineageResolve:true});
                          _expandedChildSessionKeys.add(parent);
                          await renderSessionList();
                        }''', dict(first=parents[0]['session_id'], parent=parent['session_id'], density=density))
                        page.wait_for_timeout(150)
                        page.evaluate('(n)=>{$("sessionList").scrollTop=n}', start)
                        page.wait_for_timeout(100)
                        page.evaluate('async sid=>await loadSession(sid,{skipLineageResolve:true})', target)
                        page.wait_for_timeout(350)
                        if mobile:
                            page.evaluate('_openMobileSidebarFromGesture()')
                            page.wait_for_timeout(300)
                            assert page.evaluate("document.querySelector('.sidebar').classList.contains('mobile-open')")
                        parent_position = page.evaluate('''parent=>{
                          const layout=$('sessionList')._sessionVirtualLayout;
                          return layout?layout.rows.findIndex(row=>row.id===parent):
                            _allSessions.filter(s=>!s.parent_session_id).sort(_sessionSidebarSortCompare).findIndex(s=>s.session_id===parent);
                        }''', parent['session_id'])
                        assert parent_position == 100, f'parent position {parent_position} does not reproduce reviewed scene'
                        data = page.evaluate('''sid=>{
                          const l=$('sessionList'),title=_allSessions.find(s=>s.session_id===sid)?.title;
                          const r=l.querySelector('.session-child-session[data-sid="'+sid+'"]')
                            ||Array.from(l.querySelectorAll('.session-child-session')).find(e=>e.textContent.includes(title));
                          const b=l.getBoundingClientRect(),rect=r?.getBoundingClientRect();
                          return {selected:S.session?.session_id,target:sid,top:rect?rect.top-b.top:null,
                            bottom:rect?rect.bottom-b.top:null,height:l.clientHeight,scrollTop:l.scrollTop,
                            rendered:l.querySelectorAll('.session-date-body>.session-item').length,
                            virtual:l.dataset.sessionVirtualEnabled,drawer:document.querySelector('.sidebar').getBoundingClientRect().left};
                        }''', target)
                        failures = []
                        if data['selected'] != target:
                            failures.append('loadSession did not activate child')
                        if data['top'] is None or data['top'] < -.5 or data['bottom'] > data['height']+.5:
                            failures.append('restored child not fully inside viewport')
                        if data['rendered'] >= 80:
                            failures.append('virtual DOM unbounded')
                        name = f'{mobile}-{count}-{index}-{density}-{start}'
                        page.screenshot(path=str(args.output / (name+'.png')))
                        results.append(dict(scene=name, parent_position=parent_position, data=data, failures=failures))
                browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors,
                  sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.repo, text=True).strip(), results=results)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(cases=report['cases'], failures=report['failures'], errors=errors)))
    return int(bool(report['failures'] or errors))


if __name__ == '__main__':
    raise SystemExit(main())
