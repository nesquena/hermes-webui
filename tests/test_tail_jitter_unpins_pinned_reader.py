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


def _run_drag_probe(steps: list[dict]) -> dict:
    """Drive the REAL pointerdown/pointerup/scroll listeners (extracted whole from
    ui.js) through an explicit event ordering, with a controllable clock.

    This is the harness for the async-`scroll` defect: browsers dispatch `scroll`
    asynchronously, so a quick thumb drag can deliver
    pointerdown → scrollTop change → pointerup → scroll → rAF, and the scroll
    handler runs AFTER pointerup already cleared the live drag flag.
    """
    constants = "\n".join(
        line
        for line in UI_JS.splitlines()
        if line.startswith("const MESSAGE_TAIL_JITTER_MAX_")
    )
    drag_helpers = "\n".join(
        _function_source(name)
        for name in (
            "_markScrollbarDragIntent",
            "_clearScrollbarDragIntent",
            "_consumeScrollbarDragIntent",
            "_releaseScrollbarDragIntent",
        )
    )
    payload = {
        "guard": constants + "\n" + _function_source("_isMessageTailJitter"),
        "dragHelpers": drag_helpers,
        "resetHelpers": "\n".join(
            _function_source(name)
            for name in ("_resetScrollDirectionTracker", "_resetStreamScrollFollow")
        ),
        "listener": _message_scroll_listener_source(),
        "steps": steps,
    }
    script = "const payload=" + json.dumps(payload) + ";\n" + r"""
const elHandlers={};
const windowHandlers={};
const documentHandlers={};
const el={
  scrollTop:6500, scrollHeight:7000, clientHeight:500, clientWidth:800,
  addEventListener(type,handler){elHandlers[type]=handler;},
  contains(){return false;}, matches(){return false;},
  getBoundingClientRect(){return {left:0, top:0, right:el.clientWidth, bottom:500, width:el.clientWidth, height:500};},
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
let clockNow=1000;
const performance={now(){return clockNow;}};
let _scrollbarDragActive=false;
let _scrollbarDragIntentQueued=false;
let _scrollbarDragIntentUntil=-Infinity;
let _scrollbarDragObservedTop=null;
const SCROLLBAR_DRAG_INTENT_WINDOW_MS=250;
const SCROLLBAR_DRAG_EDGE_BAND_PX=20;
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
let _lastMessageWheelIntentMs=-Infinity;
let _lastMessageTouchScrollIntentMs=-Infinity;
let _messageTouchScrollActive=false;
let _touchStartY=null;
let _deferredOlderMessagesTimer=0;
const noop=()=>{};
const _scheduleMessageVirtualizedRender=noop;
const _scheduleMessageJumpScrollReconcile=noop;
const _freshProgrammaticScrollActive=()=>false;
const _markMessageVirtualScrollActive=noop;
const _cancelBottomSettle=noop;
const _cancelMessageJumpScroll=noop;
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
eval(payload.dragHelpers);
eval(payload.resetHelpers);
eval(payload.listener);

const snapshots={};
const snapshot=(key)=>{snapshots[key]={
  dragActive:_scrollbarDragActive,
  intentQueued:_scrollbarDragIntentQueued,
  intentUntil:_scrollbarDragIntentUntil,
  rafPending:rafs.size>0,
  pinned:_scrollPinned,
  unpinned:_messageUserUnpinned,
};};
for(const step of payload.steps){
  if(step.op==='pointerdown'){
    const target=step.child?{clientWidth:el.clientWidth}:el;
    const event={target};
    if(typeof step.offsetX==='number') event.offsetX=step.offsetX;
    if(typeof step.clientX==='number') event.clientX=step.clientX;
    elHandlers.pointerdown(event);
  }else if(step.op==='scrollTop'){ el.scrollTop=step.value; }
  else if(step.op==='seed'){
    _lastScrollTop=el.scrollTop;
    _lastMessageClientHeight=el.clientHeight;
  }
  else if(step.op==='reset'){
    if(step.name==='_resetScrollDirectionTracker') _resetScrollDirectionTracker();
    else if(step.name==='_resetStreamScrollFollow') _resetStreamScrollFollow();
    else throw new Error('unknown reset '+step.name);
  }
  else if(step.op==='blur'){ windowHandlers.blur(); }
  else if(step.op==='hidden'){
    document.visibilityState='hidden';
    documentHandlers.visibilitychange();
  }
  else if(step.op==='pointerup'){ windowHandlers.pointerup(); }
  else if(step.op==='pointercancel'){ windowHandlers.pointercancel(); }
  else if(step.op==='scroll'){ elHandlers.scroll(); }
  else if(step.op==='flush'){ flushAnimationFrames(); }
  else if(step.op==='advance'){ clockNow+=step.ms; }
  else if(step.op==='snapshot'){ snapshot(step.key); }
}
console.log(JSON.stringify({snapshots,
  state:{_scrollPinned,_messageUserUnpinned,_scrollbarDragIntentUntil}}));
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
    """Reproduces the maintainer's exact async-ordering defect (gate 7268).

    `scroll` is dispatched asynchronously, so a quick 3–16px thumb drag can be
    delivered as pointerdown → scrollTop change → pointerup → scroll → rAF.
    The old latch armed only when the SYNC scroll handler saw
    _scrollbarDragActive===true — already cleared by pointerup — so the drag was
    classified as tail jitter and the reader was silently re-pinned.
    """
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "scrollTop", "value": 6492},
            # Hold the thumb past the pointerdown stamp's window, so ONLY the
            # release re-stamp can carry intent into the late scroll event.
            {"op": "advance", "ms": 251},
            {"op": "pointerup"},
            {"op": "snapshot", "key": "beforeScroll"},  # scroll NOT yet delivered
            {"op": "scroll"},
            {"op": "snapshot", "key": "afterScroll"},
            {"op": "flush"},
        ]
    )
    before = result["snapshots"]["beforeScroll"]
    # pointerup ran before the scroll event: live flag already cleared…
    assert before["dragActive"] is False
    # …and, because the movement was still undelivered, release re-armed intent.
    assert before["intentUntil"] == 1251 + 250
    # …and the async scroll still arrives afterwards, queuing its classification.
    assert result["snapshots"]["afterScroll"]["dragActive"] is False
    assert result["snapshots"]["afterScroll"]["rafPending"] is True
    # The stamp survived the release, so the first classification owns the drag…
    assert result["state"]["_messageUserUnpinned"] is True
    assert result["state"]["_scrollPinned"] is False
    # The drag's own scroll consumed the re-armed intent: nothing remains.
    assert result["snapshots"]["afterScroll"]["intentUntil"] is None


def test_overlay_scrollbar_press_inside_client_box_still_unpins():
    """Overlay scrollbars (Firefox macOS thin — bug 1568939) sit INSIDE the
    client box, so their drags report offsetX < clientWidth. A right-edge press
    (offsetX === clientWidth - 1) must still claim drag ownership and unpin."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 799},  # overlay thumb, in-box
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["state"]["_messageUserUnpinned"] is True
    assert result["state"]["_scrollPinned"] is False


