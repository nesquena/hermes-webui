"""Regression: an interrupted turn's notice — and any turn-final answer — is
displayed twice (#6948 / #2051).

A ``hermes chat`` run killed mid tool-call persists a single assistant message
``Operation interrupted.``; the WebUI renders that notice twice. The same
doubling was reported for a long tool run's final answer and for an image reply
carrying a ``MEDIA:`` line. The stored data is clean in every case — one message
in ``state.db``, one in the session sidecar, one in ``/api/session`` — so both
copies are produced client-side.

``renderMessages()`` folds intermediate assistant segments into the collapsed
Worklog card (#3401): the inline segment is classed
``assistant-segment-worklog-source`` (``display:none``) and its prose is
re-rendered as a ``.wl-reason`` row inside the Worklog. Two copies, one hidden —
correct, as long as the fold decision is correct.

``_assistantMessageBelongsInWorklog()`` folded on ``m._live`` before it could
reach the rule that protects the answer (``hasVisibleText && isTurnFinalAssistant
-> false``). Its caller only consults the predicate once the turn has settled (it
gates on ``!S.busy``), so an ``_live`` marker seen there is a leftover of the
live-snapshot projection rather than an ongoing stream. Folding on it hides the
answer inline and echoes it into the Worklog: the turn then holds two copies and
no visible content, which the #3875 blank-turn fail-safe reveals — by expanding
the Worklog, or by un-hiding the source segments — so the user sees the notice or
answer twice.

Three contracts are pinned here:

1. ``_assistantMessageBelongsInWorklog``: a live marker may no longer outrank the
   turn-final visible answer. Every other ``_live`` message folds as before, and
   the change can only keep a segment visible, never hide or drop one.
2. ``renderMessages``: the Worklog may echo an anchor's prose as a ``.wl-reason``
   row only when that anchor was folded into it —
   ``assistant-segment-worklog-source`` is that proof, and its ``display:none``
   is the only reason the echo is not a second visible copy. This also covers
   anchors that escape the fold for other reasons, such as ``_error`` messages,
   which are never folded.
3. The #3875 fail-safe must materialize a deferred settled Worklog (#5839) before
   judging it empty; otherwise it falls through to the last-resort un-hide,
   strips the fold off every source segment, and the deferred rows then render
   the same prose beside them.

Each contract is a structural/ownership check rather than a rendered-text
comparison, and each fails towards keeping content: the first only un-hides, the
second only suppresses an echo whose original is provably visible, the third only
builds rows earlier.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def js(*names):
    """Real source of the named ui.js functions — never a re-implementation."""
    return "\n".join(extract_function(UI_JS, name) for name in names)


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body not closed")


def run_node(script):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    assert "OK" in out.stdout, out.stdout
    return out.stdout


HARNESS = """
const assert=require('assert');
var window={};
function _messageHasReasoningPayload(m){ return !!(m&&(m.reasoning||m.reasoning_content)); }
const assistant=(content,extra)=>Object.assign({role:'assistant',content},extra||{});
"""


def test_live_marker_guard_precedes_the_live_fold_rule():
    """Contract 1, structurally: the guard sits before the live fold rule.

    Written as an added guard rather than an edit to `if(m._live) return true;`
    so the live rule keeps the exact shape and ordering pinned by
    tests/test_live_to_final_anchor_visible_order.py.
    """
    belongs = _function_body(UI_JS, "_assistantMessageBelongsInWorklog")
    guard = "if(m._live&&hasVisibleText&&isTurnFinalAssistant) return false;"
    assert guard in belongs, (
        "a settled-render live remnant must not fold the turn's final visible answer"
    )
    assert belongs.index(guard) < belongs.index("if(m._live) return true;"), (
        "the guard must be evaluated before the unconditional live fold"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_live_marker_never_folds_the_turn_final_answer():
    """The fold must not swallow the answer on a settled-render live remnant."""
    script = HARNESS + js(
        "msgContent",
        "_isAssistantEmptyPlaceholderContent",
        "_assistantMessageBelongsInWorklog",
    ) + """
const FINAL={isTurnFinalAssistant:true};
const MID={isTurnFinalAssistant:false};
const owners=new Set([0]);

// The bug: the settled turn-final answer folds on a stale live marker.
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('Operation interrupted.',{_live:true}),0,owners,undefined,FINAL),
  false,
  'a settled turn-final notice must never be folded into the Worklog (#2051)');
assert.strictEqual(
  _assistantMessageBelongsInWorklog(
    assistant('Here is the image you asked for.\\n\\nMEDIA:/tmp/example.png',{_live:true}),0,owners,undefined,FINAL),
  false,
  'a MEDIA-bearing turn-final answer must never be folded either');
