"""#6653 re-gate: two follow-state gaps in the layout-free follow-intent cache.

The cache from the previous round is fed by the #8070 input-tail ownership, and
both defects below live at the two boundaries of that decision:

1. **Catch-tail never resumes following.** The confirmed input-tail catch
   (`scroll listener`, the `caughtInputTail` branch) clears the sticky unpin and
   sets `_scrollPinned=true`, but it does NOT re-arm `_messageFollowIntentCache`.
   Its call to `_setMessageScrollToBottom()` therefore returns immediately on the
   false cache, and the next streamed token leaves the reader parked at the
   captured tail even though the reader deliberately returned to it.

2. **Nonmoving upward input strands a short transcript.** The wheel and keydown
   invalidations clear the cache whenever the transcript OWNS an upward gesture.
   Ownership says which surface scrolls; it does not say that the surface *can*
   scroll up. With `scrollHeight < clientHeight` and `scrollTop === 0` (a short
   answer), PageUp or a gentle `deltaY:-5` wheel only moves the page, no
   corrective scroll event ever follows, and the answer's growth is never
   followed — the viewport stays at the top.

Every case drives the REAL production bodies extracted from `static/ui.js`
(`_isTranscriptScrollTarget`, `_captureMessageScrollInputTail`,
`_recordNonMessageScrollIntent`, the keydown listener, the scroll listener,
`_setMessageScrollToBottom`). No production logic is re-implemented here.
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

_KEYS_START = UI_JS.index("const _MESSAGE_SCROLL_KEYS=new Set(")
_KEYS_DECL = UI_JS[_KEYS_START : UI_JS.index(";", _KEYS_START) + 1]

_PRELUDE = r"""
'use strict';
// ---- module state the production bodies close over (declared here so the test
// exercises the bodies, not the declarations) ----
let T = { now: 1000 };
const performance = { now: () => T.now };
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
let _newMessageCueVisible = false;
let _scrollRaf = 0;
const window = { _autoScrollFollow: true };