def test_scrollbar_press_outside_edge_band_does_not_bypass_jitter_guard():
    """Only the narrow right-edge band counts: a press well inside the client
    box arms nothing, so the same 8px drift is still classified as jitter and
    the pinned reader stays pinned (guard not weakened)."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 400},
            {"op": "scrollTop", "value": 6492},
            {"op": "pointerup"},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["state"]["_messageUserUnpinned"] is False
    assert result["state"]["_scrollPinned"] is True


def test_bubbled_child_pointerdown_never_claims_scrollbar_drag():
    """Transcript presses hit .messages-inner (target !== scroller) and must
    never claim drag ownership, edge band or not."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 799, "child": True},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["state"]["_messageUserUnpinned"] is False
    assert result["state"]["_scrollPinned"] is True


def test_drag_stamp_expires_when_no_scroll_follows_the_drag():
    """The stamp is bounded in time: a stale press (> window) must not let a
    LATER unrelated tail nudge bypass the jitter guard."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "pointerup"},
            {"op": "advance", "ms": 251},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["state"]["_messageUserUnpinned"] is False
    assert result["state"]["_scrollPinned"] is True


def test_drag_stamp_is_consumed_once_and_never_leaks_to_a_later_scroll():
    """After the drag's scroll consumed the stamp, an UNRELATED later tail nudge
    (e.g. layout settle of the forced post-drag re-render) is still classified
    as jitter — the stamp never leaks into a second classification."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "pointerup"},
            {"op": "snapshot", "key": "afterDragScroll"},
            {"op": "scrollTop", "value": 6485},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "snapshot", "key": "afterLaterNudge"},
        ]
    )
    # First classification owned the drag…
    assert result["snapshots"]["afterDragScroll"]["unpinned"] is True
    # …and the unrelated later nudge consumed/cleared the stamp instead of
    # inheriting it: by the end of the sequence no drag intent is pending
    # (JSON renders the cleared -Infinity marker as null).
    assert result["snapshots"]["afterLaterNudge"]["intentUntil"] is None


