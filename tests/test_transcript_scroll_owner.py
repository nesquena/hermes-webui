"""Behavioral geometry contracts for the bounded transcript window owner."""
import json
from tests.test_issue500_message_list_virtualization import _run_node, _extract_func_script, UI_JS_PATH


def test_window_uses_the_measured_reader_not_a_stale_scroll_estimate():
    source = _extract_func_script(UI_JS_PATH.read_text()) + """
eval(extractFunc('_messageVirtualWindow'));
const MESSAGE_VIRTUAL_THRESHOLD_ROWS=80, MESSAGE_VIRTUAL_BUFFER_PX=900;
function _messageVirtualDefaultHeightForRole(){return 140;}
const result=_messageVirtualWindow({total:1000, scrollTop:50000,
 viewportHeight:700, heights:Array(1000).fill(140), keepTailCount:50,
 reader:{index:60,offset:-300}});
console.log(JSON.stringify(result));
"""
    window = json.loads(_run_node(source))
    assert window['start'] <= 60 < window['end']
    assert window['end'] - window['start'] < 30
    assert window['bottomPad'] > 0


def test_keyed_commit_retains_reader_node_and_pixel_offset():
    source = _extract_func_script(UI_JS_PATH.read_text()) + r"""
const assert=require('assert');
let _messageWindowRevision=0;
const S={session:{session_id:'one'}};
const container={scrollTop:1550,style:{},getBoundingClientRect(){return {top:0}}};
const $=()=>container;
function _messageRawIdxForSessionIndex(i){return i;}
function _rememberMessageWindowReader(){}
let _programmaticScroll=false,_programmaticScrollSetAt=0,_lastScrollTop=0;
function _deferClearProgrammaticScroll(){}
const performance={now:()=>100};
class Row {
 constructor(id,height){this.id='';this.dataset={sessionMsgIdx:String(id),messageAnchorKey:'m'+id,msgIdx:String(id)};
 this.height=height;this.parent=null;this.disconnected=0;this._messageWindowMarkup=this.outerHTML;}
 get outerHTML(){return 'row:'+this.dataset.sessionMsgIdx+':'+this.height;}
 matches(){return true;}
 querySelectorAll(){return [];}
 get parentElement(){return this.parent;}
 get isConnected(){return this.parent===target;}
 get nextElementSibling(){return this.parent.children[this.parent.children.indexOf(this)+1]||null;}
 getBoundingClientRect(){let top=-container.scrollTop;
 for(const n of this.parent.children){if(n===this)break;top+=n.height;}
 return {top,bottom:top+this.height,height:this.height};}
 remove(){this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null;this.disconnected++;}
}
class List {
 constructor(rows){this.children=rows;this.dataset={windowSession:'one'};for(const n of rows)n.parent=this;}
 get firstElementChild(){return this.children[0]||null;}
 insertBefore(n,c){if(n.parent)n.remove();const i=c?this.children.indexOf(c):this.children.length;this.children.splice(i,0,n);n.parent=this;}
 querySelectorAll(){return this.children;}
}
const target=new List(Array.from({length:50},(_,i)=>new Row(i,100)));
const reader=target.children[15], oldOffset=reader.getBoundingClientRect().top;
const staged=new List(Array.from({length:50},(_,i)=>new Row(i+5,100)));
eval(extractFunc('_messageWindowNodeKey'));
eval(extractFunc('_restoreMessageWindowReader'));
eval(extractFunc('_initializeMessageWindowOwnership'));
eval(extractFunc('_commitMessageWindow'));
_commitMessageWindow(target,staged,{node:reader,sessionIndex:15,key:'m15',offset:oldOffset},true);
assert.equal(reader.getBoundingClientRect().top,oldOffset);
assert.equal(reader.disconnected,0);
assert.equal(target.children.length,50);
assert.equal(target.children[10],reader);
assert.equal(_messageWindowRevision,1);
console.log('ok');
"""
    assert _run_node(source) == 'ok'
