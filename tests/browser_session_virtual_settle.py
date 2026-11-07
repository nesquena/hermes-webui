"""Layout invalidation must settle the real virtual sidebar without a scroll.

Uses the production renderer, controls, geometry and RAFs; no server/state.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.browser_session_virtual_geometry import geometry_script  # noqa: E402
from tests._sidebar_child_status_helpers import ROOT  # noqa: E402

PROBE = """() => {
 const l=$('sessionList'),top=l.getBoundingClientRect().top;
 return {...visible(),dom:l.querySelectorAll('.session-date-body>.session-item').length,
   rafs:window.rafCount,spacers:[...l.querySelectorAll('.session-virtual-spacer')].map(e=>{
     const r=e.getBoundingClientRect();return Math.max(0,Math.min(r.bottom-top,l.clientHeight)-Math.max(0,r.top-top));
   }).filter(h=>h>1)};
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
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
        for grouped in [False, True]:
            for transition in ['dark', 'dark-live', 'light', 'narrow', 'wide', 'locale',
                               'density-pending', 'expanded', 'search', 'near-bottom', 'superseded']:
                page = browser.new_page(viewport={'width': 1280, 'height': 800})
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.set_content('<input id="sessionSearch" hidden><div id="sessionList" style="width:300px;height:520px;overflow:auto;background:var(--sidebar)"></div>')
                page.add_style_tag(content=source('static/style.css'))
                page.add_script_tag(content=geometry_script(source('static/sessions.js')))
                page.add_script_tag(content=source('static/i18n.js'))
                page.evaluate('''() => {
                    document.documentElement.dataset.skin='graphite';window.rafCount=0;
                    const raf=window.requestAnimationFrame;
                    window.requestAnimationFrame=cb=>raf(t=>{window.rafCount++;cb(t);});
                }''')
                page.evaluate('(g)=>{projectScene(20,g?6:1);}', grouped)
                if transition == 'light':
                    page.evaluate('document.documentElement.classList.add("dark");repaint()')
                if transition == 'expanded':
                    page.evaluate('''grouped => {
                        scene('detailed','children');
                        const rows=groups[0].items;
                        if(grouped) groups=Array.from({length:6},(_,i)=>({label:'Date '+i,items:rows.slice(i*20,(i+1)*20)}));
                        for(const row of rows){_expandedChildSessionKeys.add(row.session_id);_expandedLineageKeys.add(row.session_id);}
                        repaint();
                    }''', grouped)
                    assert page.locator('.session-child-session').count() > 0
                    assert page.locator('.session-lineage-segment').count() > 0
                if transition == 'search':
                    page.evaluate('''() => {
                        window._sidebarDensity='detailed';searchQueryRaw='needle';$('sessionSearch').value=searchQueryRaw;
                        for(const s of groups.flatMap(g=>g.items)){s.match_type='content';s.match_preview='A needle in the content';}
                        repaint();
                    }''')
                    assert page.locator('.session-search-preview').count() > 0
                if transition == 'wide':
                    page.evaluate('$("sessionList").style.width="240px";repaint()')
                page.wait_for_timeout(100)
                page.evaluate('$("sessionList").scrollTop=3000')
                if transition == 'near-bottom':
                    page.evaluate('$("sessionList").scrollTop=$("sessionList").scrollHeight-520')
                page.wait_for_timeout(150)
                if transition == 'search':
                    sid = page.evaluate('visible().rows[0].id')
                    page.evaluate('(sid)=>{activeSidForSidebar=sid;repaint();}', sid)
                    page.wait_for_timeout(100)
                    assert page.locator('.session-date-body>.session-item.active').count() == 1
                before = page.evaluate(PROBE)
                page.screenshot(path=str(args.output / f'{grouped}-{transition}-before.png'))
                # Remove only scroll delivery: the correction must work without it.
                if transition != 'dark-live':
                    page.evaluate('_sessionVirtualScrollList.removeEventListener("scroll",_scheduleSessionVirtualizedRender)')
                actions = {'dark': 'document.documentElement.classList.toggle("dark")',
                           'dark-live': 'document.documentElement.classList.toggle("dark")',
                           'light': 'document.documentElement.classList.toggle("dark")',
                           'narrow': '$("sessionList").style.width="240px"',
                           'wide': '$("sessionList").style.width="300px"',
                           'locale': 'setLocale("de")',
                           'density-pending': '_scheduleSessionVirtualizedRender();window._sidebarDensity="detailed"',
                           'expanded': 'document.documentElement.classList.toggle("dark")',
                           'search': '_hideSearchPreviewsAfterSelect=true;document.documentElement.classList.toggle("dark")',
                           'near-bottom': '$("sessionList").style.width="240px"',
                           'superseded': 'document.documentElement.classList.toggle("dark");repaint();$("sessionList").style.width="240px"'}
                page.evaluate(actions[transition] + ';repaint()')
                page.wait_for_timeout(150)
                after = page.evaluate(PROBE)
                anchor = page.evaluate('(sid)=>geometry(sid)', before['rows'][0]['id'])
                page.screenshot(path=str(args.output / f'{grouped}-{transition}-after.png'))
                page.wait_for_timeout(150)
                stable = page.evaluate(PROBE)
                failures = []
                if after['spacers']:
                    failures.append('visible blank spacer after stationary layout invalidation')
                if after['dom'] >= 80:
                    failures.append('unbounded DOM')
                if stable != after:
                    failures.append('render/RAF/geometry did not stop')
                if after['renders'] - before['renders'] > (3 if transition == 'superseded' else 2) or after['rafs'] - before['rafs'] > 3:
                    failures.append('more than one corrective render')
                # A shrinking partially clipped selected/preview row can end up
                # wholly above the viewport. Its offset must still be preserved.
                if anchor['top'] is None or abs(anchor['top'] - before['rows'][0]['y']) > 1:
                    failures.append('stationary anchor ID/offset changed')
                if anchor['bottom'] is not None and anchor['bottom'] > 0 and (
                        not after['rows'] or after['rows'][0]['id'] != before['rows'][0]['id']):
                    failures.append('still-visible top ID changed')
                results.append(dict(grouped=grouped, transition=transition, before=before, after=after,
                                    anchor=anchor, failures=failures))
                page.close()
        browser.close()
    (args.output / 'report.json').write_text(json.dumps(dict(results=results, errors=errors), indent=2))
    print(json.dumps(dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors)))
    if errors or any(r['failures'] for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
