"""Deterministic queued-writer ownership regression for #7617 (Node VM)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_issue500_message_list_virtualization import (
    UI_JS_PATH,
    _extract_func_script,
    _run_node,
)


def test_deferred_bottom_writers_respect_reader_ownership():
    source = _extract_func_script(UI_JS_PATH.read_text(encoding="utf-8")) + r"""
const assert=require('node:assert/strict');
const frames=[], timers=[];let now=1000;
const performance={now:()=>now};
const requestAnimationFrame=fn=>(frames.push(fn),frames.length);
const cancelAnimationFrame=()=>{};
const setTimeout=(fn,ms)=>(timers.push({fn,ms}),timers.length);
const clearTimeout=()=>{};
const el={clientHeight:400,scrollHeight:1000,_top:600,
 get scrollTop(){return this._top;},
 set scrollTop(value){this._top=Math.max(0,Math.min(value,this.scrollHeight-this.clientHeight));}};
const window={_autoScrollFollow:true};
const document={getElementById:id=>id==='messages'?el:{}};
const $=id=>document.getElementById(id);
const observers=[];
class ResizeObserver {
  constructor(fn){this.fn=fn;this.disconnected=false;observers.push(this);}
  observe(){} disconnect(){this.disconnected=true;}
  fire(){this.fn();}
}
let _messageUserUnpinned=false,_scrollPinned=true,_programmaticScroll=false;
let _programmaticScrollSetAt=0,_lastScrollTop=600,_lastMessageClientHeight=400,_nearBottomCount=2;
let _messageScrollInputGeneration=0,_bottomSettleToken=0,_settleRAF=0,_settleRO=null;
let _settleTimer=0,_settleFinalTimer=0,_programmaticScrollResetTimer=0;
let _messageJumpScrollOwner=null;
function _recentNonMessageScrollIntent(){return intent!=='';}
function _recentMessageTouchScrollIntent(){return intent==='touch';}
function _recentMessageScrollIntent(){return intent!=='';}
function _recentMessageWheelIntent(){return intent==='wheel';}
function _recentMessageKeyScrollIntent(){return intent==='key';}
function _messageBottomDistance(){return el.scrollHeight-el.scrollTop-el.clientHeight;}
let intent='';
// The pre-guard revision has no helper; its deferred callbacks run unguarded.
function _bottomFollowOwnsReader(){return true;}
for(const name of ['_deferClearProgrammaticScroll','_bottomFollowOwnsReader',
 '_setMessageScrollToBottom','_settleMessageScrollToBottom','_settleFinalScroll',
 'scrollIfPinned']) if(src.includes('function '+name+'(')) eval(extractFunc(name));
function reset(){
 frames.length=0;timers.length=0;observers.length=0;
 el.scrollHeight=1000;el.scrollTop=600;_lastScrollTop=600;
 _messageUserUnpinned=false;_scrollPinned=true;_programmaticScroll=false;
 _messageScrollInputGeneration=0;_bottomSettleToken=0;intent='';
}
function frame(){const q=frames.splice(0);q.forEach(fn=>fn());}
function timer(ms){const q=timers.filter(x=>x.ms===ms);q.forEach(x=>x.fn());}
function away(){el.scrollTop=320;/* scroll event may be hidden by the programmatic latch */}
function unchanged(label){assert.equal(el.scrollTop,320,label);}
// Programmatic reader movement is not a user-unpin flag; queued frame must
// still see that the current position is no longer the last follow write.
reset();_setMessageScrollToBottom();away();frame();unchanged('programmatic movement / frame');
// Wheel, key, and touch intent can precede the passive scroll event.
for(const kind of ['wheel','key','touch']){
 reset();_settleMessageScrollToBottom(false,true);intent=kind;away();
 observers[0].fire();frame();timer(300);timer(2000);
 unchanged(kind+' intent / deferred writers');
}
// ResizeObserver queues a frame while still pinned; ownership changes before
// that frame and its quiet/fallback timers execute.
reset();_settleMessageScrollToBottom(false,true);
observers[0].fire();away();frame();timer(300);timer(2000);
unchanged('stale queued resize frame and timers');
// An image grows after the reader leaves while an explicit settle is pending.
reset();_settleMessageScrollToBottom(false,true);away();
el.scrollHeight=1300;observers[0].fire();frame();timer(300);timer(2000);
unchanged('late image growth while unpinned');
// The same late growth must follow when the reader actually owns the tail.
reset();_settleMessageScrollToBottom(false,true);
el.scrollHeight=1300;observers[0].fire();frame();
assert.equal(el.scrollTop,900,'pinned resize follows tail');
el.scrollHeight=1450;timer(2000);
assert.equal(el.scrollTop,1050,'pinned fallback follows tail');
console.log('owner scenarios passed');
"""
    assert _run_node(source) == "owner scenarios passed"
