"""Production child presentation: readable labels, localized subjects and selection.

No server, Agent, credentials or real storage. Navigation is recorded at the
existing sidebar seam. --before-ref records failures on the exact prior source.
"""
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
function scene(reference,state,selected='parent',badges=false){
  window._sidebarDensity='detailed';
  const parent={session_id:'parent',title:'Debug failing authentication test',message_count:3,
    has_unread:true,_compression_segment_count:4,
    _lineage_segments:Array.from({length:4},(_,i)=>({session_id:'prior'+i,title:'Earlier turn '+i,updated_at:i+1})),
    parent_session_id:badges?'original':null,worktree_path:badges?'/fixture/worktree':null,project_id:badges?'project':null};
  const child=(id,kind)=>({session_id:id,title:id+' authentication task',message_count:3,
    parent_session_id:'parent',relationship_type:'child_session',raw_source:'subagent',session_source:kind,
    archived:reference,is_streaming:state==='running',has_unread:state==='unread',
    attention:['approval','clarify','generic'].includes(state)?{kind:state,count:1}:null});
  const children=[child('delegated','other'),child('fork','fork')];
  const result=renderFixture(reference?[parent]:[parent,...children],[parent,...children],!reference,selected);
  if(!reference)_expandedChildSessionKeys.add(_sidebarLineageKeyForRow(result.row));
  document.querySelector('#fixture').replaceChildren(_renderOneSession(result.row));
  document.querySelectorAll('*').forEach(e=>e.scrollLeft=0);
}
function measure(){
  const chip=document.querySelector('.session-child-count'),label=chip.querySelector('.session-child-count-label');
  const mark=chip.querySelector('.session-child-count-state'),clip=document.querySelector('.session-text').getBoundingClientRect();
  const r=mark.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
  const range=document.createRange();range.selectNodeContents(label);
  const lr=label.getBoundingClientRect();
  const pill=document.querySelector('.session-lineage-count'),pr=pill.getBoundingClientRect();
  const ps=getComputedStyle(pill);range.selectNodeContents(pill);
  const pillReadable=[...range.getClientRects()].every(r=>r.left>=pr.left+parseFloat(ps.paddingLeft)-.5&&r.right<=pr.right-parseFloat(ps.paddingRight)+.5);
  range.selectNodeContents(label);
  return {label:label.textContent,labelReadable:[...range.getClientRects()].every(r=>r.left>=lr.left-.5&&r.right<=lr.right+.5),
    labelWidth:lr.width,aria:chip.getAttribute('aria-label'),tooltip:chip.title,
    short:t('session_child_archived_short'),full:t('session_child_archived'),
    subject:t('session_child_attention',t('session_attention_approval_title')),open:t('session_child_open'),
    markVisible:r.left>=clip.left&&r.right<=clip.right&&!!hit&&(hit===mark||mark.contains(hit)),
    titleWidth:document.querySelector('.session-title').getBoundingClientRect().width,pillReadable,
    delegatedTitle:document.querySelector('.session-child-session-delegated')?.title};
}
function rowStyle(selector){
  const e=document.querySelector(selector),d=e.querySelector('.session-child-session-state');
  // Composite the actual computed ancestor/row paints into sRGB pixels. A
  // matching CSS token alone cannot demonstrate a visible selection wash.
  const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
  const ctx=canvas.getContext('2d');ctx.fillStyle='white';ctx.fillRect(0,0,1,1);
  const ancestors=[];for(let p=e.parentElement;p;p=p.parentElement)ancestors.unshift(p);
  for(const p of ancestors){ctx.fillStyle=getComputedStyle(p).backgroundColor;ctx.fillRect(0,0,1,1);}
  const basePixel=[...ctx.getImageData(0,0,1,1).data].slice(0,3);
  ctx.fillStyle=getComputedStyle(e).backgroundColor;ctx.fillRect(0,0,1,1);
  const pixel=[...ctx.getImageData(0,0,1,1).data].slice(0,3);
  return {background:getComputedStyle(e).backgroundColor,shadow:getComputedStyle(e).boxShadow,
    color:getComputedStyle(d).color,animation:getComputedStyle(d,'::before').animationName,basePixel,pixel};
}
"""


def contrast(a, b):
    def luminance(pixel):
        rgb = [c / 255 for c in pixel]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
        return sum(c * w for c, w in zip(linear, [.2126, .7152, .0722], strict=True))
    light, dark = sorted([luminance(a), luminance(b)], reverse=True)
    return (light + .05) / (dark + .05)


def settled_row_style(page, selector):
    """Wait for CSS transitions and three identical rendered-style frames."""
    return page.evaluate('''selector=>new Promise((resolve,reject)=>{
      const deadline=setTimeout(()=>reject(new Error('row styles did not settle in 3s')),3000);
      let previous=null,stable=0;
      const sample=()=>{
        const row=document.querySelector(selector),ancestors=[];
        for(let e=row;e;e=e.parentElement) ancestors.push(e);
        const transitioning=ancestors.some(e=>e.getAnimations({subtree:false}).some(a=>
          a instanceof CSSTransition&&a.playState==='running'));
        const value=rowStyle(selector),serialized=JSON.stringify(value);
        stable=!transitioning&&serialized===previous?stable+1:0;
        previous=serialized;
        if(stable>=3){clearTimeout(deadline);resolve(value);}else requestAnimationFrame(sample);
      };requestAnimationFrame(sample);
    })''', selector)


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
        for viewport, touch in [(1280, False), (768, False), (390, True)]:
            context = browser.new_context(viewport={'width': viewport, 'height': 800}, has_touch=touch)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.set_content('<main id="fixture" style="padding:8px;box-sizing:border-box;background:var(--sidebar)"></main>')
            page.add_style_tag(content=source('static/style.css'))
            page.add_script_tag(content=component_script(source('static/sessions.js')))
            page.add_script_tag(content=source('static/i18n.js'))
            page.add_script_tag(content=SCENE)
            locales = page.evaluate('Object.keys(LOCALES)')
            for width in [180, 300, 360]:
                page.locator('#fixture').evaluate('(e,w)=>e.style.width=w+"px"', width)
                for locale in locales:
                    page.evaluate('setLocale', locale)
                    page.evaluate('scene(true,"approval")')
                    data = page.evaluate('measure()')
                    failures = []
                    if not data['markVisible'] or not data['pillReadable']:
                        failures.append('status or prior-turn navigation clipped')
                    if width >= 300 and (not data['labelReadable'] or data['label'] != data['short']):
                        failures.append('archived label not fully readable at normal width')
                    if data['full'] not in data['tooltip'] or data['aria'] != data['tooltip']:
                        failures.append('full archived explanation inaccessible')
                    if not data['tooltip'].startswith(data['subject'] + ' · '):
                        failures.append('attention subject is not qualified as child')
                    page.evaluate('scene(false,"approval")')
                    delegated = page.evaluate('measure()')
                    if delegated['delegatedTitle'] != data['open'] + ' · ' + data['subject']:
                        failures.append('delegated action/state not localized with middle dot')
                    if delegated['titleWidth'] < 24 or not delegated['pillReadable']:
                        failures.append('title floor or detailed navigation lost')
                    results.append(dict(scene='labels', viewport=viewport, width=width, locale=locale, data=data, delegated=delegated, failures=failures))
            page.evaluate('setLocale("en")')
            page.locator('#fixture').evaluate('(e)=>e.style.width="300px"')
            for skin in ['default', 'graphite', 'codex', 'terracotta', 'github', 'geist-contrast']:
                for dark in [False, True]:
                    page.evaluate('([s,d])=>{document.documentElement.dataset.skin=s;document.documentElement.classList.toggle("dark",d)}', [skin, dark])
                    for state in ['approval', 'clarify', 'generic']:
                        for kind in ['delegated', 'fork']:
                            selector = '.session-child-session-' + kind
                            page.evaluate('s=>scene(false,s,"other",true)', state)
                            page.mouse.move(viewport-1, 799)
                            idle = settled_row_style(page, selector)
                            page.locator(selector).hover()
                            hover = settled_row_style(page, selector)
                            if skin == 'graphite' and state == 'approval' and kind == 'delegated':
                                page.screenshot(path=str(args.output / f'{viewport}-{dark}-hover.png'))
                            page.mouse.move(viewport-1, 799)
                            leave = settled_row_style(page, selector)
                            page.evaluate('([s,k])=>scene(false,s,k,true)', [state, kind])
                            selected = settled_row_style(page, selector)
                            failures = []
                            if idle['background'] == selected['background'] or hover['background'] != selected['background']:
                                failures.append('hover/selection not distinguishable from idle attention')
                            selected['contrast'] = contrast(selected['basePixel'], selected['pixel'])
                            hover['contrast'] = contrast(hover['basePixel'], hover['pixel'])
                            if min(selected['contrast'], hover['contrast']) < 1.08:
                                failures.append('selection wash not visibly distinct from surrounding surface')
                            if leave != idle:
                                failures.append('leaving hover does not restore attention tint')
                            if any(s['color'] != idle['color'] or s['shadow'] != idle['shadow'] for s in [hover, selected]):
                                failures.append('attention cue lost during hover/selection')
                            results.append(dict(scene='selection', viewport=viewport, skin=skin, dark=dark, state=state, kind=kind, idle=idle, hover=hover, selected=selected, failures=failures))
                            if skin == 'graphite' and state == 'approval' and kind == 'delegated':
                                page.screenshot(path=str(args.output / f'{viewport}-{dark}-selected.png'))
                    for reference, state in [(True, 'approval'), (False, 'running'), (False, 'unread')]:
                        page.evaluate('([r,s])=>scene(r,s)', [reference, state])
                        page.mouse.move(viewport-1, 799)
                        if skin == 'graphite':
                            page.screenshot(path=str(args.output / f'{viewport}-{dark}-{reference}-{state}.png'))
                    page.evaluate('scene(false,"approval","parent")')
                    page.locator('.session-child-session-delegated').click()
                    if page.evaluate('opened.at(-1).sid') != 'delegated':
                        errors.append('delegated navigation did not open its child')
            context.close()
        browser.close()
    failures = [r for r in results if r['failures']]
    report = dict(cases=len(results), failures=len(failures), errors=errors, results=results)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(dict(cases=len(results), failures=len(failures), errors=errors)))
    if (failures or errors) and not args.before_ref:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
