"""Regression locks: SSE-recovery / streaming scroll-stranding fixes.

Two distinct "jump back" classes, both verified live this session:

1. Content-grew-beneath-a-pinned-viewport (static/ui.js scroll listener):
   while streaming on a tall transcript, new content increases scrollHeight under
   a stationary viewport. The reader never scrolled (top did not move up,
   _messageUserUnpinned is false), but bottomDistance crosses the nearBottom
   threshold, so the old code fell through to `_scrollPinned=false`, killing
   auto-follow mid-stream. The follow writer and the scroll listener then fought
   frame-by-frame; the viewport stalled while content kept growing and was
   progressively stranded mid-transcript. Fix: in the `!_messageUserUnpinned`
   branch, when the viewport did NOT move up and auto-follow is on, keep the pin
   and re-snap to the true bottom instead of unpinning.

2. SSE-recovery follow-restore (static/messages.js): _handleStreamError (SSE
   drop), the Task-cancelled apply/fallback paths, and the reconnect-stream-dead
   cleanup all push/replace S.messages then renderMessages({preserveScroll:true}).
   preserveScroll's restore path keys on the pre-render snapshot's bottom-distance,
   which during a live stream can read large (content grew under a followed
   viewport), so it yanked a following reader up to a stale historical position on
   a process restart / SSE drop / cancel. Fix: capture follow-intent
   (_isMessagePaneNearBottom) BEFORE mutating S.messages, and after the recovery
   render, scrollToBottom() if the reader was following — so they see the
   interruption/cancellation notice in place instead of being thrown back into the
   transcript. Readers who had scrolled up to read history are left where they were.

These are structural source-locks (the behavioral A/B was verified live via
Playwright: OLD stranded the reader 470-580px from bottom, FIX landed at 1px).
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _compact(s: str) -> str:
    return "".join(s.split())


def test_content_grew_keeps_pin_and_resnaps_not_unpin():
    # The scroll listener's !_messageUserUnpinned branch must, when the viewport
    # did NOT move up and auto-follow is on, keep the pin and re-snap to bottom
    # rather than unpinning. Assert the new guard + re-snap call are present.
    c = _compact(UI_JS)
    assert _compact("}else if(!movedUp && window._autoScrollFollow && _scrollPinned){") in c, (
        "content-grew-beneath-pinned guard missing from the scroll listener"
    )
    # The guard body must re-snap to bottom, not set _scrollPinned=false.
    idx = c.find(_compact("else if(!movedUp && window._autoScrollFollow && _scrollPinned){"))
    assert idx != -1
    # The re-snap call must be the FIRST scroll action after the guard opens,
    # before the chain reaches any `else{ _scrollPinned=false }` fallthrough.
    after = c[idx:]
    resnap = after.find(_compact("_setMessageScrollToBottom()"))
    fallthrough = after.find(_compact("}else{_nearBottomCount=0;_scrollPinned=false"))
    assert resnap != -1, "content-grew guard must re-snap via _setMessageScrollToBottom()"
    assert fallthrough != -1, "the original unpin fallthrough should still exist for the real scroll-away case"
    assert resnap < fallthrough, (
        "the re-snap must be inside the content-grew guard body, BEFORE the unpin fallthrough"
    )


def test_sse_recovery_paths_capture_follow_intent_and_refollow():
    # All four SSE-recovery mutation points must capture follow-intent before
    # mutating S.messages and re-follow after the recovery render.
    c = _compact(MESSAGES_JS)
    for guard in (
        "_wasFollowingAtDisconnect",      # _handleStreamError (SSE drop)
        "_wasFollowingAtCancel",          # Task cancelled (embedded payload)
        "_wasFollowingAtCancelFb",        # Task cancelled (fallback)
        "_wasFollowingAtReconnectDead",   # reconnect-stream-dead cleanup
    ):
        assert guard in MESSAGES_JS, f"SSE-recovery follow-intent guard '{guard}' missing"
        # each guard is computed via _isMessagePaneNearBottom AND a sticky-unpin
        # check, then consumed by a scrollToBottom() re-follow.
        assert _compact("_isMessagePaneNearBottom(1200)") in c, (
            "follow-intent must be computed via _isMessagePaneNearBottom(1200)"
        )
        # STICKY-FOLLOW INVARIANT (maintainer must-fix): proximity alone is NOT
        # enough — a reader who manually scrolled up but stayed within 1200px sets
        # _messageUserUnpinned and must NOT be re-followed on recovery. Each guard
        # must AND in the sticky-unpin state via _isMessageReaderUnpinned (with a
        # _messageUserUnpinned fallback). Assert the guard is gated on NOT-unpinned.
        assert _compact("_isMessageReaderUnpinned") in c, (
            "follow-intent must consult the sticky _isMessageReaderUnpinned state"
        )
        assert _compact("_messageUserUnpinned") in c, (
            "follow-intent must fall back to _messageUserUnpinned when the helper is absent"
        )
    # Each guard must drive a scrollToBottom re-follow.
    for guard in ("_wasFollowingAtDisconnect", "_wasFollowingAtCancel",
                  "_wasFollowingAtCancelFb", "_wasFollowingAtReconnectDead"):
        assert _compact(f"if({guard} && typeof scrollToBottom==='function') scrollToBottom()") in c, (
            f"guard '{guard}' must re-follow via scrollToBottom() when the reader was following"
        )


def test_follow_intent_captured_before_mutation():
    # Ordering invariant: inside _handleStreamError, the _wasFollowingAtDisconnect
    # capture must appear BEFORE the terminal-error marker is inserted into
    # S.messages (capturing after the mutation would read a post-insert
    # bottom-distance and defeat the fix). Scope the search to the
    # _handleStreamError function body — messages.js has more than one
    # "Connection interrupted" push and more than one _handleStreamError
    # reference, so a global str.find() would compare across unrelated paths.
    src = MESSAGES_JS
    fn = src.find("function _handleStreamError")
    assert fn != -1, "could not locate _handleStreamError"
    body = src[fn:fn + 8000]
    cap = body.find("_wasFollowingAtDisconnect=")
    mutation = body.find("_ensureSingleTerminalStreamErrorMarker(S.messages)")
    assert cap != -1, "follow-intent capture not found in _handleStreamError"
    assert mutation != -1, "terminal-error marker insertion not found in _handleStreamError"
    assert cap < mutation, (
        "follow-intent must be captured BEFORE the terminal-error marker is "
        "inserted into S.messages"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_sticky_follow_invariant_unpinned_within_1200_is_not_refollowed():
    """Behavioral: the sticky-aware guard re-follows a genuine follower but spares
    a reader who manually scrolled up (unpinned) even while still within 1200px.

    Extracts the real _wasFollowingAtDisconnect expression from messages.js and
    evaluates it in Node under the four (nearBottom x unpinned) states, stubbing
    _isMessagePaneNearBottom / _isMessageReaderUnpinned to the scenario values.
    """
    src = MESSAGES_JS
    start = src.index("const _wasFollowingAtDisconnect=")
    # capture through the terminating semicolon of the const declaration
    end = src.index(";", src.index("_messageUserUnpinned", start))
    expr = src[start:end + 1]

    def run(near_bottom: bool, unpinned: bool) -> bool:
        harness = textwrap.dedent(f"""
            let _messageUserUnpinned = {str(unpinned).lower()};
            function _isMessagePaneNearBottom(px){{ return {str(near_bottom).lower()}; }}
            function _isMessageReaderUnpinned(){{ return {str(unpinned).lower()}; }}
            {expr}
            console.log(JSON.stringify(_wasFollowingAtDisconnect));
        """)
        res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout.strip())

    # Genuine follower (pinned, at/near bottom) → re-follow.
    assert run(near_bottom=True, unpinned=False) is True
    # MAINTAINER MUST-FIX: manually-unpinned reader STILL within 1200px → NOT re-followed.
    assert run(near_bottom=True, unpinned=True) is False, (
        "a reader who scrolled up (unpinned) within 1200px must NOT be re-followed on recovery"
    )
    # Scrolled far away (not near bottom), pinned flag irrelevant → not re-followed.
    assert run(near_bottom=False, unpinned=False) is False
    assert run(near_bottom=False, unpinned=True) is False


def test_cancel_handler_avoids_sync_scrollheight_layout():
    """#6653: the synchronous cancel SSE handler must NOT read #messages.scrollHeight
    via _isMessagePaneNearBottom inside the EventSource callback.

    On a long transcript (450+ messages) that geometry read forces a full
    synchronous layout in WKWebView, pegging the WebContent process at 100% CPU
    and freezing the native macOS app. Cancellation must read the layout-free
    authoritative follow-intent cache (_messageFollowIntentCache, synchronously
    invalidated on scroll-away in ui.js) plus the sticky-unpin flag
    (_messageUserUnpinned) instead of a forced layout or a raw _scrollPinned
    read (rAF-deferred, stale at cancel time), in both the embedded-snapshot
    and fallback paths.
    """
    src = MESSAGES_JS
    start = src.index("source.addEventListener('cancel',")
    end = src.index("for(const _runJournalEventName of", start)
    block = src[start:end]
    assert "_isMessagePaneNearBottom" not in block, (
        "cancel handler must not force a synchronous scrollHeight layout via "
        "_isMessagePaneNearBottom (#6653)"
    )
    assert "_scrollPinned" not in block, (
        "cancel handler must not read raw _scrollPinned (rAF-deferred, stale at "
        "cancel time) - use the layout-free _messageFollowIntentCache (#6653)"
    )
    assert "_messageFollowIntentCache" in block, (
        "cancel handler must read the layout-free follow-intent cache "
        "_messageFollowIntentCache (#6653)"
    )
    assert "_messageUserUnpinned" in block, (
        "cancel handler must keep the sticky-unpin guard _messageUserUnpinned (#6653)"
    )
    assert "_wasFollowingAtCancel" in block and "_wasFollowingAtCancelFb" in block, (
        "both cancel follow-intent guards (embedded-snapshot and fallback) must "
        "be present in the cancel handler (#6653)"
    )


def test_follow_intent_cache_synchronously_invalidated_before_raf():
    """#6653: the layout-free follow-intent cache must be invalidated in ui.js
    SYNCHRONOUSLY on scroll-away-up - in the scroll listener BEFORE the
    programmatic-scroll early-return and the rAF deferral, and in the keydown
    listener for upward keys. A cancel landing between a scroll-away and the
    next animation frame must never read stale pin state.
    """
    c = _compact(UI_JS)
    assert _compact("let _messageFollowIntentCache=true") in c, (
        "follow-intent cache declaration missing in ui.js (#6653)"
    )
    # Scroll listener: the invalidation must precede the programmatic-scroll
    # early-return and the rAF deferral within the same listener body.
    anchor = "if(_programmaticScroll&&(performance.now()-_programmaticScrollSetAt)>150) _programmaticScroll=false;"
    listener = UI_JS[UI_JS.index(anchor):]
    listener = listener[:listener.index("_scrollRaf=requestAnimationFrame")]
    lc = _compact(listener)
    invalidate = lc.find("_messageFollowIntentCache=false")
    prog_return = lc.find("if(_freshProgrammaticScrollActive())return;")
    assert invalidate != -1, (
        "scroll listener must synchronously invalidate the follow-intent cache "
        "on scroll-away-up (#6653)"
    )
    assert prog_return != -1 and invalidate < prog_return, (
        "cache invalidation must run BEFORE the programmatic-scroll early-return "
        "(and thus before the rAF deferral) in the scroll listener (#6653)"
    )
    # Keydown listener: upward keys invalidate synchronously (the native scroll
    # event and its rAF have not fired yet when the cancel lands).
    keydown = UI_JS[UI_JS.index("const _MESSAGE_SCROLL_KEYS=new Set(["):]
    keydown = keydown[:keydown.index("let _scrollRaf=0")]
    assert "_messageFollowIntentCache=false" in _compact(keydown), (
        "keydown listener must synchronously invalidate the follow-intent cache "
        "on keyboard scroll-away (#6653)"
    )


def test_shift_space_pages_up_invalidates_follow_intent_cache():
    """#6653 re-gate (maintainer round-2, finding 3): Shift+Space is a page-UP
    scroll-away and must synchronously invalidate the layout-free follow-intent
    cache in the keydown path, exactly like PageUp/ArrowUp/Home. Plain Space
    (page-down) and PageDown scroll TOWARD the bottom and must NOT invalidate -
    a follower paging down through a stream stays a follower.
    """
    keydown = UI_JS[UI_JS.index("const _MESSAGE_SCROLL_KEYS=new Set(["):]
    keydown = keydown[:keydown.index("let _scrollRaf=0")]
    kc = _compact(keydown)
    # The Shift+Space branch must be part of the same synchronous invalidation
    # statement as the other upward keys (PageUp/ArrowUp/Home). Compacted the
    # same way as kc (the space inside the ' ' key literal is stripped too).
    shift_space = _compact("((e.key===' '||e.key==='Spacebar')&&e.shiftKey)")
    invalidate = kc.index("_messageFollowIntentCache=false")
    # The invalidation statement: from the enclosing if( up to the cache write.
    stmt_start = kc.rindex("if(", 0, invalidate)
    stmt = kc[stmt_start:invalidate + len("_messageFollowIntentCache=false")]
    assert shift_space in stmt, (
        "Shift+Space must be wired into the synchronous cache invalidation "
        "statement (#6653)"
    )
    # Downward keys must NOT invalidate follow-intent - they scroll toward the
    # bottom, so a follower paging down through a stream stays a follower.
    assert "e.key==='PageDown'" not in stmt, (
        "PageDown must not invalidate follow-intent - it scrolls toward the "
        "bottom (#6653)"
    )
    # Plain Space (page-down) must only invalidate when gated on shiftKey.
    plain_space = _compact("(e.key===' '||e.key==='Spacebar')")
    assert stmt.count(plain_space) == 1, (
        "plain Space must appear exactly once in the invalidation statement, "
        "as the Shift+Space branch (#6653)"
    )
    assert plain_space + "&&e.shiftKey" in stmt, (
        "plain Space must not invalidate follow-intent on its own - only "
        "Shift+Space (page-up) may (#6653)"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_cancel_before_scroll_raf_does_not_follow_stale_pin():
    """#6653 behavioral: a cancel landing BEFORE the scroll listener's rAF has
    run must NOT re-follow (call scrollToBottom) a reader who just scrolled up.

    The rAF defers flipping _scrollPinned/_messageUserUnpinned to the next
    frame, so both are still stale (pinned) when the cancel fires. The
    synchronous _messageFollowIntentCache was already invalidated by the
    scroll-away, so the guard must read the cache and decline to follow.
    """
    src = MESSAGES_JS
    start = src.index("const _wasFollowingAtCancel=")
    end = src.index(";", src.index("_messageUserUnpinned", start))
    expr = src[start:end + 1]
    # Timing parity: the fallback API-fail path must use a byte-identical guard
    # so both cancel paths observe the same follow-intent.
    fb_start = src.index("const _wasFollowingAtCancelFb=")
    fb_end = src.index(";", src.index("_messageUserUnpinned", fb_start))
    fb_expr = src[fb_start:fb_end + 1]
    assert _compact(expr[expr.index("=") + 1:]) == _compact(fb_expr[fb_expr.index("=") + 1:]), (
        "both cancel paths (embedded-snapshot and fallback) must use identical "
        "follow-intent expressions (#6653)"
    )

    def run(follow_cache: bool, unpinned: bool) -> int:
        harness = textwrap.dedent(f"""
            let _messageFollowIntentCache = {str(follow_cache).lower()};
            let _scrollPinned = true;   // stale: the scroll rAF has not run yet
            let _messageUserUnpinned = {str(unpinned).lower()};
            function _isMessageReaderUnpinned(){{ return {str(unpinned).lower()}; }}
            let _scrollToBottomCalls = 0;
            function scrollToBottom(){{ _scrollToBottomCalls++; }}
            {expr}
            if(_wasFollowingAtCancel && typeof scrollToBottom==='function') scrollToBottom();
            console.log(JSON.stringify(_scrollToBottomCalls));
        """)
        res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout.strip())

    # THE #6653 WINDOW: reader scrolled up just before cancel. The scroll event
    # synchronously invalidated the cache, but the rAF (which flips
    # _scrollPinned/_messageUserUnpinned) has NOT run yet - both are stale
    # "pinned". scrollToBottom() must NOT be called.
    assert run(follow_cache=False, unpinned=False) == 0, (
        "a reader who just scrolled up (cache synchronously invalidated) must "
        "NOT be yanked back by the cancel handler even though _scrollPinned is "
        "still stale-true before the scroll rAF runs (#6653)"
    )
    # Genuine follower (cache armed, pinned) -> scrollToBottom() re-follows.
    assert run(follow_cache=True, unpinned=False) == 1
    # Sticky-unpin guard stays authoritative: cache armed but reader unpinned -> no follow.
    assert run(follow_cache=True, unpinned=True) == 0
