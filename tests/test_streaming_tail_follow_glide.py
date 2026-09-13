"""Regression tests for the streaming chat auto-scroll stutter.

Symptom: while an assistant reply types itself out, the transcript does not
follow the text smoothly. It sits still for several render ticks and then jumps
a whole line, and the jump repeats for the length of the answer.

Root cause: the streaming follow path snapped ``scrollTop = scrollHeight`` on
every render tick. A tick only adds a couple of words, so the scroll position
does not change at all until a line WRAPS -- at which point the whole line
height is applied in one painted step. The same path also rebuilt the settle
ResizeObserver (disconnect + ``new ResizeObserver`` + observe + two timers) per
tick, i.e. ~30 observer teardown/rebuild cycles a second on a transcript that
may be thousands of nodes deep, which is the dominant per-frame cost on mobile.

Fix: while a turn is streaming (or the word-fade is still playing text out after
the SSE ``done``), ``scrollIfPinned()`` hands the tail to a single rAF loop,
``_tailFollowFrame()``, that eases ``scrollTop`` toward the bottom -- one
``scrollHeight`` read and at most one ``scrollTop`` write per frame -- instead
of snapping and re-arming the settle. The loop bounds how far it may lag, snaps
when it is hopelessly behind, parks itself when there is nothing to chase, and
refuses on the same signals the settle path refuses on plus an active finger.

The behavioural tests drive the REAL extracted helpers in Node against a fake
scroller and a manually pumped frame queue, so they fail on the pre-fix version
(the helpers do not exist) and pass on the fixed one.
"""

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_fn(src: str, name: str) -> str:
    marker = re.search(rf"(^|\n)\s*(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"function {name}() not found"
    start = marker.start()
    brace = src.index("{", marker.end())
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name}() closing brace not found")


def _glide_source() -> str:
    """The real tail-follow block, lifted verbatim from ui.js."""
    start = UI_JS.index("const _TAIL_FOLLOW_EASE=")
    end = UI_JS.index("let _scrollIfPinnedCoalesced=false;")
    assert start < end
    return UI_JS[start:end]


_HARNESS_PRELUDE = """
const performance = { now: () => _clock };
let _clock = 0;
let _frames = [];
let _nextRafId = 1;
const _cancelled = new Set();
function requestAnimationFrame(cb){ const id = _nextRafId++; _frames.push([id, cb]); return id; }
function cancelAnimationFrame(id){ _cancelled.add(id); }
function pumpFrames(n){
  for(let i=0;i<n;i++){
    const batch = _frames; _frames = [];
    _clock += 16;
    for(const [id, cb] of batch){ if(!_cancelled.has(id)) cb(); }
  }
}
const el = { scrollTop: 0, clientHeight: 500, scrollHeight: 500 };
function $(id){ return id === 'messages' ? el : null; }
const window = { _autoScrollFollow: true, _streamFadeDrainingUntil: 0 };
const S = { activeStreamId: 'stream-1' };
let _scrollPinned = true;
let _messageUserUnpinned = false;
let _messageTouchScrollActive = false;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _lastMessageClientHeight = 500;
let _nearBottomCount = 0;
function _recentNonMessageScrollIntent(){ return false; }
function _deferClearProgrammaticScroll(){ }
"""


def _run(scenario: str) -> str:
    source = _HARNESS_PRELUDE + _glide_source() + "\n" + scenario
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(path)], cwd=str(ROOT), capture_output=True, text=True, timeout=30
        )
    finally:
        path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# --------------------------------------------------------------------------
# behaviour
# --------------------------------------------------------------------------


def test_one_wrapped_line_is_glided_not_snapped():
    """A single wrapped line (24px) must be crossed over several painted frames.

    This is the stutter itself: the old path wrote the whole 24px in one frame.
    """
    out = _run(
        """
        el.scrollHeight = 524;            // one wrapped line of growth
        _tailFollowStart();
        const seen = [];
        for(let i=0;i<40;i++){ pumpFrames(1); seen.push(Number(el.scrollTop.toFixed(3))); }
        const distinct = [...new Set(seen)];
        console.log(JSON.stringify({
          first: seen[0],
          settled: seen[seen.length-1],
          intermediates: distinct.filter(v => v > 0 && v < 24).length,
        }));
        """
    )
    import json

    data = json.loads(out)
    assert 0 < data["first"] < 24, (
        f"first frame must move part of the line, not all of it (got {data['first']})"
    )
    assert data["intermediates"] >= 3, (
        "a wrapped line must be crossed in several painted steps, "
        f"got {data['intermediates']} intermediate positions"
    )
    assert data["settled"] == 24, "the glide must still land exactly on the bottom"


def test_glide_never_overshoots_the_bottom():
    out = _run(
        """
        el.scrollHeight = 560;
        _tailFollowStart();
        let maxSeen = 0;
        for(let i=0;i<60;i++){ pumpFrames(1); maxSeen = Math.max(maxSeen, el.scrollTop); }
        console.log(String(maxSeen));
        """
    )
    assert float(out) <= 60.0, "scrollTop must never exceed scrollHeight-clientHeight"


def test_lag_stays_bounded_under_fast_multi_line_growth():
    """With the word fade off, a tick can add several lines at once. A purely
    proportional ease would park the newest lines below the fold."""
    out = _run(
        """
        _tailFollowStart();
        let worstGap = 0;
        for(let i=0;i<60;i++){
          el.scrollHeight += 60;          // ~2.5 lines of growth per frame
          pumpFrames(1);
          worstGap = Math.max(worstGap, (el.scrollHeight - el.clientHeight) - el.scrollTop);
        }
        console.log(String(worstGap));
        """
    )
    assert float(out) <= 40.0, f"tail lagged {out}px behind the bottom during fast growth"


