"""Real sidebar grouping/window/row renderer and scroll listener; no server/state.

Run with Playwright, optionally --before-ref to exercise an exact prior revision.
The fixture supplies sorted groups and navigation recording, not replacement
layout, windowing, scroll scheduling, child projection, or CSS.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402


def script(source):
    component = component_script(source).replace(
        "const animateRefresh=false, searchQueryRaw='';",
        "const animateRefresh=false;let searchQueryRaw='';")
    # The production grouping/window renderer consumes the same projected rows
    # as the full cache renderer, whose API/filtering seams are not at issue.
    start = source.index('  const flatSessionRows=[];')
    end = source.index('  const archivePagingFilterActive=', start)
    names = ['_sessionVirtualWindow', '_sessionVirtualSpacer',
             '_scheduleSessionVirtualizedRender', '_ensureSessionVirtualScrollHandler',
             '_markSessionListPointerDown', '_markSessionListPointerUp',
             '_resyncSessionVirtualWindowAfterRender',
             '_sessionSearchRanges', '_appendHighlightedText']

    for name in ['_sessionVirtualLayoutKey','_sessionVirtualLayout','_sessionVirtualOffsets','_sessionVirtualViewportAnchor','_measureSessionVirtualRows']:
        if 'function '+name+'(' in source:
            names.append(name)
    functions = '\n'.join(re.search(r'^function ' + n + r'\(.*?^\}', source, re.M | re.S).group() for n in names)
    constants = '\n'.join(re.search(r'^const ' + n + r'\s*=.*?;', source, re.M).group() for n in [
        'SESSION_VIRTUAL_ROW_HEIGHT', 'SESSION_VIRTUAL_BUFFER_ROWS', 'SESSION_VIRTUAL_THRESHOLD_ROWS'])
    return component + constants + functions + r"""
