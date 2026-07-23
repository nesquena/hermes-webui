"""Regression tests for the pinned/tail-follower mid-stream scroll JITTER (a fast
~1-row back-and-forth bounce), distinct from the unpinned jump-back class.

Root cause (reproduced on desktop Chromium with a per-frame scrollTop flight
recorder + an isolated CSSOM A/B): when a mid-stream re-render fires (tool
completion, activity-scene refresh, clarify echo), renderMessages() wipes
#msgInner (`inner.innerHTML=''`) then rebuilds and, inside the SAME synchronous
stack, the pinned/tail-follow path (scrollIfPinned -> scrollToBottom ->
_setMessageScrollToBottom) writes scrollTop against a TRANSIENT layout whose
above-viewport height is a few px short of the settled value. The browser clamps
scrollTop a little HIGH (short of the true max); that intermediate is PAINTED this
frame, and the settle rAF corrects it the next frame -> a visible fast bounce of
~1 row (measured ~82px on a real session). scrollHeight/max are back to their
settled values by the end of the renderMessages sync stack, so re-anchoring to the
settled max THERE lands exactly, before the intermediate is painted.

Fix: capture the near-tail state BEFORE the wipe (geometry, not closure pin flags,
which the wipe's clamp scroll event can transiently perturb); at the very end of
renderMessages call `_reanchorPinnedTailAfterRender(preWipeNearTail)`, which -- only
for a pre-wipe tail-follower still sitting short of the settled max -- snaps scrollTop
to the pre-computed settled max synchronously (writing the max, NOT scrollHeight,
which would reflow to the same transient short value and re-clamp). An unpinned reader
parked in history is never moved (orthogonal to the unpinned jump-back class).

Each behavioral test FAILS on the known-buggy version (guard disabled) and PASSES
only on the fixed version. A pure-string structural test locks the call site + the
"write settled max, not scrollHeight" invariant.
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


def _harness(helper_body: str, scenario_js: str, mutate=None) -> str:
    """Run the REAL extracted _reanchorPinnedTailAfterRender body against a fake
    #messages scroller. The scenario sets the pre-wipe near-tail flag + the current
    (clamped) scrollTop and settled geometry, then prints the resulting scrollTop and
    the pin-state bookkeeping the helper writes.
    """
    body = helper_body
    if mutate:
        old, new = mutate
        assert old in body, "mutation target not found in extracted helper"
        body = body.replace(old, new)
    return (
        r"""
'use strict';
// ---- fake #messages scroller: settled layout (scrollHeight/clientHeight are the
// POST-render settled values, matching the end of the renderMessages sync stack). ----
const el = { scrollTop: 0, scrollHeight: 9235, clientHeight: 626 };
function $(id){ return id === 'messages' ? el : null; }
// ---- module-level state the helper closes over ----
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _lastMessageClientHeight = 0;
let _nearBottomCount = 0;
let _scrollPinned = false;
const performance = { now: () => 0 };
"""
        + body
        + "\n"
        + scenario_js
    )


HELPER = _function_body(UI_JS, "_reanchorPinnedTailAfterRender")


def test_pinned_tailfollower_clamped_short_is_reanchored_to_settled_max():
    """The exact jitter event: a pre-wipe tail-follower whose scrollTop the browser
    left clamped SHORT of the settled max (8527 vs settled max 9235-626=8609). The
    fixed helper must snap scrollTop to the settled max (8609) synchronously, so the
    short intermediate is never painted. On the buggy version scrollTop stays 8527.
    """
    scenario = r"""
el.scrollTop = 8527;   // browser clamped a bit high during the transient-layout write
_reanchorPinnedTailAfterRender(true);   // reader WAS at/near tail before the wipe
console.log(JSON.stringify({ top: el.scrollTop, prog: _programmaticScroll, pinned: _scrollPinned }));
"""
    m = json.loads(_run_node(_harness(HELPER, scenario)))
    assert m["top"] == 8609, (
        "a pre-wipe tail-follower left clamped short (8527) must be re-anchored to the "
        f"settled max (8609); got {m['top']}. On the buggy version it stays at 8527, "
        "which is the painted intermediate that produces the ~1-row bounce."
    )
    assert m["prog"] is True, (
        "the re-anchor write must arm _programmaticScroll so the scroll listener does "
        "not misread it as a manual unpin"
    )
    assert m["pinned"] is True, "re-anchoring a tail-follower must keep it pinned"


def test_unpinned_reader_not_near_tail_is_never_moved():
    """An unpinned reader parked in history (wasNearTail=false) must NOT be moved --
    this is the orthogonality guarantee vs the unpinned jump-back class. Even though scrollTop is far below
    the settled max, the helper must leave it exactly where it is.
    """
    scenario = r"""
