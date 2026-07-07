"""Regression tests for the cold session-switch-back landing scroll jump.

Root cause (reproduced on desktop Chromium AND mobile-emulation, matching
real-device telemetry): switching to another session runs loadSession(), which
SYNCHRONOUSLY replaces #msgInner with a single centered "Loading conversation..."
placeholder div BEFORE the metadata + messages fetch. That collapses #msgInner
from the prior transcript's rows down to ~one row, so #messages.scrollHeight drops
toward the empty-table height and the browser is FORCED to clamp #messages.scrollTop
to the new (near-zero) max -- a browser primitive, no JS writes the scrollTop
(telemetry tags it JS=none). During the ensuing network round-trip the viewport is
visibly stranded at the top; then renderMessages() rebuilds and scrolls to the
bottom, so the reader sees a large yank-to-top then snap-back (the cold switch-back
landing jump).

This is the SAME wipe-collapse -> browser-clamp primitive that #5681 addressed for
the mid-stream re-render branch, but the trigger here is the loadSession()
placeholder swap, not renderMessages().

Fix: before the placeholder swap, hold the current scrollHeight as a #msgInner
min-height (_holdMessageScrollHeightForColdSwitch) so the swap does NOT collapse
scrollHeight -> no clamp. _scrollAfterMessageRender() releases the hold at its start,
once the new transcript has rendered, so the post-render scroll decision runs against
the real new-session scrollHeight and the reader still lands at the bottom.

Each behavioral test is written to FAIL on the known-buggy version (no hold: the
placeholder swap collapses scrollHeight and the browser clamps scrollTop to 0) and
PASS only on the fixed version.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
UI_JS_PATH = ROOT / "static" / "ui.js"
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"function {name}")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name} body not found")


def _run_node(source: str) -> str:
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
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


HOLD_FN = _function_body(UI_JS, "_holdMessageScrollHeightForColdSwitch")
RELEASE_FN = _function_body(UI_JS, "_releaseMessageScrollHeightHold")


def _harness(scenario_js: str) -> str:
    """Run the REAL extracted hold/release helpers against a fake #messages +
    #msgInner. A minimal fake DOM models the key behavior the browser gives for
    free: while a min-height is set on #msgInner, the scroller's scrollHeight cannot
    fall below that min-height even after the content is wiped (innerHTML='').
    """
    return (
        r"""
'use strict';
// ---- fake #msgInner whose rendered content height can change ----
const msgInner = {
  style: { minHeight: '' },
  dataset: {},
  _contentHeight: 12000,   // prior transcript rendered height
  set innerHTML(v){ this._html = v; this._contentHeight = (v === '' ? 0 : 40); },  // wipe -> ~empty
  get innerHTML(){ return this._html; },
};
// ---- fake #messages scroller. Its scrollHeight is the GREATER of the rendered
// content height and any min-height reservation on #msgInner (mirrors how a min-height
// on the inner element keeps the scroller tall even when content collapses). ----
const CLIENT_HEIGHT = 900;
const el = {
  clientHeight: CLIENT_HEIGHT,
  scrollTop: 0,
  get scrollHeight(){
    const reserved = parseInt(msgInner.style.minHeight, 10) || 0;
    return Math.max(msgInner._contentHeight, reserved);
  },
  set scrollTop(v){
    // browser clamps scrollTop into [0, scrollHeight - clientHeight]
    const maxTop = Math.max(0, this.scrollHeight - this.clientHeight);
    this._scrollTop = Math.max(0, Math.min(v, maxTop));
  },
  get scrollTop(){ return this._scrollTop || 0; },
};
function $(id){ return id === 'messages' ? el : (id === 'msgInner' ? msgInner : null); }
function requestAnimationFrame(){ /* no-op in harness: fallback timer never fires here */ }
function setTimeout(){ /* no-op: we test the synchronous hold/release, not the fallback */ }
// Model the browser re-clamp that fires when scrollHeight drops below scrollTop+clientHeight.
function browserReclamp(){ el.scrollTop = el.scrollTop; }
"""
        + HOLD_FN
        + "\n"
        + RELEASE_FN
        + "\n"
        + scenario_js
    )


def test_hold_prevents_scrollheight_collapse_on_placeholder_wipe():
    """The core fix. Reader parked mid-history in a tall transcript; a cold switch
    holds the scrollHeight, THEN the placeholder swap wipes #msgInner. With the hold,
    scrollHeight must NOT collapse (stays >= the held height) so the browser does NOT
    clamp scrollTop to 0. On the buggy version (no hold) scrollHeight would drop to ~0
    and scrollTop would clamp to 0.
    """
    scenario = r"""
