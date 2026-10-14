"""Measured offscreen cache invalidation through real batch/report lifecycles.

Production grouping, row renderer, CSS, report merge/fetch and selection toggles;
only sorted input groups and the deferred API transport are supplied.
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


def component(source):
    result = geometry_script(source).replace(
        'const _showArchived=false, _sessionSelectMode=false, _showAllProfiles=false;',
        'const _showArchived=false, _showAllProfiles=false;let _sessionSelectMode=false;')
    fetch = re.search(r'^function _fetchLineageReportForRow\(.*?^\}', source, re.M | re.S).group()
    result = result.replace("function _fetchLineageReportForRow(){throw Error('Fixture has complete lineage');}", fetch)
    for name in ['toggleSessionSelectMode', 'exitSessionSelectMode']:
        result += re.search(r'^function ' + name + r'\(.*?^\}', source, re.M | re.S).group()
    return result + """
const _selectedSessions=new Set();
let _lineageReportCacheGeneration=0,resolveReport;
function api(){return new Promise(resolve=>{resolveReport=resolve;});}
function heightState(){
 const l=$('sessionList'),layout=l._sessionVirtualLayout;
 const e=l.querySelector('.session-date-body>.session-item[data-sid="p0"]');
 return {measured:layout.measured.get('p0')||null,shape:layout.rows[0].shape,
  spacerInput:layout.offsets[1]-layout.offsets[0],scrollTop:l.scrollTop,
  actual:e?e.getBoundingClientRect().height+(parseFloat(getComputedStyle(e).marginBottom)||0):null,
  segmentIds:e?[...e.querySelectorAll('.session-lineage-segment')].map(e=>e.dataset.sid):[],
  rows:l.querySelectorAll('.session-date-body>.session-item').length,
  cachedReport:_lineageReportCache.size,inflight:_lineageReportInflight.size};
}
function inputScene(){
 _sessionSelectMode=false;_lineageReportCache.clear();_lineageReportInflight.clear();
 activeSidForSidebar='other';scene('detailed','children');
 for(const s of groups[0].items){s._compression_segment_count=1000000; s._lineage_segments=[];}
 repaint();
}
"""


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
        for width in [185, 195]:
            page = browser.new_page(viewport={'width': 390, 'height': 800}, has_touch=True)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_content(f'<input id="sessionSearch" hidden><div id="sessionList" style="width:{width}px;height:180px;overflow:auto;background:var(--sidebar)"></div>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=component(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.evaluate('document.documentElement.dataset.skin="graphite";setLocale("fr")')
            for mode in ['batch-enter', 'batch-exit', 'report', 'report-batch', 'report-local']:
                page.evaluate('inputScene()')
                if mode == 'batch-exit':
                    page.evaluate('toggleSessionSelectMode()')
                if mode.startswith('report'):
                    if mode == 'report-local':
                        page.evaluate('groups[0].items[0]._lineage_segments=[{session_id:"late0",title:"Local earlier turn",updated_at:1}];_expandedChildSessionKeys.add("p0")')
                    page.evaluate('groups[0].items[0]._compression_segment_count=4;repaint()')
                    page.locator('.session-item[data-sid="p0"] .session-lineage-count').click()
                    page.wait_for_function('_lineageReportInflight.size===1')
                page.wait_for_timeout(100)
                initial = page.evaluate('heightState()')
                page.screenshot(path=str(args.output / f'{width}-{mode}-initial.png'))
                page.evaluate('$("sessionList").scrollTop=5000')
                page.wait_for_timeout(100)
                assert page.locator('.session-date-body>.session-item[data-sid="p0"]').count() == 0
                before = page.evaluate('heightState()')
                if mode.startswith('report'):
                    if mode == 'report-batch':
                        page.evaluate('toggleSessionSelectMode()')
                    page.evaluate('resolveReport({found:true,segments:Array.from({length:4},(_,i)=>({session_id:"late"+i,title:"Fetched earlier turn "+i,updated_at:i+1}))})')
                    page.wait_for_function('_lineageReportInflight.size===0 && _lineageReportCache.size===1')
                elif mode == 'batch-enter':
                    page.evaluate('toggleSessionSelectMode()')
                else:
                    page.evaluate('exitSessionSelectMode()')
                page.wait_for_timeout(100)
                offscreen = page.evaluate('heightState()')
                page.screenshot(path=str(args.output / f'{width}-{mode}-offscreen.png'))
                page.evaluate('$("sessionList").scrollTop=0')
                page.wait_for_timeout(100)
                actual = page.evaluate('heightState()')
                page.screenshot(path=str(args.output / f'{width}-{mode}-actual.png'))
                failures = []
                if offscreen['measured'] is not None:
                    failures.append('stale measured height retained offscreen')
                if before['shape'] == offscreen['shape']:
                    failures.append('height-changing input absent from row shape')
                if initial['actual'] == actual['actual']:
                    failures.append('fixture did not change rendered height')
                if mode.startswith('report') and actual['segmentIds'] != ['late3', 'late2', 'late1', 'late0']:
                    failures.append('report rows not rendered')
                if offscreen['rows'] >= 80:
                    failures.append('unbounded DOM')
                results.append(dict(width=width,mode=mode,initial=initial,before=before,offscreen=offscreen,actual=actual,failures=failures))
            page.close()
        browser.close()
    (args.output / 'report.json').write_text(json.dumps(dict(results=results, errors=errors), indent=2))
    print(json.dumps(dict(cases=len(results),failures=sum(bool(r['failures']) for r in results),errors=errors)))
    if errors or any(r['failures'] for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
