"""Isolated production attach/render/CSS activity gate; no server or live state."""
import argparse
import json
import subprocess
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402

SCENE = r"""
let own='idle', running=true, reference=false;
const _loadingSessionId=null;
function repaint(){
  const parent={session_id:'parent',title:'Parent task with a long conversation title',message_count:3,
    is_streaming:own==='streaming',has_unread:own==='unread',
    attention:['approval','clarify'].includes(own)?{kind:own,count:1}:null};
  const child=kind=>({session_id:kind,title:kind+' task',message_count:3,is_streaming:running,
    parent_session_id:'parent',relationship_type:'child_session',raw_source:'subagent',session_source:kind});
  const children=reference?[{...child('archived'),archived:true,_lineage_root_id:'archived'}]:[child('fork'),child('delegated')];
  const raw=reference?[parent]:[parent,...children];
  const expanded=_expandedChildSessionKeys.has('parent');
  const result=renderFixture(raw,[parent,...children],expanded,activeSidForSidebar);
  document.querySelector('#fixture').replaceChildren(result.element);
}
function scene(state,ref,active){
  own=state; reference=ref; running=true; activeSidForSidebar=active;
  _expandedChildSessionKeys.clear(); repaint();
}
function measure(){
  const activity=document.querySelector('.session-child-activity-indicator');
  const dot=document.querySelector('.session-item > .session-attention-indicator');
  const pseudo=el=>{const s=getComputedStyle(el,'::before');return {
    animation:s.animationName,background:s.backgroundColor,border:s.borderTopStyle,
    borderWidth:s.borderTopWidth,width:s.width,height:s.height};};
  document.querySelectorAll('*').forEach(el=>{el.scrollLeft=0;});
  const parent=document.querySelector('.session-text').getBoundingClientRect();
  const visible=el=>{const r=el.getBoundingClientRect();return r.left>=parent.left&&r.right<=parent.right&&r.width>0&&getComputedStyle(el).visibility==='visible';};
  return {activity:activity?{...pseudo(activity),visible:visible(activity)}:null,
    ownClass:dot.className,own:pseudo(dot),
    children:[...document.querySelectorAll('.session-child-session-state')].map(pseudo),
    chip:[...document.querySelectorAll('.session-child-count-state')].map(pseudo),
    chipVisible:[...document.querySelectorAll('.session-child-count-state')].every(visible),
    opened:[...opened]};
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before-ref')
    parser.add_argument('--sidebar-width', type=int, default=240)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    def source(path):
        if args.before_ref:
            return subprocess.check_output(['git', 'show', f'{args.before_ref}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()
    results, failures, errors = [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width, touch in [(1280, False), (768, False), (390, True)]:
            context = browser.new_context(viewport={'width': width, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content(f'<main id="fixture" style="width:{args.sidebar_width}px;padding:8px;box-sizing:border-box;background:var(--sidebar)"></main>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=component_script(source('static/sessions.js')))
            page.add_script_tag(content=SCENE)
            for active in ['parent', 'other']:
                for own in ['idle', 'unread', 'approval', 'clarify', 'streaming']:
                    for reference in [False, True]:
                        page.evaluate('([own,ref,active])=>scene(own,ref,active)', [own, reference, active])
                        stages = ['collapsed', 'settled'] if reference else ['collapsed', 'expanded', 'recollapsed', 'settled']
                        for stage in stages:
                            if stage in ['expanded', 'recollapsed']:
                                chip = page.locator('.session-child-count')
                                if touch:
                                    chip.tap()
                                else:
                                    chip.focus()
                                    chip.press('Enter' if stage == 'expanded' else 'Space')
                            if stage == 'settled':
                                page.evaluate('running=false;repaint()')
                            # Move away from the row so hover does not hide its own dot.
                            page.mouse.move(width-1, 799)
                            data = page.evaluate('measure()')
                            reasons = []
                            if data['activity']:
                                reasons.append('running-only chip duplicates title-row activity')
                            if stage != 'settled' and (len(data['chip']) != 1 or data['chip'][0]['animation'] != 'spin'):
                                reasons.append('running-only chip must show its CSS spinner')
                            if own in ['approval', 'clarify'] or (own == 'unread' and active == 'other'):
                                if data['own']['animation'] != 'none' or data['own']['background'] == 'rgba(0, 0, 0, 0)':
                                    reasons.append('own dot lost to activity')
                            if ('is-streaming' in data['ownClass']) != (own == 'streaming'):
                                reasons.append('own spinner expansion independence')
                            if stage == 'expanded' and (len(data['children']) != 2 or any(c['animation'] != 'spin' for c in data['children'])):
                                reasons.append('expanded child spinners')
                            if not data['chipVisible']:
                                reasons.append('child chip status clipped')
                            case = {'width': width, 'active': active, 'own': own, 'reference': reference, 'stage': stage, 'data': data, 'failures': reasons}
                            results.append(case)
                            if reasons:
                                failures.append(case)
                            if active == 'other' and own in ['idle', 'approval'] and not reference:
                                page.screenshot(path=str(args.output / f'{width}-{own}-{stage}.png'))
            context.close()
        browser.close()
    report = {'cases': len(results), 'failures': len(failures), 'errors': errors, 'results': results}
    (args.output / 'results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'cases': len(results), 'failures': len(failures), 'errors': errors}))
    if (failures or errors) and not args.before_ref:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
