"""Regression locks for #4970 round 6: stream-end worklog collapse shrink jump.

Round 7 (scoping fix): the keep-open exception must apply to ONLY the turn that
just settled, gated on a one-shot stream-id token, NOT to every historical
settled worklog on every pinned re-render. These tests are BEHAVIORAL: they
extract the real `_shouldKeepSettledWorklogOpenForPinnedFollow` helper plus its
arm/disarm token API from static/ui.js and execute them in Node, then drive two
settled turns while pinned and assert the second (historical) turn collapses.
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
    helper = _function_body(UI_JS, "_shouldKeepSettledWorklogOpenForPinnedFollow")
    assert "_keepSettledWorklogOpenForStreamId" in helper
    assert "_scrollPinned" in helper and "!_messageUserUnpinned" in helper
    render_fn = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    assert "_shouldKeepSettledWorklogOpenForPinnedFollow(streamId)" in render_fn
    assert "collapsed:!keepSettledWorklogOpen" in render_fn
    group_fn = _function_body(UI_JS, "_anchorSceneWorklogGroup")
    assert "collapsed:(opts&&opts.collapsed!==undefined)?opts.collapsed:!live" in group_fn
    # The STREAM_DONE handler arms one-shot then disarms around the render.
    assert "_armKeepSettledWorklogOpen(_settledStreamId)" in MESSAGES_JS
    assert "_disarmKeepSettledWorklogOpen()" in MESSAGES_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_only_just_settled_turn_stays_open_pinned_history_collapses():
    """Drive two settled turns while pinned; only the just-settled one stays open."""
    helper = _extract("_shouldKeepSettledWorklogOpenForPinnedFollow")
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
        out.A_open = _shouldKeepSettledWorklogOpenForPinnedFollow('streamA');      // expect true
        out.B_history = _shouldKeepSettledWorklogOpenForPinnedFollow('streamB');   // expect false
        _disarmKeepSettledWorklogOpen();
        // After disarm, even A collapses on a later pinned re-render.
        out.A_after_disarm = _shouldKeepSettledWorklogOpenForPinnedFollow('streamA'); // false
        // Unpinned reader never keeps open even for the armed turn.
        _armKeepSettledWorklogOpen('streamA'); _messageUserUnpinned=true; _scrollPinned=false;
        out.unpinned = _shouldKeepSettledWorklogOpenForPinnedFollow('streamA');     // false
        console.log(JSON.stringify(out));
    """)
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["A_open"] is True, "just-settled turn must keep worklog open for pinned follower"
    assert out["B_history"] is False, "historical settled worklog must stay collapsed while pinned"
    assert out["A_after_disarm"] is False, "exception must be one-shot, cleared after the render"
    assert out["unpinned"] is False, "unpinned reader always gets compact settled worklog"
