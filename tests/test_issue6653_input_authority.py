"""#6653 port: the layout-free follow-intent cache must be fed by the #8070
input-tail ownership instead of living alongside the old scroll handler.

`_messageFollowIntentCache` exists so a SYNCHRONOUS reader (the SSE cancel
handler in `static/messages.js`) can ask "was this reader following the tail?"
without forcing a `scrollHeight` layout on WKWebView and without reading the
rAF-deferred `_scrollPinned` flag. Its value is only as good as the evidence
that flips it, so every invalidation has to happen where upstream already
decides that a reader input actually governs the transcript scroll surface:

- `_isTranscriptScrollTarget(target, el, dir)` is upstream's authority for
  "this wheel/touch/key gesture scrolls the transcript, not a nested pane"
  (#7494). The input-tail capture is minted inside that gate; the follow-intent
  invalidation belongs to the SAME gate and the SAME reader input, in the up
  direction (`dir < 0`) — a downward gesture scrolls toward the bottom and must
  never cancel a follower.
- a scrollbar drag is input on the transcript itself, and its direction is only
  observable as movement, so it is measured cumulatively against the scrollTop
  baseline captured at pointerdown (the per-event deltas are swallowed by the
  `_programmaticScroll` latch when the drag starts inside the suppression
  window).
- a programmatic scroll (`scrollToBottom()`, settle, re-anchor) is never reader
  input: it must never invalidate the cache, and the automatic writers must
  never re-arm a false cache (stickiness).

Each case below drives the REAL production bodies extracted from `static/ui.js`
(`_isTranscriptScrollTarget`, `_captureMessageScrollInputTail`,
`_recordNonMessageScrollIntent`, the keydown listener, the scroll listener,
`_setMessageScrollToBottom`, `_reanchorPinnedTailAfterRender`). No production
logic is re-implemented in the harness.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _balanced(src: str, brace: int) -> str:
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace : i + 1]
    raise AssertionError("unbalanced braces")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"function {name}")
    brace = src.index("{", start)
    return src[start : brace + len(_balanced(src, brace))]


def _arrow_body(src: str, name: str) -> str:
    start = src.index(f"const {name}=")
    brace = src.index("{", src.index("=>", start))
    return src[start : brace + len(_balanced(src, brace))] + ";"


def _listener_body(src: str, marker: str) -> str:
    start = src.index(marker)
    return _balanced(src, src.index("{", start))


_RECORD = _function_body(UI_JS, "_recordNonMessageScrollIntent")
_TARGET = _function_body(UI_JS, "_isTranscriptScrollTarget")
_CAPTURE = _function_body(UI_JS, "_captureMessageScrollInputTail")
_KEY_INTERACTIVE = _arrow_body(UI_JS, "_isMessageInteractiveKeyTarget")
_KEYDOWN = _listener_body(UI_JS, "document.addEventListener('keydown',(e)=>{")
_SCROLL = _listener_body(UI_JS, "el.addEventListener('scroll',()=>{")
_SET_BOTTOM = _function_body(UI_JS, "_setMessageScrollToBottom")
_REANCHOR = _function_body(UI_JS, "_reanchorPinnedTailAfterRender")

_KEYS_START = UI_JS.index("const _MESSAGE_SCROLL_KEYS=new Set([")
_KEYS_DECL = UI_JS[_KEYS_START : UI_JS.index(";", _KEYS_START) + 1]

_PRELUDE = r"""
'use strict';
// ---- module state the production bodies close over (declared here so the test
// exercises the bodies, not the declarations) ----
let T = { now: 1000 };
const performance = { now: () => T.now };
let _scrollRaf = 0;
let FRESH = false;      // _freshProgrammaticScrollActive() answer
let HOVER = true;       // el.matches(':hover')
let _messageFollowIntentCache = true;
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _nearBottomCount = 0;
let _lastScrollTop = 4200;
let _lastMessageClientHeight = 800;
let _lastMessageScrollHeight = 5000;
let _lastNonMessageScrollIntentMs = -Infinity;
let _lastMessageWheelIntentMs = -Infinity;
let _lastMessageScrollIntentMs = -Infinity;
let _lastMessageKeyScrollIntentMs = -Infinity;
let _lastMessageRenderAt = -Infinity;
let _messageTouchScrollActive = false;
let _lastMessageTouchScrollIntentMs = -Infinity;
let _touchStartY = null;
let _messageScrollInputGeneration = 0;
let _messageScrollInputTailGeneration = 0;
let _messageScrollInputTailHeight = null;
let _messageScrollInputTailConsumedGeneration = 0;
let _messageJumpScrollOwner = null;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _scrollbarDragActive = false;
let _scrollbarDragBaselineScrollTop = null;