// reader parked mid-history: content 12000 tall, scrolled to 6000
el.scrollTop = 6000;
const heldContentTop = el.scrollTop;
const shBefore = el.scrollHeight;
// cold switch: hold, then the loadSession placeholder swap wipes #msgInner
_holdMessageScrollHeightForColdSwitch();
msgInner.innerHTML = '';       // placeholder swap collapses rendered content to ~0
browserReclamp();              // browser would clamp scrollTop if scrollHeight fell
console.log(JSON.stringify({
  shBefore, shAfterWipe: el.scrollHeight, scrollTopAfterWipe: el.scrollTop,
  heldContentTop, minHeight: msgInner.style.minHeight
}));
"""
    m = json.loads(_run_node(_harness(scenario)))
    assert m["shAfterWipe"] >= m["shBefore"], (
        "with the hold, #messages.scrollHeight must NOT collapse when the placeholder "
        f"swap wipes #msgInner; got {m['shAfterWipe']} < {m['shBefore']}. Without the "
        "hold this collapses to ~0 and the browser clamps scrollTop to the top."
    )
    assert m["scrollTopAfterWipe"] == m["heldContentTop"], (
        "with scrollHeight held, the browser must NOT clamp scrollTop away from the "
        f"reader's position; expected {m['heldContentTop']}, got {m['scrollTopAfterWipe']}"
    )


def test_release_restores_real_scrollheight_after_render():
    """After the new transcript renders, releasing the hold must clear the min-height
    so the scroller reports the REAL new-session scrollHeight (not the held prior one),
    letting the post-render scrollToBottom land at the true bottom.
    """
    scenario = r"""
el.scrollTop = 6000;
_holdMessageScrollHeightForColdSwitch();
msgInner.innerHTML = '';
// new (shorter) session renders: content becomes 3000 tall
msgInner._contentHeight = 3000;
const shHeldBeforeRelease = el.scrollHeight;   // still >= 12000 (held)
_releaseMessageScrollHeightHold();
console.log(JSON.stringify({
  shHeldBeforeRelease, shAfterRelease: el.scrollHeight, minHeight: msgInner.style.minHeight
}));
"""
    m = json.loads(_run_node(_harness(scenario)))
    assert m["shHeldBeforeRelease"] >= 12000, "sanity: hold keeps scrollHeight tall pre-release"
    assert m["shAfterRelease"] == 3000, (
        "after release the scroller must report the real new-session scrollHeight (3000), "
        f"not the held prior height; got {m['shAfterRelease']}"
    )
    assert m["minHeight"] in ("", "0px"), (
        f"release must clear the held min-height; got {m['minHeight']!r}"
    )


def test_release_restores_previous_min_height_not_just_empty():
    """Release must restore whatever min-height was on #msgInner BEFORE the hold
    (some other feature may legitimately set one), not blindly clear it.
    """
    scenario = r"""
msgInner.style.minHeight = '500px';   // a pre-existing min-height (e.g. another guard)
el.scrollTop = 4000;
_holdMessageScrollHeightForColdSwitch();
const heldMin = msgInner.style.minHeight;
_releaseMessageScrollHeightHold();
console.log(JSON.stringify({ heldMin, restoredMin: msgInner.style.minHeight }));
"""
    m = json.loads(_run_node(_harness(scenario)))
    assert m["heldMin"] not in ("", "500px"), (
        "sanity: the hold must have replaced the pre-existing min-height with the held height"
    )
    assert m["restoredMin"] == "500px", (
        "release must restore the pre-hold min-height (500px), not clear it; "
        f"got {m['restoredMin']!r}"
    )


def test_release_is_idempotent_no_hold_active():
    """Calling release when no hold is active (the common _scrollAfterMessageRender
    path on a non-switch render) must be a safe no-op and not throw.
    """
    scenario = r"""
msgInner.style.minHeight = '';   // no hold, no dataset key
_releaseMessageScrollHeightHold();
_releaseMessageScrollHeightHold();
console.log(JSON.stringify({ minHeight: msgInner.style.minHeight, ok: true }));
"""
    m = json.loads(_run_node(_harness(scenario)))
    assert m["ok"] is True
    assert m["minHeight"] == "", "idempotent release must leave an unheld min-height untouched"


def test_scroll_after_message_render_releases_hold_first():
    """Structural + behavioral: _scrollAfterMessageRender must call the release at its
    very start, so every render path (preserveScroll / activeStreamId / bottom) drops
    the cold-switch hold before deciding the scroll position. The call is typeof-guarded
    (so sibling node harnesses that don't inject the helper don't ReferenceError), so we
    assert the guarded call appears before any of the branch keywords.
    """
    helper = _function_body(UI_JS, "_scrollAfterMessageRender")
    release_idx = helper.index("_releaseMessageScrollHeightHold()")
    # It must precede the first scroll-decision branch (preserveScroll / activeStreamId).
    preserve_idx = helper.index("if(preserveScroll)")
    stream_idx = helper.index("if(S.activeStreamId)")
    assert release_idx < preserve_idx < stream_idx, (
        "_scrollAfterMessageRender must release the cold-switch scrollHeight hold before "
        "any branch reads scrollHeight for a scroll decision (preserveScroll / activeStreamId)"
    )
    assert "typeof _releaseMessageScrollHeightHold==='function'" in helper[:release_idx + 80], (
        "the release call must be typeof-guarded so sibling node harnesses that extract "
        "this function without injecting the helper do not ReferenceError"
    )


def test_source_lock_loadsession_holds_before_placeholder_swap():
    """Structural lock in sessions.js: the hold call must appear BEFORE the
    "Loading conversation..." placeholder innerHTML assignment inside the cold
    session-switch branch, or the swap would collapse scrollHeight before the hold.
    """
    hold_idx = SESSIONS_JS.index("_holdMessageScrollHeightForColdSwitch()")
    # the placeholder swap for the switch branch is the FIRST "Loading conversation..."
    placeholder_idx = SESSIONS_JS.index("Loading conversation...")
    assert hold_idx < placeholder_idx, (
        "the scrollHeight hold must be called before the 'Loading conversation...' "
        "placeholder swap in the cold session-switch branch"
    )