let _sessionVirtualScrollList=null,_sessionVirtualScrollRaf=0,_sessionVirtualResyncRaf=0;
let _sessionListLastScrollAt=0,_sessionListSkeletonActive=false,_sessionListPointerActive=false;
let _pendingSessionListPayload=null,_sessionVisibleSidebarIds=[];
const _groupCollapsed={},_pending=new Set();
function _saveCollapsed(){}
const _loadingSessionId=null;
const $=id=>document.getElementById(id);
let _hideSearchPreviewsAfterSelect=false;
function li(){return '<svg width="12" height="12"></svg>';}
function _sessionTitleForForkParent(){return 'Original parent';}
function _truncatedSessionId(sid){return sid;}
function _sessionForkTooltip(parent){return parent;}
function _fetchLineageReportForRow(){throw Error('Fixture has complete lineage');}
let groups=[],renderCount=0;
function repaint(){
 renderCount++;
 const list=document.querySelector('#sessionList'),listScrollTopBeforeRender=list.scrollTop;
 const q=searchQueryRaw;
 const viewportAnchorBeforeRender=typeof _sessionVirtualViewportAnchor==='function'?_sessionVirtualViewportAnchor(list):null;
 list.replaceChildren();
""" + source[start:end] + r"""
}
function scene(density='detailed',kind='children'){
 window._sidebarDensity=density;searchQueryRaw='';
 _expandedChildSessionKeys.clear();_expandedLineageKeys.clear();
 fixtureSessions=[];
 for(let i=0;i<120;i++){
  const parent={session_id:'p'+i,title:'Conversation '+i,message_count:3,updated_at:120-i,has_unread:i===0,
   _compression_segment_count:kind==='plain'||(kind==='sparse'&&i!==17)?0:4,
   _lineage_segments:kind==='plain'||(kind==='sparse'&&i!==17)?[]:Array.from({length:4},(_,j)=>({session_id:'prior'+i+'-'+j,title:'Earlier turn '+j,updated_at:j+1}))};
  const child={session_id:'c'+i,title:'Child task '+i,message_count:3,parent_session_id:'p'+i,
   relationship_type:'child_session',raw_source:'subagent',session_source:'other',
   archived:kind==='reference',is_streaming:i===1,attention:{kind:'approval',count:1}};
  fixtureSessions.push(parent);
  if(kind!=='plain'&&kind!=='childless')fixtureSessions.push(child);
 }
 const parents=fixtureSessions.filter(s=>s.session_id.startsWith('p'));
 const projected=_attachChildSessionsToSidebarRows(parents,fixtureSessions.filter(s=>!s.archived),fixtureSessions);
 groups=[{label:'Today',items:projected}];
 document.querySelector('#sessionList').scrollTop=0;
 repaint();
}
function visible(){
 const list=document.querySelector('#sessionList'),lr=list.getBoundingClientRect();
 return {scrollTop:list.scrollTop,rows:[...list.querySelectorAll('.session-date-body>.session-item')]
  .map(e=>({id:e.dataset.sid,y:e.getBoundingClientRect().top-lr.top,height:e.getBoundingClientRect().height}))
  .filter(r=>r.y+r.height>0&&r.y<list.clientHeight),renders:renderCount};
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
        for width, touch in [(1280, False), (768, False), (390, True)]:
            context = browser.new_context(viewport={'width': width, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            sidebar_width = 180 if touch else 300
            page.set_content(f'<input id="sessionSearch" hidden><div id="sessionList" style="width:{sidebar_width}px;height:520px;overflow:auto;background:var(--sidebar)"></div>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=script(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.evaluate('document.documentElement.dataset.skin="graphite"')
            for kind in ['children', 'reference', 'sparse']:
                page.evaluate('k=>scene("detailed",k)', kind)
                summary_count = page.locator('.session-lineage-summary').count()
                assert summary_count > 0, 'Risky lineage summary must render'
                if kind == 'sparse':
                    assert summary_count == 1, 'Sparse fixture must have exactly one summary'
                # Pause event delivery, not layout or the real RAF scheduler,
                # to observe the content immediately before the scroll callback.
                page.evaluate('_sessionVirtualScrollList.removeEventListener("scroll",_scheduleSessionVirtualizedRender)')
                page.evaluate('document.querySelector("#sessionList").scrollTop=1000')
                page.wait_for_timeout(40)
                before = page.evaluate('visible()')
                page.screenshot(path=str(args.output / f'{width}-{kind}-scroll-before.png'))
                page.evaluate('_sessionVirtualScrollList.addEventListener("scroll",_scheduleSessionVirtualizedRender,{passive:true});_scheduleSessionVirtualizedRender()')
                page.wait_for_timeout(40)
                after = page.evaluate('visible()')
                page.screenshot(path=str(args.output / f'{width}-{kind}-scroll-after.png'))
                failures = []
                if (before['rows'][0] != after['rows'][0] or before['scrollTop'] != after['scrollTop']
                        or any(row not in after['rows'] for row in before['rows'])):
                    failures.append('unchanged scrollTop skips visible conversation/offset')
                if page.locator('.session-date-body>.session-item').count() >= 80:
                    failures.append('variable-height list must keep a bounded DOM window')
                page.evaluate('activeSidForSidebar="p100";repaint()')
                page.wait_for_timeout(40)
                if not any(r['id'] == 'p100' for r in page.evaluate('visible()')['rows']):
                    failures.append('active conversation not brought into view')
                page.evaluate('activeSidForSidebar="other"')
                page.evaluate('const l=document.querySelector("#sessionList");l.scrollTop=l.scrollHeight')
                page.wait_for_timeout(100)
                bottom = page.evaluate('visible()')
                if not any(r['id'] == 'p119' for r in bottom['rows']):
                    failures.append('last conversation inaccessible')
                results.append(dict(scene='scroll',width=width,kind=kind,summary_count=summary_count,before=before,after=after,bottom=bottom,failures=failures))
            # Density changes derive mode from the new rows, not retained state.
            for density, kind in [('compact','children'),('detailed','plain'),('detailed','childless'),('detailed','children')]:
                page.evaluate('([d,k])=>scene(d,k)', [density,kind])
                virtual = page.locator('.session-virtual-spacer').count() > 0
                expected = True
                results.append(dict(scene='mode',width=width,density=density,kind=kind,virtual=virtual,failures=[] if virtual==expected else ['wrong density/lineage virtualization mode']))
                if density == 'compact':
                    page.evaluate('document.querySelector("#sessionList").scrollTop=1000')
                    page.wait_for_timeout(80)
                    assert page.evaluate('Number(document.querySelector("#sessionList").dataset.sessionVirtualStart)') > 0
                    assert page.locator('.session-date-body>.session-item').count() < 120
                    page.evaluate('const l=document.querySelector("#sessionList");l.scrollTop=l.scrollHeight')
                    page.wait_for_timeout(80)
                    assert page.locator('.session-item[data-sid="p119"]').count() == 1
            # Reload and return from search must bring an off-window active row
            # into the viewport, not merely include it in the overscan DOM.
            for entry in ['reload', 'search-return']:
                page.evaluate('scene("detailed","children")')
                if entry == 'search-return':
                    page.evaluate('window.unfilteredGroups=groups;searchQueryRaw="Conversation 100";$("sessionSearch").value=searchQueryRaw;'
                                  'groups=[{label:"Today",items:groups[0].items.filter(s=>s.session_id==="p100")}];'
                                  'repaint()')
                    assert page.locator('.session-search-hit').count() > 0
                    assert page.locator('.session-child-session-delegated').count() == 1
                    target = page.locator('.session-item[data-sid="p100"] .session-title')
                    target.tap() if touch else target.click()
                    page.wait_for_function('opened.at(-1)?.sid === "p100"')
                    page.evaluate('activeSidForSidebar="p100";repaint();'
                                  'searchQueryRaw="";$("sessionSearch").value="";groups=unfilteredGroups;repaint()')
                else:
                    page.evaluate('const l=document.querySelector("#sessionList");l.scrollTop=0;'
                                  'delete l.dataset.sessionVirtualActiveAnchor;'
                                  'activeSidForSidebar="p100";repaint()')
                page.wait_for_timeout(80)
                state = page.evaluate('visible()')
                assert any(r['id'] == 'p100' for r in state['rows']), entry
                results.append(dict(scene=entry,width=width,state=state,failures=[]))
                page.evaluate('activeSidForSidebar="other"')
            # A pending RAF must use the current measured layout after density,
            # localized wrapping, width and typography have changed.
            page.evaluate('scene("detailed","children");document.querySelector("#sessionList").scrollTop=1000')
            page.wait_for_timeout(80)
            page.evaluate('_scheduleSessionVirtualizedRender();window._sidebarDensity="compact";repaint()')
            page.wait_for_timeout(80)
            assert page.locator('.session-date-body>.session-item').count() < 80
            page.evaluate('window._sidebarDensity="detailed";setLocale("pl");'
                          'document.documentElement.dataset.fontSize="xlarge";'
                          'document.documentElement.classList.add("dark");'
                          'document.querySelector("#sessionList").style.width="180px";repaint()')
            page.wait_for_timeout(80)
            state = page.evaluate('visible()')
            page.evaluate('_scheduleSessionVirtualizedRender()')
            page.wait_for_timeout(80)
            settled = page.evaluate('visible()')
            assert state['rows'][0] == settled['rows'][0]
            assert state['scrollTop'] == settled['scrollTop']
            assert page.locator('.session-date-body>.session-item').count() < 80
            heights = page.locator('.session-date-body>.session-item').evaluate_all('(els)=>els.map(e=>e.getBoundingClientRect().height)')
            results.append(dict(scene='layout-transition',width=width,state=state,settled=settled,heights=heights,failures=[]))
            page.evaluate('setLocale("en");delete document.documentElement.dataset.fontSize;'
                          'document.documentElement.classList.remove("dark")')
            page.evaluate('scene("detailed","children");_expandedChildSessionKeys.add("p0");'
                          '_expandedLineageKeys.add("p5");repaint();'
                          'document.querySelector("#sessionList").scrollTop=1000')
            page.wait_for_timeout(80)
            expanded_before = page.evaluate('visible()')
            page.evaluate('repaint();_scheduleSessionVirtualizedRender()')
            page.wait_for_timeout(80)
            expanded_after = page.evaluate('visible()')
            assert expanded_before['rows'][0] == expanded_after['rows'][0]
            assert expanded_before['scrollTop'] == expanded_after['scrollTop']
            assert page.locator('.session-date-body>.session-item').count() < 80
            results.append(dict(scene='expanded-scroll',width=width,before=expanded_before,after=expanded_after,failures=[]))
            page.evaluate('scene("detailed","children")')
            parent = page.locator('.session-item[data-sid="p0"]')
            assert 'unread' in parent.get_attribute('class'), 'Parent unread must coexist with child attention'
            chip = parent.locator('.session-child-count')
            chip.tap() if touch else chip.click()
            child = page.locator('.session-child-session-delegated').first
            assert child.is_visible(), 'Child disclosure must render a usable child'
            child.tap() if touch else child.click()
            assert page.evaluate('opened.at(-1).sid') == 'c0'
            page.locator('.session-item[data-sid="p0"] .session-child-count').press('Enter')
            assert page.locator('.session-child-session-delegated').count() == 0
            lineage = page.locator('.session-item[data-sid="p0"] .session-lineage-count')
            lineage.tap() if touch else lineage.click()
            segment = page.locator('.session-lineage-segment').first
            assert segment.is_visible(), 'Prior-turn disclosure must render a usable segment'
            segment.tap() if touch else segment.click()
            assert page.evaluate('opened.at(-1).sid') == 'prior0-3'
            page.locator('.session-item[data-sid="p0"] .session-lineage-count').press('Enter')
            assert page.locator('.session-lineage-segment').count() == 0
            assert page.locator('.session-item[data-sid="p1"] .session-child-activity-indicator').count() == 1
            results.append(dict(scene='journey',width=width,touch=touch,failures=[]))
            context.close()
        browser.close()
    failures = [r for r in results if r['failures']]
    (args.output / 'report.json').write_text(json.dumps(dict(results=results,errors=errors),indent=2))
    print(json.dumps(dict(cases=len(results),failures=len(failures),errors=errors)))
    if failures or errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
