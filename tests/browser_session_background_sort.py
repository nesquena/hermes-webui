"""Production sort/date-group/window/row pipeline: no server or Agent state.

The isolated page seeds session snapshots and records only outbound navigation.
--before-ref retains current fixture composition while replaying exact source.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.browser_session_virtual_geometry import geometry_script  # noqa: E402
from tests._sidebar_child_status_helpers import ROOT  # noqa: E402


def script(source):
    component = geometry_script(source)
    component = component.replace('fixtureSessions.map(s=>[s.session_id,{message_count:s.message_count,transcript_generation:0}])',
                                  'fixtureSessions.flatMap(s=>[s,...(s._child_sessions||[])]).map(s=>[s.session_id,{message_count:s.message_count,transcript_generation:0}])')
    # Execute production sorting and date grouping, not fixture-sorted groups.
    sort_start = source.index('  const orderedSessions=[...sessions].sort(')
    sort_end = source.index('  // Collapse state:', sort_start)
    group_start = source.index('  // Group sessions by date', sort_end)
    group_end = source.index('  const flatSessionRows=[];', group_start)
    component = component.replace(' const q=searchQueryRaw;',
                                  ' const q=searchQueryRaw;\nconst sessions=fixtureSessions;\n'
                                  + source[sort_start:sort_end] + source[group_start:group_end])
    names = ['_sessionSortTimestampMs', '_sessionRunningSortRank',
             '_sessionSidebarSortCompare', '_sessionCalendarBoundaries', '_sessionTimeBucketLabel']
    functions = '\n'.join(re.search(r'^function ' + n + r'\(.*?^\}', source, re.M | re.S).group()
                          for n in names)
    return component + functions + r"""
