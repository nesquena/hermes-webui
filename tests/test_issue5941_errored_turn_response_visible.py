"""Regression tests for #5941: an errored turn's produced response stays VISIBLE.

Reported by b3nw (Discord #report-bugs). When a turn ends in a provider/agent
error but the assistant HAD already produced content (tool calls + reasoning),
the settled-scene render folded that whole turn into a *collapsed* worklog above
the error card. The user saw only the error bubble and reasonably concluded that
nothing came back — even though the real response was one click away behind the
collapsed header.

Root cause: `_renderSettledAnchorSceneForMessage` (static/ui.js) built the
settled worklog with `collapsed: !keepSettledWorklogOpen` UNCONDITIONALLY — the
errored turn's `terminal_state` (error / no_response / tool_limit_reached / ...)
was never consulted, so an errored-but-content-bearing turn collapsed exactly
like a normal completed turn.

Fix: classify the scene's `terminal_state`. When it is an error/failure state
(NOT a normal `completed`), keep the settled worklog EXPANDED by default so the
produced response stays visible, unless the user has explicitly collapsed THIS
turn's worklog (saved 'closed' disclosure state). The genuinely-empty errored
turn is unaffected: the render gate at the top of the function requires a
worklog-worthy scene (>=1 tool/thinking/compression row), so a real no_response
with zero produced content never reaches the collapse decision and still shows
only its error card.

Behavioral tests extract the real predicate from static/ui.js and execute it in
Node; structural tests lock the wiring into the render gate.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")


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


def _extract(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    body = _function_body(src, name)
    sig = src[start : src.index("{", start)]
    return f"{sig}{{{body}}}"


def test_render_gate_wires_errored_terminal_state_into_collapse_decision():
    """Structural lock: the settled-scene collapse decision must honor the
    errored-terminal-state predicate, not just keepSettledWorklogOpen."""
    body = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    # The predicate is computed for this turn's scene...
    assert "_anchorSceneHasErroredTerminalState(scene)" in body, (
        "render gate must classify the scene's terminal_state"
    )
    # ...an explicit user-collapsed worklog is still respected...
    assert "_readActivityDisclosureState(activityKey)!=='closed'" in body, (
        "default-open must not override an explicit user collapse"
    )
    # ...and it feeds the collapsed:! decision alongside keepSettledWorklogOpen.
    assert "collapsed:!(keepSettledWorklogOpen||erroredWorklogKeepOpen)" in body, (
        "errored-turn keep-open must flow into the worklog collapsed flag"
    )
    # The worklog-worthiness guard is still the entry gate (empty errored turns
    # never reach the collapse decision → they keep their bare error card).
    assert "_anchorSceneSceneHasWorklogWorthyRows(message._anchor_activity_scene)" in body
    # Predicate definition exists.
    assert "function _anchorSceneHasErroredTerminalState" in UI_JS


@pytest.mark.skipif(NODE is None, reason="node required for behavioral test")
def test_errored_terminal_states_keep_content_visible_completed_does_not():
    """Errored/failure terminal states → true (keep visible); completion / null
    / user-stop states → false (unchanged behavior)."""
    predicate = _extract(UI_JS, "_anchorSceneHasErroredTerminalState")
    # Pull the shared Set constant the predicate closes over.
    set_line_start = UI_JS.index("const _ANCHOR_SCENE_ERRORED_TERMINAL_STATES=new Set([")
    set_line_end = UI_JS.index("]);", set_line_start) + len("]);")
    set_decl = UI_JS[set_line_start:set_line_end]
    harness = textwrap.dedent(f"""
        {set_decl}
        {predicate}
        const out = {{}};
        // (1) Provider/agent errors that produced content must keep it visible.
        out.error = _anchorSceneHasErroredTerminalState({{ terminal_state: 'error' }});
        out.no_response = _anchorSceneHasErroredTerminalState({{ terminal_state: 'no_response' }});
        out.degraded = _anchorSceneHasErroredTerminalState({{ terminal_state: 'degraded' }});
        out.connection_lost = _anchorSceneHasErroredTerminalState({{ terminal_state: 'connection_lost' }});
        out.tool_limit_reached = _anchorSceneHasErroredTerminalState({{ terminal_state: 'tool_limit_reached' }});
        out.compression_exhausted = _anchorSceneHasErroredTerminalState({{ terminal_state: 'compression_exhausted' }});
        out.error_upper = _anchorSceneHasErroredTerminalState({{ terminal_state: 'ERROR' }});
        // (2) A normal completed turn must NOT be force-opened (default collapse stays).
        out.completed = _anchorSceneHasErroredTerminalState({{ terminal_state: 'completed' }});
        // (3) User-initiated stops keep their existing behavior (own dedicated cards).
        out.cancelled = _anchorSceneHasErroredTerminalState({{ terminal_state: 'cancelled' }});
        out.interrupted = _anchorSceneHasErroredTerminalState({{ terminal_state: 'interrupted' }});
        // (4) Missing / null terminal_state → false.
        out.null_state = _anchorSceneHasErroredTerminalState({{ terminal_state: null }});
        out.no_field = _anchorSceneHasErroredTerminalState({{}});
        out.no_scene = _anchorSceneHasErroredTerminalState(null);
        console.log(JSON.stringify(out));
    """)
    res = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    # Errored/failure states keep produced content visible.
    for state in (
        "error",
        "no_response",
        "degraded",
        "connection_lost",
        "tool_limit_reached",
        "compression_exhausted",
        "error_upper",
    ):
        assert out[state] is True, f"errored terminal_state '{state}' must keep response visible"
    # Normal completion + user stops + absent state are unchanged (not force-open).
    for state in ("completed", "cancelled", "interrupted", "null_state", "no_field", "no_scene"):
        assert out[state] is False, f"terminal_state '{state}' must NOT be treated as an errored keep-open"


@pytest.mark.skipif(NODE is None, reason="node required for behavioral test")
def test_errored_worklog_keep_open_decision_matrix():
    """End-to-end of the exact collapse expression the render gate evaluates:

      erroredWorklogKeepOpen = hasErroredTerminalState(scene)
                               && savedDisclosure !== 'closed'
      collapsed = !(keepSettledWorklogOpen || erroredWorklogKeepOpen)

    Encodes the #5941 invariant plus its guards:
      * errored + content-bearing (default disclosure)      -> visible (not collapsed)
      * errored + content-bearing, user explicitly collapsed -> collapsed (respected)
      * completed + content-bearing                          -> collapsed (unchanged)
    """
    predicate = _extract(UI_JS, "_anchorSceneHasErroredTerminalState")
    set_line_start = UI_JS.index("const _ANCHOR_SCENE_ERRORED_TERMINAL_STATES=new Set([")
    set_line_end = UI_JS.index("]);", set_line_start) + len("]);")
    set_decl = UI_JS[set_line_start:set_line_end]
    harness = textwrap.dedent(f"""
        {set_decl}
        {predicate}
        function decide(scene, savedDisclosure, keepSettledWorklogOpen) {{
          const erroredWorklogKeepOpen =
            _anchorSceneHasErroredTerminalState(scene) && savedDisclosure !== 'closed';
          const collapsed = !(keepSettledWorklogOpen || erroredWorklogKeepOpen);
          return collapsed;
        }}
        const out = {{}};
        // Errored turn that produced content, no explicit user disclosure → VISIBLE.
        out.errored_default = decide({{ terminal_state: 'error' }}, null, false);
        // Same errored turn, but the user explicitly collapsed it → stays collapsed.
        out.errored_user_collapsed = decide({{ terminal_state: 'error' }}, 'closed', false);
        // Errored turn the user explicitly opened → visible.
        out.errored_user_open = decide({{ terminal_state: 'no_response' }}, 'open', false);
        // A normal completed turn collapses as before.
        out.completed_default = decide({{ terminal_state: 'completed' }}, null, false);
        console.log(JSON.stringify(out));
    """)
    res = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["errored_default"] is False, (
        "an errored turn that produced content must render its worklog EXPANDED (visible) by default"
    )
    assert out["errored_user_collapsed"] is True, (
        "a worklog the user explicitly collapsed must stay collapsed even on an errored turn"
    )
    assert out["errored_user_open"] is False, "a user-opened errored worklog stays visible"
    assert out["completed_default"] is True, (
        "a normal completed turn must keep its default-collapsed worklog (no behavior change)"
    )
