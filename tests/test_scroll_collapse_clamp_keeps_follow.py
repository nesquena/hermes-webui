"""A collapse above the tail must not unpin a reader who is flush at the tail.

Geometry: the reader is pinned, flush at the tail (bottomDistance ~= 0). A block
ABOVE the tail collapses - the settled worklog fold, a thinking/tool card
collapse, or the interim-note collapse. scrollHeight shrinks by the collapsed
height, the browser clamps scrollTop DOWN by the same amount, and a scroll event
fires with movedUp=true while bottomDistance is still ~0.

The post-render artifact suppression does not cover this: it requires a
renderMessages() within the last 1400ms, and those collapse paths run from the
streaming handlers (closeCurrentLiveActivityGroup / interim collapse), which
patch the DOM incrementally and never stamp _lastMessageRenderAt. So the
upward-unpin branch itself has to require that the reader actually left the
bottom before it treats an upward delta as scroll-away intent.

These tests drive the real rAF callback body extracted from static/ui.js in node
with controllable geometry, the same way the #4970 harness does.
"""

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _balanced_block(src: str, brace_start: int) -> str:
    depth = 0
    for i in range(brace_start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace_start + 1 : i]
    raise AssertionError("balanced block not found")


def _scroll_listener_raf_body() -> str:
    listener_start = UI_JS.index("el.addEventListener('scroll'")
    raf_start = UI_JS.index("requestAnimationFrame(()=>", listener_start)
    brace_start = UI_JS.index("{", raf_start)
    return _balanced_block(UI_JS, brace_start)


def _run_listener(
    samples,
    *,
    render_artifact=False,
    wheel_intent=False,
    touch_intent=False,
    key_scroll=False,
    scrollbar_drag=False,
    start_scroll_top=5703,
    start_client_height=474,
    auto_follow=True,
):
    """Run the extracted scroll-listener rAF body over geometry samples."""
    payload = {
        "body": _scroll_listener_raf_body(),
        "samples": samples,
        "renderArtifact": bool(render_artifact),
        "wheelIntent": bool(wheel_intent),
        "touchIntent": bool(touch_intent),
        "keyScroll": bool(key_scroll),
        "scrollbarDrag": bool(scrollbar_drag),
        "startTop": start_scroll_top,
        "startClientHeight": start_client_height,
        "autoFollow": bool(auto_follow),
    }
    script = (
        "const payload = " + json.dumps(payload) + ";\n"
        + r"""
global.window = { _autoScrollFollow: payload.autoFollow, _showThinking: true };
const step = new Function(
  'el',
  '_lastScrollTop',
  '_lastMessageClientHeight',
  '_nearBottomCount',
  '_scrollPinned',
  '_messageUserUnpinned',
  '_newMessageCueVisible',
  '_cancelBottomSettle',
  '_setMessageScrollToBottom',
  '_clearNewMessageScrollCue',
  '_syncScrollToBottomCue',
  '_updateSessionStartJumpButton',
  '_isSessionEndlessScrollEnabled',
  '_messagesTruncated',
  '_loadOlderMessages',
  '_recentMessageRenderArtifactWindow',
  '_recentMessageTouchScrollIntent',
  '_recentNonMessageScrollIntent',
  '_recentMessageWheelIntent',
  '_recentMessageKeyScrollIntent',
  '_recentMessageScrollIntent',
  '_scheduleDeferredOlderMessagesLoad',
  '_scrollbarDragActive',
  '(()=>{' + payload.body + `})();
return {
  _lastScrollTop,
  _lastMessageClientHeight,
  _nearBottomCount,
  _scrollPinned,
  _messageUserUnpinned,
};
`
);

let state = {
  _lastScrollTop: payload.startTop,
  _lastMessageClientHeight: payload.startClientHeight,
  _nearBottomCount: 0,
  _scrollPinned: true,
  _messageUserUnpinned: false,
};

const noop = () => {};
const yes = () => true;
const no = () => false;

for (const sample of payload.samples) {
  const el = {
    scrollTop: sample.scrollTop,
    scrollHeight: sample.scrollHeight,
    clientHeight: sample.clientHeight,
  };
  state = step(
    el,
    state._lastScrollTop,
    state._lastMessageClientHeight,
    state._nearBottomCount,
    state._scrollPinned,
    state._messageUserUnpinned,
    false,
    noop,
    noop,
    noop,
    noop,
    noop,
    no,
    false,
    noop,
    () => payload.renderArtifact,
    () => payload.touchIntent,
    no,
    () => payload.wheelIntent,
    () => payload.keyScroll,
    no,
    noop,
    payload.scrollbarDrag
  );
}

console.log(JSON.stringify(state));
"""
    )
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout.strip())


