"""#7882 gate round-7 regression tests.

Finding 1 [CORE]: a hidden delegation_wakeup row between the parent's answer
and the follow-up's answer deleted the user-turn boundary from the visible
projection — both answers rendered into ONE .assistant-turn container, and
installing the follow-up's Worklog hid the parent's answer segment
(`assistant-segment-worklog-source` sets aria-hidden + hidden on every
segment with data-msg-idx < the worklog owner's rawIdx inside the same
container). Fix: turn ownership derives from the RAW messages
(_rawTurnBoundaryByRawIdx) — a hidden wakeup row still acts as a boundary;
the render loop splits the container and the turn-content maps split their
runs wherever the raw owner changes.

Finding 2 [SILENT]: gateway refresh / _ensureAllMessagesLoaded re-baselined
the visible-count pair but left S.session.visible_message_count stale; the
sidebar label reads the scalar directly, so /status-style counts reported
the pre-refresh total. Fix: adopt the validated response's
visible_message_count in both idle replacements.

Tests drive the real functions from static/ui.js under node (same
lexical-extraction pattern as tests/test_7865_regate_round3.py).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS = ROOT / "static" / "ui.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

NODE_BIN = str(NODE)


def _function_source(name: str, source: str = "") -> str:
    src = source or UI_JS.read_text(encoding="utf-8")
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    params = src.find("(", start)
    assert params != -1
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1
    brace = src.find("{", close)
    assert brace != -1
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start: idx + 1]
    raise AssertionError(f"{name} body did not close")


_FN_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")

_TEST_STUBS = r"""
// Stubs for predicates the extracted functions call. The real ones live in
// ui.js too — extracted via dependency walk — except msgContent, which the
// harness provides.
function msgContent(m){
  let c=m&&m.content||'';
  if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||p.content||'').join('').trim();
  return String(c).trim();
}
function _messageHasReasoningPayload(){return false;}
function _assistantMessageHasVisibleContent(m){return !!(msgContent(m)||'').trim();}
function _isRecoveryControlMessage(){return false;}
"""


def _collect_functions(names, source=""):
    src = source or UI_JS.read_text(encoding="utf-8")
    out = {}
    stack = list(names)
    seen = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        body = _function_source(name, src)
        out[name] = body
        for dep in _FN_DECL_RE.findall(body):
            if dep not in seen:
                stack.append(dep)
    return "\n".join(out.values())


def _run_node(script: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as f:
        f.write(script)
        path = Path(f.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(path)], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestHiddenWakeupTurnBoundary:
    """Finding 1: hidden wakeup rows still split assistant turn containers."""

    def test_boundary_map_bumps_on_hidden_wakeup(self):
        script = (
            _TEST_STUBS
            + _collect_functions(
                ["_rawTurnBoundaryByRawIdx", "_isContextCompactionMessage", "_isContextCompactionText",
                 "_isPreservedCompressionTaskListMessage", "_isRecoveryControlMessage",
                 "_isRecoveryControlMessageText"]
            )
            + r"""
const messages = [
  {role: 'user', content: 'do X'},                     // 0 boundary -1 -> 0
  {role: 'assistant', content: 'did X'},               // 1 -> owner 0
  {role: 'user', content: 'WAKEUP', _source: 'delegation_wakeup'}, // 2 -> owner 2
  {role: 'assistant', content: 'did Z'},               // 3 -> owner 2
];
const map = _rawTurnBoundaryByRawIdx(messages);
const out = {
  a0: map.get(0), a1: map.get(1), a2: map.get(2), a3: map.get(3),
};
console.log(JSON.stringify(out));
"""
        )
        out = _run_node(script)
        assert out["a0"] == 0, "first turn owned by the visible user row"
        assert out["a1"] == 0
        assert out["a2"] == 2, "hidden wakeup row IS a turn boundary"
        assert out["a3"] == 2, "follow-up answer owned by the wakeup turn"
        assert out["a1"] != out["a3"], "the two answers must NOT share a turn"

    def test_boundary_map_ignores_compaction_and_recovery_rows(self):
        script = (
            _TEST_STUBS
            + _collect_functions(
                ["_rawTurnBoundaryByRawIdx", "_isContextCompactionMessage", "_isContextCompactionText",
                 "_isPreservedCompressionTaskListMessage", "_isRecoveryControlMessage",
                 "_isRecoveryControlMessageText"]
            )
            + r"""