// ---- fake #messages scroller ----
const el = {
  scrollTop: 4200, scrollHeight: 5000, clientHeight: 800, parentElement: null,
  matches: () => HOVER,
  contains: (n) => n === el || !!(n && n._inEl),
};
const document = {
  getElementById: (id) => (id === 'messages' ? el : null),
  activeElement: null,
  body: { tagName: 'BODY' },
  documentElement: { tagName: 'HTML' },
};
function $(id) { return document.getElementById(id); }
function getComputedStyle(n) { return (n && n._cs) || { overflowY: '', overscrollBehaviorY: '' }; }

// ---- stubs for neighbours the bodies call ----
function _cancelBottomSettle() {}
function _deferClearProgrammaticScroll() { _programmaticScroll = false; }
function _scheduleMessageVirtualizedRender() {}
function _scheduleMessageVirtualizedRenderNow() {}
function _markMessageVirtualScrollActive() {}
function _scheduleMessageJumpScrollReconcile() {}
function _cancelMessageJumpScroll() {}
function _clearNewMessageScrollCue() {}
function _markMessageTouchScrollIntent(active = true) {
  _messageTouchScrollActive = !!active;
  _lastMessageTouchScrollIntentMs = performance.now();
}
function _recentMessageTouchScrollIntent() {
  return _messageTouchScrollActive || performance.now() - _lastMessageTouchScrollIntentMs < 1200;
}
function _recentMessageWheelIntent() { return performance.now() - _lastMessageWheelIntentMs < 1200; }
function _recentMessageKeyScrollIntent() { return performance.now() - _lastMessageKeyScrollIntentMs < 1200; }
function _recentNonMessageScrollIntent() { return performance.now() - _lastNonMessageScrollIntentMs < 350; }
function _recentMessageRenderArtifactWindow() { return false; }
function _freshProgrammaticScrollActive() { return FRESH; }
function requestAnimationFrame(cb) { return 1; } // Deferred as in the browser; these cases inspect synchronous input only.
function cancelAnimationFrame() {}
function scrollToBottom() {}
"""

_HARNESS = (
    _PRELUDE
    + "\n// ---- REAL production bodies (extracted from static/ui.js) ----\n"
    + _CAPTURE
    + "\n"
    + _TARGET
    + "\n"
    + _RECORD
    + "\n"
    + _KEYS_DECL
    + "\n"
    + _KEY_INTERACTIVE
    + "\nconst _onKeydown = (e) => "
    + _KEYDOWN
    + ";\nconst _onScroll = () => "
    + _SCROLL
    + ";\n"
    + _SET_BOTTOM
    + "\n"
    + _REANCHOR
    + "\n// ---- scenario ----\n"
    + r"""