# The clamp sample, verbatim from a headless-Chrome run against the real app
# (kanban t_f4eb293b): a 519px block above the tail collapses while the reader is
# flush at the bottom. scrollTop 5703 -> 5184, scrollHeight 6177 -> 5658,
# bottomDistance stays 0 in both samples.
_CLAMP_AFTER = {"scrollTop": 5184, "scrollHeight": 5658, "clientHeight": 474}
_CLAMP_BEFORE_TOP = 5703
# A genuine scroll-away from the same start: the reader moves up and is no longer
# flush at the tail.
_SCROLL_AWAY = {"scrollTop": 5000, "scrollHeight": 6177, "clientHeight": 474}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestCollapseClampKeepsFollow:
    def test_above_tail_collapse_clamp_does_not_unpin(self):
        state = _run_listener([_CLAMP_AFTER], start_scroll_top=_CLAMP_BEFORE_TOP)
        assert state["_messageUserUnpinned"] is False, (
            "An above-tail collapse clamps scrollTop down while the reader is still "
            "flush at the tail (bottomDistance 0). That is a layout clamp, not "
            "scroll-away intent: it must not set the sticky manual-unpin flag, or "
            "live-follow dies mid-stream for a reader who never scrolled."
        )
        assert state["_scrollPinned"] is True

    def test_real_upward_scroll_still_unpins(self):
        state = _run_listener(
            [_SCROLL_AWAY], start_scroll_top=_CLAMP_BEFORE_TOP
        )
        assert state["_messageUserUnpinned"] is True, (
            "A real upward scroll leaves the true bottom (bottomDistance>1) and must "
            "still unpin immediately - #1731 behaviour is unchanged."
        )
        assert state["_scrollPinned"] is False

    def test_clamp_during_open_artifact_window_stays_pinned(self):
        # Same clamp geometry with a renderMessages() in the last 1400ms: that path
        # was already covered before this fix and must keep behaving the same way.
        state = _run_listener(
            [_CLAMP_AFTER],
            start_scroll_top=_CLAMP_BEFORE_TOP,
            render_artifact=True,
        )
        assert state["_messageUserUnpinned"] is False
        assert state["_scrollPinned"] is True

    def test_upward_delta_just_above_the_bottom_still_unpins(self):
        # Boundary: a few px off the true bottom (bottomDistance=4) is a real, if
        # tiny, scroll-away and is still honoured; only the 0/1px clamp case is
        # treated as a layout artifact.
        state = _run_listener(
            [{"scrollTop": 5699, "scrollHeight": 6177, "clientHeight": 474}],
            start_scroll_top=_CLAMP_BEFORE_TOP,
        )
        assert state["_messageUserUnpinned"] is True
        assert state["_scrollPinned"] is False


def test_upward_unpin_branch_requires_leaving_the_bottom():
    # Source pin: the upward branch must not fire on a pure clamp. Keeps a future
    # refactor from dropping the bottom-distance check while the behavioural tests
    # above still exercise the real callback body.
    listener_start = UI_JS.index("el.addEventListener('scroll'")
    listener = UI_JS[listener_start : listener_start + 4000]
    assert "if(movedUp&&bottomDistance>1){" in listener, (
        "The movedUp branch must require the reader to have left the true bottom "
        "(bottomDistance>1) before it unpins; an above-tail collapse clamp fires "
        "movedUp with bottomDistance~=0."
    )
