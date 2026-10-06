"""Transparent rows must not acquire the compact worklog's whole-turn window."""
import json

import pytest

from tests.test_issue500_message_list_virtualization import (
    NODE, UI_JS_PATH, _extract_func_script, _run_node,
)

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.mark.parametrize("turn_size", [55, 660])
@pytest.mark.parametrize("busy", [False, True])
def test_transparent_window_bounds_long_turns_and_retains_reader(turn_size, busy):
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
const MESSAGE_VIRTUAL_THRESHOLD_ROWS=80, MESSAGE_VIRTUAL_BUFFER_PX=900;
let _messageVirtualEstimatedRowHeight=100;
let _messageVirtualHeightCache=[];
const window={_virtualizeTranscript:true};
const S={busy:BUSY};
const $=()=>({scrollTop:0,clientHeight:900});
const chatActivityMode=()=> 'transparent_stream';
const _syncMessageVirtualHeightCache=()=>{};
const _messageVirtualDefaultHeightForRole=()=>100;
let reader;
const _messageWindowReader=()=>reader;
eval(extractFunc('_messageVirtualRoleForEntry'));
eval(extractFunc('_messageVirtualWindow'));
eval(extractFunc('_currentMessageVirtualWindow'));
const entries=[];
for(let turn=0;turn<Math.ceil(660/TURN_SIZE);turn++){
  entries.push({m:{role:'user'},rawIdx:entries.length});
  for(let i=0;i<TURN_SIZE;i++) entries.push({m:{role:'assistant',content:'source '+entries.length},rawIdx:entries.length});
}
_messageVirtualHeightCache=entries.map(()=>100);
const results=[];
for(const index of [20,150,330,600]){
  reader={index,offset:-37,height:100};
  const w=_currentMessageVirtualWindow(entries,40);
  const mounted=entries.slice(w.start,w.end).concat(entries.slice(Math.max(w.end,w.tailStart)));
  results.push({count:mounted.length, retained:mounted.includes(entries[index]),
    totalHeight:w.topPad+w.bottomPad+mounted.length*100,
    expectedHeight:entries.length*100});
}
console.log(JSON.stringify(results));
""".replace("TURN_SIZE", str(turn_size)).replace("BUSY", str(busy).lower())
    for result in json.loads(_run_node(source)):
        assert result["count"] < 190, result
        assert result["retained"], result
        assert result["totalHeight"] == result["expectedHeight"], result
