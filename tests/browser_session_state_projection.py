"""Real CSS/renderer cache invalidation and unrepresented-child activity gate."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402
from tests.browser_session_virtual_geometry import geometry_script  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before-ref')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def source(path):
        return subprocess.check_output(['git', 'show', f'{args.before_ref}:{path}'], cwd=ROOT, text=True) if args.before_ref else (ROOT/path).read_text()

    results, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content('<input id="sessionSearch" hidden><div id="sessionList" style="width:180px;height:320px;overflow:auto"></div>')
        page.add_style_tag(content=source('static/style.css'))
        page.add_script_tag(content=geometry_script(source('static/sessions.js')))
        page.add_script_tag(content=source('static/i18n.js'))
        for state in ['streaming', 'unread', 'approval']:
            for reverse in [False, True]:
                page.evaluate('''state=>{
                  document.documentElement.dataset.skin='graphite';
                  activeSidForSidebar='other';scene('detailed','children');setLocale('ru');
                  const s=groups[0].items[0];s._compression_segment_count=1000;
                  fixtureSessions.find(s=>s.session_id==='p0').has_unread=false;
                  s.has_unread=false;s.is_streaming=false;s.attention=null;
                  window.setOwn=state=>{
                    s.is_streaming=state==='streaming';s.attention=state==='approval'?{kind:'approval',count:1}:null;
                    fixtureSessions.find(s=>s.session_id==='p0').has_unread=state==='unread';
                  };setOwn(state);repaint();
                }''', state if reverse else 'idle')
                page.wait_for_timeout(100)
                initial = page.evaluate("$('sessionList')._sessionVirtualLayout.measured.get('p0')")
                page.evaluate("$('sessionList').scrollTop=3500")
                page.wait_for_timeout(100)
                assert page.locator('.session-item[data-sid="p0"]').count() == 0
                page.evaluate('state=>{setOwn(state);repaint()}', 'idle' if reverse else state)
                retained = page.evaluate("$('sessionList')._sessionVirtualLayout.measured.get('p0')||null")
                estimate = page.evaluate("$('sessionList')._sessionVirtualLayout.offsets[1]-$('sessionList')._sessionVirtualLayout.offsets[0]")
                page.screenshot(path=str(args.output/f'{state}-{reverse}-offscreen.png'))
                page.evaluate("$('sessionList').scrollTop=0")
                page.wait_for_timeout(100)
                actual = page.evaluate("$('sessionList')._sessionVirtualLayout.measured.get('p0')")
                assert actual['height'] != initial['height'], 'state transition must change real CSS height'
                failures = ['stale offscreen height survives own state transition'] if retained else []
                results.append(dict(kind='cache', state=state, reverse=reverse, initial=initial, retained=retained, estimate=estimate, actual=actual, failures=failures))
        page.close()
        page = browser.new_page(viewport={'width': 900, 'height': 800})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content('<main id="fixture" style="width:300px;padding:8px;background:var(--sidebar)"></main>')
        page.add_style_tag(content=source('static/style.css'))
        page.add_script_tag(content=component_script(source('static/sessions.js')))
        page.add_script_tag(content='''
          const _loadingSessionId=null;
          function repaint(){
            const parent={session_id:'parent',title:'Parent conversation',message_count:3,attention:{kind:'clarify',count:1}};
            const visible={session_id:'visible',title:'Visible approval child',message_count:3,parent_session_id:'parent',relationship_type:'child_session',raw_source:'subagent',session_source:'other',attention:{kind:'approval',count:1},is_streaming:window.visibleRunning};
            const hidden={session_id:'hidden',title:'Archived running child',message_count:3,parent_session_id:'parent',relationship_type:'child_session',raw_source:'subagent',session_source:'other',archived:true,is_streaming:!window.visibleRunning};
            const expanded=_expandedChildSessionKeys.has('parent');
            const {element}=renderFixture([parent,visible],[parent,visible,hidden],expanded,'other');
            document.querySelector('#fixture').replaceChildren(element);
          }
        ''')
        for visible_running in [False, True]:
            page.evaluate('value=>{window.visibleRunning=value;_expandedChildSessionKeys.clear();repaint()}', visible_running)
            for stage in ['collapsed', 'expanded', 'recollapsed']:
                if stage != 'collapsed':
                    page.locator('.session-child-count').click()
                page.mouse.move(899, 799)
                data = page.evaluate('''()=>{
                  const activity=document.querySelector('.session-child-activity-indicator'),mark=document.querySelector('.session-child-count-state');
                  return {activity:activity?getComputedStyle(activity,'::before').animationName:null,
                    priority:mark.className,own:document.querySelector('.session-item>.session-attention-indicator').className,
                    children:[...document.querySelectorAll('.session-child-session')].map(e=>e.dataset.sid)};
                }''')
                expected = not visible_running or stage != 'expanded'
                failures = []
                if (data['activity'] == 'spin') != expected:
                    failures.append('unrepresented running work hidden or visible-only spinner duplicated')
                if 'is-attention-approval' not in data['priority'] or 'is-attention-clarify' not in data['own']:
                    failures.append('child priority or independent parent cue lost')
                if stage == 'expanded' and data['children'] != ['visible']:
                    failures.append('archived child became navigable')
                page.screenshot(path=str(args.output/f'activity-{visible_running}-{stage}.png'))
                results.append(dict(kind='activity', visible_running=visible_running, stage=stage, data=data, failures=failures))
        browser.close()
    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors, results=results)
    (args.output/'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(cases=report['cases'], failures=report['failures'], errors=errors)))
    return int(bool(report['failures'] or errors))


if __name__ == '__main__':
    raise SystemExit(main())
