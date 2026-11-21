"""Real renderer/CSS: child activity ownership and primary title glyph fit."""
import argparse
import json
import subprocess
import sys
from itertools import product
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._sidebar_child_status_helpers import ROOT, component_script  # noqa: E402

SCENE = r"""
const _loadingSessionId=null;
let currentOwn='unread', childMode='concurrent', currentDensity='compact', badgeHeavy=false;
let titleText='Sidebar authentication investigation';
function li(){return '<svg width="12" height="12"></svg>';}
function _sessionTitleForForkParent(){return 'Original parent';}
function _truncatedSessionId(sid){return sid;}
function _sessionForkTooltip(parent){return parent;}
_allProjects.push({project_id:'project',name:'Project',color:'#abc'});
function repaint(){scene(currentOwn,_expandedChildSessionKeys.has('parent'),activeSidForSidebar,currentDensity);}
function scene(own,expanded=false,selected='other',density='compact'){
  currentDensity=density;
  currentOwn=own;
  window._sidebarDensity=density;
  const parent={session_id:'parent',title:titleText,message_count:3,
    has_unread:own==='unread',is_streaming:own==='streaming',
    parent_session_id:badgeHeavy?'original':null,worktree_path:badgeHeavy?'/fixture/worktree':null,
    project_id:badgeHeavy?'project':null,
    attention:['approval','clarify'].includes(own)?{kind:own,count:1}:null};
  const children=['fork','delegated'].map(kind=>({session_id:kind,title:kind+' authentication task',
    parent_session_id:'parent',relationship_type:'child_session',raw_source:'subagent',session_source:kind,
    message_count:3,is_streaming:childMode==='running'||(childMode==='concurrent'&&kind==='delegated'),
    has_unread:childMode==='unread'||childMode==='concurrent',
    attention:kind==='fork'&&['approval','concurrent'].includes(childMode)?{kind:'approval',count:1}:null}));
  const result=renderFixture([parent,...children],[parent,...children],expanded,selected);
  document.querySelector('#fixture').replaceChildren(result.element);
  document.querySelectorAll('*').forEach(e=>e.scrollLeft=0);
}
function measure(){
  const chip=document.querySelector('.session-child-count'),title=document.querySelector('.session-title');
  const label=chip.querySelector('.session-child-count-label'),activity=document.querySelector('.session-child-activity-indicator');
  const clip=document.querySelector('.session-text').getBoundingClientRect();
  const visible=e=>{const r=e.getBoundingClientRect(),h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
    return r.left>=clip.left-.5&&r.right<=clip.right+.5&&!!h&&(h===e||e.contains(h)||chip.contains(h));};
  const range=document.createRange();range.setStart(title.firstChild,0);range.setEnd(title.firstChild,5);
  const firstFive=range.getBoundingClientRect(),tr=title.getBoundingClientRect();
  const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d'),style=getComputedStyle(title);
  ctx.font=style.font;
  // CSS ellipsis occupies painted space too; reserve it independently of the text box.
  const glyphsFit=firstFive.right+ctx.measureText('…').width<=tr.right+.5;
  range.setStart(label.firstChild,0);range.setEnd(label.firstChild,1);
  const count=range.getBoundingClientRect(),lr=label.getBoundingClientRect();
  const dot=document.querySelector('.session-item > .session-attention-indicator');
  const cr=chip.getBoundingClientRect(),state=chip.querySelector('.session-child-count-state');
  const inside=e=>{const r=e.getBoundingClientRect();return r.left>=cr.left&&r.right<=cr.right&&visible(e);};
  const labelStyle=getComputedStyle(label);ctx.font=labelStyle.font;
  const countEllipsis=label.scrollWidth>label.clientWidth?ctx.measureText('…').width:0;
  const pseudo=getComputedStyle(state,'::before'),sr=state.getBoundingClientRect();
  const halo=pseudo.boxShadow==='none'?0:3;
  const paintHalf=parseFloat(pseudo.width)/2+halo;
  const statusPaintContained=sr.x+sr.width/2-paintHalf>=cr.left-.5&&sr.x+sr.width/2+paintHalf<=cr.right+.5;
  return {contained:!activity||chip.contains(activity),activityCount:document.querySelectorAll('.session-child-activity-indicator').length,
    activityVisible:!!activity&&visible(activity),activityAnimation:activity&&getComputedStyle(activity,'::before').animationName,
    activityColor:activity&&getComputedStyle(activity).color,accent:getComputedStyle(dot).color,
    statusVisible:visible(chip.querySelector('.session-child-count-state')),
    statusClass:chip.querySelector('.session-child-count-state').className,
    titleWidth:tr.width,firstFiveWidth:firstFive.width,glyphsFit,
    countReadable:count.left>=lr.left-.5&&count.right+countEllipsis<=lr.right+.5,
    chipWidth:cr.width,labelWidth:lr.width,countWidth:count.width,countEllipsis,label:label.textContent,
    markPaintContained:statusPaintContained&&inside(state)&&(!activity||inside(activity)),
    statusAnimation:pseudo.animationName,statusPaintWidth:parseFloat(pseudo.width),
    badges:document.querySelectorAll('.session-branch-indicator,.session-worktree-indicator,.session-project-dot').length,
    selected:document.querySelector('.session-item').classList.contains('active'),
    density:window._sidebarDensity,titleFont:style.font,
    timeVisible:getComputedStyle(document.querySelector('.session-time')).display!=='none',
    aria:chip.getAttribute('aria-label'),running:t('session_child_running'),unread:t('session_child_unread'),
    ownClass:dot.className,ownAnimation:getComputedStyle(dot,'::before').animationName,
    height:document.querySelector('.session-item').getBoundingClientRect().height};
}
"""


