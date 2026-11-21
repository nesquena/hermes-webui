"""#7653 re-gate round 15 — the browser render/dedup keys are occurrence-scoped.

The 10-27 re-review (warmup-7653-895c0edd) found item 3's browser half still
open at the render/dedup surface: the server hydration now keeps two owner-
scoped rows for the reviewer's ``echo FIRST`` then ``exit 3`` on one reused
``call_0``, but the browser's ``_anchorSceneToolRowLogicalKey`` still produced
identical ``call:call_0`` keys for different occurrences, so
``_anchorSceneRowsForRendering`` merged them back into ONE card (the second
occurrence's ``exit 3``) and the first card's verdict was gone.

This file drives the REAL ui.js functions (extracted from source, never
re-implemented) with the reviewer's hydrated two-occurrence scene:

- ``_anchorSceneToolRowLogicalKey`` must return DISTINCT keys for distinct
  occurrences of one tid (owner-scoped when the row names its owning
  assistant message, occurrence-scoped for live stream-born rows, bare tid
  for legacy rows that name neither — the design's ``(tid, assistant_msg_idx)``
  identity with the occurrence-ordinal fallback).
- ``_anchorSceneRowsForRendering(scene,{settled:true})`` — the shared source
  for BOTH settled renderers (compact worklog and transparent stream) — must
  keep two tool cards and their independent verdicts.
- The live-worklog step dedup (``_toolIdentity`` / ``_filterNewWorklogTools``)
  must keep two same-tid occurrences in one step instead of dropping the
  second card; it must still collapse a genuine duplicate (same occurrence).

RED controls: reverting the key-scoping change reds every case here; the
master baseline (no occurrence identity) is the pre-#7653 behaviour.
"""

import json
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


def run_node(script):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    assert "OK" in out.stdout, out.stdout
    return out.stdout


# The reviewer's hydrated two-occurrence scene (routes.py `_anchor_scene_tool_row`
# shape): one `call_0` owned by assistant 1 (echo FIRST, success), one owned by
# assistant 4 (exit 3, error), each carrying its occurrence ordinal.
SCENE_JS = """
const scene={final_answer:'done',activity_rows:[
  {role:'tool',kind:'tool_completed',status:'completed',tool_call_id:'call_0',name:'terminal',
   row_id:'hydrated:s:tool:call_0',
   group:{assistant_msg_idx:1},payload:{assistant_msg_idx:1,tid:'call_0',occurrence:0},
   tool:{id:'call_0',name:'terminal',args:{command:'echo FIRST'},snippet:'FIRST',done:true,is_error:false}},
  {role:'tool',kind:'tool_completed',status:'error',tool_call_id:'call_0',name:'terminal',
   row_id:'hydrated:s:tool:call_0',
   group:{assistant_msg_idx:4},payload:{assistant_msg_idx:4,tid:'call_0',occurrence:1},
   tool:{id:'call_0',name:'terminal',args:{command:'exit 3'},snippet:'3',done:true,is_error:true}},
]};
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_render_key_keeps_two_occurrences_of_one_tid_apart():
    """The 10-27 re-review's exact surface: identical `call:call_0` render keys
    collapsed the two hydrated occurrences into one card; distinct keys keep
    two cards with their own verdicts."""
    script = js(
        "_anchorSceneToolRowLogicalKey",
        "_anchorSceneMergeToolRows",
        "_anchorSceneIsSettledSuccessfulCompression",
        "_anchorSceneProseMatchesFinalAnswer",
        "_anchorSceneProseDuplicatesFinalAnswer",
        "_anchorSceneRowsForRendering",
    ) + SCENE_JS + """
const assert=require('assert');
const keys=scene.activity_rows.map(r=>_anchorSceneToolRowLogicalKey(r));
assert.strictEqual(keys[0]!==keys[1],true,
  'two occurrences of one tid must produce DISTINCT render keys, got '+JSON.stringify(keys));
assert.strictEqual(keys[0].startsWith('call:call_0'),true,keys[0]);
const rows=_anchorSceneRowsForRendering(scene,{settled:true});
const tools=rows.filter(r=>r.role==='tool');
assert.strictEqual(tools.length,2,
  'the settled renderer must keep two cards for two occurrences, got '+tools.length);
