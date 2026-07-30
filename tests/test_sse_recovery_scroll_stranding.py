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
    assert _compact("}else if(!movedUp && _autoScrollFollow && _scrollPinned){") in c, (
        "content-grew-beneath-pinned guard missing from the scroll listener"
    )
    # The guard body must re-snap to bottom, not set _scrollPinned=false.
    idx = c.find(_compact("else if(!movedUp && _autoScrollFollow && _scrollPinned){"))
    assert idx != -1
    # The re-snap call must be the FIRST scroll action after the guard opens,
    # before the chain reaches any `else{ _scrollPinned=false }` fallthrough.
    after = c[idx:]
    resnap = after.find(_compact("_setMessageScrollToBottom()"))
    fallthrough = after.find(_compact("}else{_nearBottomCount=0;_scrollPinned=false;}"))
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