// Renderer-independent: the decision must not depend on what the text looks like.
assert.strictEqual(
  _assistantMessageBelongsInWorklog(
    assistant('Here you go:\\n\\n```python\\nprint(1)\\n```',{_live:true}),0,owners,undefined,FINAL),
  false,
  'a fenced-code turn-final answer must never be folded');

// Everything else about the fold is unchanged.
// A live intermediate segment still folds — that is the Worklog's content.
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('Reading the files now.',{_live:true}),0,owners,undefined,MID),
  true,
  'a live intermediate segment must still fold into the Worklog');
// A live turn-final message with no visible text still folds (an empty tool-call
// anchor is Worklog material, not an answer).
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('',{_live:true}),0,owners,undefined,FINAL),
  true,
  'an empty live anchor must still fold');
// `(empty)` placeholder + reasoning payload is not visible text either.
assert.strictEqual(
  _assistantMessageBelongsInWorklog(
    assistant('(empty)',{_live:true,reasoning:'thought'}),0,owners,undefined,FINAL),
  true,
  'an (empty) placeholder anchor must still fold');
// Settled (no marker at all) is untouched in both directions.
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('Operation interrupted.'),0,owners,undefined,FINAL),
  false, 'settled turn-final answer: unchanged');
// (A settled message with visible text and no live/burst marker never folds —
// `if(hasVisibleText) return false;` precedes the tool-metadata rule. Pinned so a
// future change to the live-marker line cannot quietly alter this ordering.)
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('Reading the files now.'),0,owners,undefined,MID),
  false, 'settled visible intermediate without a live/burst marker: unchanged');
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('',{}),0,owners,undefined,MID),
  true, 'settled empty tool-call anchor still folds: unchanged');
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('Done.'),0,new Set(),undefined,MID),
  false, 'settled intermediate with no tool metadata: unchanged');
// An error message is still never folded.
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('**Error:** boom',{_error:true,_live:true}),0,owners,undefined,MID),
  false, 'an error message is never folded');
// Live burst/segment markers on a non-final message still fold.
assert.strictEqual(
  _assistantMessageBelongsInWorklog(assistant('mid',{_activityBurstId:2}),0,new Set(),undefined,MID),
  true, 'activity-burst anchors still fold');
console.log('OK');
"""
    run_node(script)


def test_worklog_reason_echo_requires_a_folded_anchor():
    """Contract 2: only a folded anchor may be echoed as a .wl-reason row."""
    body = _function_body(UI_JS, "renderMessages")
    assert "includeAnchorReason:!!includeAnchorReason&&!!anchorReasonHtml&&!!anchorIsWorklogSource," in body, (
        "the settled Worklog step may only re-render an anchor's prose when that "
        "anchor was folded into it (assistant-segment-worklog-source); without "
        "that proof the row is a second visible copy of a visible segment"
    )
    # The ownership fact must be computed for every entry, not only for the first
    # entry of a turn (where the group is constructed) — otherwise the append path
    # would read a stale/undefined value for later anchors in the same turn.
    hoisted = body.find(
        "const anchorIsWorklogSource=anchorRow.classList"
        "&&anchorRow.classList.contains('assistant-segment-worklog-source');"
    )
    state_lookup = body.find("let state=activityByTurn.get(anchorTurn);")
    assert hoisted != -1 and state_lookup != -1, "settled worklog append path not found"
    assert hoisted < state_lookup, (
        "anchorIsWorklogSource must be computed per activity entry (before the "
        "per-turn `state` lookup), not only inside the group-construction branch"
    )


def test_blank_turn_failsafe_materializes_deferred_rows_before_judging_empty():
    """Contract 3: a deferred (#5839) Worklog is not empty."""
    body = _function_body(UI_JS, "renderMessages")
    failsafe = body[body.find("Fail-safe invariant (#3875)"):]
    assert failsafe, "the #3875 fail-safe is missing"
    materialize = failsafe.find("data-worklog-rows-deferred")
    empty_check = failsafe.find("continue; // empty group can't help")
    assert materialize != -1, (
        "the fail-safe must materialize a deferred settled Worklog before judging "
        "it empty — otherwise it un-hides every folded segment and the deferred "
        "rows later render the same prose beside them"
    )
    assert materialize < empty_check, (
        "deferred rows must be materialized before the empty-group check"
    )
    # The last-resort un-hide must still exist: content is never lost.
    assert "seg.classList.remove('assistant-segment-worklog-source');" in failsafe, (
        "the last-resort un-hide must remain — a duplicate is preferable to a "
        "blank turn"
    )