const messages = [
  {role: 'user', content: 'q'},                                   // 0 -> 0
  {role: 'assistant', content: 'a'},                              // 1 -> 0
  {role: 'user', content: '[context compaction happened]'},       // 2 compaction -> NOT a boundary
  {role: 'assistant', content: 'a2'},                             // 3 -> still 0
  {role: 'user', content: '[your active task list was preserved across context compression]'}, // 4 -> NOT a boundary
  {role: 'assistant', content: 'a3'},                             // 5 -> still 0
];
const map = _rawTurnBoundaryByRawIdx(messages);
console.log(JSON.stringify({a1: map.get(1), a3: map.get(3), a5: map.get(5)}));
"""
        )
        out = _run_node(script)
        assert out["a1"] == out["a3"] == out["a5"] == 0, (
            "compaction markers and preserved task lists are skip-without-reset rows — "
            "they must NOT split the assistant container"
        )

    def test_turn_content_maps_split_runs_at_hidden_wakeup(self):
        script = (
            _TEST_STUBS
            + _collect_functions(
                [
                    "_rawTurnBoundaryByRawIdx",
                    "_assistantTurnFinalVisibleContentMap",
                    "_assistantTurnVisibleContentMap",
                    "_assistantVisibleContentForReasoningCompare",
                    "_assistantAnchorSceneFinalAnswerText",
                    "_isAssistantEmptyPlaceholderContent",
                    "_isMarkerOnlyAssistantCompressionMessage",
                    "_isPreservedCompressionTaskListMarkerOnlyText",
                    "_isPreservedCompressionTaskListMarkerText",
                    "_stripLeadingAssistantThinkingMarkup",
                    "_rawTurnBoundaryByRawIdx",
                    "_isContextCompactionMessage",
                    "_isContextCompactionText",
                    "_isPreservedCompressionTaskListMessage",
                    "_isRecoveryControlMessage",
                    "_isRecoveryControlMessageText",
                ]
            )
            + r"""
const messages = [
  {role: 'user', content: 'do X'},                                  // 0
  {role: 'assistant', content: 'parent answer'},                    // 1
  {role: 'user', content: 'WAKEUP', _source: 'delegation_wakeup'},  // 2 hidden
  {role: 'assistant', content: 'follow-up answer'},                 // 3
];
const boundary = _rawTurnBoundaryByRawIdx(messages);
const visWithIdx = [
  {m: messages[1], rawIdx: 1},
  {m: messages[3], rawIdx: 3},
];
const finalMap = _assistantTurnFinalVisibleContentMap(visWithIdx, boundary);
const visibleMap = _assistantTurnVisibleContentMap(visWithIdx, boundary);
console.log(JSON.stringify({
  final1: finalMap.get(1), final3: finalMap.get(3),
  vis1: visibleMap.get(1), vis3: visibleMap.get(3),
}));
"""
        )
        out = _run_node(script)
        assert out["final1"] == "parent answer"
        assert out["final3"] == "follow-up answer"
        assert out["final1"] != out["final3"], (
            "the echo-strip context of the parent answer must not see the "
            "follow-up answer across the hidden wakeup boundary"
        )
        assert out["vis1"] == ["parent answer"]
        assert out["vis3"] == ["follow-up answer"]

    def test_maps_unchanged_without_boundary_arg(self):
        """Backward-compat: existing callers/tests that pass only visWithIdx
        keep the exact old run semantics."""
        script = (
            _TEST_STUBS
            + _collect_functions(
                [
                    "_assistantTurnFinalVisibleContentMap",
                    "_assistantTurnVisibleContentMap",
                    "_assistantVisibleContentForReasoningCompare",
                    "_assistantAnchorSceneFinalAnswerText",
                    "_isAssistantEmptyPlaceholderContent",
                    "_isMarkerOnlyAssistantCompressionMessage",
                    "_isPreservedCompressionTaskListMarkerOnlyText",
                    "_isPreservedCompressionTaskListMarkerText",
                    "_stripLeadingAssistantThinkingMarkup",
                ]
            )
            + r"""
