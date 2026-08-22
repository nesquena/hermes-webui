"""#6999 -- virtual-window turn context must stay complete.

The first #6999 iteration fed the assistant-turn content maps
(_assistantTurnFinalVisibleContentMap / _assistantTurnVisibleContentMap) the
*render* window, ``renderVisWithIdx`` — which is
``renderHeadVisWithIdx.concat(renderTailVisWithIdx)`` around a virtualized gap
(static/ui.js:15404-15413). Those helpers derive the echo-strip context of a
rendered assistant row from ALL assistant siblings of its turn
(static/ui.js:10783-10830), so windowed-out siblings are input context even
though their own rows are not read:

* cutting through an assistant run loses the final/visible answer used to
  strip reasoning echoes -> duplicate final-answer in Worklog/Thinking;
* concatenating head+tail across an omitted user boundary merges distinct
  turns into one run -> unrelated later-turn prose becomes the echo-strip
  input.

These regressions lock the correction: renderMessages() must feed the maps
the FULL ``visWithIdx`` (never the virtual window), and the maps + echo-strip
chain must behave correctly on that full input.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _render_messages_body() -> str:
    start = UI_JS.find("function renderMessages(")
    assert start != -1, "renderMessages() not found"
    return UI_JS[start : start + 80000]


def test_render_messages_feeds_full_vis_with_idx_to_turn_maps():
    body = _render_messages_body()
    assert "_assistantTurnFinalVisibleContentMap(visWithIdx)" in body, (
        "Turn context maps must see the FULL visWithIdx so an assistant run is "
        "never cut by the virtual window."
    )
    assert "_assistantTurnVisibleContentMap(visWithIdx)" in body, (
        "Turn visible-content map must see the FULL visWithIdx."
    )
    assert "_assistantTurnFinalVisibleContentMap(renderVisWithIdx)" not in body, (
        "The virtualized render window (head+tail with a gap) must never be "
        "used as turn-context input — it cuts runs and merges distinct turns."
    )
    assert "_assistantTurnVisibleContentMap(renderVisWithIdx)" not in body


def _function_source(name: str) -> str:
    """Return the full ``function name(...) {...}`` source from static/ui.js."""
    marker = f"function {name}"
    start = UI_JS.find(marker)
    assert start != -1, f"{name} not found in static/ui.js"
    params = UI_JS.find("(", start)
    assert params != -1, f"{name} has no parameter list"
    depth = 0
    close = -1
    for idx in range(params, len(UI_JS)):
        if UI_JS[idx] == "(":
            depth += 1
        elif UI_JS[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1, f"{name} parameter list did not close"
    brace = UI_JS.find("{", close)
    assert brace != -1, f"{name} has no body"
    depth = 0
    for idx in range(brace, len(UI_JS)):
        if UI_JS[idx] == "{":
            depth += 1
        elif UI_JS[idx] == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


_FN_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")


def _collect_functions(names: list[str]) -> str:
    """Concatenate the sources of ``names`` plus every ``function X(`` dep."""
    out: dict[str, str] = {}
    stack = list(names)
    seen: set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        src = _function_source(name)
        out[name] = src
        for dep in _FN_DECL_RE.findall(src):
            if dep not in seen:
                stack.append(dep)
    return "\n".join(out.values())


_TURN_CONTEXT_NODE_BODY = r"""
// Two turns. Turn 1 is a run of THREE assistant siblings: a thinking-only
// message that exactly echoes the turn's final answer (rawIdx 1), process
// prose (rawIdx 2), and the final answer (rawIdx 3). Turn 2 follows after a
// user boundary (rawIdx 4).
const full = [
  {m: {role: 'user', content: 'Question 1'}, rawIdx: 0},
  {m: {role: 'assistant', content: '<think>The answer is 42.</think>'}, rawIdx: 1},
  {m: {role: 'assistant', content: 'I will inspect the file first.'}, rawIdx: 2},
  {m: {role: 'assistant', content: 'The answer is 42.'}, rawIdx: 3},
  {m: {role: 'user', content: 'Question 2'}, rawIdx: 4},
  {m: {role: 'assistant', content: '<think>Second thinking</think>'}, rawIdx: 5},
  {m: {role: 'assistant', content: 'Second answer.'}, rawIdx: 6},
];
// Virtualized render window: head = rawIdx 0..2, tail = rawIdx 5..6. The gap
// (rawIdx 3..4) contains turn 1's FINAL ANSWER and the user boundary that
// separates the two turns.
const gapped = [full[0], full[1], full[2], full[5], full[6]];

const finalMapFull = _assistantTurnFinalVisibleContentMap(full);
const visibleMapFull = _assistantTurnVisibleContentMap(full);
const finalMapGapped = _assistantTurnFinalVisibleContentMap(gapped);
const visibleMapGapped = _assistantTurnVisibleContentMap(gapped);

const a1FullFinal = finalMapFull.get(1) || '';
const a2FullFinal = finalMapFull.get(2) || '';
const a3FullFinal = finalMapFull.get(3) || '';
const a1FullVisible = visibleMapFull.get(1) || [];
// Echo-strip for the rendered thinking-only row (rawIdx 1) using FULL turn
// context: the thinking exactly echoes the final answer -> stripped.
const strippedFull = _worklogReasoningTextFromMessage(full[1].m, 1, new Set(), '', a1FullFinal, a1FullVisible);

// Contrast with the GAPPED window: the run is cut (final answer windowed
// out) and head+tail merge across the omitted user boundary into turn 2's
// run — rawIdx 1 would see turn 2's final answer as its strip input.
const a1GappedFinal = finalMapGapped.get(1) || '';
const a1GappedVisible = visibleMapGapped.get(1) || [];
const strippedGapped = _worklogReasoningTextFromMessage(gapped[1].m, 1, new Set(), '', a1GappedFinal, a1GappedVisible);

console.log(JSON.stringify({
  a1FullFinal,
  a2FullFinal,
  a3FullFinal,
  a1FullVisible,
  strippedFull,
  a1GappedFinal,
  a1GappedVisible,
  strippedGapped,
}));
"""


def _run_turn_context_node() -> dict:
    assert NODE, "node is required for the turn-context harness"
    fns = _collect_functions(
        [
            "_hasHiddenProcessWakeupBoundaryBefore",
            "_assistantTurnFinalVisibleContentMap",
            "_assistantTurnVisibleContentMap",
            "_assistantVisibleContentForReasoningCompare",
            "_worklogReasoningTextFromMessage",
            "_assistantReasoningPayloadText",
            "_stripVisibleAssistantEchoFromThinking",
            "_sanitizeThinkingDisplayText",
            "_normalizeThinkingEchoCompare",
            "_stripLeadingAssistantThinkingMarkup",
            "_stripXmlToolCallsDisplay",
            "_assistantAnchorSceneFinalAnswerText",
            "_isMarkerOnlyAssistantCompressionMessage",
            "_isPreservedCompressionTaskListMarkerOnlyText",
            "_isPreservedCompressionTaskListMarkerText",
            "_isAssistantEmptyPlaceholderContent",
            "_messageHasReasoningPayload",
            "msgContent",
        ]
    )
    script = f"const window = {{}};\n{fns}\n{_TURN_CONTEXT_NODE_BODY}"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_full_turn_context_propagates_final_answer_and_strips_echo():
    out = _run_turn_context_node()
    # Every assistant sibling of turn 1 — including the one whose row is
    # windowed out (rawIdx 3) — must map to the run's final visible answer.
    assert out["a1FullFinal"] == "The answer is 42."
    assert out["a2FullFinal"] == "The answer is 42."
    assert out["a3FullFinal"] == "The answer is 42."
    # The rendered thinking-only sibling sees the full visible texts of its
    # run: process prose AND the final answer.
    assert out["a1FullVisible"] == ["I will inspect the file first.", "The answer is 42."]
    # Exact echo of the final answer is stripped -> no duplicate final-answer
    # in Worklog/Thinking.
    assert out["strippedFull"] == "", (
        "Thinking that exactly echoes the turn's final answer must be "
        "suppressed when the full turn context is available."
    )


def test_gapped_window_would_cut_run_and_merge_turns():
    """Document why renderMessages must NOT use the virtual window here."""
    out = _run_turn_context_node()
    # Head+tail across the omitted user boundary merges turn 1 into turn 2's
    # run: rawIdx 1 would receive turn 2's final answer as its strip input.
    assert out["a1GappedFinal"] == "Second answer."
    # The final answer of turn 1 is not in the visible texts (windowed out),
    # so the echo is NOT stripped — the duplicate would reappear.
    assert out["strippedGapped"] != ""
