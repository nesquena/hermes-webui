"""Regression locks: a pure-prose assistant turn must NOT become a collapsed worklog.

Follow-up to the #4970/#5058 stream-end worklog collapse jump. Root cause of a
recurring "jump back" report: a turn that streamed ONLY prose (a long plain-text
answer, or a degeneration burst that floods the body with repeated tokens) still
projected an anchor activity scene whose `activity_rows` were all `prose`/`terminal`
— zero tool/thinking rows. The settle path promoted it to a collapsed worklog
anyway (the gate only checked `activity_rows.length`), hiding the whole answer and
shrinking the transcript by the full streamed height at STREAM_DONE → the browser
clamps a bottom-pinned viewport back to the top.

The fix adds a worklog-worthiness predicate at BOTH the generation gate
(`_anchorSceneHasWorklogWorthyRows` in messages.js, decides whether to attach a
scene at all) and the render gate (`_anchorSceneSceneHasWorklogWorthyRows` in
ui.js, defense-in-depth for already-persisted all-prose scenes). A scene is
worklog-worthy only if it has >=1 tool/thinking row or a compression lifecycle
card; pure prose is not.

These tests are BEHAVIORAL: they extract the real predicate functions from the
static JS and execute them in Node against representative scenes.
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


def _extract(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    body = _function_body(src, name)
    sig = src[start : src.index("{", start)]
    return f"{sig}{{{body}}}"


def test_gates_call_worklog_worthy_predicate():
    # Structural lock: BOTH the generation gate and the render gates must be
    # guarded by a worklog-worthiness predicate, not just `activity_rows.length`.
    # (Counting only the helper DEFINITION is the orphan-definition trap — assert
    # the CALL SITES too.)
    attach_fn = _function_body(MESSAGES_JS, "_attachProjectedAnchorSceneToLastAssistant")
    assert "_anchorSceneHasWorklogWorthyRows(scene)" in attach_fn, (
        "generation gate must require a worklog-worthy scene before attaching"
    )
    render_fn = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    assert "_anchorSceneSceneHasWorklogWorthyRows(message._anchor_activity_scene)" in render_fn, (
        "compact render gate must reject an all-prose persisted scene"
    )
    transparent_fn = _function_body(UI_JS, "_renderSettledAnchorSceneTransparentForMessage")
    assert "_anchorSceneSceneHasWorklogWorthyRows(message._anchor_activity_scene)" in transparent_fn, (
        "transparent render gate must reject an all-prose persisted scene"
    )
    # The predicate definitions exist on both sides.
    assert "function _anchorSceneHasWorklogWorthyRows" in MESSAGES_JS
    assert "function _anchorSceneSceneHasWorklogWorthyRows" in UI_JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_pure_prose_scene_is_not_worklog_worthy():
    """Pure-prose scene → false; tool/thinking/compression scene → true."""
    predicate = _extract(UI_JS, "_anchorSceneSceneHasWorklogWorthyRows")
    harness = textwrap.dedent(f"""
        {predicate}
        const out = {{}};
        // (1) The exact shape that caused the jump: long prose flood + a terminal/done row.
        out.pure_prose = _anchorSceneSceneHasWorklogWorthyRows({{
          activity_rows: [
            {{ role: 'prose', source_event_type: 'token', text: 'call\\ncall\\ncall' }},
            {{ role: 'terminal', source_event_type: 'done', text: '' }},
          ]
        }});                                  // expect false
        // (2) A real worklog: has a tool row.
        out.with_tool = _anchorSceneSceneHasWorklogWorthyRows({{
          activity_rows: [
            {{ role: 'prose', text: 'let me check' }},
            {{ role: 'tool', name: 'terminal', text: '' }},
          ]
        }});                                  // expect true
        // (3) A reasoning pass is worklog-worthy.
        out.with_thinking = _anchorSceneSceneHasWorklogWorthyRows({{
          activity_rows: [ {{ role: 'thinking', text: 'reasoning...' }} ]
        }});                                  // expect true
        // (4) A compression lifecycle card is worklog-worthy.
        out.with_compression = _anchorSceneSceneHasWorklogWorthyRows({{
          activity_rows: [ {{ role: 'lifecycle', source_event_type: 'compressed', text: '' }} ]
        }});                                  // expect true
        // (5) A bare terminal/done lifecycle is NOT worklog-worthy.
        out.bare_lifecycle = _anchorSceneSceneHasWorklogWorthyRows({{
          activity_rows: [ {{ role: 'lifecycle', source_event_type: 'done', text: '' }} ]
        }});                                  // expect false
        // (6) Empty / missing rows → false (no worklog for nothing).
        out.empty = _anchorSceneSceneHasWorklogWorthyRows({{ activity_rows: [] }});  // false
        out.no_scene = _anchorSceneSceneHasWorklogWorthyRows(null);                  // false
        console.log(JSON.stringify(out));
    """)
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["pure_prose"] is False, "pure-prose turn must NOT be promoted to a collapsed worklog"
    assert out["with_tool"] is True, "a turn with a tool row is a real worklog"
    assert out["with_thinking"] is True, "a turn with a thinking row is a real worklog"
    assert out["with_compression"] is True, "a compression lifecycle card is worklog-worthy"
    assert out["bare_lifecycle"] is False, "a bare terminal/done lifecycle is not worklog-worthy"
    assert out["empty"] is False
    assert out["no_scene"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_generation_side_predicate_matches_render_side():
    """The messages.js generation predicate must classify identically to ui.js."""
    gen = _extract(MESSAGES_JS, "_anchorSceneHasWorklogWorthyRows")
    harness = textwrap.dedent(f"""
        {gen}
        const out = {{}};
        out.pure_prose = _anchorSceneHasWorklogWorthyRows({{
          activity_rows: [ {{ role: 'prose', text: 'call\\ncall' }}, {{ role: 'terminal', source_event_type: 'done' }} ]
        }});
        out.with_tool = _anchorSceneHasWorklogWorthyRows({{
          activity_rows: [ {{ role: 'tool', name: 'x' }} ]
        }});
        console.log(JSON.stringify(out));
    """)
    res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["pure_prose"] is False
    assert out["with_tool"] is True
