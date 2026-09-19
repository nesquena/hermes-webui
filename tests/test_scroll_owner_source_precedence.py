"""Reader restoration prefers source identity over repeated content prefixes."""

import pytest

from tests.test_scroll_owner_activity import ROOT, function, sync_playwright


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('kind', ['message', 'reason'])
@pytest.mark.parametrize('replacement', ['exact', 'unique', 'ambiguous', 'missing'])
def test_reader_source_precedence_and_unambiguous_fallback(width, kind, replacement):
    source = (ROOT / 'static/ui.js').read_text()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': width, 'height': 844})
        page.set_content('''<style>body{margin:0}#messages{height:600px;overflow:auto;overflow-anchor:none}
            p{height:100px;margin:0}</style>
            <div id="messages"><div id="msgInner" data-window-session="one"></div></div>''')
        page.add_script_tag(content='''
            const $=id=>document.getElementById(id);
            const S={session:{session_id:'one'}};
            let _programmaticScroll=false,_programmaticScrollSetAt=0,_lastScrollTop=0;
            function _deferClearProgrammaticScroll(){}
        ''' + function(source, '_messageWindowSnapshot') + function(source, '_restoreMessageWindowReader'))
        result = page.evaluate('''({kind,replacement})=>{
            const c=$('messages'),inner=$('msgInner');
            function row(index,raw,key,label){
                const text=Array.from({length:12},(_,i)=>`<p>${label} paragraph ${i}</p>`).join('');
                const attrs=`data-msg-idx="${raw}" data-session-msg-idx="${index}"
                    data-message-anchor-key="${key}" data-worklog-anchor-key="msg:${raw}"`;
                return kind==='reason'
                    ? `<div hidden ${attrs}></div><div class="wl-reason" data-worklog-anchor-key="msg:${raw}" data-label="${label}">${text}</div>`
                    : `<div ${attrs} data-label="${label}">${text}</div>`;
            }
            inner.innerHTML='<div style="height:700px"></div>'+row(1007,7,'repeated','owner')+
                '<div style="height:1600px"></div>';
            c.scrollTop=957;
            const anchor=_messageWindowSnapshot();
            const before=anchor.node.getBoundingClientRect().top;
            let rows='';
            if(replacement==='exact') rows=row(1001,101,'repeated','earlier')+row(1007,107,'repeated','owner');
            if(replacement==='unique') rows=row(1001,101,'different','earlier')+row(1008,108,'repeated','owner');
            if(replacement==='ambiguous') rows=row(1001,101,'repeated','earlier')+row(1008,108,'repeated','other');
            if(replacement==='missing') rows=row(1001,101,'different','earlier')+row(1008,108,'also-different','other');
            inner.innerHTML='<div style="height:1300px"></div>'+rows+'<div style="height:1600px"></div>';
            const scrollBefore=c.scrollTop;
            _restoreMessageWindowReader(inner,anchor);
            const owner=inner.querySelector('[data-label="owner"]');
            return {source:anchor.sessionIndex,kind:anchor.activityKind,before,
                after:owner?.querySelectorAll('p')[anchor.landmarkIndex].getBoundingClientRect().top,
                scrollBefore,scrollAfter:c.scrollTop,programmatic:_programmaticScroll};
        }''', {'kind': kind, 'replacement': replacement})
        browser.close()
    assert result['source'] == 1007
    assert result['kind'] == ('reason' if kind == 'reason' else '')
    if replacement in ('exact', 'unique'):
        assert result['after'] == pytest.approx(result['before'], abs=0.5), result
    else:
        assert result['scrollAfter'] == result['scrollBefore'], result
        assert result['programmatic'] is False