def check(data, own, selected, density, mode='concurrent', heavy=False):
    failures = []
    concurrent = mode == 'concurrent'
    if concurrent and (not data['contained'] or not data['activityVisible'] or data['activityAnimation'] != 'spin'):
        failures.append('concurrent activity not visibly owned by chip')
    if not concurrent and data['activityCount']:
        failures.append('single-state chip duplicates activity')
    if (heavy and data['titleWidth'] < 24) or (not heavy and not data['glyphsFit']):
        failures.append('primary title recognition lost')
    if not data['markPaintContained'] or not data['statusVisible'] or not data['countReadable']:
        failures.append('secondary count or mark clipped')
    if data['statusAnimation'] != ('spin' if mode == 'running' else 'none'):
        failures.append('status pseudo-element has wrong activity')
    if concurrent and (data['running'] not in data['aria'] or data['unread'] not in data['aria']):
        failures.append('localized concurrent state inaccessible')
    expected = {'approval': 'attention-approval', 'clarify': 'attention-clarify', 'streaming': 'streaming',
                'unread': 'unread' if selected != 'parent' else None, 'idle': None}[own]
    own_states = {'is-unread', 'is-streaming', 'is-attention-approval', 'is-attention-clarify', 'is-attention-generic'}
    if own_states.intersection(data['ownClass'].split()) != ({f'is-{expected}'} if expected else set()):
        failures.append('parent own dot changed')
    if data['ownAnimation'] != ('spin' if own == 'streaming' else 'none'):
        failures.append('parent own animation changed')
    if own == 'idle' and not data['timeVisible']:
        failures.append('idle timestamp missing')
    if data['selected'] != (selected == 'parent') or data['density'] != density or data['badges'] != (3 if heavy else 0):
        failures.append('fixture omitted requested scene')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before-ref')
    parser.add_argument('--representative', action='store_true', help='Run English/German/Russian scenes before the complete locale matrix')
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
            page.add_script_tag(content=SCENE)
            locales = page.evaluate('Object.keys(LOCALES)')
            if args.representative:
                locales = ['en', 'de', 'ru']
            for width, locale, skin, dark, selected, density, own in product(
                [180, 220, 240, 300, 360], locales, ['graphite', 'default', 'catppuccin', 'geist-contrast'],
                [False, True], ['other', 'parent'], ['compact', 'detailed'],
                ['idle', 'unread', 'approval', 'clarify', 'streaming'],
            ):
                page.evaluate('a=>{document.querySelector("#fixture").style.width=a[0]+"px";setLocale(a[1]);'
                              'document.documentElement.dataset.skin=a[2];document.documentElement.classList.toggle("dark",a[3]);'
                              'badgeHeavy=false;childMode="concurrent";scene(a[6],false,a[4],a[5])}',
                              [width, locale, skin, dark, selected, density, own])
                data = page.evaluate('measure()')
                failures = check(data, own, selected, density)
                results.append(dict(viewport=viewport, width=width, locale=locale, skin=skin, dark=dark,
                                    selected=selected, density=density, own=own, data=data, failures=failures))
                if locale == 'en' and width == 180 and selected == 'parent' and density == 'detailed' and (
                    (skin == 'graphite' and own == 'approval') or
                    (skin in ['default', 'catppuccin'] and own in ['clarify', 'streaming'])
                ):
                    page.screenshot(path=str(args.output / f'{viewport}-{skin}-{own}-{dark}.png'))
            for title, skin, dark, density in product(
                ['Permission decision investigation', 'Проверка состояния дочерних задач'],
                ['graphite', 'default', 'catppuccin', 'geist-contrast'], [False, True], ['compact', 'detailed'],
            ):
                page.evaluate('a=>{document.querySelector("#fixture").style.width="180px";titleText=a[0];'
                              'document.documentElement.dataset.skin=a[1];document.documentElement.classList.toggle("dark",a[2]);'
                              'scene("approval",false,"parent",a[3])}', [title, skin, dark, density])
                data = page.evaluate('measure()')
                results.append(dict(viewport=viewport, title=title, skin=skin, dark=dark, density=density,
                                    data=data, failures=check(data, 'approval', 'parent', density)))
            page.evaluate('titleText="Sidebar authentication investigation"')
            # Competing metadata has an existing 24px title floor, not a universal
            # five-glyph promise. Assert real badges, marks and count at that floor.
            for locale, skin, selected, density, mode in product(
                locales, ['graphite', 'default', 'catppuccin', 'geist-contrast'],
                ['other', 'parent'], ['compact', 'detailed'], ['concurrent', 'running', 'approval', 'unread'],
            ):
                page.evaluate('a=>{document.querySelector("#fixture").style.width="180px";setLocale(a[0]);'
                              'document.documentElement.dataset.skin=a[1];badgeHeavy=true;childMode=a[4];'
                              'scene("approval",false,a[2],a[3])}', [locale, skin, selected, density, mode])
                data = page.evaluate('measure()')
                failures = check(data, 'approval', selected, density, mode=mode, heavy=True)
                results.append(dict(viewport=viewport, width=180, locale=locale, skin=skin, selected=selected,
                                    density=density, mode=mode, heavy=True, data=data, failures=failures))
            # Replay the reported geometry exactly, including the old fixture's
            # two in-flow placeholder spans. Production swipe affordances are absolute.
            page.add_script_tag(content="_makeSessionSwipeAffordance=()=>document.createElement('span')")
            page.evaluate('badgeHeavy=false;childMode="concurrent";setLocale("en");'
                          'document.documentElement.dataset.skin="graphite";scene("approval",false,"parent")')
            data = page.evaluate('measure()')
            results.append(dict(viewport=viewport, reported_scene=True, data=data,
                                failures=check(data, 'approval', 'parent', 'compact')))
            page.screenshot(path=str(args.output / f'{viewport}-reported-graphite.png'))
            page.add_script_tag(content="_makeSessionSwipeAffordance=d=>{const e=document.createElement('span');"
                                "e.className='session-swipe-affordance session-swipe-affordance-'+d;return e}")
            page.evaluate('childMode="concurrent";setLocale("en")')
            for skin, dark, own in product(
                ['graphite', 'default', 'catppuccin', 'geist-contrast'], [False, True],
                ['approval', 'clarify', 'streaming'],
            ):
                page.evaluate('a=>{document.querySelector("#fixture").style.width="180px";'
                              'document.documentElement.dataset.skin=a[0];document.documentElement.classList.toggle("dark",a[1]);'
                              'scene(a[2],false,"parent","detailed")}', [skin, dark, own])
                chip = page.locator('.session-child-count')
                if touch:
                    chip.tap()
                else:
                    chip.focus()
                    chip.press('Enter')
                if page.locator('.session-child-session').count() != 2:
                    errors.append(f'disclosure failed: {viewport}/{skin}/{dark}/{own}')
                fork = page.locator('.session-child-session-fork')
                delegated = page.locator('.session-child-session-delegated')
                main = fork.locator('.session-child-session-main')
                geometry = page.evaluate('''()=>({
                    coarse:matchMedia('(pointer:coarse)').matches,
                    narrow:matchMedia('(max-width:768px)').matches,
                    fork:document.querySelector('.session-child-session-fork').getBoundingClientRect().height,
                    main:document.querySelector('.session-child-session-main').getBoundingClientRect().height,
                    delegated:document.querySelector('.session-child-session-delegated').getBoundingClientRect().height})''')
                if geometry['coarse'] != touch:
                    errors.append(f'pointer fixture mismatch: {geometry}')
                failures = []
                if geometry['coarse'] or geometry['narrow']:
                    if min(geometry['fork'], geometry['main'], geometry['delegated']) < 44 or abs(geometry['fork'] - geometry['delegated']) > .5:
                        failures.append('fork and delegated touch targets differ or fall below 44px')
                results.append(dict(viewport=viewport, interaction=True, skin=skin, dark=dark, own=own,
                                    data=geometry, failures=failures))
                if touch:
                    main.tap()
                else:
                    main.focus()
                    main.press('Enter')
                if page.evaluate('opened.at(-1).sid') != 'fork':
                    errors.append(f'fork navigation failed: {viewport}/{skin}/{dark}/{own}')
                if touch:
                    delegated.tap()
                else:
                    delegated.focus()
                    delegated.press('Enter')
                if page.evaluate('opened.at(-1).sid') != 'delegated':
                    errors.append(f'child navigation failed: {viewport}/{skin}/{dark}/{own}')
            page.screenshot(path=str(args.output / f'{viewport}-expanded.png'))
            page.locator('#fixture').evaluate('(e)=>e.style.width="300px"')
            page.evaluate('setLocale("en");scene("streaming",true,"parent","detailed")')
            page.screenshot(path=str(args.output / f'{viewport}-mixed-300.png'))
            page.locator('#fixture').evaluate('(e)=>e.style.width="180px"')
            page.evaluate('document.documentElement.classList.remove("dark");scene("unread",false,"other","detailed")')
            page.wait_for_timeout(200)
            page.screenshot(path=str(args.output / f'{viewport}-180-light-detailed.png'))
            page.evaluate('scene("idle",false,"other","detailed")')
            page.screenshot(path=str(args.output / f'{viewport}-180-light-time.png'))
            # Exercise both sides of the OR media query independently, rather
            # than inferring pointer capability from the viewport width.
            for extra_viewport, extra_touch in [(1280, True), (390, False)] if viewport == 1280 else []:
                extra = browser.new_context(viewport={'width': extra_viewport, 'height': 800}, has_touch=extra_touch)
                probe = extra.new_page()
                probe.set_content('<main id="fixture" style="padding:8px;width:300px;box-sizing:border-box"></main>')
                probe.add_style_tag(content=source('static/style.css'))
                probe.add_script_tag(content=component_script(source('static/sessions.js')))
                probe.add_script_tag(content=source('static/i18n.js'))
                probe.add_script_tag(content=SCENE)
                probe.evaluate('scene("approval",true,"parent","detailed")')
                geometry = probe.evaluate('''()=>({coarse:matchMedia('(pointer:coarse)').matches,
                    narrow:matchMedia('(max-width:768px)').matches,
                    fork:document.querySelector('.session-child-session-fork').getBoundingClientRect().height,
                    main:document.querySelector('.session-child-session-main').getBoundingClientRect().height,
                    delegated:document.querySelector('.session-child-session-delegated').getBoundingClientRect().height})''')
                failures = []
                if geometry['coarse'] != extra_touch or geometry['narrow'] != (extra_viewport == 390):
                    failures.append('media query fixture mismatch')
                if min(geometry['fork'], geometry['main'], geometry['delegated']) < 44 or abs(geometry['fork'] - geometry['delegated']) > .5:
                    failures.append('fork and delegated touch targets differ or fall below 44px')
                results.append(dict(viewport=extra_viewport, touch=extra_touch, media_probe=True, data=geometry, failures=failures))
                extra.close()
            context.close()
        browser.close()
    report = dict(cases=len(results), failures=sum(bool(r['failures']) for r in results), errors=errors, results=results)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: report[k] for k in ['cases','failures','errors']}))
    if (report['failures'] or errors) and not args.before_ref:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