const byOwner={};
for(const r of tools){ byOwner[r.group&&r.group.assistant_msg_idx]=r; }
assert.strictEqual(byOwner[1]!==undefined,true,'owner 1 card missing');
assert.strictEqual(byOwner[4]!==undefined,true,'owner 4 card missing');
const first=byOwner[1], second=byOwner[4];
const err=(r)=>!!((r.tool&&r.tool.is_error)||(r.payload&&r.payload.is_error)||r.status==='error');
assert.strictEqual(err(first),false,'owner 1 (echo FIRST) must stay successful');
assert.strictEqual(err(second),true,'owner 4 (exit 3) must stay failed');
const firstCmd=(first.tool&&first.tool.args&&first.tool.args.command)||(first.payload&&first.payload.args&&first.payload.args.command);
const secondCmd=(second.tool&&second.tool.args&&second.tool.args.command)||(second.payload&&second.payload.args&&second.payload.args.command);
assert.strictEqual(firstCmd,'echo FIRST','owner 1 must keep its own body');
assert.strictEqual(secondCmd,'exit 3','owner 4 must keep its own body');
console.log('OK');
"""
    run_node(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_render_key_falls_back_to_occurrence_then_bare_tid():
    """The design's fallback chain: owner (durable) for hydrated rows,
    occurrence for live stream-born rows that have no owner yet, bare tid for
    legacy rows that name neither."""
    script = js("_anchorSceneToolRowLogicalKey") + """
const assert=require('assert');
const liveRow=(occ)=>({role:'tool',tool_call_id:'call_0',payload:{occurrence:occ}});
const legacyRow=()=>({role:'tool',tool_call_id:'call_0'});
const keyLive0=_anchorSceneToolRowLogicalKey(liveRow(0));
const keyLive1=_anchorSceneToolRowLogicalKey(liveRow(1));
assert.strictEqual(keyLive0!==keyLive1,true,'live occurrences must be distinct: '+keyLive0+' / '+keyLive1);
assert.strictEqual(keyLive0,'call:call_0#0',keyLive0);
assert.strictEqual(_anchorSceneToolRowLogicalKey(legacyRow()),'call:call_0','legacy keeps the bare tid key');
assert.strictEqual(_anchorSceneToolRowLogicalKey({role:'prose'}),'','non-tool rows have no tool key');
console.log('OK');
"""
    run_node(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_render_key_same_owner_still_collapses():
    """The SAME occurrence seen twice (same owner) must still collapse to one
    card — owner scoping exists to keep DIFFERENT occurrences apart, not to
    duplicate the same row."""
    script = js(
        "_anchorSceneToolRowLogicalKey",
        "_anchorSceneMergeToolRows",
        "_anchorSceneIsSettledSuccessfulCompression",
        "_anchorSceneProseMatchesFinalAnswer",
        "_anchorSceneProseDuplicatesFinalAnswer",
        "_anchorSceneRowsForRendering",
    ) + """
const assert=require('assert');
const scene={final_answer:'done',activity_rows:[
  {role:'tool',kind:'tool_completed',status:'completed',tool_call_id:'call_0',name:'terminal',
   group:{assistant_msg_idx:1},payload:{assistant_msg_idx:1,tid:'call_0',occurrence:0},
   tool:{id:'call_0',name:'terminal',args:{command:'ls'},snippet:'ok',done:true,is_error:false}},
  {role:'tool',kind:'tool_completed',status:'completed',tool_call_id:'call_0',name:'terminal',
   group:{assistant_msg_idx:1},payload:{assistant_msg_idx:1,tid:'call_0',occurrence:0},
   tool:{id:'call_0',name:'terminal',args:{command:'ls'},snippet:'ok',done:true,is_error:false}},
]};
const rows=_anchorSceneRowsForRendering(scene,{settled:true});
assert.strictEqual(rows.filter(r=>r.role==='tool').length,1,
  'the same occurrence seen twice must still collapse to one card');
console.log('OK');
"""
    run_node(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_worklog_step_keeps_two_occurrences_and_dedups_duplicates():
    """The live-worklog step dedup (_toolIdentity / _filterNewWorklogTools):
    two occurrences of one tid in a step are two cards; a genuine duplicate
    (same tid + same owner) is one card."""
    script = js("_toolIdentity", "_toolDisclosureIdentity", "_toolCardOwnerIndex",
                "_toolCardOccurrence", "_filterNewWorklogTools") + """