@pytest.mark.parametrize("release", ["pointerup", "pointercancel"])
def test_release_after_delivered_drag_scroll_does_not_rearm_intent(release):
    """Drag scroll delivered BEFORE release: the movement was already
    classified, so release must clear the intent instead of opening a fresh
    window that the next (non-drag) scroll would consume."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": release},
            {"op": "snapshot", "key": "afterRelease"},
        ]
    )
    assert result["snapshots"]["afterRelease"]["unpinned"] is True
    assert result["snapshots"]["afterRelease"]["intentUntil"] is None


@pytest.mark.parametrize("release", ["pointerup", "pointercancel"])
def test_render_nudge_after_drag_back_to_tail_keeps_reader_pinned(release):
    """Maintainer regression (gate 7268, round 3): drag up, drag back to the
    tail (re-pinned), release, then a render/layout-generated tail nudge. The
    release must not hand the nudge a stale drag intent, so the nudge is still
    classified as tail jitter and the reader stays pinned."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "scrollTop", "value": 6000},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "snapshot", "key": "draggedAway"},
            {"op": "scrollTop", "value": 6300},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "scrollTop", "value": 6500},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "snapshot", "key": "backAtTail"},
            {"op": release},
            {"op": "snapshot", "key": "released"},
            # Render-generated nudge (no input) after the release.
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["snapshots"]["draggedAway"]["unpinned"] is True
    assert result["snapshots"]["backAtTail"]["pinned"] is True
    assert result["snapshots"]["backAtTail"]["unpinned"] is False
    assert result["snapshots"]["released"]["intentUntil"] is None
    assert result["state"]["_scrollPinned"] is True
    assert result["state"]["_messageUserUnpinned"] is False


def test_queued_drag_back_at_true_bottom_does_not_own_later_render_nudge():
    """Maintainer regression (gate 7268, round 4): both drag scroll events are
    delivered before release but their shared classification frame is still
    queued. Returning to the true bottom must discard that queued ownership so
    an 8px render nudge before rAF remains browser jitter, not reader intent."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "scrollTop", "value": 6500},
            {"op": "scroll"},
            {"op": "snapshot", "key": "backAtTailBeforeRelease"},
            {"op": "pointerup"},
            {"op": "snapshot", "key": "releasedAtTail"},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["snapshots"]["backAtTailBeforeRelease"]["intentQueued"] is True
    assert result["snapshots"]["releasedAtTail"]["intentQueued"] is False
    assert result["state"]["_scrollPinned"] is True
    assert result["state"]["_messageUserUnpinned"] is False


def test_queued_drag_that_leaves_true_bottom_still_unpins():
    """A final upward thumb move that is still awaiting its scroll event at
    release remains genuine drag intent, even when the last delivered position
    was the true bottom."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "scrollTop", "value": 6500},
            {"op": "scroll"},
            {"op": "scrollTop", "value": 6492},  # late movement, not delivered yet
            {"op": "advance", "ms": 251},
            {"op": "pointerup"},
            {"op": "snapshot", "key": "releasedAboveTail"},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["snapshots"]["releasedAboveTail"]["intentQueued"] is True
    assert result["snapshots"]["releasedAboveTail"]["intentUntil"] == 1251 + 250
    assert result["state"]["_scrollPinned"] is False
    assert result["state"]["_messageUserUnpinned"] is True


