"""Real grouped sidebar geometry: active anchoring and search-preview invalidation.

No server or agent state. Uses production controls, grouping renderer, rows,
measured virtualization, scroll listener and CSS; only sorted groups are seeded.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.browser_lineage_scroll_selection import script  # noqa: E402
from tests._sidebar_child_status_helpers import ROOT  # noqa: E402


def geometry_script(source):
    component = script(source)
    preview = re.search(r'^function _sessionSearchContentPreview\(.*?^\}', source, re.M | re.S).group()
    component = component.replace("function _sessionSearchContentPreview(){return '';}", preview)
    # Include the actual profile/archive controls preceding the date groups.
    start = source.index('  // Project filter bar — show when there are real projects')
    end = source.index('  // Empty state for active project filter', start)
    component = component.replace(' list.replaceChildren();', ' list.replaceChildren();\n' + source[start:end])
    return component + """
const _otherProfileCount=3,archivedCount=12,profileFiltered=[];
let _activeProject=null;
const NO_PROJECT_FILTER='unassigned',projectIdFor=s=>s.project_id;
function _sidebarHasUnprojectedRows(){return false;}
let projectGroups=[];
function _setActiveProjectFilter(id){
 _activeProject=id;
 groups=projectGroups.map(g=>({...g,items:g.items.filter(s=>!id||s.project_id===id)}));
 repaint();
}
function projectScene(count=20,headerCount=6){
 _allProjects.splice(0,_allProjects.length,...Array.from({length:count},(_,i)=>({project_id:'project'+i,name:'Project '+i+' authentication',color:'#abc'})));
 activeSidForSidebar='other';window._sidebarDensity='compact';searchQueryRaw='';
 const rows=Array.from({length:200},(_,i)=>({session_id:'p'+i,title:'Conversation '+i,message_count:3,project_id:'project'+i%count}));
 fixtureSessions=rows;
 groups=Array.from({length:headerCount},(_,i)=>({label:'Date '+i,items:rows.slice(Math.floor(i*200/headerCount),Math.floor((i+1)*200/headerCount))}));
 projectGroups=groups;
 $('sessionList').scrollTop=0;repaint();
}
function groupScene(){
 _allProjects.length=0;
 scene('detailed','plain');
 const rows=groups[0].items;
 groups=['★ Pinned','Today','Yesterday','This week','Last week','Older'].map((label,i)=>({
   label,isPinned:i===0,items:rows.slice(i*20,(i+1)*20)}));
 repaint();
}
function geometry(id){
 const l=$('sessionList'),e=l.querySelector('.session-date-body>.session-item[data-sid="'+id+'"]');
 const r=e?.getBoundingClientRect(),lr=l.getBoundingClientRect();
 return {id,top:r?r.top-lr.top:null,bottom:r?r.bottom-lr.top:null,height:l.clientHeight,
   scrollTop:l.scrollTop,rows:l.querySelectorAll('.session-date-body>.session-item').length,
   headers:l.querySelectorAll('.session-date-header').length};
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--before-ref')
    parser.add_argument('--case', choices=['anchor', 'selection', 'previews', 'projects', 'all'], default='all')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def source(path):
        if args.before_ref:
            return subprocess.check_output(['git', 'show', f'{args.before_ref}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()

    results, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width, touch in [(1280, False), (390, True)]:
            context = browser.new_context(viewport={'width': width, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content('<input id="sessionSearch" hidden><div id="sessionList" style="width:300px;height:180px;overflow:auto;background:var(--sidebar)"></div>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=geometry_script(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.evaluate('document.documentElement.dataset.skin="graphite"')
            if touch:
                page.evaluate('$("sessionList").style.width="180px"')
            if args.case in ['anchor', 'all']:
                for entry, sid, position in [(entry, sid, position)
                                             for entry in ['reload', 'search-return']
                                             for sid, position in [('p0', 0), ('p2', 0), ('p10', 0),
                                                                   ('p15', 0), ('p100', 0), ('p10', 900)]]:
                    page.evaluate('activeSidForSidebar="other";groupScene()')
                    page.evaluate('(position)=>$("sessionList").scrollTop=position', position)
                    page.wait_for_timeout(100)
                    initial = page.evaluate('(sid)=>geometry(sid)', sid)
                    if entry == 'search-return':
                        page.evaluate('(sid)=>{window.savedGroups=groups;searchQueryRaw="Conversation "+sid.slice(1);$("sessionSearch").value=searchQueryRaw;'
                                      'groups=[{label:"Older",items:groups.flatMap(g=>g.items).filter(s=>s.session_id===sid)}];repaint()}', sid)
                        target = page.locator(f'.session-item[data-sid="{sid}"] .session-title')
                        target.tap() if touch else target.click()
                        page.evaluate('(sid)=>{activeSidForSidebar=sid;repaint();searchQueryRaw="";$("sessionSearch").value="";groups=savedGroups;repaint()}', sid)
                    else:
                        page.evaluate('(sid)=>{activeSidForSidebar=sid;delete $("sessionList").dataset.sessionVirtualActiveAnchor;repaint()}', sid)
                    page.wait_for_timeout(100)
                    state = page.evaluate('(sid)=>geometry(sid)', sid)
                    page.screenshot(path=str(args.output / f'{width}-{entry}-{sid}-{position}.png'))
                    failures = []
                    if state['top'] is None or state['top'] < 0 or state['bottom'] > state['height']:
                        failures.append('active row not fully within short grouped viewport')
                    if state['rows'] >= 80:
                        failures.append('unbounded DOM')
                    if entry == 'reload' and initial['top'] is not None and initial['top'] >= 0 and initial['bottom'] <= initial['height']:
                        if state['scrollTop'] != initial['scrollTop']:
                            failures.append('already visible activation steals scroll')
                    # Ordinary refresh must not recenter an unchanged active ID.
                    page.evaluate('$("sessionList").scrollTop=3500')
                    page.wait_for_timeout(100)
                    before_refresh = page.evaluate('visible()')
                    page.evaluate('repaint()')
                    page.wait_for_timeout(100)
                    after_refresh = page.evaluate('visible()')
                    if before_refresh['rows'][0] != after_refresh['rows'][0]:
                        failures.append('ordinary refresh steals scroll from active row')
                    results.append(dict(scene=entry, width=width, state=state, initial=initial, failures=failures))
            if args.case in ['selection', 'all']:
                for kind, sid in [('plain', 'p0'), ('children', 'prior0-0')]:
                    page.evaluate('([kind,sid])=>{activeSidForSidebar=sid;scene("detailed",kind)}', [kind, sid])
                    initial = page.evaluate('$("sessionList")._sessionVirtualLayout.measured.get("p0")')
                    assert page.locator('.session-date-body>.session-item[data-sid="p0"].active').count() == 1
                    page.evaluate('$("sessionList").scrollTop=3500')
                    page.wait_for_timeout(100)
                    assert page.locator('.session-date-body>.session-item[data-sid="p0"]').count() == 0
                    page.evaluate('activeSidForSidebar="other";repaint()')
                    retained = page.evaluate('$("sessionList")._sessionVirtualLayout.measured.get("p0") || null')
                    page.evaluate('$("sessionList").scrollTop=0')
                    page.wait_for_timeout(100)
                    actual = page.evaluate('$("sessionList")._sessionVirtualLayout.measured.get("p0")')
                    failures = []
                    if retained is not None:
                        failures.append('offscreen selected measurement survives deselection')
                    if initial['height'] == actual['height']:
                        failures.append('fixture does not exercise selection height change')
                    results.append(dict(scene='selection', width=width, kind=kind, initial=initial,
                                        retained=retained, actual=actual, failures=failures))
            if args.case in ['previews', 'all']:
                for transition in ['hide', 'show']:
                    page.evaluate('activeSidForSidebar="other";scene("detailed","plain");'
                                  'searchQueryRaw="needle";$("sessionSearch").value=searchQueryRaw;'
                                  'for(const s of groups[0].items){s.match_type="content";s.match_preview="A needle in the content";}')
                    page.evaluate('hide=>{_hideSearchPreviewsAfterSelect=hide;repaint();}', transition == 'show')
                    initial = page.locator('.session-search-preview').count()
                    assert (initial > 0) == (transition == 'hide')
                    # Measure p0 while rendered, then make it offscreen.
                    page.evaluate('$("sessionList").scrollTop=3500')
                    page.wait_for_timeout(100)
                    assert page.locator('.session-date-body>.session-item[data-sid="p0"]').count() == 0
                    before = page.evaluate('visible()')
                    page.evaluate('_hideSearchPreviewsAfterSelect=!_hideSearchPreviewsAfterSelect;repaint()')
                    page.wait_for_timeout(100)
                    after = page.evaluate('visible()')
                    state = page.evaluate('''() => {
                        const l=$('sessionList'),row=l.querySelector('.session-date-body>.session-item');
                        const height=row.getBoundingClientRect().height+(parseFloat(getComputedStyle(row).marginBottom)||0);
                        return {prefix:l._sessionVirtualLayout.offsets[10],expectedPrefix:10*height,
                          previewCount:l.querySelectorAll('.session-search-preview').length,
                          cached:l._sessionVirtualLayout.measured.get('p0'),rows:l.querySelectorAll('.session-date-body>.session-item').length};
                    }''')
                    page.screenshot(path=str(args.output / f'{width}-previews-{transition}.png'))
                    failures = []
                    if abs(state['prefix'] - state['expectedPrefix']) > 1:
                        failures.append('offscreen preview measurement corrupts spacer heights')
                    if state['rows'] >= 80:
                        failures.append('unbounded DOM')
                    if after['rows'][0]['id'] != before['rows'][0]['id'] or abs(after['rows'][0]['y'] - before['rows'][0]['y']) > 1:
                        failures.append('preview transition moves top conversation/offset')
                    assert (state['previewCount'] > 0) == (transition == 'show')
                    results.append(dict(scene='previews-' + transition, width=width, before=before, after=after, state=state, failures=failures))
            if args.case == 'projects':
                page.evaluate('$("sessionList").style.height="520px"')
                for headers in [6, 40]:
                    page.evaluate('(n)=>projectScene(20,n)', headers)
                    assert page.locator('.project-chip').count() == 21
                    page.evaluate('_sessionVirtualScrollList.removeEventListener("scroll",_scheduleSessionVirtualizedRender);$("sessionList").scrollTop=596')
                    page.wait_for_timeout(50)
                    before = page.evaluate('visible()')
                    controls = page.locator('.project-bar').bounding_box()
                    page.screenshot(path=str(args.output / f'{width}-{headers}-projects-before.png'))
                    page.evaluate('_sessionVirtualScrollList.addEventListener("scroll",_scheduleSessionVirtualizedRender,{passive:true});_scheduleSessionVirtualizedRender()')
                    page.wait_for_timeout(100)
                    after = page.evaluate('visible()')
                    page.screenshot(path=str(args.output / f'{width}-{headers}-projects-after.png'))
                    failures = []
                    if (any(row not in after['rows'] for row in before['rows'])
                            or before['scrollTop'] != after['scrollTop']):
                        failures.append('grouped Compact scroll omits visible rows or changes offset')
                    if not after['rows'] or after['rows'][0]['y'] > before['rows'][0]['y'] + 1:
                        failures.append('blank band above first conversation')
                    if page.locator('.session-date-body>.session-item').count() >= 80:
                        failures.append('unbounded DOM')
                    page.evaluate('$("sessionList").scrollTop=$("sessionList").scrollHeight')
                    page.wait_for_timeout(100)
                    bottom = page.evaluate('visible()')
                    if not any(r['id'] == 'p199' for r in bottom['rows']):
                        failures.append('last conversation unreachable')
                    # Seeded filtering supplies rows; the production project control,
                    # headers/window/measurement and queued scheduler remain real.
                    stress = []
                    for search in ([] if args.before_ref else [False, True]):
                        page.evaluate('(s)=>{searchQueryRaw=s?"Conversation":"";$("sessionSearch").value=searchQueryRaw;}', search)
                        page.locator('.project-chip').nth(1).evaluate('(e)=>e.onclick({})')
                        page.wait_for_timeout(260)
                        assert page.locator('.session-date-body>.session-item').count() == 10
                        page.locator('.project-chip').first.evaluate('(e)=>e.onclick({})')
                        page.wait_for_timeout(100)
                        # Include a real empty header and collapsed body before rows.
                        page.evaluate('groups.unshift({label:"Empty",items:[]});_groupCollapsed[groups[1].label]=true;'
                                      '$("sessionList").scrollTop=1200;_scheduleSessionVirtualizedRender();'
                                      'window._sidebarDensity="detailed";$("sessionList").style.width="240px";')
                        page.wait_for_timeout(100)
                        state = page.evaluate('''() => {
                            const l=$('sessionList'),layout=l._sessionVirtualLayout,top=l.getBoundingClientRect().top;
                            return {key:layout.key,rows:l.querySelectorAll('.session-date-body>.session-item').length,
                              errors:[...l.querySelectorAll('.session-date-body>.session-item')].map(el=>{
                                const i=layout.rows.findIndex(row=>row.id===el.dataset.sid);
                                return Math.abs(el.getBoundingClientRect().top-top+l.scrollTop-layout.contentOffsets[i]);
                              })};
                        }''')
                        if state['rows'] >= 80 or max(state['errors'], default=0) > 1:
                            failures.append('pending layout/collapsed/empty header content geometry incorrect')
                        stress.append(dict(search=search, state=state))
                        page.evaluate('_groupCollapsed[groups[1].label]=false;searchQueryRaw="";$("sessionSearch").value="";'
                                      'window._sidebarDensity="compact";$("sessionList").style.width="'+str(180 if touch else 300)+'px";')
                    results.append(dict(scene='projects', width=width, headers=headers, controls=controls,
                                        before=before, after=after, bottom=bottom, stress=stress, failures=failures))
            context.close()
        browser.close()
    (args.output / 'report.json').write_text(json.dumps(dict(results=results, errors=errors), indent=2))
    print(json.dumps(dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors)))
    if errors or any(r['failures'] for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
