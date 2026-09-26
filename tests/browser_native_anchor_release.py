"""Real Chromium native anchoring after commit and input-invalidated JS snapshot.
Run .venv/bin/python tests/browser_native_anchor_release.py [--baseline]
Uses a synthetic DOM, production transaction/observer helpers and real CSS.
"""
import subprocess
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
source = (subprocess.check_output(['git', 'show', 'dff630e3:static/ui.js'], cwd=ROOT).decode()
          if '--baseline' in sys.argv else (ROOT / 'static/ui.js').read_text())


def function(name):
    start = source.index('function ' + name + '(')
    brace = source.index('{', start)
    depth = 0
    for end in range(brace, len(source)):
        depth += (source[end] == '{') - (source[end] == '}')
        if not depth:
            return source[start:end + 1]
    raise AssertionError(name)


with sync_playwright() as p:
    browser = p.chromium.launch()
    for touch in (True, False):
        page = browser.new_page(viewport={'width': 390 if touch else 1440, 'height': 844}, has_touch=touch, is_mobile=touch)
        page.set_content('<div id="messages" class="messages" style="height:600px;flex:none"><div id="msgInner"></div></div>')
        page.add_style_tag(content=(ROOT / 'static/style.css').read_text())
        page.add_script_tag(content="""
const $=id=>document.getElementById(id);
const S={session:{session_id:'test'},messages:[]};
let _messageWindowRevision=0,_messageWindowInputEpoch=0,_messageWindowObserved=null,_messageWindowResizeObserver=null;
let _messageUserUnpinned=true,_scrollPinned=false;
let _programmaticScroll=false,_programmaticScrollSetAt=0,_lastScrollTop=0;
function _deferClearProgrammaticScroll(){}
function _messageRawIdxForSessionIndex(i){return i;}
function _scheduleMessageVirtualizedRender(){}
function _messageWindowSnapshot(){return null;}
""" + '\n'.join(function(n) for n in ['_messageWindowNodeKey', '_commitMessageWindow', '_restoreMessageWindowReader', '_initializeMessageWindowOwnership', '_rememberMessageWindowReader', '_settleMessageWindowReader', '_browserOverflowAnchorActive', '_suppressBrowserOverflowAnchor']))
        page.evaluate("""() => {
const staged=document.createElement('div');
for(let i=0;i<30;i++){
 const row=document.createElement('div'); row.style.height='100px'; row.textContent='Reader row '+i;
 row.dataset.sessionMsgIdx=String(i); row.dataset.msgIdx=String(i); staged.append(row);
}
_commitMessageWindow($('msgInner'),staged,null,false);
$('messages').scrollTop=1000;
}""")
        page.wait_for_timeout(100)
        expected = 'auto' if touch else 'none'
        assert page.evaluate("getComputedStyle($('messages')).overflowAnchor") == expected, ('resting', touch)
        # Shared deferred suppression overlaps the synchronous transaction.
        page.evaluate("""() => {
 const release=_suppressBrowserOverflowAnchor($('messages'));
 const staged=$('msgInner').cloneNode(true);
 _commitMessageWindow($('msgInner'),staged,null,true);
 if(release){if($('messages').style.overflowAnchor!=='none')throw Error('lost outer owner');release();}
}""")
        # The shared suppressor releases on the next animation frame, not after
        # a fixed wall-clock delay. Wait for that paint opportunity so a busy
        # headless browser cannot make this an arbitrary 100 ms timing test.
        page.wait_for_function("expected => getComputedStyle($('messages')).overflowAnchor === expected",
                               arg=expected, timeout=1500)
        # The mobile hold must also survive commit and release on its own timer.
        mobile_start = source.index('const _MOBILE_ANCHOR_BASE_SETTLE_MS=')
        mobile_end = source.index('\n};', source.index('window._fixMobileScrollJank=function')) + 3
        page.add_script_tag(content=source[mobile_start:mobile_end])
        page.evaluate("""() => {
 window._fixMobileScrollJank();
 const held=$('messages').style.overflowAnchor;
 _commitMessageWindow($('msgInner'),$('msgInner').cloneNode(true),null,true);
 if($('messages').style.overflowAnchor!==held)throw Error('lost mobile owner');
}""")
        page.wait_for_timeout(1300)
        assert page.evaluate("getComputedStyle($('messages')).overflowAnchor") == expected
        # Explicit priority is preserved, not normalized to an empty declaration.
        assert page.evaluate("""() => {
 const el=$('messages'); el.style.setProperty('overflow-anchor','auto','important');
 _commitMessageWindow($('msgInner'),$('msgInner').cloneNode(true),null,true);
 const ok=el.style.getPropertyPriority('overflow-anchor')==='important'; el.style.removeProperty('overflow-anchor'); return ok;
}""")
        page.wait_for_timeout(100)
        before = page.evaluate("""() => {
_rememberMessageWindowReader();
$('messages').dispatchEvent(new WheelEvent('wheel',{deltaY:1}));
if(_messageWindowObserved.input===_messageWindowInputEpoch)throw Error('input did not invalidate snapshot');
return $('msgInner').children[10].getBoundingClientRect().top;
}""")
        page.evaluate("""() => {
 const img=document.createElement('img'); img.style.display='block';
 $('msgInner').children[0].style.height='auto';
 $('msgInner').children[0].append(img);
 img.src='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"></svg>');
}""")
        page.wait_for_timeout(200)
        after = page.evaluate("$('msgInner').children[10].getBoundingClientRect().top")
        if touch:
            assert abs(after-before) < 1, ('native late image anchor', before, after)
        print({'touch': touch, 'computed': expected, 'reader_before': before, 'reader_after': after})
        page.close()
    browser.close()
