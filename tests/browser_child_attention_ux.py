"""Real sidebar renderer/CSS regression for concurrent child activity and row tint."""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402

SCENE = r"""
const _loadingSessionId=null;
let state='running', reference=false, selected='other', own='unread';
function repaint(){
  const parent={session_id:'parent',title:'Parent task with a long title',message_count:3,
    has_unread:own==='unread',is_streaming:own==='streaming'};
  const child=(sid,kind)=>({session_id:sid,title:sid+' task',message_count:3,
    parent_session_id:'parent',relationship_type:'child_session',raw_source:'subagent',session_source:kind,
    is_streaming:sid==='running',has_unread:sid==='waiting'&&state==='unread',
    attention:sid==='waiting'&&['approval','clarify','generic'].includes(state)?{kind:state,count:1}:null});
  const children=[child('running','fork'),child('waiting','other'),child('waiting-fork','fork')];
  children[2].attention=children[1].attention;children[2].has_unread=children[1].has_unread;
  if(reference) children.forEach(c=>{c.archived=true;c._lineage_root_id=c.session_id;});
  const raw=reference?[parent]:[parent,...children];
  const result=renderFixture(raw,[parent,...children],_expandedChildSessionKeys.has('parent'),selected);
  document.querySelector('#fixture').replaceChildren(result.element);
}
function scene(s,ref,active,parentOwn){
  state=s;reference=ref;selected=active;own=parentOwn;searchQueryRaw='';
  _expandedChildSessionKeys.clear();repaint();
}
function measure(){
  document.querySelectorAll('*').forEach(e=>e.scrollLeft=0);
  const root=document.querySelector('.session-item'), chip=root.querySelector('.session-child-count');
  const pseudo=e=>{const s=getComputedStyle(e,'::before');return {animation:s.animationName,background:s.backgroundColor};};
  const tint=document.createElement('div');tint.className='session-item needs-attention'+(state==='approval'?' attention-approval':'');
  document.body.appendChild(tint);const ts=getComputedStyle(tint), expectedBackground=ts.backgroundColor;
  const token=document.createElement('span');token.style.color=`var(--${state==='approval'?'error':'warning'})`;
  document.body.appendChild(token);const expectedAccent=getComputedStyle(token).color;token.remove();tint.remove();
  // A neutral selected child is the selection reference, not a fixed paint
  // string: attention must not override selection in either theme.
  const selection=document.createElement('button');selection.className='session-child-session active';
  root.appendChild(selection);const expectedSelection=getComputedStyle(selection).backgroundColor;selection.remove();
  const mark=chip.querySelector('.session-child-count-state'), r=mark.getBoundingClientRect();
  const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
  return {activity:root.querySelectorAll('.session-child-activity-indicator').length,
    activityContained:[...root.querySelectorAll('.session-child-activity-indicator')].every(e=>chip.contains(e)),
    activityStates:[...root.querySelectorAll('.session-child-activity-indicator')].map(pseudo),
    chip:pseudo(mark),markClass:mark.className,aria:chip.getAttribute('aria-label'),expanded:chip.getAttribute('aria-expanded'),
    markVisible:!!hit&&(hit===mark||mark.contains(hit)),own:pseudo(root.querySelector(':scope > .session-attention-indicator')),
    expectedBackground,expectedSelection,expectedAccent,
    children:[...root.querySelectorAll('.session-child-session')].map(e=>({className:e.className,
      background:getComputedStyle(e).backgroundColor,shadow:getComputedStyle(e).boxShadow,
      color:getComputedStyle(e.querySelector('.session-child-session-state')).color,
      selectionAlpha:(()=>{const probe=document.createElement('canvas').getContext('2d');
      probe.fillStyle=getComputedStyle(e).backgroundColor;probe.fillRect(0,0,1,1);
      return probe.getImageData(0,0,1,1).data[3]/255;})(),
    state:pseudo(e.querySelector('.session-child-session-state'))}))};
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
        for width, touch in [(1280, False), (390, True)]:
            context = browser.new_context(viewport={'width': width, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content('<main id="fixture" style="width:180px;padding:8px;box-sizing:border-box;background:var(--sidebar)"></main>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=component_script(source('static/sessions.js')).replace(
                "const animateRefresh=false, searchQueryRaw='';", "const animateRefresh=false;let searchQueryRaw='';"))
            page.add_script_tag(content=source('static/i18n.js'))
            for name in ['_sessionSearchRanges', '_appendHighlightedText']:
                match = re.search(r'^function ' + name + r'\(.*?^\}', source('static/sessions.js'), re.M | re.S)
                assert match, name
                page.add_script_tag(content=match.group())
            page.add_script_tag(content=SCENE)
            for skin in ['default', 'graphite', 'codex', 'terracotta', 'github', 'geist-contrast']:
                for dark in [False, True]:
                    page.evaluate('([skin,dark])=>{document.documentElement.dataset.skin=skin;document.documentElement.classList.toggle("dark",dark);setLocale("en");}', [skin, dark])
                    for state in ['running', 'approval', 'clarify', 'generic', 'unread']:
                        for reference in [False, True]:
                            for selected in ['parent', 'waiting', 'waiting-fork', 'other']:
                                for own in ['unread', 'streaming']:
                                    page.evaluate('args=>scene(...args)', [state, reference, selected, own])
                                    stages = ['collapsed'] if reference else ['collapsed', 'expanded', 'recollapsed', 'search']
                                    for stage in stages:
                                        if stage in ['expanded', 'recollapsed']:
                                            chip = page.locator('.session-child-count')
                                            if touch:
                                                chip.tap()
                                            else:
                                                chip.focus()
                                                chip.press('Enter')
                                        if stage == 'search':
                                            page.evaluate("searchQueryRaw='task';repaint()")
                                        page.mouse.move(width-1, 799)
                                        data = page.evaluate('measure()')
                                        attention = state in ['approval', 'clarify', 'generic']
                                        expanded = stage in ['expanded', 'search']
                                        expected_activity = int(attention and not expanded)
                                        failures = []
                                        if data['activity'] != expected_activity or not data['activityContained']:
                                            failures.append('concurrent child activity must belong to attention chip')
                                        if any(a['animation'] != 'spin' for a in data['activityStates']):
                                            failures.append('supplemental activity must be a CSS spinner')
                                        if not attention and data['chip']['animation'] != 'spin':
                                            failures.append('running chip must keep its single spinner')
                                        if own == 'unread' and selected == 'other' and (data['own']['animation'] != 'none' or data['own']['background'] == 'rgba(0, 0, 0, 0)'):
                                            failures.append('parent unread hidden by child activity')
                                        if own == 'streaming' and data['own']['animation'] != 'spin':
                                            failures.append('parent own running lost')
                                        if state == 'unread' and 'Unread child completion' not in data['aria']:
                                            failures.append('concurrent child unread missing from accessible projection')
                                        if expanded and data['expanded'] != 'true':
                                            failures.append('search/disclosure expansion mismatch')
                                        for child in data['children']:
                                            if 'needs-attention' in child['className']:
                                                active = 'active' in child['className'].split()
                                                expected_background = data['expectedSelection'] if active else data['expectedBackground']
                                                if child['background'] != expected_background:
                                                    failures.append('child attention must use selection wash when active, parent tint when idle')
                                                # Independent visibility floor, not sampled from the
                                                # selected reference rule. Contrast is additionally
                                                # exercised by browser_child_finishing_ux.py.
                                                if active and child['selectionAlpha'] < 0.04:
                                                    failures.append('selected child wash is transparent or negligible')
                                                if active and child['background'] == data['expectedBackground']:
                                                    failures.append('child selection indistinguishable from idle attention tint')
                                                if data['expectedAccent'] not in child['shadow'] or child['color'] != data['expectedAccent']:
                                                    failures.append('child semantic attention bar/mark lost')
                                        if not data['markVisible']:
                                            failures.append('chip mark fails hit test')
                                        results.append(dict(width=width, skin=skin, dark=dark, state=state, reference=reference, selected=selected, own=own, stage=stage, data=data, failures=failures))
                                        if skin == 'github' and selected == 'other' and own == 'unread' and not reference and state in ['running', 'approval'] and stage in ['collapsed', 'expanded']:
                                            page.screenshot(path=str(args.output / f'{width}-{dark}-{state}-{stage}.png'))
            context.close()
        browser.close()
    failures = [r for r in results if r['failures']]
    (args.output / 'report.json').write_text(json.dumps(dict(cases=len(results), failures=len(failures), errors=errors, results=results), indent=2))
    print(json.dumps(dict(cases=len(results), failures=len(failures), errors=errors)))
    if (failures or errors) and not args.before_ref:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
