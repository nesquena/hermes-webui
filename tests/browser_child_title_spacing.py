"""Production sidebar geometry for badge-heavy and childless compressed rows."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402

SCENE = r"""
const _loadingSessionId=null;
function li(){return '<svg width="12" height="12"></svg>';}
function _sessionTitleForForkParent(){return 'Original parent';}
function _truncatedSessionId(sid){return sid;}
function _sessionForkTooltip(parent){return parent;}
_allProjects.push({project_id:'project',name:'Project',color:'#abc'});
function scene(children,badges,own,density){
  window._sidebarDensity=density;
  const parent={session_id:'parent',title:'Parent task with a long title',message_count:3,
    _compression_segment_count:4,
    _lineage_segments:Array.from({length:4},(_,i)=>({session_id:'prior'+i,title:'Earlier turn '+i,updated_at:i+1})),
    has_unread:own==='unread',attention:own==='approval'?{kind:'approval',count:1}:null,
    parent_session_id:badges&1?'original':null,worktree_path:badges&2?'/fixture/worktree':null,
    project_id:badges&4?'project':null};
  const child=(sid)=>({session_id:sid,title:sid+' task',message_count:3,
    parent_session_id:'parent',relationship_type:'child_session',session_source:'other',raw_source:'subagent',
    attention:sid==='approval'?{kind:'approval',count:1}:null,is_streaming:sid==='running'});
  const raw=children?[parent,child('approval'),child('running')]:[parent];
  document.querySelector('#fixture').replaceChildren(renderFixture(raw,raw,false,'other').element);
  document.querySelectorAll('*').forEach(e=>e.scrollLeft=0);
  const text=document.querySelector('.session-text'), clip=text.getBoundingClientRect();
  const mark=document.querySelector('.session-child-count-state'),pill=document.querySelector('.session-lineage-count');
  const visible=e=>{
    if(!e)return false;
    const r=e.getBoundingClientRect(), hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
    return r.left>=clip.left&&r.right<=clip.right&&r.width>0&&(hit===e||e.contains(hit));
  };
  const title=document.querySelector('.session-title');
  return {markVisible:visible(mark),pillVisible:visible(pill),
    titleMinWidth:getComputedStyle(title).minWidth,titleWidth:title.getBoundingClientRect().width,
    pillRect:pill?{left:pill.getBoundingClientRect().left,right:pill.getBoundingClientRect().right}:null,
    badges:document.querySelectorAll('.session-branch-indicator,.session-worktree-indicator,.session-project-dot').length};
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before-ref')
    parser.add_argument('--baseline-ref', default='origin/master', help='Available master revision for childless geometry parity')
    args = parser.parse_args()
    baseline_ref = subprocess.run(['git', 'rev-parse', '--verify', f'{args.baseline_ref}^{{commit}}'], cwd=ROOT, text=True, capture_output=True)
    if baseline_ref.returncode:
        parser.error(f'Baseline {args.baseline_ref!r} is unavailable; fetch it or pass --baseline-ref <available master revision>')
    args.baseline_ref = baseline_ref.stdout.strip()
    args.output.mkdir(parents=True, exist_ok=True)

    def source(path):
        if args.before_ref:
            return subprocess.check_output(['git', 'show', f'{args.before_ref}:{path}'], cwd=ROOT, text=True)
        return (ROOT / path).read_text()

    results, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for viewport, touch in [(1280, False), (390, True)]:
            context = browser.new_context(viewport={'width': viewport, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content('<main id="fixture" style="padding:8px;box-sizing:border-box"></main>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=component_script(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.add_script_tag(content=SCENE)
            baseline = context.new_page()
            baseline.set_content('<main id="fixture" style="padding:8px;box-sizing:border-box"></main>')
            master = args.baseline_ref
            baseline.add_style_tag(content=subprocess.check_output(['git', 'show', f'{master}:static/style.css'], cwd=ROOT, text=True))
            baseline.add_script_tag(content=component_script(subprocess.check_output(['git', 'show', f'{master}:static/sessions.js'], cwd=ROOT, text=True)))
            baseline.add_script_tag(content=source('static/i18n.js'))
            baseline.add_script_tag(content=SCENE)
            locales = page.evaluate('Object.keys(LOCALES)')
            for width in [180, 210, 240]:
                page.locator('#fixture').evaluate('(e,w)=>e.style.width=w+"px"', width)
                baseline.locator('#fixture').evaluate('(e,w)=>e.style.width=w+"px"', width)
                for locale in locales:
                    page.evaluate('locale=>setLocale(locale)', locale)
                    baseline.evaluate('locale=>setLocale(locale)', locale)
                    for children in [False, True]:
                        for badges in range(8) if children else [0]:
                            for own in ['idle', 'unread', 'approval']:
                                for density in ['compact', 'detailed'] if children else ['detailed']:
                                    data = page.evaluate('args=>scene(...args)', [children, badges, own, density])
                                    failures = []
                                    if children and not data['markVisible']:
                                        failures.append('badge-heavy child status clipped')
                                    if children and data['titleWidth'] < 24:
                                        failures.append('child title floor missing')
                                    if not children:
                                        master_data = baseline.evaluate('args=>scene(...args)', [children, badges, own, density])
                                        if any(data[key] != master_data[key] for key in ['titleMinWidth', 'titleWidth', 'pillRect', 'pillVisible']):
                                            failures.append('childless compressed row differs from master geometry')
                                        data['master'] = master_data
                                    if data['badges'] != badges.bit_count():
                                        failures.append('fixture did not render requested badges')
                                    results.append(dict(viewport=viewport, width=width, locale=locale, children=children, badges=badges, own=own, density=density, data=data, failures=failures))
            context.close()
        browser.close()
    failures = [r for r in results if r['failures']]
    (args.output / 'report.json').write_text(json.dumps(dict(baseline_ref=args.baseline_ref, cases=len(results), failures=len(failures), errors=errors, results=results), indent=2))
    print(json.dumps(dict(cases=len(results), failures=len(failures), errors=errors)))
    if (failures or errors) and not args.before_ref:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