// ---- fake #messages scroller ----
const el = {
  scrollTop: 4200, scrollHeight: 5000, clientHeight: 800, parentElement: null,
  matches: () => HOVER,
  contains: (n) => n === el || !!(n && n._inEl),
};
const document = { getElementById: (id) => (id === 'messages' ? el : null), activeElement: null };
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
function _clearNewMessageScrollCue() { _newMessageCueVisible = false; }
function _syncScrollToBottomCue() {}
function _isSessionEndlessScrollEnabled() { return false; }
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
function requestAnimationFrame(cb) { cb(); return 1; }
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
    + "\n// ---- scenario ----\n"
    + r"""
const pane = {
  scrollTop: 500, scrollHeight: 2000, clientHeight: 500, parentElement: el,
  _cs: { overflowY: 'auto', overscrollBehaviorY: '' },
};
const paneTarget = { parentElement: pane, _inEl: true };
const directTarget = { parentElement: el, _inEl: true };
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


# --------------------------------------------------------------------------- #
# Defect 1 - catching the captured tail must resume following
# --------------------------------------------------------------------------- #
def test_catching_the_captured_tail_re_arms_follow_intent_and_follows_the_next_token():
    """Composed regression: an unpinned reader returns to the tail they aimed at
    (the captured input tail), arrives within the 80px catch band, and then the
    stream grows the transcript. The catch is authorized reader return, so the
    next token must follow to the new bottom instead of leaving the reader
    parked at the previously captured tail."""
    m = _run(
        # Reader had scrolled away: sticky unpin on, follow-intent false.
        "_messageFollowIntentCache = false;\n"
        "_messageUserUnpinned = true;\n"
        "_scrollPinned = true;\n"
        # A wheel/key capture happened at scrollHeight 4300 (the tail aimed at).
        "_messageScrollInputTailGeneration = 1;\n"
        "_messageScrollInputTailHeight = 4300;\n"
        "_messageScrollInputTailConsumedGeneration = 0;\n"
        # The reader now scrolls DOWN to that captured tail while content grew.
        "el.scrollTop = 4250;\n"
        "_lastScrollTop = 4100;\n"
        "_onScroll();\n"
        "const afterCatch = {cache: _messageFollowIntentCache, top: el.scrollTop,"
        " pinned: _scrollPinned, unpinned: _messageUserUnpinned};\n"
        # Next streamed token grows the transcript.
        "el.scrollHeight = 5900;\n"
        "_setMessageScrollToBottom();\n"
        "out({afterCatch, top: el.scrollTop, max: el.scrollHeight - el.clientHeight});\n"
    )
    assert m["afterCatch"]["cache"] is True, (
        "the confirmed input-tail catch is authorized reader return: it must re-arm the "
        "follow-intent cache before calling the automatic writer"
    )
    assert m["afterCatch"]["unpinned"] is False
    assert m["afterCatch"]["top"] == 5000, (
        "the catch must snap to the then-current bottom (the automatic writer is allowed "
        "to run once the cache is re-armed)"
    )
    assert m["top"] == 5900 and m["max"] == 5100, (
        "after the catch, the next streamed growth must follow to the new bottom, not stay "
        "parked at the captured tail"
    )


def test_downward_scroll_that_misses_the_captured_tail_stays_unpinned():
    """Control (#4295 invariant): a downward scroll that does NOT reach the
    captured tail keeps the reader unpinned and must not re-arm follow-intent —
    otherwise the nearest-token yank the cache was introduced to prevent comes
    back through the catch branch."""
    m = _run(
        "_messageFollowIntentCache = false;\n"
        "_messageUserUnpinned = true;\n"
        "_scrollPinned = true;\n"
        "_messageScrollInputTailGeneration = 1;\n"
        "_messageScrollInputTailHeight = 9000;\n"   # tail aimed at, far below
        "_messageScrollInputTailConsumedGeneration = 0;\n"
        "el.scrollTop = 4300;\n"                    # down a little, nowhere near 8920
        "_lastScrollTop = 4200;\n"
        "el.scrollHeight = 6200;\n"                 # transcript grew towards it
        "_onScroll();\n"
        "const afterScroll = {cache: _messageFollowIntentCache, top: el.scrollTop,"
        " pinned: _scrollPinned, unpinned: _messageUserUnpinned};\n"
        "el.scrollHeight = 7000;\n"
        "_setMessageScrollToBottom();\n"
        "out({afterScroll, top: el.scrollTop});\n"
    )
    assert m["afterScroll"]["cache"] is False, (
        "missing the captured tail is not reader return: the false cache must stay sticky"
    )
    assert m["top"] == 4300, (
        "an unpinned reader that did not reach the captured tail must never be yanked by "
        "the next token"
    )


# --------------------------------------------------------------------------- #
# Defect 2 - nonmoving upward input must not cancel follow on a short transcript
# --------------------------------------------------------------------------- #
def test_page_up_on_a_short_transcript_keeps_follow_intent_and_follows_growth():
    """With scrollHeight 500 and clientHeight 800 there is nowhere to scroll up,
    so PageUp only moves the page: no corrective scroll event will ever arrive.
    The cache must stay armed and the answer's growth must be followed."""
    m = _run(
        "el.scrollHeight = 500; el.clientHeight = 800; el.scrollTop = 0;\n"
        "_lastScrollTop = 0;\n"
        "document.activeElement = el; HOVER = true;\n"
        "_onKeydown({key:'PageUp', target:el, shiftKey:false});\n"
        "const afterKey = {cache: _messageFollowIntentCache, gen: _messageScrollInputTailGeneration,"
        " pinned: _scrollPinned, unpinned: _messageUserUnpinned};\n"
        "el.scrollHeight = 1600;\n"
        "_setMessageScrollToBottom();\n"
        "out({afterKey, top: el.scrollTop, max: el.scrollHeight - el.clientHeight});\n"
    )
    assert m["afterKey"]["gen"] == 1, (
        "the capture stays: PageUp on the transcript is still reader input (re-pin authority)"
    )
    assert m["afterKey"]["cache"] is True, (
        "upward input that cannot move the transcript must not cancel follow-intent"
    )
    assert m["top"] == 1600 and m["max"] == 800, (
        "a short answer that grows must be followed; staying at the top strands the reply"
    )


def test_gentle_wheel_up_on_a_short_transcript_keeps_follow_intent_and_follows_growth():
    """Same boundary through the wheel path: a gentle `deltaY:-5` trackpad
    gesture owns the transcript but cannot scroll it up from scrollTop 0."""
    m = _run(
        "el.scrollHeight = 500; el.clientHeight = 800; el.scrollTop = 0;\n"
        "_lastScrollTop = 0;\n"
        "FRESH = false;\n"
        "_recordNonMessageScrollIntent({type:'wheel', deltaY:-5, target:directTarget});\n"
        "const afterWheel = {cache: _messageFollowIntentCache, gen: _messageScrollInputTailGeneration,"
        " pinned: _scrollPinned, unpinned: _messageUserUnpinned};\n"
        "el.scrollHeight = 1600;\n"
        "_setMessageScrollToBottom();\n"
        "out({afterWheel, top: el.scrollTop, max: el.scrollHeight - el.clientHeight});\n"
    )
    assert m["afterWheel"]["gen"] == 1, "the gentle wheel still captures the input tail"
    assert m["afterWheel"]["cache"] is True, (
        "a wheel-up with no upward travel must not cancel follow-intent"
    )
    assert m["top"] == 1600 and m["max"] == 800, (
        "the growing short answer must be followed to the bottom"
    )


# --------------------------------------------------------------------------- #
# Controls - the accepted protections of the previous round must survive
# --------------------------------------------------------------------------- #
def test_page_up_on_a_scrollable_transcript_still_invalidates_and_stays_sticky():
    """The #6653 protection itself: with real upward travel available, PageUp is
    an authoritative scroll-away. The cache flips and stays false, so the next
    token must not yank the reader back."""
    m = _run(
        "el.scrollHeight = 5000; el.clientHeight = 800; el.scrollTop = 4200;\n"
        "_lastScrollTop = 4200;\n"
        "document.activeElement = el; HOVER = true;\n"
        "_onKeydown({key:'PageUp', target:el, shiftKey:false});\n"
        "const afterKey = {cache: _messageFollowIntentCache, gen: _messageScrollInputTailGeneration};\n"
        "el.scrollHeight = 6400;\n"
        "_setMessageScrollToBottom();\n"
        "out({afterKey, top: el.scrollTop});\n"
    )
    assert m["afterKey"]["gen"] == 1, "PageUp on the transcript still captures the input tail"
    assert m["afterKey"]["cache"] is False, (
        "upward travel is possible here, so PageUp must still invalidate follow-intent"
    )
    assert m["top"] == 4200, (
        "a reader who genuinely scrolled up must not be yanked by the next token"
    )


def test_downward_wheel_on_a_scrollable_transcript_keeps_following():
    """Genuine-follower control: a downward wheel scrolls toward the bottom, so a
    follower stays a follower and the next token follows."""
    m = _run(
        "el.scrollHeight = 5000; el.clientHeight = 800; el.scrollTop = 4200;\n"
        "_lastScrollTop = 4200;\n"
        "FRESH = false;\n"
        "_recordNonMessageScrollIntent({type:'wheel', deltaY:40, target:directTarget});\n"
        "const afterWheel = {cache: _messageFollowIntentCache, gen: _messageScrollInputTailGeneration};\n"
        "el.scrollHeight = 5900;\n"
        "_setMessageScrollToBottom();\n"
        "out({afterWheel, top: el.scrollTop});\n"
    )
    assert m["afterWheel"]["cache"] is True, "a downward wheel must not cancel a follower"
    assert m["top"] == 5900, "a genuine follower must still follow the streamed growth"


def test_nested_pane_input_on_a_short_transcript_keeps_follow_intent():
    """Nested-input control at the new boundary: input consumed by a nested pane
    never reaches the transcript, so it must neither capture nor invalidate —
    independent of whether the transcript could scroll up."""
    m = _run(
        "el.scrollHeight = 500; el.clientHeight = 800; el.scrollTop = 0;\n"
        "document.activeElement = paneTarget; HOVER = true;\n"
        "_onKeydown({key:'PageUp', target:paneTarget, shiftKey:false});\n"
        "_recordNonMessageScrollIntent({type:'wheel', deltaY:-40, target:paneTarget});\n"
        "out({cache: _messageFollowIntentCache, gen: _messageScrollInputTailGeneration});\n"
    )
    assert m["gen"] == 0, "nested-pane input must not mint transcript re-pin authority"
    assert m["cache"] is True, "nested-pane input must not cancel the transcript follower"
