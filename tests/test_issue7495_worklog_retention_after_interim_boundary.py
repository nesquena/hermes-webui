"""Regression tests for #7495 re-gate: Compact Worklog retention after an interim boundary.

Reproduction (maintainer re-gate, Chromium): while a turn is still busy, an
``interim_assistant`` boundary collapses the live worklog group
(``closeCurrentLiveActivityGroup`` → ``_finalizeLiveActivityDisclosureGroup``
strips ``data-live-tool-call-group`` / ``data-live-activity-current``) but the
group KEEPS ``data-anchor-scene-owner="1"``. The next live scene render's
``retainedGroup`` selector matched on scene-owner + worklog key alone, so it
reused that finalized (collapsed, static) group: zero visible tools and zero
prose after the boundary, while master showed both. The Compact Worklog drive
of the stream gate also never completed on the regressed head.

Fix (Codex-verified reproduction): require ``[data-live-tool-call-group="1"]``
in the ``retainedGroup`` selector, so a finalized group is no longer retained,
the sweep removes it, and ``_anchorSceneWorklogGroup`` builds a fresh live one.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _extract_js_function(name: str) -> str:
    """Brace-match a top-level function out of ui.js (mirrors test_issue5637)."""
    marker = f"function {name}("
    start = UI_JS.index(marker)
    index = start
    depth = 0
    in_line_comment = False
    in_block_comment = False
    quote = None
    while index < len(UI_JS):
        char = UI_JS[index]
        next_char = UI_JS[index + 1] if index + 1 < len(UI_JS) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : index + 1]
        index += 1
    raise AssertionError(f"JavaScript function {name} did not close")


# Minimal compound-selector matcher: ".class[attr="v"]" chains, comma-separated
# any-of. Covers exactly the selector shapes renderLiveAnchorActivityScene uses
# for the retained group and both sweeps.
_MATCHER = r"""
function _matchesSelector(node, sel){
  const parts = sel.split(',');
  for(const part of parts){
    const tokens = part.trim().match(/\.[A-Za-z0-9_-]+|\[[^\]]*\]/g) || [];
    if(!tokens.length) continue;
    let ok = true;
    for(const tk of tokens){
      if(tk[0] === '.'){
        if(!node.classList.has(tk.slice(1))){ ok = false; break; }
      }else{
        const m = tk.match(/\[([^=\]]+)="([^"]*)"\]/);
        if(!m){ ok = false; break; }
        const v = node.getAttribute(m[1]);
        if(String(v === null || v === undefined ? '' : v) !== m[2]){ ok = false; break; }
      }
    }
    if(ok) return true;
  }
  return false;
}
"""


def _retention_harness(*, group_attrs) -> str:
    production = _extract_js_function("renderLiveAnchorActivityScene")
    attrs_js = json.dumps(group_attrs)
    body = """
const calls = { freshGroup: 0 };
function makeNode(name, attrs, classes){
  const classSet = new Set(classes || ['tool-worklog-group']);
  const attrMap = {};
  for(const k of Object.keys(attrs)) attrMap[k] = attrs[k];
  return {
    _name: name,
    _removed: false,
    classList: classSet,
    getAttribute(k){ return (k in attrMap) ? attrMap[k] : null; },
    setAttribute(k, v){ attrMap[k] = v; },
    removeAttribute(k){ delete attrMap[k]; },
    remove(){ this._removed = true; },
    contains(){ return false; },
    querySelector(){ return null; },
  };
}
const group = makeNode('group', ATTRS_JSON, ['tool-worklog-group']);
const blocks = {
  _children: [group],
  querySelector(sel){
    for(const child of this._children) if(_matchesSelector(child, sel)) return child;
    return null;
  },
  querySelectorAll(sel){
    return this._children.filter((child) => _matchesSelector(child, sel));
  },
};
const emptyState = { style: {} };
const turn = { dataset: {}, setAttribute(){}, querySelectorAll(){ return []; } };
function $(id){
  if(id === 'emptyState') return emptyState;
  if(id === 'liveAssistantTurn') return turn;
  return null;
}
function chatActivityMode(){ return 'compact_worklog'; }
function isSimplifiedToolCalling(){ return true; }
const S = { session: { session_id: 'sid-1', pending_started_at: 1 }, activeStreamId: 'stream-1' };
function _anchorSceneRowsForRendering(){ return []; }
function _assistantTurnBlocks(){ return blocks; }
function _captureWorklogDetailDisclosureState(){ return null; }
function _captureMessageScrollSnapshot(){ return {}; }
function _prepareLiveAnchorScrollRebuildGuard(){ return { readerAwayFromBottom: false }; }
function _anchorSceneWorklogGroup(){ calls.freshGroup += 1; return {}; }
function _renderAnchorSceneRowsIntoWorklog(){ return true; }
function _toolWorklogListEl(){ return null; }
function _syncToolCallGroupSummary(){}
function _restoreWorklogDetailDisclosureState(){}
function _startActivityElapsedTimer(){}
function _dedupeLiveProcessedWorklogAnchors(){}
function _moveLiveRunStatusToTurnEnd(){}
function _restoreMessageScrollSnapshotSameFrame(){}
function _restoreLiveAnchorScrollSnapshotAfterRebuild(){}
function scrollIfPinned(){}
const CSS = { escape(v){ return v; } };
renderLiveAnchorActivityScene('stream-1', { activity_rows: [] }, { sessionId: 'sid-1' });
console.log(JSON.stringify({ removed: group._removed, freshGroup: calls.freshGroup }));
"""
    return production + "\n" + _MATCHER + body.replace("ATTRS_JSON", attrs_js)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_finalized_group_is_not_retained_after_interim_boundary():
    # The interim boundary finalizes the group in place: scene-owner and
    # worklog-key survive, the LIVE flag does not. The retainedGroup selector
    # must therefore require the live flag, so the finalized group is swept and
    # a fresh live group is built (tools + later prose become visible again).
    finalized = {
        "data-anchor-scene-owner": "1",
        "data-tool-worklog-key": "live:stream-1",
        # data-live-tool-call-group deliberately ABSENT (finalized).
        "data-activity-disclosure-key": "live:stream-1",
    }
    result = json.loads(
        _run_node(_retention_harness(group_attrs=finalized))
    )
    assert result["removed"] is True, (
        "A finalized worklog group (live flag stripped by the interim_assistant "
        "boundary) must not be retained by the next live scene render — the "
        "sweep has to remove it so a fresh live group is built. Reusing it kept "
        "tools and later live prose invisible for the rest of the busy turn "
        "(#7495 re-gate)."
    )
    assert result["freshGroup"] == 1, (
        "After removing the finalized group the render must request a fresh "
        "live group from _anchorSceneWorklogGroup."
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_genuinely_live_group_is_still_retained():
    # Positive path: a group that still carries the live flag (ordinary live
    # render, no interim boundary in between) must keep being retained and
    # re-rendered in place — the fix must not rebuild the live group every tick.
    live = {
        "data-anchor-scene-owner": "1",
        "data-live-tool-call-group": "1",
        "data-live-activity-current": "1",
        "data-tool-worklog-key": "live:stream-1",
    }
    result = json.loads(
        _run_node(_retention_harness(group_attrs=live))
    )
    assert result["removed"] is False, (
        "A still-live worklog group must remain retained across live renders."
    )


def _run_node(script: str) -> str:
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()
