"""A virtual row's height must not change when its next source mounts."""

from tests.test_scroll_owner_live_reconciliation import isolated_webui, page  # noqa: F401


def test_leading_reasoning_height_belongs_to_following_source(page):  # noqa: F811
    result = page.evaluate("""() => {
      const root=document.createElement('div');
      root.style.cssText='position:absolute;left:0;top:0;width:600px';
      document.body.appendChild(root);
      const node=(classes,height,idx)=>{
        const el=document.createElement('div');el.className=classes;
        el.style.cssText=`display:block;height:${height}px;min-height:0;margin:0;padding:0;border:0;box-sizing:border-box`;
        if(idx!==undefined)el.dataset.msgIdx=String(idx);
        root.appendChild(el);return el;
      };
      try{
        node('assistant-segment assistant-segment-worklog-source',0,0);
        node('tool-card-row transparent-event-row',35);
        const before=_measureMessageVirtualRow(root,{rawIdx:0});
        const thinking=node('thinking-card-row transparent-thinking-event',25);
        const next=node('assistant-segment',50,1);
        const tool=node('tool-card-row transparent-event-row',35);
        const withNeighbor=[0,1].map(rawIdx=>_measureMessageVirtualRow(root,{rawIdx}));
        thinking.remove();next.remove();tool.remove();
        const after=_measureMessageVirtualRow(root,{rawIdx:0});
        return {before,withNeighbor,after};
      }finally{root.remove();}
    }""")
    assert result == {'before': 35, 'withNeighbor': [35, 110], 'after': 35}
