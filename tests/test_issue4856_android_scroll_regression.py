"""Regression tests for #4856: Android scroll-to-top on every interaction."""

from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")

_FUNC_MARKER = "window._fixMobileScrollJank=function _fixMobileScrollJank(){"
_RAF_MARKER = "setTimeout(()=>{"


def _extract_fix_mobile_scroll_jank(src: str) -> str:
    idx = src.find(_FUNC_MARKER)
    assert idx != -1, "_fixMobileScrollJank not found in ui.js"
    depth = 0
    for i, ch in enumerate(src[idx:], idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
    raise AssertionError("Could not extract _fixMobileScrollJank")


def _extract_raf_body(fn_src: str) -> str:
    # The deferred-release cleanup now lives inside the final setTimeout callback
    # (see the two-rAF-hop + settle-timeout structure), not a bare rAF. Extract
    # that callback body so the cleanup-check assertions below still apply.
    idx = fn_src.find(_RAF_MARKER)
    assert idx != -1, "deferred-release setTimeout callback not found in function"
    depth = 0
    for i, ch in enumerate(fn_src[idx:], idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return fn_src[idx : i + 1]
    raise AssertionError("Could not extract deferred-release cleanup body")


def test_fix_sets_none_not_auto():
    # Base-fails/head-passes: function must set 'none' to suppress Chromium
    # scroll-anchor re-selection; 'auto' is already the CSS default so setting
    # it is a no-op and leaves the anchor engine active during the DOM wipe.
    fn = _extract_fix_mobile_scroll_jank(UI_JS)
    assert "overflowAnchor='none'" in fn, (
        "_fixMobileScrollJank() must set overflowAnchor='none' to suppress "
        "Chromium scroll-anchor re-selection during the DOM wipe (#4856)."
    )
    assert "overflowAnchor='auto'" not in fn, (
        "_fixMobileScrollJank() must not set overflowAnchor='auto'; that is "
        "already the CSS resting value on mobile and is a no-op."
    )


def test_raf_cleanup_checks_none():
    # The release cleanup must check for 'none' so it only clears the inline
    # style it set; checking 'auto' was always false and left the inline style
    # on. The cleanup now lives in the shared _liftMobileAnchorSuppression()
    # helper (called by every release path: base settle, transition settle, and
    # the independent hard cap), so assert the invariant there.
    lift_idx = UI_JS.find("function _liftMobileAnchorSuppression(")
    assert lift_idx != -1, (
        "_liftMobileAnchorSuppression() must exist as the single cleanup path "
        "shared by the base-settle, transition-settle, and hard-cap releases."
    )
    lift = _balanced_block(UI_JS, UI_JS.index("{", lift_idx))
    assert "overflowAnchor==='none'" in lift, (
        "The cleanup in _liftMobileAnchorSuppression() must check for 'none' so "
        "it clears only the inline style it set, not a legitimately re-armed one."
    )
    assert "overflowAnchor==='auto'" not in lift, (
        "The cleanup must not check for 'auto'; that check was always false."
    )


def test_rebuild_path_calls_fix_before_wipe():
    # renderMessages() must call _fixMobileScrollJank() before innerHTML='' so
    # anchor suppression is active during the full wipe-and-rebuild window.
    fix_idx = UI_JS.find("window._fixMobileScrollJank()")
    assert fix_idx != -1, (
        "renderMessages() must call window._fixMobileScrollJank() before innerHTML=''."
    )
    wipe_idx = UI_JS.find("innerHTML=''", fix_idx)
    assert wipe_idx != -1, (
        "innerHTML='' not found after _fixMobileScrollJank() call site."
    )
    assert fix_idx < wipe_idx, (
        "_fixMobileScrollJank() must be called before innerHTML='' in renderMessages()."
    )


def test_rebuild_path_marks_dom_wipe_scroll_as_programmatic():
    # During innerHTML='' the scroller can transiently collapse to clientHeight
    # and clamp scrollTop to 0. That browser event must be suppressed as
    # programmatic; otherwise the scroll listener treats it as user upward
    # intent and disables live auto-follow.
    fix_idx = UI_JS.find("window._fixMobileScrollJank()")
    assert fix_idx != -1, "renderMessages() guard call not found"
    wipe_idx = UI_JS.find("innerHTML=''", fix_idx)
    assert wipe_idx != -1, "innerHTML='' not found after _fixMobileScrollJank()"
    window = UI_JS[fix_idx:wipe_idx]
    assert "_programmaticScroll=true" in window, (
        "renderMessages() must mark the DOM wipe/rebuild scroll event as "
        "programmatic before innerHTML='' can clamp scrollTop."
    )
    assert "_programmaticScrollSetAt=performance.now()" in window
    assert UI_JS.find("_deferClearProgrammaticScroll(160)", wipe_idx) != -1, (
        "renderMessages() must clear the programmatic-scroll suppression after "
        "the rebuild/post-render paint window."
    )


def test_recent_render_scroll_artifact_window_suppresses_upward_unpin():
    # Some browsers emit a follow-up scroll event shortly after renderMessages()
    # finishes (for example while late layout settles after a send). With no
    # wheel/touch intent, that post-render upward delta is still a render
    # artifact and must not disable live follow.
    assert "let _lastMessageRenderAt=-Infinity" in UI_JS
    assert "_lastMessageRenderAt=performance.now()" in UI_JS
    assert "function _recentMessageRenderArtifactWindow" in UI_JS
    listener_idx = UI_JS.find("el.addEventListener('scroll'")
    assert listener_idx != -1, "messages scroll listener not found"
    listener = UI_JS[listener_idx: listener_idx + 4000]
    assert "_recentMessageRenderArtifactWindow(1400)" in listener
    assert "!_recentMessageTouchScrollIntent()" in listener
    assert "!_recentNonMessageScrollIntent()" in listener
    assert "!_recentMessageWheelIntent()" in listener, (
        "#4970: the post-render artifact suppression must also require no recent "
        "low-delta message-pane wheel intent so a gentle trackpad scroll-up is "
        "not swallowed."
    )
    assert listener.find("return;") < listener.find("if(movedUp){"), (
        "recent render artifact scrolls must return before the movedUp branch "
        "can mark the reader unpinned."
    )


# ── #4970 low-delta wheel intent: behavioral node-harness ────────────────────
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

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


def _run_listener_with_wheel_intent(
    samples, *, render_artifact, wheel_intent, scrollbar_drag=False, key_scroll=False
):
    """Run the extracted scroll-listener body in node with controllable stubs.

    Mirrors the #4295 harness shape but injects the #4970 helpers so we can
    exercise the production suppression path: artifact window active AND a
    recent gentle wheel intent must still unpin (return _messageUserUnpinned).
    """
    payload = {
        "body": _scroll_listener_raf_body(),
        "samples": samples,
        "renderArtifact": bool(render_artifact),
        "wheelIntent": bool(wheel_intent),
        "scrollbarDrag": bool(scrollbar_drag),
        "keyScroll": bool(key_scroll),
    }
    script = (
        "const payload = " + json.dumps(payload) + ";\n"
        + r"""
const step = new Function(
  'el',
  '_lastScrollTop',
  '_lastMessageClientHeight',
  '_nearBottomCount',
  '_scrollPinned',
  '_messageUserUnpinned',
  '_newMessageCueVisible',
  '_programmaticScroll',
  '_cancelBottomSettle',
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
  '_scrollbarDragActive',
  '_recentMessageKeyScrollIntent',
  // The extracted listener body uses bare `return;` in the suppression branch.
  // Wrap it in an inner arrow IIFE so that early return exits the IIFE (not the
  // outer Function), then read the mutated locals afterward. Without this the
  // suppression path would return undefined before the state snapshot.
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
  _lastScrollTop: 800,
  _lastMessageClientHeight: null,
  _nearBottomCount: 0,
  _scrollPinned: true,
  _messageUserUnpinned: false,
};

const noop = () => {};
const renderArtifact = () => payload.renderArtifact;
const noTouch = () => false;
const noNonMessage = () => false;
const wheelIntent = () => payload.wheelIntent;
const keyScroll = () => payload.keyScroll;

for (const sample of payload.samples) {
  state = step(
    sample,
    state._lastScrollTop,
    state._lastMessageClientHeight,
    state._nearBottomCount,
    state._scrollPinned,
    state._messageUserUnpinned,
    false,
    false,
    noop,
    noop,
    noop,
    noop,
    () => false,
    false,
    noop,
    renderArtifact,
    noTouch,
    noNonMessage,
    wheelIntent,
    payload.scrollbarDrag,
    keyScroll
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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestPostRenderWheelIntentScope:
    # A small upward scrollTop delta (800 -> 760) is `movedUp`. We hold the
    # render artifact window OPEN for all cases so the only variable is whether
    # the reader had recent gentle wheel intent.
    _SAMPLES = [{"scrollTop": 760, "scrollHeight": 1200, "clientHeight": 200}]

    def test_gentle_wheel_inside_artifact_window_still_unpins(self):
        # #4970 MUST-FIX: with the artifact window active but a recent low-delta
        # wheel intent, a real upward scroll must NOT be swallowed — it must
        # unpin live-follow just like any genuine scroll-up.
        state = _run_listener_with_wheel_intent(
            self._SAMPLES, render_artifact=True, wheel_intent=True
        )
        assert state["_messageUserUnpinned"] is True, (
            "A genuine gentle (low-delta) wheel scroll-up inside the post-render "
            "artifact window must still unpin; the suppression must be scoped to "
            "the no-intent artifact case only."
        )
        assert state["_scrollPinned"] is False

    def test_no_intent_artifact_inside_window_is_suppressed(self):
        # Control: same upward delta, same open window, but NO wheel intent — a
        # true post-render artifact — stays pinned (the suppression still works).
        state = _run_listener_with_wheel_intent(
            self._SAMPLES, render_artifact=True, wheel_intent=False
        )
        assert state["_messageUserUnpinned"] is False, (
            "A no-intent upward delta inside the artifact window is a render "
            "artifact and must be suppressed (reader stays pinned)."
        )
        assert state["_scrollPinned"] is True

    def test_gentle_wheel_outside_window_unpins(self):
        # Outside the artifact window the suppression never applies, so the
        # upward delta unpins regardless of intent tracking.
        state = _run_listener_with_wheel_intent(
            self._SAMPLES, render_artifact=False, wheel_intent=False
        )
        assert state["_messageUserUnpinned"] is True
        assert state["_scrollPinned"] is False

    def test_scrollbar_drag_inside_window_still_unpins(self):
        # #4970 review SHOULD-FIX: a manual scrollbar-drag upward scroll inside
        # the post-render window is real user intent and must NOT be swallowed,
        # even with no wheel/touch intent recorded.
        state = _run_listener_with_wheel_intent(
            self._SAMPLES,
            render_artifact=True,
            wheel_intent=False,
            scrollbar_drag=True,
        )
        assert state["_messageUserUnpinned"] is True, (
            "A scrollbar-drag upward scroll inside the artifact window must "
            "unpin; the suppression must not swallow an active scrollbar drag."
        )
        assert state["_scrollPinned"] is False

    def test_keyboard_scroll_inside_window_still_unpins(self):
        # #4970 review (greptile P1): a keyboard scroll-up (PageUp/Arrow/etc.)
        # inside the post-render window is real intent and must NOT be swallowed,
        # even with no wheel/touch/scrollbar intent recorded.
        state = _run_listener_with_wheel_intent(
            self._SAMPLES,
            render_artifact=True,
            wheel_intent=False,
            key_scroll=True,
        )
        assert state["_messageUserUnpinned"] is True, (
            "A keyboard scroll-up inside the artifact window must unpin; the "
            "suppression must not swallow a recent keyboard scroll intent."
        )
        assert state["_scrollPinned"] is False


def test_low_delta_wheel_intent_is_tracked_separately():
    # The intent recorder must stamp _lastMessageWheelIntentMs for ANY upward
    # wheel (deltaY<0), not only the decisive deltaY<-30 sticky-unpin threshold.
    assert "let _lastMessageWheelIntentMs=-Infinity" in UI_JS
    assert "function _recentMessageWheelIntent" in UI_JS
    rec_idx = UI_JS.find("function _recordNonMessageScrollIntent")
    assert rec_idx != -1, "_recordNonMessageScrollIntent not found"
    rec = UI_JS[rec_idx: rec_idx + 1400]
    assert "e.deltaY<0) _lastMessageWheelIntentMs=performance.now()" in rec, (
        "#4970: _recordNonMessageScrollIntent must record low-delta upward wheel "
        "intent (deltaY<0) separately from the decisive deltaY<-30 unpin."
    )
    # The decisive sticky-unpin threshold must remain unchanged.
    assert "e.deltaY< -30" in rec, (
        "The existing deltaY<-30 direct sticky-unpin threshold must be preserved."
    )


def _extract_fn_body(name: str) -> str:
    idx = UI_JS.find("function " + name + "(")
    assert idx != -1, name + " not found in ui.js"
    brace = UI_JS.index("{", idx)
    depth = 0
    for i in range(brace, len(UI_JS)):
        ch = UI_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[idx : i + 1]
    raise AssertionError("unbalanced body for " + name)


def test_session_switch_reset_clears_wheel_intent():
    # #4970 review MUST-FIX 1: _resetScrollDirectionTracker() must clear the
    # low-delta wheel intent stamp so a gentle wheel in the previous chat does
    # not leak into the new chat's first post-render artifact window.
    body = _extract_fn_body("_resetScrollDirectionTracker")
    assert "_lastMessageWheelIntentMs=-Infinity" in body, (
        "_resetScrollDirectionTracker() must reset _lastMessageWheelIntentMs so "
        "stale wheel intent cannot cross a session switch."
    )


def test_stream_start_reset_clears_wheel_intent():
    # #4970 review MUST-FIX 2: _resetStreamScrollFollow() must clear the wheel
    # intent stamp so a gentle wheel just before a fresh stream cannot
    # under-suppress a no-intent artifact and silently disable live follow.
    body = _extract_fn_body("_resetStreamScrollFollow")
    assert "_lastMessageWheelIntentMs=-Infinity" in body, (
        "_resetStreamScrollFollow() must reset _lastMessageWheelIntentMs so "
        "stale wheel intent cannot cross a fresh stream start."
    )


def test_suppression_gates_on_scrollbar_drag():
    # #4970 review SHOULD-FIX 3: the post-render suppression must not fire while
    # a scrollbar drag is active — that upward scroll is real user intent.
    assert "let _scrollbarDragActive=false" in UI_JS
    listener_idx = UI_JS.find("el.addEventListener('scroll'")
    assert listener_idx != -1, "messages scroll listener not found"
    listener = UI_JS[listener_idx: listener_idx + 4000]
    assert "!_scrollbarDragActive" in listener, (
        "#4970 review: the suppression branch must reference !_scrollbarDragActive "
        "so a scrollbar-drag upward scroll inside the window is not swallowed."
    )


def test_keyboard_scroll_intent_tracked_and_gated():
    # #4970 review (greptile P1): keyboard message-pane scrolling must be recorded
    # as user intent and excluded from the post-render suppression branch.
    assert "let _lastMessageKeyScrollIntentMs=-Infinity" in UI_JS
    assert "function _recentMessageKeyScrollIntent" in UI_JS
    # A keydown listener must stamp the intent for the pane scroll keys.
    assert "_lastMessageKeyScrollIntentMs=now;" in UI_JS, (
        "a keydown handler must stamp _lastMessageKeyScrollIntentMs when the "
        "reader uses the keyboard to scroll the message pane."
    )
    assert "if(bottomDistance>120) _lastMessageScrollIntentMs=now;" in UI_JS, (
        "keyboard-driven manual-reader snapshot intent must be guarded by "
        "distance from the live tail."
    )
    assert "'PageUp'" in UI_JS and "'PageDown'" in UI_JS
    # The suppression branch must consult it.
    listener_idx = UI_JS.find("el.addEventListener('scroll'")
    listener = UI_JS[listener_idx: listener_idx + 4000]
    assert "!_recentMessageKeyScrollIntent()" in listener, (
        "#4970 review: the suppression branch must reference "
        "!_recentMessageKeyScrollIntent() so a keyboard scroll-up unpins."
    )
    # Both resets must clear the keyboard stamp (stale-state hygiene).
    assert "_lastMessageKeyScrollIntentMs=-Infinity" in _extract_fn_body(
        "_resetScrollDirectionTracker"
    )
    assert "_lastMessageKeyScrollIntentMs=-Infinity" in _extract_fn_body(
        "_resetStreamScrollFollow"
    )
    assert "_isMessageInteractiveKeyTarget" in UI_JS
    assert "button,a[href],select,summary" in UI_JS


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_space_on_transcript_button_does_not_stamp_key_scroll_intent():
    # Maintainer MUST-FIX: Space on a focused transcript control activates the
    # control; it is NOT a scroll. Because the handler is capture-phase, it must
    # inspect the target/active element directly rather than rely on defaultPrevented.
    start = UI_JS.index("const _MESSAGE_SCROLL_KEYS=new Set")
    end = UI_JS.index("  let _scrollRaf=0;", start)
    region = UI_JS[start:end]
    script = (
        "const region = " + json.dumps(region) + ";\n"
        + r"""
let _lastMessageKeyScrollIntentMs = -Infinity;
const performance = { now: () => 1234 };
const el = {
  contains(node){ return !!(node && node.inMessages); },
  matches(sel){ return sel === ':hover'; },
};
const document = {
  activeElement: null,
  _handler: null,
  addEventListener(type, fn){ if(type === 'keydown') this._handler = fn; },
};
function makeNode({tag='DIV', inMessages=true, interactive=false, editable=false}={}){
  return {
    tagName: tag,
    inMessages,
    isContentEditable: editable,
    closest(sel){ return interactive ? this : null; },
  };
}
// Run via a closure so the stamped variable lives with the extracted handler.
const env = Function('el','document','performance', `let _lastMessageKeyScrollIntentMs=-Infinity; ${region}\nreturn {handler:document._handler, get:()=>_lastMessageKeyScrollIntentMs, setActive:(n)=>{document.activeElement=n;}};`)(el, document, performance);
const button = makeNode({tag:'BUTTON', interactive:true});
env.setActive(button);
env.handler({key:' ', target:button});
const afterSpace = env.get();
const pane = makeNode({tag:'DIV', interactive:false});
env.setActive(pane);
env.handler({key:'PageUp', target:pane});
const afterPageUp = env.get();
console.log(JSON.stringify({afterSpace, afterPageUp}));
"""
    )
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout.strip())
    assert state["afterSpace"] is None, (
        "JSON serializes -Infinity as null; Space on a focused transcript button "
        "must leave the stamp at -Infinity/null."
    )
    assert state["afterPageUp"] == 1234


def test_streaming_tick_calls_fix_before_dom_writes():
    # The streaming render tick in messages.js must call _fixMobileScrollJank()
    # before _lastRenderMs=performance.now() so anchor suppression covers every
    # incremental DOM update during streaming.
    guard_idx = MESSAGES_JS.find("window._fixMobileScrollJank")
    assert guard_idx != -1, (
        "The streaming tick must call window._fixMobileScrollJank() before DOM writes."
    )
    render_idx = MESSAGES_JS.find("_lastRenderMs=performance.now()")
    assert render_idx != -1, "streaming render timestamp not found in messages.js"
    assert guard_idx < render_idx, (
        "The mobile scroll-jank guard must run before streaming DOM work begins."
    )


def test_post_process_runs_under_overflow_anchor_suppression():
    """#5338 follow-up: the async post-render settle window must stay suppressed.

    Root cause of the residual mobile "往回大跳": postProcessRenderedMessages()
    is scheduled a FRAME LATER via requestAnimationFrame(), after the synchronous
    _fixMobileScrollJank()/_suppressBrowserOverflowAnchor() guards have already
    released. It runs highlightCode()/load*Inline()/katex/mermaid, all of which
    can change the height of rows ABOVE the viewport. On mobile (overflow-anchor:
    auto) the browser's native anchor engine then compensates scrollTop a SECOND
    time in that unguarded frame, yanking an unpinned reader to another turn.

    The fix wraps every deferred post-process in _postProcessWithAnchorSuppression()
    so the browser layer stays suppressed across the post-process + one media-reflow
    frame. Desktop rests at overflow-anchor:none so the wrapper is a no-op there.
    """
    # The wrapper exists and engages the shared suppression helper.
    wrapper_idx = UI_JS.find("function _postProcessWithAnchorSuppression(")
    assert wrapper_idx != -1, (
        "_postProcessWithAnchorSuppression() wrapper must exist to keep the "
        "browser overflow-anchor layer suppressed across the deferred post-render "
        "settle window (#5338 mobile 往回大跳 follow-up)."
    )
    wrapper = UI_JS[wrapper_idx: wrapper_idx + 900]
    assert "_suppressBrowserOverflowAnchor(scroller)" in wrapper, (
        "_postProcessWithAnchorSuppression() must route through the shared "
        "_suppressBrowserOverflowAnchor() helper so desktop stays a verified no-op."
    )
    assert "postProcessRenderedMessages(container)" in wrapper, (
        "_postProcessWithAnchorSuppression() must still call the real "
        "postProcessRenderedMessages() inside the suppression window."
    )
    # Suppression is held across ONE extra frame so late media/layout reflow
    # cannot re-anchor either.
    assert "requestAnimationFrame(release)" in wrapper, (
        "_postProcessWithAnchorSuppression() must defer the suppression release "
        "by one frame so image-decode / katex / mermaid reflow is also covered."
    )

    # EVERY deferred post-process dispatch must go through the wrapper — a raw
    # requestAnimationFrame(()=>postProcessRenderedMessages(...)) would leave that
    # path unguarded and re-open the jump.
    raw_dispatch = "requestAnimationFrame(()=>postProcessRenderedMessages("
    assert raw_dispatch not in UI_JS, (
        "All deferred postProcessRenderedMessages() dispatches must go through "
        "_postProcessWithAnchorSuppression(); a raw rAF dispatch re-opens the "
        "unguarded async settle window on mobile (#5338)."
    )
    wrapped_dispatch = "requestAnimationFrame(()=>_postProcessWithAnchorSuppression("
    assert UI_JS.count(wrapped_dispatch) >= 3, (
        "All three post-render paths (fast-path cache branch, main render tail, "
        "live-tool remount) must dispatch post-process through the suppression "
        "wrapper; found fewer than 3."
    )


def test_fix_mobile_scroll_jank_defers_release_across_height_churn():
    """Root-cause fix: the anchor suppression must span the WHOLE height-churn
    window, not just one frame.

    Real mobile flight-recorder data proved the jump-back is the browser's own
    overflow-anchor engine re-compensating scrollTop in the LAYOUT phase when
    above-viewport content changes height (virtual-scroll topPad recompute,
    worklog live->settled collapse, STREAM_DONE multi-render, media/katex
    reflow). That compensation is independent of which frame our JS wrote
    scrollTop, so the previous single-rAF restore released too early -- the
    collapse/reflow lands a frame or two later, after suppression lifted.

    The fix DEFERS release: each call re-arms and cancels any pending release so
    a burst of renders shares one suppression window that only lifts after the
    layout has been quiet for two animation frames + a settle timeout. These
    source invariants encode that behavior (base-fails on the old single-rAF
    body / head-passes on the deferred-release body).
    """
    fn = _extract_fix_mobile_scroll_jank(UI_JS)
    # Re-arm must cancel a pending release so consecutive renders EXTEND, not
    # restart-and-shorten, the window.
    assert "clearTimeout(" in fn, (
        "_fixMobileScrollJank() must clearTimeout() any pending release on "
        "re-arm so a burst of renders shares one suppression window."
    )
    assert "cancelAnimationFrame(" in fn, (
        "_fixMobileScrollJank() must cancelAnimationFrame() any pending release "
        "hop on re-arm so consecutive renders extend the suppression window."
    )
    # Release must be deferred past the render frame: a settle timeout gated
    # behind animation-frame hops (not a bare single rAF).
    assert "setTimeout(" in fn, (
        "_fixMobileScrollJank() must defer the anchor restore behind a settle "
        "timeout so late layout reflow after the render cannot re-anchor."
    )
    # The module-level re-arm state the deferred release needs.
    assert "_mobileAnchorSuppressReleaseTimer" in UI_JS, (
        "The deferred-release timer handle must be tracked module-level so "
        "successive _fixMobileScrollJank() calls can cancel and re-arm it."
    )
    # Guard against regressing to the old immediate single-rAF restore: the
    # function body must NOT restore overflowAnchor inside a bare
    # requestAnimationFrame(()=>{...}) with no intervening defer.
    assert "requestAnimationFrame(()=>{\n    if(el.style.overflowAnchor==='none')" not in fn, (
        "_fixMobileScrollJank() must not restore overflow-anchor in a single "
        "immediate rAF; that released before the height-churn window closed "
        "(the residual mobile scroll jump-back this fix addresses)."
    )


def test_fix_mobile_scroll_jank_tracks_css_max_height_animations():
    """The deferred window must EXTEND across CSS max-height animations.

    A fixed rAF+timeout window (~150ms) under-covers the real churn: the
    dominant above-viewport height change during streaming is CSS max-height
    collapse/expand animations on worklog rows -- `.activity-body`
    (transition: max-height .34s), `.tool-group-body` (.3s),
    `.tool-card-detail` (.26s). Those run 260-340ms, LONGER than a fixed window,
    so overflow-anchor:none lifts mid-animation and the remaining frames of the
    animation still jump (the residual on-device report captured mid-stream).

    Fix: bind transitionrun/transitionend for `max-height` on #messages so an
    animation start HOLDS suppression (cancels the pending release) and an
    animation end schedules a short settle after the last one, bounded by a hard
    cap. This test locks in that the guard listens for the animation lifecycle.
    """
    assert "_bindMobileAnchorTransitionExtender" in UI_JS, (
        "A transition extender must exist so the suppression window tracks CSS "
        "max-height animations that outlast the fixed deferred window."
    )
    # It must listen for the START of an animation (hold) and the END (settle).
    assert "'transitionrun'" in UI_JS or '"transitionrun"' in UI_JS, (
        "The extender must listen for transitionrun so an animation beginning "
        "AFTER the base window started counting down still holds suppression."
    )
    assert "'transitionend'" in UI_JS or '"transitionend"' in UI_JS, (
        "The extender must listen for transitionend so suppression settles only "
        "after the animation actually finishes."
    )
    # It must gate on the height property, not every transition (opacity etc.).
    assert "propertyName!=='max-height'" in UI_JS or 'propertyName!=="max-height"' in UI_JS, (
        "The extender must act only on max-height transitions -- those are the "
        "ones that change above-viewport height and drive the anchor jump."
    )
    # A hard cap must exist so a looping/pathological transition can't pin
    # overflow-anchor:none forever.
    assert "_MOBILE_ANCHOR_MAX_HOLD_MS" in UI_JS, (
        "A max-hold cap must bound the transition-extended suppression so a "
        "looping transition cannot pin overflow-anchor:none indefinitely."
    )


# ── Behavioral harness: execute the REAL _fixMobileScrollJank against a mock
# #messages element + fake timers, so we test BEHAVIOR (does re-arm push the
# release out? does the hard cap fire when transitionend is missed?) instead of
# source strings. These two behaviors were the gate-cert defects on #5392:
#   1. re-arm was DEAD CODE — the computed-value predicate short-circuited the
#      2nd..Nth call of a burst because our own inline overflow-anchor:none had
#      already flipped computed to 'none', so consecutive renders never extended
#      the window (collapsed to a single first-call window).
#   2. the "hard cap" was only a guard clause inside onRun, not an independent
#      release timer — so a MISSED transitionend (interrupted animation, detached
#      element) pinned overflow-anchor:none forever, violating the #5338 contract
#      that mobile rests at 'auto'.
# Both assertions are mutation-verified: reverting either fix flips the matching
# test to FAIL (proven at authoring time by reverting each hunk).
_ANCHOR_BLOCK_START = "const _MOBILE_ANCHOR_BASE_SETTLE_MS"


def _extract_anchor_block(src: str) -> str:
    start = src.find(_ANCHOR_BLOCK_START)
    assert start != -1, "anchor suppression block not found in ui.js"
    fn_mark = src.find("window._fixMobileScrollJank=function", start)
    assert fn_mark != -1, "_fixMobileScrollJank not found"
    brace_open = src.index("{", src.index("(){", fn_mark))
    depth = 0
    end = -1
    for i in range(brace_open, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end != -1, "could not balance _fixMobileScrollJank braces"
    return src[start : src.index(";", end) + 1]


_ANCHOR_HARNESS = r"""
const block = %s;
function makeSandbox() {
  let now = 0; const timers = []; let nextId = 1;
  const schedule = (cb, delay) => { const id = nextId++; timers.push({ id, at: now + (delay||0), cb }); return id; };
  const cancel = (id) => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };
  const advance = (ms) => { const target = now + ms;
    for (;;) { const due = timers.filter(t => t.at <= target).sort((a,b)=>a.at-b.at);
      if (!due.length) break; const t = due[0]; timers.splice(timers.indexOf(t),1); now = t.at; t.cb(); }
    now = target; };
  const setTimeout = (cb,d)=>schedule(cb,d); const clearTimeout = (id)=>cancel(id);
  const requestAnimationFrame = (cb)=>schedule(cb,16); const cancelAnimationFrame = (id)=>cancel(id);
  const performance = { now: () => now };
  const listeners = {};
  const el = { style:{overflowAnchor:'auto'},
    addEventListener:(t,f)=>{(listeners[t]=listeners[t]||[]).push(f);},
    _fire:(t,e)=>{(listeners[t]||[]).forEach(f=>f(e));} };
  const _browserOverflowAnchorActive = (e)=>e.style.overflowAnchor==='auto';
  const document = { getElementById:(id)=>id==='messages'?el:null }; const win = {};
  new Function('setTimeout','clearTimeout','requestAnimationFrame','cancelAnimationFrame',
    'performance','document','window','_browserOverflowAnchorActive', block
  )(setTimeout,clearTimeout,requestAnimationFrame,cancelAnimationFrame,
    performance,document,win,_browserOverflowAnchorActive);
  return { fix: win._fixMobileScrollJank, el, advance };
}
const out = {};
// TEST 1: re-arm extends the window across consecutive calls.
{ const s = makeSandbox();
  s.fix(); const armed1 = s.el.style.overflowAnchor;
  s.advance(200); const midStill = s.el.style.overflowAnchor;
  s.fix(); s.advance(300); const afterFirstWouldRelease = s.el.style.overflowAnchor;
  s.advance(400); const afterSecond = s.el.style.overflowAnchor;
  out.rearm = { armed1, midStill, afterFirstWouldRelease, afterSecond }; }
// TEST 2: hard cap releases when transitionend is missed.
{ const s = makeSandbox();
  s.fix(); s.el._fire('transitionrun', { propertyName:'max-height' });
  s.advance(500); const heldMid = s.el.style.overflowAnchor;
  s.advance(1000); const afterCap = s.el.style.overflowAnchor;
  out.hardcap = { heldMid, afterCap }; }
console.log(JSON.stringify(out));
"""


def _run_anchor_harness() -> dict:
    block = _extract_anchor_block(UI_JS)
    script = _ANCHOR_HARNESS % json.dumps(block)
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout.strip())


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_behavioral_rearm_extends_suppression_window():
    """Consecutive _fixMobileScrollJank() calls must EXTEND the window.

    Gate-cert defect: the computed-value predicate short-circuited the 2nd call
    (our inline 'none' made computed 'none' -> predicate false -> early return),
    so re-arm never ran. Behaviorally: with the bug, the release stays anchored
    to call #1 and fires ~432ms in, so at t=500 (after a 2nd call at t=200) the
    anchor is already restored. With the fix, the 2nd call re-arms and the anchor
    is STILL 'none' at t=500. Mutation-verified: reverting the `alreadySuppressed`
    guard flips `afterFirstWouldRelease` from 'none' to ''.
    """
    r = _run_anchor_harness()["rearm"]
    assert r["armed1"] == "none", "first call must engage suppression"
    assert r["midStill"] == "none", "suppression must hold within the first window"
    assert r["afterFirstWouldRelease"] == "none", (
        "the 2nd _fixMobileScrollJank() call must RE-ARM and extend the window; "
        "if suppression released here, re-arm is dead code (the computed-value "
        "predicate short-circuited the 2nd call)."
    )
    assert r["afterSecond"] == "", (
        "suppression must eventually release to the mobile resting 'auto' after "
        "the (extended) window elapses."
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_behavioral_hard_cap_releases_when_transitionend_missed():
    """A missed transitionend must NOT pin overflow-anchor:none forever.

    Gate-cert defect: the 'hard cap' was only a guard clause inside onRun, not an
    independent release timer. If a max-height animation STARTS (transitionrun
    holds suppression) but its transitionend never fires (interrupted / element
    detached), nothing restored 'auto' -> stuck 'none', violating the #5338
    resting-value contract. With the fix, an independent max-hold timer restores
    'auto' by _MOBILE_ANCHOR_MAX_HOLD_MS. Mutation-verified: removing the
    independent timer leaves `afterCap` stuck at 'none'.
    """
    r = _run_anchor_harness()["hardcap"]
    assert r["heldMid"] == "none", (
        "transitionrun must HOLD suppression across the animation (the settle "
        "release is cancelled while animating)."
    )
    assert r["afterCap"] == "", (
        "when transitionend is missed, the independent hard-cap timer MUST still "
        "restore the mobile resting 'auto'; a guard-clause-only cap pins 'none' "
        "forever."
    )



