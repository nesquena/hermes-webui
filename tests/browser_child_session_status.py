"""Isolated production sidebar DOM/CSS gate; no server, Agent or credentials.

Run with a Playwright-enabled Python: python tests/browser_child_session_status.py
--output <artifact-directory>. --before records failures without aborting, for
before/after evidence against the exact PR head. Fixture seams replace navigation
and persistence, not the production attach/render functions, stylesheet or locale.
"""
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402

SKINS = ["default", "graphite", "codex", "terracotta", "github", "geist-contrast"]
SCENE = r"""
let sceneState='approval', sceneExpanded=false;
function repaint(){
  const parent={session_id:'parent',title:'Parent conversation with a long title',message_count:3,last_message_at:10};
  const child=(sid,kind)=>({session_id:sid,title:sid+' child with a long task description',
    parent_session_id:'parent',relationship_type:'child_session',raw_source:'subagent',
    session_source:kind,message_count:3,last_message_at:10,
    is_streaming:sceneState==='streaming',has_unread:sceneState==='unread',
    attention:['approval','clarify'].includes(sceneState)?{kind:sceneState,count:1}:null});
  const raw=[parent,child('fork','fork'),child('delegated','other')];
  // Preserve the production disclosure state across its rerender callback.
  sceneExpanded=_expandedChildSessionKeys.has('parent');
  const result=renderFixture(raw,raw,sceneExpanded,'parent');
  document.querySelector('#fixture').replaceChildren(result.element);
}
function scene(state,expanded){
  sceneState=state;
  _expandedChildSessionKeys.clear();
  if(expanded)_expandedChildSessionKeys.add('parent');
  repaint();
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results, errors, failures, screenshots = [], [], [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width, touch in [(1280, False), (768, True), (390, True), (1280, True), (768, False)]:
            context = browser.new_context(viewport={'width': width, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_content('<main id="fixture" style="width:300px;max-width:100%;padding:8px;box-sizing:border-box;background:var(--sidebar)"></main>')
            page.add_style_tag(content=(ROOT / 'static/style.css').read_text())
            page.add_script_tag(content=component_script())
            page.add_script_tag(content=(ROOT / 'static/i18n.js').read_text())
            page.add_script_tag(content=SCENE)
            for skin in SKINS:
                for dark in [False, True]:
                    page.evaluate('([skin,dark])=>{document.documentElement.dataset.skin=skin;document.documentElement.classList.toggle("dark",dark);setLocale("en");}', [skin, dark])
                    for state in ['approval', 'clarify', 'streaming', 'unread']:
                        for expanded in [False, True]:
                            page.evaluate('([state,expanded])=>scene(state,expanded)', [state, expanded])
                            data = page.evaluate(r"""()=>{
                              const chip=document.querySelector('.session-child-count');
                              const dot=chip.querySelector('.session-child-count-state');
                              const style=getComputedStyle(chip), ds=getComputedStyle(dot);
                              const token=name=>{const el=document.createElement('span');el.style.color=`var(--${name})`;document.body.appendChild(el);const c=getComputedStyle(el).color;el.remove();return c;};
                              const tint=name=>{const el=document.createElement('span');el.style.backgroundColor=`color-mix(in srgb,var(--${name}) 16%,var(--surface))`;document.body.appendChild(el);const c=getComputedStyle(el).backgroundColor;el.remove();return c;};
                              const children=[...document.querySelectorAll('.session-child-session')];
                              const parentDot=document.querySelector('.session-item > .session-attention-indicator');
                              const bounds=document.querySelector('#fixture').getBoundingClientRect();
                              return {chipClass:chip.className,title:chip.title,aria:chip.getAttribute('aria-label'),
                                color:style.color,background:style.backgroundColor,dotColor:ds.color,
                                error:token('error'),warning:token('warning'),accent:token('accent'),approvalTint:tint('error'),clarifyTint:tint('warning'),
                                ownDot:parentDot.className,children:children.map(e=>{const d=e.querySelector('.session-child-session-state');return {kind:e.className,height:e.getBoundingClientRect().height,width:getComputedStyle(d).width,indicatorHeight:getComputedStyle(d).height,color:getComputedStyle(d).color};}),
                                clipped:[chip,dot,...children,...document.querySelectorAll('.session-child-session-state,.session-actions-trigger')].some(e=>{const r=e.getBoundingClientRect();return r.left<bounds.left||r.right>bounds.right||r.top<0||r.bottom>innerHeight;})};
                            }""")
                            hover = page.locator('.session-child-count')
                            hover.hover()
                            data['hoverColor'] = hover.evaluate('(el)=>getComputedStyle(el).color')
                            data['hoverBackground'] = hover.evaluate('(el)=>getComputedStyle(el).backgroundColor')
                            reasons = []
                            expected = data['error' if state == 'approval' else 'warning' if state == 'clarify' else 'accent']
                            if data['dotColor'] != expected:
                                reasons.append('active parent neutralizes indicator color')
                            if state in ('approval', 'clarify'):
                                if data['color'] != expected or data['hoverColor'] != expected or f'is-attention-{state}' not in data['chipClass']:
                                    reasons.append('blocking pill lacks semantic tint')
                                if data['background'] != data[state + 'Tint'] or data['hoverBackground'] != data[state + 'Tint']:
                                    reasons.append('blocking pill background lacks semantic tint')
                                lead = 'Waiting for permission decision' if state == 'approval' else 'Waiting for your answer'
                                if not data['title'].startswith(lead + ' · ') or data['title'].count(' · ') != 1 or ' — ' in data['title']:
                                    reasons.append('tooltip does not lead with state and one separator')
                            if data['aria'] != data['title']:
                                reasons.append('aria/title mismatch')
                            if any(c in data['ownDot'].split() for c in ['is-streaming', 'is-unread', 'is-attention-approval', 'is-attention-clarify']):
                                reasons.append('child state leaked into own dot')
                            for child in data['children']:
                                if child['color'] != expected:
                                    reasons.append('active parent neutralizes child row indicator color')
                                if child['width'] != '14px' or child['indicatorHeight'] != '14px':
                                    reasons.append('unequal child indicator size')
                                if 'session-child-session-delegated' in child['kind']:
                                    if (touch or width <= 768) and child['height'] < 44:
                                        reasons.append('delegated touch target below 44px')
                                    if not touch and width > 768 and child['height'] >= 44:
                                        reasons.append('desktop delegated row unnecessarily enlarged')
                            if data['clipped']:
                                reasons.append('clipping outside sidebar/viewport')
                            row = {'width': width, 'touch': touch, 'skin': skin, 'dark': dark, 'state': state, 'expanded': expanded, **data, 'failures': reasons}
                            results.append(row)
                            failures.extend([{k: row[k] for k in ['width', 'touch', 'skin', 'dark', 'state', 'expanded']} | {'reason': r} for r in reasons])
                            if width != 1280 or not touch:
                                if (skin == 'github' and dark and state in ('approval', 'streaming')) or (width == 1280 and state in ('approval', 'clarify') and expanded):
                                    name=f'{width}-{skin}-{"dark" if dark else "light"}-{state}-{"expanded" if expanded else "collapsed"}.png'
                                    page.screenshot(path=str(args.output / name))
                                    screenshots.append(name)
            # Real Playwright keyboard and touchscreen input, through production handlers.
            page.evaluate("scene('approval',false)")
            chip = page.locator('.session-child-count')
            chip.focus()
            page.keyboard.press('Enter')
            assert page.locator('.session-child-session').count() == 2
            page.locator('.session-child-count').focus()
            page.keyboard.press('Space')
            assert page.locator('.session-child-session').count() == 0
            if touch:
                page.locator('.session-child-count').tap()
                page.locator('.session-child-session-delegated').tap()
            else:
                page.locator('.session-child-count').click()
                page.locator('.session-child-session-delegated').focus()
                page.keyboard.press('Enter')
            assert page.evaluate('opened') == [{'sid': 'delegated', 'options': {'skipLineageResolve': True}}]
            context.close()
        # Reference-only chips have no disclosure/count and must fit SIDEBAR_MIN.
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<main id="fixture" style="padding:8px;box-sizing:border-box;background:var(--sidebar)"></main>')
        page.add_style_tag(content=(ROOT / 'static/style.css').read_text())
        page.add_script_tag(content=component_script())
        page.add_script_tag(content=(ROOT / 'static/i18n.js').read_text())
        locales = page.evaluate('Object.keys(LOCALES)')
        for sidebar_width in [180, 240, 300, 360]:
            page.locator('#fixture').evaluate('(el,width)=>el.style.width=width+"px"', sidebar_width)
            for locale in locales:
                for skin in SKINS:
                    for dark in [False, True]:
                        for state in ['approval', 'clarify', 'streaming', 'unread']:
                            data = page.evaluate(r"""([locale,skin,dark,state])=>{
                              document.documentElement.dataset.skin=skin;
                              document.documentElement.classList.toggle('dark',dark);
                              setLocale(locale);
                              const parent={session_id:'parent',title:'Parent conversation with a long title',message_count:3,last_message_at:10};
                              const reference={session_id:'archived',archived:true,parent_session_id:'parent',
                                _lineage_root_id:'archived',relationship_type:'child_session',message_count:3,
                                is_streaming:state==='streaming',has_unread:state==='unread',
                                attention:['approval','clarify'].includes(state)?{kind:state,count:1}:null};
                              const result=renderFixture([parent],[parent,reference],false,'parent');
                              document.querySelector('#fixture').replaceChildren(result.element);
                              for(const el of [document.scrollingElement,document.querySelector('#fixture'),result.element,...result.element.querySelectorAll('*')]) el.scrollLeft=0;
                              const chip=document.querySelector('.session-child-count'), dot=chip.querySelector('.session-child-count-state');
                              const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
                              const box=rect(result.element), mark=rect(dot), pill=rect(chip), titleRow=rect(chip.parentElement);
                              const ds=getComputedStyle(dot);
                              const probe=document.createElement('span');probe.style.color=`var(--${state==='approval'?'error':state==='clarify'?'warning':'accent'})`;
                              document.body.appendChild(probe);const expectedColor=getComputedStyle(probe).color;probe.remove();
                              return {box,mark,pill,titleRow,title:chip.title,aria:chip.getAttribute('aria-label'),
                                label:t('session_child_archived'),text:chip.textContent,dotColor:ds.color,expectedColor,
                                display:ds.display,visibility:ds.visibility,opacity:ds.opacity,
                                role:chip.getAttribute('role'),tabindex:chip.getAttribute('tabindex'),
                                ownDot:result.element.querySelector(':scope > .session-attention-indicator').className,
                                children:result.element.querySelectorAll('.session-child-session').length};
                            }""", [locale, skin, dark, state])
                            reasons = []
                            for name in ['mark', 'pill']:
                                r = data[name]
                                for container in ['box', 'titleRow']:
                                    b = data[container]
                                    if r['left'] < b['left'] - 0.5 or r['right'] > b['right'] + 0.5 or r['top'] < b['top'] - 0.5 or r['bottom'] > b['bottom'] + 0.5:
                                        reasons.append(f'{name} clipped outside {container}')
                            if data['mark']['width'] != 10 or data['mark']['height'] != 10 or data['display'] == 'none' or data['visibility'] != 'visible' or data['opacity'] == '0':
                                reasons.append('status mark not fully visible at 10px')
                            if data['dotColor'] != data['expectedColor']:
                                reasons.append('active parent neutralizes archived indicator color')
                            if not data['title'].endswith(' · ' + data['label']) or data['text'] != data['label'] or data['aria'] not in (None, data['title']):
                                reasons.append('localized label or full tooltip/aria lost')
                            if data['role'] is not None or data['tabindex'] is not None or data['children']:
                                reasons.append('reference-only chip became navigable')
                            if any(c.startswith('is-') for c in data['ownDot'].split()):
                                reasons.append('reference state leaked into own dot')
                            if locale in ['en', 'pl'] and skin == 'github' and state == 'approval':
                                name = f'archived-{sidebar_width}-{locale}-{skin}-{"dark" if dark else "light"}.png'
                                page.screenshot(path=str(args.output / name))
                                screenshots.append(name)
                            page.locator('.session-child-count').click()
                            if page.evaluate('opened.length') or page.locator('.session-child-session').count():
                                reasons.append('reference click navigates or expands')
                            row = {'scene': 'reference-only', 'sidebar_width': sidebar_width, 'locale': locale, 'skin': skin, 'dark': dark, 'state': state, **data, 'failures': reasons}
                            results.append(row)
                            failures.extend({k: row[k] for k in ['scene', 'sidebar_width', 'locale', 'skin', 'dark', 'state']} | {'reason': r} for r in reasons)
        context.close()
        browser.close()
    report = {'cases': len(results), 'screenshots': sorted(set(screenshots)), 'errors': errors, 'failures': failures, 'results': results}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'cases': len(results), 'screenshots': len(set(screenshots)), 'errors': errors, 'failures': len(failures), 'output': str(args.output)}))
    if not args.before:
        assert not errors and not failures, failures[:5]


if __name__ == '__main__':
    main()
