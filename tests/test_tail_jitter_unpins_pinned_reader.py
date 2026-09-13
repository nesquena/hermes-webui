"""Behavioral regressions for pinned-reader browser tail jitter."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_issue4295_scroll_pin_reentry import _scroll_listener_raf_body

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_source(name: str) -> str:
    start = UI_JS.index(f"function {name}(")
    brace = UI_JS.index("{", start)
    depth = 0
    for index in range(brace, len(UI_JS)):
        if UI_JS[index] == "{":
            depth += 1
        elif UI_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : index + 1]
    raise AssertionError(f"function body not found: {name}")


def _message_scroll_listener_source() -> str:
    marker = "(function(){\n  const el=document.getElementById('messages');"
    start = UI_JS.index(marker, UI_JS.index("function _isMessageTailJitter"))
    end = UI_JS.index("\n})();", start) + len("\n})();")
    return UI_JS[start:end]


def _run_scroll_frames(samples: list[dict]) -> dict:
    constants = "\n".join(
        line
        for line in UI_JS.splitlines()
        if line.startswith("const MESSAGE_TAIL_JITTER_MAX_")
    )
    payload = {
        "body": _scroll_listener_raf_body(),
        "guard": constants + "\n" + _function_source("_isMessageTailJitter"),
        "samples": samples,
    }
    script = "const payload=" + json.dumps(payload) + ";\n" + r"""
const step = new Function(
  'el', '_lastScrollTop', '_lastMessageClientHeight', '_nearBottomCount',
  '_scrollPinned', '_messageUserUnpinned', '_newMessageCueVisible',
  '_scrollbarDragActive', '_recentMessageWheelIntent',
  '_recentMessageTouchScrollIntent', '_recentMessageKeyScrollIntent',
  '_recentNonMessageScrollIntent', '_recentMessageRenderArtifactWindow',
  '_cancelBottomSettle', '_clearNewMessageScrollCue',
  '_syncScrollToBottomCue', '_updateSessionStartJumpButton',
  '_isSessionEndlessScrollEnabled', '_messagesTruncated',
  '_loadOlderMessages', '_scheduleDeferredOlderMessagesLoad',
  '_setMessageScrollToBottom', 'window',
  payload.guard + '\n' + payload.body + `
return {_lastScrollTop,_lastMessageClientHeight,_nearBottomCount,
        _scrollPinned,_messageUserUnpinned};`
);
let state={_lastScrollTop:null,_lastMessageClientHeight:null,_nearBottomCount:0,
           _scrollPinned:true,_messageUserUnpinned:false};
const trace=[];
const noop=()=>{};
for(const el of payload.samples){
  const intent=el.intent||{};
  let cancels=0, writes=0;
  state=step(
    el, state._lastScrollTop, state._lastMessageClientHeight,
    state._nearBottomCount, state._scrollPinned, state._messageUserUnpinned,
    false, !!intent.scrollbar, ()=>!!intent.wheel, ()=>!!intent.touch,
    ()=>!!intent.key, ()=>!!intent.nonMessage, ()=>false,
    ()=>{cancels++;}, noop, noop, noop, ()=>false, false, noop, noop,
    ()=>{writes++;el.scrollTop=el.scrollHeight-el.clientHeight;},
    {_autoScrollFollow:true}
  );
  trace.push({state:{...state},cancels,writes,
              bottomDistance:el.scrollHeight-el.scrollTop-el.clientHeight});
}
console.log(JSON.stringify({state,trace}));
"""
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _run_deferred_scrollbar_drag() -> dict:
    constants = "\n".join(
        line
        for line in UI_JS.splitlines()
        if line.startswith("const MESSAGE_TAIL_JITTER_MAX_")
    )
    payload = {
        "guard": constants + "\n" + _function_source("_isMessageTailJitter"),
        "listener": _message_scroll_listener_source(),
    }
    script = "const payload=" + json.dumps(payload) + ";\n" + r"""