const visWithIdx = [
  {m: {role: 'assistant', content: 'one'}, rawIdx: 1},
  {m: {role: 'assistant', content: 'two'}, rawIdx: 3},
];
const finalMap = _assistantTurnFinalVisibleContentMap(visWithIdx);
const visibleMap = _assistantTurnVisibleContentMap(visWithIdx);
console.log(JSON.stringify({final1: finalMap.get(1), final3: finalMap.get(3), vis1: visibleMap.get(1)}));
"""
        )
        out = _run_node(script)
        assert out["final1"] == "two", "no boundary map -> run continues across entries"
        assert out["final3"] == "two"
        assert out["vis1"] == ["one", "two"]

    def test_render_messages_wires_boundary_checks(self):
        """The render loop must consult the boundary map: split the container
        on a raw-owner change and feed the maps the boundary map."""
        body = _function_source("renderMessages")
        assert "_rawTurnBoundaryByRawIdx(S.messages)" in body, (
            "renderMessages must derive turn ownership from the RAW messages"
        )
        assert "_assistantTurnFinalVisibleContentMap(visWithIdx,_turnBoundaryMap)" in body
        assert "_assistantTurnVisibleContentMap(visWithIdx,_turnBoundaryMap)" in body
        assert (
            "if(prevEntry&&_turnBoundaryMap.get(prevEntry.rawIdx)!==_turnBoundaryMap.get(rawIdx)){\n        currentAssistantTurn=null;"
        ) in body, "the render loop must split the assistant container when the raw turn owner changes"

    def test_render_split_survives_virtual_gap(self):
        """The split must be computed per rendered pair (vi-1 -> vi), so a
        virtualized gap cannot stitch two turns back together."""
        body = _function_source("renderMessages")
        assert "const prevEntry=renderVisWithIdx[vi-1];" in body

    def test_mutation_bite_boundary_check_removal(self):
        """The regression is load-bearing: the boundary-driven reset must sit
        inside the render loop, after the loop header and before the
        compaction skip / user branch / container creation — removing it must
        fail this pin (proves the pin bites a revert)."""
        body = _function_source("renderMessages")
        assert "currentAssistantTurn=null;" in body
        loop = body.find("for(let vi=0;vi<renderVisWithIdx.length;vi++)")
        split = body.find("if(currentAssistantTurn&&vi>0){")
        marker_only = body.find("if(!isUser&&_isMarkerOnlyAssistantCompressionMessage(m)){")
        user_branch = body.find("if(isUser){", loop)
        container = body.find("if(!currentAssistantTurn){", loop)
        assert loop < split < marker_only < user_branch < container, (
            "boundary split must sit inside the loop before the compaction "
            "skip, the user-row reset and the container creation"
        )


class TestVisibleCountScalarSync:
    """Finding 2: the cached visible_message_count scalar must follow the
    validated reload response in both idle replacement paths."""

    def test_gateway_refresh_adopts_scalar(self):
        body = _function_source("_applyGatewayCliImportRefresh", SESSIONS_JS.read_text(encoding="utf-8"))
        assert "S.session.visible_message_count=res.session.visible_message_count;" in body, (
            "_applyGatewayCliImportRefresh must adopt the response's visible_message_count"
        )

    def test_full_reload_adopts_scalar(self):
        sessions_src = SESSIONS_JS.read_text(encoding="utf-8")
        # Anchor on _ensureAllMessagesLoaded's distinctive response fetch, then
        # find the idle re-baseline branch within it.
        fn_body = _function_source("_ensureAllMessagesLoaded", sessions_src)
        idle_idx = fn_body.find("if(!S.busy){")
        assert idle_idx != -1, "_ensureAllMessagesLoaded must have an idle re-baseline branch"
        idle_body = fn_body[idle_idx:]
        assert "S.session.visible_message_count=data.session.visible_message_count;" in idle_body, (
            "_ensureAllMessagesLoaded's idle branch must adopt the response's visible_message_count"
        )

    def test_mutation_bite_scalar_adopt_removal(self):
        sessions_src = SESSIONS_JS.read_text(encoding="utf-8")
        gateway_body = _function_source("_applyGatewayCliImportRefresh", sessions_src)
        # Removing the adoption must leave the body without it — the pin
        # checks the exact statement, so a revert bites.
        stripped = gateway_body.replace(
            "S.session.visible_message_count=res.session.visible_message_count;", ""
        )
        assert stripped != gateway_body
