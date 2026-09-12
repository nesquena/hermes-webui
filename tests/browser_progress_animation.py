#!/usr/bin/env python3
"""Actual stylesheet: running progress animates without per-frame layout."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as pw:
        for engine in ('chromium', 'webkit'):
            browser = getattr(pw, engine).launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.set_content('<div style="width:300px"><div class="transparent-event-progress" data-progress-running="1" data-progress-percent="60%" style="--transparent-progress-percent:60%"></div></div>')
                page.add_style_tag(path=str(ROOT/'static/style.css'))
                page.wait_for_timeout(400)
                track_width=page.locator('.transparent-event-progress').evaluate('(e)=>e.getBoundingClientRect().width')
                frame = page.evaluate("""() => {
                    const a=document.getAnimations().find(a=>a.animationName==='transparent-progress-shimmer');
                    if(!a) throw new Error('missing running indicator animation');
                    const frames=a.effect.getKeyframes();
                    return {frames:frames.map(f=>({left:f.left,right:f.right,transform:f.transform})),width:getComputedStyle(document.querySelector('.transparent-event-progress'),'::before').width};
                }""")
                assert all(not f.get('left') and not f.get('right') and f.get('transform') for f in frame['frames']), frame
                assert abs(float(frame['width'].removesuffix('px'))-track_width*0.6)<0.1, frame
                geometry=page.evaluate("""() => {
                    const a=document.getAnimations().find(a=>a.animationName==='transparent-progress-shimmer');
                    a.pause(); a.currentTime=800;
                    const e=document.querySelector('.transparent-event-progress');
                    const s=getComputedStyle(e,'::before');
                    const x=new DOMMatrixReadOnly(s.transform).m41;
                    const result={x,width:parseFloat(s.width),track:e.getBoundingClientRect().width};
                    a.play(); return result;
                }""")
                assert geometry['x']>=0 and geometry['x']+geometry['width']<=geometry['track'],geometry
                if engine=='chromium':
                    cdp=page.context.new_cdp_session(page)
                    cdp.send('Performance.enable')
                    page.wait_for_timeout(400)
                    before={m['name']:m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
                    page.wait_for_timeout(1000)
                    after={m['name']:m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
                    layouts=after['LayoutCount']-before['LayoutCount']
                    assert layouts<5, layouts
                    print(json.dumps({'engine':engine,'layouts':layouts}))
                page.emulate_media(reduced_motion='reduce')
                assert page.evaluate("getComputedStyle(document.querySelector('.transparent-event-progress'),'::before').animationName")=='none'
                page.emulate_media(reduced_motion='no-preference')
                page.evaluate("const e=document.querySelector('.transparent-event-progress');e.removeAttribute('data-progress-running');e.setAttribute('data-progress-percent','100%');e.style.setProperty('--transparent-progress-percent','100%')")
                page.wait_for_timeout(400)
                state=page.evaluate("(()=>{const s=getComputedStyle(document.querySelector('.transparent-event-progress'),'::before');return {animation:s.animationName,width:s.width,transform:s.transform}})()")
                assert state['animation']=='none' and state['transform']=='none',state
                assert abs(float(state['width'].removesuffix('px'))-track_width)<0.1,state
                print(json.dumps({'engine':engine,'completion':state,'reduced_motion':'passed'}))
            finally:
                browser.close()


if __name__=='__main__':
    main()
