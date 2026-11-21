"""Real-page scrollbar, chip keyboard and readable count regression.

Imports local conversations; overlays only sidebar metadata. No Agent/SSE claim.
Use --repo for an exact-head control. Chromium has real non-overlay scrollbars.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--scroll-only', action='store_true', help='Compare native wheel work with master, which has no keyboard chip')
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
    results, errors = [], []
    with (args.output / 'server.log').open('w') as log:
        proc = subprocess.Popen([os.environ.get('HERMES_WEBUI_SERVER_PYTHON', sys.executable), 'server.py'],
                                cwd=args.repo, env=env, stdout=log, stderr=subprocess.STDOUT)
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
                browser = pw.chromium.launch(ignore_default_args=['--hide-scrollbars'])
                ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, has_touch=True)
                ctx.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
                page = ctx.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_selector('#msg')
                page.wait_for_timeout(1000)
                seeds = page.evaluate('''async () => {
                  const rows=[];
                  for(let i=0;i<135;i++){
                    const r=await fetch('/api/session/import',{method:'POST',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify({title:'Conversation '+i,messages:[{role:'user',content:'hello'},{role:'assistant',content:'hello back'}]})});
                    if(!r.ok) throw new Error('import failed');rows.push((await r.json()).session);
                  }
                  return rows;
                }''')
                now = time.time() - 86400
                parents = [dict(s, title=f'Primary conversation {i:03}', updated_at=now-i*60,
                                last_message_at=now-i*60, created_at=now-i*60,
                                session_source='webui', profile='default') for i, s in enumerate(seeds[:120])]
                children = [dict(s, title=f'Delegated task {i}', updated_at=now, last_message_at=now,
                                 created_at=now, parent_session_id=parents[i//3]['session_id'],
                                 relationship_type='child_session', raw_source='subagent', session_source='other',
                                 profile='default') for i, s in enumerate(seeds[120:135])]
                # Concurrent child work belongs inside the aggregate chip.
                for i, child in enumerate(children):
                    if i % 3 == 0 and i < 12:
                        child['attention'] = {'kind': 'approval', 'count': 1}
                    if i % 3 == 1:
                        child['is_streaming'] = True
                payload = dict(sessions=parents+children, webui_session_count=135)
                ctx.route('**/api/sessions?*', lambda route: route.fulfill(json=payload))
                ctx.route('**/api/sessions', lambda route: route.fulfill(json=payload))
                page.evaluate('''async first=>{
                  await loadSession(first,{skipLineageResolve:true});
                  window._sidebarDensity='compact';await renderSessionList();
                  window.repairRenders=0;const render=renderSessionListFromCache;
                  renderSessionListFromCache=function(...a){repairRenders++;return render(...a);};
                }''', parents[0]['session_id'])
                page.wait_for_timeout(250)
                # Force appearance from an empty list, then real native wheel input.
                page.evaluate('''()=>{
                  $('sessionList').innerHTML='';renderSessionListFromCache();
                  $('sessionList').scrollTop=400;
                }''')
                page.wait_for_timeout(250)
                initial = page.evaluate('''()=>{const l=$('sessionList');return {
                  width:l.clientWidth,gutter:l.offsetWidth-l.clientWidth,
                  key:l._sessionVirtualLayout?.key,dom:l.querySelectorAll('.session-date-body>.session-item').length,
                  height:l.clientHeight,scrollHeight:l.scrollHeight};}''')
                page.evaluate('repairRenders=0')
                page.wait_for_timeout(250)
                idle = page.evaluate('repairRenders')
                page.locator('#sessionList').hover()
                page.evaluate('repairRenders=0')
                for _ in range(10):
                    page.mouse.wheel(0, 1)
                    page.wait_for_timeout(45)
                wheel = page.evaluate('repairRenders')
                page.wait_for_timeout(250)
                tail = page.evaluate('repairRenders')
                failures = []
                if initial['gutter'] < 8:
                    failures.append('real scrollbar gutter absent')
                if idle or tail != wheel:
                    failures.append('idle render work')
                if wheel > 1:
                    failures.append('tiny native wheels repeatedly full-render')
                if initial['dom'] >= 80:
                    failures.append('virtual DOM unbounded')
                results.append(dict(scene='real-scrollbar', initial=initial, idle=idle, wheel=wheel, tail=tail, failures=failures))
                if args.scroll_only:
                    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors,
                                  sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.repo,text=True).strip(), results=results)
                    (args.output/'report.json').write_text(json.dumps(report,indent=2))
                    print(json.dumps(report))
                    browser.close()
                    return int(bool(report['failures'] or errors))
                # Select a visible first owner, then consecutive keys WITHOUT refocus.
                page.evaluate('''()=>{$('sessionList').scrollTop=0;renderSessionListFromCache();}''')
                page.wait_for_timeout(150)
                chip = page.locator('.session-child-count[role=button]').first
                chip.focus()
                keys = []
                for key in ['Enter', 'Space', 'Enter', 'Space']:
                    page.keyboard.press(key)
                    page.wait_for_timeout(80)
                    keys.append(page.evaluate('''()=>({tag:document.activeElement.tagName,
                      chip:document.activeElement.classList.contains('session-child-count'),
                      expanded:document.activeElement.getAttribute('aria-expanded'),
                      owner:document.activeElement.closest('.session-item')?.dataset.sid})'''))
                page.screenshot(path=str(args.output/'keyboard.png'))
                page.locator('#sessionSearch').focus()
                page.evaluate('renderSessionListFromCache()')
                no_steal = page.evaluate("document.activeElement.id==='sessionSearch'")
                results.append(dict(scene='consecutive-keyboard', keys=keys, no_steal=no_steal,
                                    failures=[] if all(k['chip'] and k['owner']==parents[0]['session_id'] and
                                                      k['expanded']==('true' if i%2==0 else 'false')
                                                      for i,k in enumerate(keys)) and no_steal else ['chip focus/expansion continuity or focus stealing']))
                # Small realistic inventory; all dictionaries checked in the same real page.
                locales = page.evaluate('Object.keys(LOCALES)')
                scenes = [(390,844,180,'graphite',False), (390,844,320,'codex',True),
                          (820,1180,320,'terracotta',False), (820,1180,180,'github',True),
                          (1440,900,320,'geist-contrast',False), (1440,900,180,'graphite',True)]
                for width,height,sidebar,skin,dark in scenes:
                    page.set_viewport_size({'width':width,'height':height})
                    page.evaluate('''({sidebar,skin,dark})=>{
                      document.documentElement.dataset.skin=skin;document.documentElement.classList.toggle('dark',dark);
                      document.documentElement.style.setProperty('--sidebar-width',sidebar+'px');
                      document.querySelector('.sidebar').style.width=sidebar+'px';
                      $('sessionSearch').value='';_expandedChildSessionKeys.clear();
                      if(_isDesktopWidth()) expandSidebar();else _openMobileSidebarFromGesture();
                    }''', dict(sidebar=sidebar,skin=skin,dark=dark))
                    page.wait_for_timeout(300)
                    page.screenshot(path=str(args.output/f'visibility-{width}-{sidebar}.png'))
                    assert page.locator('.sidebar').is_visible(), page.evaluate("({width:innerWidth,desktop:_isDesktopWidth(),cls:document.querySelector('.sidebar').className,root:document.documentElement.outerHTML.slice(0,600),layout:document.querySelector('.layout').className,css:getComputedStyle(document.querySelector('.sidebar')).display})")
                    for locale in locales:
                        page.evaluate('''locale=>{setLocale(locale);$('sessionList').scrollTop=0;renderSessionListFromCache();}''', locale)
                        page.wait_for_timeout(20)
                        data = page.evaluate('''()=>Array.from(document.querySelectorAll('.session-child-count[role=button]')).slice(0,5).map(c=>{
                          const l=c.querySelector('.session-child-count-label'),r=document.createRange();r.selectNodeContents(l);
                          const b=l.getBoundingClientRect(),glyph=r.getBoundingClientRect(),owner=c.closest('.session-item');
                          const title=owner.querySelector('.session-title').getBoundingClientRect();
                          return {owner:owner.dataset.sid,text:l.textContent,available:b.width,glyph:glyph.width,titleWidth:title.width,
                            aria:c.getAttribute('aria-label'),tooltip:c.title,expected:t('session_meta_children',3),
                            sidebar:document.querySelector('.sidebar').getBoundingClientRect().width,
                            marks:[...c.querySelectorAll('.session-child-count-state,.session-child-activity-indicator')].map(m=>{
                              const mr=m.getBoundingClientRect(),cr=c.getBoundingClientRect(),clip=c.closest('.session-text').getBoundingClientRect();
                              return {width:mr.width,inside:mr.left>=cr.left&&mr.right<=cr.right&&mr.left>=clip.left&&mr.right<=clip.right};
                            }),guards:{rename:_renamingSid,menu:!!_sessionActionMenu,skeleton:_sessionListSkeletonActive}};
                        })''')
                        issues = []
                        if not data:
                            issues.append('navigable counts absent')
                        for d in data:
                            if d['text'] != '3' or d['glyph'] > d['available']+.5:
                                issues.append('count is clipped wording rather than readable numeral')
                            if d['expected'] not in d['aria'] or d['aria'] != d['tooltip']:
                                issues.append('full localized accessible meaning lost')
                            if d['titleWidth'] < 24:
                                issues.append('primary title budget lost')
                            expected_marks = 1 if d['owner'] == parents[4]['session_id'] else 2
                            if len(d['marks']) != expected_marks or not all(m['width'] >= 10 and m['inside'] for m in d['marks']):
                                issues.append('concurrent child marks missing or clipped')
                            if abs(d['sidebar']-sidebar)>1:
                                issues.append('wrong real sidebar width')
                        results.append(dict(scene='readable-count', width=width, sidebar=sidebar, skin=skin,
                                            dark=dark, locale=locale, data=data, failures=issues))
                    page.screenshot(path=str(args.output/f'{width}-{sidebar}-{skin}-{dark}.png'))
                    if width==390:
                        page.evaluate("setLocale('en');renderSessionListFromCache()")
                        # The smallest running-only count is the risky touch target:
                        # synthetic hover must not remove the adjacent timestamp's width.
                        chip = page.locator(f'.session-item[data-sid="{parents[4]["session_id"]}"] .session-child-count')
                        chip.tap()
                        page.wait_for_timeout(350)
                        expanded = chip.get_attribute('aria-expanded')
                        chip.tap()
                        page.wait_for_timeout(350)
                        collapsed = chip.get_attribute('aria-expanded')
                        results.append(dict(scene='touch', width=sidebar, expanded=expanded, collapsed=collapsed,
                                            failures=[] if expanded=='true' and collapsed=='false' else ['touch disclosure failed']))
                browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors,
                  sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.repo,text=True).strip(), results=results)
    (args.output/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(dict(cases=report['cases'],failures=report['failures'],errors=errors)))
    return int(bool(report['failures'] or errors))


if __name__ == '__main__':
    raise SystemExit(main())