def test_huge_gap_snaps_instead_of_gliding():
    out = _run(
        """
        el.scrollHeight = 2000;           // 1500px behind: a glide would crawl
        _tailFollowStart();
        pumpFrames(1);
        console.log(String(el.scrollTop));
        """
    )
    assert float(out) == 1500.0, "a hopeless gap must snap in one frame, not glide"


def test_glide_yields_to_an_unpinned_reader():
    out = _run(
        """
        el.scrollHeight = 900;
        _tailFollowStart();
        pumpFrames(1);
        const afterFirst = el.scrollTop;
        _messageUserUnpinned = true;      // reader scrolled up mid-stream
        pumpFrames(10);
        console.log(JSON.stringify({moved: afterFirst > 0, after: el.scrollTop === afterFirst}));
        """
    )
    import json

    data = json.loads(out)
    assert data["moved"], "sanity: the glide should have moved before the unpin"
    assert data["after"], "the glide must not move a reader who scrolled away"


def test_glide_yields_while_a_finger_is_down():
    """touchstart marks the transcript touch-active. A per-frame write during an
    active drag would cancel the drag / iOS momentum."""
    out = _run(
        """
        el.scrollHeight = 900;
        _messageTouchScrollActive = true;
        _tailFollowStart();
        pumpFrames(10);
        console.log(String(el.scrollTop));
        """
    )
    assert float(out) == 0.0, "the glide must not write while a finger is on the transcript"


def test_glide_parks_itself_when_there_is_nothing_to_chase():
    """The loop must stop scheduling frames once caught up, so an idle session
    does not hold a permanent rAF loop."""
    out = _run(
        """
        el.scrollHeight = 524;
        _tailFollowStart();
        pumpFrames(60);                   // catch up, then go quiet
        const before = _frames.length;
        pumpFrames(5);
        console.log(JSON.stringify({pendingAfterQuiet: _frames.length, before}));
        """
    )
    import json

    assert json.loads(out)["pendingAfterQuiet"] == 0, (
        "the tail-follow loop must park itself after a quiet stretch"
    )


def test_drain_deadline_keeps_the_glide_after_the_stream_id_clears():
    """The SSE `done` handler clears S.activeStreamId while the word fade is
    still releasing text, so the drain deadline must keep the glide engaged."""
    out = _run(
        """
        S.activeStreamId = null;
        const off = _tailFollowStreamLive();
        window._streamFadeDrainingUntil = performance.now() + 400;
        const during = _tailFollowStreamLive();
        _clock += 1000;                   // deadline lapses on its own
        const expired = _tailFollowStreamLive();
        console.log(JSON.stringify({off, during, expired}));
        """
    )
    import json

    data = json.loads(out)
    assert data["off"] is False
    assert data["during"] is True, "the fade drain must keep owning the scroll tail"
    assert data["expired"] is False, (
        "an abandoned drain must expire on its own, never strand the glide on"
    )


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_scroll_if_pinned_routes_a_live_stream_to_the_glide():
    fn = _extract_fn(UI_JS, "scrollIfPinned")
    assert "_tailFollowStreamLive()" in fn and "_tailFollowStart();" in fn
    # The >500px recovery snap still runs first, and the per-tick settle stays as
    # the non-streaming path (locked by test_issue3319).
    assert fn.index("_messageBottomDistance()>500") < fn.index("_tailFollowStart();")
    assert fn.index("_tailFollowStart();") < fn.index("_settleMessageScrollToBottom(false)")


def test_settle_and_cancel_take_ownership_back_from_the_glide():
    """Two writers must never chase the tail at once."""
    assert "_tailFollowStop()" in _extract_fn(UI_JS, "_settleMessageScrollToBottom")
    assert "_tailFollowStop()" in _extract_fn(UI_JS, "_cancelBottomSettle")


def test_fade_render_cadence_is_vsync_aligned():
    """setTimeout(33)+rAF lands anywhere in 33-50ms, so the released word wave
    arrives unevenly and reads as typing stutter."""
    fn = _extract_fn(MESSAGES_JS, "_scheduleRender")
    assert "frameIntervalMs-4" in fn, "fade path must poll rAF against the interval"
    assert "_pendingRafHandle=requestAnimationFrame(_poll);" in fn
    # The self-reschedule while the fade is still catching up must not stack a
    # 33ms timer on top of _scheduleRender()'s own 33ms gate; rAF first, with the
    # timer kept only as the no-rAF fallback.
    assert "requestAnimationFrame(()=>_scheduleRender());" in fn
    reschedule = fn[fn.index("if(!caughtUp&&!_streamFinalized){"):]
    assert reschedule.index("requestAnimationFrame(()=>_scheduleRender());") < reschedule.index(
        "setTimeout(()=>_scheduleRender(), 33);"
    ), "rAF must be the primary path, the 33ms timer only the fallback"


def test_post_done_fade_drain_is_vsync_aligned_and_marks_ownership():
    fn = _extract_fn(MESSAGES_JS, "_drainStreamFadeBeforeDone")
    assert "setTimeout(()=>requestAnimationFrame(step), 33);" not in fn
    assert "window._streamFadeDrainingUntil=performance.now()+400" in fn
    assert "window._streamFadeDrainingUntil=0" in fn, "drain must release ownership"
