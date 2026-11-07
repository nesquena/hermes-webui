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
 contains(n){return this.children.includes(n);}
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


def test_commit_reindex_skips_display_only_rows_and_empty_indices():
    """The keyed commit's reindex must not fabricate canonical indices (gate af988ebd).

    The loop Number()-coerces data-session-msg-idx for every row. A display-only
    recovered prompt (#8519/#8543) carries an EMPTY value: Number('') is 0, and
    the row was re-stamped as canonical message 0 after an older-history load,
    letting a registered extension message action resolve onto the wrong
    message. Display-only rows and empty/non-canonical indices are skipped; the
    builder's raw index survives until the mirrored canonical row lands.
    """
    source = _extract_func_script(UI_JS_PATH.read_text()) + r"""
const assert=require('assert');
let _messageWindowRevision=0;
const S={session:{session_id:'one'}};
const container={scrollTop:0,style:{},getBoundingClientRect(){return {top:0}}};
const $=()=>container;
// Identity mapper: a canonical row's msgIdx must be restamped straight from
// its absolute session index.
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
 contains(n){return this.children.includes(n);}
 insertBefore(n,c){if(n.parent)n.remove();const i=c?this.children.indexOf(c):this.children.length;this.children.splice(i,0,n);n.parent=this;}
 querySelectorAll(){return this.children;}
}
const target=new List(Array.from({length:20},(_,i)=>new Row(i,100)));
// Canonical row whose raw msgIdx went stale on a prepend; the absolute session
// index is authoritative and must win the restamp.
const canonical=new Row(21,100);canonical.dataset.msgIdx='4';
// Display-only recovered prompt: EMPTY session index, displayOnly stamped, raw
// index kept by the builder. Must NOT be restamped to 0 (or anything else).
const recovered=new Row(99,100);
recovered.dataset={sessionMsgIdx:'',displayOnly:'1',messageAnchorKey:'m99',msgIdx:'99'};
const staged=new List(target.children.slice().concat([canonical,recovered]));
eval(extractFunc('_messageWindowNodeKey'));
eval(extractFunc('_restoreMessageWindowReader'));
eval(extractFunc('_initializeMessageWindowOwnership'));
eval(extractFunc('_commitMessageWindow'));
_commitMessageWindow(target,staged,{node:target.children[5],sessionIndex:5,key:'m5',offset:-500},true);
assert.equal(canonical.dataset.msgIdx,'21','canonical row must restamp from its session index');
assert.equal(recovered.dataset.msgIdx,'99','display-only row must keep its raw index');
assert.equal(recovered.dataset.sessionMsgIdx,'','display-only row stays non-canonical');
console.log('ok');
"""
    assert _run_node(source) == 'ok'
