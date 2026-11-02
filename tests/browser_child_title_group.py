"""Real renderer/CSS: child status fits beside an approval parent's title group."""
import argparse
import itertools
import json
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402
from tests.browser_child_title_spacing import SCENE  # noqa: E402

PROBE = r"""
function probe(selected, badges, state, density) {
  scene(true,badges,'approval',density);
  const raw=fixtureSessions.filter(s=>s.session_id==='parent'||
    (state==='concurrent'||s.session_id===state));
  document.querySelector('#fixture').replaceChildren(renderFixture(raw,raw,false,selected?'parent':'other').element);
  document.querySelectorAll('*').forEach(e=>e.scrollLeft=0);
  const row=document.querySelector('.session-item');
  const rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
  const visible=e=>{
    const r=rect(e);
    if(r.width<=0||r.height<=0||getComputedStyle(e).visibility!=='visible')return false;
    for(let a=e.parentElement;a;a=a.parentElement){
      const s=getComputedStyle(a),c=rect(a);
      if(/hidden|clip|auto|scroll/.test(s.overflowX)&&(r.left<c.left-0.5||r.right>c.right+0.5))return false;
      if(/hidden|clip|auto|scroll/.test(s.overflowY)&&(r.top<c.top-0.5||r.bottom>c.bottom+0.5))return false;
    }
    const hit=document.elementFromPoint((r.left+r.right)/2,(r.top+r.bottom)/2);
    return hit===e||e.contains(hit)||(getComputedStyle(e).pointerEvents==='none'&&hit===e.parentElement);
  };
  const chip=row.querySelector('.session-child-count');
  const marks=Array.from(chip.querySelectorAll('.session-child-count-state,.session-child-activity-indicator'));
  const label=chip.querySelector('.session-child-count-label');
  const range=document.createRange();range.selectNodeContents(label);
  const glyph=range.getBoundingClientRect(),labelBox=rect(label);
  return {
    labelFits:visible(label)&&glyph.width>0&&glyph.left>=labelBox.left-0.5&&glyph.right<=labelBox.right+0.5,
    parentApproval:row.classList.contains('attention-approval'),
    ownDot:!!row.querySelector(':scope > .session-attention-indicator.is-attention-approval'),
    selected:row.classList.contains('active'),
    badges:row.querySelectorAll('.session-branch-indicator,.session-worktree-indicator,.session-project-dot').length,
    title:rect(row.querySelector('.session-title')),group:rect(row.querySelector('.session-title-group')),
    chip:rect(chip),marks:marks.map(e=>({classes:e.className,visible:visible(e),rect:rect(e)})),
    label:chip.querySelector('.session-child-count-label').textContent,
    chipVisible:visible(chip),aria:chip.getAttribute('aria-label')
  };
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before-ref', help='Exact integration revision without the CSS repair')
    args = parser.parse_args()
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
            page.set_content('<main id="fixture" style="padding:8px;box-sizing:border-box;background:var(--sidebar)"></main>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=component_script(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.add_script_tag(content=SCENE + PROBE)
            matrix = itertools.product([180, 210, 300], ['default', 'graphite', 'codex', 'terracotta', 'github', 'geist-contrast'],
                                       [False, True], ['compact', 'detailed'], [False, True], [0, 7],
                                       ['approval', 'running', 'concurrent'])
            for width, skin, dark, density, selected, badges, state in matrix:
                page.evaluate('([w,skin,dark])=>{document.querySelector("#fixture").style.width=w+"px";document.documentElement.dataset.skin=skin;document.documentElement.classList.toggle("dark",dark);}', [width, skin, dark])
                data = page.evaluate('args=>probe(...args)', [selected, badges, state, density])
                failures = []
                if not data['parentApproval'] or not data['ownDot'] or data['selected'] != selected:
                    failures.append('parent-own approval/selection fixture missing')
                if data['badges'] != badges.bit_count():
                    failures.append('requested badges missing')
                if len(data['marks']) != (2 if state == 'concurrent' else 1):
                    failures.append('requested child status missing')
                if not data['chipVisible'] or not all(mark['visible'] for mark in data['marks']):
                    failures.append('child chip/status clipped before actionability scrolling')
                if data['title']['width'] < 23.5:
                    failures.append('title glyph floor lost')
                if not data['labelFits'] or data['label'] != ('2' if state == 'concurrent' else '1') or not data['aria']:
                    failures.append('complete child numeral/accessible description lost')
                results.append(dict(viewport=viewport, width=width, skin=skin, dark=dark, density=density,
                                    selected=selected, badges=badges, state=state, data=data, failures=failures))
                if skin == 'graphite' and badges == 7 and state == 'concurrent':
                    page.screenshot(path=str(args.output / f'{viewport}-{width}-{density}-{"dark" if dark else "light"}-{"selected" if selected else "idle"}.png'))
            context.close()
        browser.close()
    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors, results=results)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ['cases', 'failures', 'errors']}))
    if report['failures'] or errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