const elHandlers={};
const windowHandlers={};
const documentHandlers={};
const el={
  scrollTop:6500, scrollHeight:7000, clientHeight:500, clientWidth:800,
  addEventListener(type,handler){elHandlers[type]=handler;},
  contains(){return false;}, matches(){return false;},
};
const document={
  activeElement:null, visibilityState:'visible',
  getElementById(id){return id==='messages'?el:null;},
  addEventListener(type,handler){documentHandlers[type]=handler;},
};
const window={
  _autoScrollFollow:true,
  addEventListener(type,handler){windowHandlers[type]=handler;},
};
let nextRaf=1;
const rafs=new Map();
function requestAnimationFrame(callback){const id=nextRaf++;rafs.set(id,callback);return id;}
function cancelAnimationFrame(id){rafs.delete(id);}
function flushAnimationFrames(){
  const queued=[...rafs.values()];
  rafs.clear();
  for(const callback of queued) callback();
}
let _scrollbarDragActive=false;
let _scrollbarDragIntentQueued=false;
let _messageScrollInputGeneration=0;
let _messageJumpScrollOwner=null;
let _lastScrollTop=6500;
let _lastMessageClientHeight=500;
let _nearBottomCount=0;
let _scrollPinned=true;
let _messageUserUnpinned=false;
let _newMessageCueVisible=false;
let _lastMessageKeyScrollIntentMs=-Infinity;
let _lastMessageScrollIntentMs=-Infinity;
const performance={now(){return 1000;}};
const noop=()=>{};
const _scheduleMessageVirtualizedRender=noop;
const _scheduleMessageJumpScrollReconcile=noop;
const _freshProgrammaticScrollActive=()=>false;
const _markMessageVirtualScrollActive=noop;
const _cancelBottomSettle=noop;
const _clearNewMessageScrollCue=noop;
const _syncScrollToBottomCue=noop;
const _updateSessionStartJumpButton=noop;
const _isSessionEndlessScrollEnabled=()=>false;
const _messagesTruncated=false;
const _loadOlderMessages=noop;
const _scheduleDeferredOlderMessagesLoad=noop;
const _setMessageScrollToBottom=noop;
const _recentMessageRenderArtifactWindow=()=>false;
const _recentMessageTouchScrollIntent=()=>false;
const _recentNonMessageScrollIntent=()=>false;
const _recentMessageWheelIntent=()=>false;
const _recentMessageKeyScrollIntent=()=>false;
eval(payload.guard);
eval(payload.listener);

elHandlers.pointerdown({target:el,offsetX:el.clientWidth});
el.scrollTop=6492;
elHandlers.scroll();
const queuedBeforePointerUp=rafs.size;
windowHandlers.pointerup();
const dragActiveBeforeFlush=_scrollbarDragActive;
flushAnimationFrames();
console.log(JSON.stringify({
  queuedBeforePointerUp, dragActiveBeforeFlush,
  _messageUserUnpinned, _scrollPinned,
}));
"""
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_stream_growth_and_no_input_tail_jitter_keep_runtime_pin_stable():
    """Streaming growth re-anchors once; an 8px browser drift never unpins."""
    growth = _run_scroll_frames(
        [
            {"scrollTop": 6500, "scrollHeight": 7000, "clientHeight": 500},
            {"scrollTop": 6500, "scrollHeight": 7400, "clientHeight": 500},
            {"scrollTop": 6500, "scrollHeight": 7000, "clientHeight": 500},
        ]
    )
    assert growth["trace"][1]["writes"] == 1
    assert sum(frame["writes"] for frame in growth["trace"]) == 1
    assert growth["trace"][-1]["bottomDistance"] == 0
    assert growth["state"]["_scrollPinned"] is True
    assert growth["state"]["_messageUserUnpinned"] is False

    jitter = _run_scroll_frames(
        [
            {"scrollTop": 6500, "scrollHeight": 7000, "clientHeight": 500},
            {"scrollTop": 6492, "scrollHeight": 7000, "clientHeight": 500},
        ]
    )
    assert jitter["trace"][1]["cancels"] == 0
    assert jitter["state"]["_scrollPinned"] is True
    assert jitter["state"]["_messageUserUnpinned"] is False


def test_scrollbar_drag_intent_survives_pointerup_before_scroll_frame():
    """The queued scroll keeps drag ownership after pointerup clears the live flag."""
    result = _run_deferred_scrollbar_drag()
    assert result["queuedBeforePointerUp"] == 1
    assert result["dragActiveBeforeFlush"] is False
    assert result["_messageUserUnpinned"] is True
    assert result["_scrollPinned"] is False


@pytest.mark.parametrize("reset", ["_resetScrollDirectionTracker", "_resetStreamScrollFollow"])
def test_scrollbar_drag_intent_latch_is_cleared_by_scroll_ownership_resets(reset):
    source = _function_source(reset)
    assert "_scrollbarDragActive=false;" in source
    assert "_scrollbarDragIntentQueued=false;" in source


@pytest.mark.parametrize("intent", ["wheel", "touch", "key", "scrollbar", "nonMessage"])
def test_genuine_input_bypasses_tail_jitter_and_unpins_at_runtime(intent):
    """Every real input detector must affect the observable listener state."""
    result = _run_scroll_frames(
        [
            {"scrollTop": 6500, "scrollHeight": 7000, "clientHeight": 500},
            {
                "scrollTop": 6492,
                "scrollHeight": 7000,
                "clientHeight": 500,
                "intent": {intent: True},
            },
        ]
    )
    assert result["trace"][1]["cancels"] == 1
    assert result["state"]["_scrollPinned"] is False
    assert result["state"]["_messageUserUnpinned"] is True