def test_scrollbar_click_without_movement_leaves_no_intent_for_render_nudge():
    """A press/release on the scrollbar that never moved scrollTop has no drag
    scroll to own: release clears the pointerdown stamp, and a render nudge
    right after cannot consume it."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "pointerup"},
            {"op": "snapshot", "key": "released"},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["snapshots"]["released"]["intentUntil"] is None
    assert result["state"]["_scrollPinned"] is True
    assert result["state"]["_messageUserUnpinned"] is False


def test_pending_drag_scroll_after_release_owns_intent_then_render_nudge_cannot():
    """Drag scroll delivered AFTER release: the re-armed intent is consumed by
    that drag scroll only; once the reader is back at the tail and re-pinned, a
    later render nudge is classified as jitter again."""
    result = _run_drag_probe(
        [
            {"op": "pointerdown", "offsetX": 800},
            {"op": "scrollTop", "value": 6492},
            {"op": "advance", "ms": 251},
            {"op": "pointercancel"},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "snapshot", "key": "dragClassified"},
            # Reader returns to the tail (re-pins after two downward frames).
            {"op": "scrollTop", "value": 6496},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "scrollTop", "value": 6500},
            {"op": "scroll"},
            {"op": "flush"},
            {"op": "snapshot", "key": "repinned"},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    assert result["snapshots"]["dragClassified"]["unpinned"] is True
    assert result["snapshots"]["dragClassified"]["intentUntil"] is None
    assert result["snapshots"]["repinned"]["pinned"] is True
    assert result["state"]["_scrollPinned"] is True
    assert result["state"]["_messageUserUnpinned"] is False


@pytest.mark.parametrize("reset", ["_resetScrollDirectionTracker", "_resetStreamScrollFollow"])
@pytest.mark.parametrize("pending_intent", ["stamp", "queued_frame"])
def test_scroll_ownership_resets_prevent_drag_intent_leaking(reset, pending_intent):
    """Session/stream ownership changes discard both undelivered drag stamps and
    drag intent already queued for classification. A no-input tail nudge in the
    new owner must therefore remain jitter instead of unpinning the reader."""
    steps = [{"op": "pointerdown", "offsetX": 800}]
    if pending_intent == "queued_frame":
        steps.extend(
            [
                {"op": "scrollTop", "value": 6492},
                {"op": "scroll"},  # queues old owner's drag classification
            ]
        )
    steps.extend(
        [
            {"op": "reset", "name": reset},
            # Session loading/programmatic placement seeds the new owner's tail.
            {"op": "scrollTop", "value": 6500},
            {"op": "seed"},
            # Browser-only 8px layout nudge: no scrollbar input in this owner.
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    result = _run_drag_probe(steps)
    assert result["state"]["_scrollPinned"] is True
    assert result["state"]["_messageUserUnpinned"] is False


@pytest.mark.parametrize("abort", ["blur", "hidden"])
@pytest.mark.parametrize("pending_intent", ["stamp", "queued_frame"])
def test_focus_loss_discards_drag_intent_before_later_tail_nudge(abort, pending_intent):
    """Losing focus aborts scrollbar ownership, including an undelivered stamp
    or drag intent already queued for rAF classification. A later no-input tail
    nudge must stay pinned rather than inheriting the abandoned drag."""
    steps = [{"op": "pointerdown", "offsetX": 800}]
    if pending_intent == "queued_frame":
        steps.extend(
            [
                {"op": "scrollTop", "value": 6492},
                {"op": "scroll"},
            ]
        )
    steps.extend(
        [
            {"op": abort},
            {"op": "scrollTop", "value": 6500},
            {"op": "seed"},
            {"op": "scrollTop", "value": 6492},
            {"op": "scroll"},
            {"op": "flush"},
        ]
    )
    result = _run_drag_probe(steps)
    assert result["state"]["_scrollPinned"] is True
    assert result["state"]["_messageUserUnpinned"] is False


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
