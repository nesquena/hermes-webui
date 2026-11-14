"""Tool IDs can repeat in distinct source messages; restore their exact owner."""

from tests.test_scroll_owner_live_reconciliation import isolated_webui, page  # noqa: F401


def test_duplicate_tool_identity_keeps_source_owner_after_prepend(page):  # noqa: F811
    result = page.evaluate("""()=>{
      const c=$('messages'),inner=$('msgInner');
      const tool=()=>({role:'assistant',content:'',tool_calls:[{id:'repeated',function:{name:'web_search',arguments:'{}'}}]});
      S.busy=false;S.activeStreamId=null;delete INFLIGHT.fixture;
      S.messages=[{role:'user',content:'Question'},tool(),tool(),{role:'assistant',content:'Answer'}];
      S.toolCalls=[];_oldestIdx=0;renderMessages();
      const rendered=Array.from(inner.querySelectorAll('.tool-card-row[data-tool-disclosure-key="id:repeated"]'));
      const renderedOwners=rendered.map(n=>n.dataset.toolSourceSessionIdx||null);
      function draw(base,pad){
        inner.innerHTML=`<div style="height:${pad}px"></div>`;
        for(let i=0;i<2;i++){
          const source=document.createElement('div');source.hidden=true;
          source.dataset.msgIdx=String(base+i);source.dataset.sessionMsgIdx=String(i+1);inner.append(source);
          const card=document.createElement('div');card.className='tool-card-row';
          card.dataset.toolDisclosureKey='id:repeated';card.dataset.toolSourceSessionIdx=String(i+1);
          card.style.cssText='height:200px';inner.append(card);
        }
        inner.insertAdjacentHTML('beforeend','<div style="height:1500px"></div>');
        inner.dataset.windowSession=S.session.session_id;
      }
      draw(1,700);c.scrollTop=950;
      const anchor=_messageWindowSnapshot();
      const before=inner.querySelector('[data-tool-source-session-idx="2"]').getBoundingClientRect().top;
      draw(101,1300);_restoreMessageWindowReader(inner,anchor);
      return {renderedOwners,source:anchor.sessionIndex,
        drift:inner.querySelector('[data-tool-source-session-idx="2"]').getBoundingClientRect().top-before};
    }""")
    assert result['renderedOwners'] == ['1', '2'], result
    assert result['source'] == 2, result
    assert abs(result['drift']) < 1, result