const assert=require('assert');
const mk=(owner,occ)=>({name:'terminal',tid:'call_0',assistant_msg_idx:owner,occurrence:occ,
  args:{command:'ls'},snippet:'ok'});
const cards=[mk(1,0),mk(4,1)];
const seen=new Set();
const kept=_filterNewWorklogTools(cards,seen);
assert.strictEqual(kept.length,2,
  'two occurrences of one tid must BOTH survive the step dedup, got '+kept.length);
assert.strictEqual(seen.size,2,'both occurrences register in seenTools');
const dupSeen=new Set();
const dedup=_filterNewWorklogTools([mk(1,0),mk(1,0)],dupSeen);
assert.strictEqual(dedup.length,1,'a genuine duplicate (same occurrence) still collapses');
console.log('OK');
"""
    run_node(script)

# ---------------------------------------------------------------------------
# Settle path: the real _completeSettledAnchorSceneForTurn merges the two
# spellings of one occurrence (projected occurrence-spelled row + settled
# owner-spelled row) and keeps two occurrences of one tid apart.
# ---------------------------------------------------------------------------

MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

_SETTLE_EXTRACT_JS = """
function extractFunc(name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')'){
      depth--;
      if(depth === 0){ close = i; break; }
    }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){
      depth--;
      if(depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' body did not close');
}
"""

_SETTLE_BOOT = _SETTLE_EXTRACT_JS + """
const fs = require('fs');
const src = fs.readFileSync(SOURCE_PATH, 'utf8');
global.window = { chatActivityMode(){ return 'compact_worklog'; }, _chatActivityDisplayMode: 'compact_worklog', };
global.S = { session: {} };
eval(extractFunc('_anchorSceneCleanText'));
eval(extractFunc('_anchorSceneTextKey'));
eval(extractFunc('_anchorSceneExistingRowKey'));
eval(extractFunc('_anchorSceneRowHasLiveIdentity'));
eval(extractFunc('_anchorSceneSettleLiveRunningRow'));
eval(extractFunc('_anchorSceneRowLooksLikeFinalAnswer'));
eval(extractFunc('_anchorSceneRowTextOverlapsExisting'));
eval(extractFunc('_anchorSceneMessageRowsHaveThinking'));
eval(extractFunc('_anchorSceneActiveMode'));
eval(extractFunc('_anchorSceneRowDisplayHintForMode'));
function _anchorSceneFinalAnswerText(message){ return message && (message.final_answer || message.content || ''); }
function _anchorSceneRowsByMessageIndex(){ return new Map(); }
function _anchorSceneMessageRef(message){ return String(message && message.id || ''); }
function _anchorSceneTurnDurationForSettlement(_lastAsst, base){ return (base && base.turn_duration) ? base.turn_duration : 0; }
eval(extractFunc('_completeSettledAnchorSceneForTurn'));
""".replace(
    "SOURCE_PATH",
    json.dumps(str(ROOT / "static" / "messages.js")),
)


def _run_settle(script):
    assert NODE, "node not on PATH"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settle_keeps_two_occurrences_and_bridges_each_twin():
    """The reviewer's echo FIRST / exit 3 turn, settled: the projected scene
    holds two occurrence-spelled rows (call_0#0, call_0#1) and the transcript
    rebuild holds two owner-spelled rows (call_0@1, call_0@4). The settle path
    must keep exactly TWO cards — each owner row bridges to its own occurrence
    twin instead of appending a third/fourth row."""
    script = _SETTLE_BOOT + """
const messages = [
  {role:'user', content:'go', id:'user-1'},
  {role:'assistant', content:'first', id:'asst-1'},
  {role:'assistant', content:'second', id:'asst-4'},
  {role:'assistant', content:'Final answer', id:'asst-final'},
];
function _anchorSceneRowsByMessageIndex(){
  return new Map([
    [1, [{role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'completed',
          tool_call_id:'call_0', row_id:'settled:1', group:{assistant_msg_idx:1},
          tool:{id:'call_0', name:'terminal', args:{command:'echo FIRST'}, snippet:'FIRST', done:true, is_error:false},
          payload:{assistant_msg_idx:1, tid:'call_0', snippet:'FIRST'}}]],
    [4, [{role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'error',
          tool_call_id:'call_0', row_id:'settled:4', group:{assistant_msg_idx:4},
          tool:{id:'call_0', name:'terminal', args:{command:'exit 3'}, snippet:'3', done:true, is_error:true},
          payload:{assistant_msg_idx:4, tid:'call_0', snippet:'3'}}]],
  ]);
}
const projectedScene = {
  mode:'compact_worklog', final_answer:'Final answer',
  identity:{source_message_refs:['asst-final']}, lifecycle:{},
  activity_rows:[
    {role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'completed',
     tool_call_id:'call_0', row_id:'live:0', local_id:'live-tool:0',
     payload:{tid:'call_0', occurrence:0, snippet:'FIRST', is_error:false}},
    {role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'error',
     tool_call_id:'call_0', row_id:'live:1', local_id:'live-tool:1',
     payload:{tid:'call_0', occurrence:1, snippet:'3', is_error:true}},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 3, projectedScene);
const tools = (scene && scene.activity_rows || []).filter(r => r.role === 'tool');
process.stdout.write(JSON.stringify(tools.map(r => ({
  row_id: r.row_id,
  key: _anchorSceneExistingRowKey(r),
  occ: (r.payload && r.payload.occurrence!==undefined) ? r.payload.occurrence : (r.tool && r.tool.occurrence!==undefined ? r.tool.occurrence : null),
  owner: (r.group && r.group.assistant_msg_idx!==undefined) ? r.group.assistant_msg_idx : null,
  err: !!((r.tool && r.tool.is_error) || (r.payload && r.payload.is_error) || r.status === 'error'),
}))));
"""
    tools = _run_settle(script)
    assert len(tools) == 2, f"expected two tool cards, got {tools}"
    owners = [t for t in tools if t["owner"] is not None]
    occs = [t for t in tools if t["occ"] is not None]
    assert len(owners) == 0 and len(occs) == 2, (
        "the bridge must keep the PROJECTED occurrence rows (the live registry "
        f"row carries the freshest body), got owners={owners} occs={occs}: {tools}"
    )
    byOcc = {t["occ"]: t for t in tools}
    assert byOcc[0]["err"] is False, f"occurrence 0 (echo FIRST) must stay successful: {tools}"
    assert byOcc[1]["err"] is True, f"occurrence 1 (exit 3) must stay failed: {tools}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settle_single_occurrence_projected_and_settled_bridge_to_one_card():
    """One occurrence, two spellings (projected #0 + settled @1): the bridge
    collapses them to ONE card."""
    script = _SETTLE_BOOT + """
const messages = [
  {role:'user', content:'go', id:'user-1'},
  {role:'assistant', content:'once', id:'asst-1'},
  {role:'assistant', content:'Final answer', id:'asst-final'},
];
function _anchorSceneRowsByMessageIndex(){
  return new Map([
    [1, [{role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'completed',
          tool_call_id:'call_0', row_id:'settled:1', group:{assistant_msg_idx:1},
          tool:{id:'call_0', name:'terminal', args:{command:'ls'}, snippet:'ok', done:true, is_error:false},
          payload:{assistant_msg_idx:1, tid:'call_0', snippet:'ok'}}]],
  ]);
}
const projectedScene = {
  mode:'compact_worklog', final_answer:'Final answer',
  identity:{source_message_refs:['asst-final']}, lifecycle:{},
  activity_rows:[
    {role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'completed',
     tool_call_id:'call_0', row_id:'live:0', local_id:'live-tool:0',
     payload:{tid:'call_0', occurrence:0, snippet:'ok', is_error:false}},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 2, projectedScene);
const tools = (scene && scene.activity_rows || []).filter(r => r.role === 'tool');
process.stdout.write(JSON.stringify(tools.map(r => ({
  row_id: r.row_id,
  key: _anchorSceneExistingRowKey(r),
  occ: (r.payload && r.payload.occurrence!==undefined) ? r.payload.occurrence : null,
  owner: (r.group && r.group.assistant_msg_idx!==undefined) ? r.group.assistant_msg_idx : null,
}))));
"""
    tools = _run_settle(script)
    assert len(tools) == 1, f"expected one tool card, got {tools}"
    assert tools[0]["occ"] == 0, f"the projected occurrence row must survive: {tools}"
