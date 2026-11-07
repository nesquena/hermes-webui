"""Collapsed card descendants cannot become the transcript reader landmark."""

from tests.test_scroll_owner_live_reconciliation import isolated_webui, page  # noqa: F401


def test_snapshot_ignores_clipped_paragraph_after_card_rebuild(page):  # noqa: F811
    result = page.evaluate("""()=>{
      const c=$('messages'),inner=$('msgInner');
      S.messages=[{role:'assistant',tool_calls:[{id:'synthetic-call'}]}];
      inner.dataset.windowSession=S.session.session_id;
      const draw=gap=>{
        inner.innerHTML=`<div hidden data-msg-idx="0" data-session-msg-idx="0"></div>
          <div class="tool-card-row" data-tool-disclosure-key="id:synthetic-call" style="height:30px">
            <div style="height:30px">Tool header</div>
            <div style="height:0;overflow:hidden"><div style="height:${gap}px"></div>
              <p style="height:40px;margin:0">Hidden details</p></div>
          </div><div style="height:1500px"></div>`;
      };
      draw(400);c.scrollTop=0;
      const anchor=_messageWindowSnapshot();
      const before=inner.querySelector('.tool-card-row').getBoundingClientRect().top;
      draw(50);_restoreMessageWindowReader(inner,anchor);
      return {landmarkIndex:anchor.landmarkIndex,offset:anchor.offset,rowOffset:anchor.rowOffset,
        drift:inner.querySelector('.tool-card-row').getBoundingClientRect().top-before};
    }""")
    assert result['landmarkIndex'] == -1, result
    assert result['offset'] == result['rowOffset'], result
    assert abs(result['drift']) < 1, result
