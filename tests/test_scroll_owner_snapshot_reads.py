"""Reader snapshots reuse geometry only during a single read-only capture."""

from tests.test_scroll_owner_live_reconciliation import isolated_webui, page  # noqa: F401


def test_snapshot_reads_each_node_once_and_refreshes_after_scroll(page):  # noqa: F811
    result = page.evaluate("""() => {
      const inner=document.getElementById('msgInner'),c=document.getElementById('messages');
      inner.innerHTML='';inner.dataset.windowSession=S.session.session_id;
      const group=document.createElement('div');
      group.style.cssText='display:block;overflow:hidden';inner.append(group);
      for(let i=0;i<100;i++){
        const row=document.createElement('div');
        row.dataset.msgIdx=String(i);row.dataset.sessionMsgIdx=String(i);
        row.style.cssText='height:50px;margin:0';group.append(row);
      }
      c.scrollTop=1000;
      const nativeRect=Element.prototype.getBoundingClientRect,nativeStyle=window.getComputedStyle;
      const rectReads=new Map(),styleReads=new Map();
      Element.prototype.getBoundingClientRect=function(){
        rectReads.set(this,(rectReads.get(this)||0)+1);return nativeRect.call(this);
      };
      window.getComputedStyle=function(node,...args){
        styleReads.set(node,(styleReads.get(node)||0)+1);return nativeStyle.call(window,node,...args);
      };
      try{
        const a=_messageWindowSnapshot();
        const maxRect=Math.max(...rectReads.values()),maxStyle=Math.max(...styleReads.values());
        const top=c.scrollTop;c.scrollTop+=50;
        rectReads.clear();styleReads.clear();
        const b=_messageWindowSnapshot();
        return {maxRect,maxStyle,indexDelta:b.sessionIndex-a.sessionIndex,
          scrollDelta:c.scrollTop-top,offsetDelta:b.offset-a.offset};
      }finally{
        Element.prototype.getBoundingClientRect=nativeRect;window.getComputedStyle=nativeStyle;
      }
    }""")
    assert result['maxRect'] == 1, result
    assert result['maxStyle'] == 1, result
    assert result['indexDelta'] == 1, result
    assert result['scrollDelta'] == 50, result
    assert abs(result['offsetDelta']) < 1, result
