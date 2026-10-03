"""Transparent event border boxes exclude their one-pixel layout margin."""
from tests.test_scroll_owner_live_reconciliation import isolated_webui, page  # noqa: F401


def test_virtual_measurement_includes_transparent_event_margins(page):  # noqa: F811
    result = page.evaluate("""() => {
      const root=document.createElement('div');
      root.style.cssText='position:absolute;width:600px;display:flex;flex-direction:column';
      document.body.appendChild(root);
      root.innerHTML='<div class="assistant-segment assistant-segment-worklog-source" data-msg-idx="0" style="height:0"></div>'+
        '<div class="tool-card-row transparent-event-row" style="height:27px;min-height:0;box-sizing:border-box"></div>'+
        '<div class="tool-card-row transparent-event-row" style="height:27px;min-height:0;box-sizing:border-box"></div>';
      try {
        const events=[...root.querySelectorAll('.transparent-event-row')];
        const borderBoxes=events.reduce((sum,node)=>sum+node.getBoundingClientRect().height,0);
        const expected=events.reduce((sum,node)=>sum+node.getBoundingClientRect().height+parseFloat(getComputedStyle(node).marginBottom),0);
        const legacy=eval('('+_measureMessageVirtualRow.toString().replace(
          'return height+(parseFloat(getComputedStyle(node).marginBottom)||0);','return height;')+')');
        return {measured:_measureMessageVirtualRow(root,{rawIdx:0}),legacy:legacy(root,{rawIdx:0}),expected,borderBoxes};
      } finally {root.remove();}
    }""")
    assert result['expected'] > result['borderBoxes']
    assert result['legacy'] == result['borderBoxes']
    assert result['legacy'] != result['expected']
    assert result['measured'] == result['expected']
