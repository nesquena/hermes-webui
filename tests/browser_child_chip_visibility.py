"""Real sidebar renderer/CSS clipping and accessible-name regression gate."""
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
function scene(own,reference,search,density,state){
  window._sidebarDensity=density;
  searchQueryRaw=search?'task':'';
  const parent={session_id:'parent',title:'Parent task with a long conversation title',message_count:3,
    _compression_segment_count:4,
    _lineage_segments:Array.from({length:4},(_,i)=>({session_id:'prior'+i,title:'Earlier turn '+i,updated_at:i+1})),
    has_unread:own.includes('unread'),attention:own.includes('approval')?{kind:'approval',count:1}:own.includes('clarify')?{kind:'clarify',count:1}:null};
  const child=(id,state)=>({session_id:id,title:id+' task',message_count:3,parent_session_id:'parent',
    relationship_type:'child_session',raw_source:'subagent',session_source:'other',
    is_streaming:state==='running',has_unread:state==='unread',
    attention:state==='approval'?{kind:'approval',count:1}:state==='clarify'?{kind:'clarify',count:1}:null,
    archived:reference,_lineage_root_id:reference?id:undefined});
  const children=state.split('+').map((state,i)=>child('child'+i,state));
  window.repaint=()=>{
    const result=renderFixture(reference?[parent]:[parent,...children],[parent,...children],false,'other');
    document.querySelector('#fixture').replaceChildren(result.element);
  };
  repaint();
  document.querySelectorAll('*').forEach(el=>{el.scrollLeft=0;});
  const chip=document.querySelector('.session-child-count'),mark=chip.querySelector('.session-child-count-state');
  const text=document.querySelector('.session-text'),clip=text.getBoundingClientRect();
  const fullyVisible=el=>{
    const r=el.getBoundingClientRect();
    const clips=[];
    for(let ancestor=el.parentElement;ancestor;ancestor=ancestor.parentElement) clips.push(ancestor);
    return r.width>0&&r.height>0&&clips.every(ancestor=>{
      const c=ancestor.getBoundingClientRect();
      return r.left>=c.left&&r.right<=c.right&&r.top>=c.top&&r.bottom<=c.bottom;
    })&&(()=>{
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      // The activity spinner deliberately has pointer-events:none.
      return hit===el||el.contains(hit)||(getComputedStyle(el).pointerEvents==='none'&&hit===el.parentElement);
    })();
  };
  const activity=document.querySelector('.session-child-activity-indicator');
  const pill=document.querySelector('.session-lineage-count');
  const labelFits=el=>{
    if(!el) return true;
    const r=el.getBoundingClientRect(),style=getComputedStyle(el);
    const range=document.createRange();range.selectNodeContents(el);
    const left=r.left+parseFloat(style.paddingLeft)+parseFloat(style.borderLeftWidth);
    const right=r.right-parseFloat(style.paddingRight)-parseFloat(style.borderRightWidth);
    // Range geometry measures the actual glyphs even when CSS paints an ellipsis.
    return el.scrollWidth<=el.clientWidth&&Array.from(range.getClientRects()).every(text=>
      text.left>=left-0.5&&text.right<=right+0.5&&text.top>=r.top&&text.bottom<=r.bottom);
  };
  return {visible:fullyVisible(mark),
    pill:!!pill,pillLabel:pill?.textContent,expectedPillLabel:t('session_meta_segments',4),
    pillVisible:!pill||fullyVisible(pill),pillLabelFits:labelFits(pill),
    pillWidth:pill?.getBoundingClientRect().width,
    titleWidth:document.querySelector('.session-title').getBoundingClientRect().width,
    chipWidth:chip.getBoundingClientRect().width,clipWidth:clip.width,
    aria:chip.getAttribute('aria-label'),tip:chip.title,
    expanded:chip.getAttribute('aria-expanded'),rows:document.querySelectorAll('.session-child-session').length,
    activity:!!activity,activityVisible:!activity||fullyVisible(activity),
    runningLabel:t('session_child_running'),unreadLabel:t('session_child_unread')};
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

    results = []
    interactions = []
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for viewport, touch in [(1280, False), (768, False), (390, True)]:
            context = browser.new_context(viewport={'width': viewport, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content('<main id="fixture" style="padding:8px;box-sizing:border-box;background:var(--sidebar)"></main>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=component_script(source('static/sessions.js')).replace("const animateRefresh=false, searchQueryRaw='';", "const animateRefresh=false;let searchQueryRaw='';"))
            page.add_script_tag(content=source('static/i18n.js'))
            for name in ['_sessionSearchRanges', '_appendHighlightedText']:
                match = re.search(r'^function ' + name + r'\(.*?^\}', source('static/sessions.js'), re.M | re.S)
                assert match, name
                page.add_script_tag(content=match.group())
            page.add_script_tag(content=SCENE)
            locales = page.evaluate('Object.keys(LOCALES)')
            for width in [180, 240, 300]:
                page.locator('#fixture').evaluate('(el,w)=>el.style.width=w+"px"', width)
                for locale in locales:
                    page.evaluate('locale=>setLocale(locale)', locale)
                    for own in ['idle', 'unread', 'approval', 'clarify', 'unread-approval', 'unread-clarify']:
                        for reference in [False, True]:
                            for search in [False, True]:
                                for density in ['compact', 'detailed']:
                                    for state in ['approval', 'clarify', 'running', 'unread', 'approval+running', 'clarify+running']:
                                        data = page.evaluate('args=>scene(...args)', [own, reference, search, density, state])
                                        failures = []
                                        if not data['visible'] or not data['activityVisible']:
                                            failures.append('status clipped before actionability scrolling')
                                        if data['titleWidth'] < 20:
                                            failures.append('title minimum lost')
                                        if data['pill'] != (density == 'detailed') or (data['pill'] and data['pillLabel'] != data['expectedPillLabel']):
                                            failures.append('real compression prior-turns pill missing or unexpected')
                                        if not data['pillVisible']:
                                            failures.append('prior-turns pill clipped')
                                        if not data['pillLabelFits']:
                                            failures.append('prior-turns count and localized cue do not fit')
                                        if data['activity'] != ('+' in state and 'running' in state and (reference or not search)):
                                            failures.append('collapsed activity projection missing or unexpected')
                                        if data['aria'] != data['tip'] or ('running' in state and data['runningLabel'] not in (data['aria'] or '')):
                                            failures.append('concurrent running missing from accessible name')
                                        if data['runningLabel'] == 'session_child_running' or data['unreadLabel'] == 'session_child_unread':
                                            failures.append('child locale keys missing')
                                        if not reference and data['expanded'] != ('true' if search else 'false'):
                                            failures.append('search expansion misreported')
                                        if reference and density == 'compact' and width == 300 and data['titleWidth'] < 80:
                                            failures.append('archived chip crowds title')
                                        results.append(dict(viewport=viewport, width=width, locale=locale, own=own, reference=reference, search=search, density=density, state=state, data=data, failures=failures))
                                        if locale == 'en' and own == 'approval' and not search and state == 'approval+running':
                                            page.screenshot(path=str(args.output / f'{viewport}-{width}-{density}-{"archived" if reference else "interactive"}.png'))
            # A failing historical layout already fails the gate; do not let
            # actionability scrolling or interactions replace its geometry evidence.
            interaction_widths = [] if args.before_ref and any(r['failures'] for r in results) else [180, 240, 300]
            for width in interaction_widths:
                page.locator('#fixture').evaluate('(el,w)=>el.style.width=w+"px"', width)
                for locale in locales:
                    page.evaluate('locale=>setLocale(locale)', locale)
                    for reference in [False, True]:
                        page.evaluate('_expandedLineageKeys.clear();opened.length=0')
                        data = page.evaluate('args=>scene(...args)', ['approval', reference, False, 'detailed', 'approval+running'])
                        pill = page.locator('.session-lineage-count')
                        if not data['pillVisible'] or not data['pillLabelFits']:
                            # Geometry failures are recorded by the complete matrix above.
                            continue
                        assert pill.get_attribute('role') == 'button'
                        assert pill.get_attribute('aria-expanded') == 'false'
                        assert pill.get_attribute('tabindex') == '0'
                        pill.focus()
                        page.keyboard.press('Enter')
                        assert pill.get_attribute('aria-expanded') == 'true'
                        assert page.locator('.session-lineage-segment').count() == 4
                        assert page.evaluate('opened.length') == 0
                        pill.focus()
                        page.keyboard.press('Space')
                        assert pill.get_attribute('aria-expanded') == 'false'
                        assert page.locator('.session-lineage-segment').count() == 0
                        (pill.tap if touch else pill.click)()
                        assert pill.get_attribute('aria-expanded') == 'true'
                        segment = page.locator('.session-lineage-segment').first
                        if touch:
                            segment.tap()
                        else:
                            segment.focus()
                            page.keyboard.press('Enter')
                        actual_opened = page.evaluate('opened')
                        assert actual_opened == [{'sid': 'prior3', 'options': {'skipLineageResolve': True}}], (viewport, width, locale, reference, actual_opened, page.evaluate('document.activeElement.outerHTML'))
                        interactions.append(dict(viewport=viewport, width=width, locale=locale, reference=reference, touch=touch))
            context.close()
        browser.close()
    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors, interactions=interactions, results=results)
    (args.output / 'results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ['cases', 'failures', 'errors']}))
    if report['failures'] or errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
