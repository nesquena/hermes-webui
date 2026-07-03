"""Regression locks for #4970 round 6: stream-end worklog collapse shrink jump.

Round 7 (scoping fix): the keep-open exception must apply to ONLY the turn that
just settled, gated on a one-shot stream-id token, NOT to every historical
settled worklog on every re-render.

Round 8 (mobile unpinned fix, #MOBILESCROLL follow-up): the keep-open exception
must also cover the UNPINNED reader who scrolled up to read inside the
just-settled turn. The earlier round scoped keep-open to pinned followers only,
on the assumption that unpinned readers "preserve their viewport normally". That
holds on desktop (overflow-anchor:none + JS snapshot restore) but NOT on mobile:
the CSS resting value is overflow-anchor:auto and _fixMobileScrollJank() flips an
inline overflow-anchor:none over the settle render, so native anchoring is
suppressed during the one frame the unpinned reader needs it to absorb the
above-viewport worklog shrink — the content leaps to the top of the turn (the
"往回大跳" report). Keeping the just-settled worklog open removes the shrink for
every device/anchor-mode, so the helper now returns true for the armed turn
regardless of pin state.

These tests are BEHAVIORAL: they extract the real
`_shouldKeepSettledWorklogOpenForStreamSettle` helper plus its arm/disarm token
API from static/ui.js and execute them in Node, then drive two settled turns and
assert the second (historical) turn collapses while the just-settled one stays
open for both pin states.
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


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"function {name} body not found")


def _extract(name: str) -> str:
    """Return the full `function name(...){...}` text from ui.js."""
    marker = f"function {name}"
    start = UI_JS.index(marker)
    body = _function_body(UI_JS, name)
    sig = UI_JS[start : UI_JS.index("{", start)]
    return f"{sig}{{{body}}}"


def test_helper_and_token_threaded_through_render():
    # Structural: the helper takes a streamId and gates on the one-shot token,
    # and the call site threads the message's stream id (not a no-arg call).
    helper = _function_body(UI_JS, "_shouldKeepSettledWorklogOpenForStreamSettle")
    assert "_keepSettledWorklogOpenForStreamId" in helper
    # Round 8: the helper must NOT re-narrow to pinned-only — keep-open now covers
    # the unpinned reader too, so the pin-state gate is gone from the return.
    assert "_scrollPinned" not in helper, (
        "keep-open must apply to the just-settled turn regardless of pin state "
        "(unpinned mobile readers also get the shrink jump); do not gate on "
        "_scrollPinned/_messageUserUnpinned."
    )
    render_fn = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    assert "_shouldKeepSettledWorklogOpenForStreamSettle(streamId)" in render_fn
    assert "collapsed:!keepSettledWorklogOpen" in render_fn
    group_fn = _function_body(UI_JS, "_anchorSceneWorklogGroup")
    assert "collapsed:(opts&&opts.collapsed!==undefined)?opts.collapsed:!live" in group_fn
    # The STREAM_DONE handler arms one-shot then disarms around the render.
    assert "_armKeepSettledWorklogOpen(_settledStreamId)" in MESSAGES_JS
    assert "_disarmKeepSettledWorklogOpen()" in MESSAGES_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_just_settled_turn_stays_open_for_both_pin_states_history_collapses():
    """Only the just-settled turn stays open; it does so for pinned AND unpinned."""
    helper = _extract("_shouldKeepSettledWorklogOpenForStreamSettle")
    arm = _extract("_armKeepSettledWorklogOpen")
    disarm = _extract("_disarmKeepSettledWorklogOpen")
    harness = textwrap.dedent(f"""
        let _keepSettledWorklogOpenForStreamId=null;
        let _scrollPinned=true, _messageUserUnpinned=false;  // pinned follower
        {helper}
        {arm}
        {disarm}
        const out={{}};
        // Turn A just settled: arm A, render A (open), render historical B (collapsed), disarm.
        _armKeepSettledWorklogOpen('streamA');
        out.A_open_pinned = _shouldKeepSettledWorklogOpenForStreamSettle('streamA');   // expect true
        out.B_history = _shouldKeepSettledWorklogOpenForStreamSettle('streamB');       // expect false
        // Round 8: an UNPINNED reader of the just-settled turn must ALSO keep it open
        // (the mobile above-viewport shrink jump). This is the behavior change.
        _messageUserUnpinned=true; _scrollPinned=false;
        out.A_open_unpinned = _shouldKeepSettledWorklogOpenForStreamSettle('streamA'); // expect true
        out.B_history_unpinned = _shouldKeepSettledWorklogOpenForStreamSettle('streamB'); // expect false
        _disarmKeepSettledWorklogOpen();
        // After disarm, even A collapses on a later re-render (one-shot scope intact).
        out.A_after_disarm = _shouldKeepSettledWorklogOpenForStreamSettle('streamA');  // false
        // No token at all → never keep open.
        out.no_token = _shouldKeepSettledWorklogOpenForStreamSettle('');               // false
        console.log(JSON.stringify(out));
    """)
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["A_open_pinned"] is True, "just-settled turn must keep worklog open for pinned follower"
    assert out["A_open_unpinned"] is True, (
        "just-settled turn must ALSO keep worklog open for an unpinned reader — "
        "the above-viewport collapse shrink janks them to the turn top on mobile"
    )
    assert out["B_history"] is False, "historical settled worklog must stay collapsed (pinned)"
    assert out["B_history_unpinned"] is False, "historical settled worklog must stay collapsed (unpinned)"
    assert out["A_after_disarm"] is False, "exception must be one-shot, cleared after the render"
    assert out["no_token"] is False, "no armed stream id → never keep open"


def test_forced_open_dom_is_not_cached_while_token_armed():
    # Round 9 (#5260 gate-cert, RED): the keep-open settle render force-opens the
    # worklog; that DOM must NOT be written into _sessionHtmlCache while the token
    # is armed, or it persists the forced-open worklog across session switches /
    # restores and silently overrides a user-collapsed worklog.
    armed = _function_body(UI_JS, "_isKeepSettledWorklogOpenArmed")
    assert "_keepSettledWorklogOpenForStreamId!==null" in armed, (
        "_isKeepSettledWorklogOpenArmed() must report whether the one-shot "
        "keep-open token is currently armed."
    )
    # The cache-write guard in renderMessages must consult the armed check (via a
    # typeof-safe local) so the forced-open settle render is excluded from
    # _sessionHtmlCache.set().
    assert "typeof _isKeepSettledWorklogOpenArmed==='function'" in UI_JS, (
        "the cache guard must call _isKeepSettledWorklogOpenArmed() through a "
        "typeof check so standalone renderMessages() harnesses still work (#5260)."
    )
    assert "!_keepOpenArmed" in UI_JS, (
        "the _sessionHtmlCache population guard must include !_keepOpenArmed so the "
        "forced-open settle render DOM is not cached (#5260)."
    )
    # Anchor it to the actual cache-write site: the armed check sits on the same
    # condition as the INFLIGHT / transient-UI guards that gate the cache .set().
    cache_guard_idx = UI_JS.index("!_keepOpenArmed")
    window = UI_JS[cache_guard_idx : cache_guard_idx + 400]
    assert "_sessionHtmlCache.set(" in window, (
        "the !_keepOpenArmed guard must gate the "
        "_sessionHtmlCache.set() call, not some unrelated branch."
    )


def test_stream_done_runs_scroll_preserving_collapse_pass_after_disarm():
    # Round 9 (#5260 gate-cert, RED x2): disarming the token alone leaves the
    # forced-open worklog on screen. The first re-push collapse-rendered only the
    # NON-following path and let a pinned follower fall through to scrollToBottom()
    # — but scrollToBottom() does NOT re-render (ui.js), so the armed-open DOM
    # persisted for pinned followers. Fix: run the scroll-PRESERVING collapse pass
    # UNCONDITIONALLY right after disarm (covers both pin states), THEN scrollToBottom()
    # only for followers to re-settle at the tail. This makes keep-open genuinely
    # one-frame for everyone.
    disarm_idx = MESSAGES_JS.index("_disarmKeepSettledWorklogOpen()")
    after = MESSAGES_JS[disarm_idx : disarm_idx + 700]
    # The collapse pass must run after disarm for BOTH pin states.
    assert "_renderMessagesWithScrollSnapshot()" in after, (
        "after _disarmKeepSettledWorklogOpen() the STREAM_DONE handler must run a "
        "scroll-preserving collapse pass (_renderMessagesWithScrollSnapshot) so the "
        "forced-open worklog collapses back to the user/live state without the jump."
    )
    # The follower re-settle (scrollToBottom) must come AFTER the collapse render —
    # otherwise a pinned follower keeps the forced-open DOM (scrollToBottom does not
    # re-render). This is the exact pinned-path bug the second RED gate-cert caught.
    collapse_pos = after.index("_renderMessagesWithScrollSnapshot()")
    follow_pos = after.index("shouldFollowOnDone")
    assert collapse_pos < follow_pos, (
        "the collapse render must run BEFORE the shouldFollowOnDone scrollToBottom() "
        "so BOTH pinned and unpinned readers get the worklog collapsed; scrollToBottom() "
        "alone does not re-render and would leave a pinned follower forced-open."
    )
    assert "scrollToBottom()" in after, (
        "a pinned/near-bottom follower must scrollToBottom() after the collapse "
        "render to re-settle exactly at the tail."
    )
    # And the wrapper must exist and be scroll-preserving (capture → render →
    # restore same-frame), so the collapse height change is absorbed by the JS
    # restore rather than left to native scroll-anchoring (suppressed on mobile).
    wrapper = _function_body(UI_JS, "_renderMessagesWithScrollSnapshot")
    assert "_captureMessageScrollSnapshot()" in wrapper
    assert "_restoreMessageScrollSnapshotSameFrame" in wrapper

