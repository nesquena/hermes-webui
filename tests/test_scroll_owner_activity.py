"""Real layout tests of reader helpers; composed renderer covered by browser gate."""
import os
from pathlib import Path

import pytest
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def function(source, name):
    start = source.index('function ' + name + '(')
    end = source.index('\n}', start) + 2
    return source[start:end]


@pytest.mark.parametrize('kind', ['reason', 'tool'])
@pytest.mark.parametrize('width', [1440, 390])
def test_activity_reader_survives_rebuilt_prepend(kind, width):
    source = Path(os.environ.get('SCROLL_ACTIVITY_SOURCE', ROOT / 'static/ui.js')).read_text()
    functions = '\n'.join(function(source, name) for name in [
        '_messageWindowSnapshot', '_messageWindowReader', '_restoreMessageWindowReader',
        '_messageVirtualWindow'])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 844})
        page.set_content('''<style>body{margin:0} #messages{height:600px;overflow:auto;overflow-anchor:none}
            p{margin:0;height:100px} [hidden]{display:none}</style>
            <div id="messages"><div id="msgInner" data-window-session="one"></div></div>''')
        page.add_script_tag(content='''
            const $=id=>document.getElementById(id);
            const S={session:{session_id:'one'}, messages:[]}; let base=941;
            function _messageSessionIndexForRawIdx(i){return base+i;}
            function _messageVisibleIndexForAnchorKey(){return -1;}
            function _toolDisclosureIdentity(tc){return 'id:'+tc.id;}
            let _programmaticScroll=false,_programmaticScrollSetAt=0,_lastScrollTop=0;
            function _deferClearProgrammaticScroll(){}
            const MESSAGE_VIRTUAL_THRESHOLD_ROWS=80,MESSAGE_VIRTUAL_BUFFER_PX=900;
            function _messageVirtualDefaultHeightForRole(){return 140;}
        ''' + functions)
        result = page.evaluate('''kind=>{
            const c=$('messages'), inner=$('msgInner');
            function scene(raw,pad){
                base=1007-raw; S.messages=Array.from({length:200},()=>({}));
                S.messages[raw]={tool_calls:[{id:'public-8-22'}]};
                inner.innerHTML=`<div style="height:${pad}px" data-msg-idx="0" data-session-msg-idx="${base}">older</div>
                    <div hidden data-msg-idx="${raw}" data-session-msg-idx="1007" data-worklog-anchor-key="msg:${raw}"></div>
                    <div class="${kind==='reason'?'wl-reason':'tool-card-row'}"
                    ${kind==='reason'?`data-worklog-anchor-key="msg:${raw}"`:'data-tool-disclosure-key="id:public-8-22"'}>
                    ${Array.from({length:20},(_,i)=>`<p>Activity paragraph ${i}</p>`).join('')}</div>
                    <div style="height:1000px" data-msg-idx="199" data-session-msg-idx="${base+199}">later</div>`;
            }
            scene(66,800); c.scrollTop=1357;
            const anchor=_messageWindowSnapshot();
            const entries=S.messages.map((m,rawIdx)=>({m,rawIdx}));
            const reader=_messageWindowReader(entries);
            const windowRange=_messageVirtualWindow({total:200,scrollTop:99999,viewportHeight:600,
                heights:Array(200).fill(140),keepTailCount:0,reader});
            const expected=inner.querySelectorAll('p')[5].getBoundingClientRect().top;
            scene(106,1630); _restoreMessageWindowReader(inner,anchor);
            return {sessionIndex:anchor.sessionIndex,kind:anchor.activityKind,reader,
                windowRange,expected,actual:inner.querySelectorAll('p')[5].getBoundingClientRect().top,
                sourceCount:inner.querySelectorAll('[data-msg-idx]').length};
        }''', kind)
        browser.close()
    assert result['sessionIndex'] == 1007
    assert result['kind'] == kind
    assert result['reader']['index'] == 66
    assert result['windowRange']['start'] <= 66 < result['windowRange']['end']
    assert result['actual'] == pytest.approx(result['expected'], abs=0.5)
    assert result['sourceCount'] == 3


@pytest.mark.parametrize('width', [1440, 390])
def test_collapsed_activity_cannot_own_visible_reader(width):
    source = Path(os.environ.get('SCROLL_ACTIVITY_SOURCE', ROOT / 'static/ui.js')).read_text()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 844})
        page.set_content('<style>body{margin:0}#messages{height:600px;overflow:auto}\n          .collapsed{height:0;overflow:hidden}p{height:300px;margin:0}</style>\n          <div id="messages"><div id="msgInner" data-window-session="one">\n          <div hidden data-msg-idx="0" data-session-msg-idx="0" data-worklog-anchor-key="msg:0"></div>\n          <div class="collapsed"><div class="wl-reason" data-worklog-anchor-key="msg:0"><p>Not painted</p></div></div>\n          <div data-msg-idx="1" data-session-msg-idx="1"><p>Actual reader</p></div>\n          </div></div>')
        page.add_script_tag(content="const $=id=>document.getElementById(id);const S={session:{session_id:'one'}};" + function(source, '_messageWindowSnapshot'))
        result = page.evaluate("() => {const a=_messageWindowSnapshot();return {index:a.sessionIndex,text:a.node.textContent};}")
        browser.close()
    assert result == {'index': 1, 'text': 'Actual reader'}


@pytest.mark.parametrize('width', [1440, 390])
def test_reason_projection_can_restore_to_its_visible_source(width):
    source = Path(os.environ.get('SCROLL_ACTIVITY_SOURCE', ROOT / 'static/ui.js')).read_text()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 844})
        page.set_content("""<style>body{margin:0}#messages{height:600px;overflow:auto;overflow-anchor:none}
          p{height:100px;margin:0}</style><div id="messages"><div id="msgInner" data-window-session="one"></div></div>""")
        page.add_script_tag(content="""
          const $=id=>document.getElementById(id);const S={session:{session_id:'one'}};
          let _programmaticScroll=false,_programmaticScrollSetAt=0,_lastScrollTop=0;
          function _deferClearProgrammaticScroll(){}
        """ + function(source, '_messageWindowSnapshot') + function(source, '_restoreMessageWindowReader'))
        result = page.evaluate("""() => {
          const c=$('messages'),inner=$('msgInner');
          const text=Array.from({length:25},(_,i)=>`<p>Paragraph ${i}</p>`).join('');
          inner.innerHTML=`<div hidden data-msg-idx="5" data-session-msg-idx="1007" data-worklog-anchor-key="msg:5"></div>
            <div class="wl-reason" data-worklog-anchor-key="msg:5">${text}</div>`;
          c.scrollTop=412;
          const anchor=_messageWindowSnapshot();
          const before=anchor.node.getBoundingClientRect().top;
          inner.innerHTML=`<div style="height:4686px"></div><div data-msg-idx="55" data-session-msg-idx="1007">${text}</div>`;
          _restoreMessageWindowReader(inner,anchor);
          const after=inner.querySelectorAll('p')[anchor.landmarkIndex].getBoundingClientRect().top;
          return {before,after,kind:anchor.activityKind,text:anchor.node.textContent};
        }""")
        browser.close()
    assert result['kind'] == 'reason'
    assert result['after'] == pytest.approx(result['before'], abs=0.5), result