const fixtureNow=new Date(2026,9,9,12).getTime();
function _serverNowMs(){return fixtureNow;}
const newTabs=[];
window.open=(...args)=>{newTabs.push(args);return null;};
function backgroundScene(count){
 _allProjects.length=0;
 activeSidForSidebar='p0';window._sidebarDensity='detailed';searchQueryRaw='';
 _expandedChildSessionKeys.clear();_expandedLineageKeys.clear();
 fixtureSessions=Array.from({length:count},(_,i)=>({session_id:'p'+i,
  title:'Conversation '+i,message_count:3,profile:'default',
  updated_at:fixtureNow/1000-86400*(1+Math.floor(i/10))-i,
  _compression_segment_count:4,
  _lineage_segments:Array.from({length:4},(_,j)=>({session_id:'prior'+i+'-'+j,title:'Earlier '+j})),
  _child_session_count:1,_child_sessions:[{session_id:'c'+i,title:'Child '+i,message_count:3,profile:'default',relationship_type:'child_session',session_source:'other'}]}));
 const l=$('sessionList');l.scrollTop=0;repaint();
}
function snapshot(){
 return {...visible(),selected:activeSidForSidebar,
  virtual:$('sessionList').dataset.sessionVirtualEnabled,
  rendered:$('sessionList').querySelectorAll('.session-date-body>.session-item').length};
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before-ref')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def source(path):
        if args.before_ref:
            return subprocess.check_output(['git', 'show', f'{args.before_ref}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()

    results, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in [300, 180]:
            page = browser.new_page(viewport={'width': 900, 'height': 800})
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content(f'<base href="http://fixture.invalid/"><input id="sessionSearch" hidden>'
                             f'<div id="sessionList" style="width:{width}px;height:320px;overflow:auto;background:var(--sidebar)"></div>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=script(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.evaluate('document.documentElement.dataset.skin="graphite"')
            for count in [40, 120]:
                for transition in ['refresh', 'resort', 'height', 'expansion']:
                    page.evaluate('(n)=>backgroundScene(n)', count)
                    page.wait_for_timeout(80)
                    page.evaluate('$("sessionList").scrollTop=600')
                    page.wait_for_timeout(100)
                    # Force a known partially clipped first row without further
                    # user input during the background transition.
                    page.evaluate('''() => {
                      const l=$('sessionList'),r=visible().rows[0];
                      l.scrollTop+=r.y+12;
                    }''')
                    page.wait_for_timeout(100)
                    before = page.evaluate('snapshot()')
                    assert before['rows'][0]['y'] < 0
                    assert not any(r['id'] == 'p0' for r in before['rows'])
                    assert (before['virtual'] == 'true') == (count > 80)
                    page.screenshot(path=str(args.output / f'{width}-{count}-{transition}-before.png'))
                    page.evaluate('''([transition,id]) => {
                      if(transition==='resort'){
                        fixtureSessions.find(s=>s.session_id===id).updated_at=fixtureNow/1000;
                      }else if(transition==='height'){
                        window._sidebarDensity='compact';
                      }else if(transition==='expansion'){
                        const index=fixtureSessions.findIndex(s=>s.session_id===id);
                        _expandedChildSessionKeys.add(fixtureSessions[index-1].session_id);
                      }
                      repaint();
                    }''', [transition, before['rows'][0]['id']])
                    page.wait_for_timeout(150)
                    after = page.evaluate('snapshot()')
                    page.screenshot(path=str(args.output / f'{width}-{count}-{transition}-after.png'))
                    failures = []
                    if transition in ['resort', 'refresh']:
                        if abs(after['scrollTop'] - before['scrollTop']) > 1:
                            failures.append('background refresh steals numeric scroll')
                    else:
                        anchor = next((r for r in after['rows'] if r['id'] == before['rows'][0]['id']), None)
                        if not anchor or abs(anchor['y'] - before['rows'][0]['y']) > 1:
                            failures.append('stable-order height correction loses anchor offset')
                        if abs(after['scrollTop'] - before['scrollTop']) < 1:
                            failures.append('fixture did not exercise height correction')
                    if count > 80 and after['rendered'] >= 80:
                        failures.append('unbounded DOM')
                    if transition == 'resort':
                        assert page.evaluate('(id)=>$("sessionList")._sessionVirtualLayout.rows[0].id===id', before['rows'][0]['id'])
                        assert page.locator('.session-date-header').first.text_content().endswith('Today')
                    results.append(dict(width=width, count=count, transition=transition,
                                        before=before, after=after, failures=failures))
            # Positive navigation assertions use real renderer and actual event
            # consumption; only window.open and same-tab transport are recorded.
            page.evaluate('backgroundScene(40);$("sessionList").scrollTop=0;opened.length=0;newTabs.length=0;')
            row = page.locator('.session-item[data-sid="p0"] .session-title')
            for modifier in ['Control', 'Meta']:
                row.click(modifiers=[modifier])
                assert page.evaluate('opened.length') == 0
                assert page.evaluate('newTabs.at(-1)[0]') == '/session/p0'
            row.click(button='middle')
            assert page.evaluate('newTabs.length') == 3
            assert page.evaluate('opened.length') == 0
            page.evaluate('_expandedChildSessionKeys.add("p0");repaint()')
            child = page.locator('.session-child-session[data-sid="c0"]')
            child.click(button='middle')
            assert page.evaluate('newTabs.at(-1)[0]') == '/session/c0?exact=1'
            assert page.evaluate('opened.length') == 0
            # Action exclusion: count disclosure must not open a tab.
            page.locator('.session-item[data-sid="p0"] .session-child-count').click(modifiers=['Control'])
            assert page.evaluate('newTabs.length') == 4
            row.click()
            page.wait_for_function('opened.at(-1)?.sid === "p0"')
            results.append(dict(width=width, transition='navigation', tabs=page.evaluate('newTabs'), failures=[]))
            page.close()
        browser.close()
    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results),
                  page_errors=errors, results=results)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return int(bool(report['failures'] or errors))


if __name__ == '__main__':
    raise SystemExit(main())