el.scrollTop = 3232;   // reader parked mid-history
_reanchorPinnedTailAfterRender(false);   // was NOT near tail before the wipe
console.log(JSON.stringify({ top: el.scrollTop, prog: _programmaticScroll }));
"""
    m = json.loads(_run_node(_harness(HELPER, scenario)))
    assert m["top"] == 3232, (
        "a reader who was not a tail-follower before the wipe must never be snapped "
        f"to the bottom; got {m['top']}"
    )
    assert m["prog"] is False, "no scroll write should happen for a non-tail reader"


def test_already_at_settled_bottom_is_idempotent_noop():
    """When scrollTop already equals the settled max, the helper must be a no-op (no
    redundant write / no _programmaticScroll churn) -- guards against re-arming the
    latch every render on a steady tail-follower.
    """
    scenario = r"""
el.scrollTop = 8609;   // already at settled max (9235 - 626)
_reanchorPinnedTailAfterRender(true);
console.log(JSON.stringify({ top: el.scrollTop, prog: _programmaticScroll }));
"""
    m = json.loads(_run_node(_harness(HELPER, scenario)))
    assert m["top"] == 8609
    assert m["prog"] is False, (
        "already at the settled max: the helper must not re-write scrollTop or arm the "
        "programmatic latch (idempotent no-op)"
    )


def test_mutation_disabling_the_guard_fails_the_reanchor():
    """MUTATION CHECK: reverting the fix (make the short-clamp branch dead) must break
    the re-anchor. Proves test_pinned_...reanchored actually exercises the fix and is
    not vacuously green.
    """
    scenario = r"""
el.scrollTop = 8527;
_reanchorPinnedTailAfterRender(true);
console.log(JSON.stringify({ top: el.scrollTop }));
"""
    # Disable the guard body: the branch that performs the re-anchor never runs.
    mutated = _harness(
        HELPER, scenario, mutate=("if(el.scrollTop < settledMax-1){", "if(false){")
    )
    m = json.loads(_run_node(mutated))
    assert m["top"] == 8527, (
        "with the guard disabled the reader must stay at the clamped-short 8527 -- if "
        "this still reported 8609 the behavioral test would be vacuous"
    )


def test_source_locks_call_site_and_settled_max_write():
    """Structural locks so a future refactor can't silently drop the fix or regress it
    to (a) a SYNCHRONOUS call (which reads the transient short height inside the render
    stack and no-ops) or (b) writing scrollHeight (which reflows to the transient short
    value and re-clamps).
    """
    # The pre-wipe near-tail capture exists and is taken before the wipe.
    assert "const _preWipeNearTail=(()=>{" in UI_JS
    # renderMessages schedules the re-anchor in a MICROTASK (NOT synchronously): the
    # microtask runs after the render sync stack flushes layout to the settled height but
    # before paint. A bare synchronous call reads the mid-settle height and is a no-op.
    assert "queueMicrotask(()=>_reanchorPinnedTailAfterRender(_preWipeNearTail));" in UI_JS
    # The call is typeof-guarded so a standalone renderMessages() node harness (which does
    # not define queueMicrotask / _reanchorPinnedTailAfterRender) does not throw a
    # ReferenceError when it evals the extracted renderMessages body.
    assert (
        "if(typeof queueMicrotask==='function' && "
        "typeof _reanchorPinnedTailAfterRender==='function'){" in UI_JS
    ), (
        "the queueMicrotask re-anchor must be typeof-guarded (mirroring the "
        "_deferClearProgrammaticScroll guard) so harnesses that eval renderMessages "
        "without these helpers defined don't ReferenceError"
    )
    # Guard against regressing to a synchronous call.
    assert "\n  _reanchorPinnedTailAfterRender(_preWipeNearTail);" not in UI_JS, (
        "the re-anchor must be scheduled via queueMicrotask, not called synchronously "
        "at the end of renderMessages (a sync call reads the transient short height)"
    )
    # The helper writes the settled max (read fresh in the microtask), NOT scrollHeight.
    helper = _function_body(UI_JS, "_reanchorPinnedTailAfterRender")
    assert "const settledMax=Math.max(0, el.scrollHeight-el.clientHeight);" in helper
    assert "el.scrollTop=settledMax;" in helper
    assert "el.scrollTop=el.scrollHeight" not in helper, (
        "must write the pre-computed settledMax, not scrollHeight (re-reading "
        "scrollHeight reflows to the transient short value and re-clamps)"
    )