const pane = {
  scrollTop: 500, scrollHeight: 2000, clientHeight: 500, parentElement: el,
  _cs: { overflowY: 'auto', overscrollBehaviorY: '' },
};
const paneTarget = { parentElement: pane, _inEl: true };
const directTarget = { parentElement: el, _inEl: true };
// A widget OUTSIDE the transcript that can own the keyboard (tree row, palette,
// drawer): focus there means the key is consumed off-pane (#6653 gate).
const treeRow = { tagName: 'LI', parentElement: null, _inEl: false, closest: () => null };
function out(obj) { console.log(JSON.stringify(obj)); }
"""
)


def _run(scenario: str) -> dict:
    source = _HARNESS + scenario
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_wheel_up_on_the_transcript_invalidates_follow_intent_synchronously():
    """A wheel-up over the transcript is authoritative reader input: the cache
    must flip on the input event itself, not on the next scroll rAF, or a cancel
    landing in that window still reads "following" and yanks the reader back."""
    m = _run(
        "FRESH = false;\n"
        "_recordNonMessageScrollIntent({type:'wheel', deltaY:-40, target:directTarget});\n"
        "out({cache:_messageFollowIntentCache, gen:_messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 1, "the transcript wheel-up must mint input-tail authority (#7494)"
    assert m["cache"] is False, (
        "an upward wheel on the transcript must invalidate the follow-intent cache "
        "synchronously in the same input handler that captures the tail (#6653)"
    )


def test_wheel_down_on_the_transcript_keeps_follow_intent():
    """Control: paging DOWN scrolls toward the bottom, so a follower stays a
    follower — and the input tail is still captured for re-pinning."""
    m = _run(
        "FRESH = false;\n"
        "_recordNonMessageScrollIntent({type:'wheel', deltaY:40, target:directTarget});\n"
        "out({cache:_messageFollowIntentCache, gen:_messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 1, "a downward wheel still captures the input tail (re-pin authority)"
    assert m["cache"] is True, (
        "a downward wheel must NOT invalidate follow-intent — it scrolls toward the bottom"
    )


def test_wheel_up_consumed_by_a_nested_pane_keeps_follow_intent():
    """Control (#7494 ownership): a wheel over a nested pane that can still
    scroll in that direction never moves the transcript, so it must not cancel a
    follower's follow-intent — and it must not mint re-pin authority either."""
    m = _run(
        "FRESH = false;\n"
        "_recordNonMessageScrollIntent({type:'wheel', deltaY:-40, target:paneTarget});\n"
        "out({cache:_messageFollowIntentCache, gen:_messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 0, "a nested-pane-consumed gesture must not capture the input tail"
    assert m["cache"] is True, (
        "a scroll-up consumed by a nested pane does not move the transcript, so it must "
        "not invalidate the transcript follow-intent cache"
    )


def test_scrollbar_drag_up_invalidates_while_the_programmatic_latch_is_fresh():
    """The drag case the latch swallows: `scrollToBottom()` set the programmatic
    latch, the reader drags the scrollbar up inside that window, and the
    per-event deltas are skipped by the early return. The cumulative drag
    distance against the pointerdown baseline is still reader input, so the
    cache must be invalidated BEFORE the guards."""
    m = _run(
        "_programmaticScroll = true; _programmaticScrollSetAt = performance.now();\n"
        "FRESH = true;                    // latch is fresh: the listener returns early\n"
        "_scrollbarDragActive = true;\n"
        "_scrollbarDragBaselineScrollTop = 4300;\n"
        "el.scrollTop = 4200;             // thumb dragged up 100px\n"
        "_onScroll();\n"
        "out({cache:_messageFollowIntentCache});\n"
    )
    assert m["cache"] is False, (
        "a scrollbar drag measured against the pointerdown baseline must invalidate the "
        "follow-intent cache even while the programmatic latch is fresh (#6653)"
    )


def test_scrollbar_drag_down_keeps_follow_intent():
    m = _run(
        "FRESH = true;\n"
        "_scrollbarDragActive = true;\n"
        "_scrollbarDragBaselineScrollTop = 4200;\n"
        "el.scrollTop = 4400;             // thumb dragged down\n"
        "_onScroll();\n"
        "out({cache:_messageFollowIntentCache});\n"
    )
    assert m["cache"] is True, "a downward scrollbar drag must not cancel follow-intent"


def test_scrollbar_drag_back_to_the_tail_rearms_follow_intent_while_the_latch_is_fresh():
    """The reverse of the drag-up case: a live token's follow write keeps the
    programmatic latch fresh while the reader drags up and then back to the
    tail. The return must reach the listener's tail reconciliation (its rAF),
    or the cache stays false and auto-follow is silently off for the rest of
    the stream."""
    m = _run(
        "const _rafQueue = [];\n"
        "requestAnimationFrame = (cb) => { _rafQueue.push(cb); return _rafQueue.length; };\n"
        "// neighbours the listener's rAF reconciliation calls\n"
        "globalThis.window = { _autoScrollFollow: true };\n"
        "Object.assign(globalThis, { _messagesTruncated: false, _newMessageCueVisible: false,\n"
        "  _isSessionEndlessScrollEnabled: () => false, _loadOlderMessages() {},\n"
        "  _scheduleDeferredOlderMessagesLoad() {}, _syncScrollToBottomCue() {},\n"
        "  _updateSessionStartJumpButton() {} });\n"
        "_programmaticScroll = true; _programmaticScrollSetAt = performance.now();\n"
        "FRESH = true;                    // a token's follow write keeps the latch fresh\n"
        "_scrollbarDragActive = true;\n"
        "_scrollbarDragBaselineScrollTop = 4200;\n"
        "el.scrollTop = 4100;             // thumb dragged up 100px\n"
        "_onScroll();\n"
        "const afterUp = _messageFollowIntentCache;\n"
        "T.now += 16; _rafQueue.splice(0).forEach((cb) => cb());\n"
        "// a real drag back emits a scroll event per step\n"
        "el.scrollTop = 4150; _onScroll(); T.now += 16; _rafQueue.splice(0).forEach((cb) => cb());\n"
        "el.scrollTop = 4200;             // thumb back at the tail\n"
        "_onScroll();\n"
        "T.now += 16; _rafQueue.splice(0).forEach((cb) => cb());\n"
        "out({afterUp, cache:_messageFollowIntentCache, pinned:_scrollPinned, unpinned:_messageUserUnpinned});\n"
    )
    assert m["afterUp"] is False, "the upward drag must still invalidate the cache (#6653)"
    assert m["cache"] is True and m["pinned"] is True and m["unpinned"] is False, (
        "dragging the scrollbar back to the tail while the programmatic latch is fresh "
        f"must re-arm following (#6653): {m}"
    )


def test_programmatic_scroll_never_invalidates_follow_intent():
    """Control: no drag, no reader input, latch fresh — a programmatic scroll
    must leave the cache alone, or a genuine follower silently stops following."""
    m = _run(
        "_programmaticScroll = true; _programmaticScrollSetAt = performance.now();\n"
        "FRESH = true;\n"
        "_lastScrollTop = 4300;\n"
        "el.scrollTop = 4100;             // upward delta, but nobody touched the pane\n"
        "_onScroll();\n"
        "out({cache:_messageFollowIntentCache});\n"
    )
    assert m["cache"] is True, (
        "a programmatic scroll must never invalidate follow-intent — only reader input may"
    )


def test_page_up_on_the_transcript_invalidates_follow_intent():
    m = _run(
        "document.activeElement = el; HOVER = true;\n"
        "_onKeydown({key:'PageUp', target:el, shiftKey:false});\n"
        "out({cache:_messageFollowIntentCache, gen:_messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 1, "PageUp on the transcript must capture the input tail"
    assert m["cache"] is False, "PageUp scrolls up: it must invalidate follow-intent"


def test_page_up_consumed_by_a_nested_pane_keeps_follow_intent():
    """Control (#7494 ownership): with focus in a nested pane that consumes the
    key, the transcript never scrolls — so the cache must stay armed. An
    unconditional keydown invalidation would cancel a follower reading a tool
    output pane."""
    m = _run(
        "document.activeElement = paneTarget; HOVER = true;\n"
        "_onKeydown({key:'PageUp', target:paneTarget, shiftKey:false});\n"
        "out({cache:_messageFollowIntentCache, gen:_messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 0, "a nested-pane-consumed key must not capture the input tail"
    assert m["cache"] is True, (
        "a PageUp consumed by a nested pane must not invalidate the transcript "
        "follow-intent cache"
    )


def test_shift_space_pages_up_and_plain_space_does_not():
    """Shift+Space is a page-up in the browser; plain Space is page-down."""
    up = _run(
        "document.activeElement = el; HOVER = true;\n"
        "_onKeydown({key:' ', target:el, shiftKey:true});\n"
        "out({cache:_messageFollowIntentCache});\n"
    )
    assert up["cache"] is False, "Shift+Space pages up: it must invalidate follow-intent"
    down = _run(
        "document.activeElement = el; HOVER = true;\n"
        "_onKeydown({key:' ', target:el, shiftKey:false});\n"
        "out({cache:_messageFollowIntentCache});\n"
    )
    assert down["cache"] is True, "plain Space pages down: it must not cancel a follower"


def test_false_cache_blocks_every_automatic_writer_and_stays_sticky():
    """End-to-end contract of the cache itself: with a false cache, the streaming
    follow writer and the post-render re-anchor must not move the reader and must
    not re-arm the cache."""
    m = _run(
        "_messageFollowIntentCache = false;\n"
        "el.scrollTop = 4000;\n"
        "_setMessageScrollToBottom();\n"
        "const afterSet = {top: el.scrollTop, cache: _messageFollowIntentCache, prog: _programmaticScroll};\n"
        "el.scrollTop = 3820;\n"
        "_reanchorPinnedTailAfterRender(true);\n"
        "out({afterSet, top: el.scrollTop, cache: _messageFollowIntentCache});\n"
    )
    assert m["afterSet"]["top"] == 4000, (
        "an automatic follow write must not yank a reader whose follow-intent is false"
    )
    assert m["afterSet"]["cache"] is False, "the follow writer must not re-arm a false cache"
    assert m["top"] == 3820, (
        "the post-render re-anchor must not move a reader whose follow-intent is false"
    )
    assert m["cache"] is False, "the re-anchor must keep the false cache sticky"


def test_hovered_transcript_does_not_steal_a_key_owned_by_a_focused_widget():
    """#6653 gate, the production case: the pointer rests over the transcript
    while the keyboard focus sits in a widget outside it (tree row, palette,
    drawer). That widget consumes ArrowUp/PageUp itself, so the transcript never
    scrolls — hovering it must not cost a follower its intent."""
    m = _run(
        "document.activeElement = treeRow; HOVER = true;\n"
        "_onKeydown({key:'ArrowUp', target:treeRow, shiftKey:false});\n"
        "out({cache:_messageFollowIntentCache, gen:_messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 0, "a key consumed off-pane must not mint transcript re-pin authority"
    assert m["cache"] is True, (
        "hover alone must not invalidate follow-intent while another widget owns the "
        "keyboard and consumes the key (#6653 gate)"
    )


def test_hover_fallback_survives_when_no_widget_owns_the_keyboard():
    """Control: the body/hover fallback is intentional (#7494). With nothing
    focused the hovered transcript does receive the key, so it must still
    invalidate — the gate only removes hover's authority over a focused widget."""
    m = _run(
        "document.activeElement = document.body; HOVER = true;\n"
        "_onKeydown({key:'PageUp', target:document.body, shiftKey:false});\n"
        "out({cache:_messageFollowIntentCache, gen:_messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 1, "with no keyboard owner the hovered transcript still captures the tail"
    assert m["cache"] is False, (
        "with no competing keyboard owner, PageUp on the hovered transcript must still "
        "invalidate follow-intent (#7494 fallback preserved)"
    )


def test_foreign_key_keeps_following_through_cancellation_and_growth():
    """Composed contract: an off-pane key while hovering is followed by a
    cancellation and live content growth. Because the transcript kept its
    follow-intent, the automatic writer must still reach the tail — and the
    growth must not be mistaken for reader input."""
    m = _run(
        "document.activeElement = treeRow; HOVER = true;\n"
        "_onKeydown({key:'ArrowUp', target:treeRow, shiftKey:false});\n"
        "const afterKey = {cache:_messageFollowIntentCache, top:el.scrollTop};\n"
        "el.scrollTop = 4000;             // streamed content previously moved the reader\n"
        "_setMessageScrollToBottom();      // automatic follow on live growth\n"
        "out({afterKey, top:el.scrollTop, cache:_messageFollowIntentCache});\n"
    )
    assert m["afterKey"]["cache"] is True, (
        "the off-pane key must leave a follower following so the next growth can re-pin"
    )
    assert m["afterKey"]["top"] == 4200, "the off-pane key must not move the reader"
    assert m["top"] == 5000, (
        "with follow-intent intact, the automatic writer must still reach the tail"
    )
    assert m["cache"] is True, "the follow writer must not flip follow-intent off"
